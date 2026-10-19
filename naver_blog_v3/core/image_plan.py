# core/image_plan.py
# 🗺 이미지 기획(계획) 스키마 · 로컬 결정론 플래너 · 프롬프트 빌더 (2026-09-10)
# ─────────────────────────────────────────────────────────────────────────────
# 이 모듈이 존재하는 이유
#   ① 기획을 **글 만들 때 한 번** 만들어 저장하고, 이미지 채움은 **생성만** 하게 한다.
#      (예전: 채움 때마다 `image_prompt_builder.plan_infocards` 로 Claude CLI 를 한 번 더
#       호출해 카드 구성안을 만들었다 — 채움 1회 = 기획 LLM 1회 + 카드마다 비전 OCR 1회.)
#   ② 본문 이미지는 **글자 없는** 관련 이미지, 썸네일만 짧은 문구를 새긴다(사용자 결정).
#      텍스트가 많은 인포카드는 만들지 않는다 → 한글 baked 정확도 검증 비용도 사라진다.
#   ③ 슬롯 id 를 고정해 **부분 실패·재시도·재사용**이 안정적으로 맞물리게 한다.
#
# 설계 원칙
# - 순수 로컬: 이 모듈은 **어떤 LLM/네트워크/서브프로세스도 호출하지 않는다.** 로컬 계획은
#   제목·본문 소제목·카테고리만으로 결정론적으로 만든다(같은 입력 → 같은 계획).
# - 주제·계정 하드코딩 0: 어떤 프로필(정부지원·영어교육·맛집…)이든 코드 수정 없이 동작한다.
#   본문에서 뽑은 문구를 "장면 힌트"로 쓸 뿐, 도메인 어휘 사전을 두지 않는다.
# - fail-open: 공개 함수는 예외를 밖으로 던지지 않는다. 실패는 None/기본 계획으로 수렴한다.
# - 인젝션 안전: 계획 텍스트(제목·소제목·LLM 출력)는 외부 웹 텍스트가 섞일 수 있는 표면이라
#   프롬프트에 넣기 전에 제어문자·백틱·따옴표를 정규화한다.
# - 순환 임포트 금지: 이 모듈은 draft_store·draft_image_fill·content_generator 를
#   임포트하지 않는다(그쪽이 이 모듈을 임포트한다).
from __future__ import annotations

import hashlib
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from core.editorial_brief import LIMIT_MESSAGE as THUMB_LABEL_MAX_CHARS

try:  # 프로필 격리 로거(있으면 사용) — 없으면 표준 로깅 폴백.
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001 — config 미로드 환경(경량 테스트 스텁)
    import logging
    log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 계약 상수
# ─────────────────────────────────────────────────────────────────────────────
PLAN_VERSION: int = 1

SLOT_THUMB: str = "thumb"
ROLE_THUMBNAIL: str = "thumbnail"
ROLE_BODY: str = "body"

TEXT_POLICY_LABEL_ONLY: str = "label_only"   # 썸네일 기본 — 장면과 짧은 한글을 통합 생성
TEXT_POLICY_NO_TEXT: str = "no_text"         # 본문 — 글자 0
TEXT_POLICY_OVERLAY: str = "overlay"         # 선택 경로 — 배경만 생성, 한글은 우리가 합성

DEFAULT_THUMB_RATIO: str = "1:1"
DEFAULT_BODY_RATIO: str = "2:3"
DEFAULT_BODY_COUNT: int = 3

# 썸네일 라벨 길이 상한은 editorial_brief.LIMIT_MESSAGE 단일 정의를 따른다.

# 본문 슬롯 수 방어선 — 계획이 장수를 정하되 이 범위를 벗어나지 못한다(요건 5).
BODY_COUNT_ABS_MAX: int = 6
BODY_COUNT_FLOOR: int = 2


def body_slot_id(index: int) -> str:
    """본문 슬롯의 안정 id — 1부터. 재시도·재사용이 이 id 로 맞물린다."""
    return f"body_{int(index):02d}"


def local_thumb_text_enabled() -> bool:
    """썸네일 한글을 로컬 합성할지 반환한다. 기본 False는 장면·한글 통합 생성.

    True: 생성 엔진은 글자 없는 배경만 만들고 `core.thumbnail_compose` 가 한글을 그린다
          → 모델이 글자를 잘못 새기는 오류가 없어지고(받은 문구를 그대로 렌더) 편당 비전
          검증이 0회가 된다. ⚠️ 문구 자체의 오타·글꼴 미지원 글자까지 보장하지는 않는다.
    False: 모델이 짧은 한글과 장면을 함께 디자인하고 비전 검증으로 확인한다.
    """
    try:
        from config import settings as _s
        return bool(getattr(_s, "THUMB_LOCAL_TEXT", False))
    except (ImportError, AttributeError):
        log.debug("[image_plan] 썸네일 설정을 읽지 못해 통합 생성 기본값 사용", exc_info=True)
        return False


def clamp_body_count(proposed: Any, configured: Any) -> int:
    """계획이 제안한 본문 이미지 수를 **허용 범위로 조인다**(요건 5).

    · 설정이 0(본문 이미지 끄기)이면 항상 0 — 사용자 설정을 계획이 뒤집지 못한다.
    · 그 외에는 `[min(설정, BODY_COUNT_FLOOR) … min(설정+2, BODY_COUNT_ABS_MAX)]`.
      **0 은 허용하지 않는다** — 모델이 빈 계획을 내서 "채울 슬롯이 없으니 완성"이라는
      식으로 발행 게이트를 우회하는 경로를 막는다.
    """
    try:
        cfg = int(configured)
    except (TypeError, ValueError):
        cfg = DEFAULT_BODY_COUNT
    cfg = max(0, min(BODY_COUNT_ABS_MAX, cfg))
    if cfg == 0:
        return 0
    floor = min(cfg, BODY_COUNT_FLOOR)
    ceiling = min(BODY_COUNT_ABS_MAX, cfg + 2)
    try:
        value = int(proposed)
    except (TypeError, ValueError):
        return cfg
    if value <= 0:
        return floor
    return max(floor, min(ceiling, value))


# ─────────────────────────────────────────────────────────────────────────────
# 텍스트 유틸(로컬·결정론)
# ─────────────────────────────────────────────────────────────────────────────
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_MD_H2_RE = re.compile(r"^\s{0,3}#{2,3}\s+(.+?)\s*#*\s*$", re.MULTILINE)
_HTML_H2_RE = re.compile(r"<h[23][^>]*>(.*?)</h[23]>", re.IGNORECASE | re.DOTALL)
_IMG_MARKER_RE = re.compile(r"<!--\s*IMG:\s*(.+?)\s*-->", re.IGNORECASE | re.DOTALL)
_DECOR_RE = re.compile(r"[^0-9A-Za-z가-힣\s·:%~\-]+")

# 장면으로 그릴 가치가 없는 관용 소제목(범용 — 도메인 어휘가 아니라 글 구조 어휘).
_SKIP_HEADINGS: Tuple[str, ...] = (
    "faq", "자주 묻는", "정리", "한 줄 결론", "마무리", "참고", "출처", "목차",
)


def to_plain_text(content: Any) -> str:
    """본문(마크다운/HTML)을 평문으로. 태그 제거 + 공백 접기."""
    if not isinstance(content, str) or not content.strip():
        return ""
    text = _TAG_RE.sub(" ", content)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&lt;", "<").replace("&gt;", ">"))
    return _WS_RE.sub(" ", text).strip()


def sanitize(text: Any, *, limit: int = 120) -> str:
    """프롬프트에 넣을 텍스트를 인젝션 안전하게 정규화(제어문자·백틱·따옴표 제거)."""
    if not isinstance(text, str) or not text:
        return ""
    cleaned = "".join(" " if (ord(c) < 0x20 or ord(c) == 0x7F) else c for c in text)
    cleaned = cleaned.replace("`", "")
    for quote in ('"', "“", "”"):
        cleaned = cleaned.replace(quote, "'")
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    return cleaned[:limit]


def fingerprint(title: Any, content: Any) -> str:
    """계획이 어떤 원고에서 나왔는지 기록하는 소스 지문(sha1 앞 16자).

    제목 + 본문 평문(공백 접기)만 쓴다 — 같은 원고면 같은 지문, 원고가 바뀌면 달라진다.
    지문 불일치는 **계획 폐기 사유가 아니다**(본문 후처리·QA 재생성으로 늘 조금씩 바뀐다).
    "이 계획이 어느 원고 기준인지"를 사후에 알기 위한 관측 필드다.
    """
    base = f"{sanitize(title, limit=200)}\n{to_plain_text(content)[:4000]}"
    return hashlib.sha1(base.encode("utf-8", errors="replace")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def section_hints(content: Any, *, limit: int = 12) -> List[str]:
    """본문에서 '장면 힌트'가 될 문구를 결정론적으로 뽑는다.

    우선순위: ① `<!-- IMG: 캡션 -->` 마커(집필 모델이 직접 고른 이미지 자리)
             ② 마크다운/HTML 소제목(H2·H3) — FAQ·정리 같은 구조 헤딩은 제외.
    도메인 어휘 사전 없이 글 자체에서만 뽑으므로 어떤 주제·프로필에서도 동작한다.
    """
    raw = content if isinstance(content, str) else ""
    if not raw.strip():
        return []
    out: List[str] = []
    seen: set = set()

    def _add(value: str) -> None:
        text = sanitize(_DECOR_RE.sub(" ", value or ""), limit=80)
        text = _WS_RE.sub(" ", text).strip()
        if len(text) < 2:
            return
        low = text.lower()
        if any(skip in low for skip in _SKIP_HEADINGS):
            return
        if low in seen:
            return
        seen.add(low)
        out.append(text)

    for m in _IMG_MARKER_RE.finditer(raw):
        _add(m.group(1))
    for m in _MD_H2_RE.finditer(raw):
        _add(m.group(1))
    for m in _HTML_H2_RE.finditer(raw):
        _add(_TAG_RE.sub(" ", m.group(1)))
    return out[:limit]


def thumb_label_from_title(title: Any) -> str:
    """제목에서 썸네일에 새길 **짧은** 문구를 결정론적으로 만든다.

    긴 제목 전체를 렌더링 강제하지 않는다 — 핵심 어절을 공통 메시지 상한 안에 둔다.
    (앞쪽에 광역 키워드를 두는 것이 이 저장소의 제목 규칙이라 앞 어절이 곧 핵심이다.)
    """
    text = sanitize(title, limit=80)
    text = _DECOR_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text).strip()
    if not text:
        return ""
    if len(text) <= THUMB_LABEL_MAX_CHARS:
        return text
    words = text.split(" ")
    picked: List[str] = []
    for word in words:
        candidate = " ".join(picked + [word])
        if picked and len(candidate) > THUMB_LABEL_MAX_CHARS:
            break
        picked.append(word)
        if len(" ".join(picked)) >= THUMB_LABEL_MAX_CHARS:
            break
    label = " ".join(picked).strip()
    return label[:THUMB_LABEL_MAX_CHARS] if label else text[:THUMB_LABEL_MAX_CHARS]


# ─────────────────────────────────────────────────────────────────────────────
# 프롬프트 빌더 — 본문은 무문자, 썸네일만 짧은 문구
# ─────────────────────────────────────────────────────────────────────────────
# 본문 이미지의 절대 금지 — 글자·숫자·표·로고·워터마크·가짜 문서·UI.
NO_TEXT_CLAUSE: str = (
    "ABSOLUTE RULE - TEXT-FREE IMAGE: render NO text of any kind. "
    "No Korean or English letters, no numbers, no digits, no captions, labels, titles, "
    "subtitles or watermarks. No tables, charts with labels, infographics, bullet lists, "
    "logos, brand marks, signage text, price tags, forms, certificates, documents, "
    "screenshots, app or website UI, buttons or icons with text. "
    "If any glyph-like shape would appear, leave that area empty instead. "
    "이미지 안에 글자·숫자·표·로고·워터마크·가짜 문서·화면 UI 를 "
    "절대 넣지 마세요. 상황·대상·행동만 그립니다."
)

# 썸네일: 명세에 적힌 짧은 문구만. 명세 밖 사실(기관명·수치·날짜) 날조 금지.
THUMB_TEXT_CLAUSE: str = (
    "STRICT RULE: bake ONLY the one short Korean phrase quoted above. Do NOT add any other "
    "text, and do NOT invent any institution name, number, amount, percentage, date or "
    "statistic. 명세에 없는 기관명·숫자·금액·날짜를 절대 더하지 마세요."
)

_BODY_STYLE: str = (
    "Style: warm, natural editorial photography look; realistic Korean people and Korean "
    "everyday settings when people appear; soft daylight, shallow depth of field, calm and "
    "trustworthy mood; uncluttered composition with generous empty space."
)

_PREVIOUS_THUMB_STYLE: str = (
    "Style: bright, natural editorial photography with ONE concrete subject or everyday "
    "situation directly related to the article and headline. Make the subject recognizable "
    "at mobile thumbnail size. Integrate large Korean typography naturally into the scene "
    "with clear contrast and generous margins; a key word may use a contrasting color. "
    "Use natural material colors and realistic light. No fixed lower-half navy panel, "
    "no forced gold luxury palette, no generic stock 3D icons, trophies or gold coins."
)

_THUMB_STYLE: str = (
    "Style: professional documentary-style editorial photography, realistic materials, "
    "soft natural daylight, shallow depth of field and a clear foreground, midground "
    "and background. Default to ONE topic-matched photo-like scene with a relevant person, "
    "place, object or action, whichever best fits the article. Use a diagram only when the "
    "article itself explains a process or structure; never use one by default. If the article "
    "compares two options, show TWO distinct, equally prominent subjects in one coherent "
    "scene; otherwise use one focal subject with supporting context. Make the topic "
    "recognizable at mobile thumbnail size. Avoid an isolated generic object floating "
    "on an empty background, flat pictograms, generic stock 3D icons and plain text cards. "
    "Use natural material colors; no forced navy-and-gold luxury palette."
)

_THUMB_LAYOUT: str = (
    "ART DIRECTION: create a complete photographic editorial cover with a deliberate "
    "visual hierarchy. Let the scene occupy roughly the upper 55-65 percent; use the "
    "lower 35-45 percent for an oversized, heavy-weight Korean headline. Break the "
    "EXACT supplied phrase at word boundaries into 2-3 balanced lines when its length "
    "allows, preserving every word and its order; keep a short phrase on one line. "
    "Use a translucent dark gradient over the photograph behind the headline where "
    "needed, rather than a fixed opaque navy panel. Emphasize the most important word "
    "or line with ONE vivid accent color chosen from the scene, keeping other text "
    "high-contrast. Keep 5-7 percent safe margins, no cropped letters, no thin type, "
    "and enough separation between the scene and the headline. Richness must come "
    "from photographic detail, composition and typography, never invented labels, "
    "badges, subtitles, dates or decorative filler."
)

_THUMB_CLICK_REASON: str = (
    "This exact phrase should give the reader a useful reason to open the article: "
    "a question, condition, decision point or practical benefit the article can answer. "
    "Do not invent urgency or facts, and do not merely repeat the title."
)


def build_body_prompt(hint: str, *, category: str = "", ratio: str = DEFAULT_BODY_RATIO,
                      scene: Optional[Dict[str, Any]] = None) -> str:
    """본문 이미지 프롬프트 — **대상·장소·행동**을 글자 없이 묘사한다.

    `scene`(집필 브리프가 준 `{subject, setting, action, purpose}`)이 있으면 그 세 축을
    각각 문장으로 준다. 없으면 예전처럼 소제목 힌트 한 줄로 유추하게 한다
    (소제목 단어를 기계적으로 그리던 문제의 정면 대응 — 요건 5).
    """
    safe_hint = sanitize(hint, limit=80)
    safe_cat = sanitize(category, limit=30)
    subject = setting = action = purpose = ""
    if isinstance(scene, dict):
        subject = sanitize(scene.get("subject"), limit=60)
        setting = sanitize(scene.get("setting"), limit=60)
        action = sanitize(scene.get("action"), limit=80)
        purpose = sanitize(scene.get("purpose"), limit=80)

    lines = ["Create ONE photo-like editorial image for a Korean blog article body."]
    if subject or setting or action:
        if subject:
            lines.append(f"Who or what is in frame: '{subject}'.")
        if setting:
            lines.append(f"Where this happens: '{setting}'.")
        if action:
            lines.append(f"What is happening: '{action}'.")
        lines.append(
            "Compose these three into one natural moment - not a collage, not a diagram.")
        if purpose:
            lines.append(
                "Role of this image in the article (guidance only, never rendered): "
                f"'{purpose}'.")
    else:
        lines.append(f"Depict the situation implied by this Korean phrase: '{safe_hint}'.")
        lines.append("Show the people, place and action that the phrase is about "
                     "(who is involved, where they are, what they are doing).")
    lines.append("All Korean text above is guidance for you only - it must NOT appear "
                 "anywhere in the image.")
    if safe_cat:
        lines.append(f"Topic context (guidance only, never rendered): '{safe_cat}'.")
    lines.append(_BODY_STYLE)
    lines.append(NO_TEXT_CLAUSE)
    lines.append(
        "Negative constraints: no text, no letters, no numbers, no logo, no watermark, "
        "no collage, no split frames, no distorted hands or faces.")
    return "\n".join(lines) + "\n"


def build_thumb_bg_prompt(*, message: str = "", title: str = "",
                          style: Optional[Dict[str, Any]] = None,
                          ratio: str = DEFAULT_THUMB_RATIO) -> str:
    """썸네일 **배경** 프롬프트 — 글자 0. 한글은 `core.thumbnail_compose` 가 그린다.

    핵심 두 가지를 모델에게 요구한다:
      ① 글자를 하나도 넣지 말 것(본문 이미지와 같은 금지 조항 재사용)
      ② 화면 **위쪽 약 55-65%는 주제에 맞는 사진 장면**, 아래쪽 35-45%는 한글 영역으로 둔다.
    제목·메시지에서 구체적인 소재를 고르고, 계정 색은 작은 보조 요소로만 쓴다.
    """
    st = style or {}
    bg = sanitize(st.get("bg"), limit=12) or "#12213f"
    accent = sanitize(st.get("accent"), limit=12) or "#e7b64b"
    mood = sanitize(st.get("mood"), limit=60)
    safe_msg = sanitize(message, limit=40)
    safe_title = sanitize(title, limit=60)

    lines = [
        "Create ONE square 1:1 cover background image for a Korean blog post.",
        "This is a BACKGROUND ONLY - the headline will be added separately by software, "
        "so the image itself must contain no text at all.",
        _THUMB_STYLE,
        "Use natural material colors and clear subject-background contrast suited to "
        "this topic, with realistic light and texture. "
        f"Account colors {bg} and {accent} may appear only as small supporting accents; "
        "do not recolor the whole scene or force a navy-and-gold luxury palette.",
        "COMPOSITION - IMPORTANT: default to one topic-matched, photo-like documentary "
        "scene filling roughly the UPPER 55-65 percent of the square. Show the relevant "
        "person, place, object or action that fits the article. Use a diagram only when "
        "the article itself explains a process or structure; never make diagrams the default. "
        "Reserve only the LOWER 35-45 percent for a visually quiet headline area with a "
        "natural dark gradient; do not leave half the canvas empty. Keep important scene "
        "details away from the bottom edge.",
        "Use two focal subjects only when the article actually compares options; otherwise "
        "show one clear topic-specific subject with its real setting and useful context. "
        "Avoid busy decoration, "
        "no borders, no frames, no collage.",
    ]
    if safe_msg:
        lines.append("Reader question or takeaway to illustrate through the subject "
                     "(guidance only, never rendered as text): "
                     f"'{safe_msg}'.")
    if safe_title:
        lines.append(f"Article topic that determines the concrete subject "
                     f"(guidance only, never rendered): '{safe_title}'.")
    if mood:
        lines.append(f"Secondary visual identity note (guidance only): '{mood}'. "
                     "Apply only where compatible with a concrete, natural topic scene.")
    lines.append(NO_TEXT_CLAUSE)
    lines.append(
        "Negative constraints: no text, no letters, no numbers, no headline, no caption, "
        "no logo, no watermark, no signage, no book cover, no obi strip, no UI mockup, "
        "no generic stock 3D icons, no trophies, no gold coins, no invented official "
        "seals or institutional endorsement, no fear-based clickbait or exaggerated "
        "distress, no invented benefit amounts, deadlines or guarantees.")
    return "\n".join(lines) + "\n"


def build_thumb_prompt(label: str, *, title: str = "",
                       ratio: str = DEFAULT_THUMB_RATIO) -> str:
    """사진 장면·비교 구도·큰 제목 위계를 갖춘 표지. 문구는 정확히 유지한다."""
    safe_label = sanitize(label, limit=THUMB_LABEL_MAX_CHARS)
    safe_title = sanitize(title, limit=60)
    lines = [
        "Design ONE Korean blog cover thumbnail, square 1:1, studio quality, highly legible.",
        _THUMB_STYLE,
        _THUMB_LAYOUT,
        "CRITICAL - bake exactly this one short Korean phrase, spelling every character "
        "EXACTLY as given, large and bold, placed in naturally quiet space with "
        f"generous margins, without obscuring the focal subject:\n\"{safe_label}\"",
        _THUMB_CLICK_REASON,
        "Do NOT render the full article title, a subtitle, an author name, a publisher, a "
        "byline, a price band or a book-cover obi strip. One short phrase only.",
    ]
    if safe_title:
        lines.append(
            f"Article topic for your understanding only (never rendered): '{safe_title}'.")
    lines.append(THUMB_TEXT_CLAUSE)
    lines.append(
        "Negative constraints: no misspelled or broken Hangul, no gibberish, no lorem ipsum, "
        "no watermark, no logo, no English filler text, no duplicated or overlapping text, "
        "no invented amounts, deadlines, institutions or guarantees, no fear-based clickbait. "
        "Do not add any words or numbers beyond the exact phrase specified above.")
    return "\n".join(lines) + "\n"


def _is_previous_default_thumb_prompt(prompt: str, label: str, *, title: Any,
                                     hint: str, ratio: str) -> bool:
    """직전 기본 빌더의 전체 출력과 일치할 때만 갱신한다. 커스텀 문장은 보존."""
    for original_title in (title, hint):
        previous = build_thumb_prompt(label, title=original_title, ratio=ratio)
        previous = previous.replace(_THUMB_CLICK_REASON + "\n", "", 1)
        previous = previous.replace(_THUMB_LAYOUT + "\n", "", 1)
        previous = previous.replace(_THUMB_STYLE, _PREVIOUS_THUMB_STYLE, 1)
        if prompt == previous:
            return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 계획 만들기 — 로컬(결정론) / LLM 응답 정규화
# ─────────────────────────────────────────────────────────────────────────────
def _slot(slot_id: str, role: str, *, prompt: str, ratio: str, text_policy: str,
          label: str = "", hint: str = "",
          overlay: Optional[Dict[str, Any]] = None,
          scene: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    slot: Dict[str, Any] = {
        "id": slot_id,
        "role": role,
        "ratio": ratio,
        "text_policy": text_policy,
        "label": label,
        "hint": hint,
        "prompt": prompt,
    }
    if overlay:
        slot["overlay"] = overlay          # 썸네일 로컬 합성 명세(메시지 + 계정 색)
    if scene:
        slot["scene"] = scene              # 본문 슬롯의 대상·장소·행동·역할(관측·재구성용)
    return slot


def thumb_style(profile: Optional[str] = None) -> Dict[str, Any]:
    """계정별 썸네일 시각 정체성. 조회 실패 시 기존 딥네이비·골드 기본값(fail-open)."""
    try:
        from core import editorial_profile as ep
        return ep.thumbnail_style(profile)
    except Exception as exc:  # noqa: BLE001 — 설정 미로드면 기존 색
        log.debug(f"[image_plan] 썸네일 색 조회 실패({type(exc).__name__}) — 기본값")
        return {"bg": "#12213f", "accent": "#e7b64b", "text": "#f7f7f2", "mood": ""}


def _thumb_slot(*, message: str, title: Any, ratio: str,
                style: Optional[Dict[str, Any]] = None,
                local_text: Optional[bool] = None) -> Dict[str, Any]:
    ratio = DEFAULT_THUMB_RATIO
    """썸네일 슬롯을 만든다. 기본 통합 생성, True 설정으로 로컬 합성 복귀."""
    use_local = local_text if local_text is not None else local_thumb_text_enabled()
    if not use_local:
        label = sanitize(message, limit=THUMB_LABEL_MAX_CHARS)
        return _slot(SLOT_THUMB, ROLE_THUMBNAIL,
                     prompt=build_thumb_prompt(label, title=title, ratio=ratio),
                     ratio=ratio, text_policy=TEXT_POLICY_LABEL_ONLY, label=label,
                     hint=sanitize(title, limit=60))
    st = dict(style) if style else thumb_style()
    return _slot(SLOT_THUMB, ROLE_THUMBNAIL,
                 prompt=build_thumb_bg_prompt(message=message, title=title,
                                              style=st, ratio=ratio),
                 ratio=ratio, text_policy=TEXT_POLICY_OVERLAY,
                 label=message, hint=sanitize(title, limit=60),
                 overlay={"message": message, "style": st})


def _body_hints(content: Any, title: Any, count: int) -> List[str]:
    """본문 슬롯 수만큼 장면 힌트를 확보한다(모자라면 제목으로 결정론 보충)."""
    hints = section_hints(content)
    fallback = sanitize(title, limit=60) or "글의 핵심 주제"
    out: List[str] = []
    for i in range(max(0, int(count))):
        out.append(hints[i] if i < len(hints) else fallback)
    return out


def _thumb_message(label: Any, title: Any, *, local_text: bool) -> str:
    """썸네일에 쓸 문구를 정한다 — 브리프/호출자 값 우선, 없으면 로컬 폴백.

    두 경로 모두 같은 길이 상한 메시지와 제목 기반 어절 경계 폴백을 쓴다.
    """
    if label:
        try:
            from core.editorial_brief import LIMIT_MESSAGE, resolve_thumbnail_message
            limit = LIMIT_MESSAGE
        except Exception:  # noqa: BLE001
            limit = 24
        text = sanitize(label, limit=200)
        if text:
            try:
                return resolve_thumbnail_message(text, title=title, limit=limit)
            except Exception:  # noqa: BLE001 — 로컬 브리프 부재 시 안전한 기존 문구
                return text
    try:
        from core.editorial_brief import message_from_title
        return message_from_title(title)
    except Exception:  # noqa: BLE001 — 브리프 모듈 부재 시 예전 규칙
        return thumb_label_from_title(title)


def refresh_thumbnail_message(plan: Dict[str, Any], *, title: Any,
                               message: Any) -> bool:
    """미완 썸네일 계획만 새 메시지와 그에 맞는 프롬프트로 갱신한다."""
    if not isinstance(plan, dict) or not isinstance(plan.get("slots"), list):
        return False
    slot = next((item for item in plan["slots"]
                 if isinstance(item, dict) and item.get("role") == ROLE_THUMBNAIL), None)
    if slot is None:
        return False
    resolved = _thumb_message(message, title, local_text=local_thumb_text_enabled())
    if not resolved or resolved == str(slot.get("label") or ""):
        return False
    style = (slot.get("overlay") or {}).get("style")
    fresh = _thumb_slot(message=resolved, title=title,
                        ratio=slot.get("ratio") or DEFAULT_THUMB_RATIO,
                        style=style if isinstance(style, dict) else None)
    # 슬롯 ID와 순서는 계약이므로 메시지/프롬프트 관련 필드만 바꾼다.
    for key in ("label", "hint", "prompt", "text_policy", "overlay"):
        if key in fresh:
            slot[key] = fresh[key]
        else:
            slot.pop(key, None)
    return True


def local_plan(*, title: Any, content: Any, category: Any = "",
               body_count: int = DEFAULT_BODY_COUNT,
               thumb_ratio: str = DEFAULT_THUMB_RATIO,
               body_ratio: str = DEFAULT_BODY_RATIO,
               label: Any = None,
               style: Optional[Dict[str, Any]] = None,
               scenes: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """LLM 0회 — 제목·본문 소제목·카테고리만으로 만드는 결정론 계획."""
    body_count = max(0, int(body_count or 0))
    local_text = local_thumb_text_enabled()
    message = _thumb_message(label, title, local_text=local_text)
    cat = sanitize(category, limit=30)

    slots: List[Dict[str, Any]] = [
        _thumb_slot(message=message, title=title, ratio=thumb_ratio, style=style,
                    local_text=local_text),
    ]
    scene_list = list(scenes or [])
    for idx, hint in enumerate(_body_hints(content, title, body_count), start=1):
        scene = scene_list[idx - 1] if idx - 1 < len(scene_list) else None
        slots.append(_slot(
            body_slot_id(idx), ROLE_BODY,
            prompt=build_body_prompt(hint, category=cat, ratio=body_ratio, scene=scene),
            ratio=body_ratio, text_policy=TEXT_POLICY_NO_TEXT, hint=hint, scene=scene))

    return {
        "version": PLAN_VERSION,
        "source": "local",
        "created_at": _now_iso(),
        "fingerprint": fingerprint(title, content),
        "category": cat,
        "body_target": body_count,
        "slots": slots,
    }


def plan_from_llm(raw: Any, *, title: Any, content: Any, category: Any = "",
                  body_count: int = DEFAULT_BODY_COUNT,
                  thumb_ratio: str = DEFAULT_THUMB_RATIO,
                  body_ratio: str = DEFAULT_BODY_RATIO) -> Optional[Dict[str, Any]]:
    """집필 응답의 `image_plan` 조각을 정규화한다(LLM 추가 호출 0).

    받는 형태(집필 프롬프트가 요구하는 최소 스키마)::

        {"thumbnail_label": "짧은 문구",
         "body_images": [{"hint": "장면 한 줄"}, "장면 한 줄", ...]}

    프롬프트 문장은 **여기서** 만든다 — 모델이 준 문자열을 그대로 프롬프트로 쓰지 않는다
    (인젝션·무문자 규칙 누락 방지). 쓸 만한 재료가 하나도 없으면 None(호출자가 로컬 폴백).
    """
    if not isinstance(raw, dict):
        return None
    label = (raw.get("thumbnail_message") or raw.get("thumbnail_label")
             or raw.get("label") or raw.get("thumb_label"))
    items = raw.get("body_images")
    if not isinstance(items, (list, tuple)):
        items = []

    local_text = local_thumb_text_enabled()
    hints: List[str] = []
    scenes: List[Optional[Dict[str, Any]]] = []
    for item in items:
        scene: Optional[Dict[str, Any]] = None
        if isinstance(item, str):
            text = item
        elif isinstance(item, dict):
            scene = {
                "subject": sanitize(item.get("subject"), limit=60),
                "setting": sanitize(item.get("setting"), limit=60),
                "action": sanitize(item.get("action"), limit=80),
                "purpose": sanitize(item.get("purpose") or item.get("role"), limit=80),
            }
            scene = {k: v for k, v in scene.items() if v}
            parts = [str(item.get(k) or "") for k in
                     ("hint", "subject", "setting", "action", "scene", "caption")]
            text = " ".join(p for p in parts if p.strip())
        else:
            continue
        text = sanitize(_DECOR_RE.sub(" ", text), limit=80).strip()
        if len(text) < 2 or text in hints:
            continue
        hints.append(text)
        scenes.append(scene or None)

    msg_limit = THUMB_LABEL_MAX_CHARS
    if local_text:
        try:
            from core.editorial_brief import LIMIT_MESSAGE
            msg_limit = LIMIT_MESSAGE
        except Exception:  # noqa: BLE001
            msg_limit = 24
    # The shared resolver handles punctuation and number tokens; stripping commas here
    # would turn valid amounts such as 1,918원 into a misleading split number.
    label_text = sanitize(str(label or ""), limit=200).strip()
    if not label_text and not hints:
        return None

    # 장수는 계획이 정하되 허용 범위로 조인다 — 빈 계획으로 발행 게이트를 우회할 수 없다.
    target = clamp_body_count(len(hints) or None, body_count)
    plan = local_plan(title=title, content=content, category=category,
                      body_count=target, thumb_ratio=thumb_ratio,
                      body_ratio=body_ratio, label=label_text or None)
    # 본문 슬롯의 힌트·장면만 모델이 준 것으로 갈아끼운다(모자라면 로컬 힌트 유지).
    body_slots = [s for s in plan["slots"] if s.get("role") == ROLE_BODY]
    cat = sanitize(category, limit=30)
    for index, slot in enumerate(body_slots):
        if index >= len(hints):
            break
        scene = scenes[index] if index < len(scenes) else None
        slot["hint"] = hints[index]
        if scene:
            slot["scene"] = scene
        slot["prompt"] = build_body_prompt(hints[index], category=cat,
                                           ratio=slot.get("ratio") or body_ratio,
                                           scene=scene)
    plan["source"] = "llm" if (label_text and hints) else "llm+local"
    return plan


# ─────────────────────────────────────────────────────────────────────────────
# 저장된 계획 정규화 · 보완(로컬만 — LLM 재기획 폴백 없음)
# ─────────────────────────────────────────────────────────────────────────────
def _normalize_slot(raw: Any, *, index: int, title: Any, category: Any,
                    thumb_ratio: str, body_ratio: str,
                    preserve_completed_thumbnail: bool = False) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    slot_id = str(raw.get("id") or "").strip()
    role = str(raw.get("role") or "").strip().lower()
    if role not in (ROLE_THUMBNAIL, ROLE_BODY):
        role = ROLE_THUMBNAIL if slot_id == SLOT_THUMB else ROLE_BODY
    if not slot_id:
        slot_id = SLOT_THUMB if role == ROLE_THUMBNAIL else body_slot_id(index)
    hint = sanitize(raw.get("hint"), limit=80)
    label = sanitize(raw.get("label"), limit=THUMB_LABEL_MAX_CHARS)
    ratio = str(raw.get("ratio") or "").strip()
    prompt = raw.get("prompt")
    prompt = prompt if isinstance(prompt, str) and prompt.strip() else ""

    if role == ROLE_THUMBNAIL:
        if preserve_completed_thumbnail:
            # The caller proved this exact mapped file has a valid provenance record.
            # Keep the stored message contract byte-for-byte until the user refills it.
            preserved = dict(raw)
            preserved["role"] = ROLE_THUMBNAIL
            return preserved
        # Saved plans may contain a stale custom ratio. Preserve their exact prompt,
        # but every new thumbnail request and slot contract is square.
        ratio = DEFAULT_THUMB_RATIO
        local_text = local_thumb_text_enabled()
        stored_overlay = raw.get("overlay") if isinstance(raw.get("overlay"), dict) else None
        overlay_message = (sanitize(stored_overlay.get("message"), limit=200)
                           if stored_overlay else "")
        stored_label = sanitize(raw.get("label"), limit=200)
        message = overlay_message or stored_label
        # A missing label can be repaired locally. Existing labels stay byte-for-byte
        # semantically aligned with their already generated thumbnail until refill.
        if not message:
            message = _thumb_message(None, title, local_text=local_text)
        if not local_text:
            # 통합 생성 — 사용자 프롬프트를 보존하되, **overlay 계획으로 저장된
            # 글자 없는 배경 프롬프트는 갈아끼운다.** 안 그러면 되돌리기(THUMB_LOCAL_TEXT
            # =False) 직후 "글자를 절대 넣지 마라"는 배경 프롬프트를 들고 baked 정책으로
            # 생성하게 돼, 라벨 검증이 영영 불합격(슬롯 미완 → 발행 차단 + 재생성 낭비)이고
            # 검증을 꺼 두면 **글자 없는 썸네일이 완성으로 집계**된다.
            label = sanitize(message, limit=THUMB_LABEL_MAX_CHARS)
            if (not prompt or str(raw.get("text_policy") or "") != TEXT_POLICY_LABEL_ONLY
                    or _is_previous_default_thumb_prompt(
                        prompt, label, title=title, hint=hint, ratio=ratio)):
                prompt = build_thumb_prompt(label, title=title, ratio=ratio)
            return _slot(slot_id, role, prompt=prompt, ratio=ratio,
                         text_policy=TEXT_POLICY_LABEL_ONLY, label=label, hint=hint)
        style = (stored_overlay or {}).get("style")
        style = dict(style) if isinstance(style, dict) and style else thumb_style()
        # 저장된 계획이 예전 baked 프롬프트를 들고 있으면 **배경 프롬프트로 갈아끼운다**
        # (레거시 글을 다시 채울 때 글자 있는 썸네일이 또 만들어지지 않게).
        if not prompt or str(raw.get("text_policy") or "") != TEXT_POLICY_OVERLAY:
            prompt = build_thumb_bg_prompt(message=message, title=title, style=style,
                                           ratio=ratio)
        return _slot(slot_id, role, prompt=prompt, ratio=ratio,
                     text_policy=TEXT_POLICY_OVERLAY,
                     label=label or sanitize(message, limit=THUMB_LABEL_MAX_CHARS), hint=hint,
                     overlay={"message": overlay_message or message, "style": style})

    ratio = ratio or body_ratio
    if not hint:
        hint = sanitize(title, limit=60)
    scene = raw.get("scene") if isinstance(raw.get("scene"), dict) else None
    if scene:
        scene = {k: sanitize(v, limit=80) for k, v in scene.items()
                 if k in ("subject", "setting", "action", "purpose") and v}
        scene = scene or None
    if not prompt:
        prompt = build_body_prompt(hint, category=category, ratio=ratio, scene=scene)
    return _slot(slot_id, role, prompt=prompt, ratio=ratio,
                 text_policy=TEXT_POLICY_NO_TEXT, label=label, hint=hint, scene=scene)


def normalize_plan(raw: Any, *, title: Any, content: Any, category: Any = "",
                   thumb_ratio: str = DEFAULT_THUMB_RATIO,
                   body_ratio: str = DEFAULT_BODY_RATIO,
                   preserve_completed_thumbnail: bool = False) -> Optional[Dict[str, Any]]:
    """저장돼 있던 계획을 현재 스키마로 정규화한다. 못 쓰면 None(호출자가 로컬 생성).

    버전이 다르거나 필드가 빠졌어도 **로컬로 보완**한다 — 어떤 경우에도 LLM 을 다시
    부르지 않는다(요건 3). 슬롯이 하나도 살아남지 못할 때만 None.
    """
    if not isinstance(raw, dict):
        return None
    raw_slots = raw.get("slots")
    if not isinstance(raw_slots, (list, tuple)) or not raw_slots:
        return None
    slots: List[Dict[str, Any]] = []
    seen_ids: set = set()
    body_index = 0
    for item in raw_slots:
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "").strip().lower() != ROLE_THUMBNAIL:
            body_index += 1
        slot = _normalize_slot(item, index=body_index, title=title, category=category,
                               thumb_ratio=thumb_ratio, body_ratio=body_ratio,
                               preserve_completed_thumbnail=(
                                   preserve_completed_thumbnail
                                   and item.get("role") == ROLE_THUMBNAIL))
        if slot is None or slot["id"] in seen_ids:
            continue
        seen_ids.add(slot["id"])
        slots.append(slot)
    if not slots:
        return None
    plan = {
        "version": PLAN_VERSION,
        "source": str(raw.get("source") or "stored"),
        "created_at": str(raw.get("created_at") or _now_iso()),
        "fingerprint": str(raw.get("fingerprint") or fingerprint(title, content)),
        "category": sanitize(raw.get("category") or category, limit=30),
        "slots": slots,
    }
    # 계획이 정한 본문 장수 — 있으면 보존한다(설정 기본값이 계획을 덮지 않게).
    try:
        stored_target = raw.get("body_target")
        if stored_target is not None:
            plan["body_target"] = max(0, min(BODY_COUNT_ABS_MAX, int(stored_target)))
    except (TypeError, ValueError):
        pass
    if int(raw.get("version") or 0) != PLAN_VERSION:
        plan["source"] = f"{plan['source']}+migrated"
    return plan


def ensure_plan(post_data: Any, *, title: Any = None, content: Any = None,
                category: Any = "", body_count: int = DEFAULT_BODY_COUNT,
                thumb_ratio: str = DEFAULT_THUMB_RATIO,
                body_ratio: str = DEFAULT_BODY_RATIO,
                reserved_slot_ids: Sequence[str] = (),
                preserve_completed_thumbnail: bool = False,
                message: Any = None) -> Tuple[Dict[str, Any], bool]:
    """이 글에 쓸 계획을 확보한다 — 저장된 것 우선, 없으면/망가졌으면 로컬 생성.

    Args:
        post_data: 보관본 payload(dict). `image_plan` 이 있으면 그것을 정규화해 쓴다.
        reserved_slot_ids: 이미 이미지가 붙어 있는 슬롯 id — 예산이 줄어도 잘라내지 않는다.

    Returns:
        (plan, changed). changed=True 면 호출자가 저장해야 한다(요건 3 — "한 번 만들어
        저장 후 생성"). **LLM 재기획 폴백은 존재하지 않는다.**
    """
    src = post_data if isinstance(post_data, dict) else {}
    title = src.get("title") if title is None else title
    content = src.get("content") if content is None else content
    category = category or src.get("category") or ""
    body_count = max(0, int(body_count or 0))

    stored = normalize_plan(src.get("image_plan"), title=title, content=content,
                            category=category, thumb_ratio=thumb_ratio,
                            body_ratio=body_ratio,
                            preserve_completed_thumbnail=preserve_completed_thumbnail)
    if stored is None:
        plan = local_plan(title=title, content=content, category=category,
                          body_count=body_count, thumb_ratio=thumb_ratio,
                          body_ratio=body_ratio,
                          label=(str(message).strip() or None) if message else None)
        return plan, True

    changed = stored != src.get("image_plan")
    # 계획이 스스로 정한 장수가 있으면 그것이 기준이다(요건 5) — 단:
    #   · 설정이 0(본문 이미지 끄기)이면 사용자 설정이 이긴다.
    #   · **모델이 정한 장수만** 설정을 이긴다(`source` 가 llm 계열). 로컬 계획의 target 은
    #     그때의 설정을 그대로 적어 둔 값이라, 사용자가 나중에 설정을 올리면 예전처럼
    #     모자란 만큼 덧붙여야 한다(설정이 무시되는 것처럼 보이지 않게).
    stored_target = stored.get("body_target")
    model_chosen = str(stored.get("source") or "").startswith("llm")
    if stored_target is not None and body_count > 0 and model_chosen:
        clamped = clamp_body_count(stored_target, body_count)
        if clamped != body_count:
            body_count = clamped
    if stored.get("body_target") != body_count:
        stored["body_target"] = body_count
        changed = True

    body_slots = [s for s in stored["slots"] if s.get("role") == ROLE_BODY]
    reserved = {str(s) for s in (reserved_slot_ids or ())}

    if len(body_slots) < body_count:
        # 예산이 늘었다 → 모자란 만큼만 로컬로 덧붙인다(기존 슬롯·프롬프트 불변).
        used = {s["id"] for s in stored["slots"]}
        hints = _body_hints(content, title, body_count)
        cat = sanitize(category, limit=30)
        idx = 1
        while len([s for s in stored["slots"] if s.get("role") == ROLE_BODY]) < body_count:
            sid = body_slot_id(idx)
            idx += 1
            if sid in used:
                continue
            hint = hints[min(len(hints) - 1, idx - 2)] if hints else sanitize(title, limit=60)
            stored["slots"].append(_slot(
                sid, ROLE_BODY, prompt=build_body_prompt(hint, category=cat, ratio=body_ratio),
                ratio=body_ratio, text_policy=TEXT_POLICY_NO_TEXT, hint=hint))
            used.add(sid)
            changed = True
            if idx > body_count + 20:      # 방어 — 무한 루프 금지
                break
    elif len(body_slots) > body_count:
        # 예산이 줄었다 → **이미지가 붙지 않은** 뒤쪽 슬롯만 잘라낸다(자산 보존).
        keep: List[Dict[str, Any]] = [s for s in stored["slots"] if s.get("role") != ROLE_BODY]
        kept_bodies: List[Dict[str, Any]] = []
        for slot in body_slots:
            if len(kept_bodies) < body_count or slot["id"] in reserved:
                kept_bodies.append(slot)
            else:
                changed = True
        stored["slots"] = keep + kept_bodies
        # 썸네일이 항상 맨 앞이 되도록 정렬(역할 우선, 그다음 원래 순서).
        stored["slots"].sort(key=lambda s: 0 if s.get("role") == ROLE_THUMBNAIL else 1)
    return stored, changed


# ─────────────────────────────────────────────────────────────────────────────
# 슬롯 ↔ 자산(이미지 파일) 매핑
# ─────────────────────────────────────────────────────────────────────────────
def _is_file(path: Any) -> bool:
    text = str(path or "").strip()
    if not text:
        return False
    try:
        return Path(text).is_file()
    except (OSError, ValueError):
        return False


def slot_assets(plan: Dict[str, Any], post_data: Any) -> Tuple[Dict[str, str], List[str], List[str]]:
    """이 글에 이미 있는 이미지를 슬롯 id 에 매핑한다.

    ① `post_data["image_slots"]`(신규 자산 — 역할 보존)이 최우선.
    ② 없으면 레거시 `image_paths` 를 **최소 호환 매핑**한다: [0]=썸네일, 나머지는 본문
       슬롯 순서. ★썸네일 슬롯이 비어도 첫 본문 이미지를 썸네일로 승격하지 않는다 —
       슬롯 map 이 있는 글은 그 map 이 역할의 단일 진실이다(요건 5).

    Returns:
        (mapped, missing, extras)
        · mapped  = {slot_id: 실재 파일 경로}
        · missing = 경로는 있는데 파일이 사라진 것(유실 — 그 슬롯은 다시 채움 대상)
        · extras  = 실재하지만 어느 슬롯에도 못 붙은 유효 자산(사진 글 등에서 들어온 것).
          **삭제하지 않고** 목록 뒤에 그대로 보존한다.
    """
    src = post_data if isinstance(post_data, dict) else {}
    slot_ids = [str(s.get("id")) for s in (plan.get("slots") or []) if isinstance(s, dict)]
    mapped: Dict[str, str] = {}
    missing: List[str] = []
    extras: List[str] = []

    raw_slots = src.get("image_slots")
    if isinstance(raw_slots, dict) and raw_slots:
        for sid, path in raw_slots.items():
            text = str(path or "").strip()
            if not text:
                continue
            if _is_file(text):
                mapped[str(sid)] = text
            else:
                missing.append(text)
        known = set(mapped.values()) | set(missing)
        for path in (src.get("image_paths") or []):
            text = str(path or "").strip()
            if not text or text in known:
                continue
            known.add(text)
            (extras if _is_file(text) else missing).append(text)
        return mapped, missing, extras

    legacy = [str(p or "").strip() for p in (src.get("image_paths") or []) if str(p or "").strip()]
    body_ids = [sid for sid in slot_ids if sid != SLOT_THUMB]
    for index, path in enumerate(legacy):
        if not _is_file(path):
            missing.append(path)
            continue
        if index == 0:
            mapped[SLOT_THUMB] = path
            continue
        pos = index - 1
        if pos < len(body_ids):
            mapped[body_ids[pos]] = path
        else:
            extras.append(path)       # 계획보다 많은 자산 — 지우지 않고 뒤에 보존
    return mapped, missing, extras


def ordered_paths(plan: Dict[str, Any], slot_map: Dict[str, str],
                  extras: Sequence[str] = ()) -> List[str]:
    """계획 슬롯 순서대로 이미지 경로를 나열한다(썸네일 [0] 고정, 빠진 슬롯은 건너뜀).

    `extras`(슬롯에 못 붙은 유효 자산)는 순서 안정성을 위해 맨 뒤에 붙인다.
    """
    out: List[str] = []
    for slot in (plan.get("slots") or []):
        if not isinstance(slot, dict):
            continue
        path = slot_map.get(str(slot.get("id")))
        if path and path not in out:
            out.append(path)
    for path in (extras or []):
        text = str(path or "").strip()
        if text and text not in out:
            out.append(text)
    return out


def expected_texts(slot: Dict[str, Any]) -> List[str]:
    """이 슬롯의 검증 기대 텍스트 — 통합 생성 썸네일만 1개, 그 외는 빈 목록.

    ★ overlay 선택 시에는 **모델이 글자를 쓰지 않는다** — 우리가 합성한다. 그래서
      전사 검증 대상이 없고, 편당 비전(OCR) 호출이 0회가 된다.
    """
    if not isinstance(slot, dict):
        return []
    if slot.get("text_policy") == TEXT_POLICY_LABEL_ONLY:
        label = str(slot.get("label") or "").strip()
        return [label] if label else []
    return []


def overlay_spec(slot: Any) -> Optional[Dict[str, Any]]:
    """이 슬롯이 로컬 한글 합성을 요구하는가 — 그렇다면 {message, style}, 아니면 None."""
    if not isinstance(slot, dict):
        return None
    if slot.get("text_policy") != TEXT_POLICY_OVERLAY:
        return None
    spec = slot.get("overlay")
    if not isinstance(spec, dict):
        return None
    message = str(spec.get("message") or slot.get("label") or "").strip()
    if not message:
        return None
    style = spec.get("style")
    return {"message": message,
            "style": dict(style) if isinstance(style, dict) else thumb_style()}


def plan_summary(plan: Any) -> Dict[str, Any]:
    """로그·메타에 남길 얇은 요약(프롬프트 본문은 제외)."""
    if not isinstance(plan, dict):
        return {}
    slots = [s for s in (plan.get("slots") or []) if isinstance(s, dict)]
    thumb = next((s for s in slots if s.get("role") == ROLE_THUMBNAIL), None)
    return {
        "version": plan.get("version"),
        "source": plan.get("source"),
        "fingerprint": plan.get("fingerprint"),
        "slots": len(slots),
        "body_slots": len([s for s in slots if s.get("role") == ROLE_BODY]),
        "body_target": plan.get("body_target"),
        "thumb_policy": (thumb or {}).get("text_policy"),
        "scenes": len([s for s in slots if isinstance(s.get("scene"), dict)]),
    }


__all__ = [
    "PLAN_VERSION", "SLOT_THUMB", "ROLE_THUMBNAIL", "ROLE_BODY",
    "TEXT_POLICY_LABEL_ONLY", "TEXT_POLICY_NO_TEXT", "TEXT_POLICY_OVERLAY",
    "DEFAULT_THUMB_RATIO", "DEFAULT_BODY_RATIO", "DEFAULT_BODY_COUNT",
    "THUMB_LABEL_MAX_CHARS", "BODY_COUNT_ABS_MAX", "BODY_COUNT_FLOOR",
    "NO_TEXT_CLAUSE", "THUMB_TEXT_CLAUSE",
    "body_slot_id", "local_thumb_text_enabled", "clamp_body_count",
    "to_plain_text", "sanitize", "fingerprint", "section_hints",
    "thumb_label_from_title", "thumb_style", "build_body_prompt", "build_thumb_prompt",
    "build_thumb_bg_prompt", "local_plan", "plan_from_llm", "normalize_plan",
    "ensure_plan", "refresh_thumbnail_message", "slot_assets", "ordered_paths", "expected_texts", "overlay_spec",
    "plan_summary",
]
