# core/competitor_analyzer.py — 경쟁 블로그 분석 → 글쓰기 고도화 (2026-08-02)
"""상위노출·전문 경쟁 블로그를 발굴·분석해 제목·주제·발행 전략을 '신호'로만
학습하고, 그 신호를 생성 프롬프트에 주입해 우리 글쓰기를 고도화한다.

폐루프:
  타깃 키워드 → 블로그검색 API SERP(rank_tracker._search)에서 경쟁 블로그 발굴
  → **주제 관련성 필터**로 전문 블로그(specialist) vs 잡블로그(generalist) 분류
  → 전문 블로그 최근 글(blog_fetcher)에서 구조/공식 신호만 집계
  → 도메인 프로파일(logs/competitor_profiles.json, TTL 7일)
  → ①content_generator 프롬프트 힌트(prompt_hint) ②reports/{date}_competitor_analysis.md

**윤리(top_post_analyzer 계승)**: 표현·문장·본문은 절대 저장·모방하지 않는다.
저장하는 것은 오직 **신호/공식**뿐 — 제목 패턴 빈도(연도·숫자·질문형·의도 수식어),
주제 토큰 분포, 발행 빈도, 주제 갭. 표절·유사도 리스크 0(duplicate_checker 정책 일관).

원칙:
- 전 경로 fail-open: 자격증명·네트워크·데이터 부재는 조용히 스킵(빈 프로파일).
- 주입식 searcher/fetcher — 실서버 없이 결정론적 테스트.
- 저빈도·예의: 키워드당 1콜(SERP) + 후보당 1콜(글 목록), 요청 사이 sleep.
- 프로필 격리: 캐시·리포트는 활성 프로필 경로(config.profile).
- opt-in: content_generator 주입은 COMPETITOR_ANALYSIS_ENABLED=True 일 때만.

⚠️ **주제 관련성 필터가 핵심**: 상위노출 최상위 블로그가 종종 잡블로그(복지 키워드에
뜨지만 최근 글은 다이어트·프로야구·여행)라, 무필터 분석은 오프토픽 노이즈로 오염된다.
그래서 후보의 최근 글 제목이 우리 도메인 어휘(온브랜드 lexicon)와 겹치는 비율을
계산해 임계값 이상인 전문 블로그만 채택한다.
"""
from __future__ import annotations

import re
import statistics
import time
from datetime import date, datetime
from typing import Callable, Dict, List, Optional, Tuple

from config.logger import get_logger

log = get_logger(__name__)

from config import profile as _nbaprofile

_PROFILES_PATH = _nbaprofile.app_data_root() / "logs" / "competitor_profiles.json"
_TTL_SEC = 7 * 24 * 3600

# ── 튜너블 기본값(settings 로 오버라이드 가능) ─────────────────────────
_DEFAULT_RELEVANCE = 0.40      # 전문 블로그 판정 임계(온브랜드 제목 비율)
_DEFAULT_MAX_COMPETITORS = 12  # 분류·분석할 후보 상한(SERP 등장 빈도 상위)
_DEFAULT_POSTS_PER_BLOG = 20   # 후보당 조회할 최근 글 수
_DEFAULT_MIN_POSTS = 5         # 관련성 판정에 필요한 최소 표본(이하면 판단 보류)
_DEFAULT_SLEEP = 0.3           # 실호출 시 요청 사이 대기(ToS 예의)

_BLOG_ID_RE = re.compile(r"blog\.naver\.com/([A-Za-z0-9_-]+)", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")
_YEAR_PREFIX_RE = re.compile(r"^\s*\[?\s*20\d{2}")
_QUESTION_RE = re.compile(r"\?|까요|나요|은가요|인가요|일까|하나요|되나요")

# 의도 수식어 — 제목 공식 분석용(rank_tracker._INTENT_MODIFIERS 와 같은 어휘군).
_INTENT_MODIFIERS: Tuple[str, ...] = (
    "신청방법", "신청기간", "신청기한", "지급일", "지급시기", "신청", "자격",
    "조건", "대상", "금액", "한도", "방법", "기간", "서류", "후기", "조회",
    "계산", "시기", "종류", "혜택", "소급", "인상", "비교", "차이",
)

# 주제 토큰 분포에서 제외할 범용어(의도 수식어 + 콘텐츠 잡음 + 프레이밍어). 실제 '주제'가 아님.
# ⚠️ 이 집합은 topics 집계(_topic_distribution)와 lexicon 정제(_domain_lexicon)에서만 쓰인다.
#    title_formula 의 의도 수식어 집계는 _INTENT_MODIFIERS 를 직접 쓰므로(후기·차이·비교
#    포함) 여기 프레이밍어를 늘려도 modifiers 집계는 영향받지 않는다(두 집계 분리).
_TOPIC_STOPWORDS = frozenset(_INTENT_MODIFIERS) | frozenset({
    "정리", "총정리", "핵심", "최신", "안내", "정보", "완벽", "그리고", "하는",
    "위한", "무엇", "얼마", "언제", "어디", "누구", "어떻게", "정말", "진짜",
    # 프레이밍어(2026-08-02 라이브 105곳 수집에서 topics·gap 을 오염시킨 것들)
    "이유", "알아보기", "확인하기", "일정", "사이트", "링크", "예약", "예매",
    "홈페이지", "고객센터", "가이드", "내용", "발급", "조회", "홈피", "바로가기",
})


# ─────────────────────────────────────────────────────
# 온브랜드 lexicon · 우리 발행 주제 (주제 관련성 필터의 기반)
# ─────────────────────────────────────────────────────
def _tokens(text: str) -> set:
    """제목/키워드 → 토큰 집합(길이 2+ 한글·영숫자). 표현이 아닌 어휘 신호."""
    return {t.lower() for t in _TOKEN_RE.findall(text or "") if len(t) >= 2}


def _domain_lexicon() -> frozenset:
    """우리 도메인 어휘 토큰 집합 — 온브랜드 판정의 기준.

    소스(둘 다 프로필 격리·주제 하드코딩 0):
      ① query_evidence 실측 하위검색어 풀(검색광고 harvest) — 공백 보존 키워드
      ② performance_db 우리 발행 글 제목(clean_title)
    둘 다 비면 빈 집합 → 관련성 판정 불가(호출측이 프로파일을 만들지 않음).
    """
    toks: set = set()
    try:
        from core import query_evidence
        for row in query_evidence.load_pool():
            toks |= _tokens(row.get("keyword", ""))
    except Exception as exc:  # noqa: BLE001 — 근거 풀 부재/오류는 다음 소스로
        log.debug(f"[competitor] lexicon(query_evidence) 실패(무시): {exc}")
    toks |= _our_title_tokens()
    # 범용 의도어는 lexicon 에서 제외 — '조건'·'신청' 같은 말이 잡블로그 제목에도
    # 흔해 오프토픽을 온브랜드로 오판정하게 만든다(주제어만 남긴다).
    toks -= _TOPIC_STOPWORDS
    return frozenset(t for t in toks if len(t) >= 2)


def _our_title_tokens() -> set:
    """우리가 발행한 글 제목의 토큰 집합(주제 갭 계산 '우리가 다룬 주제'). 실패 시 빈 집합."""
    toks: set = set()
    try:
        from core import performance_db as pdb
        data = pdb.load()
        for rec in (data.get("posts") or {}).values():
            title = rec.get("clean_title") or rec.get("title") or ""
            toks |= _tokens(title)
    except Exception as exc:  # noqa: BLE001 — DB 부재/오류는 빈 집합(주제 갭 스킵)
        log.debug(f"[competitor] 우리 제목 토큰 로드 실패(무시): {exc}")
    return toks


def _is_domainful_keyword(despaced_kw: str) -> bool:
    """검색량 키워드가 '주제어'인가 — 범용 의도어(신청·조건·방법 등)만으로 이뤄지지 않았나.

    엑셀 키는 despaced(공백 없음)라 토큰화가 안 되므로, 스톱워드 부분문자열을 긴 것부터
    제거해 남는 실체가 2자 이상이면 도메인 주제어로 본다. 예: '정부지원금신청방법' →
    '신청방법'·'방법'·'신청' 제거 후 '정부지원금' 잔존(주제어) / '신청방법' → 잔존 없음(의도어뿐).
    """
    residual = despaced_kw
    for sw in sorted(_TOPIC_STOPWORDS, key=len, reverse=True):
        residual = residual.replace(sw, "")
    return len(residual) >= 2


def _title_is_on_brand(title: str, lexicon: frozenset) -> bool:
    """제목이 우리 도메인과 관련 있는가.

    ① 제목 토큰이 lexicon(스톱워드 제외 도메인 어휘)과 하나라도 겹치면 온브랜드.
    ② 폴백: 검색량 엑셀에 등록된 키워드를 부분문자열로 포함하면 온브랜드.
       단 ①과 기준을 일치시키기 위해 **매칭된 키워드가 주제어일 때만** 인정한다
       (범용 의도어만으로 된 합성 키워드가 오프토픽 제목을 온브랜드로 새게 하지 않게 — M1).
    """
    if lexicon and (_tokens(title) & lexicon):
        return True
    try:
        from core import search_volume
        kw, vol = search_volume.best_keyword(title)
        if vol > 0 and _is_domainful_keyword(kw):
            return True
    except Exception:  # noqa: BLE001 — 검색량 소스 부재는 무시
        pass
    return False


def _relevance(titles: List[str], lexicon: frozenset) -> float:
    """제목 목록 중 온브랜드 비율(0.0~1.0). 빈 목록이면 0.0."""
    if not titles:
        return 0.0
    on_brand = sum(1 for t in titles if _title_is_on_brand(t, lexicon))
    return round(on_brand / len(titles), 3)


# ─────────────────────────────────────────────────────
# 1) 경쟁 블로그 발굴 (SERP → bloggerlink 집계)
# ─────────────────────────────────────────────────────
def _blog_id_of(item: dict) -> str:
    """SERP item(dict)에서 블로그 ID 추출. bloggerlink 우선, link 폴백. 없으면 ''."""
    for field in ("bloggerlink", "link"):
        m = _BLOG_ID_RE.search(item.get(field) or "")
        if m:
            return m.group(1)
    return ""


def _creds() -> Tuple[str, str, str]:
    """(client_id, client_secret, my_blog_id). rank_tracker 자격증명 재사용."""
    try:
        from core.rank_tracker import _creds as _rt_creds
        return _rt_creds()
    except Exception:  # noqa: BLE001
        return "", "", ""


def _default_search(keyword: str, cid: str, secret: str) -> List[dict]:
    """블로그검색 API SERP items. 자격증명/네트워크 실패 시 빈 리스트."""
    if not (cid and secret):
        return []
    try:
        from core.rank_tracker import _search
        items, status = _search(keyword, cid, secret)
        return items if status == "ok" else []
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[competitor] SERP 조회 실패 ({keyword[:20]}): {exc}")
        return []


def discover_competitors(keywords: List[str], *, searcher: Optional[Callable] = None,
                         exclude_self: bool = True, max_per_keyword: int = 20,
                         sleep: float = _DEFAULT_SLEEP) -> Dict[str, int]:
    """타깃 키워드들의 SERP 에서 경쟁 블로그를 발굴. {blog_id: 등장한 키워드 수}.

    같은 키워드 내 중복 등장은 1회로 센다(등장 키워드 수 = 여러 주제에 걸친 상위노출력).

    Args:
        keywords: 타깃 키워드 목록.
        searcher: 테스트 주입용 — keyword → SERP items(dict) 리스트. None 이면 실 API.
        exclude_self: 우리 블로그 ID 를 결과에서 제외.
        max_per_keyword: 키워드당 훑을 상위 item 수.
        sleep: 실 API 호출 시 요청 사이 대기(주입 searcher 면 무시).
    """
    cid, secret, my_id = _creds()
    search = searcher or (lambda kw: _default_search(kw, cid, secret))
    my_id_l = (my_id or "").lower()

    counts: Dict[str, set] = {}
    for kw in keywords:
        if not kw:
            continue
        try:
            items = search(kw) or []
        except Exception as exc:  # noqa: BLE001 — 한 키워드 실패가 전체를 막지 않게
            log.debug(f"[competitor] searcher 실패 ({str(kw)[:20]}): {exc}")
            items = []
        seen_this_kw: set = set()
        for it in items[:max_per_keyword]:
            bid = _blog_id_of(it if isinstance(it, dict) else {})
            if not bid or bid in seen_this_kw:
                continue
            if exclude_self and my_id_l and bid.lower() == my_id_l:
                continue
            seen_this_kw.add(bid)
            counts.setdefault(bid, set()).add(kw)
        if searcher is None:
            time.sleep(sleep)
    return {bid: len(kws) for bid, kws in counts.items()}


# ─────────────────────────────────────────────────────
# 2) 주제 관련성 필터 (specialist vs generalist)
# ─────────────────────────────────────────────────────
def classify_competitors(candidates: Dict[str, int], *,
                         fetcher: Optional[Callable] = None,
                         lexicon: Optional[frozenset] = None,
                         min_relevance: float = _DEFAULT_RELEVANCE,
                         max_competitors: int = _DEFAULT_MAX_COMPETITORS,
                         posts_per_blog: int = _DEFAULT_POSTS_PER_BLOG,
                         min_posts: int = _DEFAULT_MIN_POSTS,
                         sleep: float = _DEFAULT_SLEEP) -> Tuple[List[dict], List[dict]]:
    """후보 블로그를 전문(specialist)/잡(generalist)으로 분류.

    각 후보의 최근 글 제목이 도메인 lexicon 과 겹치는 비율(관련성)을 계산해
    min_relevance 이상이면 specialist. 표본이 min_posts 미만이면 판단 보류(제외).

    Returns:
        (specialists, generalists). specialist 항목은 posts(제목·날짜) 포함 —
        후속 analyze() 가 신호를 집계한다. generalist 는 진단용 최소 정보만.
    """
    if lexicon is None:
        lexicon = _domain_lexicon()
    fetch = fetcher or (lambda bid: _safe_fetch(bid, posts_per_blog))

    ranked = sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)
    specialists: List[dict] = []
    generalists: List[dict] = []
    for bid, kw_hits in ranked[:max_competitors]:
        try:
            posts = fetch(bid) or []
        except Exception as exc:  # noqa: BLE001 — 한 블로그 실패가 전체를 막지 않게
            log.debug(f"[competitor] fetch 실패 ({bid}): {exc}")
            posts = []
        titles = [p.get("title", "") for p in posts if p.get("title")]
        if len(titles) < min_posts:
            continue  # 표본 부족 — 관련성을 신뢰할 수 없어 판단 보류
        rel = _relevance(titles, lexicon)
        if rel >= min_relevance:
            specialists.append({
                "blog_id": bid, "relevance": rel, "keyword_hits": kw_hits,
                "posts_sampled": len(titles), "posts": posts,
            })
        else:
            generalists.append({
                "blog_id": bid, "relevance": rel, "keyword_hits": kw_hits,
                "posts_sampled": len(titles),
            })
        if fetcher is None:
            time.sleep(sleep)
    return specialists, generalists


def _safe_fetch(blog_id: str, count: int) -> List[dict]:
    """blog_fetcher fail-open 래퍼 위임(임포트 실패도 흡수)."""
    try:
        from core import blog_fetcher
        return blog_fetcher.fetch_posts_safe(blog_id, count=count)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[competitor] blog_fetcher 임포트/호출 실패 ({blog_id}): {exc}")
        return []


# ─────────────────────────────────────────────────────
# 3) 신호 집계 (제목 공식 · 주제 분포 · 발행 빈도 · 주제 갭)
# ─────────────────────────────────────────────────────
def _title_formula(titles: List[str]) -> dict:
    """제목 목록 → 공식 신호(비율·평균). 표현은 저장하지 않는다."""
    n = len(titles)
    if not n:
        return {}
    year = sum(1 for t in titles if _YEAR_PREFIX_RE.search(t))
    number = sum(1 for t in titles if re.search(r"\d", t))
    question = sum(1 for t in titles if _QUESTION_RE.search(t))
    avg_len = round(sum(len(t) for t in titles) / n, 1)
    mod_counts: Dict[str, int] = {}
    for t in titles:
        despaced = t.replace(" ", "")
        for mod in _INTENT_MODIFIERS:
            if mod in despaced:
                mod_counts[mod] = mod_counts.get(mod, 0) + 1
    top_mods = sorted(mod_counts.items(), key=lambda kv: kv[1], reverse=True)[:6]
    return {
        "year_share": round(year / n, 2),
        "number_share": round(number / n, 2),
        "question_share": round(question / n, 2),
        "avg_len": avg_len,
        "modifiers": [{"word": w, "share": round(c / n, 2)} for w, c in top_mods],
        "sample_titles": n,
    }


def _topic_distribution(titles: List[str], *, lexicon: Optional[frozenset] = None,
                        top_n: int = 12) -> List[dict]:
    """제목 토큰 빈도 → 주제 분포(상위 토큰·빈도). 범용어(stopword)·숫자는 제외.

    lexicon 이 비어있지 않으면 **온브랜드 토큰(lexicon 멤버)만 채택**한다 — 오프브랜드
    재테크(etf·주가 등 우리 도메인 어휘에 없는 토큰)와 프레이밍어를 자동 제거해 주제
    신호를 정제한다. lexicon=None/빈값이면 스톱워드-only 필터(기존 동작 — fail-open·회귀0).
    """
    counts: Dict[str, int] = {}
    for t in titles:
        for tok in _tokens(t):
            if tok in _TOPIC_STOPWORDS or tok.isdigit():
                continue
            if lexicon and tok not in lexicon:
                continue     # 온브랜드 토큰만(오프브랜드·프레이밍어 제거)
            counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [{"topic": w, "count": c} for w, c in ranked[:top_n] if c >= 2]


def _parse_date(raw: str) -> Optional[date]:
    """PostTitleListAsync 날짜 문자열 → date. 파싱 불가면 None(fail-open)."""
    if not raw:
        return None
    m = re.search(r"(20\d{2})\D+(\d{1,2})\D+(\d{1,2})", raw)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 13:  # epoch millis
        try:
            return datetime.fromtimestamp(int(digits[:13]) / 1000).date()
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _posts_per_week(posts: List[dict]) -> Optional[float]:
    """글 날짜들로 발행 빈도(주당 글 수) 추정. 날짜 2개 미만이면 None."""
    dates = [d for d in (_parse_date(p.get("date", "")) for p in posts) if d]
    if len(dates) < 2:
        return None
    span_days = (max(dates) - min(dates)).days
    if span_days < 1:
        return None
    return round(len(dates) / (span_days / 7.0), 1)


def _topic_gap(topics: List[dict], our_tokens: set, *, top_n: int = 10) -> List[str]:
    """경쟁 전문 블로그가 다루는데 우리가 안 다룬 주제 토큰. 우리 토큰 없으면 [](정직)."""
    if not our_tokens:
        return []
    return [t["topic"] for t in topics if t["topic"] not in our_tokens][:top_n]


def _build_profile(specialists: List[dict], generalists: List[dict],
                   keywords: List[str], now: float,
                   lexicon: Optional[frozenset] = None) -> Optional[dict]:
    """specialist 글들에서 신호를 집계한 도메인 프로파일. specialist 없으면 None.

    lexicon 을 넘기면 주제 분포를 온브랜드 토큰으로 정제한다(오프브랜드·프레이밍어 제거).
    None 이면 스톱워드-only 필터(기존 동작 — 회귀0).
    """
    if not specialists:
        return None
    all_titles: List[str] = []
    freqs: List[float] = []
    for s in specialists:
        posts = s.get("posts", [])
        all_titles += [p.get("title", "") for p in posts if p.get("title")]
        f = _posts_per_week(posts)
        if f is not None:
            freqs.append(f)
    topics = _topic_distribution(all_titles, lexicon=lexicon)
    return {
        "cached_at": now,
        "generated_at": date.today().strftime("%Y-%m-%d"),
        "keywords": list(keywords),
        "specialist_count": len(specialists),
        "generalist_excluded": len(generalists),
        "specialists": [
            {"blog_id": s["blog_id"], "relevance": s["relevance"],
             "keyword_hits": s["keyword_hits"], "posts_sampled": s["posts_sampled"]}
            for s in specialists
        ],
        "generalists": [
            {"blog_id": g["blog_id"], "relevance": g["relevance"]}
            for g in generalists
        ],
        "title_formula": _title_formula(all_titles),
        "topics": topics,
        "topic_gap": _topic_gap(topics, _our_title_tokens()),
        "posts_per_week_median": (round(statistics.median(freqs), 1)
                                  if freqs else None),
        "samples": len(all_titles),
    }


def _empty_profile(keywords: List[str], now: float, *,
                   generalist_excluded: int = 0) -> dict:
    """전문 블로그 0 결과의 '빈 마커' 프로파일 — TTL 창당 재스윕을 막는 캐시 대상.

    성공 프로파일과 같은 스키마(specialist_count=0·빈 리스트)라 소비자(prompt_hint·
    render_report)가 안전하게 '유효-but-empty'로 다룬다. empty=True 로 명시.
    """
    return {
        "cached_at": now,
        "generated_at": date.today().strftime("%Y-%m-%d"),
        "keywords": list(keywords),
        "specialist_count": 0,
        "generalist_excluded": generalist_excluded,
        "specialists": [],
        "generalists": [],
        "title_formula": {},
        "topics": [],
        "topic_gap": [],
        "posts_per_week_median": None,
        "samples": 0,
        "empty": True,
    }


# ─────────────────────────────────────────────────────
# 캐시
# ─────────────────────────────────────────────────────
def _load_cache() -> dict:
    from config import safe_load_json
    d = safe_load_json(_PROFILES_PATH, default=dict)
    return d if isinstance(d, dict) else {}


def _save_cache(profile: dict) -> None:
    from config import atomic_write_json
    _PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(_PROFILES_PATH), {"profile": profile})


def cached_profile(*, now: Optional[float] = None) -> Optional[dict]:
    """캐시된 도메인 프로파일(TTL 7일 이내). 네트워크 0. 없거나 만료면 None."""
    now = now or time.time()
    entry = _load_cache().get("profile")
    if (isinstance(entry, dict)
            and isinstance(entry.get("cached_at"), (int, float))
            and (now - entry["cached_at"]) <= _TTL_SEC):
        return entry
    return None


# ─────────────────────────────────────────────────────
# 오케스트레이션
# ─────────────────────────────────────────────────────
def _settings() -> dict:
    """settings 오버라이드(전부 선택). 실패 시 기본값."""
    out = {
        "enabled": False,
        "relevance": _DEFAULT_RELEVANCE,
        "max_competitors": _DEFAULT_MAX_COMPETITORS,
        "posts_per_blog": _DEFAULT_POSTS_PER_BLOG,
    }
    try:
        from config import settings as _s
        out["enabled"] = bool(getattr(_s, "COMPETITOR_ANALYSIS_ENABLED", False))
        out["relevance"] = float(getattr(_s, "COMPETITOR_MIN_RELEVANCE",
                                         _DEFAULT_RELEVANCE))
        out["max_competitors"] = int(getattr(_s, "COMPETITOR_MAX_BLOGS",
                                             _DEFAULT_MAX_COMPETITORS))
        out["posts_per_blog"] = int(getattr(_s, "COMPETITOR_POSTS_PER_BLOG",
                                            _DEFAULT_POSTS_PER_BLOG))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[competitor] settings 로드 실패(기본값 사용): {exc}")
    return out


def _head_of(keyword: str, n: int = 2) -> str:
    """키워드에서 앞 1~2어절(헤드)만 — SERP 발굴은 롱테일보다 헤드가 경쟁 블로그를 더 잘 잡는다."""
    return " ".join((keyword or "").split()[:n]).strip()


def default_keywords(limit: int = 60) -> List[str]:
    """온브랜드 경쟁 발굴 키워드 소스 — question_seeds 헤드 + 워치리스트 합집합.

    run_ops 스텝이 워치리스트(5개 안팎)만으론 빈약하므로 온브랜드가 보장된
    question_seeds(발행 시드)의 헤드를 얹는다. 공백 제거 소문자 키로 dedup,
    limit 캡. question_seeds 없으면 워치리스트 폴백, 둘 다 없으면 [](fail-open).
    """
    seen: set = set()
    out: List[str] = []

    def _add(kw: str) -> None:
        kw = (kw or "").strip()
        key = kw.replace(" ", "").lower()
        if kw and key not in seen:
            seen.add(key)
            out.append(kw)

    try:
        from core.demand_miner import load_question_seeds
        for seed in load_question_seeds():
            _add(_head_of(seed, 2))
    except Exception as exc:  # noqa: BLE001 — 시드 부재/오류는 워치리스트 폴백
        log.debug(f"[competitor] question_seeds 로드 실패(무시): {exc}")
    try:
        from core import rank_watchlist
        for kw in rank_watchlist.keywords():
            _add(kw)
    except Exception as exc:  # noqa: BLE001 — 워치리스트 부재도 무시(빈 리스트 가능)
        log.debug(f"[competitor] 워치리스트 로드 실패(무시): {exc}")
    return out[:max(1, limit)]


def analyze(keywords: Optional[List[str]] = None, *, searcher: Optional[Callable] = None,
            fetcher: Optional[Callable] = None, now: Optional[float] = None,
            max_competitors: Optional[int] = None, use_cache: bool = True) -> Optional[dict]:
    """경쟁 블로그 발굴 → 관련성 분류 → 신호 집계 → 캐시. 도메인 프로파일 반환.

    keywords 미지정 시 default_keywords(). specialist 를 못 찾거나 lexicon 이 비면 None
    (무필터 잡블로그 신호로 생성 프롬프트를 오염시키지 않기 위해 — 정직한 no-op).

    max_competitors: 분류할 후보 상한 오버라이드. None 이면 settings(COMPETITOR_MAX_BLOGS,
        기본 12)를 그대로 사용 — 기존 호출 회귀0. run_ops 는 40 을 넘겨 커버리지를 넓힌다.

    ⚠️ **프로필당 단일 도메인 프로파일 가정**: TTL(7일) 캐시는 키워드 집합으로 키잉하지
    않는다 — use_cache=True 로 다른 keywords 를 넘겨도 7일 내 이전 프로파일을 그대로
    반환한다. 특정 키워드 집합으로 강제 재분석하려면 use_cache=False 로 호출한다.
    """
    now = now or time.time()
    if use_cache:
        cached = cached_profile(now=now)
        if cached:
            return cached

    keywords = keywords or default_keywords()
    if not keywords:
        log.info("[competitor] 분석할 키워드 없음 — 스킵 (fail-open)")
        return None

    lexicon = _domain_lexicon()
    if not lexicon:
        # 온브랜드 판정 기준이 없으면 전문/잡 블로그를 가릴 수 없다 → 분석하지 않는다.
        log.info("[competitor] 도메인 lexicon 비어 있음 — 관련성 필터 불가로 스킵")
        return None

    cfg = _settings()
    cap = cfg["max_competitors"] if max_competitors is None else int(max_competitors)
    # ── 여기서부터 네트워크 스윕 발생 — 결과가 비어도 빈 마커를 캐시해 TTL 창당
    #    재스윕을 막는다(render_report·다음 실행이 캐시 히트로 재스윕하지 않게). ──
    candidates = discover_competitors(keywords, searcher=searcher)
    if not candidates:
        log.info("[competitor] SERP 경쟁 블로그 후보 0 — 빈 마커 캐시(재스윕 차단)")
        empty = _empty_profile(keywords, now)
        _save_cache(empty)
        return empty

    specialists, generalists = classify_competitors(
        candidates, fetcher=fetcher, lexicon=lexicon,
        min_relevance=cfg["relevance"], max_competitors=cap,
        posts_per_blog=cfg["posts_per_blog"])

    profile = _build_profile(specialists, generalists, keywords, now, lexicon=lexicon)
    if profile is None:
        log.info(f"[competitor] 전문 블로그 0(후보 {len(candidates)}·잡블로그 "
                 f"{len(generalists)} 제외) — 빈 마커 캐시(재스윕 차단)")
        empty = _empty_profile(keywords, now, generalist_excluded=len(generalists))
        _save_cache(empty)
        return empty
    _save_cache(profile)
    log.info(f"[competitor] 전문 블로그 {profile['specialist_count']}곳 분석 · "
             f"주제 {len(profile['topics'])} · 잡블로그 {profile['generalist_excluded']} 제외")
    return profile


# ─────────────────────────────────────────────────────
# 소비: 프롬프트 힌트 · 리포트
# ─────────────────────────────────────────────────────
def prompt_hint(text: str = "") -> str:
    """content_generator 주입용 제목·주제 공식 힌트 한 블록. 네트워크 0.

    COMPETITOR_ANALYSIS_ENABLED=True + 캐시된 프로파일이 있을 때만 반환.
    비활성/캐시 없음/표본 부족이면 "" (기존 생성 완전 무영향).

    ⚠️ **도메인 전역 힌트**: 바로 위 top_post_analyzer.prompt_hint 는 text(제목/주제)에
    앵커된 주제별 규격이지만, 이 힌트는 도메인 전체 단일 프로파일이라 카테고리·주제와
    무관하게 모든 글에 같은 경향을 주입한다(주제별 겨냥 아님 — 블록 문구로 명시). text
    인자는 API 대칭을 위해 받되 현재는 사용하지 않는다(도메인 전역이 의도된 설계).
    """
    del text   # 도메인 전역 힌트 — text 로 좁히지 않는다(위 docstring 참조)
    if not _settings()["enabled"]:
        return ""
    p = cached_profile()
    if not p or p.get("specialist_count", 0) < 1:
        return ""
    tf = p.get("title_formula") or {}
    if tf.get("sample_titles", 0) < 3:   # 표본 부족은 공식이라 하기 어렵다
        return ""

    lines = [f"[이 블로그가 겨루는 분야 전반의 상위 경쟁 블로그 실측 — 특정 주제가 아닌 "
             f"도메인 공통 제목·주제 경향, 전문 블로그 {p['specialist_count']}곳]"]
    tparts = []
    if tf.get("year_share") is not None:
        tparts.append(f"연도 접두 {int(tf['year_share'] * 100)}%")
    if tf.get("number_share") is not None:
        tparts.append(f"숫자 포함 {int(tf['number_share'] * 100)}%")
    if tf.get("question_share") is not None:
        tparts.append(f"질문형 {int(tf['question_share'] * 100)}%")
    if tf.get("avg_len"):
        tparts.append(f"평균 {tf['avg_len']}자")
    if tparts:
        lines.append(f"- 제목: {' · '.join(tparts)}")
    mods = [m["word"] for m in (tf.get("modifiers") or [])[:5]]
    if mods:
        lines.append(f"- 자주 쓰는 의도 수식어: {' · '.join(mods)}")
    topics = [t["topic"] for t in (p.get("topics") or [])[:8]]
    if topics:
        lines.append(f"- 활발히 다루는 주제: {' · '.join(topics)}")
    lines.append("- ★ 위는 제목 구성·주제 선택의 참고 신호입니다. 문장·표현은 절대 "
                 "모방하지 말고 기존 지침(품질 게이트·금지어·글자수 상한)을 우선하세요.")
    return "\n".join(lines)


def render_report(reports_dir=None, *, searcher: Optional[Callable] = None,
                  fetcher: Optional[Callable] = None) -> str:
    """경쟁 분석 리포트 → reports/{date}_competitor_analysis.md. 반환 파일명.

    캐시가 신선하면 그걸 쓰고, 없으면 analyze() 로 새로 만든다(fail-open).
    """
    from pathlib import Path
    if reports_dir:
        reports = Path(reports_dir)
    else:
        try:
            reports = _nbaprofile.reports_dir()
        except Exception:  # noqa: BLE001
            reports = Path(__file__).resolve().parent.parent.parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y-%m-%d")

    profile = cached_profile()
    if profile is None:
        try:
            profile = analyze(searcher=searcher, fetcher=fetcher, use_cache=False)
        except Exception as exc:  # noqa: BLE001 — 리포트는 절대 예외로 죽지 않게
            log.debug(f"[competitor] analyze 실패(리포트 no-data): {exc}")
            profile = None

    name = f"{today}_competitor_analysis.md"
    (reports / name).write_text(_render_markdown(profile, today), encoding="utf-8")
    return name


def _render_markdown(profile: Optional[dict], today: str) -> str:
    o = [f"# 🔎 경쟁 블로그 분석 — {today}\n"]
    if not profile or profile.get("specialist_count", 0) < 1:  # 빈 마커 포함 no-data
        o.append("> 아직 분석된 전문 경쟁 블로그가 없어요. 워치리스트·검색광고 근거 풀을 "
                 "채운 뒤 `python -m core.competitor_analyzer` 또는 순위 모니터를 실행하세요.\n"
                 ">\n> (상위노출 블로그가 전부 잡블로그였거나 자격증명/근거가 없을 수 있어요.)")
        return "\n".join(o)

    o.append(f"- 전문 블로그 **{profile['specialist_count']}곳** 분석 · "
             f"잡블로그 **{profile['generalist_excluded']}곳** 제외(주제 관련성 필터)")
    ppw = profile.get("posts_per_week_median")
    if ppw is not None:
        o.append(f"- 발행 빈도(중앙값): 주 **{ppw}편**")
    o.append("")

    o.append("## 전문 경쟁 블로그")
    o += ["| 블로그 | 관련성 | 등장 키워드 | 표본 |", "|---|---|---|---|"]
    for s in profile.get("specialists", []):
        o.append(f"| {s['blog_id']} | {int(s['relevance'] * 100)}% | "
                 f"{s['keyword_hits']} | {s['posts_sampled']} |")

    tf = profile.get("title_formula") or {}
    if tf:
        o.append("\n## 제목 공식(신호만 — 표현 모방 금지)")
        o.append(f"- 연도 접두 {int(tf.get('year_share', 0) * 100)}% · "
                 f"숫자 포함 {int(tf.get('number_share', 0) * 100)}% · "
                 f"질문형 {int(tf.get('question_share', 0) * 100)}% · "
                 f"평균 {tf.get('avg_len', 0)}자")
        mods = tf.get("modifiers") or []
        if mods:
            o.append("- 자주 쓰는 의도 수식어: "
                     + " · ".join(f"{m['word']}({int(m['share'] * 100)}%)"
                                  for m in mods))

    topics = profile.get("topics") or []
    if topics:
        o.append("\n## 다루는 주제 분포")
        o += ["| 주제 | 글 수 |", "|---|---|"]
        for t in topics:
            o.append(f"| {t['topic']} | {t['count']} |")

    gap = profile.get("topic_gap") or []
    if gap:
        o.append("\n## 주제 갭(그들은 다루는데 우리는 안 다룬 주제)")
        o.append("- " + " · ".join(gap))
        o.append("\n> 참고용입니다 — 자동 발행 큐에 넣지 않습니다. "
                 "온브랜드 여부를 사람이 판단해 골라 쓰세요.")

    o.append("\n> 이 프로파일은 생성 프롬프트에 **제목·주제 공식**으로만 주입됩니다"
             "(COMPETITOR_ANALYSIS_ENABLED). 표현·문장은 저장·모방하지 않습니다.")
    return "\n".join(o)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    prof = analyze(use_cache=False)
    if prof:
        print(f"전문 블로그 {prof['specialist_count']}곳 · 주제 {len(prof['topics'])} "
              f"→ reports/{render_report()}")
    else:
        print(f"분석 결과 없음 → reports/{render_report()}")
