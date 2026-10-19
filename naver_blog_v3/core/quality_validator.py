# core/quality_validator.py
# ① 글 품질 검증 + 자동 재생성
# 기준 미달 글은 Claude에게 재생성을 요청합니다
from __future__ import annotations

import os
import re
import sys
from datetime import date as _date
from typing import List, Optional, Tuple
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
from config import settings as _s
log = get_logger(__name__)

# ─────────────────────────────────────────────────────
# 품질 기준 (settings.py 상수를 참조, 폴백 기본값 제공)
# ─────────────────────────────────────────────────────
_STOP_WORDS = frozenset({
    "총정리", "핵심정리", "완벽정리", "신청방법", "신청자격",
    "지원금액", "주의사항", "확인하세요", "알아보기", "정리",
    "최대", "최소", "방법", "조건", "기준", "대상", "해당",
    "이번", "지금", "오늘", "어떻게", "얼마", "각각", "최근", "그리고",
    "만원", "천원", "억원", "원", "연", "월", "일", "만", "천",
    "예시", "사례", "팁", "내용", "결과", "목록", "항목",
})

# 한국어 단어 끝의 흔한 조사·어미 (긴 것부터 매칭)
_TRAIL_PATTERNS = sorted([
    "이라면", "다면서", "라면서", "이라도", "라도", "이면", "다면", "라면",
    "에서", "까지", "부터", "보다", "처럼", "으로", "이라는", "라는",
    "을까", "할까", "될까", "일까", "까요", "어요", "아요", "네요", "지요",
    "이가", "이는", "이를", "이의",
    "이", "가", "을", "를", "은", "는", "에", "의", "와", "과", "도", "만", "면", "로",
], key=len, reverse=True)


def _strip_josa(word: str) -> str:
    """한국어 단어 끝의 조사·어미를 제거한다 (단순 휴리스틱).

    예: '변화가'→'변화', '소득이면'→'소득', '감액될까'→'감액'
    조사 제거 후 결과가 2자 미만이면 원본 유지.
    """
    for trail in _TRAIL_PATTERNS:
        if word.endswith(trail) and len(word) - len(trail) >= 2:
            return word[:-len(trail)]
    return word

QUALITY_RULES = {
    "min_chars":      getattr(_s, 'QV_MIN_CHARS', 1800),
    # 폴백은 settings.py 에서 키가 사라졌을 때만 쓰인다 — 옛 값(3000)을 남겨두면
    # 그 순간 상한이 조용히 되돌아간다(gui/config_io 폴백과 같은 숫자로 유지할 것).
    "max_chars":      getattr(_s, 'QV_MAX_CHARS', 3200),
    "require_table":  getattr(_s, 'QV_REQUIRE_TABLE', True),
    "require_faq":    getattr(_s, 'QV_REQUIRE_FAQ', True),
    "require_h2":     getattr(_s, 'QV_MIN_H2_COUNT', 3),
    "min_tags":       getattr(_s, 'QV_MIN_TAGS', 10),
    "max_retries":    getattr(_s, 'QV_MAX_RETRIES', 2),
}

# ─────────────────────────────────────────────────────
# 카테고리별 품질 룰 머지
# 디지털 카테고리만 settings.QV_CATEGORY_OVERRIDES["디지털"]의 키로 완화되며,
# 그 외 카테고리는 기본 QUALITY_RULES가 그대로 반환된다.
# ─────────────────────────────────────────────────────
_CATEGORY_OVERRIDES: dict = getattr(_s, "QV_CATEGORY_OVERRIDES", {}) or {}


def _get_rules_for_category(category: Optional[str]) -> dict:
    """카테고리별 품질 룰을 반환합니다 (얕은 머지).

    settings.QV_CATEGORY_OVERRIDES에 카테고리가 등록돼 있으면
    해당 키들만 기본 QUALITY_RULES 위에 덮어씌워 새 dict를 반환합니다.
    오버라이드가 없거나 카테고리가 None/빈 문자열이면 기본 QUALITY_RULES의 사본을 반환합니다.

    Args:
        category: 카테고리 문자열(예: "디지털", "복지"). None/빈 문자열 허용.

    Returns:
        해당 카테고리에 적용할 룰 dict. 호출자가 안전하게 수정해도
        모듈 전역 QUALITY_RULES에는 영향이 없도록 얕은 사본을 반환합니다.
    """
    rules = dict(QUALITY_RULES)
    if not category:
        return rules
    override = _CATEGORY_OVERRIDES.get(category)
    if isinstance(override, dict) and override:
        # 알려진 키만 덮어쓰기 — 오타가 silent 무시되도록(예: "min_char")
        for key in (
            "min_chars", "max_chars",
            "require_table", "require_faq",
            "require_h2", "min_tags", "max_retries",
        ):
            if key in override:
                rules[key] = override[key]
    return rules


def _has_category_override(category: Optional[str]) -> bool:
    """해당 카테고리에 오버라이드가 등록돼 있는지 여부."""
    if not category:
        return False
    override = _CATEGORY_OVERRIDES.get(category)
    return isinstance(override, dict) and bool(override)


_FAQ_HEADING_RE = re.compile(r'(FAQ|자주\s*묻)', re.IGNORECASE)


def _is_faq_heading(heading_text: str) -> bool:
    return bool(_FAQ_HEADING_RE.search(heading_text or ""))


def _load_forbidden_words(category: Optional[str]) -> List[str]:
    """카테고리별 금지어 목록을 로드한다(글로벌 금지어 포함, 합집합).

    content_generator._load_prompt_strategy 를 재사용해 단일 로딩 경로를 유지한다
    (그쪽이 templates/prompt_strategies.json 의 forbidden_words 와
    forbidden_words_global 을 이미 합쳐서 반환).

    prompt_strategies.json 로드 실패·import 실패는 조용히 빈 목록을 반환한다
    (금지어 검사 없음). 절대 예외를 전파하지 않는다.
    """
    try:
        from core.content_generator import _load_prompt_strategy
        strategy = _load_prompt_strategy(category or "")
    except (ImportError, AttributeError, OSError, ValueError, TypeError) as exc:
        log.debug(f"금지어 목록 로드 스킵(전략 로드 실패): {exc}")
        return []
    words = strategy.get("forbidden_words") if isinstance(strategy, dict) else None
    if not isinstance(words, list):
        return []
    return [w for w in words if isinstance(w, str) and w.strip()]


def _hard_trim(content: str, max_chars: int) -> str:
    trimmed = content[:max_chars]
    last_newline = trimmed.rfind('\n')
    if last_newline > max_chars * 0.8:
        trimmed = trimmed[:last_newline]
    return trimmed


def _trim_content(content: str, max_chars: int) -> str:
    """글자 수 초과 시 ## 섹션 단위로 잘라 max_chars 이하로 맞춘다.

    FAQ 섹션(헤딩에 "FAQ" 또는 "자주 묻" 포함)은 보호 대상이며,
    뒤에서부터 보호 대상이 아닌 섹션을 우선 제거한다.
    """
    if len(content) <= max_chars:
        return content

    matches = list(re.finditer(r'^## (.+)$', content, re.MULTILINE))
    if len(matches) <= 3:
        return _hard_trim(content, max_chars)

    sections = []
    for idx, m in enumerate(matches):
        start = m.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(content)
        sections.append({
            "start": start,
            "end": end,
            "text": m.group(1).strip(),
            "is_faq": _is_faq_heading(m.group(1)),
            "removed": False,
        })

    # 보호 대상: FAQ 섹션 + 마지막 섹션 (Claude가 FAQ를 헤딩 없이 본문에 섞어
    # 작성한 경우 _is_faq_heading이 못 잡으므로 마지막 섹션도 함께 보호).
    last_idx = len(sections) - 1
    removable_order = [
        i for i, sec in enumerate(sections)
        if i >= 2 and i < last_idx and not sec["is_faq"]
    ]

    def _rebuild(secs):
        if not secs:
            return content
        kept_intro = content[:secs[0]["start"]]
        kept_body = "".join(
            content[sec["start"]:sec["end"]] for sec in secs if not sec["removed"]
        )
        return (kept_intro + kept_body).rstrip() + "\n"

    for i in reversed(removable_order):
        sections[i]["removed"] = True
        candidate = _rebuild(sections)
        if len(candidate) <= max_chars:
            return candidate

    candidate = _rebuild(sections)
    if len(candidate) <= max_chars:
        return candidate

    return _hard_trim(candidate, max_chars)


# ─────────────────────────────────────────────────────
# 키워드 신호 — 정확 매칭 + 파생어 합산 (2026-08-02 P1-⑤)
# ─────────────────────────────────────────────────────
# QRA(네이버 검색 추천 알고리즘)는 "단어 반복보다 맥락"을 본다. 종전 게이트는
# 핵심어 **정확 반복 3회**를 강제해 오히려 기계적 반복(=AI 티)을 유도했다.
# 새 규격:
#   · 정확 매칭 하한 1회 (+ 첫 300자 등장)은 **유지** — 검색 노출의 최후 방어선
#   · 나머지는 파생어·문맥어 등장을 합산해 '맥락 신호'로 인정
#   · 파생어를 못 구하면(캐시 미스·오프라인) 정확 1회로 통과 = fail-open
# 어떤 경우에도 종전(정확 3회)보다 엄격해지지 않는다.
_MIN_EXACT_KEYWORD = 1     # 정확 매칭 하한 — 이 아래는 무조건 실패(노출 방어선)
_MIN_CONTEXT_SIGNAL = 3    # 정확 + 파생어 합산 하한(파생어를 구했을 때만 적용)


def _despace_text(text: str) -> str:
    """공백 제거 — 본문이 '영어 공부'로 띄어 써도 핵심어 '영어공부'로 인정."""
    return re.sub(r"\s+", "", text or "")


def extract_title_keywords(title: str,
                           category: Optional[str] = None) -> Tuple[Optional[str], List[str]]:
    """제목에서 핵심 키워드(primary)와 보조 키워드 목록을 뽑는다.

    게이트(_keyword_failures)와 생성 프롬프트(content_generator)가 **같은 핵심어**를
    보게 하려고 공개 함수로 분리했다. 규칙은 종전 validate 내부 로직 그대로다.

    Args:
        title: 글 제목(날짜 접두사 포함 가능).
        category: 감지된 카테고리. 주면 CATEGORY_KEYWORDS 와 겹치는 제목어를
            핵심어로 우선한다(수식어로 시작하는 제목 보정).

    Returns:
        (primary_keyword | None, keywords[:3]).
    """
    clean_title = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', title or "")
    clean_title = re.sub(r'^\d{4}\s*', '', clean_title)  # 연도 제거
    title_words = re.findall(r'[가-힣]{2,}', clean_title)
    seen = set()
    keywords: List[str] = []
    for word in title_words:
        stripped = _strip_josa(word)
        if stripped and stripped not in seen and stripped not in _STOP_WORDS and len(stripped) >= 2:
            seen.add(stripped)
            keywords.append(stripped)
    keywords = keywords[:3]
    if not keywords:
        return None, []

    primary_kw = keywords[0]  # 기본: 제목 첫 단어
    # 제목이 수식어로 시작하면(예: "초등 영어캠프…") 첫 단어가 진짜 주제어가 아니다.
    # 감지된 카테고리의 키워드에 해당하는 제목어를 핵심어로 우선(브랜드 인식 매핑).
    try:
        from core.content_generator import CATEGORY_KEYWORDS as _CK
        _cat_kws = _CK.get(category or "", [])
        for _kw in keywords:
            if any(_kw in ck or ck in _kw for ck in _cat_kws):
                primary_kw = _kw
                break
    except Exception:  # noqa: BLE001
        pass
    return primary_kw, keywords


def keyword_variants(keyword: str) -> List[str]:
    """핵심어의 파생어·문맥어 목록(네트워크 호출 없음).

    keyword_expander.local_variants 를 재사용한다 — 파생어 규칙의 단일 소스.
    임포트·생성 실패는 빈 리스트로 흡수한다(호출자가 fail-open 처리).

    Args:
        keyword: 핵심 키워드.

    Returns:
        파생어 리스트(구하지 못하면 빈 리스트).
    """
    if not keyword:
        return []
    try:
        from core.keyword_expander import local_variants
        return [v for v in local_variants(keyword) if v]
    except Exception as exc:  # noqa: BLE001 — 파생어 부재가 검증을 막으면 안 된다
        log.debug(f"파생어 조회 실패(정확 매칭만으로 판정): {exc}")
        return []


def _count_variant_hits(residual: str, variants: List[str]) -> int:
    """파생어 등장 횟수 합산. 긴 파생어부터 세고 매칭 구간을 소거해 중복 계수를 막는다.

    Args:
        residual: 공백 제거 + **정확 키워드를 이미 제거한** 본문.
                  (정확 매칭 안에 들어있는 조각을 파생어로 또 세지 않기 위함)
        variants: 파생어 목록.

    Returns:
        파생어 등장 총 횟수.
    """
    if not residual or not variants:
        return 0
    hits = 0
    for variant in sorted({v for v in variants if v}, key=len, reverse=True):
        variant_ds = _despace_text(variant)
        if len(variant_ds) < 2:
            continue
        found = residual.count(variant_ds)
        if found:
            hits += found
            residual = residual.replace(variant_ds, "\x00")  # 겹침 재계수 방지
    return hits


def _keyword_failures(post_data: dict, content: str) -> List[str]:
    """키워드 신호 게이트 — 정확 매칭 하한 + 파생어 합산 맥락 신호.

    Args:
        post_data: 최소 "title"(+선택 "category")을 담은 글 데이터.
        content:   트리밍 후 본문(마크다운).

    Returns:
        실패 사유 리스트(통과면 빈 리스트).
    """
    failures: List[str] = []
    primary_kw, keywords = extract_title_keywords(
        post_data.get("title", ""), post_data.get("category"))
    if not keywords or not primary_kw:
        return failures

    content_ds = _despace_text(content)
    primary_ds = _despace_text(primary_kw)
    exact_count = content_ds.count(primary_ds) if primary_ds else 0

    if exact_count < _MIN_EXACT_KEYWORD:
        failures.append(
            f"keyword_density: 핵심 키워드 '{primary_kw}'가 본문에 {exact_count}회만 등장 "
            f"(최소 {_MIN_EXACT_KEYWORD}회 필요)"
        )
    else:
        # 정확 매칭 구간을 뺀 나머지에서만 파생어를 센다(같은 자리 이중 계수 방지).
        variants = keyword_variants(primary_kw)
        if variants:
            residual = content_ds.replace(primary_ds, "\x00")
            variant_hits = _count_variant_hits(residual, variants)
            context_signal = exact_count + variant_hits
            if context_signal < _MIN_CONTEXT_SIGNAL:
                failures.append(
                    f"keyword_density: 핵심 키워드 '{primary_kw}' 맥락 신호 부족 — "
                    f"정확 {exact_count}회 + 파생어 {variant_hits}회 = {context_signal} "
                    f"(최소 {_MIN_CONTEXT_SIGNAL} 필요). 같은 단어를 반복하지 말고 "
                    f"파생어·유의어로 변주해 주제를 더 다루세요."
                )
        # variants 가 비면(캐시 미스·오프라인) 정확 1회로 통과 — fail-open.

    # 노출 방어선: 핵심어는 도입부(첫 300자)에 정확한 형태로 한 번은 나와야 한다.
    if primary_ds not in _despace_text(content[:300]):
        failures.append(
            f"keyword_early: 핵심 키워드 '{primary_kw}'가 본문 첫 300자 이내에 없음"
        )

    if len(keywords) >= 2:
        longtail_1 = keywords[1]
        if _despace_text(longtail_1) not in content_ds:
            failures.append(
                f"longtail_missing: 롱테일 키워드 '{longtail_1}'가 본문에 없음 (최소 1회 필요)"
            )

    if len(keywords) >= 3:
        longtail_2 = keywords[2]
        if _despace_text(longtail_2) not in content_ds:
            log.warning(
                f"longtail_warning: 보조 키워드 '{longtail_2}'가 본문에 없음 (권장 1회 이상)"
            )
    return failures


# ─────────────────────────────────────────────────────
# 품질 검사
# ─────────────────────────────────────────────────────
def _apply_policy_brief_rules(rules: dict, post_data: dict) -> dict:
    """'정책 소개형' 글의 **분량 계약만** 좁게 조정한다(2026-09-21).

    소개형은 확정 금액·소득구간·수치표를 빼므로 기본 하한(1,800자)을 채우려면
    없는 수치를 지어내거나 같은 말을 늘려야 한다 — 게이트가 날조를 유도하는 꼴이다.
    그래서 하한/상한을 소개형 목표(1,200~1,800자)에 맞춘다.

    ★ 움직이는 것은 min_chars·max_chars 둘뿐이다. 표·FAQ·소제목·태그·금지어·
      사실 게이트·중복 판정은 **그대로** — 분량만 문제였지 나머지는 아니다.
    ★ `post_data["_policy_brief"]` 가 참일 때만 적용한다. 플래그가 없는 기존 글은
      바이트 동일하게 예전 계약으로 검사된다(레거시 재고 무영향).
    """
    if not post_data.get("_policy_brief"):
        return rules
    try:
        from core.content_generator import (POLICY_BRIEF_MIN_CHARS,
                                            POLICY_BRIEF_MAX_CHARS)
    except Exception as exc:  # noqa: BLE001 — 못 읽으면 기존 계약 유지(fail-closed)
        log.debug(f"[policy_brief] 분량 계약 로드 실패, 기본 룰 유지({type(exc).__name__})")
        return rules
    out = dict(rules)
    out["min_chars"] = POLICY_BRIEF_MIN_CHARS
    # 상한은 원래 상한보다 낮아질 때만 내린다(운영자가 더 낮게 잡았으면 그쪽 존중).
    _cur_max = out.get("max_chars")
    if not isinstance(_cur_max, (int, float)) or _cur_max > POLICY_BRIEF_MAX_CHARS:
        out["max_chars"] = POLICY_BRIEF_MAX_CHARS
    log.info("정책 소개형 분량 계약 적용: %s~%s자",
             out["min_chars"], out["max_chars"])
    return out


def _apply_blueprint_rules(rules: dict, post_data: dict) -> dict:
    """블루프린트 골격이 있으면 FAQ/표/H2 강제 게이트를 그 골격 기대치로 완화한다.

    설계도 주도 변주(2026-08-25)는 글마다 골격이 달라진다 — 어떤 골격은 FAQ 0개,
    표 없음, 소제목 1~2개다. 전역 게이트(require_faq/require_table/require_h2)를 그대로
    두면 그런 골격의 글이 QA 에서 전멸한다(2026-08-02 감사에서 QV 강제가 새 골격을
    지웠던 사고 클래스). post_data["_blueprint"] 가 있을 때만 완화하고, 없으면 rules
    를 그대로 반환한다(바이트 동일·회귀 0).

    ⚠️ 완화는 '요구를 낮추는' 방향으로만 한다(더 엄격하게 만들지 않음).
    """
    return _apply_editorial_rules(_apply_blueprint_skeleton(rules, post_data), post_data)


def _apply_editorial_rules(rules: dict, post_data: dict) -> dict:
    """편집 브리프가 '이 주제엔 표/FAQ 가 필요 없다'고 **선언한 글만** 그 요구를 해제한다.

    집필 프롬프트(2026-09-18)는 주제에 맞는 구성을 모델이 고르게 하고 그 선택을
    `editorial.structure` 로 받는다. 프롬프트만 바꾸고 이 게이트를 그대로 두면 표·FAQ
    없이 쓴 글이 QA 에서 전멸하고 재생성만 태운다(2026-08-02 감사에서 확인된 사고
    클래스 — 프롬프트와 검사 경로는 **함께** 움직여야 한다).

    브리프가 없거나 선언이 없으면 rules 를 그대로 반환한다(바이트 동일·회귀 0).
    ⚠️ 요구를 **낮추는 방향으로만** 적용한다.

    ⚠️ 완화에는 두 개의 자물쇠가 있다(선언만으로 게이트를 무력화하지 못하게):
      ① **선언만 있는 브리프는 무시한다** — 독자 질문·답 요약이 둘 다 비어 있으면 그건
         편집 판단이 아니라 '면제 요청'일 뿐이다.
      ② **운영자가 카테고리에 명시한 요구(`QV_CATEGORY_OVERRIDES[...] = True`)는 못 끈다**
         — 사람이 켠 설정을 모델 선언이 뒤집지 못한다(image_plan 장수 하한과 같은 원칙).
    완화 대상은 `require_table`·`require_faq` 둘뿐이며, 글자수·소제목 수·금지어·사실
    게이트·중복 검사는 **어떤 선언으로도 달라지지 않는다.**
    """
    brief = post_data.get("_editorial_brief") or post_data.get("editorial_brief")
    if not isinstance(brief, dict):
        return rules
    try:
        from core.editorial_brief import structure_rules
        relax = structure_rules(brief)
    except Exception as exc:  # noqa: BLE001 — 브리프 해석 실패는 기존 게이트 유지
        log.debug(f"[quality] 편집 브리프 구조 해석 실패 — 기존 게이트 유지: {exc}")
        return rules
    if not relax:
        return rules
    if not _brief_has_substance(brief):
        log.info("[quality] 구조 선언만 있는 브리프 — 완화하지 않고 기존 게이트 유지")
        return rules
    locked = _operator_required_keys(post_data.get("category") or "")
    adjusted = dict(rules)
    for key in ("require_table", "require_faq"):
        if relax.get(key) is not False or not adjusted.get(key):
            continue
        if key in locked:
            log.info("[quality] %s 는 카테고리 설정이 요구한다 — 브리프 선언으로 끄지 않는다", key)
            continue
        adjusted[key] = False
        log.info("[quality] 편집 브리프 선언에 따라 %s 요구 해제", key)
    return adjusted


def _brief_has_substance(brief: dict) -> bool:
    """이 브리프가 실제 편집 판단을 담고 있는가(독자 질문 또는 답 요약)."""
    for key in ("reader_question", "answer_summary"):
        if str(brief.get(key) or "").strip():
            return True
    return False


def _operator_required_keys(category: str) -> set:
    """운영자가 이 카테고리에 **명시적으로 True 로 켜 둔** 구조 요구 키."""
    override = _CATEGORY_OVERRIDES.get(category)
    if not isinstance(override, dict):
        return set()
    return {key for key in ("require_table", "require_faq") if override.get(key) is True}


def _apply_blueprint_skeleton(rules: dict, post_data: dict) -> dict:
    """블루프린트 골격 기반 완화(기존 로직 — 이름만 분리했다)."""
    bp = post_data.get("_blueprint")
    if not isinstance(bp, dict):
        return rules
    sk = bp.get("skeleton")
    if not isinstance(sk, dict):
        return rules
    adjusted = dict(rules)
    # FAQ: 골격이 0개면 요구 해제. 1개 이상이면 기존 '존재 여부' 요구 유지.
    if int(sk.get("faq_count", 0) or 0) <= 0:
        adjusted["require_faq"] = False
    # 표: 골격이 표를 허용하지 않으면 필수 해제.
    if not sk.get("table_allowed", False):
        adjusted["require_table"] = False
    # H2: 골격의 소제목 수가 더 낮으면 그 값으로 하향(올리지는 않는다).
    try:
        sk_h2 = int(sk.get("h2_count", 0) or 0)
        if sk_h2 > 0:
            adjusted["require_h2"] = min(int(rules.get("require_h2", sk_h2)), sk_h2)
    except (TypeError, ValueError):
        pass
    return adjusted


def validate(post_data: dict) -> Tuple[bool, List[str]]:
    """
    Returns (passed: bool, failures: list[str])

    post_data["category"]에 따라 카테고리별 룰(_get_rules_for_category)을 적용한다.
    카테고리 키가 없거나 오버라이드 미등록이면 기본 QUALITY_RULES와 동일하게 동작.

    post_data["_blueprint"] 가 있으면(설계도 주도 변주 ON) FAQ/표/H2 강제 게이트를
    그 골격 기대치로 완화한다(_apply_blueprint_rules). 없으면 기존 게이트 그대로.
    """
    content  = post_data.get("content", "")
    try:
        from core.article_quality import evaluate as _evaluate_topic_quality
        post_data["topic_quality_review"] = _evaluate_topic_quality(
            post_data.get("title", ""), content, post_data.get("category", ""))
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        log.debug("주제별 독자 검수 신호 기록 생략(%s)", type(exc).__name__)
    tags     = post_data.get("tags", [])
    category = post_data.get("category") or ""
    rules    = _apply_blueprint_rules(_get_rules_for_category(category), post_data)
    rules    = _apply_policy_brief_rules(rules, post_data)
    failures = []

    # 1. 글자 수 — 하한은 2%(최소 20자) 근소 미달 유예. 1,794자(하한 1,800) 같은
    #    몇 자 차이로 3연속 재생성 끝에 발행이 통째로 차단되는 낭비를 막는다
    #    (2026-07-19 exampleprofile 실측). 유예 밖(진짜 부족)은 종전대로 실패.
    char_count = len(content)
    _did_trim = False
    _min_chars = rules["min_chars"]
    _grace = max(20, int(_min_chars * 0.02))
    if char_count < _min_chars - _grace:
        failures.append(
            f"글자 수 부족: {char_count}자 (최소 {_min_chars}자 필요)"
        )
    elif char_count < _min_chars:
        log.warning(
            f"글자 수 근소 미달 허용: {char_count}자 (하한 {_min_chars}자, 유예 {_grace}자)"
        )
    elif char_count > rules["max_chars"]:
        log.warning(f"글자 수 초과: {char_count}자 → {rules['max_chars']}자로 절삭")
        post_data["content"] = _trim_content(content, rules["max_chars"])
        content = post_data["content"]  # 이후 검증에서 트리밍된 내용 사용
        _did_trim = True
        # 절삭으로 최소 글자 하한이 깨졌으면 승격(조용한 통과 방지).
        # 재절삭·재귀는 하지 않는다 — 트리밍된 결과 길이만 1회 재확인(동일 유예).
        trimmed_len = len(content)
        if trimmed_len < _min_chars - _grace:
            log.warning(
                f"절삭 후 글자 수 미달: {trimmed_len}자 (최소 {rules['min_chars']}자)"
            )
            failures.append(
                f"절삭 후 글자 수 부족: {trimmed_len}자 (최소 {rules['min_chars']}자 필요)"
            )

    # 2. 표 포함 여부 — 트리밍된 content 기준(절삭으로 표가 사라지면 여기서 승격됨)
    if rules["require_table"]:
        has_table = bool(re.search(r'\|.+\|', content))
        if not has_table:
            _msg = "마크다운 표가 없음 (최소 1개 필수)"
            if _did_trim:
                _msg = "절삭으로 마크다운 표가 사라짐 (최소 1개 필수)"
                log.warning("절삭 후 필수 표가 사라짐 — 재생성 필요")
            failures.append(_msg)

    # 3. FAQ 포함 여부 — 트리밍된 content 기준
    if rules["require_faq"]:
        has_faq = bool(re.search(r'(FAQ|자주.*묻|Q\.|Q:)', content, re.IGNORECASE))
        if not has_faq:
            _msg = "FAQ 섹션이 없음"
            if _did_trim:
                _msg = "절삭으로 FAQ 섹션이 사라짐"
                log.warning("절삭 후 필수 FAQ 섹션이 사라짐 — 재생성 필요")
            failures.append(_msg)

    # 4. ## 헤딩 개수 — 트리밍된 content 기준
    h2_count = len(re.findall(r'^## ', content, re.MULTILINE))
    if h2_count < rules["require_h2"]:
        _msg = f"소제목(##) 개수 부족: {h2_count}개 (최소 {rules['require_h2']}개)"
        if _did_trim:
            _msg = (f"절삭 후 소제목(##) 개수 부족: {h2_count}개 "
                    f"(최소 {rules['require_h2']}개)")
            log.warning("절삭 후 소제목(##) 개수가 최소 미달 — 재생성 필요")
        failures.append(_msg)

    # 5. 태그 개수
    if len(tags) < rules["min_tags"]:
        failures.append(
            f"태그 부족: {len(tags)}개 (최소 {rules['min_tags']}개)"
        )

    # 6. 현재 연도(2026) 포함 여부 — C-02 출처 신선도 경고
    current_year = str(_date.today().year)
    if current_year not in content:
        log.warning(f"본문에 현재 연도({current_year})가 포함되지 않음 — 정보 신선도 확인 필요")

    # 7. 키워드 밀도 검증 — R-01 (정확 매칭 + 파생어 합산, 2026-08-02 P1-⑤)
    failures.extend(_keyword_failures(post_data, content))

    # 8. 금지어 사후검사 — 카테고리별 forbidden_words(+글로벌)를 대소문자 무시 부분일치로 검사
    forbidden_words = _load_forbidden_words(category)
    if forbidden_words:
        content_lower = content.lower()
        found_forbidden = [
            w for w in forbidden_words if w.lower() in content_lower
        ]
        if found_forbidden:
            joined = ", ".join(found_forbidden[:10])
            log.warning(f"금지어 발견({len(found_forbidden)}종): {joined}")
            failures.append(f"forbidden_words: 금지 표현 사용 — {joined}")

    # 9. 사실 정확성 게이트(fact_gate) — 허구 수치·수치 모순·무근거 YMYL 차단.
    #    허구/모순은 카테고리 무관, 무근거 발행 차단은 YMYL + 수치 클레임 조건에서만.
    #    web_search_done / sources 는 content_generator 가 post_data 에 실어 보낸다.
    #    fact_gate 로드/평가 실패는 조용히 스킵(기존 검증을 절대 깨지 않음).
    try:
        from core.fact_gate import check as _fact_check
        fg_passed, fg_failures = _fact_check(
            content,
            category or None,
            web_search_done=bool(post_data.get("web_search_done")),
            sources=post_data.get("sources"),
        )
        if not fg_passed:
            failures.extend(fg_failures)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        log.debug(f"fact_gate 검사 스킵(로드/평가 실패): {exc}")

    # 정책 소개형은 확정 수치가 없어 fact_gate의 YMYL 수치 분기를 통과할 수 있다.
    # 생성 단계에서 확인한 실제 검색 완료 플래그와 공식 상세 URL을 별도로 요구한다.
    if post_data.get("_policy_brief") is True:
        try:
            from core.content_generator import _official_detail_path
            official_detail_sources = [
                source for source in (post_data.get("sources") or [])
                if isinstance(source, str) and source.strip()
                and _official_detail_path(source)
            ]
            if not (post_data.get("web_search_done") is True and official_detail_sources):
                failures.append(
                    "source_evidence_missing: 정책 소개형은 실제 웹 검색과 "
                    "gov.kr/go.kr 공식 상세 페이지 근거가 필요합니다"
                )
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            log.warning("정책 근거 검증기를 불러오지 못해 근거 누락으로 처리합니다(%s)",
                        type(exc).__name__)
            failures.append("source_evidence_missing: 정책 공식 상세 근거를 검증할 수 없습니다")

    passed = len(failures) == 0
    return passed, failures


# ─────────────────────────────────────────────────────
# D.I.A.+ 사전 점수 통합 (2026-05-25)
# ─────────────────────────────────────────────────────
def validate_with_dia(post_data: dict) -> dict:
    """기본 validate() + D.I.A.+ 가산 신호 점수를 합쳐 통합 결과 반환.

    Args:
        post_data: content / content_html / title / tags / category 를 포함하는 dict.

    Returns:
        {
            "passed": bool,              # 기본 룰만 PASS 여부
            "failures": [str, ...],      # 기본 룰 실패 사유
            "dia": {                     # D.I.A.+ 결과
                "score": float,
                "passed": bool,          # PASS_THRESHOLD(70) 이상
                "weaknesses": [...],
                "grades": {...},
                "counts": {...},
            },
            "combined_passed": bool,     # 기본 PASS AND dia.passed
        }
    """
    base_passed, failures = validate(post_data)
    html = _resolve_html_for_scoring(post_data)

    try:
        from core.dia_scorer import score as dia_score
        dia = dia_score(html)
        dia_dict = dia.to_dict()
    except (ImportError, AttributeError) as exc:
        log.warning(f"[quality_validator] dia_scorer 호출 실패: {exc}")
        dia_dict = {
            "score": 0.0, "passed": False,
            "weaknesses": [], "grades": {}, "counts": {},
        }

    return {
        "passed": base_passed,
        "failures": list(failures),
        "dia": dia_dict,
        "combined_passed": base_passed and dia_dict.get("passed", False),
    }


def validate_with_dia_and_aeo(post_data: dict) -> dict:
    """기본 룰 + D.I.A.+ + AEO 세 차원을 모두 평가해 통합 결과 반환 (2026-05-25).

    네이버 AI 요약이 검색 상단에 노출되는 변화에 따라 AEO(Answer Engine
    Optimization) 신호를 D.I.A.+ 와 동등하게 평가한다.

    Returns:
        {
            "passed": bool,              # 기본 룰만 PASS 여부
            "failures": [str, ...],      # 기본 룰 실패 사유
            "dia": {...},                # D.I.A.+ 결과
            "aeo": {...},                # AEO 결과
            "combined_passed": bool,     # 기본 PASS AND dia.passed AND aeo.passed
            "combined_weaknesses": [...], # D.I.A.+ + AEO 약점 합집합 (중복 제거)
        }
    """
    base_passed, failures = validate(post_data)
    html = _resolve_html_for_scoring(post_data)
    # 이미지는 생성 후 dispatcher 가 figure 슬롯을 채운다 → 채점 시점의 '0장' 부당 감점 방지.
    html = _credit_planned_images(html, post_data.get("category", ""))

    # [설계도 주도 변주] 채점을 장르에 맞춰 — 비-info 장르에 정부출처·FAQ·통계·단계
    # 감점을 매기지 않게 blueprint 를 스코어러에 넘긴다. 없으면 None → 기존 채점과
    # 바이트 동일(회귀 0). fail-open: 파싱 실패해도 None 으로 폴백.
    _bp = post_data.get("_blueprint")
    if not isinstance(_bp, dict):
        _bp = None

    # D.I.A.+
    try:
        from core.dia_scorer import score as dia_score
        dia_dict = dia_score(html, blueprint=_bp).to_dict()
    except (ImportError, AttributeError) as exc:
        log.warning(f"[quality_validator] dia_scorer 호출 실패: {exc}")
        dia_dict = {"score": 0.0, "passed": False, "weaknesses": [],
                    "grades": {}, "counts": {}}

    # AEO
    try:
        from core.aeo_scorer import score as aeo_score
        aeo_dict = aeo_score(html, blueprint=_bp).to_dict()
    except (ImportError, AttributeError) as exc:
        log.warning(f"[quality_validator] aeo_scorer 호출 실패: {exc}")
        aeo_dict = {"score": 0.0, "passed": False, "weaknesses": [],
                    "grades": {}, "counts": {}}

    # 약점 합집합 — 순서 유지하면서 중복 제거. D.I.A.+ 약점을 먼저, AEO 를 뒤에.
    combined: list = []
    seen: set = set()
    for w in (*dia_dict.get("weaknesses", []), *aeo_dict.get("weaknesses", [])):
        if w in seen:
            continue
        seen.add(w)
        combined.append(w)

    return {
        "passed": base_passed,
        "failures": list(failures),
        "dia": dia_dict,
        "aeo": aeo_dict,
        "combined_passed": (
            base_passed
            and dia_dict.get("passed", False)
            and aeo_dict.get("passed", False)
        ),
        "combined_weaknesses": combined,
    }


def _dia_max_score() -> int:
    """D.I.A.+ 만점(가중치 합) — 로그 문구가 만점을 하드코딩하지 않게 한다.

    dia_scorer 에서 신호가 추가·제거되면(예: 2026-08-02 동영상 임베드 제거로 100→90)
    자동으로 따라온다. 임포트 실패 시 90 폴백 — 로그 문구 하나 때문에 검증이 죽지 않게.
    """
    try:
        from core.dia_scorer import MAX_SCORE
        return int(MAX_SCORE)
    except Exception:  # noqa: BLE001 — 로그용 값이라 실패해도 검증은 계속
        return 90


def _resolve_html_for_scoring(post_data: dict) -> str:
    """점수 산출용 HTML 본문을 추출. content_html 이 있으면 그것.

    ⚠️ 없을 때(생성 초기 검증 단계)는 **실제 발행 HTML**(markdown_to_naver_html)로
    변환한다 — 이게 표·리스트·FAQ 를 제대로 `<table>/<ol>` 태그로 만들고 figure 슬롯까지
    넣어 채점 점수가 발행물과 일치한다. (기존 _markdown_to_minimal_html 은 정밀도가 낮아
    표·리스트를 놓쳐 D.I.A.+ 점수를 구조적으로 과소평가했다.) 변환 실패 시 최소 변환 폴백.
    """
    html = post_data.get("content_html") or post_data.get("content_naver_html")
    if html:
        return html
    md = post_data.get("content", "")
    try:
        from core.content_generator import markdown_to_naver_html
        full = markdown_to_naver_html(
            md, title=post_data.get("title", ""), category=post_data.get("category", ""))
        if full and full.strip():
            return full
    except Exception:  # noqa: BLE001 — 변환 실패 시 최소 변환 폴백
        pass
    return _markdown_to_minimal_html(md)


def _planned_image_slots(category: str) -> int:
    """생성 후 image_dispatcher 가 채울 figure 슬롯 수(채점 시점엔 아직 미주입).

    D.I.A.+ 채점(validate_and_fix)은 markdown_to_naver_html + figure 주입 **이전**에
    돌기 때문에, 이미지가 항상 0장으로 잡혀 20점(최대 가중치)을 부당하게 잃는다.
    IMAGE_INJECT_ENABLED + 카테고리 화이트리스트를 반영해 '붙을 이미지 수'를 돌려준다.
    주입이 꺼져 있으면 0(크레딧 없음).
    """
    try:
        from config import settings as s
        if not getattr(s, "IMAGE_INJECT_ENABLED", False):
            return 0
        by_cat = getattr(s, "IMAGE_INJECT_BY_CATEGORY", None)
        if isinstance(by_cat, dict) and by_cat:
            if category in by_cat:
                if not bool(by_cat[category]):
                    return 0
            elif not bool(by_cat.get("default", True)):
                return 0
        if category == "디지털":
            return int(getattr(s, "IMAGE_PER_POST_DIGITAL", 8))
        return int(getattr(s, "IMAGE_PER_POST_DEFAULT", 6))
    except Exception:  # noqa: BLE001 — 설정 로드 실패 시 크레딧 없음(보수적)
        return 0


def _credit_planned_images(html: str, category: str) -> str:
    """채점용 HTML 에 '주입 예정 이미지'를 반영. 실제 content 는 건드리지 않는다.

    이미지가 생성 **후** 붙는 파이프라인에서 D.I.A.+ 이미지 20점을 부당하게 잃지 않게,
    아직 없는 figure 슬롯을 채점용 문자열에만 스텁으로 덧붙인다.
    """
    planned = _planned_image_slots(category)
    if planned <= 0:
        return html
    try:
        from core.dia_scorer import _IMG_RE
        have = len(_IMG_RE.findall(html or ""))
    except Exception:  # noqa: BLE001
        have = 0
    if have >= planned:
        return html
    stub = '<figure data-role="planned"><img src="" alt=""></figure>' * (planned - have)
    return (html or "") + stub


# ─────────────────────────────────────────────────────
# 재생성 프롬프트 공통 규격 (2026-08-02 감사 C2)
# ─────────────────────────────────────────────────────
# 재생성(build_fix_prompt / build_dia_aeo_fix_prompt)은 **본문 전체를 다시 쓴다**.
# 따라서 생성 프롬프트(content_generator)와 규격이 어긋나면, 재생성이 도는 순간
# 새로 넣은 차별 신호(판단 흔적·3줄 요약·FAQ 5개·구간별 톤)가 조용히 사라진다.
# 아래 상수·헬퍼가 두 경로의 규격을 한 곳에서 맞춘다.

# 생성 프롬프트가 외치는 '목표 분량'과 같은 숫자(상한은 rules["max_chars"] 단일 소스).
# 상한(3,200)은 게이트 값이고, 목표는 그보다 낮게 잡아 _trim_content 절삭을 예방한다.
_TARGET_MIN_CHARS = 2200
_TARGET_MAX_CHARS = 3000


def _target_length_range(rules: Optional[dict]) -> tuple[int, int]:
    """재생성 목표를 기존 목표와 유효 검증 구간의 겹치는 범위로 제한한다.

    기존 목표와 검증 구간이 겹치지 않으면 검증 구간 전체를 목표로 삼는다.
    룰이 잘못되었거나 범위가 역전된 경우에는 안전하게 기존 목표로 폴백한다.
    """
    if not isinstance(rules, dict):
        return _TARGET_MIN_CHARS, _TARGET_MAX_CHARS
    minimum = rules.get("min_chars", QUALITY_RULES["min_chars"])
    maximum = rules.get("max_chars", QUALITY_RULES["max_chars"])
    if (isinstance(minimum, bool) or isinstance(maximum, bool)
            or not isinstance(minimum, int) or not isinstance(maximum, int)
            or minimum <= 0 or maximum < minimum):
        return _TARGET_MIN_CHARS, _TARGET_MAX_CHARS
    overlap_min = max(_TARGET_MIN_CHARS, minimum)
    overlap_max = min(_TARGET_MAX_CHARS, maximum)
    if overlap_min <= overlap_max:
        return overlap_min, overlap_max
    return minimum, maximum


def _policy_brief_prompt_block(enabled: bool, target_min: int, target_max: int) -> str:
    """정책 소개형 플래그가 명시된 재생성에 생성 경로의 동일 계약을 붙인다."""
    if not enabled:
        return ""
    try:
        from core.content_generator import (
            POLICY_BRIEF_MAX_CHARS, POLICY_BRIEF_MIN_CHARS, _policy_brief_block,
        )
        block = _policy_brief_block().replace(
            f"{POLICY_BRIEF_MIN_CHARS}~{POLICY_BRIEF_MAX_CHARS}자",
            f"{target_min}~{target_max}자",
        )
        # 생성 프롬프트의 소개형 블록에는 일반 글 목표(2,200~3,000자)를 무시하라는
        # 고정 문장이 있다. 여기서는 앞선 재생성 요구도 이미 유효 목표로 조정됐으므로
        # 기본 숫자를 다시 노출하지 않고, 같은 유효 목표를 따르도록 연결한다.
        return re.sub(
            r"\(앞에 적힌 [\d,]+~[\d,]+자 목표는 이 글에 적용하지 않습니다\)\.",
            "(위에서 정한 목표 분량을 따르세요).",
            block,
        )
    except (ImportError, AttributeError):
        log.warning("정책 소개형 재생성 지침을 불러오지 못했습니다")
        return ""

# 재생성 호출에 조회 도구(WebSearch/WebFetch)를 붙일지 여부.
# ⚠️ validate_and_fix 의 _call_claude 호출과 [판단 흔적] 블록 분기가 **이 상수 하나**를
#    같이 본다. 재생성에서 웹검색을 켜려면 이 값만 True 로 바꾸면 두 곳이 함께 움직인다.
#    (False 인데 "확인해봤다·비교해봤다"를 쓰라고 지시하면 그건 날조 지시다 — P0 감사 C1.)
_REGEN_USE_WEB_SEARCH = False


def _judgment_prompt_block(has_research: bool,
                           target_min_chars: Optional[int] = None,
                           target_max_chars: Optional[int] = None) -> str:
    """생성 프롬프트와 동일한 [판단 흔적] + [안전선] 블록을 재생성용으로 가져온다.

    content_generator._judgment_block 을 재사용해 두 경로가 같은 문구를 쓰게 한다
    (복사본을 두면 한쪽만 고쳐지는 열화가 반드시 난다). 1차 자료 예시는 브랜드팩에서
    해석하므로 주제·계정 하드코딩이 없다.

    Args:
        has_research: 이 재생성 턴에 실제 조회 수단이 있는가(=_REGEN_USE_WEB_SEARCH).
        target_min_chars: 이 재생성에서 유효한 목표 하한. None이면 기본 목표를 사용한다.
        target_max_chars: 이 재생성에서 유효한 목표 상한. None이면 기본 목표를 사용한다.

    Returns:
        프롬프트에 이어붙일 블록. import 실패 시 빈 문자열(재생성 자체는 계속 진행).

    ⚠️ import 는 함수 안에서 — content_generator 가 quality_validator 를 함수 스코프로
    임포트하므로 모듈 레벨에서 맞물리면 순환 import 가 된다.
    """
    try:
        from core.content_generator import _brand_prompt_bits, _judgment_block
        bits = _brand_prompt_bits(_date.today().strftime("%Y년 %m월 %d일"), "")
        block = _judgment_block(bool(has_research), bits["judgment_sources"])
        if target_min_chars is None or target_max_chars is None:
            target_min_chars, target_max_chars = _target_length_range(QUALITY_RULES)
        return block.replace(
            "위 목표(2,200~3,000자) 그대로 유지합니다.",
            f"위 목표({target_min_chars:,}~{target_max_chars:,}자) 그대로 유지합니다.",
        )
    except Exception as exc:  # noqa: BLE001 — 블록 없이라도 재생성은 진행
        log.warning("[quality_validator] 판단 흔적 블록 로드 실패 (생략): %s", exc)
        return ""


def _keyword_prompt_block(title: str, category: Optional[str]) -> str:
    """생성 프롬프트와 동일한 [핵심 키워드 운용] 블록을 재생성용으로 가져온다.

    content_generator._keyword_variation_block 재사용 — 문구를 복사해 두면
    한쪽만 고쳐져 재작성본에서만 기계적 반복이 되살아난다.

    Args:
        title: 글 제목.
        category: 카테고리(없으면 핵심어 선정 보정 없이 진행).

    Returns:
        블록 문자열. 실패하면 빈 문자열(재생성 자체는 계속 진행).
    """
    try:
        from core.content_generator import _keyword_variation_block
        return _keyword_variation_block(title or "", category or "")
    except Exception as exc:  # noqa: BLE001 — 블록 없이라도 재생성은 진행
        log.warning("[quality_validator] 키워드 변주 블록 로드 실패 (생략): %s", exc)
        return ""


def _rewrite_style_spec(has_research: bool, title: str = "",
                        category: Optional[str] = None,
                        target_min_chars: Optional[int] = None,
                        target_max_chars: Optional[int] = None) -> str:
    """재생성본이 지켜야 할 도입부·문체·키워드·마무리·마커·장식 규격.

    생성 프롬프트(content_generator)와 같은 규격을 두 재생성 경로
    (build_fix_prompt / build_dia_aeo_fix_prompt)에 한 번에 싣는 단일 소스다.
    한쪽에만 넣으면 그 경로를 안 탄 재작성본에서만 조용히 신호가 빠진다.

    포함 규격:
      · 도입부 순서(인사말 → 훅 → 3줄 요약 → 판단 흔적)
      · 구간별 톤·종결어미 3연속 금지·핵심 키워드 변주
      · 마무리 구독 CTA 1줄 — 2026-08-02 추가. 라이브 10편 실측에서 생성본에는 있던
        이웃추가·구독 한 줄이 재작성 후 8/10에서 사라졌다(engagement subscribe_cta absent).
      · 본문 마커 규칙 — `<!-- IMG: -->` 만 허용. `<!-- VIDEO: -->` 는 발행기가 처리하지
        못해 본문에 그대로 남으므로 금지(실측 4/10편에서 발생).

    Args:
        has_research: 조회 수단 유무 — [판단 흔적] ②의 서술을 가른다.
        title: 글 제목 — [핵심 키워드 운용] 블록의 핵심어·파생어 힌트에 쓴다.
        category: 카테고리 — 핵심어 선정 보정용(없어도 동작).
        target_min_chars: 판단 흔적 블록에 전달할 유효 목표 하한.
        target_max_chars: 판단 흔적 블록에 전달할 유효 목표 상한.
    """
    judgment_block = _judgment_prompt_block(
        has_research, target_min_chars, target_max_chars,
    )
    from core.topic_policy import initial_answer_axes
    summary_axes = initial_answer_axes(title, category)
    keyword_block = _keyword_prompt_block(title, category)
    if keyword_block:
        keyword_block += "\n"
    if judgment_block:
        # 블록 끝의 "금지 표현 목록" 언급은 생성 프롬프트의 [카테고리 맞춤 전략]을 가리킨다 —
        # 재생성 프롬프트에는 그 섹션이 없으므로 어디를 말하는지 한 줄로 못 박는다.
        judgment_block += '\n  (여기서 말하는 "금지 표현 목록"은 위 [필수 반영 사항]에 적힌 목록입니다.)'
    return f"""[★ 도입부 순서 — 인사말 → 훅 → 3줄 요약 → 판단 흔적]
- 도입 훅 뒤에 "핵심만 먼저 정리하면" 또는 "결론부터 말하면" 으로 운을 뗀 뒤,
  이 글의 핵심 결론을 3줄로 씁니다(각 1문장, 줄바꿈으로 구분).
- 3줄에 담을 것: "{summary_axes}"의 결론.
- 각 줄은 그 한 줄만 읽어도 이해되게 자기완결적으로 (AI 요약이 이런 문장을 통째로 발췌합니다).
- ★ answer-first 첫 문장과 겹치지 않게 — 3줄 요약의 첫 줄이 곧 그 직접 답변이 되도록 이어 쓰고,
  같은 내용을 도입부에서 두 번 반복하지 마세요.
- ⚠️ 이건 **본문 문장**입니다. 이모지 헤딩(## 📌 핵심)·뱃지·테두리 박스·체크리스트 기호(□ ☑)로
  꾸미는 순간 아래 [장식 금지]에 걸립니다.

{judgment_block}

[★ 문체 — 구간별 톤 분리]
- 사실 전달 문장(자격·금액·기한·조건·절차, 표 안, FAQ 답변의 결론 문장)
  → "~입니다", "~합니다" 체 유지. 정확성이 우선이라 여기선 구어체를 쓰지 말 것.
- 도입 훅 · 판단 흔적 · 마무리 CTA
  → 말하듯 구어체 허용: "~더라고요", "~거든요", "~같아요", "~추천드려요", "~보시면 좋겠어요".
- ★ 같은 종결어미를 3문장 연속 쓰지 말 것. "~입니다"만 죽 이어지면 사람이 쓴 글로 읽히지 않습니다.
- ★ 문장 길이 변주: 짧은 문장과 긴 문장을 의도적으로 섞을 것.
- ★ 같은 단어(특히 핵심 키워드)를 기계적으로 반복하지 말 것 — 반복 밀도는 AI 티 신호입니다.
- 위 금지 표현 목록이 이 문체 지시보다 우선합니다(구어체가 금지 표현에 걸리면 다른 말로 바꿀 것).

{keyword_block}
[★ 마무리 — 구독 유도 1줄까지가 마무리다]
- "## 정리"(또는 "## 한 줄 결론") 소제목 + 핵심 1~2문장 요약으로 본문을 닫습니다.
- ★ 그 바로 뒤에 **행동 유도 한 줄**: 도움이 됐다면 이웃추가·구독을 권하는 문장 1줄
  (과하지 않게, 구어체 허용. 예: "도움이 되셨다면 이웃 추가해두시면 좋겠어요.")
- ⚠️ 이 한 줄을 빼먹지 마세요 — 재작성본에서 가장 자주 사라지는 구성 요소입니다.
- 그 뒤에 #태그 줄이 옵니다.

[본문 마커 규칙 — 허용은 IMG 하나뿐]
- ✅ 허용: `<!-- IMG: 추천 캡션 -->` (사용자가 그 자리에 직접 사진을 첨부합니다)
- ❌ 금지: `<!-- VIDEO: ... -->` 를 비롯한 그 밖의 모든 HTML 주석 마커.
  발행 파이프라인에 동영상 임베드 경로가 없어, 처리되지 못한 채 본문에 그대로 남습니다.
- ❌ 금지: `<iframe>`·`<video>` 등 직접 임베드 태그.

[장식 금지]
- ❌ 최신 정보를 확인한 것처럼 보이는 뱃지·배너, 목차(TOC), 체크리스트(□ ☑)
- ❌ 장식용 "요약 박스" 위젯, 이모지 헤딩(## 📌, ## ✅). 소제목은 텍스트만.
- ❌ 같은 내용을 다른 표현으로 반복"""


def _blueprint_regen_appendix(blueprint: Optional[dict]) -> str:
    """블루프린트가 있으면 재생성 프롬프트에 덧붙일 골격/어법 지시(없으면 "").

    content_generator.bp_regen_appendix 를 재사용 — 생성과 재생성이 같은 골격을
    쓰게 하는 단일 소스(복사본을 두면 한쪽만 갱신되는 열화가 난다). import 는 함수
    안에서(순환 import 회피).
    """
    if not isinstance(blueprint, dict):
        return ""
    try:
        from core.content_generator import bp_regen_appendix
        block = bp_regen_appendix(blueprint)
        return ("\n\n" + block) if block else ""
    except Exception as exc:  # noqa: BLE001 — 블록 없이라도 재생성은 진행
        log.debug("[quality_validator] 블루프린트 재생성 블록 생략: %s", exc)
        return ""


def _editorial_brief_prompt(brief: Optional[dict], category: str = "",
                            title: str = "", content: str = "") -> str:
    """Render declared editorial intent for each QA rewrite without inventing details."""
    from core.topic_policy import topic_guidance
    from core.article_quality import prompt_block as article_quality_prompt
    topic_block = topic_guidance(category=category, title=title)
    quality_block = article_quality_prompt(title, category, content=content)
    if not isinstance(brief, dict):
        return "\n\n".join(part for part in (topic_block, quality_block) if part)
    rows = []
    for key, label in (("reader_question", "독자 질문"), ("answer_summary", "직접 답"),
                       ("reader_action", "다음 행동"), ("focus", "편집 초점")):
        value = str(brief.get(key) or "").strip()
        if value:
            rows.append(f"- {label}: {value}")
    structure = brief.get("structure")
    if isinstance(structure, dict):
        if structure.get("table") is False:
            rows.append("- 표: 독자 답변에 필요하지 않으므로 넣지 않는다")
        if structure.get("faq_count") == 0:
            rows.append("- FAQ: 실제 후속 질문이 없으면 만들지 않는다")
    brief_block = ("[유지할 편집 브리프 — 독자 질문이 형식 요구보다 우선]\n" + "\n".join(rows)
                   if rows else "")
    return "\n\n".join(block for block in (topic_block, quality_block, brief_block) if block)


def build_dia_aeo_fix_prompt(title: str, prev_content: str,
                              combined_weaknesses: list[str],
                              rules: Optional[dict] = None,
                              has_research: bool = _REGEN_USE_WEB_SEARCH,
                              category: Optional[str] = None,
                              blueprint: Optional[dict] = None,
                              editorial_brief: Optional[dict] = None,
                              policy_brief: bool = False) -> str:
    """D.I.A.+ + AEO 약점 합집합을 그대로 재작성 프롬프트로 변환.

    quality_validator.build_fix_prompt 와 시그니처가 일치 (failures 자리에
    combined_weaknesses 가 들어감). validate_with_dia_and_aeo 의
    combined_weaknesses 를 받아 자동 보완 루프에서 사용.

    ⚠️ 이 프롬프트는 **본문 전체를 다시 쓰게 한다**. DIA_AEO_AUTOFIX_BY_CATEGORY 가
    기본 활성이라 사실상 모든 글이 이 경로를 지날 수 있으므로, 생성 프롬프트와 규격
    (분량·FAQ 개수·판단 흔적·3줄 요약·장식 금지)이 반드시 일치해야 한다.

    Args:
        title: 글 제목.
        prev_content: 직전 본문(앞 800자만 참고로 전달).
        combined_weaknesses: D.I.A.+/AEO 약점 목록. 비면 빈 문자열 반환.
        rules: 분량 상한 등 카테고리별 룰(미지정 시 기본 QUALITY_RULES).
        has_research: 이 재생성 턴에 조회 도구가 붙는지(기본 _REGEN_USE_WEB_SEARCH).
        category: 카테고리 — [핵심 키워드 운용] 블록의 핵심어 선정 보정용(선택).
    """
    if not combined_weaknesses:
        return ""

    if rules is None:
        rules = QUALITY_RULES
    max_chars = rules.get("max_chars", QUALITY_RULES["max_chars"])
    target_min_chars, target_max_chars = _target_length_range(rules)

    # 브랜드 팩 오버라이드(named 프로필) — default 는 빈 문자열이라 기존 리터럴로 폴백.
    try:
        from config import brand as _brand
        configured_role = _brand.writer_role()
        configured_sources = _brand.primary_sources()
    except Exception:  # noqa: BLE001 — 브랜드 로드 실패 시 기존 동작 유지
        configured_role = ""
        configured_sources = ""
    from core.topic_policy import is_government_policy, source_guidance, writer_role
    policy_topic = is_government_policy(title, category)
    role = writer_role(title, category, configured_role)
    sources = source_guidance(title, category, configured_sources)

    issues = "\n".join(f"  - {w}" for w in combined_weaknesses[:8])
    today = _date.today().strftime("%Y년 %m월 %d일")
    year = _date.today().year
    # 재작성이 금지어를 새로 유입시키지 않게 전체 목록 재고지(build_fix_prompt 와 동일 취지).
    _forbidden_all = _load_forbidden_words(None)
    _forbidden_line = ("\n- 금지 표현(하나도 쓰지 말 것): " + ", ".join(_forbidden_all)
                       if _forbidden_all else "")
    # 생성 프롬프트와 같은 규격(3줄 요약·판단 흔적·키워드 변주·구간별 톤·장식 금지).
    style_spec = _rewrite_style_spec(
        has_research, title, category, target_min_chars, target_max_chars,
    )
    # 설계도 주도 변주 ON 이면 이 글의 골격/어법 지시를 덧붙인다(생성과 같은 골격 재현).
    style_spec += _blueprint_regen_appendix(blueprint)
    brief_block = _editorial_brief_prompt(editorial_brief, category or "", title,
                                           prev_content)

    # ★ [필수 반영 사항]을 rules(=블루프린트 반영 regen_rules) 로 조건부화 (C2 수정).
    #   build_fix_prompt 와 동일 패턴 — FAQ/표/단계/마무리 요구가 블루프린트 골격
    #   (FAQ 0개·표 없음·고정 헤더 금지)과 모순되지 않게 한다. 블루프린트가 있으면
    #   구조·마무리는 위 style_spec 의 설계도 지시가 소유하므로 여기선 뺀다.
    require_faq   = policy_topic and rules.get("require_faq", QUALITY_RULES["require_faq"])
    require_table = policy_topic and rules.get("require_table", QUALITY_RULES["require_table"])
    dia_req = ["- 독자가 던질 핵심 질문에 먼저 답하고, 적용 조건과 주제에 맞는 근거를 설명한다."]
    dia_req.append("- 분량·FAQ·표 형식보다 독자 질문의 직접 답변을 우선하고, 채우기용 반복은 쓰지 않는다.")
    if blueprint is None:
        dia_req.append('- 본문에 단계 마커("1단계 / 2단계 ..." 또는 "① ② ③") 5개 이상으로 절차 분명화')
    if require_faq:
        dia_req.append(
            '- "❓ FAQ"(자주 묻는 질문) 섹션에 Q1. / Q2. / Q3. / Q4. / Q5. 형식 정확히 5문항\n'
            "  (질문은 의문문 종결, 각 답변 2문장 이내·결론을 첫 문장에, 5개가 서로 다른 것을 물을 것)")
    if policy_topic:
        dia_req.append(f"- 해당 제도의 1차 출처({sources})를 실제 확인한 경우에만 출처로 밝힌다.")
        dia_req.append("- 확인된 구체 수치(금액·자격·기간)를 독자가 이해하기 쉽게 설명한다. 근거 없는 수치는 만들지 않는다.")
    if require_table:
        dia_req.append("- 마크다운 표 1개 이상 (자격·금액 등 비교·정리)")
    if blueprint is None:
        dia_req.append(
            '- 본문 끝 "## 한 줄 결론" 또는 "## 정리" 섹션 1개로 핵심 1~2문장 마무리(이모지 없이 텍스트만)')
    dia_req += [
        "- 이미지 위치 마커 `<!-- IMG: 추천 캡션 -->` 2~3개를 자연스러운 위치에",
        "- 본문 맨 끝에 #태그 10개 (한 줄로 이어서)",
        f"- {sources} 등 주제에 맞는 근거를 실제로 확인한 경우에만 명시한다. 확인하지 않은 출처·경험은 쓰지 않는다.",
        f"- 글자 수: 목표 {target_min_chars:,}~{target_max_chars:,}자 — ★ {max_chars:,}자 초과 절대 금지\n"
        "  (넘기면 시스템이 ## 섹션째 잘라내 내용이 손실됩니다)",
    ]
    if policy_topic:
        dia_req.append(f"- {year}년 현재 확인되는 정책·자격·신청 정보를 다루고, 불확실하면 단정하지 않는다{_forbidden_line}")
    else:
        dia_req.append("- 현재 기준이 필요한 사실은 근거를 확인하고, 확인할 수 없는 내용은 불확실성을 분명히 밝힌다" + _forbidden_line)
    dia_required_block = "\n".join(dia_req)

    # JSON content 힌트도 요구치에 맞춰(모순 문구 제거).
    _content_bits = ["도입: 훅+3줄 요약+판단 흔적"]
    if require_table:
        _content_bits.append("표")
    if require_faq:
        _content_bits.append("FAQ")
    if blueprint is None:
        _content_bits.append("'## 정리' 마무리")
    _content_hint = ", ".join(_content_bits)
    policy_brief_block = _policy_brief_prompt_block(
        policy_brief, target_min_chars, target_max_chars)
    return f"""당신은 {role}입니다.
아래 글을 D.I.A.+ 검색 가산 신호와 AEO(AI 요약 인용) 신호 양쪽에서 보강하도록 다시 작성해주세요.

제목: {title}
오늘 날짜: {today}

[보완해야 할 항목]
{issues}

[필수 반영 사항]
{dia_required_block}

{style_spec}

{brief_block}

{policy_brief_block}

이전 글 참고:
{prev_content[:800]}...

반드시 JSON 형식으로만 응답 (마크다운 코드블록 없이):
{{
  "title": "SEO+AEO 최적화된 최종 제목",
  "content": "마크다운 본문 전체 (목표 {target_min_chars}~{target_max_chars}자·{max_chars}자 초과 금지, {_content_hint})",
  "summary": "한 줄 요약 (60자 이내)",
  "tags": ["태그1","태그2",..."태그15"],
  "sources": []
}}"""


def _markdown_to_minimal_html(md: str) -> str:
    """가벼운 마크다운 → HTML 변환 — D.I.A.+ 신호 인식용 (정밀하지 않음)."""
    if not isinstance(md, str):
        return ""
    out = md
    # 표 — |로 시작하는 줄을 <table>로 묶기 (간략)
    if re.search(r"^\|.+\|", out, re.MULTILINE):
        out = re.sub(
            r"((?:^\|.+\|\s*\n)+)",
            lambda m: "<table>" + m.group(1) + "</table>",
            out, flags=re.MULTILINE,
        )
    # 이미지 — ![alt](url)
    out = re.sub(r"!\[[^\]]*\]\([^\)]+\)", "<figure><img src=\"\" alt=\"\"></figure>", out)
    # 리스트
    out = re.sub(r"((?:^\-\s+.+\n?)+)", r"<ul>\1</ul>", out, flags=re.MULTILINE)
    out = re.sub(r"((?:^\d+\.\s+.+\n?)+)", r"<ol>\1</ol>", out, flags=re.MULTILINE)
    # 헤딩
    out = re.sub(r"^##\s+(.+)$", r"<h2>\1</h2>", out, flags=re.MULTILINE)
    out = re.sub(r"^###\s+(.+)$", r"<h3>\1</h3>", out, flags=re.MULTILINE)
    return out


# ─────────────────────────────────────────────────────
# 재생성 요청 프롬프트 구성
# ─────────────────────────────────────────────────────
def build_fix_prompt(title: str, prev_content: str, failures: list[str],
                     rules: Optional[dict] = None,
                     category: Optional[str] = None,
                     has_research: bool = _REGEN_USE_WEB_SEARCH,
                     blueprint: Optional[dict] = None,
                     editorial_brief: Optional[dict] = None,
                     policy_brief: bool = False) -> str:
    """재생성 프롬프트를 구성합니다.

    글자수·H2·표·FAQ·태그 요구치는 실제 검증에 쓰이는 rules dict에서 그대로
    가져와 프롬프트에 노출합니다(단일 소스화 — 하드코딩과 검증 룰의 불일치 제거).
    rules 미지정 시 기본 QUALITY_RULES 를 사용합니다.

    category 가 주어지고 failures 에 fact_gate 유래 실패(허구수치·수치모순 등)가
    있으면, welfare_reference.anchor_block 으로 공식 확정값 블록을 프롬프트에 추가해
    모델이 수치를 다시 지어내지 않게 앵커한다(복지/급여 카테고리일 때만, 아니면 무동작).

    재작성은 본문 전체를 다시 쓰므로 생성 프롬프트와 같은 규격(3줄 요약·판단 흔적·
    구간별 톤·장식 금지)을 _rewrite_style_spec 으로 함께 싣는다. has_research 는 이
    재생성 턴에 조회 도구가 붙는지 여부(기본 _REGEN_USE_WEB_SEARCH)로, 도구가 없으면
    "확인해봤다·비교해봤다" 같은 조회 행위 주장을 금지하는 판이 실린다.
    """
    if rules is None:
        rules = QUALITY_RULES

    # 브랜드 팩 오버라이드(named 프로필) — default 는 빈 문자열이라 기존 리터럴로 폴백.
    try:
        from config import brand as _brand
        configured_role = _brand.writer_role()
        configured_sources = _brand.primary_sources()
    except Exception:  # noqa: BLE001 — 브랜드 로드 실패 시 기존 동작 유지
        configured_role = ""
        configured_sources = ""
    from core.topic_policy import is_government_policy, source_guidance, writer_role
    policy_topic = is_government_policy(title, category)
    role = writer_role(title, category, configured_role)
    sources = source_guidance(title, category, configured_sources)

    issues = "\n".join(f"  - {f}" for f in failures)
    today = _date.today().strftime("%Y년 %m월 %d일")

    min_chars = rules.get("min_chars", QUALITY_RULES["min_chars"])
    max_chars = rules.get("max_chars", QUALITY_RULES["max_chars"])
    target_min_chars, target_max_chars = _target_length_range(rules)
    min_h2    = rules.get("require_h2", QUALITY_RULES["require_h2"])
    min_tags  = rules.get("min_tags", QUALITY_RULES["min_tags"])
    require_table = policy_topic and rules.get("require_table", QUALITY_RULES["require_table"])
    require_faq   = policy_topic and rules.get("require_faq", QUALITY_RULES["require_faq"])

    # 검증 룰과 1:1로 대응하는 요구사항 라인만 조건부로 노출.
    req_lines = [
        "- 독자가 던질 핵심 질문에 먼저 답하고, 적용 조건과 주제에 맞는 근거를 설명한다.",
        "- 분량·FAQ·표 형식보다 독자 질문의 직접 답변을 우선하고, 채우기용 반복은 쓰지 않는다.",
        f"- 글자 수: {min_chars:,}~{max_chars:,}자 — ★ {max_chars:,}자 초과 절대 금지",
        # 게이트는 넓지만(하한~상한) 실제로 겨냥할 구간은 생성 프롬프트와 같은 목표치다.
        # 상한에 붙여 쓰면 _trim_content 가 ## 섹션째 잘라내 판단 흔적·3줄 요약이 먼저 날아간다.
        f"- 그중 목표는 {target_min_chars:,}~{target_max_chars:,}자 — 늘어난 분량은 "
        f"섹션 수가 아니라 각 섹션의 충실도에 쓸 것(새 소제목을 만들지 말 것)",
    ]
    if require_table:
        req_lines.append("- 마크다운 표는 독자의 질문을 명확히 비교·정리하는 데 도움이 될 때만 사용한다")
    if require_faq:
        # 2026-09-18 — 생성 프롬프트가 "필요할 때만 3~5개" 로 바뀌었으므로 재생성도 같은
        # 범위를 요구한다. 여기만 "정확히 5개"로 남겨두면 재작성본에서만 억지 FAQ 가
        # 되살아나 생성 경로와 어긋난다(양쪽을 함께 옮긴다 — 요건 3).
        # 검증 게이트는 여전히 '존재 여부'만 보므로 개수 때문에 FAIL 되지는 않는다.
        req_lines.append(
            "- FAQ: \"자주 묻는 질문\" 섹션에 Q&A 형식 3~5개 "
            "(Q1. Q2. … 각 답변 2문장 이내·결론 먼저, 서로 다른 것을 물을 것)")
    req_lines.append(f"- ## 소제목: 최소 {min_h2}개 (질문형 또는 핵심 키워드형, 이모지 없이)")
    req_lines.append(f"- 태그: {min_tags}개")
    # 재작성은 본문을 통째로 새로 쓴다 — 생성 프롬프트가 만들던 구조 신호를 여기서 다시
    # 요구하지 않으면 재작성본에서만 조용히 사라진다(마무리·이미지 슬롯·해시태그).
    # ★ 블루프린트가 있으면 마무리 형태는 골격(closing)이 소유하므로 "## 정리" 고정 헤더를
    #   요구하지 않는다(C3 — blacklist_global 의 '고정 마무리 헤더 금지'와의 상시 모순 제거).
    if blueprint is None:
        req_lines.append(
            "- 구조 유지: 본문 끝 \"## 정리\"(또는 \"## 한 줄 결론\") 소제목 + 핵심 1~2문장 마무리, "
            "이미지 위치 마커 `<!-- IMG: 추천 캡션 -->` 2~3개, 본문 맨 끝에 #태그 10개 한 줄"
        )
    else:
        req_lines.append(
            "- 구조 유지: 이미지 위치 마커 `<!-- IMG: 추천 캡션 -->` 2~3개, 본문 맨 끝에 #태그 10개 한 줄 "
            "(마무리 형태는 아래 [이 글의 설계도] 지시를 따를 것)"
        )
    # 1차 출처 — YMYL 무근거 발행 차단(fact_gate)을 정직하게 통과시키기 위해, 재생성 시에도
    # 공식 기관 대표 URL 을 sources 배열에 반드시 채운다(실재·안정적인 1차 출처만).
    if policy_topic:
        req_lines.append(
            f"- 해당 제도의 1차 출처({sources}) 등 공식 자료를 실제 확인했을 때만 sources와 본문에 출처를 적는다. "
            "확인하지 않은 URL이나 지원 조건·수치를 만들지 않는다."
        )
        req_lines.append(
            f"- {_date.today().year}년 현재 확인되는 정책 정보만 단정하고, 불확실한 금액·자격·기한은 확인 필요로 표시한다."
        )
    else:
        req_lines.append(
            f"- {sources} 등 주제에 맞는 1차·전문 자료를 실제 확인했을 때만 sources와 본문에 출처를 적는다. "
            "출처·수치·개인 경험을 지어내지 말고, 확인하지 못한 사실은 한계를 밝힌다."
        )
    # 금지어 전체 목록 재고지(2026-07-19) — 종전엔 '걸린 단어'만 failures 로 전달돼
    # 재작성본이 다른 금지어를 새로 쓰는 두더지잡기가 났다(3연속 소진 실측:
    # 살펴보겠습니다→꼭·반드시→최근·을 통해). 어떤 실패 유형이든 전체 목록을 다시 박는다.
    _forbidden_all = _load_forbidden_words(category)
    if _forbidden_all:
        req_lines.append(
            "- ★ 금지 표현 전체 목록(아래 단어·표현을 하나도 쓰지 말 것 — "
            "지적된 것 외의 단어도 전부 해당): " + ", ".join(_forbidden_all)
        )
    required_block = "\n".join(req_lines)

    # fact_gate 유래 실패(허구 수치·수치 모순·무근거 YMYL)면 공식 확정값 앵커를 덧붙인다.
    # 복지/급여 카테고리 + 관련 급여가 실제 언급될 때만 블록이 나오고, 그 외엔 빈 문자열
    # (없는 근거를 지어내게 유도하지 않는다). 앵커가 비면 기존 프롬프트 그대로 —
    # 그래도 실패하면 auto_scheduler 가 본문을 needs_fix 로 보관한다(안전장치).
    fact_markers = ("허구수치", "수치모순", "무근거_YMYL", "[fact_gate]")
    if failures and any(any(m in f for m in fact_markers) for f in failures):
        try:
            from core.welfare_reference import anchor_block
            anchor = anchor_block(category, failures, prev_content)
        except Exception:  # noqa: BLE001 — 앵커 실패가 재생성을 막으면 안 됨
            anchor = ""
        if anchor:
            required_block += "\n\n" + anchor

    tags_example = ",".join(f"\"태그{i}\"" for i in range(1, min_tags + 1))
    content_hint = f"마크다운 본문 전체 ({min_chars:,}~{max_chars:,}자, {max_chars:,}자 초과 금지"
    if require_table:
        content_hint += ", 표 포함"
    if require_faq:
        content_hint += ", FAQ 5개 포함"
    content_hint += "; 독자 질문을 우선하고 표·FAQ는 답변에 실제로 도움이 될 때만 포함)"

    # 생성 프롬프트와 같은 규격(3줄 요약·판단 흔적·키워드 변주·구간별 톤·장식 금지).
    style_spec = _rewrite_style_spec(
        has_research, title, category, target_min_chars, target_max_chars,
    )
    # 설계도 주도 변주 ON 이면 이 글의 골격/어법 지시를 덧붙인다(생성과 같은 골격 재현).
    style_spec += _blueprint_regen_appendix(blueprint)
    brief_block = _editorial_brief_prompt(editorial_brief, category or "", title,
                                           prev_content)
    policy_brief_block = _policy_brief_prompt_block(
        policy_brief, target_min_chars, target_max_chars)

    return f"""당신은 {role}입니다.
아래 제목의 글을 다시 작성해주세요. 이전 글에서 다음 문제점이 발견됐습니다:

제목: {title}
오늘 날짜: {today}
문제점:
{issues}

[필수 수정 사항]
{required_block}

{style_spec}

{brief_block}

{policy_brief_block}

이전 글 참고 (개선 필요):
{prev_content[:800]}...

반드시 아래 JSON 형식으로만 응답 (마크다운 코드블록 없이):
{{
  "title": "SEO 최적화된 최종 제목",
  "content": "{content_hint}",
  "summary": "한 줄 요약",
  "tags": [{tags_example}],
  "category": "{category or 'general'}",
  "sources": []
}}"""


# ─────────────────────────────────────────────────────
# 검증 + 재생성 통합 함수
# ─────────────────────────────────────────────────────
def is_dia_aeo_autofix_enabled(category: str | None) -> bool:
    """settings.DIA_AEO_AUTOFIX_BY_CATEGORY 에 따라 카테고리별 자동 보완 활성 여부 판정.

    None/누락 → False (기존 동작 유지). dict 이면 화이트리스트:
        - 카테고리 키가 있으면 그 값(True/False) 반영.
        - 누락된 카테고리는 "default" 키 값(없으면 False) 사용.
    settings.py 가 GUI 에서 갱신될 수 있어 reload 를 시도하지만, reload 실패 시
    sys.modules 의 현재 모듈을 그대로 사용해 테스트가 fake settings 를
    주입할 수 있게 한다.
    """
    try:
        import sys as _sys
        _s = _sys.modules.get("config.settings")
        if _s is None:
            import config.settings as _s  # noqa: F811
        try:
            # 재로드 정책은 config.reload_settings 한 곳에만 있다(테스트 격리 존중).
            from config import reload_settings as _reload_settings
            _reload_settings()
        except Exception:  # noqa: BLE001 — reload 실패해도 현재 모듈 사용
            pass
        cfg = getattr(_s, "DIA_AEO_AUTOFIX_BY_CATEGORY", None)
    except (ImportError, AttributeError, ValueError):
        return False

    if not isinstance(cfg, dict) or not cfg:
        return False
    if category and category in cfg:
        return bool(cfg[category])
    return bool(cfg.get("default", False))


def _inherit_web_evidence(new_data: dict, prev_data: dict) -> None:
    """일반 재생성의 모델 근거를 버리고 이전 검증 근거만 승계한다.

    일반 수정 호출은 웹 검색을 하지 않는다. 따라서 모델 응답의 sources 나
    web_search_done 값은 증거로 취급하지 않고, content_generator의 공통 수확기가
    원본에서 검증된 근거만 보존하도록 위임한다.

    Args:
        new_data:  재생성 응답 dict(채택 예정). 이 함수가 제자리에서 근거 키를 채운다.
        prev_data: 직전(원본) post_data dict. 검증된 근거의 보존 출처.
    """
    if not isinstance(new_data, dict) or not isinstance(prev_data, dict):
        return
    # 모델 출력의 sources / True는 증거가 아니다. WebFetch 영수증이 없는 일반
    # 재생성은 이전에 검증된 근거만 보존하도록 생성기의 공통 게이트에 맡긴다.
    try:
        from core.content_generator import apply_web_read_evidence
    except (ImportError, AttributeError):
        # 구형/부분 테스트 환경에서도 모델이 만든 출처를 승격하지 않는다.
        new_data.pop("sources", None)
        new_data["web_search_done"] = False
        new_data.pop("research_cache_used", None)
        return
    apply_web_read_evidence(new_data, frozenset(), requested=False, previous=prev_data)


def _needs_evidence_recovery(post_data: dict, failures: List[str]) -> bool:
    """근거 부재만으로 막힌 글을 한 번의 검색 보강 대상으로 식별한다."""
    if post_data.get("_failed_reason") == "policy_no_evidence":
        return True
    return any(
        "무근거_YMYL" in failure or "source_evidence_missing" in failure
        for failure in failures
    )


def _has_recovered_evidence(post_data: dict) -> bool:
    """실제 읽기 플래그와 출처가 모두 있는지 확인한다."""
    sources = [source for source in (post_data.get("sources") or [])
               if isinstance(source, str) and source.strip()]
    if post_data.get("web_search_done") is not True or not sources:
        return False
    if post_data.get("_policy_brief") is True:
        try:
            from core.content_generator import _official_detail_path
        except (ImportError, AttributeError):
            return False
        return any(_official_detail_path(source) for source in sources)
    return True


def _mark_evidence_recovery_failed(post_data: dict, cause: str) -> dict:
    """근거 회복을 못 한 글을 발행 보류 상태로 남긴다."""
    post_data["_qa_failed"] = True
    post_data["_qa_failed_reason"] = "source_evidence_missing"
    post_data["_qa_failures"] = [f"source_evidence_missing: {cause}"]
    post_data["_failed"] = True
    post_data["_failed_reason"] = "source_evidence_missing"
    log.warning("근거 회복 실패 — 일반 무웹 재생성 없이 발행 보류: %s", cause)
    return post_data


def _clear_recovered_evidence_failure(post_data: dict) -> None:
    """근거 회복 성공 시 근거 부재 원인만 제거한다."""
    if post_data.get("_failed_reason") == "policy_no_evidence":
        post_data.pop("_failed", None)
        post_data.pop("_failed_reason", None)
    failures = post_data.get("_qa_failures")
    if isinstance(failures, list):
        remaining = [
            failure for failure in failures
            if not any(marker in str(failure) for marker in
                       ("source_evidence_missing", "무근거_YMYL"))
        ]
        if remaining:
            post_data["_qa_failures"] = remaining
        else:
            post_data.pop("_qa_failures", None)
            if post_data.get("_qa_failed_reason") in (
                "source_evidence_missing", "policy_no_evidence",
            ):
                post_data.pop("_qa_failed", None)
                post_data.pop("_qa_failed_reason", None)


def _inherit_editorial_brief(new_data: dict, prev_data: dict) -> None:
    """재생성본이 원본의 편집 브리프를 이어받게 한다(제자리 갱신).

    브리프는 "이 글이 어떤 질문에 답하는가 + 표/FAQ 를 쓰는가"를 담은다. 재생성이 이걸
    버리면 ① 검증이 다시 전역 게이트로 돌아가 방금 완화한 구성이 실패로 뒤집히고
    ② 썸네일 메시지가 사라져 본문과 썸네일이 다른 말을 한다(2026-09-10 "재생성이 계획을
    삼키던" 사고와 같은 클래스).
    """
    if not isinstance(new_data, dict) or not isinstance(prev_data, dict):
        return
    for key in ("_editorial_brief", "editorial_brief"):
        if key not in new_data and isinstance(prev_data.get(key), dict):
            new_data[key] = prev_data[key]
    # 정책 소개형 플래그도 같은 이유로 승계한다 — 재생성본이 이걸 잃으면 분량 계약이
    # 기본(1,800자 하한)으로 되돌아가, 수치를 뺀 소개형이 "너무 짧다"로 뒤집힌다.
    # 내부 모드는 이전 호출 컨텍스트만 권위가 있다. 모델이 켜거나 끄지 못하게 고정.
    if prev_data.get("_policy_brief") is True:
        new_data["_policy_brief"] = True
    else:
        new_data.pop("_policy_brief", None)


def _discard_model_failure_markers(new_data: dict) -> None:
    """재생성 JSON이 주입한 내부 실패 표식은 상태로 채택하지 않는다."""
    for key in ("_failed", "_failed_reason", "_qa_failed", "_qa_failed_reason",
                "_qa_failures"):
        new_data.pop(key, None)


def _inherit_image_plan(new_data: dict, prev_data: dict) -> None:
    """Retain current plans, user prompts and attached image assets through QA retries."""
    if not isinstance(new_data, dict) or not isinstance(prev_data, dict):
        return
    # These fields are trusted only from the pre-rewrite draft. The model response
    # cannot introduce file paths, slot assets, or preservation metadata.
    new_data.pop("_preserved_image_prompts", None)
    for key in ("image_slots", "image_paths"):
        if key in prev_data:
            new_data[key] = prev_data[key]
        else:
            new_data.pop(key, None)
    latest_plan = new_data.get("image_plan")
    previous_plan = prev_data.get("image_plan") or prev_data.get("_image_plan")
    preserved_plan = prev_data.get("_image_plan") or previous_plan
    if not isinstance(latest_plan, dict) or not latest_plan:
        latest_plan = previous_plan
    if isinstance(latest_plan, dict) and latest_plan:
        latest_plan = dict(latest_plan)
        previous_slots = {
            str(slot.get("id")): slot for slot in (preserved_plan or {}).get("slots", [])
            if isinstance(slot, dict) and slot.get("id")
        } if isinstance(preserved_plan, dict) else {}
        preserved_prompts = {
            slot_id: slot["prompt"] for slot_id, slot in previous_slots.items()
            if isinstance(slot.get("prompt"), str) and slot["prompt"].strip()
        }
        slots = latest_plan.get("slots")
        if isinstance(slots, list):
            preserved_slots = []
            for slot in slots:
                if not isinstance(slot, dict):
                    preserved_slots.append(slot)
                    continue
                slot = dict(slot)
                if str(slot.get("id")) in preserved_prompts:
                    slot["prompt"] = preserved_prompts[str(slot.get("id"))]
                preserved_slots.append(slot)
            latest_plan["slots"] = preserved_slots
        new_data["image_plan"] = latest_plan
        new_data["_image_plan"] = preserved_plan


def _inherit_regenerated_brief(new_data: dict, prev_data: dict, category: str) -> None:
    """Normalize a model-updated brief, or carry the original brief if omitted."""
    raw_brief = new_data.pop("editorial", None) or new_data.pop("editorial_brief", None)
    if isinstance(raw_brief, dict):
        try:
            from core.editorial_brief import normalize
            normalized = normalize(raw_brief, title=new_data.get("title") or prev_data.get("title") or "",
                                  category=category)
            if normalized:
                new_data["_editorial_brief"] = normalized
        except (ImportError, AttributeError, TypeError, ValueError) as exc:
            log.debug("재생성 브리프 정규화 실패, 원본을 승계합니다(%s)", type(exc).__name__)
    _inherit_editorial_brief(new_data, prev_data)


def validate_and_fix(post_data: dict, title: str, stop_flag=None,
                      *, enable_dia_aeo: bool | None = None,
                      dia_aeo_max_retries: int = 1) -> dict:
    """
    글을 검증하고 품질 미달 시 자동 재생성합니다.
    최대 rules['max_retries']회 재시도.

    post_data["category"]를 보고 카테고리별 룰을 적용하며,
    재생성 시에도 같은 카테고리의 모델을 일관되게 사용한다(_call_claude(category=...)).

    Args:
        enable_dia_aeo: True 면 기본 룰 PASS 후에도 D.I.A.+ / AEO 점수를 추가
            평가하고 70점 미달 항목이 있으면 한 번 더 보강 재작성 시도.
            None(기본) 이면 settings.DIA_AEO_AUTOFIX_BY_CATEGORY 를 따라
            카테고리별로 자동 결정 — 호출자가 신경 쓸 필요 없음.
            (2026-05-25 추가) 네이버 AI 요약 상단 노출 + 검색 가산 신호 강화 목적.
        dia_aeo_max_retries: D.I.A.+/AEO 보강 재작성 한도. 기본 1.
    """
    from core.content_generator import _call_claude, _parse_json_response

    category = post_data.get("category") or ""
    rules    = _apply_policy_brief_rules(_get_rules_for_category(category), post_data)

    # [설계도 주도 변주] 이 글의 블루프린트를 캡처한다. 재생성 프롬프트에는 골격/어법
    # 지시를 덧붙이고, 재생성 결과에는 _blueprint 를 다시 실어 검증 게이트가 같은 골격
    # 기대치로 판정하게 한다(없으면 None → 기존 동작 그대로·바이트 동일).
    blueprint = post_data.get("_blueprint")
    if not isinstance(blueprint, dict):
        blueprint = None
    # 재생성 프롬프트에 노출할 요구치 — 블루프린트 골격·편집 브리프 선언에 맞춰 FAQ/표/H2
    # 완화(생성과 정합). 둘 다 없으면 `_apply_blueprint_rules` 가 rules 를 그대로 돌려준다.
    regen_rules = _apply_blueprint_rules(rules, post_data)

    # enable_dia_aeo 가 None 이면 settings 의 카테고리별 토글로 자동 결정
    if enable_dia_aeo is None:
        enable_dia_aeo = is_dia_aeo_autofix_enabled(category)
    rule_tag = "디지털 완화" if _has_category_override(category) else "기본"

    content_before_len = len(post_data.get("content", ""))
    evidence_recovery_attempted = False

    def _mark_qa_failed_if_fail(pd: dict, reason: str) -> dict:
        """조기 반환 직전, 최종 검증이 FAIL이면 발행 차단 표식을 세운다.

        content_generator 하단 게이트가 _qa_failed 를 _failed 로 승격한다.
        PASS 상태로 조기 반환하는 경우(있다면)는 표식을 세우지 않는다.
        """
        try:
            still_passed, still_failures = validate(pd)
        except (TypeError, AttributeError, ValueError) as exc:
            # 검증 자체가 깨지면 안전하게 실패로 간주(발행 차단).
            log.warning(f"조기 반환 검증 중 예외 — QA 실패로 간주: {exc}")
            still_passed = False
            still_failures = [f"검증 예외: {exc}"]
        if not still_passed:
            pd["_qa_failed"] = True
            pd["_qa_failed_reason"] = reason
            # 최종 실패 사유 목록을 실어 보낸다 — auto_scheduler 가 needs_fix 보관 시
            # error 필드에 '어떤 수치가 문제였는지'를 남길 수 있게 한다(fact_gate 포함).
            pd["_qa_failures"] = list(still_failures or [])
            log.warning(f"⛔ QA 실패 표식 설정 — 사유: {reason}")
        return pd

    base_passed = False
    for attempt in range(rules["max_retries"] + 1):
        passed, failures = validate(post_data)
        if (post_data.get("_failed_reason") == "policy_no_evidence"
                and not any("source_evidence_missing" in item for item in failures)):
            failures.append(
                "source_evidence_missing: 기존 생성 단계에서 정책 공식 근거가 확인되지 않았습니다"
            )
            passed = False

        content_after_len = len(post_data.get("content", ""))
        if content_after_len < content_before_len:
            _, post_trim_failures = validate(post_data)
            for f in post_trim_failures:
                if "h2" in f.lower() or "heading" in f.lower() or "소제목" in f:
                    log.warning(f"트리밍 후 검증 실패: {f}")
            content_before_len = content_after_len

        if passed:
            if attempt > 0:
                log.info(f"품질 검증 통과 (재생성 {attempt}회 후, 룰: {rule_tag})")
            else:
                log.info(f"품질 검증 통과 (룰: {rule_tag} | "
                         f"{len(post_data.get('content',''))}자 | "
                         f"태그 {len(post_data.get('tags',[]))}개)")
            base_passed = True
            break

        log.warning(
            f"품질 미달 (시도 {attempt+1}/{rules['max_retries']+1}, 룰: {rule_tag}):"
        )
        for f in failures:
            log.warning(f"  • {f}")

        # 무근거 정책/YMYL 실패는 무웹 재생성을 반복해도 해결되지 않는다. 실제 검색
        # 도구가 붙는 전용 회복 호출을 글당 한 번만 실행하고, WebFetch 영수증으로
        # 출처를 검증한 뒤 일반 QA로 돌아간다.
        if (not evidence_recovery_attempted
                and _needs_evidence_recovery(post_data, failures)):
            evidence_recovery_attempted = True
            try:
                from core.content_generator import (
                    apply_web_read_evidence, get_last_web_read_urls,
                )
            except (ImportError, AttributeError) as exc:
                return _mark_evidence_recovery_failed(
                    post_data, f"검색 근거 검증기를 사용할 수 없음({type(exc).__name__})",
                )
            evidence_prompt = build_fix_prompt(
                title, post_data.get("content", ""), failures, regen_rules,
                category=category, has_research=True, blueprint=blueprint,
                editorial_brief=post_data.get("_editorial_brief") or
                                post_data.get("editorial_brief"),
                policy_brief=post_data.get("_policy_brief") is True,
            )
            try:
                evidence_raw = _call_claude(
                    evidence_prompt, max_retries=0, use_web_search=True,
                    stop_flag=stop_flag, category=category,
                )
                evidence_data = _parse_json_response(
                    evidence_raw, title, post_data.get("category", "default"),
                )
            except ValueError as exc:
                return _mark_evidence_recovery_failed(
                    post_data, f"검색 보강 응답을 해석하지 못함({type(exc).__name__})",
                )
            except (OSError, RuntimeError, TimeoutError) as exc:
                return _mark_evidence_recovery_failed(
                    post_data, f"검색 보강 호출 실패({type(exc).__name__})",
                )

            if not evidence_data.get("content"):
                return _mark_evidence_recovery_failed(post_data, "검색 보강 본문이 비어 있음")
            read_urls = get_last_web_read_urls()
            evidence_data.setdefault("category", category or post_data.get("category", "default"))
            _discard_model_failure_markers(evidence_data)
            _inherit_regenerated_brief(evidence_data, post_data, category)
            apply_web_read_evidence(
                evidence_data, read_urls, requested=True, previous=post_data,
            )
            if not _has_recovered_evidence(evidence_data):
                return _mark_evidence_recovery_failed(
                    post_data, "실제 읽은 URL에서 유효한 근거를 확인하지 못함",
                )
            if blueprint:
                evidence_data["_blueprint"] = blueprint
            _inherit_image_plan(evidence_data, post_data)
            _clear_recovered_evidence_failure(evidence_data)
            post_data = evidence_data
            log.info("실제 검색 근거 회복 성공 — 일반 QA를 계속합니다")
            passed, failures = validate(post_data)
            if passed:
                base_passed = True
                break
            log.warning("근거 회복 후에도 일반 QA 실패가 남아 재생성이 필요합니다")
            for failure in failures:
                log.warning("  • %s", failure)

        if attempt >= rules["max_retries"]:
            log.warning("재생성 한도 초과 — 현재 버전으로 진행")
            return _mark_qa_failed_if_fail(post_data, "retries_exceeded")

        log.info("재생성 요청 중...")
        prompt   = build_fix_prompt(title, post_data.get("content",""), failures, regen_rules,
                                    category=category,
                                    has_research=_REGEN_USE_WEB_SEARCH,
                                    blueprint=blueprint,
                                    editorial_brief=post_data.get("_editorial_brief") or
                                                    post_data.get("editorial_brief"),
                                    policy_brief=post_data.get("_policy_brief") is True)
        # use_web_search 를 명시로 넘긴다 — 프롬프트의 [판단 흔적] 분기와 실제 도구 부착이
        # 같은 상수를 보게 해 "도구 없이 확인·비교했다고 쓰라"는 날조 지시를 구조적으로 차단.
        raw = _call_claude(prompt, use_web_search=_REGEN_USE_WEB_SEARCH,
                           stop_flag=stop_flag, category=category)
        try:
            new_data = _parse_json_response(raw, title, post_data.get("category", "default"))
        except ValueError as e:
            log.warning(f"재생성 JSON 필수 키 누락 — 이전 버전 유지: {e}")
            return _mark_qa_failed_if_fail(post_data, "regen_json_parse_failed")

        if new_data.get("content"):
            new_data.setdefault("category", category or post_data.get("category", "default"))
            _discard_model_failure_markers(new_data)
            # ★ 재생성이 원본의 웹검색 근거(sources·web_search_done)를 버리지 않게 승계.
            #   (핵심 버그 수정: 회복 가능한 forbidden_words 실패가 재생성 중 근거 소실로
            #    '무근거 YMYL' 발행 차단으로 변질되던 문제. 원본에 실제 있던 값만 승계.)
            _inherit_web_evidence(new_data, post_data)
            # 재생성 결과가 같은 골격·구성 기대치로 검증되도록 블루프린트와 편집 브리프를 승계.
            if blueprint:
                new_data["_blueprint"] = blueprint
            _inherit_regenerated_brief(new_data, post_data, category)
            _inherit_image_plan(new_data, post_data)
            post_data = new_data
        else:
            log.error("재생성 응답 없음 — 이전 버전 유지")
            return _mark_qa_failed_if_fail(post_data, "regen_empty_response")

    # ── D.I.A.+ + AEO 보강 단계 (2026-05-25) ─────────────
    if not enable_dia_aeo or not base_passed:
        return post_data

    last_base_qa_passed_post = post_data
    for dia_attempt in range(dia_aeo_max_retries + 1):
        result = validate_with_dia_and_aeo(post_data)
        if not result.get("passed", False):
            base_failures = result.get("failures") or []
            log.warning(
                "D.I.A.+/AEO 보강본이 기본 QA에 실패 — 마지막 기본 QA 통과 본문 유지: %s",
                "; ".join(str(item) for item in base_failures[:5]) or "기본 QA 실패",
            )
            last_base_qa_passed_post["_dia_aeo_base_qa_rejected"] = True
            last_base_qa_passed_post["_dia_aeo_base_qa_failures"] = list(base_failures)
            return last_base_qa_passed_post
        # 새 보강본이 기본 QA를 통과할 때마다 스냅샷을 갱신한다. 다음 보강이 실패하면
        # 직전까지 실제로 통과한 본문을 반환한다.
        last_base_qa_passed_post = post_data
        dia = result.get("dia", {})
        aeo = result.get("aeo", {})
        dia_score_val = dia.get("score", 0)
        aeo_score_val = aeo.get("score", 0)
        weaknesses = result.get("combined_weaknesses", [])

        if dia.get("passed") and aeo.get("passed"):
            log.info(
                f"D.I.A.+ / AEO 보강 PASS — D.I.A.+ {dia_score_val:.0f}, "
                f"AEO {aeo_score_val:.0f} (재시도 {dia_attempt}회)"
            )
            post_data["_dia_score"] = dia_score_val
            post_data["_aeo_score"] = aeo_score_val
            return post_data

        if dia_attempt >= dia_aeo_max_retries:
            log.info(
                f"D.I.A.+ / AEO 보강 한도 초과 — D.I.A.+ {dia_score_val:.0f}, "
                f"AEO {aeo_score_val:.0f} (현재 버전 유지)"
            )
            post_data["_dia_score"] = dia_score_val
            post_data["_aeo_score"] = aeo_score_val
            return post_data

        log.warning(
            f"D.I.A.+ {dia_score_val:.0f}/{_dia_max_score()}, AEO {aeo_score_val:.0f}/100 "
            f"— 약점 {len(weaknesses)}건 보강 재작성 시도"
        )
        for w in weaknesses[:5]:
            log.warning(f"  • {w}")

        prompt = build_dia_aeo_fix_prompt(
            title, post_data.get("content", ""), weaknesses, regen_rules,
            has_research=_REGEN_USE_WEB_SEARCH, category=category,
            blueprint=blueprint,
            editorial_brief=post_data.get("_editorial_brief") or
                            post_data.get("editorial_brief"),
            policy_brief=post_data.get("_policy_brief") is True,
        )
        # 보강은 비차단(실패해도 발행 진행). 재시도로 300s×3=15분 낭비 방지 → 단일 시도.
        raw = _call_claude(prompt, max_retries=0, use_web_search=_REGEN_USE_WEB_SEARCH,
                           stop_flag=stop_flag, category=category)
        try:
            new_data = _parse_json_response(
                raw, title, post_data.get("category", "default"),
            )
        except ValueError as e:
            log.warning(f"D.I.A.+/AEO 보강 JSON 파싱 실패 — 이전 버전 유지: {e}")
            return post_data

        if new_data.get("content"):
            new_data.setdefault("category", category or post_data.get("category", "default"))
            _discard_model_failure_markers(new_data)
            # 보강 재생성도 같은 사실 본문의 경미 수정 — 원본 근거를 이어받아 fact_gate
            # 오차단을 막는다(원본에 실제 있던 sources·web_search_done 만 승계).
            _inherit_web_evidence(new_data, post_data)
            if blueprint:
                new_data["_blueprint"] = blueprint
            _inherit_regenerated_brief(new_data, post_data, category)
            _inherit_image_plan(new_data, post_data)
            post_data = new_data
        else:
            log.warning("D.I.A.+/AEO 보강 응답 없음 — 이전 버전 유지")
            return post_data

    return post_data
