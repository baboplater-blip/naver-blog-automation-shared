# core/fact_gate.py
# 사실 정확성 게이트 (결정적·오프라인·규칙 기반, Claude 미호출)
# ─────────────────────────────────────────────────────
# 레드팀이 찾은 두 부류의 사고를 발행 전에 차단한다:
#   ① 무근거 YMYL 발행 — 금액·자격·일정 같은 핵심 수치 클레임이 있는데
#      웹검색이 실제로 수행되지 않았거나 인용 출처가 0인 경우.
#   ② 허구 수치 — 지어낸 환산·모의계산("약 3억 → 월 516만원 환산" 등)과
#      같은 글 안에서 정면 모순되는 수치(같은 라벨에 크게 다른 두 금액).
#
# 이 모듈은 순수 함수 위주로, Claude 를 호출하지 않고 정규식/규칙만으로
# 판정한다(결정적·오프라인). 판정 결과는 quality_validator.validate() 의
# 기존 PASS/FAIL 파이프라인과 동일한 (passed, failures) 형태로 합류한다.
from __future__ import annotations

import os
import re
import sys
from typing import Iterable, List, Optional, Tuple

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────
# YMYL(Your Money or Your Life) 카테고리 판별
# ─────────────────────────────────────────────────────
# 금액·자격·일정이 사람의 재정/건강에 직접 영향을 주는 카테고리.
# content_generator 의 정부지원·복지·연금·금융 계열과 일치시킨다.
# settings.FACT_GATE_YMYL_CATEGORIES 로 재정의 가능(없으면 이 리터럴 사용).
_DEFAULT_YMYL_CATEGORIES = frozenset({
    "청년", "복지", "교육", "취업", "주거", "의료",
    "바우처", "부동산", "절세", "금융", "노후",
})


def _ymyl_categories() -> frozenset:
    """YMYL 카테고리 집합을 반환한다(settings 오버라이드 우선).

    settings.FACT_GATE_YMYL_CATEGORIES 가 iterable 이면 그 문자열들을 집합으로,
    아니면 기본 리터럴을 반환한다. 설정 로드 실패는 조용히 기본값으로 폴백한다.
    """
    try:
        from config import settings as _s
        override = getattr(_s, "FACT_GATE_YMYL_CATEGORIES", None)
    except (ImportError, AttributeError) as exc:
        log.debug(f"YMYL 카테고리 설정 로드 스킵: {exc}")
        override = None
    if override:
        try:
            cats = {str(c).strip() for c in override if str(c).strip()}
            if cats:
                return frozenset(cats)
        except TypeError:
            log.debug("FACT_GATE_YMYL_CATEGORIES 가 iterable 이 아님 — 기본값 사용")
    return _DEFAULT_YMYL_CATEGORIES


def is_ymyl_category(category: Optional[str]) -> bool:
    """카테고리가 YMYL(재정·건강 직결)인지 여부."""
    if not category:
        return False
    return category.strip() in _ymyl_categories()


# ─────────────────────────────────────────────────────
# 핵심 수치 클레임 탐지
# ─────────────────────────────────────────────────────
# 금액: "295,200원", "70만원", "1억", "월 30만 원" 등.
# 숫자 뒤에 통화 단위(원/만원/억)가 붙는 형태만 잡는다(전화번호·조문번호 오탐 방지).
_MONEY_RE = re.compile(r'\d[\d,]*\s*(?:억\s*)?(?:만\s*)?원')
_MONEY_EOK_RE = re.compile(r'\d[\d,]*\s*억')

# 자격 연령/소득 기준: "만 65세", "65세 이상", "기준 중위소득 50%" 등.
# 소득 관련 라벨만 적힌 안내는 수치 클레임이 아니며, 실제 % 수치가 붙을 때만 잡는다.
_AGE_RE = re.compile(r'(?:만\s*)?\d{1,3}\s*세\s*(?:이상|이하|미만|초과)?')
_INCOME_STD_RE = re.compile(
    r'(?:기준\s*중위소득|중위소득|소득인정액|소득\s*기준|재산\s*기준)'
    r'[^\n\d]{0,12}\d[\d,]*(?:\.\d+)?\s*%'
)

# 신청 기간/날짜: "2026년 3월 15일", "3월 1일부터", "~ 12월 31일까지",
# "신청기간", "접수기간", "2026.03.15" 등.
_DATE_RE = re.compile(
    r'\d{4}\s*[.\-년]\s*\d{1,2}\s*[.\-월]\s*\d{1,2}\s*일?'
    r'|\d{4}\s*년\s*\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?'
    r'|\d{1,2}\s*월\s*\d{1,2}\s*일'
    r'|\d{1,2}\s*월(?:\s*\d{1,2}\s*일)?\s*(?:부터|까지|이후|이전)'
    r'|(?:신청|접수|모집|공고)\s*기간[^\n\d]{0,12}'
    r'(?:\d{4}\s*년\s*)?\d{1,2}\s*월'
)


def has_numeric_claim(content: str) -> bool:
    """본문에 핵심 수치 클레임(금액·연령/소득 기준·신청 날짜)이 있는지 여부.

    하나라도 있으면 True. YMYL 카테고리에서 이 값이 True 인데 근거가 없으면
    발행 게이트가 FAIL 한다.
    """
    if not content:
        return False
    if _MONEY_RE.search(content) or _MONEY_EOK_RE.search(content):
        return True
    if _AGE_RE.search(content):
        return True
    if _INCOME_STD_RE.search(content):
        return True
    if _DATE_RE.search(content):
        return True
    return False


# ─────────────────────────────────────────────────────
# 허구 수치(지어낸 환산·모의계산) 탐지
# ─────────────────────────────────────────────────────
# 레드팀 증거: "3억 아파트 → 월 516만원 환산"(실제 100만원대의 약 5배).
# 근거 문장 없이 등장하는 구체적 환산·백분율은 대개 모델이 지어낸 값이다.
# WHY 정규식만: 오프라인·결정적이어야 하고(Claude 재호출 시 예산·비결정성 문제),
#              레드팀이 실제로 목격한 패턴은 문구 형태가 일정하기 때문.
_FABRICATION_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # "약 3억을 월 516만원으로 환산", "3억 → 월 516만원 환산"
    (
        "환산_금액",
        re.compile(r'(?:약\s*)?\d[\d,]*\s*(?:억|만\s*원|원)[^\n]{0,20}?환산'),
    ),
    # "월 516만원으로 환산", "월소득 516만원으로 환산됩니다"
    (
        "월환산",
        re.compile(r'월\s*(?:소득\s*)?\d[\d,]*\s*만?\s*원(?:으?로)?\s*환산'),
    ),
    # "약 29.5만~70만 원으로 환산" — 범위형 환산액
    (
        "범위환산",
        re.compile(r'(?:약\s*)?\d[\d.,]*\s*~\s*\d[\d.,]*\s*만?\s*원(?:으?로)?\s*환산'),
    ),
    # "최대 약 30~50%", "약 15~25%로" — 근거 없이 던지는 백분율 범위
    (
        "백분율범위",
        re.compile(r'(?:최대의?\s*)?약\s*\d[\d.]*\s*~\s*\d[\d.]*\s*%'),
    ),
    # "약 5배", "약 3.2배 수준" — 근거 없는 배수 추정
    (
        "배수추정",
        re.compile(r'약\s*\d[\d.]*\s*배'),
    ),
    # "예상 수령액을 계산하면", "모의계산 결과 월 ...원" — 지어낸 계산 서술
    (
        "모의계산",
        re.compile(r'(?:모의\s*계산|예상\s*수령액을?\s*계산)[^\n]{0,30}?\d[\d,]*\s*만?\s*원'),
    ),
)

# 환산·모의계산 패턴이라도, 바로 근처(같은 문장/직후)에서 1차 출처를 명시하면
# 정당한 근거 있는 계산일 수 있으므로 오탐을 줄인다.
_SOURCE_NEARBY_RE = re.compile(
    r'(?:출처|근거|자료|기준)\s*[:：]|정부24|복지로|국세청|국민연금공단|'
    r'보건복지부|국토교통부|https?://'
)


def _line_has_source(line: str) -> bool:
    """한 줄(또는 문장)에 1차 출처·근거 표기가 있는지."""
    return bool(_SOURCE_NEARBY_RE.search(line))


def detect_fabricated_figures(content: str) -> List[str]:
    """지어낸 환산·모의계산·근거 없는 백분율/배수를 탐지해 사유 목록을 반환한다.

    각 사유는 사람이 읽을 수 있는 문자열: "허구수치(환산_금액): '<탐지 문구>'".
    근처에 1차 출처가 명시된 줄은 제외해 오탐을 줄인다.
    반환 목록이 비어 있으면 문제 없음.
    """
    if not content:
        return []
    reasons: List[str] = []
    seen: set = set()
    for label, pattern in _FABRICATION_PATTERNS:
        for match in pattern.finditer(content):
            snippet = match.group(0).strip()
            # 매칭 지점이 속한 줄을 찾아 출처 근접 여부 확인.
            line = _enclosing_line(content, match.start())
            if _line_has_source(line):
                continue
            key = (label, snippet)
            if key in seen:
                continue
            seen.add(key)
            reasons.append(f"허구수치({label}): '{_truncate(snippet, 40)}'")
    return reasons


def _enclosing_line(content: str, pos: int) -> str:
    """content[pos] 가 속한 한 줄을 반환한다(개행 기준)."""
    start = content.rfind("\n", 0, pos) + 1
    end = content.find("\n", pos)
    if end == -1:
        end = len(content)
    return content[start:end]


def _truncate(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ─────────────────────────────────────────────────────
# 같은 글 내 수치 모순 탐지
# ─────────────────────────────────────────────────────
# 레드팀 증거: 같은 제도 두 글이 "1인 29.5만~70만" vs "295,200원" 으로 모순.
# 한 글 안에서 같은 라벨(예: "1인 가구")에 크게 다른 두 금액이 병존하면 탐지.
#
# 접근: (라벨 토큰 + 금액) 쌍을 수집해, 같은 라벨에 붙은 금액들이
# 최대/최소 비율이 임계(_CONTRADICTION_RATIO)를 넘으면 모순으로 본다.
# 단순 "만원 단위"와 "원 단위"의 표기 차이는 정규화해서 오탐을 막는다.
_CONTRADICTION_RATIO = 2.5   # 최대금액/최소금액 배수 임계(2.5배 이상 = 모순 의심)
_LABEL_TOKENS: Tuple[str, ...] = (
    "1인", "2인", "3인", "4인", "5인",
    "1인 가구", "2인 가구", "3인 가구", "4인 가구",
    "단독", "부부", "월 최대", "연 최대", "최대 지원",
    "기초연금", "생계급여", "주거급여", "청년월세",
)

# "금액 + (원|만원|억)" 을 원 단위 정수로 정규화하기 위한 파서.
_AMOUNT_TOKEN_RE = re.compile(
    r'(\d[\d,]*(?:\.\d+)?)\s*(억\s*원|억|만\s*원|만|원)'
)


def _to_won(number_text: str, unit: str) -> Optional[int]:
    """'29.5' + '만원' → 295000(원). 파싱 실패 시 None."""
    try:
        value = float(number_text.replace(",", ""))
    except ValueError:
        return None
    unit = unit.replace(" ", "")
    if unit in ("억원", "억"):
        return int(value * 100_000_000)
    if unit in ("만원", "만"):
        return int(value * 10_000)
    if unit == "원":
        return int(value)
    return None


def _most_specific_label(line: str) -> Optional[str]:
    """한 줄에서 가장 구체적인 라벨 토큰 하나만 고른다.

    "1인 가구" 와 "1인" 이 동시에 매칭되면 더 긴(구체적인) "1인 가구" 만 채택해
    같은 금액이 두 라벨에 중복 집계되는 것을 막는다. 매칭 없으면 None.
    """
    matched = [tok for tok in _LABEL_TOKENS if tok in line]
    if not matched:
        return None
    # 가장 긴(구체적인) 라벨 하나만 대표로 쓴다 — 줄 단위 결합의 단순화(오탐 억제).
    return max(matched, key=len)


def detect_numeric_contradictions(content: str) -> List[str]:
    """같은 라벨에 크게 다른 두 금액이 병존하는 글 내 모순을 탐지한다.

    각 라벨이 등장하는 '줄'에서 금액을 원 단위로 정규화해, 라벨별로 (줄 인덱스,
    금액) 쌍을 모은다. 같은 라벨의 최소·최대 금액 비율이 _CONTRADICTION_RATIO
    이상이고 그 두 극단값이 **서로 다른 줄**에서 나왔을 때만 모순으로 본다.
    (같은 줄의 "29.5만~70만원" 같은 명시적 범위는 의도된 표기라 모순이 아님.)
    반환 목록이 비어 있으면 모순 없음.

    한계:
      - 라벨-금액 결합은 '같은 줄' 휴리스틱이라, 표의 다른 셀·문단을 건너뛴
        결합은 잡지 못한다(오탐보다 미탐을 택함).
      - 서로 '다른 글' 사이의 모순(레드팀의 핵심 사례)은 단일 글 게이트가
        구조적으로 볼 수 없다 — 그건 중복/일관성 크로스체크의 몫.
    """
    if not content:
        return []
    # label -> list of (line_index, won)
    label_points: dict = {}
    for line_idx, line in enumerate(content.splitlines()):
        label = _most_specific_label(line)
        if not label:
            continue
        for num, unit in _AMOUNT_TOKEN_RE.findall(line):
            won = _to_won(num, unit)
            # 0원·비현실적 소액(1000원 미만)은 라벨 금액이 아닐 확률이 높아 제외.
            if won and won >= 1_000:
                label_points.setdefault(label, []).append((line_idx, won))

    reasons: List[str] = []
    for label, points in label_points.items():
        if len(points) < 2:
            continue
        low_idx, low = min(points, key=lambda p: p[1])
        high_idx, high = max(points, key=lambda p: p[1])
        if low <= 0 or high / low < _CONTRADICTION_RATIO:
            continue
        # 두 극단값이 같은 줄이면 명시적 범위(의도된 표기)로 보고 제외.
        if low_idx == high_idx:
            continue
        reasons.append(
            f"수치모순('{label}'): {_won_fmt(low)} vs {_won_fmt(high)} "
            f"(약 {high / low:.1f}배 차이)"
        )
    return reasons


def _won_fmt(won: int) -> str:
    """원 단위 정수를 사람이 읽기 좋은 문자열로."""
    if won >= 100_000_000:
        return f"{won / 100_000_000:.1f}억원"
    if won >= 10_000:
        return f"{won // 10_000:,}만원"
    return f"{won:,}원"


# ─────────────────────────────────────────────────────
# 근거(출처) 개수 산정
# ─────────────────────────────────────────────────────
def count_sources(sources: Optional[Iterable]) -> int:
    """인용 출처(URL/근거) 개수를 센다(빈 문자열·None 제외)."""
    if not sources:
        return 0
    if isinstance(sources, str):
        return 1 if sources.strip() else 0
    try:
        return sum(1 for s in sources if s and str(s).strip())
    except TypeError:
        return 0


# ─────────────────────────────────────────────────────
# 통합 게이트
# ─────────────────────────────────────────────────────
def check(
    content: str,
    category: Optional[str],
    web_search_done: bool = False,
    sources: Optional[Iterable] = None,
) -> Tuple[bool, List[str]]:
    """사실 정확성 게이트. quality_validator.validate() 와 동일한 반환 형태.

    Args:
        content: 검사할 본문(마크다운 또는 텍스트).
        category: 카테고리 문자열. YMYL 여부 판정에 사용.
        web_search_done: 이 글 생성에 웹검색이 실제로 수행됐는지 여부.
        sources: 인용 출처(URL/근거) 목록. YMYL 근거 판정에 사용.

    Returns:
        (passed, failures):
            passed  — 게이트 통과 여부(True 면 발행 가능).
            failures — 실패 사유 문자열 목록(호출자가 재생성 트리거·발행 보류에 사용).

    규칙:
        1) 허구 수치·수치 모순은 **카테고리 무관** 항상 FAIL(지어낸 값은 어디서든 해악).
        2) YMYL 카테고리에서 핵심 수치 클레임이 있는데 웹검색 미수행 또는
           출처 0 이면 FAIL(무근거 발행 차단).
        3) 비YMYL 이거나 수치 클레임이 없으면 근거 요건은 적용하지 않는다.
    """
    failures: List[str] = []
    text = content or ""

    # 1) 허구 수치 — 카테고리 무관 항상 검사(지어낸 값은 어디서든 차단).
    failures.extend(detect_fabricated_figures(text))

    # 2) 같은 글 내 수치 모순 — 카테고리 무관.
    failures.extend(detect_numeric_contradictions(text))

    # 3) YMYL 무근거 발행 차단.
    if is_ymyl_category(category) and has_numeric_claim(text):
        n_sources = count_sources(sources)
        if not web_search_done and n_sources == 0:
            failures.append(
                "무근거_YMYL: 금액·자격·일정 수치 클레임이 있으나 "
                "웹검색 미수행 + 인용 출처 0 (발행 보류)"
            )
        elif not web_search_done and n_sources > 0:
            # 출처는 있으나 검색이 안 돌았음 — 약한 경고만(발행은 허용).
            log.warning(
                f"[fact_gate] YMYL 수치 클레임에 웹검색 미수행이나 출처 {n_sources}건 존재 — 통과"
            )
        elif web_search_done and n_sources == 0:
            failures.append(
                "무근거_YMYL: 웹검색은 수행됐으나 본문에 인용 출처가 0건 "
                "(근거 인용 필요)"
            )

    passed = len(failures) == 0
    if not passed:
        log.warning(
            f"[fact_gate] FAIL (category={category!r}, "
            f"web_search_done={web_search_done}, sources={count_sources(sources)}): "
            + " | ".join(failures[:6])
        )
    return passed, failures
