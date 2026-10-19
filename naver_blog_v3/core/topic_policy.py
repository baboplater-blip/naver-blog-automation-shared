"""Shared distinction between government-support policy writing and other YMYL topics."""
from __future__ import annotations

import re
from typing import Any


_POLICY_TERMS = re.compile(
    r"정부.{0,8}(?:지원|정책|사업|혜택|보조금)|국가.{0,8}(?:지원|사업|보조금)|"
    r"지원금|보조금|바우처|복지급여|기초생활|기초연금|주거급여|아동수당|"
    r"청년수당|청년지원금|장려금|세액공제|정책자금|수급자격|수급 자격|"
    r"청년도약계좌|청년희망적금|청년내일저축계좌|내일채움공제|국민취업지원제도|국가장학금|"
    r"청년.{0,8}(?:월세지원|지원금|수당)|소상공인.{0,8}(?:지원|정책자금)|"
    r"복지.{0,8}(?:지원|급여|수당|제도)"
)
_POLICY_CATEGORIES = frozenset({"복지", "바우처", "주거", "청년", "취업"})
_DEFAULT_POLICY_ROLES = frozenset({"대한민국 정부지원 블로그 작가", "정부지원 정보 전문 작가"})
_DEFAULT_POLICY_SOURCES = "정부24·복지로·국세청"
_POLICY_SOURCE_GUIDANCE = f"{_DEFAULT_POLICY_SOURCES} (gov.kr / bokjiro.go.kr / nts.go.kr)"
_TOPIC_GUIDANCE = {
    "general": "독자가 실제로 묻는 핵심부터 답하고, 적용 조건·예외와 다음 판단을 구분한다.",
    "government_policy": "정책·지원 조건은 공식 원문에서 확인하고 대상·기한·신청 경로를 구분해 설명한다.",
    "it": "제품·버전·실행 환경을 밝혀 재현 조건과 예외를 설명한다. 확인하지 않은 테스트 결과는 쓰지 않는다.",
    "travel": "장소와 방문 시점, 이동 순서, 변동 가능한 운영·비용 정보를 구분한다. 실제 경험을 꾸며내지 않는다.",
    "review": "제공된 사용 근거로 장단점을 비교하고, 어떤 독자에게 맞는지 근거와 함께 밝힌다.",
    "learning": "학습자의 수준과 선행 조건을 밝히고, 따라 할 순서와 이해를 확인하는 방법을 안내한다.",
}


def is_government_policy(title: Any = "", category: Any = "") -> bool:
    """Return true only when topic wording signals a government program or benefit."""
    if selected_topic_mode(category) == "government_policy":
        return True
    title_text = str(title or "").strip()
    category_text = str(category or "").strip()
    combined = f"{title_text} {category_text}"
    if _POLICY_TERMS.search(combined):
        return True
    if category_text in {"복지", "바우처", "주거"} and not title_text:
        return True
    return category_text in _POLICY_CATEGORIES and bool(
        re.search(r"지원|자격|대상|급여|혜택|수급|수당|장려금|바우처|정책|사업|공제|보조금", title_text)
    )


def selected_topic_mode(category: Any = "") -> str:
    """Read the active profile's explicit topic mode; failure falls back to auto."""
    try:
        from config import brand
        category_genres = brand.category_genre()
        if isinstance(category_genres, dict) and category_genres.get(str(category or "")):
            return "auto"
        if brand.genre():
            return "auto"
    except (ImportError, AttributeError, OSError, ValueError, TypeError):
        pass
    try:
        from core.editorial_profile import load
        mode = str(load().get("topic_mode") or "auto")
        return mode if mode in {"auto", "general", "government_policy", "it", "travel",
                                "review", "learning"} else "auto"
    except (ImportError, AttributeError, OSError, ValueError, TypeError):
        return "auto"


def has_explicit_brand_genre(category: Any = "") -> bool:
    """Detect configured global/category genres so their existing priority remains intact."""
    try:
        from config import brand
        category_genres = brand.category_genre()
        return bool((isinstance(category_genres, dict)
                     and category_genres.get(str(category or ""))) or brand.genre())
    except (ImportError, AttributeError, OSError, ValueError, TypeError):
        return False


def topic_mode_for(title: Any = "", category: Any = "") -> str:
    """Resolve explicit topic mode first, then conservative title/category hints."""
    configured = selected_topic_mode(category)
    if configured != "auto":
        return configured
    if is_government_policy(title, category):
        return "government_policy"
    combined = f"{title or ''} {category or ''}".casefold()
    hints = (
        ("it", r"\b(?:it|api|software|windows|linux|macos|컴퓨터|개발|코딩|프로그램|앱|오류|버전)\b"),
        ("travel", r"\b(?:여행|관광|숙소|호텔|맛집|카페|공항|여행지|교통|동선|방문)\b"),
        ("review", r"\b(?:후기|리뷰|사용기|비교|추천|장단점|구매|써본)\b"),
        ("learning", r"\b(?:공부|학습|강의|수업|시험|자격증|배우기|연습|교육)\b"),
    )
    for mode, pattern in hints:
        if re.search(pattern, combined, flags=re.IGNORECASE):
            return mode
    return "general"


def topic_guidance(mode: str = "", *, title: Any = "", category: Any = "") -> str:
    """Return a short topic-family instruction for initial writing and QA rewrites."""
    selected = mode or topic_mode_for(title, category)
    guidance = _TOPIC_GUIDANCE.get(selected, "")
    return f"[계정 주제 분야]\n- {guidance}" if guidance else ""


def writer_role(title: Any = "", category: Any = "", configured: Any = "") -> str:
    """Choose a neutral role for general topics while preserving configured brand roles."""
    role = str(configured or "").strip()
    if role and role not in _DEFAULT_POLICY_ROLES:
        return role
    if is_government_policy(title, category):
        return role or "대한민국 정부지원 블로그 작가"
    return "주제를 정확히 설명하는 네이버 블로그 작가"


def source_guidance(title: Any = "", category: Any = "", configured: Any = "") -> str:
    """Return topic-matched source guidance without imposing government portals."""
    sources = str(configured or "").strip()
    if is_government_policy(title, category) or selected_topic_mode(category) == "government_policy":
        if sources and sources not in {_DEFAULT_POLICY_SOURCES, _POLICY_SOURCE_GUIDANCE}:
            return f"{_POLICY_SOURCE_GUIDANCE} 및 {sources}"
        return _POLICY_SOURCE_GUIDANCE
    if sources and sources not in {_DEFAULT_POLICY_SOURCES, _POLICY_SOURCE_GUIDANCE}:
        return sources
    mode = selected_topic_mode(category)
    if mode == "it":
        return "제품·플랫폼의 공식 기술 문서·지원 문서"
    if mode == "travel":
        return "지역·교통·시설 운영기관의 공식 안내"
    if mode == "review":
        return "제조사 공식 사양과 실제 사용 근거"
    if mode == "learning":
        return "교육기관·시험 주관기관의 공식 자료"
    return "해당 주제의 공식 자료·전문기관·원문 자료"


def initial_answer_axes(title: Any = "", category: Any = "") -> str:
    """Describe topic-specific answer dimensions without inventing facts or costs."""
    mode = topic_mode_for(title, category)
    if mode == "government_policy":
        # Preserve the established policy-summary contract. The adjacent topic QA
        # checklist separately requires source-backed exclusions and application
        # routes; this axis names the reader-facing summary dimensions only.
        return "누가(대상) / 얼마(금액·규모) / 언제·어떻게(기한·신청 방법)"
    if mode == "it":
        return "적용 제품·버전·환경 / 재현 또는 실행 순서 / 예외·실패 조건"
    if mode == "travel":
        return "장소·방문 시점 / 이동 동선 / 확인된 운영시간·비용과 변동 조건"
    if mode == "review":
        return "실제 확인된 사용 근거 / 비교 기준과 장단점 / 맞는 독자와 맞지 않는 상황"
    if mode == "learning":
        return "학습 목표와 수준 / 필요한 선행 조건 / 실행 순서와 이해 확인법"
    return "독자가 궁금해하는 핵심 답 / 적용 조건·상황 / 다음 판단이나 행동"


__all__ = ["is_government_policy", "writer_role", "source_guidance", "initial_answer_axes",
           "selected_topic_mode", "topic_mode_for", "topic_guidance",
           "has_explicit_brand_genre"]
