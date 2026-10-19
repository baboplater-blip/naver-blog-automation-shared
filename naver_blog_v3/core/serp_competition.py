# core/serp_competition.py — 실측 SERP 경쟁 판정 (개선안 #4 · 승산의 진실화, 2026-07-08)
"""승산(winnability)을 '어절 개수'라는 환각 상수에서 '실제 SERP 경쟁'으로 교체한다.

기존 opportunity_score._winnability 는 오직 토큰 수만 보고 승산을 매겼다 — 실제로
그 키워드의 통합검색 상단을 무엇이 먹고 있는지는 한 번도 관측하지 않았다. 그 결과
볼륨 20만짜리 헤드("디지털온누리 신청 방법")가 신규·저권위 블로그에서도 승산 1.0
"발행 1순위"로 떠버리는 환각이 났다. 고볼륨 헤드일수록 통합검색 상단을 광고·공식
(정부/공공기관)·플레이스가 차지해 블로그 탭 도달이 거의 없는데도 말이다.

이 모듈은 그 고리를 끊는다:
  1) 관측: 키워드의 네이버 **통합검색 SERP** 상위 구성을 판정 — 블로그 vs
     공식/정부(go.kr·공공기관)·광고·플레이스·지식iN·카페·뉴스.
  2) 산정: winnability = **블로그 탭 점유 비율** 계열(공식/광고/플레이스가 상단을
     지배하면 낮게). 신규 블로그가 파고들 여지를 실측으로 추정.
  3) 캐시: 같은 키워드 재조회를 막기 위해 파일 캐시(TTL). SERP 조회는 비싸다.

설계 원칙(전 경로 fail-open):
- 주입식 fetcher — 실서버 없이 결정론적 테스트. 마크업이 바뀌어도 파서만 교체.
- 조회 실패·판별 불가 시 None(unknown) → 호출측이 기존 어절 휴리스틱으로 폴백.
- 어떤 실패도 발행/점수 산정을 막지 않는다.

⚠️ 한계: 네이버 통합검색 HTML 구조는 자주 바뀌고 개인화·지역화된다. 여기의 판정은
   취약한 best-effort 이며, 실제 가치는 (1)(2)(3)의 관측→실측승산→캐시 프레임이다.
   조회 소스는 언제든 교체 가능하다.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent
from config import profile as _nbaprofile

_DATA_DIR = _nbaprofile.app_data_root()
_CACHE_PATH = _DATA_DIR / "logs" / "serp_competition_cache.json"

# 캐시 만료(초). SERP 경쟁 구도는 며칠 단위로만 유의미하게 변하므로 기본 7일.
_CACHE_TTL_SEC = 7 * 24 * 3600

# 상위 몇 개 결과 블록을 경쟁 구성으로 볼지. 통합검색 1스크린 ≒ 상위 10.
_TOP_N = 10

# 결과 블록 타입 판정용 마커(마크업 변경 대비 OR 다중). 소문자 비교.
# 네이버 통합검색은 섹션마다 클래스·URL 패턴이 다르다 — 링크 도메인 + 섹션 클래스 병용.
_OFFICIAL_HOST_RE = re.compile(
    r"https?://(?:[a-z0-9-]+\.)*"
    r"(?:go\.kr|gov\.kr|korea\.kr|molit\.go\.kr|bokjiro\.go\.kr|gov24|"
    r"or\.kr|re\.kr|ac\.kr)",
    re.IGNORECASE,
)
_BLOG_HOST_RE = re.compile(
    r"https?://(?:m\.)?blog\.naver\.com/[^\"'/\s]+", re.IGNORECASE
)
_CAFE_HOST_RE = re.compile(
    r"https?://(?:m\.)?cafe\.naver\.com/", re.IGNORECASE
)
_KIN_HOST_RE = re.compile(
    r"https?://kin\.naver\.com/", re.IGNORECASE
)

# 섹션/블록 클래스·라벨 마커(도메인만으로 안 잡히는 광고·플레이스·파워링크).
_AD_MARKERS = ("파워링크", "powerlink", "power_link", "ad_area", "광고 ",
               "adsystem", "shopping-box", "브랜드검색", "sp_nreview_ad")
_PLACE_MARKERS = ("place_area", "place_map", "map.naver", "지도에서 보기",
                  "플레이스", "loc_map", "place.naver", "sp_place")
_NEWS_MARKERS = ("news.naver.com", "언론사 선정", "sp_nnews", "news_area")


# ─────────────────────────────────────────────────────
# SERP 구성 판정
# ─────────────────────────────────────────────────────
def _bucket_for_link(link: str) -> Optional[str]:
    """단일 결과 링크(도메인)로 블록 타입 판정. 못 정하면 None."""
    if not link:
        return None
    if _BLOG_HOST_RE.search(link):
        return "blog"
    if _OFFICIAL_HOST_RE.search(link):
        return "official"
    if _CAFE_HOST_RE.search(link):
        return "cafe"
    if _KIN_HOST_RE.search(link):
        return "kin"
    return None


# 결과 링크 후보 추출: href="..." 중 http 로 시작하는 것.
_HREF_RE = re.compile(r'href=["\'](https?://[^"\']+)["\']', re.IGNORECASE)


def classify_serp(html: str, *, top_n: int = _TOP_N) -> Optional[dict]:
    """통합검색 HTML → 상위 구성 집계.

    반환(dict) 또는 판정 불가 시 None:
      {
        "total": 분류된 블록 수(>=1),
        "counts": {"blog":.., "official":.., "cafe":.., "kin":..,
                   "ad":.., "place":.., "news":..},
        "blog_share": 블로그 블록 / total (0..1),
        "official_dominant": bool(공식+광고+플레이스가 과반),
      }

    HTML 이 비었거나 링크를 하나도 못 뽑으면 None(unknown) → 호출측 폴백.
    """
    if not html:
        return None
    low = html.lower()

    counts = {"blog": 0, "official": 0, "cafe": 0, "kin": 0,
              "ad": 0, "place": 0, "news": 0}

    # 1) 도메인 기반 결과 링크 집계(상위 top_n 까지). 중복 URL 은 한 번만.
    seen_links: set = set()
    domain_blocks = 0
    for match in _HREF_RE.finditer(html):
        if domain_blocks >= top_n:
            break
        link = match.group(1)
        key = link.split("?")[0].lower()
        if key in seen_links:
            continue
        bucket = _bucket_for_link(link)
        if bucket is None:
            continue
        seen_links.add(key)
        counts[bucket] += 1
        domain_blocks += 1

    # 2) 섹션 마커 기반(도메인으로 안 잡히는 광고·플레이스·뉴스 존재 신호).
    #    카운트는 '존재 여부'를 1블록으로 근사(정확한 항목 수는 HTML 구조상 불안정).
    if any(m in low for m in _AD_MARKERS):
        counts["ad"] += 1
    if any(m in low for m in _PLACE_MARKERS):
        counts["place"] += 1
    if any(m in low for m in _NEWS_MARKERS):
        counts["news"] += 1

    total = sum(counts.values())
    if total <= 0:
        return None  # 아무 블록도 판정 못 함 → unknown

    blog_share = counts["blog"] / total
    non_blog_head = counts["official"] + counts["ad"] + counts["place"]
    official_dominant = non_blog_head > (total / 2.0)

    return {
        "total": total,
        "counts": counts,
        "blog_share": round(blog_share, 3),
        "official_dominant": official_dominant,
    }


def _winnability_from_composition(comp: dict) -> float:
    """SERP 구성 → winnability(0..1).

    핵심: 블로그 탭 점유 비율이 곧 신규 블로그의 진입 여지다. 다만 공식/광고/
    플레이스가 상단을 '지배'하면 블로그가 몇 개 있어도 클릭이 그 위에서 가로채이므로
    추가 감점한다(못 이길 헤드 강등).
    """
    blog_share = comp.get("blog_share", 0.0)
    counts = comp.get("counts", {})
    total = max(1, comp.get("total", 1))

    win = blog_share  # 기본선 = 블로그 탭 점유율

    # 공식/광고/플레이스 지배 시 상단 클릭 가로챔 → 강등 배수.
    non_blog_head = counts.get("official", 0) + counts.get("ad", 0) + counts.get("place", 0)
    head_ratio = non_blog_head / total
    if comp.get("official_dominant"):
        win *= 0.4          # 상단 과반이 비블로그 = 실질 승산 붕괴
    elif head_ratio >= 0.3:
        win *= 0.7          # 상단 일부 잠식

    return max(0.0, min(1.0, round(win, 3)))


# ─────────────────────────────────────────────────────
# 파일 캐시(TTL) — 같은 키워드 재조회 방지
# ─────────────────────────────────────────────────────
def _load_cache() -> dict:
    from config import safe_load_json
    data = safe_load_json(_CACHE_PATH, default={})
    return data if isinstance(data, dict) else {}


def _cache_get(key: str, *, ttl_sec: int, now: float) -> Optional[dict]:
    """캐시에서 만료 안 된 항목 반환. 없거나 만료면 None."""
    cache = _load_cache()
    entry = cache.get(key)
    if not isinstance(entry, dict):
        return None
    ts = entry.get("cached_at", 0)
    if not isinstance(ts, (int, float)) or (now - ts) > ttl_sec:
        return None
    return entry


def _cache_put(key: str, value: dict, *, now: float) -> None:
    """캐시에 항목 저장(원자적). 쓰기 실패는 무해하게 넘어감(fail-open)."""
    from config import atomic_write_json
    try:
        cache = _load_cache()
        cache[key] = {**value, "cached_at": now}
        atomic_write_json(_CACHE_PATH, cache)
    except (OSError, ValueError, TypeError) as exc:
        log.debug("[serp_competition] 캐시 저장 실패(무해): %s", exc)


def _cache_key(keyword: str) -> str:
    return (keyword or "").strip().lower()


# ─────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────
def real_winnability(
    keyword: str,
    *,
    fetcher: Optional[Callable[[str, dict], "tuple[int, str]"]] = None,
    use_cache: bool = True,
    ttl_sec: int = _CACHE_TTL_SEC,
    now: Optional[float] = None,
) -> Optional[float]:
    """키워드의 실측 승산(0..1). 조회/판정 실패 시 None(→ 호출측 어절 폴백).

    Args:
        keyword: 조회할 키워드.
        fetcher: serp_fetch.HtmlFetcher (url, headers)->(status, body). 주입 시
                 실서버 없이 결정론적 테스트. 미지정 시 기본 통합검색 fetcher.
        use_cache: 파일 캐시 사용 여부(기본 True — 같은 키워드 재조회 방지).
        ttl_sec: 캐시 만료 초(기본 7일).
        now: 결정적 테스트용 타임스탬프 주입(미지정 시 time.time()).

    Returns:
        0.0~1.0 winnability, 또는 판정 불가 시 None.
    """
    if not keyword or not keyword.strip():
        return None
    now = time.time() if now is None else now
    key = _cache_key(keyword)

    if use_cache:
        cached = _cache_get(key, ttl_sec=ttl_sec, now=now)
        if cached is not None:
            win = cached.get("winnability")
            return float(win) if isinstance(win, (int, float)) else None

    comp = classify_keyword(keyword, fetcher=fetcher)
    if comp is None:
        return None  # unknown — 캐시에 저장하지 않는다(다음에 재시도 여지).

    win = _winnability_from_composition(comp)
    if use_cache:
        _cache_put(key, {"winnability": win,
                         "blog_share": comp.get("blog_share"),
                         "counts": comp.get("counts")}, now=now)
    return win


def classify_keyword(
    keyword: str,
    *,
    fetcher: Optional[Callable[[str, dict], "tuple[int, str]"]] = None,
) -> Optional[dict]:
    """키워드 → SERP 경쟁 구성(classify_serp 결과). 조회/판정 실패 시 None.

    캐시를 타지 않는 순수 조회+판정(디버깅·리포트용). real_winnability 가 내부 사용.
    """
    if not keyword or not keyword.strip():
        return None
    from core import serp_fetch

    html, status = serp_fetch.fetch_integrated_search(keyword, fetcher=fetcher)
    if status != "ok" or not html:
        return None
    try:
        return classify_serp(html)
    except (re.error, ValueError, TypeError) as exc:
        log.debug("[serp_competition] SERP 파싱 실패(%s): %s", keyword[:20], exc)
        return None


__all__ = [
    "classify_serp",
    "classify_keyword",
    "real_winnability",
]
