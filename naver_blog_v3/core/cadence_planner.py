# core/cadence_planner.py — 일캐던스 분산 스케줄러 (Phase 4 · L5)
"""8일마다 20편 몰아 발행(배치 덤프) → 매일 고르게 분산.

네이버 C-Rank 는 '꾸준한 발행'을 신선도 신호로 본다. 한 번에 쏟아붓는 배치는
오히려 신선도가 끊긴다. 이 모듈은 발행 큐(opportunity_score)를 여러 날·여러
시간 슬롯으로 분산 배치하고, 카테고리가 한 날에 몰리지 않게 인터리브한다.

- 순수 함수(items·설정 주입 가능) → 오프라인 테스트. 실제 예약은 사용자 트리거.
- 카테고리별 선호 시간대(affinity)로 슬롯 배정.
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)
BASE_DIR = Path(__file__).parent.parent

try:
    from config.settings import RESERVATION_TIME_SLOTS, POSTS_PER_DAY
except (ImportError, AttributeError):
    RESERVATION_TIME_SLOTS = ["07:00", "08:30", "10:00", "12:00", "13:30", "17:00", "19:30", "21:00"]
    POSTS_PER_DAY = 3

# 카테고리별 선호 시간대(추천피드·타깃 독자 활동시간). 없으면 균등 분산.
_CATEGORY_WINDOW = {
    "취업": ["08:30", "12:00"], "청년": ["12:00", "21:00"],
    "금융": ["08:30", "12:00"], "부동산": ["10:00", "21:00"],
    "복지": ["10:00", "13:30"], "노후": ["10:00", "13:30"],
    "주거": ["10:00", "19:30"], "바우처": ["10:00", "13:30"],
    "교육": ["13:30", "19:30"], "디지털": ["19:30", "21:00"],
    "의료": ["10:00", "13:30"], "환경": ["13:30", "17:00"],
}


def _interleave_by_category(items: list) -> list:
    """같은 카테고리가 연속/한날 몰리지 않게 라운드로빈 재배열."""
    buckets: dict[str, list] = {}
    for it in items:
        buckets.setdefault(it.get("category") or "기타", []).append(it)
    order = sorted(buckets, key=lambda c: -len(buckets[c]))  # 많은 카테고리부터
    out = []
    while any(buckets[c] for c in order):
        for c in order:
            if buckets[c]:
                out.append(buckets[c].pop(0))
    return out


def _slots_for_day(count: int, category_hint: str = "") -> list:
    """하루 count 편에 시간 슬롯 배정 — 하루 안에서도 아침·낮·저녁 분산."""
    slots = list(RESERVATION_TIME_SLOTS)
    if not slots:
        return ["09:00"] * count
    if count >= len(slots):
        picks = (slots * (count // len(slots) + 1))[:count]
    else:
        # 균등 간격으로 count 개 추출(아침~밤 고르게)
        step = len(slots) / count
        picks = [slots[min(int(i * step), len(slots) - 1)] for i in range(count)]
    return picks


def plan_cadence(items: list | None = None, *, posts_per_day: int | None = None,
                 days: int | None = None, start_date: date | None = None,
                 skip_weekends: bool = False) -> dict:
    """발행 큐를 날짜×슬롯으로 분산 배치.

    items: [{keyword, category, score?}] (None 이면 opportunity_score 큐 사용)
    반환: {"schedule":[{keyword,category,date,time,score}], "days","per_day","summary"}
    """
    posts_per_day = posts_per_day or POSTS_PER_DAY
    start_date = start_date or (date.today() + timedelta(days=1))  # 기본 내일부터

    if items is None:
        try:
            from core.opportunity_score import rank_publish_queue
            q = rank_publish_queue(top_n=posts_per_day * 7, use_miner=False)
            items = [{"keyword": r["keyword"], "category": r.get("category", ""),
                      "score": r.get("score", 0)} for r in q]
        except (ImportError, OSError, ValueError):
            items = []
    if not items:
        return {"schedule": [], "days": 0, "per_day": posts_per_day, "summary": {}}

    ordered = _interleave_by_category(items)
    schedule = []
    day_idx = 0
    i = 0
    while i < len(ordered):
        d = start_date + timedelta(days=day_idx)
        if skip_weekends and d.weekday() >= 5:
            day_idx += 1
            continue
        day_items = ordered[i:i + posts_per_day]
        slots = _slots_for_day(len(day_items))
        for it, slot in zip(day_items, slots):
            schedule.append({
                "keyword": it.get("keyword", ""),
                "category": it.get("category", ""),
                "date": d.isoformat(), "time": slot,
                "score": it.get("score", 0),
            })
        i += posts_per_day
        day_idx += 1
        if days and day_idx >= days:
            break

    used_days = len({s["date"] for s in schedule})
    from collections import Counter
    by_day = Counter(s["date"] for s in schedule)
    by_cat = Counter(s["category"] for s in schedule)
    return {
        "schedule": schedule, "days": used_days, "per_day": posts_per_day,
        "summary": {"total": len(schedule), "by_day": dict(by_day),
                    "by_category": dict(by_cat),
                    "avg_per_day": round(len(schedule) / used_days, 1) if used_days else 0},
    }


def to_reservation_briefs(plan: dict) -> list:
    """스케줄 → 예약 brief 리스트(publish_brief 호환 최소 필드). 실제 예약은 사용자."""
    out = []
    for s in plan.get("schedule", []):
        out.append({
            "keyword": s["keyword"], "category": s["category"],
            "schedule_at": f"{s['date']}T{s['time']}:00+09:00",
            "opportunity_score": s.get("score", 0),
            "human_approval_required": True,
        })
    return out


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    p = plan_cadence()
    print(f"분산 계획: {p['summary'].get('total',0)}편 / {p['days']}일 "
          f"(하루 평균 {p['summary'].get('avg_per_day',0)})")
    for s in p["schedule"][:12]:
        print(f"  {s['date']} {s['time']} [{s['category']}] {s['keyword']}")
