# core/reservation_ledger.py — 예약 대장(단일 진실원) (2026-07-04)
"""예약 발행 슬롯을 영속 대장에 기록해 '가장 먼 예약 다음날' 이월을 보장한다.

배경: assign_reservation_times 의 예약 이월(가장 먼 예약일 +1)은 원래 runs/*/publish_brief.yaml
스캔에만 의존했다. 그 파일이 정리되거나 다른 환경이면 미래 예약을 못 봐 '다음날 고정'으로
폴백된다(사용자 관측 증상). 이 대장은 예약 성공 시마다 append 되는 단일 진실원으로,
brief 유무와 무관하게 이월을 신뢰성 있게 만든다.

- logs/reservations.json 에 누적. 과거 슬롯은 조회 시 자동 프루닝.
- fail-open: 손상/부재 시 빈 리스트. 부수효과=파일 쓰기만(발행 아님).
- assign_reservation_times 는 brief 스캔 ∪ 이 대장을 합쳐 max(date) 를 취한다.
"""
from __future__ import annotations

import os
import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import atomic_write_json, safe_load_json
from config.logger import get_logger

log = get_logger(__name__)
BASE_DIR = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

LEDGER_PATH = _DATA_DIR / "logs" / "reservations.json"

# "2026-07-07 09:00" | "2026-07-07T09:00:00+09:00" | "2026-07-07"
_ISO_TZ = re.compile(r"[T ](\d{2}):(\d{2})")


def _parse(schedule_at: str) -> datetime | None:
    """schedule_at 문자열 → naive datetime(로컬/KST 취급). 실패 시 None."""
    if not schedule_at:
        return None
    sa = str(schedule_at).strip()
    m_date = re.match(r"(\d{4})-(\d{2})-(\d{2})", sa)
    if not m_date:
        return None
    y, mo, d = (int(x) for x in m_date.groups())
    hh, mm = 0, 0
    m_time = _ISO_TZ.search(sa)
    if m_time:
        hh, mm = int(m_time.group(1)), int(m_time.group(2))
    try:
        return datetime(y, mo, d, hh, mm)
    except ValueError:
        return None


def _load() -> list[dict]:
    data = safe_load_json(LEDGER_PATH, default=dict)
    if not isinstance(data, dict):
        return []
    rows = data.get("reservations", [])
    return rows if isinstance(rows, list) else []


def _save(rows: list[dict]) -> None:
    try:
        atomic_write_json(LEDGER_PATH, {
            "reservations": rows, "count": len(rows),
            "updated_at": datetime.now().isoformat(timespec="seconds")})
    except OSError as e:
        log.warning(f"[reservation_ledger] 저장 실패: {e}")


def record(schedule_at: str, *, slug: str = "", category: str = "") -> bool:
    """예약 슬롯을 대장에 기록. 같은 (slug, schedule_at) 중복이면 no-op.

    schedule_at 이 파싱 불가면 무시(False). 성공 시 True.
    """
    dt = _parse(schedule_at)
    if dt is None:
        return False
    rows = _load()
    key = (slug or "", str(schedule_at).strip())
    for r in rows:
        if (r.get("slug", ""), str(r.get("schedule_at", "")).strip()) == key:
            return False  # 이미 기록됨
    rows.append({"schedule_at": str(schedule_at).strip(), "slug": slug,
                 "category": category or "default",
                 "recorded_at": datetime.now().isoformat(timespec="seconds")})
    _save(rows)
    return True


def future_reservations(*, now: datetime | None = None,
                        prune: bool = True) -> list[dict]:
    """미래 예약 슬롯 목록. 과거는 제외(+ prune=True면 대장에서 정리).

    반환: [{"datetime","date","time","category","slug"}], datetime 오름차순.
    """
    now_dt = now or datetime.now()
    rows = _load()
    fut, kept = [], []
    changed = False
    for r in rows:
        dt = _parse(r.get("schedule_at", ""))
        if dt is None:
            changed = True            # 손상 항목 정리
            continue
        if dt < now_dt:
            changed = True            # 과거 → 프루닝 대상
            continue
        kept.append(r)
        fut.append({"datetime": dt, "date": dt.date(),
                    "time": dt.strftime("%H:%M"),
                    "category": r.get("category") or "default",
                    "slug": r.get("slug") or ""})
    if prune and changed:
        _save(kept)
    fut.sort(key=lambda x: x["datetime"])
    return fut


def last_reserved_date(*, now: datetime | None = None) -> date | None:
    """미래 예약 중 가장 먼 날짜(없으면 None)."""
    fut = future_reservations(now=now, prune=False)
    return max((r["date"] for r in fut), default=None)
