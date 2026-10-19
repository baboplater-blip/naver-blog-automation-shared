# core/serp_rank.py — 통합검색 실노출 순위 (순위 모니터 P1, 2026-07-19)
"""내 글이 네이버 '통합검색' 화면에서 실제 몇 번째 블로그 결과인가.

rank_tracker(공식 블로그검색 API, sort=sim)는 프록시라 스마트블록·인기글이 섞인
통합검색 실노출과 다를 수 있다. 이 모듈은 워치리스트 키워드에 한해(≤50개)
통합검색 HTML 을 하루 1회 저빈도로 가져와 '블로그 링크 등장 순서' 기준 실노출
순위를 잰다. 데이터랩툴즈 모니터의 실측 관측을 서버 없는 방식으로 재현.

설계 원칙(전 경로 fail-open):
- 주입식 fetcher — 실서버 없이 결정론적 테스트. 마크업 변경은 파서만 교체.
- 조회 실패·파싱 불가 → None/0 기록 없이 조용히 스킵. API 순위는 그대로 유지.
- 일 1회 캐시(logs/serp_rank_cache.json) + 요청 간 sleep — 저빈도 원칙.
- 기록은 performance_db rank_history 에 tab="total" 로 병기(blog 탭과 공존).

⚠️ 한계: 통합검색은 개인화·마크업 변경이 잦다. 여기 값은 '참고치'이며
   API 순위와의 큰 불일치(API 상위인데 실노출 없음)가 AEO 리프레시 신호다.
"""
from __future__ import annotations

import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

from config import profile as _nbaprofile

_DATA_DIR = _nbaprofile.app_data_root()
_CACHE_PATH = _DATA_DIR / "logs" / "serp_rank_cache.json"

_SERP_URL = "https://search.naver.com/search.naver"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
# 블로그 포스트 링크(모바일/PC 공통) — (blog_id, log_no)
_BLOG_LINK_RE = re.compile(
    r"https?://(?:m\.)?blog\.naver\.com/([A-Za-z0-9_.-]+)/(\d+)", re.IGNORECASE)

try:
    from config.settings import API_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    API_REQUEST_TIMEOUT = 10


def _enabled() -> bool:
    try:
        from config import settings as _s
        return bool(getattr(_s, "SERP_RANK_ENABLED", True))
    except Exception:  # noqa: BLE001
        return True


def _blog_id() -> str:
    try:
        from config.settings import NAVER_ID
        bid = (NAVER_ID or "").strip()
        return "" if bid == "your_naver_id" else bid
    except (ImportError, AttributeError):
        return ""


def _fetch_html(keyword: str) -> str | None:
    """통합검색 HTML. 실패는 None(호출측이 스킵)."""
    qs = urllib.parse.urlencode({"where": "nexearch", "query": keyword})
    req = urllib.request.Request(f"{_SERP_URL}?{qs}", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=API_REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 — 네트워크·차단·인코딩 전부 조용히 스킵
        log.debug(f"[serp_rank] 조회 실패 ({keyword[:20]}): {e}")
        return None


def find_my_rank(html: str, blog_id: str) -> dict | None:
    """HTML 에서 내 블로그의 실노출 순위(블로그 링크 등장 순서 기준).

    반환 {rank(1-base, 0=미노출), of(전체 블로그 결과 수), log_no} 또는
    블로그 링크를 하나도 못 찾으면 None(파싱 불가 — 마크업 변경 등).
    """
    if not html or not blog_id:
        return None
    seen: list[tuple[str, str]] = []
    for bid, log_no in _BLOG_LINK_RE.findall(html):
        key = (bid.lower(), log_no)
        if key not in seen:
            seen.append(key)
    if not seen:
        return None
    bid_l = blog_id.lower()
    for idx, (bid, log_no) in enumerate(seen, start=1):
        if bid == bid_l:
            return {"rank": idx, "of": len(seen), "log_no": log_no}
    return {"rank": 0, "of": len(seen), "log_no": ""}


def check_serp_rank(keyword: str, *, blog_id: str = "",
                    fetcher=None) -> dict:
    """단일 키워드 실노출 조회. {found, rank, of, reason}."""
    bid = blog_id or _blog_id()
    if not bid:
        return {"found": False, "rank": 0, "of": 0, "reason": "no_blog_id"}
    html = (fetcher or _fetch_html)(keyword)
    if html is None:
        return {"found": False, "rank": 0, "of": 0, "reason": "fetch_failed"}
    parsed = find_my_rank(html, bid)
    if parsed is None:
        return {"found": False, "rank": 0, "of": 0, "reason": "parse_failed"}
    return {"found": parsed["rank"] > 0, "reason": "", **parsed}


# ─────────────────────────────────────────────────────
# 워치리스트 일간 추적
# ─────────────────────────────────────────────────────
def _cache_load() -> dict:
    from config import safe_load_json
    d = safe_load_json(_CACHE_PATH, default=dict)
    return d if isinstance(d, dict) else {}


def _cache_save(d: dict) -> None:
    from config import atomic_write_json
    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(_CACHE_PATH), d)


def track_watchlist(*, sleep: float = 1.5, fetcher=None,
                    data: dict | None = None) -> dict:
    """워치리스트 키워드의 통합검색 실노출을 하루 1회 측정·기록.

    반환 {checked, found, skipped_cached, reason?}. 어떤 실패도 예외로 새지 않는다.
    """
    if not _enabled():
        return {"checked": 0, "found": 0, "skipped_cached": 0,
                "reason": "disabled"}
    bid = _blog_id()
    if not bid:
        return {"checked": 0, "found": 0, "skipped_cached": 0,
                "reason": "no_blog_id"}

    from core import performance_db as pdb
    from core import rank_watchlist

    kws = rank_watchlist.keywords()
    if not kws:
        return {"checked": 0, "found": 0, "skipped_cached": 0,
                "reason": "empty_watchlist"}

    today = date.today().strftime("%Y-%m-%d")
    cache = _cache_load()
    own = data is None
    if own:
        data = pdb.load()

    checked = found = cached = 0
    for kw in kws:
        ck = f"{today}|{kw}"
        if ck in cache:
            cached += 1
            continue
        res = check_serp_rank(kw, blog_id=bid, fetcher=fetcher)
        cache[ck] = {"rank": res["rank"], "of": res.get("of", 0),
                     "reason": res.get("reason", "")}
        if res.get("reason") in ("fetch_failed", "parse_failed"):
            # 실패는 기록하지 않는다(참고치조차 오염 금지) — 캐시로 재시도만 방지
            time.sleep(sleep)
            continue
        checked += 1
        if res["found"]:
            found += 1
        # ① 워치리스트 자체 이력(글 앵커 무관, 항상 표시 가능)
        rank_watchlist.record_measurement(kw, day=today, serp_rank=res["rank"])
        # ② 이 키워드를 blog 탭으로 추적 중인 글이 있으면 total 탭 병기
        hist = rank_watchlist._best_hist_for_keyword(data, kw)
        if hist:
            for key, rec in data.get("posts", {}).items():
                if (rec.get("rank_history") or {}).get(kw) is hist:
                    pdb.record_rank(key, kw, res["rank"], tab="total",
                                    day=today, data=data)
                    break
        time.sleep(sleep)

    _cache_save(cache)
    if own:
        pdb.save(data)
    log.info(f"[serp_rank] 실노출 측정 {checked} · 노출 {found} · 캐시 {cached}")
    return {"checked": checked, "found": found, "skipped_cached": cached}
