# core/institution_dedup.py
# 제도-단위 중복 게이트 — "사촌 글 자기잠식" 차단 (개선안 #3)
#
# 배경 (레드팀 증거):
#   발행 사전차단(auto_scheduler)이 check_duplicate(title) 로 "제목만" 비교하고,
#   토큰화가 조사만 떼는 수준이라 Jaccard 0.80 임계에 사실상 도달하지 못했다.
#   결과: "2026 에너지바우처 vs 난방비…", "에너지바우처 신청방법 총정리" 처럼
#   같은 제도를 조금씩 다른 제목으로 감싼 사촌 글이 전부 통과 → 에너지바우처 ~7편,
#   긴급생계지원 ~6편이 서로의 검색 유입을 갉아먹는(자기잠식) 상태가 됐다.
#
# 이 모듈이 하는 일 (순수 함수 위주·결정적·부수효과 0):
#   1. normalize_institution(): 제목/키워드에서 핵심 "제도명"을 추출·정규화한다.
#      (사전 매칭 우선 → 실패 시 공백/연도/수식어 제거 휴리스틱)
#   2. count_recent(): 최근 N일 안에 같은 정규화 제도명으로 발행/예약된 편수를 센다.
#   3. evaluate_gate(): 그 편수가 임계(기본 3)를 초과하면 "신규 발행 차단 + 기존
#      최약 글 리프레시 대상 표기"를 판정해 돌려준다. **폐기가 아니라 리프레시**다.
#
# 실제 발행·리프레시·병합은 기존 트리거(auto_scheduler·post_refresher·topic_cluster)
# 가 수행한다. 이 모듈은 판정과 대상 선정·브리프 생성까지만 담당한다.
from __future__ import annotations

import os
import re
import sys
from datetime import datetime, timedelta
from typing import Iterable, List, Optional, Tuple

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.logger import get_logger  # noqa: E402

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# 설정 (settings 우선, 없으면 기본값) — 임계·윈도우는 운영 중 조정 가능해야 한다.
# ─────────────────────────────────────────────────────────────────────────────
try:
    from config.settings import (  # type: ignore
        INSTITUTION_DEDUP_MAX_PER_WINDOW as _MAX_PER_WINDOW,
        INSTITUTION_DEDUP_WINDOW_DAYS as _WINDOW_DAYS,
        INSTITUTION_BODY_SIM_THRESHOLD as _BODY_SIM_THRESHOLD,
    )
except (ImportError, AttributeError):
    _MAX_PER_WINDOW = 3        # 같은 제도 30일 내 허용 편수(초과 시 신규 차단)
    _WINDOW_DAYS = 30          # 카운트 윈도우(일)
    _BODY_SIM_THRESHOLD = 0.55  # 골격어 제외 후 본문 Jaccard 차단 임계

# 발행 히스토리의 posted_at 저장 포맷(duplicate_checker.record_post 와 동일).
_POSTED_AT_FMT = "%Y-%m-%d %H:%M"

# ─────────────────────────────────────────────────────────────────────────────
# 1) 제도명 사전 — 잘 알려진 고검색 제도. draft_triage._CORE_TOPICS 와 목적이
#    같지만, 여기서는 "정규화 대표어"로 쓰이므로 표기 흔들림(에너지 바우처 ↔
#    에너지바우처)을 흡수하도록 공백 없는 형태로 매칭한다. 사전에 없는 제도도
#    휴리스틱으로 정규화되므로, 사전은 "표기 통일 앵커"일 뿐 완결 목록이 아니다.
# ─────────────────────────────────────────────────────────────────────────────
_INSTITUTION_LEXICON: Tuple[str, ...] = (
    # 에너지·주거·생계
    "에너지바우처", "긴급생계지원", "긴급복지지원", "긴급복지", "생계급여",
    "주거급여", "교육급여", "의료급여", "기초연금", "기초생활수급", "차상위계층",
    "한부모가족지원", "청년월세지원", "청년월세", "월세지원", "전세자금대출",
    "버팀목전세자금", "디딤돌대출", "보금자리론",
    # 금융·자산형성
    "청년도약계좌", "청년내일저축계좌", "내일채움공제", "근로장려금", "자녀장려금",
    "햇살론", "국민취업지원제도", "실업급여", "구직급여",
    # 육아·연금·복지
    "부모급여", "아동수당", "육아휴직급여", "출산지원금", "첫만남이용권",
    "국민연금", "노령연금", "장애인연금", "장애수당",
    # 바우처·이용권
    "문화누리카드", "농식품바우처", "기저귀바우처", "산모신생아건강관리",
)
# 길이 내림차순 — "긴급복지지원"이 "긴급복지"보다 먼저 매칭돼야 더 구체적 제도를 잡는다.
_LEXICON_BY_LEN: Tuple[str, ...] = tuple(
    sorted(_INSTITUTION_LEXICON, key=len, reverse=True)
)

# ─────────────────────────────────────────────────────────────────────────────
# 2) 정규화 휴리스틱용 토큰 — 사전 미매칭 제목에서 제도명 앞뒤에 붙는 군더더기.
# ─────────────────────────────────────────────────────────────────────────────
# 연도·수식 접두(제목 맨 앞): "2026", "2026년", "최신"
_YEAR_PREFIX_RE = re.compile(r"^(?:20\d{2}년?|최신|올해|신규)\s*")
# 제목 뒤쪽 흔한 동작·형식어(정규화 시 잘라낸다) — 제도명 자체는 아니다.
_TAIL_MODIFIERS: Tuple[str, ...] = (
    "신청방법", "신청자격", "신청기간", "신청", "자격", "조건", "대상", "금액",
    "지급일", "지급", "방법", "총정리", "완벽정리", "정리", "안내", "가이드",
    "혜택", "비교", "차이", "후기", "얼마", "언제", "서류", "기준", "한도",
)
# 정규화 결과에서 항상 제거하는 문자(공백·구분기호·조사 흔적).
_NON_WORD_RE = re.compile(r"[^0-9A-Za-z가-힣]+")

# 제도명 최소 길이 — 이보다 짧으면 신뢰할 수 없는 정규화로 보고 빈 문자열 반환.
_MIN_INSTITUTION_LEN = 2

# ─────────────────────────────────────────────────────────────────────────────
# 3) 본문 유사도용 골격어(불용어) — 어느 제도 글에나 공통으로 등장해 유사도를
#    부풀리는 뼈대 단어. 이걸 빼야 "같은 제도의 다른 각도" 글끼리도 본문 신호가
#    변별력을 갖는다. duplicate_checker 의 조사 제거와 상호 보완.
# ─────────────────────────────────────────────────────────────────────────────
_SKELETON_STOPWORDS: frozenset = frozenset({
    "신청", "자격", "금액", "방법", "대상", "지원", "조건", "기준", "서류",
    "혜택", "안내", "정리", "총정리", "확인", "발급", "접수", "지급", "기간",
    "한도", "소득", "가구", "온라인", "방문", "정부", "지원금", "제도", "경우",
    "필요", "가능", "이용", "준비", "관련", "이상", "이하", "문의", "홈페이지",
})


def normalize_institution(text: str) -> str:
    """제목/키워드에서 핵심 제도명을 뽑아 정규화한 대표어를 반환한다.

    Args:
        text: 원본 제목 또는 키워드. 앞에 붙은 'YYYY-MM-DD — ' 접두는 제거한다.

    Returns:
        정규화된 제도명(공백 제거). 신뢰할 수 없으면 빈 문자열.

    전략:
        1. 사전(_INSTITUTION_LEXICON) 매칭 — 공백을 지운 텍스트에 포함되면
           가장 긴 사전어를 대표어로 반환(표기 흔들림 흡수). 이게 1순위.
        2. 사전 미매칭이면 휴리스틱 — 날짜·연도 접두, 비교 기호(vs), 꼬리
           동작·형식어를 제거하고 남은 앞머리 토큰을 제도명 후보로 삼는다.
    """
    if not text:
        return ""

    # 'YYYY-MM-DD — 제목' 접두 제거(히스토리 title 포맷 호환).
    cleaned = re.sub(r"^\d{4}-\d{2}-\d{2}\s*[—\-]\s*", "", str(text)).strip()
    if not cleaned:
        return ""

    # 1) 사전 매칭(공백 무시) — 가장 구체적인(긴) 제도명 우선.
    compact = _NON_WORD_RE.sub("", cleaned)
    for inst in _LEXICON_BY_LEN:
        if inst in compact:
            return inst

    # 2) 휴리스틱 정규화.
    #   a. "vs"/"비교" 앞부분만 사용 — "A vs B" 는 A 제도 글로 본다.
    head = re.split(r"\s+vs\.?\s+|\s+대\s+|\s+비교", cleaned, maxsplit=1)[0]
    #   b. 연도·수식 접두 제거.
    head = _YEAR_PREFIX_RE.sub("", head).strip()
    #   c. 꼬리 동작·형식어 제거(뒤에서부터, 붙어 있든 띄어 있든).
    compact_head = _NON_WORD_RE.sub("", head)
    changed = True
    while changed and compact_head:
        changed = False
        for mod in _TAIL_MODIFIERS:
            if compact_head.endswith(mod) and len(compact_head) - len(mod) >= _MIN_INSTITUTION_LEN:
                compact_head = compact_head[: -len(mod)]
                changed = True
                break

    if len(compact_head) < _MIN_INSTITUTION_LEN:
        return ""
    # 남은 앞머리가 제도명이 아니라 골격어/형식어 자체면(예: '신청 방법 총정리' →
    # '신청') 신뢰할 수 없는 정규화다 → 빈 문자열로 오탐을 막는다(게이트 fail-open).
    if compact_head in _SKELETON_STOPWORDS or compact_head in _TAIL_MODIFIERS:
        return ""
    return compact_head


# ─────────────────────────────────────────────────────────────────────────────
# 히스토리 로드 — duplicate_checker 의 post_history.json 을 진실 원본으로 재사용.
# ─────────────────────────────────────────────────────────────────────────────
def _load_history() -> List[dict]:
    """post_history.json 레코드 리스트를 로드(실패 시 빈 리스트)."""
    try:
        from core import duplicate_checker as dc
        from config import safe_load_json
        dc.DB_PATH.parent.mkdir(exist_ok=True)
        records = safe_load_json(dc.DB_PATH, default=list)
        return records if isinstance(records, list) else []
    except (ImportError, OSError, ValueError) as exc:
        log.warning(f"[institution_dedup] 히스토리 로드 실패 — 게이트 무력화: {exc}")
        return []


def _parse_posted_at(value: str) -> Optional[datetime]:
    """레코드의 posted_at('YYYY-MM-DD HH:MM')을 datetime 으로. 실패 시 None."""
    if not value:
        return None
    raw = str(value).strip()
    # 초까지 붙은 변형도 관대하게 수용.
    for fmt in (_POSTED_AT_FMT, "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw[: len(datetime.now().strftime(fmt))], fmt)
        except ValueError:
            continue
    # ISO 포맷 폴백.
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def count_recent(
    institution: str,
    *,
    records: Optional[Iterable[dict]] = None,
    now: Optional[datetime] = None,
    window_days: int = _WINDOW_DAYS,
) -> int:
    """최근 window_days 안에 같은 정규화 제도명으로 발행된 편수를 센다.

    Args:
        institution: 이미 정규화된 제도명(normalize_institution 결과).
        records:     히스토리 레코드 iterable. 생략 시 post_history.json 로드.
                     (예약 대장 등 외부 소스를 합쳐 넘기고 싶을 때 주입 가능.)
        now:         기준 시각(테스트 결정성). 생략 시 datetime.now().
        window_days: 카운트 윈도우(일).

    Returns:
        윈도우 내 매칭 편수(≥0). institution 이 비면 0.

    Note:
        posted_at 파싱이 실패한 레코드는 **윈도우 내로 포함**한다(보수적).
        오래된 글의 시각이 유실됐다고 게이트를 뚫리게 하는 것보다, 있는 것으로
        간주해 과잉 발행을 막는 편이 자기잠식 방지 목적에 맞다.
    """
    if not institution:
        return 0
    if records is None:
        records = _load_history()
    ref = now or datetime.now()
    cutoff = ref - timedelta(days=window_days)

    count = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rec_title = rec.get("clean_title") or rec.get("title") or ""
        rec_inst = normalize_institution(rec_title)
        if rec_inst != institution:
            continue
        posted = _parse_posted_at(rec.get("posted_at", ""))
        if posted is None or posted >= cutoff:
            count += 1
    return count


# ─────────────────────────────────────────────────────────────────────────────
# 본문 유사도(골격어 제외) — 사전차단 경로에서 본문까지 비교할 때 쓰는 보조.
# duplicate_checker._keywords 를 재사용하되 골격어를 뺀 뒤 Jaccard.
# ─────────────────────────────────────────────────────────────────────────────
def _content_keywords(text: str) -> set:
    """본문에서 골격어(불용어)를 제외한 키워드 집합을 만든다."""
    if not text:
        return set()
    try:
        from core.duplicate_checker import _keywords
    except ImportError:
        # 폴백 — 최소 토큰화(2자 이상 한글/영문).
        raw = _NON_WORD_RE.sub(" ", text).split()
        return {w for w in raw if len(w) >= 2 and w not in _SKELETON_STOPWORDS}
    return {w for w in _keywords(text) if w not in _SKELETON_STOPWORDS}


def body_similarity(content_a: str, content_b: str) -> float:
    """골격어 제외 후 두 본문의 Jaccard 유사도(0.0~1.0)."""
    kw_a = _content_keywords(content_a)
    kw_b = _content_keywords(content_b)
    if not kw_a or not kw_b:
        return 0.0
    return len(kw_a & kw_b) / len(kw_a | kw_b)


# ─────────────────────────────────────────────────────────────────────────────
# 게이트 판정 — auto_scheduler 사전차단에서 호출하는 최소 진입점.
# ─────────────────────────────────────────────────────────────────────────────
def evaluate_gate(
    title: str,
    *,
    records: Optional[Iterable[dict]] = None,
    now: Optional[datetime] = None,
    window_days: int = _WINDOW_DAYS,
    max_per_window: int = _MAX_PER_WINDOW,
) -> dict:
    """제도-단위 중복 게이트를 판정한다(발행 차단 여부 + 리프레시 힌트).

    Args:
        title:          검사할 신규 글 제목.
        records:        히스토리 레코드(생략 시 post_history.json).
        now:            기준 시각(테스트 결정성).
        window_days:    카운트 윈도우(일).
        max_per_window: 윈도우 내 허용 편수. count 가 이 값을 **초과**하면 차단.

    Returns:
        {
          "blocked":     bool,   # True 면 신규 발행 차단(폐기 아님)
          "institution": str,    # 정규화 제도명("" 면 판정 불가 → 통과)
          "count":       int,    # 윈도우 내 기존 편수
          "limit":       int,    # 적용된 임계
          "action":      str,    # "pass" | "refresh_weakest"
          "reason":      str,    # 로그·기록용 사유 태그
        }

    설계 원칙:
        - institution 이 비면(정규화 실패) 절대 차단하지 않는다(오탐 방지, fail-open).
        - count 가 limit "이하"면 통과. limit "초과"부터 차단 → 기본 3편까지 허용.
        - 차단은 폐기가 아니라 "기존 최약 글 리프레시" 신호(action=refresh_weakest).
    """
    institution = normalize_institution(title)
    if not institution:
        return {
            "blocked": False, "institution": "", "count": 0,
            "limit": max_per_window, "action": "pass",
            "reason": "no_institution",
        }

    count = count_recent(
        institution, records=records, now=now, window_days=window_days,
    )
    blocked = count > max_per_window
    return {
        "blocked": blocked,
        "institution": institution,
        "count": count,
        "limit": max_per_window,
        "action": "refresh_weakest" if blocked else "pass",
        "reason": (
            f"institution_saturated:{institution}:{count}>{max_per_window}"
            if blocked else "under_limit"
        ),
    }


# ─────────────────────────────────────────────────────────────────────────────
# 사촌 글 자동 병합 — 대상 자동 선정 + 브리프 생성(발행/재작성은 기존 트리거).
# ─────────────────────────────────────────────────────────────────────────────
def _record_url(rec: dict) -> str:
    return str(rec.get("url", "") or "")


def find_weakest_sibling(
    institution: str,
    *,
    records: Optional[Iterable[dict]] = None,
) -> Optional[dict]:
    """같은 제도 사촌 글 중 '가장 약한'(오래된) 1편을 리프레시 후보로 고른다.

    성능 실측 신호(방문자·순위)가 이 히스토리에는 없으므로, 결정적이고 안전한
    프록시로 **가장 오래된(posted_at 최소) 글**을 최약체로 본다 — 리프레시 파이프
    라인이 실측 기반으로 재선정할 수 있게 후보만 넘긴다.

    Returns:
        최약 사촌 레코드(dict) 또는 매칭 없으면 None.
    """
    if not institution:
        return None
    if records is None:
        records = _load_history()

    siblings: List[Tuple[datetime, dict]] = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rec_title = rec.get("clean_title") or rec.get("title") or ""
        if normalize_institution(rec_title) != institution:
            continue
        posted = _parse_posted_at(rec.get("posted_at", "")) or datetime.min
        siblings.append((posted, rec))

    if not siblings:
        return None
    siblings.sort(key=lambda pr: pr[0])   # 오래된 순
    return siblings[0][1]


def build_merge_brief(
    title: str,
    *,
    records: Optional[Iterable[dict]] = None,
    category: str = "",
) -> Optional[dict]:
    """제도-포화로 차단된 신규 글에 대해 '사촌 병합/리프레시 브리프'를 만든다.

    실제 병합·재작성은 하지 않는다(폐기 아님). 기존 리프레시/필러 파이프라인이
    소비할 수 있는 **대상 선정 + 필러 링크 힌트**만 산출한다.

    Returns:
        {
          "institution": str,
          "sibling_count": int,          # 히스토리상 사촌 총수
          "refresh_target": Optional[dict],  # 최약 사촌(리프레시 대상)
          "pillar_links": list,          # topic_cluster 가 제안하는 필러/형제 링크
          "note": str,
        }
        정규화 실패면 None.
    """
    institution = normalize_institution(title)
    if not institution:
        return None
    if records is None:
        records = list(_load_history())
    else:
        records = list(records)

    sibling_count = count_recent(
        institution, records=records, window_days=10_000,   # 전체(윈도우 무제한)
    )
    target = find_weakest_sibling(institution, records=records)

    # 필러 링크는 topic_cluster 의 기존 클러스터 그래프를 재사용(있으면).
    pillar_links: list = []
    try:
        from core import topic_cluster as tc
        pillar_links = tc.links_for_new(title, category, k=3)
    except (ImportError, OSError, ValueError, KeyError) as exc:
        log.debug(f"[institution_dedup] 필러 링크 조회 생략: {exc}")

    return {
        "institution": institution,
        "sibling_count": sibling_count,
        "refresh_target": target,
        "pillar_links": pillar_links,
        "note": (
            f"'{institution}' 제도가 이미 {sibling_count}편 — 신규 대신 최약 사촌 "
            f"1편을 리프레시하고 필러로 링크 통합 권장(폐기 아님)."
        ),
    }
