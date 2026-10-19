# core/rank_watchlist.py — 순위 모니터 워치리스트 (순위 모니터 P0, 2026-07-19)
"""데이터랩툴즈식 '내 글 순위 추적'의 등록 저장소.

글×키워드를 등록해 두면 scheduler/rank_monitor 일간 루프가 매일 순위를 재고,
serp_rank(P1)가 통합검색 실노출을 보정하며, rank_alerts 가 변동을 카카오
한 줄로 알린다. 설계: reports/2026-07-19_rank_monitor_design.html

- 저장: logs/rank_watchlist.json (활성 프로필 기준)
- 상한 50개 — 통합검색 실측(P1)의 저빈도 원칙과 맞물린 상한
- suggest_candidates: performance_db 에서 '추적 가치' 높은 순 자동 제안
  (클릭 가로챔 용의자 최우선 → TOP20 노출 × 검색량 큰 순)
- rows: GUI/리포트용 — 키워드별 어제·오늘·7일 추이·실노출을 한 번에
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

from config import profile as _nbaprofile

_DATA_DIR = _nbaprofile.app_data_root()
WATCHLIST_PATH = _DATA_DIR / "logs" / "rank_watchlist.json"
MAX_ITEMS = 50

# 7일 추이 스파크 문자(순위 1위가 가장 높게). 미측정은 '·'.
_SPARK = "█▇▆▅▄▃▂▁"


def _load() -> dict:
    from config import safe_load_json
    data = safe_load_json(WATCHLIST_PATH, default=dict)
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        return {"items": []}
    return data


def _save(data: dict) -> None:
    from config import atomic_write_json
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(WATCHLIST_PATH), data)


def items() -> list[dict]:
    return _load()["items"]


def keywords() -> list[str]:
    return [it.get("keyword", "") for it in items() if it.get("keyword")]


def add(keyword: str, *, title: str = "", title_key: str = "",
        volume: int | None = None) -> tuple[bool, str]:
    """워치 등록. (ok, message). 중복·상한은 거부."""
    keyword = (keyword or "").strip()
    if not keyword:
        return False, "키워드가 비어 있어요"
    data = _load()
    if any(it.get("keyword") == keyword for it in data["items"]):
        return False, f"이미 등록됨: {keyword}"
    if len(data["items"]) >= MAX_ITEMS:
        return False, f"상한 {MAX_ITEMS}개 — 먼저 하나를 해제하세요"
    if volume is None:
        try:
            from core.search_volume import best_volume
            volume = best_volume(keyword)
        except Exception:  # noqa: BLE001 — 볼륨 미상이어도 등록은 유효
            volume = 0
    data["items"].append({
        "keyword": keyword, "title": title, "title_key": title_key,
        "added": date.today().strftime("%Y-%m-%d"),
        "volume": int(volume or 0), "alert": True,
    })
    _save(data)
    log.info(f"[watchlist] 등록: {keyword} (총 {len(data['items'])}개)")
    return True, f"등록: {keyword}"


def remove(keyword: str) -> tuple[bool, str]:
    data = _load()
    before = len(data["items"])
    data["items"] = [it for it in data["items"] if it.get("keyword") != keyword]
    if len(data["items"]) == before:
        return False, f"목록에 없음: {keyword}"
    _save(data)
    log.info(f"[watchlist] 해제: {keyword}")
    return True, f"해제: {keyword}"


# ─────────────────────────────────────────────────────
# 자동 후보 제안 — 추적 가치 높은 순
# ─────────────────────────────────────────────────────
def suggest_candidates(n: int = 15, *, data: dict | None = None) -> list[dict]:
    """performance_db 에서 워치 후보를 제안한다(등록은 사용자/호출측 몫).

    우선순위: ①클릭 가로챔 용의자(고순위·저유입 — 리프레시 1순위라 추적 필수)
    ②TOP20 노출 키워드를 검색량 큰 순. 이미 등록된 키워드는 제외.
    """
    from core import performance_db as pdb

    data = data or pdb.load()
    watched = set(keywords())
    out, seen = [], set()

    # ① 클릭 가로챔 용의자
    try:
        gap = pdb.rank_view_gap(data=data)
        for s in (gap.get("suspects") or [])[:n]:
            kw = (s.get("keyword") or "").strip()
            if kw and kw not in watched and kw not in seen:
                seen.add(kw)
                out.append({"keyword": kw, "title": s.get("title", ""),
                            "volume": 0, "best_rank": s.get("best_rank", 0),
                            "reason": "클릭 가로챔 용의자"})
    except Exception as e:  # noqa: BLE001 — 제안은 어떤 실패에도 계속
        log.debug(f"[watchlist] gap 후보 실패(무시): {e}")

    # ② TOP20 노출 × 검색량 — volume>0 필수. 검색량 0짜리 rank_history 키는
    #    대부분 '제목 전체'(#7 허영지표 시대 기록)라 추적 가치가 없다(2026-07-19 실측).
    ranked = []
    for key, rec in data.get("posts", {}).items():
        for kw, hist in (rec.get("rank_history") or {}).items():
            if not hist or kw in watched or kw in seen:
                continue
            latest = sorted(hist, key=lambda h: h.get("date", ""))[-1]
            if latest.get("tab") == pdb.SPECIFIC_TAB:
                continue  # 구체쿼리 이력은 니어미스(near_miss_posts) 전용 — 워치 제안서 제외
            r = int(latest.get("rank", 0) or 0)
            vol = int(latest.get("volume", 0) or 0)
            if 0 < r <= 20 and vol > 0:
                ranked.append({"keyword": kw, "title": rec.get("clean_title", ""),
                               "volume": vol,
                               "best_rank": r, "reason": f"TOP{r} 노출"})
    ranked.sort(key=lambda x: (-x["volume"], x["best_rank"]))
    for c in ranked:
        if len(out) >= n:
            break
        if c["keyword"] not in seen:
            seen.add(c["keyword"])
            out.append(c)
    return out[:n]


# ─────────────────────────────────────────────────────
# 측정 이력 (워치리스트 자체 저장 — 글 앵커 없이도 표시 가능)
# ─────────────────────────────────────────────────────
_HISTORY_CAP = 30    # 일 단위 30개


def record_measurement(keyword: str, *, day: str = "",
                       api_rank: int | None = None,
                       serp_rank: int | None = None) -> bool:
    """워치 항목에 일간 측정값 기록(upsert). 등록 안 된 키워드면 False.

    performance_db 는 '글' 단위라 워치 키워드가 어떤 글의 대표 키워드가 아니면
    저장할 곳이 없다(2026-07-19 라이브 실측) — 워치리스트가 자체 이력을 갖는다.
    """
    day = day or date.today().strftime("%Y-%m-%d")
    data = _load()
    for it in data["items"]:
        if it.get("keyword") != keyword:
            continue
        hist = it.setdefault("history", [])
        entry = next((h for h in hist if h.get("date") == day), None)
        if entry is None:
            entry = {"date": day}
            hist.append(entry)
        if api_rank is not None:
            entry["api"] = int(api_rank)
        if serp_rank is not None:
            entry["serp"] = int(serp_rank)
        hist.sort(key=lambda h: h.get("date", ""))
        del hist[:-_HISTORY_CAP]
        _save(data)
        return True
    return False


def _item_day(it: dict, day: str, field: str) -> int | None:
    for h in it.get("history", []):
        if h.get("date") == day:
            v = h.get(field)
            return int(v) if v is not None else None
    return None


# ─────────────────────────────────────────────────────
# GUI/리포트용 행 데이터
# ─────────────────────────────────────────────────────
def _day_rank(hist: list, day: str, tab: str) -> int:
    """해당 날짜·탭의 순위. 없으면 0."""
    for h in hist:
        if h.get("date") == day and h.get("tab", "blog") == tab:
            return int(h.get("rank", 0) or 0)
    return 0


def _best_hist_for_keyword(data: dict, keyword: str) -> list:
    """이 키워드를 추적 중인 글들 중 '가장 좋은 최신 순위'를 가진 글의 이력."""
    from core.performance_db import SPECIFIC_TAB

    best_hist, best_rank = [], 10 ** 9
    for rec in data.get("posts", {}).values():
        hist = (rec.get("rank_history") or {}).get(keyword)
        if not hist:
            continue
        latest = sorted(hist, key=lambda h: h.get("date", ""))[-1]
        if latest.get("tab") == SPECIFIC_TAB:
            continue  # 구체쿼리 이력은 워치 표시(rows)의 blog/total 탭 소스가 아님
        r = int(latest.get("rank", 0) or 0) or 10 ** 8   # 미노출은 뒤로
        if r < best_rank:
            best_rank, best_hist = r, hist
    return best_hist


def _merged_day(it: dict, hist: list, day: str, field: str, tab: str) -> int:
    """하루치 순위 — 워치 자체 이력 우선, 없으면 pdb rank_history 폴백. 0=미측정."""
    v = _item_day(it, day, field)
    if v is not None:
        return v
    return _day_rank(hist, day, tab)


def _spark(it: dict, hist: list, days: int) -> str:
    """최근 days 일의 API 순위 추이 문자열. 1위=█ … 30위+=▁, 미측정=·"""
    today = date.today()
    out = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        r = _merged_day(it, hist, d, "api", "blog")
        if r <= 0:
            out.append("·")
        else:
            idx = min(len(_SPARK) - 1, (r - 1) * len(_SPARK) // 30)
            out.append(_SPARK[idx])
    return "".join(out)


def rows(days: int = 7, *, data: dict | None = None) -> list[dict]:
    """워치리스트 각 항목의 표시용 행.

    {keyword, volume, today, yesterday, delta, serp_today, spark}
    today/yesterday: API 순위(워치 자체 이력 우선 → pdb blog 탭 폴백), 0=미측정/미노출.
    serp_today: 통합검색 실노출(자체 이력 → pdb total 탭), 0=미측정.
    """
    from core import performance_db as pdb

    data = data or pdb.load()
    t = date.today().strftime("%Y-%m-%d")
    y = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    out = []
    for it in items():
        kw = it.get("keyword", "")
        hist = _best_hist_for_keyword(data, kw)
        today_r = _merged_day(it, hist, t, "api", "blog")
        yest_r = _merged_day(it, hist, y, "api", "blog")
        delta = (yest_r - today_r) if (today_r and yest_r) else None
        out.append({
            "keyword": kw,
            "volume": int(it.get("volume", 0) or 0),
            "today": today_r, "yesterday": yest_r, "delta": delta,
            "serp_today": _merged_day(it, hist, t, "serp", "total"),
            "spark": _spark(it, hist, days),
        })
    return out
