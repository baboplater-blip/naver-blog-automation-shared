# core/performance_db.py — 유입 성과 중앙 저장소 (Phase 1 · L1)
"""발행한 글이 "실제로 몇 명을, 어떤 검색어로, 몇 위에서" 데려오는지 조인 저장소.

지금까지 시스템은 *산출*(발행 편수)만 기록했다. 이 모듈은 *성과*(편당 유입)를
기록해 폐루프의 오른쪽 절반을 채운다.

- 키: clean_title 정규화(발행 시 url 미기록 이력 때문). 통계·순위 수집이 실제
  url/log_no 를 발견하면 같은 레코드에 병합(reconcile)한다.
- 부수효과: 파일 쓰기(atomic)만. 네트워크 없음 — 수집은 stats_fetcher/rank_tracker 담당.
- fail-open: 데이터 없으면 빈 결과 반환, 예외로 파이프라인 막지 않음.

스키마 (logs/performance_db.json):
{
  "posts": {
    "<key>": {
      "title": str, "clean_title": str, "url": str, "log_no": str,
      "category": str, "keyword": str, "posted_at": str,
      "views": {"YYYY-MM-DD": int},                 # 일별 조회수
      "referral_keywords": {"검색어": int},          # 누적 유입 검색어 카운트
      "rank_history": {"키워드": [{"date","rank","tab"}]}
    }, ...
  },
  "visitors": {"YYYY-MM-DD": {"today","yesterday","total"}},  # 블로그 전체(미러)
  "updated_at": str
}
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

DB_PATH = _DATA_DIR / "logs" / "performance_db.json"

# 날짜 접두어("2026-05-17 — ") 및 공백/구두점 정규화용
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}\s*[—\-]\s*")
_NON_KEY = re.compile(r"[\s\W_]+", re.UNICODE)

# rank_history 의 tab 값 (rank_tracker 가 기록):
#   "blog"          — 헤드 키워드 공식 블로그검색 API 순위(기존, 상위노출율 KPI 소스)
#   "total"         — 통합검색 실노출(serp_rank P1, 참고치)
#   "blog_specific" — 구체 롱테일 쿼리 순위(2026-08-01 신규). 글이 실제 노출·트래픽을
#     얻는 구체 쿼리(예 '주거급여 지급일')를 추적한다. **기존 소비자(get_summary·
#     rank_movement·rank_view_gap·rank_alerts·rank_watchlist)는 이 tab 을 읽지 않고**,
#     니어미스 탐지 신규 소비자 near_miss_posts 만 읽는다(회귀 0 원칙).
SPECIFIC_TAB = "blog_specific"


# ─────────────────────────────────────────────────────
# 저장/로드
# ─────────────────────────────────────────────────────
def _empty() -> dict:
    return {"posts": {}, "visitors": {}, "updated_at": ""}


def load() -> dict:
    from config import safe_load_json
    data = safe_load_json(DB_PATH, default=_empty)
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("posts", {})
    data.setdefault("visitors", {})
    data.setdefault("updated_at", "")
    return data


def save(data: dict) -> None:
    from config import atomic_write_json
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(DB_PATH, data)


def _now_date() -> str:
    return date.today().strftime("%Y-%m-%d")


def normalize_key(title: str) -> str:
    """제목 → 안정적 매칭 키. 날짜 접두어 제거 후 공백·구두점 제거."""
    if not title:
        return ""
    t = _DATE_PREFIX.sub("", str(title)).strip()
    return _NON_KEY.sub("", t).lower()


# ─────────────────────────────────────────────────────
# 쓰기 API (수집기가 호출)
# ─────────────────────────────────────────────────────
def upsert_post(*, title: str = "", clean_title: str = "", url: str = "",
                log_no: str = "", category: str = "", keyword: str = "",
                posted_at: str = "", data: dict | None = None) -> tuple[dict, str]:
    """제목으로 레코드를 찾아 병합(없으면 생성). (data, key) 반환.

    data 를 넘기면 그 dict 를 in-place 수정(배치 저장용). 없으면 load→반환만.
    """
    own = data is None
    if own:
        data = load()
    base = clean_title or title
    key = normalize_key(base)
    if not key:
        return data, ""
    rec = data["posts"].setdefault(key, {
        "title": "", "clean_title": "", "url": "", "log_no": "",
        "category": "", "keyword": "", "posted_at": "",
        "views": {}, "referral_keywords": {}, "rank_history": {},
    })
    # 빈 값은 덮어쓰지 않고 채우기만 (reconcile 안전)
    for field, val in (("title", title), ("clean_title", clean_title or title),
                       ("url", url), ("log_no", log_no), ("category", category),
                       ("keyword", keyword), ("posted_at", posted_at)):
        if val and not rec.get(field):
            rec[field] = val
    if own:
        save(data)
    return data, key


def record_views(key: str, views: int, *, day: str = "", data: dict | None = None) -> None:
    own = data is None
    if own:
        data = load()
    rec = data["posts"].get(key)
    if rec is not None:
        rec.setdefault("views", {})[day or _now_date()] = int(views)
    if own:
        save(data)


def record_referral(key: str, keyword: str, count: int = 1, *,
                    data: dict | None = None) -> None:
    own = data is None
    if own:
        data = load()
    rec = data["posts"].get(key)
    if rec is not None and keyword:
        ref = rec.setdefault("referral_keywords", {})
        ref[keyword] = int(count)  # 수집값은 누적 스냅샷 → set
    if own:
        save(data)


def record_rank(key: str, keyword: str, rank: int, *, tab: str = "blog",
                day: str = "", volume: int = 0, estimated: bool = False,
                data: dict | None = None) -> None:
    own = data is None
    if own:
        data = load()
    rec = data["posts"].get(key)
    if rec is not None and keyword:
        hist = rec.setdefault("rank_history", {}).setdefault(keyword, [])
        d = day or _now_date()
        # 같은 날 중복 기록 방지 — 갱신. 단 (날짜, 탭) 단위 — API(blog)와
        # 통합검색 실노출(total, serp_rank P1)이 같은 날 공존해야 한다.
        hist[:] = [h for h in hist
                   if h.get("date") != d or h.get("tab", "blog") != tab]
        # volume: 이 키워드의 검색량(#7). 상위노출율 KPI 에서 '검색 수요 있는' 키워드만
        # 세기 위해 저장. 0 = 미상(옛 기록·엑셀 없음) → KPI 에서 미측정으로 분류.
        entry = {"date": d, "rank": int(rank), "tab": tab, "volume": int(volume or 0)}
        # estimated=True 는 실측 근거(검색광고 볼륨) 없이 추정된 구체 쿼리(수식어 폴백).
        # 기존 엔트리 형태를 바꾸지 않도록 True 일 때만 키를 추가(레거시 byte-identical).
        if estimated:
            entry["estimated"] = True
        hist.append(entry)
    if own:
        save(data)


def record_visitors(visitors: dict, *, day: str = "", data: dict | None = None) -> None:
    own = data is None
    if own:
        data = load()
    if isinstance(visitors, dict):
        data["visitors"][day or _now_date()] = {
            "today": int(visitors.get("today", 0) or 0),
            "yesterday": int(visitors.get("yesterday", 0) or 0),
            "total": int(visitors.get("total", 0) or 0),
        }
    if own:
        save(data)


def sync_from_history() -> int:
    """post_history.json 의 발행 카탈로그를 DB 에 시드(제목·카테고리·발행일).

    통계가 0 이어도 '무엇을 발행했는지'는 채워 둔다. 반환: 신규/갱신 편수.
    """
    from config import safe_load_json
    from core.content_generator import detect_category

    history = safe_load_json(_DATA_DIR / "logs" / "post_history.json", default=list)
    if not isinstance(history, list) or not history:
        return 0
    data = load()
    n = 0
    for rec in history:
        title = rec.get("title") or ""
        clean = rec.get("clean_title") or _DATE_PREFIX.sub("", title).strip()
        if not clean:
            continue
        try:
            cat = detect_category(clean)
        except (ImportError, AttributeError, TypeError, ValueError):
            cat = ""
        posted = (rec.get("posted_at") or "")[:10]
        _, key = upsert_post(title=title, clean_title=clean, url=rec.get("url") or "",
                             category=cat, keyword=clean, posted_at=posted, data=data)
        if key:
            n += 1
    save(data)
    log.info(f"[performance_db] 카탈로그 시드 완료 — {n}편")
    return n


# ─────────────────────────────────────────────────────
# 읽기/질의 API (리포트·대시보드·Phase 2 기회점수가 소비)
# ─────────────────────────────────────────────────────
def _recent_views(rec: dict, days: int) -> int:
    views = rec.get("views") or {}
    if not views:
        return 0
    cutoff = date.today() - timedelta(days=days - 1)
    total = 0
    for d, v in views.items():
        try:
            if date.fromisoformat(d) >= cutoff:
                total += int(v)
        except (ValueError, TypeError):
            continue
    return total


def top_posts_by_views(*, days: int = 7, n: int = 20,
                       data: dict | None = None) -> list[dict]:
    data = data or load()
    rows = []
    for key, rec in data["posts"].items():
        v = _recent_views(rec, days)
        if v <= 0:
            continue
        rows.append({"key": key, "title": rec.get("clean_title") or rec.get("title"),
                     "views": v, "category": rec.get("category", ""),
                     "url": rec.get("url", "")})
    rows.sort(key=lambda r: r["views"], reverse=True)
    return rows[:n]


def top_referral_keywords(*, n: int = 20, data: dict | None = None) -> list[dict]:
    """모든 글에 걸쳐 유입 검색어를 합산 → 상위 N. 실제 수요의 진실원본."""
    data = data or load()
    agg: dict[str, int] = {}
    for rec in data["posts"].values():
        for kw, c in (rec.get("referral_keywords") or {}).items():
            agg[kw] = agg.get(kw, 0) + int(c or 0)
    rows = [{"keyword": k, "visits": v} for k, v in agg.items()]
    rows.sort(key=lambda r: r["visits"], reverse=True)
    return rows[:n]


def adjacent_keyword_opportunities(*, n: int = 30,
                                   data: dict | None = None) -> list[dict]:
    """유입은 되는데 아직 '전용 글'이 없는 검색어 = 최고 ROI 확장 후보 (Phase 2 연료).

    referral_keywords 중, 어떤 글의 primary keyword 로도 잡히지 않은 것.
    """
    data = data or load()
    targeted = {normalize_key(r.get("keyword", "")) for r in data["posts"].values()
                if r.get("keyword")}
    agg: dict[str, int] = {}
    for rec in data["posts"].values():
        for kw, c in (rec.get("referral_keywords") or {}).items():
            if normalize_key(kw) in targeted:
                continue
            agg[kw] = agg.get(kw, 0) + int(c or 0)
    rows = [{"keyword": k, "visits": v} for k, v in agg.items()]
    rows.sort(key=lambda r: r["visits"], reverse=True)
    return rows[:n]


def rank_movement(*, days: int = 14, data: dict | None = None) -> list[dict]:
    """키워드별 최근 순위 변화(상승/하락). 최신 vs days 전 비교."""
    data = data or load()
    out = []
    cutoff = date.today() - timedelta(days=days)
    for rec in data["posts"].values():
        for kw, hist in (rec.get("rank_history") or {}).items():
            if not hist or _is_specific_hist(hist):
                continue  # 구체쿼리(blog_specific)는 기존 순위 변동 리포트서 제외
            sorted_h = sorted(hist, key=lambda h: h.get("date", ""))
            latest = sorted_h[-1]
            prev = None
            for h in sorted_h:
                try:
                    if date.fromisoformat(h["date"]) <= cutoff:
                        prev = h
                except (ValueError, TypeError, KeyError):
                    continue
            base = prev or sorted_h[0]
            delta = (base.get("rank", 0) - latest.get("rank", 0))  # +면 상승(순위 숫자↓)
            out.append({
                "keyword": kw, "title": rec.get("clean_title") or rec.get("title"),
                "rank": latest.get("rank"), "prev_rank": base.get("rank"),
                "delta": delta, "tab": latest.get("tab", "blog"),
                "date": latest.get("date"),
            })
    out.sort(key=lambda r: r["rank"] if r["rank"] else 999)
    return out


def visitor_series(*, days: int = 30, data: dict | None = None) -> list[dict]:
    data = data or load()
    vis = data.get("visitors", {})
    out = []
    for i in range(days - 1, -1, -1):
        d = (date.today() - timedelta(days=i)).strftime("%Y-%m-%d")
        out.append({"date": d, "count": (vis.get(d, {}) or {}).get("today", 0)})
    return out


def _latest_entry(hist: list) -> dict:
    """이력 리스트의 최신(날짜 최댓값) 엔트리. 비면 {}."""
    return sorted(hist, key=lambda h: h.get("date", ""))[-1] if hist else {}


def _is_specific_hist(hist: list) -> bool:
    """이 키워드 이력이 구체쿼리(blog_specific) 전용인지.

    한 키워드-이력은 tab 이 균질하다(헤드 키워드와 구체 쿼리는 서로 다른 dict 키라
    한 리스트에 섞이지 않는다) → 최신 엔트리의 tab 으로 전체를 판정할 수 있다.
    기존 소비자가 이 이력을 건너뛰게 해 회귀 0(레거시 데이터엔 blog_specific 자체가
    없으므로 이 가드는 no-op)을 보장한다.
    """
    return bool(hist) and _latest_entry(hist).get("tab") == SPECIFIC_TAB


def _best_rank(rec: dict) -> tuple[int | None, str]:
    """레코드의 최고(최소 숫자) 최신 순위와 그 키워드. 없으면 (None, "").

    구체쿼리(blog_specific) 이력은 제외 — 클릭 가로챔 용의자·워치 제안 등 기존
    소비자가 구체 쿼리 순위를 헤드 순위로 오인하지 않게 한다(near_miss_posts 전용).
    """
    best, best_kw = None, ""
    for kw, hist in (rec.get("rank_history") or {}).items():
        if not hist or _is_specific_hist(hist):
            continue
        r = sorted(hist, key=lambda h: h.get("date", ""))[-1].get("rank")
        if r and (best is None or r < best):
            best, best_kw = r, kw
    return best, best_kw


def _median(nums: list[int]) -> float:
    s = sorted(nums)
    if not s:
        return 0.0
    m = len(s) // 2
    return float(s[m]) if len(s) % 2 else (s[m - 1] + s[m]) / 2.0


def rank_view_gap(*, top_rank: int = 3, days: int = 14, low_ratio: float = 0.25,
                  n: int = 30, data: dict | None = None) -> dict:
    """'고순위인데 유입 바닥'인 글 = **AI요약이 클릭 가로챈 1순위 용의자** + AEO 리프레시 최우선 타깃.

    검색 상위노출(TOP top_rank)인데 실제 유입이 동급 고순위 글 중앙값의 low_ratio 미만이면,
    독자가 검색결과를 클릭하지 않고 AI 요약/스니펫만 보고 이탈했을 가능성이 크다. 이 글들은
    답을 더 자기완결적으로(AEO) 리프레시해 "요약에 인용되게" 만드는 게 최고 ROI.

    유입 데이터가 전무하면(전부 0) status="needs_view_data" — 방문자/통계 인제스트 활성화 대기.
    반환: {status, checked_ranked, peer_median, threshold, suspects:[...]}
    """
    data = data or load()
    ranked = []
    for key, rec in data["posts"].items():
        br, kw = _best_rank(rec)
        if br is None:
            continue
        ranked.append((key, rec, br, kw, _recent_views(rec, days)))

    if not ranked:
        return {"status": "no_rank_data", "checked_ranked": 0,
                "peer_median": 0, "threshold": 0, "suspects": []}

    top = [t for t in ranked if t[2] <= top_rank]
    peer_views = [t[4] for t in top if t[4] > 0]
    if not peer_views:
        # 순위는 있는데 유입이 전부 0 → 측정 활성화 대기(용의자 판별 불가)
        return {"status": "needs_view_data", "checked_ranked": len(ranked),
                "top_rank_posts": len(top), "peer_median": 0, "threshold": 0,
                "suspects": []}

    peer_median = _median(peer_views)
    threshold = max(1.0, peer_median * low_ratio)
    suspects = []
    measured = 0
    for key, rec, br, kw, v in top:
        # 조회수 데이터가 없는 글은 '미측정'이라 용의자 판정 보류(0=저유입으로 오인 금지).
        if not rec.get("views"):
            continue
        measured += 1
        if v < threshold:
            suspects.append({
                "key": key,
                "title": rec.get("clean_title") or rec.get("title"),
                "url": rec.get("url", ""),
                "best_rank": br, "keyword": kw,
                "recent_views": v,
                "gap": round(threshold - v, 1),
            })
    # 순위 좋을수록(가로채임 손해 큼)·유입 적을수록 먼저
    suspects.sort(key=lambda s: (s["best_rank"], s["recent_views"]))
    return {"status": "ok", "checked_ranked": len(ranked),
            "top_rank_posts": len(top), "measured_top_rank": measured,
            "unmeasured_top_rank": len(top) - measured,
            "peer_median": round(peer_median, 1),
            "threshold": round(threshold, 1), "suspects": suspects[:n]}


def near_miss_posts(*, low: int = 11, high: int = 30, days: int = 30,
                    n: int = 50, data: dict | None = None) -> list[dict]:
    """구체 롱테일 쿼리(blog_specific)에서 현재 low~high 위(1페이지 문턱)인 글.

    헤드(초경쟁·천장)가 아니라 글이 **실제 노출·트래픽을 얻는 구체 쿼리**(예
    '주거급여 지급일') 기준이라, 조금만 끌어올리면 1페이지에 드는 '리프레시 최우선
    타깃'을 준다. run_rank_tracking 이 tab="blog_specific" 로 기록한 데이터를 읽는
    **신규 소비자** — 기존 순위 소비자(rank_alerts·rank_watchlist·get_summary)와 분리.

    Args:
        low/high: 니어미스 순위 창(기본 11~30 = 1페이지 바로 밖).
        days: 최신 측정으로 인정할 최대 경과일(오래된 stale 측정 제외).
        n: 반환 상한.
    반환: [{key, title, url, keyword, rank, volume, estimated, date}] —
        **실측(estimated=False) 우선**, 그 다음 rank 오름차순(문턱에 가까운 순),
        동순위는 검색량 큰 순. estimated=True 는 검색광고 실측 볼륨 없이 제목 수식어로
        추정한 구체 쿼리(volume 0) — 다운스트림이 실측/추정을 구분해 실측을 먼저 쓴다.
    """
    data = data or load()
    if low > high:
        low, high = high, low
    cutoff = date.today() - timedelta(days=max(0, days - 1))
    out: list[dict] = []
    for key, rec in data["posts"].items():
        for kw, hist in (rec.get("rank_history") or {}).items():
            if not _is_specific_hist(hist):
                continue
            latest = _latest_entry(hist)
            r = int(latest.get("rank", 0) or 0)
            if not (low <= r <= high):
                continue
            try:
                if date.fromisoformat(latest.get("date", "")) < cutoff:
                    continue  # stale 측정 — 최근 창 밖이면 제외
            except (ValueError, TypeError):
                pass  # 날짜 파싱 실패는 관대하게 포함(fail-open)
            out.append({
                "key": key,
                "title": rec.get("clean_title") or rec.get("title"),
                "url": rec.get("url", ""),
                "keyword": kw,
                "rank": r,
                "volume": int(latest.get("volume", 0) or 0),
                "estimated": bool(latest.get("estimated", False)),
                "date": latest.get("date", ""),
            })
    # 실측 우선(estimated False<True) → rank 근접 → 검색량 큰 순
    out.sort(key=lambda x: (x["estimated"], x["rank"], -x["volume"]))
    return out[:n]


def _rank_kpi_min_volume() -> int:
    try:
        from config import settings as _s
        return int(getattr(_s, "RANK_KPI_MIN_VOLUME", 500))
    except (ImportError, AttributeError, ValueError):
        return 500


def get_summary(*, data: dict | None = None) -> dict:
    """리포트/대시보드용 일괄 요약.

    #7: 상위노출율(keywords_ranked/top10)은 **검색 수요(volume≥임계)가 있는 헤드
    키워드**만 센다. 제목 전체를 검색해 얻은 자기충족 1위(허영지표)는 volume=0 으로
    기록돼 keywords_unmeasured 로 분리된다 → KPI 가 실제 경쟁 노출을 반영.
    """
    data = data or load()
    posts = data["posts"]
    min_vol = _rank_kpi_min_volume()
    with_views = [r for r in posts.values() if r.get("views")]
    total_recent = sum(_recent_views(r, 7) for r in posts.values())

    def _latest(hist: list) -> dict:
        return sorted(hist, key=lambda h: h.get("date", ""))[-1] if hist else {}

    ranked = top10 = unmeasured = 0
    for r in posts.values():
        rh = r.get("rank_history") or {}
        # 구체쿼리(blog_specific) 이력은 상위노출율 KPI 에서 완전히 제외(신규 소비자
        # near_miss_posts 전용). 레거시 데이터엔 이 tab 이 없어 no-op → 회귀 0.
        rh = {kw: h for kw, h in rh.items() if not _is_specific_hist(h)}
        if not rh:
            continue
        # 검색량 보유(measured) 키워드 히스토리만 KPI 대상
        measured = [h for h in rh.values()
                    if h and _latest(h).get("volume", 0) >= min_vol]
        if measured:
            ranked += 1
            if any(_latest(h).get("rank", 999) <= 10 for h in measured):
                top10 += 1
        else:
            unmeasured += 1        # 무수요/옛 기록 = 허영지표에서 제외
    return {
        "total_posts": len(posts),
        "posts_with_views": len(with_views),
        "weekly_visits_from_posts": total_recent,
        "keywords_ranked": ranked,
        "keywords_top10": top10,
        "keywords_unmeasured": unmeasured,
        "top_posts": top_posts_by_views(days=7, n=10, data=data),
        "top_referrals": top_referral_keywords(n=15, data=data),
        "opportunities": adjacent_keyword_opportunities(n=15, data=data),
        "click_interception": rank_view_gap(n=15, data=data),
    }


if __name__ == "__main__":
    # 수동 실행: 카탈로그 시드 + 요약 출력
    import logging
    logging.basicConfig(level=logging.INFO)
    sync_from_history()
    s = get_summary()
    print(f"총 {s['total_posts']}편 · 조회수 있는 글 {s['posts_with_views']}편 · "
          f"순위추적 {s['keywords_ranked']}키워드 · TOP10 {s['keywords_top10']}")
