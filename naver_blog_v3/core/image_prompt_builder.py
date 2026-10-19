"""image_prompt_builder

examplebrand 네이버 블로그 이미지 자동 생성용 codex exec 프롬프트 합성기.

- `templates/image_prompts.json`의 4개 역할(thumb/body/og/info) 베이스 프롬프트를
  로드해 카테고리 페르소나·제목·부제·일러스트 힌트를 끼워 넣어 최종 영문 프롬프트를
  돌려준다.
- `templates/prompt_strategies.json`이 한국어 페르소나의 SSOT이지만, gpt-image-2가
  영어 directive를 더 안정적으로 해석하므로 카테고리별 영문 directive를 별도로 보관한다.
- Day 2의 `codex_cli_bot`은 `build_image_prompt(...)`로 프롬프트를 만들고,
  `codex exec` 실행 직후 `latest_generated_image(...)`로 결과 PNG 경로를 회수한다.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, List, Literal, Optional

try:  # 로거 — 실패해도(경량 임포트 환경) 무해한 no-op 로 폴백.
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001 — config 미로드 환경(테스트 스텁)에서도 임포트 성공 보장
    import logging
    log = logging.getLogger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# 상수
# ──────────────────────────────────────────────────────────────────────────────

ROLE = Literal["thumb", "body", "og", "info"]
VALID_ROLES: tuple[str, ...] = ("thumb", "body", "og", "info")

SIZE: str = "2048x2048"
PALETTE_MINT: str = "#b0f4d6"
PALETTE_PURPLE: str = "#a78bfa"
PALETTE_WHITE: str = "#ffffff"

_TEMPLATES_DIR: Path = Path(__file__).parent.parent / "templates"
_IMAGE_PROMPTS_PATH: Path = _TEMPLATES_DIR / "image_prompts.json"
_STRATEGIES_PATH: Path = _TEMPLATES_DIR / "prompt_strategies.json"

_GENERATED_IMAGE_GLOB: str = "ig_*.png"
# ★2026-09-09 실측: codex 는 산출물을 `ig_*.png` 로만 저장하지 않는다. 실제
#   generated_images/ 에 `exec-*.png` 형태도 존재한다(예: 01a08189-…/exec-…png).
#   글롭이 `ig_*` 하나뿐이라 그런 산출물은 통째로 놓쳐 no_png 실패가 됐다.
#   신규 core.codex_image 는 확장자 기준으로 잡도록 고쳤고, 형제 모듈인 이 함수에도
#   같은 수정을 전파한다(한쪽만 고치고 다른 쪽을 빠뜨리는 재발 패턴 차단).
_GENERATED_IMAGE_GLOBS: tuple = ("ig_*.png", "exec-*.png")
_DEFAULT_SESSION_ROOT: Path = Path.home() / ".codex" / "generated_images"

# 카테고리별 영문 페르소나/톤 directive.
# prompt_strategies.json의 한글 persona/tone을 examplebrand 운용 톤에 맞춰 영문으로
# 큐레이션한 결과(한국어 그대로 박지 않고 gpt-image-2가 잘 해석하는 형태로 변환).
_PERSONA_DIRECTIVES: dict[str, str] = {
    "청년": (
        "Subject persona: young Korean adults in their 20s and 30s "
        "(early-career workers and job seekers). "
        "Tone: friendly, practical, plain-language explanation of administrative terms. "
        "Do not depict seniors or elderly subjects."
    ),
    "복지": (
        "Subject persona: Korean adults aged 50 to 70, or the adult child searching on a parent's behalf. "
        "Tone: respectful and easy to understand, step-by-step pacing. "
        "Avoid trendy youth slang in the illustration."
    ),
    "교육": (
        "Subject persona: Korean parents or university students, or adults interested in continuing education. "
        "Tone: trustworthy and information-dense, comparison-table friendly. "
        "Show notebooks, schedules, or calendars rather than elderly subjects."
    ),
    "취업": (
        "Subject persona: Korean job seekers, career-changers, and women re-entering the workforce. "
        "Tone: encouraging yet concrete, action-oriented. "
        "Show a confident working-age Korean character with a resume, briefcase, or office cue."
    ),
    "주거": (
        "Subject persona: Korean adults seeking housing stability "
        "(newlyweds, single-person households, non-homeowners). "
        "Tone: meticulous and practical, with clear distinction by income bracket. "
        "Show apartment, key, or small house icons rather than elderly subjects."
    ),
    "의료": (
        "Subject persona: Korean patients or family caregivers feeling burdened by medical costs. "
        "Tone: warm and reassuring, step-by-step explanation of complex healthcare procedures. "
        "Show stethoscope, prescription, or hospital cue with a calm Korean character."
    ),
    "바우처": (
        "Subject persona: Korean voucher recipients or their family members "
        "wondering where and how to spend the voucher. "
        "Tone: kind and concrete, covering usable stores, balance check, and card pickup. "
        "Show a card and a small map or store icon."
    ),
    "부동산": (
        "Subject persona: Korean adults in their 30s to 50s researching home purchase, "
        "subscription (cheongyak), trade, or lease. "
        "Tone: calm, fact-centered, statistics-aware. No price predictions. "
        "Show apartment buildings or document icons; cite that figures vary by date."
    ),
    "절세": (
        "Subject persona: Korean salaried workers, self-employed, and retirees who file taxes themselves. "
        "Tone: precise and procedure-oriented, with the relevant tax year noted. "
        "Show a calculator, receipt, or Hometax-style screen mockup."
    ),
    "금융": (
        "Subject persona: Korean adults aged 30 to 60 comparing savings, loans, and credit cards. "
        "Tone: objective, comparison-driven, neutral about specific products. "
        "Show a chart, scale, or comparison table icon; never a specific real bank name."
    ),
    "노후": (
        "Subject persona: Korean adults aged 50 to 65 nearing retirement, "
        "or the adult child preparing alongside their parent. "
        "Tone: calm and trustworthy, with year-based caveats. No guarantees of payout amounts. "
        "Show a relaxed Korean character with a savings-jar or pension-document cue."
    ),
    "디지털": (
        "Subject persona: Korean adults aged 50 to 70 who need digital onboarding help, "
        "or the adult child guiding their parent. "
        "Tone: very kind, step-by-step, with concrete on-screen menu locations. "
        "Show a smartphone or laptop mockup with a clearly highlighted button."
    ),
    "default": (
        "Subject persona: members of the Korean general public interested in government support policies. "
        "Tone: friendly yet trustworthy, with clear and concise key information. "
        "Use a neutral working-age Korean character."
    ),
}


# ──────────────────────────────────────────────────────────────────────────────
# 내부 유틸
# ──────────────────────────────────────────────────────────────────────────────


class _SafePromptDict(dict):
    """`str.format_map`이 누락 키를 만나도 빈 문자열로 폴백하게 만드는 dict."""

    def __missing__(self, key: str) -> str:  # noqa: D401 - dict hook
        return ""


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"필수 템플릿 파일 없음: {path}")
    with path.open("r", encoding="utf-8") as fp:
        data = json.load(fp)
    if not isinstance(data, dict):
        raise ValueError(f"JSON 루트가 객체가 아님: {path}")
    return data


def _template_path(filename: str, fallback: Path) -> Path:
    """브랜드 팩 템플릿 오버라이드 경로 해석(로드 시점).

    named 프로필이 `brand/templates/<filename>`을 두면 그것, 없으면 공유 템플릿(fallback).
    default 프로필·브랜드 모듈 로드 실패 시 항상 fallback(기존 동작 무변화).
    """
    try:
        from config import brand as _brand
        return _brand.template_path(filename)
    except Exception:  # noqa: BLE001 — 브랜드 라우터 실패 시 하드코딩 폴백
        return fallback


def _resolve_persona_directive(category: str) -> str:
    """카테고리 → 영문 directive. 미지 카테고리는 default로 폴백."""
    if category in _PERSONA_DIRECTIVES:
        return _PERSONA_DIRECTIVES[category]
    # prompt_strategies.json에 카테고리가 있으면 default를 쓰되, 없으면 그래도 default.
    # (영문 directive 큐레이션이 SSOT이므로 strategies는 존재 여부만 참고)
    try:
        strategies = _load_json(_template_path("prompt_strategies.json", _STRATEGIES_PATH))
    except (FileNotFoundError, ValueError):
        strategies = {}
    _ = strategies.get(category)  # 향후 톤·금지어 활용 여지 (현재는 영문 directive 사용)
    # default 폴백 — named 프로필이 자기 분야 directive를 주면 그것, 없으면 내장 directive.
    try:
        from config import brand as _brand
        return _brand.image_persona_default() or _PERSONA_DIRECTIVES["default"]
    except Exception:  # noqa: BLE001 — 브랜드 로드 실패 시 내장 directive
        return _PERSONA_DIRECTIVES["default"]


def _render_items(body_items: Iterable[dict[str, Any]]) -> str:
    """info 역할의 `{items}` placeholder 렌더링.

    각 item은 `{"q": ..., "a": ...}` 또는 `{"step": ..., "desc": ...}` 형태.
    """
    lines: list[str] = []
    q_index = 1
    step_index = 1
    for item in body_items:
        if not isinstance(item, dict):
            raise ValueError(f"body_items 원소는 dict여야 함: {item!r}")
        if "q" in item and "a" in item:
            q = str(item["q"]).strip()
            a = str(item["a"]).strip()
            lines.append(f'Q{q_index}: "{q}"  ->  A: "{a}"')
            q_index += 1
        elif "step" in item and "desc" in item:
            step = str(item["step"]).strip()
            desc = str(item["desc"]).strip()
            lines.append(f'Step{step_index}: "{step}"  ->  "{desc}"')
            step_index += 1
        else:
            raise ValueError(
                f"body_items 원소는 q/a 또는 step/desc 키를 가져야 함: {item!r}"
            )
    if not lines:
        raise ValueError("body_items가 비어 있음")
    return "\n".join(lines)


def _validate_text(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name}는 비어있지 않은 문자열이어야 함")


# ──────────────────────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────────────────────


def build_image_prompt(
    *,
    role: ROLE,
    title: str,
    subtitle: str,
    category: str,
    body_items: Optional[list[dict[str, Any]]] = None,
    illustration_hint: Optional[str] = None,
) -> str:
    """역할별 codex exec용 영문 프롬프트를 합성한다.

    Args:
        role: thumb / body / og / info 중 하나.
        title: 한글 메인 제목 (예: "2026 청년 월세 지원금").
        subtitle: 한글 부제 (예: "신청 자격 · 금액 · 서류 한 번에").
        category: examplebrand 카테고리 키. 미지 카테고리는 default로 폴백.
        body_items: info 역할에서 Q&A 또는 step 카드 데이터.
        illustration_hint: 일러스트 자유 힌트(영문 권장, 한글도 허용).

    Returns:
        완성된 영문 codex exec 프롬프트 문자열.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"role은 {VALID_ROLES} 중 하나여야 함: {role!r}")
    _validate_text("title", title)
    _validate_text("subtitle", subtitle)
    _validate_text("category", category)

    prompts = _load_json(_template_path("image_prompts.json", _IMAGE_PROMPTS_PATH))
    roles = prompts.get("roles")
    if not isinstance(roles, dict) or role not in roles:
        raise ValueError(f"image_prompts.json에 role 정의 없음: {role}")
    template = roles[role].get("prompt_template")
    if not isinstance(template, str) or not template:
        raise ValueError(f"role {role}의 prompt_template이 비어 있음")

    negative = prompts.get("_global_negative", "")
    if not isinstance(negative, str) or not negative.strip():
        raise ValueError("_global_negative가 비어 있음")
    negative_clause = f"Negative constraints: {negative}."

    persona_directive = _resolve_persona_directive(category)

    illust = (illustration_hint or "").strip()
    illust_clause = f"Illustration hint: {illust}" if illust else ""

    items_block = ""
    if role == "info":
        if not body_items:
            raise ValueError("role='info'에는 body_items가 필수")
        items_block = _render_items(body_items)
    elif body_items:
        # info가 아닌 역할에 body_items가 들어오면 무시하지 않고 명시적으로 거부.
        raise ValueError("body_items는 role='info'에서만 사용됨")

    mapping = _SafePromptDict(
        title=title,
        subtitle=subtitle,
        persona_directive=persona_directive,
        illustration_hint=illust_clause,
        items=items_block,
        negative=negative_clause,
    )
    rendered = template.format_map(mapping)
    if role == "thumb":
        rendered = "Aspect ratio: square 1:1. Keep the complete title and subject inside the frame.\n" + rendered

    # 연속 공백/빈 줄 정리 — placeholder가 비었을 때 생기는 빈 줄을 한 번만 둔다.
    cleaned_lines: list[str] = []
    blank_run = 0
    for line in rendered.splitlines():
        if line.strip() == "":
            blank_run += 1
            if blank_run <= 1:
                cleaned_lines.append("")
        else:
            blank_run = 0
            cleaned_lines.append(line)
    return "\n".join(cleaned_lines).strip() + "\n"


# ──────────────────────────────────────────────────────────────────────────────
# 무텍스트 일러스트 프롬프트 (FLUX 등 한글을 못 그리는 모델용)
# ──────────────────────────────────────────────────────────────────────────────
_NO_TEXT_CLAUSE = (
    "CRITICAL RULE — render ZERO text of any kind: no letters, no words, no numbers, "
    "no Korean characters, no Hangul, no Latin text, no captions, no labels, no titles, "
    "no headings, no signage, no posters, no banners, no whiteboard or blackboard writing, "
    "no book titles, no screen text, no UI text, no speech bubbles, no watermark, no logo. "
    "Every wall, book cover, screen, paper, sign and surface is completely BLANK and empty. "
    "The picture is purely visual illustration with NO written characters anywhere. "
    "Leave calm, uncluttered empty margins at the top and bottom for later software captions."
)
_ROLE_COMPOSITION = {
    "thumb": (
        "Square 1:1 composition. Single friendly Korean cartoon character centered, "
        "warm round face, holding or "
        "pointing to one concrete object related to the topic. Simple soft background. "
        "Keep the top third and bottom sixth of the frame visually calm and empty."
    ),
    "body": (
        "Editorial illustration with a clear concrete composition (a before/after split or "
        "a left-to-right sequence) using Korean cartoon characters and simple icons. "
        "Keep the top area calm and empty for a caption."
    ),
    "info": (
        "Soft, calm, uncluttered themed background scene with a few small related icons near "
        "the edges only. Keep the center and lower two-thirds simple, low-contrast and empty "
        "so a text panel can be overlaid on top."
    ),
}
_ILLUST_STYLE = (
    "Kawaii pastel sticker-art illustration style: thick clean rounded outlines, "
    "flat adorable shapes, big cute friendly expressions, soft pastel palette, "
    "gentle soft lighting, cohesive and high quality."
)


def build_illustration_prompt(
    *,
    role: str,
    title: str = "",          # 참고용 — 프롬프트에 문자로 노출하지 않는다.
    subtitle: str = "",       # 참고용 — 동일.
    category: str = "default",
    illustration_hint: Optional[str] = None,
    body_items: Optional[Iterable[dict[str, Any]]] = None,   # 미사용(텍스트는 오버레이가 그림)
) -> str:
    """한글을 못 그리는 모델(FLUX 등)용 '글자 없는 일러스트' 프롬프트를 만든다.

    제목·부제·Q&A 같은 한글 텍스트는 프롬프트에 절대 넣지 않는다 — 그건 코드 오버레이
    (`core.text_overlay`)가 이미지 위에 그린다. 여기서는 소재(페르소나)·구도·스타일과
    강한 no-text 제약만 준다.
    """
    if role not in _ROLE_COMPOSITION:
        raise ValueError(f"지원되지 않는 role: {role}")
    persona = _resolve_persona_directive(category)
    comp = _ROLE_COMPOSITION[role]
    hint = (illustration_hint or "").strip()
    parts = [
        "One single clean flat illustration, purely visual with no text of any kind.",
        comp,
        persona,
        f"Scene hint (subject only, never render as text): {hint}" if hint else "",
        _ILLUST_STYLE,
        _NO_TEXT_CLAUSE,
    ]
    return "\n".join(p for p in parts if p) + "\n"


def latest_generated_image(
    session_dir_root: Path = _DEFAULT_SESSION_ROOT,
    min_mtime: Optional[float] = None,
) -> Path:
    """codex CLI가 저장한 모든 세션 폴더에서 mtime이 가장 최근인 산출물 이미지 절대 경로.

    대상 파일명: ``ig_*.png`` 및 ``exec-*.png``(둘 다 실측 확인된 codex 저장 형식).

    Args:
        session_dir_root: 세션 폴더 검색 루트.
        min_mtime: 주어지면 ``st_mtime > min_mtime`` 인 파일만 후보로 삼는다.
            "이번 호출로 새로 생성된 파일"만 채택해, 과거의 무관한 PNG(다른 슬러그·
            다른 세션·직전 카드 잔여물)를 조용히 재채택하는 것을 막는다. None(기본)이면
            전역 최신 파일을 고른다(기존 동작 불변).

    Raises:
        FileNotFoundError: 세션 루트가 없거나, 조건에 맞는 ig_*.png가 하나도 없을 때.
    """
    root = Path(session_dir_root)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"codex generated_images 루트 없음: {root}")

    latest: Optional[Path] = None
    latest_mtime: float = -1.0
    seen: set = set()
    candidates = [p for g in _GENERATED_IMAGE_GLOBS for p in root.rglob(g)]
    for png in candidates:
        if not png.is_file() or str(png) in seen:
            continue
        seen.add(str(png))
        try:
            mtime = png.stat().st_mtime
        except OSError:
            continue
        # NTFS mtime 세밀도 한계를 고려해 strict '>' — 호출 직전 시각과 동률은 배제.
        if min_mtime is not None and not (mtime > min_mtime):
            continue
        if mtime > latest_mtime:
            latest_mtime = mtime
            latest = png

    if latest is None:
        raise FileNotFoundError(
            f"{root} 하위에서 {'/'.join(_GENERATED_IMAGE_GLOBS)} 파일을 찾지 못함"
            + (f" (min_mtime={min_mtime} 이후 신규 없음)" if min_mtime is not None else "")
        )
    return latest.resolve()


# ══════════════════════════════════════════════════════════════════════════════
# 본문 분석형 2단계 인포카드 (GPT-Image 로 한글을 이미지에 직접 baked)
# ──────────────────────────────────────────────────────────────────────────────
# 기존 build_illustration_prompt(FLUX 무텍스트 일러스트 + text_overlay 덧그림) 경로와
# 정반대다. 여기서는 Claude 가 본문 전체를 읽고 카드뉴스 세트를 기획(Stage1)한 뒤,
# GPT-Image 가 주어진 한글을 이미지에 정확히 새기도록(Stage2) 리치 영문 directive 를
# 만든다. 이 경로는 전부 신규·옵트인이며 위 공개 API 를 하나도 건드리지 않는다.
# ══════════════════════════════════════════════════════════════════════════════

# 서비스 런타임 Claude 기본 모델(하네스 기본 = sonnet). fable-5 금지.
_INFOCARD_PLAN_MODEL: str = "sonnet"
_INFOCARD_PLAN_TIMEOUT_SEC: int = 180
# CLI 인자 길이 한계(특히 Windows)를 넘지 않도록 기획 프롬프트에 넣는 본문 상한.
# fact-grounding 검증은 항상 원본 전체(article_text)로 수행하므로 이 상한과 무관하다.
_INFOCARD_ARTICLE_PROMPT_CAP: int = 12000

# 허용 enum + 안전 기본값(coerce 대상). 모르는 값은 하드페일 대신 기본으로 강등.
_INFOCARD_ROLES: frozenset = frozenset({"hero", "info", "closing"})
_INFOCARD_ROLE_DEFAULT: str = "info"

_INFOCARD_LAYOUTS: dict[str, str] = {
    "hero_split": (
        "Vertical hero cover: the big Korean headline sits large and bold in the upper "
        "two-thirds, the model stands on the right third holding or presenting toward the "
        "title, a soft rounded panel behind the text for contrast."
    ),
    "numbered_list_5": (
        "A vertical stack of numbered rounded cards (circular number badges 1,2,3...), each "
        "row shows one label on the left and its value on the right with a small line icon; "
        "even row spacing, generous padding, the model small in a bottom corner."
    ),
    "badge_stack": (
        "A stack of pill/badge chips, each chip holds one short Korean label with an icon; "
        "chips arranged in a clean vertical rhythm on a gradient background, the model peeking "
        "from one side."
    ),
    "checklist": (
        "A checklist panel: each row starts with a rounded check mark, followed by one Korean "
        "item; a light card background, the model pointing at the list."
    ),
    "faq_qa": (
        "A Q&A card: a bold 'Q.' bubble with the question, below it an 'A.' bubble with the "
        "answer, rounded speech-bubble shapes, the model in a thinking pose to one side."
    ),
    "compare_2col": (
        "A two-column comparison table with a clear divider, a header cell per column and rows "
        "of paired Korean labels/values, alternating row tint for readability."
    ),
    "steps_flow": (
        "A left-to-right / top-to-bottom step flow with numbered circular nodes connected by "
        "arrows, one short Korean caption per step, the model presenting beside the flow."
    ),
    "closing_banner": (
        "A calm closing banner: one warm Korean closing line centered, a subtle source/credit "
        "line at the very bottom, the model waving or presenting warmly, lots of clean margin."
    ),
}
_INFOCARD_LAYOUT_DEFAULT: str = "badge_stack"

_INFOCARD_PALETTES: dict[str, str] = {
    "navy_gold": (
        "deep navy background (#12213f) with warm gold accents (#e7b64b) and off-white text "
        "(#f7f7f2); premium, trustworthy government-brochure mood."
    ),
    "mint_teal": (
        "soft mint (#b7f4dc) to teal (#2bb6a3) gradient with charcoal text (#1f2d2b); fresh, "
        "friendly, approachable."
    ),
    "purple_gradient": (
        "violet (#7c5cff) to indigo (#4b32c3) gradient with white text (#ffffff) and light "
        "lavender panels; modern, energetic."
    ),
    "teal_deep": (
        "deep teal (#0f5a5e) with aqua accents (#3fd0c9) and cream text (#f4efe4); calm, "
        "clinical-clean, reassuring."
    ),
    "blue_clean": (
        "clean white background with clear blue accents (#2f6fed) and dark slate text "
        "(#22303c); minimal, corporate, high legibility."
    ),
}
_INFOCARD_PALETTE_DEFAULT: str = "navy_gold"

_MODEL_POSES: dict[str, str] = {
    "holding_book": "holding an open booklet, gesturing toward the reader",
    "check_list": "holding a checklist clipboard and pointing at the checkmarks",
    "pointing": "pointing toward the headline with an open, confident hand",
    "thinking": "one hand near the chin in a thoughtful, reassuring pose",
    "presenting": "standing and presenting the information with an open palm gesture",
    "holding_card": "holding a small card, showing it toward the viewer",
}
_MODEL_POSE_DEFAULT: str = "presenting"

# 일관된 브랜드 모델 기본 묘사 — config.brand.image_model_desc() 또는
# config.settings.IMAGE_MODEL_DESC 로 오버라이드 가능(둘 다 없으면 이 기본값).
_BRAND_MODEL_DESC: str = (
    "a consistent professional Korean woman in her early 30s, wearing a clean navy business "
    "suit, long wavy dark-brown hair, a warm confident friendly expression, natural soft "
    "studio lighting; keep the exact same face, hairstyle and outfit across every card"
)

_INFOCARD_PLAN_SCHEMA: str = json.dumps(
    {
        "type": "object",
        "properties": {
            "cards": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "role": {"type": "string", "enum": sorted(_INFOCARD_ROLES)},
                        "headline": {"type": "string"},
                        "subhead": {"type": "string"},
                        "bullets": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "label": {"type": "string"},
                                    "value": {"type": "string"},
                                    "icon_hint": {"type": "string"},
                                },
                                "required": ["label"],
                            },
                        },
                        "layout": {"type": "string", "enum": sorted(_INFOCARD_LAYOUTS)},
                        "palette": {"type": "string", "enum": sorted(_INFOCARD_PALETTES)},
                        "model_pose": {"type": "string", "enum": sorted(_MODEL_POSES)},
                        "fact_refs": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["role", "headline", "layout", "palette", "model_pose"],
                },
            }
        },
        "required": ["cards"],
    },
    ensure_ascii=False,
)


@dataclass
class InfoCardBullet:
    """카드 한 행 — 라벨(한글) + 값(한글, 선택) + 아이콘 힌트(영문, 선택)."""

    label: str
    value: str = ""
    icon_hint: str = ""


@dataclass
class InfoCard:
    """카드 1장의 구성안(Stage1 산출)."""

    role: str
    headline: str
    layout: str
    palette: str
    model_pose: str
    subhead: str = ""
    bullets: List[InfoCardBullet] = field(default_factory=list)
    fact_refs: List[str] = field(default_factory=list)


@dataclass
class ImagePlan:
    """카드 세트 구성안 — plan_infocards 반환. cards 가 비면 폴백 신호."""

    cards: List[InfoCard] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


@dataclass
class FlaggedCard:
    """fact-grounding 또는 구조 검증에 걸린 카드와 사유."""

    card: InfoCard
    reason: str


@dataclass
class ValidationReport:
    """검증 결과 — 근거 있는 카드만 grounded_cards 로 통과."""

    grounded_cards: List[InfoCard] = field(default_factory=list)
    flagged: List[FlaggedCard] = field(default_factory=list)
    ok: bool = False


# ──────────────────────────────────────────────────────────────────────────────
# 내부 유틸 — Claude CLI 호출 · JSON 추출 · 숫자 근거 검증
# ──────────────────────────────────────────────────────────────────────────────


def _claude_cmd() -> str:
    """content_generator 와 동일한 단일 경로로 claude 실행 파일을 찾는다."""
    try:
        from core.content_generator import _find_claude_cmd
        return _find_claude_cmd()
    except (ImportError, AttributeError):
        import shutil
        return shutil.which("claude") or ""


def _extract_json_object(text: str) -> Optional[dict]:
    """모델 응답에서 첫 균형 잡힌 JSON 객체를 추출(코드펜스·뒤 산문 안전 제거).

    ``--json-schema`` 는 순수 JSON 을 강제하지 않아 응답이 ```json 코드펜스 + 설명
    산문으로 나온다. 문자열 리터럴 내부의 중괄호를 세지 않도록 상태를 추적하며
    첫 ``{`` 부터 균형이 맞는 지점까지 잘라 파싱한다.
    """
    if not text:
        return None
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip())
    start = stripped.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(stripped)):
        char = stripped[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(stripped[start:index + 1])
                    return parsed if isinstance(parsed, dict) else None
                except ValueError:
                    return None
    return None


def _coerce_choice(value: object, allowed, default: str) -> str:
    """enum 값 coerce — 허용 집합에 있으면 그대로, 아니면 안전 기본값."""
    text = str(value or "").strip()
    return text if text in allowed else default


def _coerce_bullets(raw_bullets: object) -> List[InfoCardBullet]:
    """모델이 준 bullets 를 InfoCardBullet 리스트로. 라벨 없는 행은 버린다."""
    bullets: List[InfoCardBullet] = []
    if not isinstance(raw_bullets, list):
        return bullets
    for item in raw_bullets:
        if not isinstance(item, dict):
            continue
        label = str(item.get("label") or "").strip()
        if not label:
            continue
        bullets.append(InfoCardBullet(
            label=label,
            value=str(item.get("value") or "").strip(),
            icon_hint=str(item.get("icon_hint") or "").strip(),
        ))
    return bullets


def _card_from_dict(raw: dict) -> Optional[InfoCard]:
    """plan JSON 의 card dict 하나를 InfoCard 로. headline 없으면 None."""
    if not isinstance(raw, dict):
        return None
    headline = str(raw.get("headline") or "").strip()
    if not headline:
        return None
    fact_refs_raw = raw.get("fact_refs")
    fact_refs = (
        [str(v).strip() for v in fact_refs_raw if str(v).strip()]
        if isinstance(fact_refs_raw, list) else []
    )
    return InfoCard(
        role=_coerce_choice(raw.get("role"), _INFOCARD_ROLES, _INFOCARD_ROLE_DEFAULT),
        headline=headline,
        layout=_coerce_choice(raw.get("layout"), _INFOCARD_LAYOUTS, _INFOCARD_LAYOUT_DEFAULT),
        palette=_coerce_choice(raw.get("palette"), _INFOCARD_PALETTES, _INFOCARD_PALETTE_DEFAULT),
        model_pose=_coerce_choice(raw.get("model_pose"), _MODEL_POSES, _MODEL_POSE_DEFAULT),
        subhead=str(raw.get("subhead") or "").strip(),
        bullets=_coerce_bullets(raw.get("bullets")),
        fact_refs=fact_refs,
    )


# 숫자·날짜·%·통화 토큰(단위는 긴 것부터 — 정규식 alternation 은 앞선 매치 우선).
_NUMBER_UNIT = (
    r"%|％|퍼센트|만원|억원|천원|개월|가구|시간|원|억|만|천|년|월|일|세|명|건|회|분|평|㎡"
)
_NUMBER_TOKEN_RE = re.compile(r"\d[\d,]*(?:\.\d+)?\s*(?:" + _NUMBER_UNIT + r")?")

# 근거 검증 대상 단위(정규화 이후 형태). ％·퍼센트는 정규화 단계에서 % 로 통일된다.
# 단위가 여러 글자인 것(만원·개월·가구 등)은 부분 문자열로 검사해 한 글자 단위(가 등)의
# 오탐(예: "5가지")을 피한다. 긴 단위부터 두어 사람이 읽기 쉽게 정렬.
_GROUNDING_UNITS: tuple[str, ...] = (
    "%", "만원", "억원", "천원", "개월", "가구", "시간",
    "원", "억", "만", "천", "년", "월", "일", "세", "명", "건", "회", "분", "평", "㎡",
)

# 구분자 날짜(YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD / MM/DD)에서 월·일 토큰을 파생.
_FULL_DATE_RE = re.compile(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})")
_SHORT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)")


def _collapse_ws(text: str) -> str:
    """공백류(스페이스·탭·개행)를 전부 제거해 substring 대조를 관대하게 만든다."""
    return re.sub(r"\s+", "", text or "")


def _normalize_number_token(token: str) -> str:
    """숫자 토큰 정규화 — 콤마·공백 제거, 전각 % 통일."""
    text = token.strip().replace(",", "").replace(" ", "")
    return text.replace("％", "%").replace("퍼센트", "%")


def _is_grounding_relevant(normalized_token: str) -> bool:
    """정규화 토큰이 근거 검증 대상(단위를 가진 수치)인지 판정.

    숫자·콤마·점을 제거하고 남은 문자에 통화/퍼센트/날짜/나이/기간 등
    ``_NUMBER_UNIT`` 계열 단위가 하나라도 있으면 True. 단위 없는 순수 숫자
    (순위·"5가지" 같은 구조 카운트·FAQ 번호)는 요구도 증거도 아니므로 False —
    fact-grounding 대상에서 제외한다.
    """
    residue = normalized_token.replace(",", "").replace(".", "").strip()
    residue = "".join(ch for ch in residue if not ch.isdigit())
    if not residue:
        return False
    return any(unit in residue for unit in _GROUNDING_UNITS)


def _number_tokens(text: str) -> List[str]:
    """텍스트에서 정규화된 숫자/날짜/%/통화 토큰 목록을 뽑는다."""
    tokens: List[str] = []
    for match in _NUMBER_TOKEN_RE.finditer(text or ""):
        normalized = _normalize_number_token(match.group(0))
        # 순수 콤마/점만 남는 경우 방지 — 숫자가 반드시 포함되게.
        if any(ch.isdigit() for ch in normalized):
            tokens.append(normalized)
    return tokens


def _derive_date_tokens(text: str) -> List[str]:
    """구분자 날짜에서 "M월"/"D일" 토큰을 파생(선행 0 제거).

    카드가 본문 "2026-05-20" 을 "5월 20일" 로 재작성해도 근거 매칭되도록,
    article 토큰 집합을 넓히는 용도. 컴플라이언스는 단위 있는 토큰 비교라 불변.
    """
    tokens: List[str] = []
    source = text or ""
    for month, day in (
        (m.group(2), m.group(3)) for m in _FULL_DATE_RE.finditer(source)
    ):
        tokens.extend(_month_day_tokens(month, day))
    for month, day in (
        (m.group(1), m.group(2)) for m in _SHORT_DATE_RE.finditer(source)
    ):
        tokens.extend(_month_day_tokens(month, day))
    return tokens


def _month_day_tokens(month: str, day: str) -> List[str]:
    """("05","20") → ["5월","20일"]. 범위 밖 값(월>12·일>31)은 버린다."""
    out: List[str] = []
    try:
        month_int = int(month)
        day_int = int(day)
    except (TypeError, ValueError):
        return out
    if 1 <= month_int <= 12:
        out.append(f"{month_int}월")
    if 1 <= day_int <= 31:
        out.append(f"{day_int}일")
    return out


def _card_number_tokens(card: InfoCard) -> List[str]:
    """카드가 렌더할 텍스트(headline/subhead/bullet label·value)의 근거 대상 토큰.

    ``_render_bullet_lines`` 가 label·value 를 **둘 다** 이미지에 baked 하므로
    label 도 검사한다(레퍼런스 카드뉴스는 금액을 label 에 넣는다). 단위 없는
    순수 숫자는 ``_is_grounding_relevant`` 로 걸러 요구 대상에서 제외한다.
    """
    parts: List[str] = [card.headline, card.subhead]
    for bullet in card.bullets:
        parts.append(bullet.label)
        parts.append(bullet.value)
    tokens: List[str] = []
    for part in parts:
        tokens.extend(_number_tokens(part))
    return [t for t in tokens if _is_grounding_relevant(t)]


# ──────────────────────────────────────────────────────────────────────────────
# Stage 1 — 구성안 기획
# ──────────────────────────────────────────────────────────────────────────────


def plan_infocards(
    *,
    article_text: str,
    title: str,
    category: str,
    card_budget: int = 5,
    model: Optional[str] = None,
    timeout: Optional[int] = None,
    runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> ImagePlan:
    """본문 전체를 Claude 가 읽고 한국 카드뉴스 세트(≤card_budget)를 기획한다.

    Args:
        article_text: 글 전체 본문(평문). 카드 사실은 이 안에 실제로 있는 값만 써야 한다.
        title: 글 제목(한글).
        category: 카테고리 키(로그·문맥용).
        card_budget: 생성할 카드 최대 장수(hero + 정보카드 + closing).
        model: Claude 모델 별칭. None 이면 sonnet(하네스 기본).
        timeout: CLI 타임아웃(초). None 이면 모듈 기본.
        runner: subprocess.run 주입(테스트용). None 이면 subprocess.run.

    Returns:
        ImagePlan. claude 부재·타임아웃·JSON 파손 등 어떤 실패도 cards=[] 로 fail-open.
        예외를 절대 전파하지 않는다(호출자가 cards 비면 옛 슬롯 경로로 폴백).
    """
    if not isinstance(article_text, str) or not article_text.strip():
        log.warning("[infocard] 본문이 비어 기획 스킵")
        return ImagePlan()

    try:
        budget = max(1, int(card_budget))
    except (TypeError, ValueError):
        budget = 5

    run = runner or subprocess.run
    cmd = _claude_cmd()
    if not cmd:
        log.warning("[infocard] claude CLI 없음 — 기획 스킵(폴백)")
        return ImagePlan()

    resolved_model = (model or _INFOCARD_PLAN_MODEL).strip() or _INFOCARD_PLAN_MODEL
    resolved_timeout = int(timeout) if timeout else _INFOCARD_PLAN_TIMEOUT_SEC

    # content_generator._call_claude 와 동일한 서브프로세스 규약:
    # 중첩 Claude 세션 방지 + API 키 제거(과금 방지).
    for var in ("CLAUDECODE", "CLAUDE_CODE"):
        os.environ.pop(var, None)
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)

    prompt = _build_plan_prompt(
        article_text=article_text[:_INFOCARD_ARTICLE_PROMPT_CAP],
        title=title,
        category=category,
        card_budget=budget,
    )
    # 프롬프트(본문 포함)는 stdin 으로 파이프한다 — Windows claude.CMD 는 cmd.exe 를
    # 거쳐 명령줄 8,191자 한계가 있어, 큰 본문을 ``-p <prompt>`` argv 로 넣으면 rc=1 로
    # 잘린다(content_generator._call_claude 와 동일한 회피). ``--json-schema`` 는 이
    # CLI 버전에서 JSON 출력을 강제하지 못해(모델이 산문·표로 답함) 프롬프트로 순수
    # JSON 을 강제하고 _extract_json_object 로 방어 파싱한다.
    args = [
        cmd, "-p",
        "--output-format", "text",
        "--model", resolved_model,
    ]
    try:
        proc = run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            timeout=resolved_timeout,
        )
    except subprocess.TimeoutExpired:
        log.warning(f"[infocard] 기획 타임아웃({resolved_timeout}s) — 폴백")
        return ImagePlan()
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning(f"[infocard] 기획 호출 실패({type(exc).__name__}) — 폴백")
        return ImagePlan()

    if getattr(proc, "returncode", 1) != 0:
        log.warning(f"[infocard] 기획 rc={getattr(proc, 'returncode', '?')} — 폴백")
        return ImagePlan()

    parsed = _extract_json_object(getattr(proc, "stdout", "") or "")
    if not parsed:
        log.warning("[infocard] 기획 응답 JSON 파싱 실패 — 폴백")
        return ImagePlan()

    raw_cards = parsed.get("cards")
    cards: List[InfoCard] = []
    if isinstance(raw_cards, list):
        for raw in raw_cards[:budget]:
            card = _card_from_dict(raw)
            if card is not None:
                cards.append(card)
    log.info(f"[infocard] 기획 완료: {len(cards)}장(예산 {budget}, 카테고리={category})")
    return ImagePlan(cards=cards, raw=parsed)


def _build_plan_prompt(*, article_text: str, title: str, category: str,
                       card_budget: int) -> str:
    """Stage1 기획 프롬프트 — 본문에 있는 사실만으로 카드 세트를 설계하게 지시."""
    return (
        "당신은 한국 정부지원·정보성 블로그의 카드뉴스 디자이너입니다. 아래 블로그 본문 "
        "전체를 읽고, 독자가 한눈에 이해할 수 있는 카드뉴스 세트를 설계하세요.\n\n"
        f"[글 제목] {title}\n"
        f"[카테고리] {category}\n"
        f"[최대 카드 수] {card_budget}장 (표지 hero 1장 + 정보 카드 여러 장 + 마무리 "
        "closing 1장 구성 권장)\n\n"
        "[절대 규칙]\n"
        "1. 카드에 넣는 금액·날짜·퍼센트·나이·기간·기관명 등 모든 사실은 **본문에 실제로 "
        "등장하는 값만** 쓰세요. 본문에 없는 수치를 절대 지어내지 마세요.\n"
        "2. 모든 라벨·헤드라인·값은 한글로 쓰세요(영문은 icon_hint 에만).\n"
        "3. 정보 카드는 자격/금액/신청방법/FAQ/체크리스트/비교 중 본문에 근거가 있는 것만 "
        "만드세요. 근거가 부족하면 카드 수를 줄이세요.\n"
        "4. 각 카드에는 layout, palette, model_pose 를 스키마의 허용값 중에서 고르세요.\n"
        "5. fact_refs 에는 그 카드가 근거로 삼은 본문 문구를 짧게 인용하세요.\n\n"
        "아래 JSON 하나만 출력하세요(스키마 준수):\n"
        "cards: [{ role(hero|info|closing), headline, subhead, "
        "bullets:[{label, value, icon_hint}], layout, palette, model_pose, fact_refs:[..] }]\n\n"
        "───── 본문 시작 ─────\n"
        f"{article_text}\n"
        "───── 본문 끝 ─────\n\n"
        "[출력 형식 — 엄수]\n"
        "오직 JSON 객체 하나만 출력하세요. 설명·요약·표·마크다운·코드펜스를 절대 "
        "붙이지 마세요. 응답의 첫 글자는 반드시 '{' 이고 마지막 글자는 '}' 여야 합니다."
    )


def validate_image_plan(
    plan: ImagePlan,
    *,
    article_text: str,
    card_budget: int,
) -> ValidationReport:
    """구성안을 검증하고 근거 있는 카드만 통과시킨다(fact-grounding).

    검사:
    - 1 ≤ 카드 수 ≤ card_budget (초과분은 잘라낸다).
    - headline 비어있지 않음(빈 카드는 flagged).
    - role/layout/palette/model_pose 는 허용 enum(모르는 값은 이미 안전 기본으로 coerce).
    - fact-grounding: headline/subhead/bullet label·value 의 **단위 있는** 숫자·날짜·
      %·통화·나이·기간 토큰이 본문(정규화)에 반드시 등장해야 통과. 근거 없는 수치
      카드는 flagged. 단위 없는 순수 숫자(순위·구조 카운트·FAQ 번호)는 검증 대상이 아님.
    - fact_refs 가 제공된 카드는, 인용 문구 중 하나라도 본문에 substring 으로 있어야 통과.

    ⚠️ 검증 범위: 이 함수는 **숫자 계열(금액·날짜·%·나이·기간) 토큰만** 대조한다.
    기관명·자격요건 같은 텍스트 주장, 그리고 "특정 숫자가 특정 항목에 속하는가" 같은
    숫자-주장 결합(semantic pairing)은 검증 범위 밖이다(운영자 오인 주의).

    Returns:
        ValidationReport(grounded_cards, flagged, ok). 예외를 전파하지 않는다.
    """
    report = ValidationReport()
    if plan is None or not plan.cards:
        return report

    try:
        budget = max(1, int(card_budget))
    except (TypeError, ValueError):
        budget = 5

    # article 토큰도 카드 토큰과 동일 기준(단위 있는 토큰)으로 필터해 apples-to-apples.
    article_tokens = {
        t for t in _number_tokens(article_text or "") if _is_grounding_relevant(t)
    }
    # 구분자 날짜에서 파생한 "M월"/"D일" 을 추가(과차단만 완화, 컴플라이언스 불변).
    article_tokens.update(
        t for t in _derive_date_tokens(article_text or "") if _is_grounding_relevant(t)
    )
    # fact_refs substring 대조는 공백만 접어 비교(과차단 방지).
    article_flat = _collapse_ws(article_text or "")

    for card in plan.cards[:budget]:
        if not card.headline.strip():
            report.flagged.append(FlaggedCard(card, "headline_empty"))
            continue
        card_tokens = _card_number_tokens(card)
        ungrounded = [t for t in card_tokens if t not in article_tokens]
        if ungrounded:
            report.flagged.append(
                FlaggedCard(card, f"ungrounded_numbers:{','.join(sorted(set(ungrounded)))}")
            )
            continue
        # fact_refs 가 제공됐는데 어느 것도 본문에 없으면 flag(제공 안 된 카드는 미검사).
        refs = [r for r in card.fact_refs if r.strip()]
        if refs and not any(_collapse_ws(r) in article_flat for r in refs):
            report.flagged.append(FlaggedCard(card, "fact_refs_absent"))
            continue
        report.grounded_cards.append(card)

    report.ok = len(report.grounded_cards) >= 1
    return report


# ──────────────────────────────────────────────────────────────────────────────
# Stage 2 — 카드별 GPT-Image 프롬프트
# ──────────────────────────────────────────────────────────────────────────────


def _resolve_brand_model_desc(brand_style: Optional[str] = None) -> str:
    """일관된 모델 묘사 해석 — brand_style > config.brand > config.settings > 기본값."""
    if isinstance(brand_style, str) and brand_style.strip():
        return brand_style.strip()
    try:
        from config import brand as _brand
        getter = getattr(_brand, "image_model_desc", None)
        if callable(getter):
            value = getter()
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception:  # noqa: BLE001 — 브랜드 로드 실패 시 다음 후보로
        pass
    try:
        from config import settings as _s
        value = getattr(_s, "IMAGE_MODEL_DESC", "")
        if isinstance(value, str) and value.strip():
            return value.strip()
    except Exception:  # noqa: BLE001 — settings 미로드면 기본값
        pass
    return _BRAND_MODEL_DESC


_INFOCARD_SIZE_RE = re.compile(r"^\d+x\d+$")


def _resolve_infocard_size() -> str:
    """카드 캔버스 크기 — settings.IMAGE_INFOCARD_SIZE, 없으면 세로 카드 기본.

    값은 "가로x세로" 형식(예: "1024x1280")일 때만 사용한다. 형식이 어긋나면
    (오타·주입) 프롬프트에 이상 문자열이 새지 않도록 기본값으로 폴백.
    """
    try:
        from config import settings as _s
        value = str(getattr(_s, "IMAGE_INFOCARD_SIZE", "") or "").strip()
        if value and _INFOCARD_SIZE_RE.match(value):
            return value
    except Exception:  # noqa: BLE001 — settings 미로드면 기본값
        pass
    return "1024x1280"


def _sanitize_render_text(text: str) -> str:
    """프롬프트에 따옴표로 감싸 넣을 카드 텍스트를 인젝션 안전하게 정규화.

    카드 텍스트는 Claude 가 (리서치 캐시로 들어온 외부 웹텍스트를 요약해) 만든 값이라
    간접 프롬프트 인젝션 표면이다. 임베드된 ``"``·개행이 섞이면 따옴표 밖으로 탈출해
    뒤 지시문처럼 해석될 수 있으므로 다음을 수행한다:

    - 개행·캐리지리턴·탭 등 제어문자 제거(공백으로 대체)
    - 백틱 제거
    - 임베드된 큰따옴표(``"``·``“``·``”``)를 작은따옴표로 치환(따옴표 탈출 차단)
    - 연속 공백 정리

    한글·숫자·일반 문장부호는 보존한다. 실제 baked 텍스트가 따옴표만 살짝 정규화됨은
    허용한다(의미는 유지).
    """
    if not text:
        return ""
    cleaned_chars: List[str] = []
    for char in text:
        code = ord(char)
        if code < 0x20 or code == 0x7F:  # 제어문자(개행·CR·탭 포함) → 공백
            cleaned_chars.append(" ")
        else:
            cleaned_chars.append(char)
    cleaned = "".join(cleaned_chars)
    cleaned = cleaned.replace("`", "")
    for quote in ('"', "“", "”"):  # " " " → '
        cleaned = cleaned.replace(quote, "'")
    return re.sub(r"\s+", " ", cleaned).strip()


def _render_bullet_lines(bullets: List[InfoCardBullet]) -> List[str]:
    """카드 행을 '정확히 렌더할 한글' 라인으로 변환(값·아이콘 힌트 포함).

    label·value·icon_hint 는 프롬프트에 넣기 직전 ``_sanitize_render_text`` 로 통과시켜
    따옴표 탈출·개행 인젝션을 차단한다.
    """
    lines: List[str] = []
    for index, bullet in enumerate(bullets, start=1):
        label = _sanitize_render_text(bullet.label)
        value = _sanitize_render_text(bullet.value)
        icon_hint = _sanitize_render_text(bullet.icon_hint)
        value_part = f'  ->  value "{value}"' if value else ""
        icon_part = f"  (icon: {icon_hint})" if icon_hint else ""
        lines.append(f'Row {index}: label "{label}"{value_part}{icon_part}')
    return lines


def build_infocard_prompt(
    card: InfoCard,
    *,
    title: str,
    category: str,
    model_ref_name: Optional[str] = None,
    brand_style: Optional[str] = None,
) -> str:
    """카드 1장을 GPT-Image 용 리치 영문 프롬프트로 — 주어진 한글을 이미지에 정확히 baked.

    Args:
        card: Stage1/검증을 통과한 InfoCard.
        title: 글 제목(문맥용).
        category: 카테고리(문맥용).
        model_ref_name: cwd 에 있는 참조 모델시트 파일명. 주어지면 Read 로 일관성 유지 지시.
        brand_style: 모델 묘사 오버라이드(테스트/커스텀). None 이면 자동 해석.

    Returns:
        완성된 영문 GPT-Image directive. 예외 없이 항상 문자열 반환.
    """
    layout_directive = _INFOCARD_LAYOUTS.get(card.layout, _INFOCARD_LAYOUTS[_INFOCARD_LAYOUT_DEFAULT])
    palette_directive = _INFOCARD_PALETTES.get(card.palette, _INFOCARD_PALETTES[_INFOCARD_PALETTE_DEFAULT])
    pose_directive = _MODEL_POSES.get(card.model_pose, _MODEL_POSES[_MODEL_POSE_DEFAULT])
    model_desc = _resolve_brand_model_desc(brand_style)
    canvas = _resolve_infocard_size()

    # 정확히 렌더할 한글 텍스트 블록(철자 그대로). 인젝션 안전화(따옴표·개행 정규화).
    headline = _sanitize_render_text(card.headline)
    subhead = _sanitize_render_text(card.subhead)
    text_lines: List[str] = [f'Headline (render exactly): "{headline}"']
    if subhead:
        text_lines.append(f'Subhead (render exactly): "{subhead}"')
    text_lines.extend(_render_bullet_lines(card.bullets))

    parts: List[str] = [
        "Design a single professional Korean card-news infographic (a vertical poster-style "
        "card), studio-quality, clean and highly legible.",
        f"Canvas: portrait card, target size {canvas} pixels, 4:5 vertical proportions.",
        f"Layout: {layout_directive}",
        f"Color palette: {palette_directive}",
        (
            "Consistent presenter model: " + model_desc + ". In this card the model is "
            + pose_directive + "."
        ),
        (
            "CRITICAL — bake the following Korean text into the image, spelling every "
            "character EXACTLY as given. Do NOT translate it, do NOT mangle or invent "
            "Hangul, and do NOT add any other text, numbers, amounts, dates or statistics "
            "that are not listed here:"
        ),
        "\n".join(text_lines),
        (
            "Korean card-news aesthetic: crisp bold Korean typography with strong contrast, "
            "rounded panels and chips, soft drop shadows, clear visual hierarchy, generous "
            "padding, a small subtle source/credit line at the bottom."
        ),
        (
            f"Topic context (for tone only, do not render as text): title '{title}', "
            f"category '{category}'."
        ),
        (
            "Negative constraints: no misspelled or broken Hangul, no gibberish characters, "
            "no invented numbers/amounts/dates beyond the provided text, no lorem ipsum, no "
            "watermark, no logo, no English filler text, no duplicated or overlapping text."
        ),
    ]

    if model_ref_name:
        parts.insert(
            3,
            (
                f"A reference model sheet image named '{model_ref_name}' is in the current "
                "folder — Read it and keep the exact same person, face, hairstyle and outfit "
                "for series consistency."
            ),
        )

    return "\n".join(parts) + "\n"
