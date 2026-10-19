# core/draft_top_upgrade.py — 보관본 초안의 "상단(제목 + 도입부)"만 현재 규격으로 승격
"""draft_ready 보관본 1편의 **상단 영역만** 최신 규격으로 다듬는다.

배경(실측): draft_ready 1,164편 중 📌 결론부터 박스가 있는 건 43편(3%)뿐이고,
인사말 없는 글도 212편(19%)이다. 본문은 이미 완성돼 있으니 **첫 `<h2>` 이전의
도입부만** ①인사말 ②"📌 결론부터:" 한 줄 요약(AEO TL;DR)으로 승격하면, 대량
재생성 없이 상단 규격만 저비용으로 맞출 수 있다.

핵심 불변식:
  · **본문 절대 보존** — 첫 `<h2>` 이후 문자열은 바이트 단위로 그대로 둔다.
  · **멱등** — 이미 박스가 있으면 재삽입하지 않는다. 인사말+박스가 다 있으면 무변경.
  · **fail-open** — 어떤 예외에도 원본을 건드리지 않고 {"ok": False, ...} 를 돌려준다.
  · **원본 백업** — 저장 직전 post_data.json → post_data.bak.json 로 복사.

Claude 호출은 본문에 이미 있는 사실만 요약하도록 지시한다(새 수치·사실 창작 금지 —
본문과 불일치·fact 리스크 방지). 실제 발행·엑셀 쓰기는 이 모듈이 트리거하지 않는다.
"""
from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional

from config import atomic_write_json, safe_load_json
from config.logger import get_logger

log = get_logger(__name__)

# ── 앱 루트 / 진행상황 파일 (재개용, 원자적 쓰기) ─────────────
_APP_ROOT = Path(__file__).resolve().parent.parent          # naver_blog_v3/
# 모듈 전역 — 테스트에서 monkeypatch 로 tmp 로 돌릴 수 있도록 함수에서 이 이름을 참조한다.
# 진행 커서는 프로필별(2026-07-19 격리 감사) — 공유하면 프로필끼리 재개 커서를 덮어쓴다.
try:
    from config import profile as _nbaprofile
    _PROGRESS_PATH = _nbaprofile.logs_dir() / "top_upgrade_progress.json"
except Exception:  # noqa: BLE001 — 프로필 레이어 부재 시 레거시
    _PROGRESS_PATH = _APP_ROOT / "logs" / "top_upgrade_progress.json"

# ── 상단 HTML 규격 (기존 보관본과 동일한 인라인 스타일) ───────
_PARA_STYLE = "line-height:1.9;margin:10px 0;font-size:15px;color:#333;"
_BOX_STYLE = (
    "line-height:1.9;margin:10px 0;padding:12px 16px;background:#fff8e1;"
    "border-left:5px solid #ffb300;border-radius:0 8px 8px 0;font-size:15px;color:#333;"
)
_DEFAULT_GREETING = "안녕하세요, 예시브랜드입니다"

# ── 경계·검출 정규식 ─────────────────────────────────────
# 첫 <h2> (대소문자·선행 공백 허용) 를 상단/본문 경계로 삼는다.
_H2_RE = re.compile(r"<\s*h2", re.IGNORECASE)
# <h2> 가 없을 때의 폴백 경계: 첫 IMG 마커.
_IMG_RE = re.compile(r"<!--\s*IMG", re.IGNORECASE)
# 멱등 검출 — 상단에 이미 결론/한 줄/📌/✅ 한 줄/TL;DR 이 있으면 박스 재생성 금지.
_BOX_PRESENT_RE = re.compile(r"결론부터|한\s*줄로|📌|✅\s*한\s*줄|TL;DR", re.IGNORECASE)
# 인사말 검출(상단 한정).
_GREETING_RE = re.compile(r"안녕하세요")
# 프롬프트에 넘길 본문 참고 텍스트 상한(토큰 절약).
_MAX_BODY_CHARS = 1800


# ─────────────────────────────────────────────────────
# 상단/본문 분리
# ─────────────────────────────────────────────────────
def _split_top_body(content: str) -> tuple:
    """content 를 (상단, 본문) 으로 나눈다. 본문은 반환 문자열을 그대로 보존한다.

    경계 우선순위:
      1) 첫 `<h2` 위치
      2) (없으면) 첫 `<!-- IMG` 위치
      3) (없으면) 300자 이후 첫 `</p>` 끝, 없으면 정확히 300자
      4) (300자 이하·마커 없음) 상단=전체, 본문="" (보존할 본문 없음)

    반환한 (상단, 본문) 은 상단 + 본문 == content 를 항상 만족한다.
    """
    match = _H2_RE.search(content)
    if match:
        return content[: match.start()], content[match.start() :]

    match = _IMG_RE.search(content)
    if match:
        return content[: match.start()], content[match.start() :]

    if len(content) > 300:
        cut = content.find("</p>", 300)
        cut = cut + len("</p>") if cut != -1 else 300
        return content[:cut], content[cut:]

    return content, ""


# ─────────────────────────────────────────────────────
# 상단 재조립
# ─────────────────────────────────────────────────────
def _greeting_html(category: str) -> str:
    """카테고리 전략의 intro_greeting(없으면 기본값)로 인사말 <p> 를 만든다."""
    greeting = _DEFAULT_GREETING
    try:
        from core.content_generator import _load_prompt_strategy

        strategy = _load_prompt_strategy(category or "")
        candidate = (strategy or {}).get("intro_greeting")
        if isinstance(candidate, str) and candidate.strip():
            greeting = candidate.strip()
    except (ImportError, AttributeError, OSError, ValueError, TypeError) as exc:
        log.debug(f"[draft_top_upgrade] 인사말 전략 로드 실패 — 기본값 사용: {exc}")
    greeting = greeting.rstrip(".。").rstrip()
    return f'<p style="{_PARA_STYLE}">{greeting}.</p>'


def _box_html(box_text: str) -> str:
    """'📌 결론부터: ...' 강조 박스 <p> 를 만든다."""
    return f'<p style="{_BOX_STYLE}">{box_text}</p>'


def _normalize_box_text(lead: str) -> str:
    """Claude 가 준 한 줄 요약을 '📌 결론부터: ...' 형태로 정규화(중복 접두어 방지)."""
    text = " ".join((lead or "").split())
    if not text:
        return ""
    if text.startswith("📌"):
        return text
    if text.startswith("결론부터"):
        return f"📌 {text}"
    return f"📌 결론부터: {text}"


def _insert_box_after_greeting(top: str, box_paragraph: str) -> str:
    """인사말 문단 바로 뒤에 박스를 삽입한다(상단의 나머지 바이트는 보존)."""
    match = _GREETING_RE.search(top)
    if not match:
        # 방어: 호출자가 greeting_present=True 로만 부르지만, 못 찾으면 맨 앞에.
        return box_paragraph + "\n" + top

    close = top.find("</p>", match.start())
    if close == -1:
        # 인사말이 <p> 로 감싸이지 않은 경우 — 인사말 줄 다음에 삽입.
        newline = top.find("\n", match.start())
        pos = newline + 1 if newline != -1 else len(top)
        return top[:pos] + box_paragraph + "\n" + top[pos:]

    pos = close + len("</p>")
    return top[:pos] + "\n" + box_paragraph + top[pos:]


def _reassemble_top(
    top: str, box_paragraph: Optional[str], greeting_present: bool, category: str
) -> str:
    """상단을 재조립한다.

    - 인사말 있음 + 박스 새로 생성: 인사말 뒤에 박스 삽입.
    - 인사말 있음 + 박스 없음(기존 박스 유지): 상단 그대로.
    - 인사말 없음: 인사말(+박스) 을 상단 앞에 붙임.
    """
    if greeting_present:
        if box_paragraph:
            return _insert_box_after_greeting(top, box_paragraph)
        return top

    prefix = _greeting_html(category) + "\n"
    if box_paragraph:
        prefix += box_paragraph + "\n"
    return prefix + top


# ─────────────────────────────────────────────────────
# Claude 호출 (제목 + 한 줄 요약)
# ─────────────────────────────────────────────────────
def _strip_tags(html: str, limit: Optional[int] = None) -> str:
    """HTML → 평문(주석·태그 제거 + 공백 정리). limit 지정 시 상한(프롬프트용).

    검증용(가드 2)은 limit 없이 본문 전체를 훑어야 하므로 상한을 선택적으로 둔다.
    """
    text = re.sub(r"<!--.*?-->", " ", html or "", flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text[:limit] if limit else text


def _build_prompt(title: str, body_html: str) -> str:
    """상단 승격 프롬프트. 본문에 있는 사실만 요약하도록 강하게 제약한다."""
    body_text = _strip_tags(body_html, _MAX_BODY_CHARS)
    return f"""당신은 네이버 블로그 "예시브랜드"의 편집자입니다.
아래 글의 "제목"과 "도입부 한 줄 요약(결론부터 박스)"만 현재 규격으로 다듬어 주세요.
본문은 절대 새로 쓰지 않습니다 — 아래 본문에 실제로 있는 사실만 근거로 삼으세요.

[현재 제목]
{title}

[본문 (참고용 — 수정하지 말 것)]
{body_text}

[작업]
1. content: "📌 결론부터: <핵심 한 문장>" 형식의 한 줄 요약.
   - 본문에 실제로 있는 금액·자격·기간 중 하나 이상을 명시.
   - 본문에 없는 새로운 수치·사실을 지어내지 말 것(본문과 불일치 금지).
   - 정확히 한 문장, 반드시 "📌" 로 시작.
2. title: 현재 제목이 이미 양호(2026 연도 + 핵심 키워드 포함)하면 그대로 두고,
   아니면 경미하게만 SEO 보정(핵심 키워드·의미 보존, 과한 재작성 금지, 22~32자).

반드시 아래 JSON 형식으로만 응답 (마크다운 코드블록 없이):
{{
  "title": "다듬은(또는 그대로인) 제목",
  "content": "📌 결론부터: 본문에 있는 사실 한 문장"
}}"""


# ─────────────────────────────────────────────────────
# 숫자 fact 가드 (드레인 발행은 fact_gate 를 우회하므로 상단의 틀린 숫자를 결정론적으로 차단)
# ─────────────────────────────────────────────────────
# 숫자 토큰: 콤마 천단위 + 소수점 허용. 예 "4,400" · "27.5" · "2026".
_NUM_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# 숫자에 붙는 단위(긴 것 우선 — 만원>만, 개월>월). digit-core+단위 동반 매칭용.
_BOX_UNIT = (
    r"(?:개월|시간|퍼센트|만원|천원|억원|만|억|원|％|%|년|월|일|명|세|개|주|분|건|회|배|호)"
)
_BOX_NUM_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(" + _BOX_UNIT + r")?")


def _digit_core(token: str) -> str:
    """숫자 토큰에서 콤마·공백만 제거(소수점 유지). 예 "4,400"→"4400", "27.5"→"27.5"."""
    return token.replace(",", "").replace(" ", "")


def _title_numbers(text: str) -> set:
    """문자열에서 숫자 토큰의 digit-core 집합을 뽑는다(제목 보수화 가드용)."""
    result = set()
    for match in _NUM_TOKEN_RE.finditer(text or ""):
        core = _digit_core(match.group())
        if core:
            result.add(core)
    return result


def _guarded_title(original_title: str, candidate_title: Optional[str]) -> tuple:
    """새 제목은 숫자 집합이 기존의 부분집합일 때만 채택. 아니면 기존 유지.

    잘림 복원·재포맷(숫자 동일/부분집합)은 통과하고, 27.5→33 같은 숫자 변경·추가는
    거부한다(드레인 발행 시 fact_gate 를 못 거치므로 상단 숫자 창작을 원천 차단).

    Returns:
        (채택할 제목, reverted). reverted=True 면 새 제목을 버리고 기존을 유지했다는 뜻.
    """
    candidate = (candidate_title or "").strip()
    if not candidate or candidate == original_title:
        return original_title, False
    if _title_numbers(candidate) <= _title_numbers(original_title):
        return candidate, False
    return original_title, True


def _box_number_tokens(box_text: str) -> list:
    """박스 문자열에서 (digit-core, 단위, 원문) 토큰 목록을 뽑는다."""
    tokens = []
    for match in _BOX_NUM_RE.finditer(box_text or ""):
        core = _digit_core(match.group(1))
        if not core:
            continue
        tokens.append((core, match.group(2) or "", match.group(0).strip()))
    return tokens


def _box_missing_numbers(box_text: str, body_html: str) -> list:
    """박스 숫자 중 본문에 없는 것들의 원문 목록. 비어 있으면 전부 검증됨.

    비교는 콤마·공백을 무시한다. 단위가 있으면 digit-core+단위 동반으로(예 "33만원")
    확인해 연도("2026") 같은 값에 대한 오검증을 줄이고, 단위가 없으면 digit-core 부분일치.
    """
    tokens = _box_number_tokens(box_text)
    if not tokens:
        return []
    body_norm = _digit_core(_strip_tags(body_html))
    missing = []
    for core, unit, raw in tokens:
        # 단위가 있으면 digit-core+단위 동반으로 확인("33만원") — 연도("2026") 안의
        # "20" 같은 부분일치 오검증을 막는다. 단위가 없으면 digit-core 부분일치.
        key = core + unit if unit else core
        if key in body_norm:
            continue
        missing.append(raw)
    return missing


def _box_numbers_verified(box_text: str, body_html: str) -> bool:
    """박스 숫자가 전부 본문에 있으면(또는 숫자가 없으면) True."""
    return not _box_missing_numbers(box_text, body_html)


def _call_claude_with_model(prompt: str, category: str, model: Optional[str]) -> str:
    """content_generator._call_claude 재사용 — model 지정 시에만 임시 override.

    `_call_claude` 는 model 인자를 받지 않고 카테고리 매핑/settings.CLAUDE_MODEL 로
    모델을 고른다. override 는 category 를 비워 매핑을 건너뛰고, 호출 1회 동안만
    settings.CLAUDE_MODEL 을 교체한다(`_call_claude` 가 호출 시점에
    `from config.settings import CLAUDE_MODEL` 로 현재 값을 읽으므로 반영됨) → finally 원복.
    단일 스레드 배치를 전제한다. model 이 None 이면 기존 카테고리 라우팅(기본 sonnet) 그대로.
    """
    from core import content_generator as cg

    if not model:
        return cg._call_claude(prompt, category=category or "")

    import config.settings as app_settings

    previous = getattr(app_settings, "CLAUDE_MODEL", "sonnet")
    app_settings.CLAUDE_MODEL = model
    try:
        return cg._call_claude(prompt, category="")
    finally:
        app_settings.CLAUDE_MODEL = previous


def _generate_top_fields(
    title: str, body_html: str, category: str, model: Optional[str]
) -> Optional[dict]:
    """Claude 로 {title, content(한 줄 요약)} 를 받아 파싱. 실패 시 None(원본 무변경 신호).

    파싱은 content_generator._parse_json_response 를 재사용한다. 그 함수는 title+content
    를 강제하므로 한 줄 요약을 "content" 키로 전송한다(요약 문장이 곧 content 값).
    """
    from core import content_generator as cg

    raw = _call_claude_with_model(_build_prompt(title, body_html), category, model)
    if not raw or not raw.strip():
        log.warning("[draft_top_upgrade] Claude 빈 응답 — 원본 보존")
        return None
    try:
        parsed = cg._parse_json_response(raw, title, category or "")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        log.warning(f"[draft_top_upgrade] 응답 파싱 실패 — 원본 보존: {exc}")
        return None
    if not isinstance(parsed, dict) or not parsed:
        return None
    return parsed


# ─────────────────────────────────────────────────────
# 백업 / 로드 헬퍼
# ─────────────────────────────────────────────────────
def _load_draft_ready(slug: str) -> Optional[dict]:
    """slug 의 draft_ready 보관본 엔트리를 반환. 없거나 다른 status 면 None."""
    from core import draft_store

    by_slug = {entry.get("slug"): entry for entry in draft_store.load_drafts("draft_ready")}
    return by_slug.get(slug)


def _backup_post_data(slug: str) -> None:
    """저장 직전 원본 post_data.json → post_data.bak.json 복사(있을 때만)."""
    from core import draft_store

    source = draft_store._RUNS_DIR / slug / "post_data.json"
    if not source.exists():
        return
    target = draft_store._RUNS_DIR / slug / "post_data.bak.json"
    try:
        shutil.copy2(str(source), str(target))
    except OSError as exc:
        log.warning(f"[draft_top_upgrade] 백업 실패 {slug}: {exc}")


# ─────────────────────────────────────────────────────
# 공개 API — 단건 상단 승격
# ─────────────────────────────────────────────────────
def upgrade_draft_top(
    slug: str, *, dry_run: bool = True, model: Optional[str] = None
) -> dict:
    """draft_ready 보관본 1편의 상단(제목 + 도입부)만 현재 규격으로 승격한다.

    본문(첫 `<h2>` 이후)은 바이트 단위로 그대로 보존한다.

    Args:
        slug: 대상 보관본 슬러그. status 가 draft_ready 가 아니면 거부.
        dry_run: True(기본)면 저장하지 않고 미리보기만 반환.
        model: Claude 모델 override(예: "sonnet"). None 이면 카테고리 라우팅(기본 sonnet).

    Fact 가드(드레인 발행은 fact_gate 를 우회하므로 상단 숫자를 결정론적으로 검증):
        · 가드 1 — 새 제목 숫자가 기존 제목 숫자의 부분집합일 때만 채택(아니면 기존 유지,
          title_reverted=True). 27.5→33 같은 숫자 변경·추가를 거부.
        · 가드 2 — 박스(📌) 숫자가 본문에 실제 존재할 때만 삽입. 지어낸 숫자면 박스 드롭
          (box_status="unverified_dropped"). 숫자 없는 정성 요약은 통과(no_number).

    Returns:
        - draft_ready 아님: {"ok": False, "reason": "not_draft_ready"}
        - 이미 규격 충족(인사말+박스): {"ok": True, "skipped": "already_current", "slug": ...}
        - 파싱 실패: {"ok": False, "reason": "parse_failed"}
        - dry_run: {"ok": True, "dry_run": True, "before", "after", "content_preview",
          "box_status", "title_reverted"}
        - 순 변화 없음(박스 드롭+인사말 이미 있음+제목 유지): {"ok": True, "skipped": "no_change", ...}
        - 저장 성공: {"ok": True, "slug", "title", "box_added", "greeting_added",
          "box_status", "title_reverted"}
        - 예외: {"ok": False, "reason": str(e)}  (원본 무변경)
        box_status ∈ {"verified", "unverified_dropped", "no_number", None(박스 미생성)}.
    """
    try:
        entry = _load_draft_ready(slug)
        if entry is None:
            return {"ok": False, "reason": "not_draft_ready"}

        post_data = entry.get("post_data") or {}
        content = post_data.get("content") or ""
        original_title = post_data.get("title") or entry.get("title") or ""
        category = post_data.get("category") or ""
        if not content:
            return {"ok": False, "reason": "empty_content"}

        top, body = _split_top_body(content)
        box_present = bool(_BOX_PRESENT_RE.search(top))
        greeting_present = bool(_GREETING_RE.search(top))

        # 멱등 — 인사말 + 박스가 모두 있으면 손대지 않는다.
        if box_present and greeting_present:
            return {"ok": True, "skipped": "already_current", "slug": slug}

        new_title = original_title
        title_reverted = False
        box_paragraph: Optional[str] = None
        box_text = ""
        box_status: Optional[str] = None

        # Claude 호출은 박스가 없을 때만(있으면 기존 박스 유지 — 중복 삽입 금지).
        if not box_present:
            parsed = _generate_top_fields(original_title, body, category, model)
            if parsed is None:
                return {"ok": False, "reason": "parse_failed"}
            lead = parsed.get("lead_box") or parsed.get("content") or ""
            box_text = _normalize_box_text(lead)
            if not box_text:
                return {"ok": False, "reason": "parse_failed"}

            # 가드 1 — 제목 보수화: 새 제목 숫자가 기존의 부분집합일 때만 채택.
            new_title, title_reverted = _guarded_title(original_title, parsed.get("title"))

            # 가드 2 — 박스 숫자는 본문에 실제 있는 값만. 지어낸 숫자면 박스 드롭(원본 상단 유지,
            # 인사말 보정은 유지). 재시도 없이 단순·안전 우선.
            if not _box_number_tokens(box_text):
                box_status = "no_number"          # 정성 요약 — 통과
                box_paragraph = _box_html(box_text)
            else:
                missing = _box_missing_numbers(box_text, body)
                if missing:
                    box_status = "unverified_dropped"
                    log.warning(
                        f"[draft_top_upgrade] {slug} 박스 드롭 — 본문에 없는 숫자 "
                        f"{missing} (지어낸 값 차단)"
                    )
                    box_text = ""                 # 드롭 — 박스 미삽입
                else:
                    box_status = "verified"
                    box_paragraph = _box_html(box_text)

        new_top = _reassemble_top(top, box_paragraph, greeting_present, category)
        new_content = new_top + body

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "before": {"title": original_title, "box_present": box_present},
                "after": {"title": new_title, "lead_box": box_text},
                "content_preview": new_top[:400],
                "box_status": box_status,
                "title_reverted": title_reverted,
            }

        # 순 변화가 없으면(박스 드롭 + 인사말 이미 있음 + 제목 유지) 저장하지 않는다 —
        # created_at 리셋·불필요한 백업 방지. 품질 카운트는 box_status 로 그대로 전달.
        if new_content == content and new_title == original_title:
            return {
                "ok": True,
                "skipped": "no_change",
                "slug": slug,
                "box_status": box_status,
                "title_reverted": title_reverted,
            }

        # 저장 — 원본 백업 후 제자리 갱신(본문·이미지·태그·카테고리 보존).
        _backup_post_data(slug)
        from core import draft_store

        new_post_data = {
            "title": new_title,
            "content": new_content,
            "tags": post_data.get("tags") or [],
            "category": post_data.get("category"),
        }
        draft_store.save_draft(
            slug,
            new_post_data,
            post_data.get("image_paths") or [],
            entry.get("schedule_at"),
        )
        return {
            "ok": True,
            "slug": slug,
            "title": new_title,
            "box_added": box_paragraph is not None,
            "greeting_added": not greeting_present,
            "box_status": box_status,
            "title_reverted": title_reverted,
        }
    except Exception as exc:  # noqa: BLE001 — fail-open 계약: 어떤 예외에도 원본 무변경
        log.error(f"[draft_top_upgrade] {slug} 실패: {exc}", exc_info=True)
        return {"ok": False, "reason": str(exc)}


# ─────────────────────────────────────────────────────
# 진행상황 (재개용)
# ─────────────────────────────────────────────────────
def _load_progress() -> set:
    """처리 완료 슬러그 집합을 로드(없으면 빈 집합)."""
    try:
        data = safe_load_json(_PROGRESS_PATH, default=None)
        if isinstance(data, dict):
            done = data.get("processed")
            if isinstance(done, list):
                return {str(item) for item in done}
    except (OSError, ValueError, TypeError) as exc:
        log.debug(f"[draft_top_upgrade] 진행상황 로드 실패: {exc}")
    return set()


def _save_progress(processed: set) -> None:
    """처리 완료 슬러그 집합을 원자적으로 저장."""
    try:
        _PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            _PROGRESS_PATH,
            {
                "processed": sorted(processed),
                "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            },
        )
    except (OSError, ValueError, TypeError) as exc:
        log.warning(f"[draft_top_upgrade] 진행상황 저장 실패: {exc}")


def _target_slugs(scope: str) -> List[str]:
    """처리 대상 슬러그 목록. elite=발행 버킷(수요·품질순), all=draft_ready 전체."""
    from core import draft_store

    if scope == "elite":
        try:
            from core import draft_triage

            result = draft_triage.triage_drafts()
            publish = sorted(
                result["buckets"]["publish"],
                key=lambda item: (-item.get("demand", 0), -item.get("aeo", 0)),
            )
            slugs = [item["slug"] for item in publish if item.get("slug")]
            if slugs:
                return slugs
            log.info("[draft_top_upgrade] elite 발행 버킷 비어 있음 — all 로 폴백")
        except (KeyError, ImportError, OSError, ValueError, TypeError) as exc:
            log.warning(f"[draft_top_upgrade] elite scope 실패 — all 폴백: {exc}")

    return [entry["slug"] for entry in draft_store.load_drafts("draft_ready") if entry.get("slug")]


# ─────────────────────────────────────────────────────
# 공개 API — 배치(재개 가능)
# ─────────────────────────────────────────────────────
def run_top_upgrade_batch(
    *,
    scope: str = "elite",
    limit: Optional[int] = None,
    dry_run: bool = True,
    progress_cb: Optional[Callable[[str, str, int, int], None]] = None,
) -> dict:
    """상단 승격을 대량으로 실행하는 재개 가능 루프.

    재개는 두 겹으로 보장된다:
      1) 멱등 — 이미 승격된 글은 upgrade_draft_top 이 'already_current' 로 건너뛴다.
      2) 진행상황 파일 — 실제 저장(비 dry_run) 성공/스킵 슬러그를 기록해, 재실행 시
         load/Claude 없이 곧바로 건너뛴다(dry_run 은 아무것도 바꾸지 않으므로 미기록).

    Args:
        scope: "elite"(발행 버킷 우선) 또는 "all"(draft_ready 전체).
        limit: 이번 실행에서 처리할 최대 편수. None 이면 전부.
        dry_run: True(기본)면 저장 없이 미리보기만.
        progress_cb: progress_cb(slug, status, i, n) 로 진행 통지.
            status ∈ {"resume_skip", "upgraded", "skipped", "failed"}.

    Returns:
        {"total", "upgraded", "skipped", "failed", "box_verified", "box_dropped",
         "title_reverted", "scope", "dry_run"}.
        (total 은 이번 실행에서 실제로 처리를 시도한 편수 — resume_skip 은 제외.
         box_verified/box_dropped/title_reverted 는 발행 전 fact 품질 집계용.)
    """
    slugs = _target_slugs(scope)
    processed = _load_progress()
    total_count = len(slugs)
    counts = {
        "total": 0,
        "upgraded": 0,
        "skipped": 0,
        "failed": 0,
        "box_verified": 0,
        "box_dropped": 0,
        "title_reverted": 0,
        "scope": scope,
        "dry_run": dry_run,
    }
    attempted = 0

    for index, slug in enumerate(slugs, start=1):
        if limit is not None and attempted >= limit:
            break

        # 진행상황 파일에 기록된(이미 실제 처리된) 슬러그는 재실행 시 건너뛴다.
        if slug in processed:
            if progress_cb:
                progress_cb(slug, "resume_skip", index, total_count)
            continue

        result = upgrade_draft_top(slug, dry_run=dry_run)
        counts["total"] += 1
        attempted += 1

        if result.get("ok") and result.get("skipped"):
            status = "skipped"
            counts["skipped"] += 1
        elif result.get("ok"):
            status = "upgraded"
            counts["upgraded"] += 1
        else:
            status = "failed"
            counts["failed"] += 1

        # 품질 집계 — box_status/title_reverted 는 박스 생성 시도가 있었던 결과에만 존재.
        box_status = result.get("box_status")
        if box_status == "verified":
            counts["box_verified"] += 1
        elif box_status == "unverified_dropped":
            counts["box_dropped"] += 1
        if result.get("title_reverted"):
            counts["title_reverted"] += 1

        # 실제 저장(비 dry_run) 성공/스킵만 진행상황에 남긴다 — dry_run 은 원본을
        # 바꾸지 않으므로 기록하면 후속 실제 실행이 잘못 건너뛴다. 실패는 다음에 재시도.
        if not dry_run and result.get("ok"):
            processed.add(slug)
            _save_progress(processed)

        if progress_cb:
            progress_cb(slug, status, index, total_count)

    log.info(
        f"[draft_top_upgrade] 배치 완료(scope={scope}, dry_run={dry_run}): "
        f"시도 {counts['total']} · 승격 {counts['upgraded']} · 스킵 {counts['skipped']} · "
        f"실패 {counts['failed']} · 박스검증 {counts['box_verified']} · "
        f"박스드롭 {counts['box_dropped']} · 제목보존 {counts['title_reverted']}"
    )
    return counts


__all__ = ["upgrade_draft_top", "run_top_upgrade_batch"]
