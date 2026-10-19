# core/work_queue.py — 무인 운영 지속 작업큐 (M1, 2026-09-18)
"""오늘 처리할(처리 중인) 발행 항목을 `work_queue.json` 에 지속시킨다.

상태 전이는 write-ahead(디스크 기록 후 행동) 원칙을 따른다:
`reserving`/`unconfirmed`/`missing` 은 어떤 경로로도 자동 재발행되지 않고,
`reserved`/`live` 로의 역행(뒤로 되돌리기)은 무시된다.

경로는 `config.profile.logs_dir()` 를 함수 호출 시점에 읽는다(모듈 상수로
굳히지 않는다). 이 파일은 draft_store 를 import 하지 않는다(순환·부작용 회피) —
원자 쓰기·PID 생존 확인을 자체 구현한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from config.logger import get_logger

log = get_logger(__name__)

SCHEMA = "nba/work_queue@1"

PUBLISH_STATES = (
    "none", "selected", "reserving", "unconfirmed", "reserved",
    "verifying", "live", "missing", "failed",
)
OPEN_STATES = ("reserving", "unconfirmed", "verifying")  # 사람 확인/대조 필요
_TERMINAL_STATES = ("live", "reserved", "failed")
_REGRESSION_GUARD = {  # 이 상태에서는 → 아래 목표상태로 절대 되돌리지 않는다
    "live": {"selected", "reserving", "none"},
    "reserved": {"selected", "reserving", "none"},
}

_QUEUE_FILE_NAME = "work_queue.json"
_LOCK_FILE_NAME = "work_queue.lock"
_DEFAULT_LOCK_TIMEOUT = 5.0

_local = threading.local()


# ─────────────────────────────────────────────────────
# 경로 · 원자 쓰기 · PID 생존 확인
# ─────────────────────────────────────────────────────
def _queue_path(profile: Optional[str] = None) -> Path:
    """큐 파일 경로. `profile` 은 **읽기 전용 조회**에서만 쓴다.

    쓰기 경로(`upsert_item`/`set_state`/`lease`/`release`/`purge_old`)는 여전히
    활성 프로필에만 쓴다 — "한 프로세스 = 한 프로필" 규약을 깨지 않기 위해서다.
    다른 계정의 상태를 **보여 주기만** 하는 화면(전체 계정 운영 현황)이 활성
    프로필을 바꾸지 않고도 읽을 수 있게 하는 것이 이 인자의 유일한 목적이다.
    """
    from config import profile as _profile

    return _profile.logs_dir(profile) / _QUEUE_FILE_NAME


def _lock_path() -> Path:
    from config import profile as _profile

    return _profile.logs_dir() / _LOCK_FILE_NAME


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".work_queue_", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except (OSError, TypeError, ValueError):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise


def _read_json(path: Path) -> Optional[dict]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"[work_queue] read failed {path.name}: {exc}")
        return None


def _pid_alive(pid: Optional[int]) -> bool:
    """PID 가 현재 살아 있는지 — auto_scheduler.PipelineLock._pid_alive 와 같은 규칙."""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFO
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_lock_owner(path: Path) -> Optional[int]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        return int(raw) if raw else None
    except (OSError, ValueError):
        return None


class _QueueLock:
    """work_queue.json 의 읽기-수정-쓰기 구간을 보호하는 락.

    같은 프로세스(같은 스레드) 안 재진입은 허용한다(스레드-로컬 깊이 카운터).
    다른 프로세스와는 O_CREAT|O_EXCL 파일로 배타하고, 죽은 PID 의 락은 회수한다.
    락 인프라 자체가 막히면(타임아웃) 파이프라인을 죽이지 않고 진행한다(fail-open) —
    단, `lease()`/`release()` 처럼 정확성이 중요한 연산은 이 락 없이도 파일 내용
    비교로 안전하게 동작하도록 설계돼 있다.
    """

    def __init__(self, timeout: float = _DEFAULT_LOCK_TIMEOUT):
        self._timeout = timeout
        self._acquired_here = False

    def __enter__(self):
        depth = getattr(_local, "depth", 0)
        if depth > 0:
            _local.depth = depth + 1
            self._acquired_here = False
            return self

        lock_path = _lock_path()
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout
        acquired = False
        while not acquired:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                try:
                    os.write(fd, str(os.getpid()).encode("utf-8"))
                finally:
                    os.close(fd)
                acquired = True
            except FileExistsError:
                owner = _read_lock_owner(lock_path)
                if owner is not None and not _pid_alive(owner):
                    try:
                        lock_path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() > deadline:
                    log.warning("[work_queue] 락 대기 시간 초과 — 락 없이 진행")
                    break
                time.sleep(0.02)
        self._acquired_here = acquired
        _local.depth = 1
        return self

    def __exit__(self, exc_type, exc, tb):
        depth = getattr(_local, "depth", 1) - 1
        _local.depth = depth
        if depth > 0:
            return False
        if self._acquired_here:
            lock_path = _lock_path()
            try:
                if _read_lock_owner(lock_path) == os.getpid():
                    lock_path.unlink()
            except OSError:
                pass
        return False


def _locked(timeout: float = _DEFAULT_LOCK_TIMEOUT) -> _QueueLock:
    return _QueueLock(timeout)


# ─────────────────────────────────────────────────────
# 기본 CRUD
# ─────────────────────────────────────────────────────
def make_item_id(day: str, title: str, slot_at: str) -> str:
    """결정론적 id — 같은 (day, title, slot_at) 이면 같은 id."""
    raw = f"{day}|{title}|{slot_at}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def load(*, profile: Optional[str] = None) -> dict:
    """큐 내용을 읽는다. `profile` 지정은 **읽기 전용**(다른 계정 현황 표시용)."""
    path = _queue_path(profile)
    data = _read_json(path)
    if not isinstance(data, dict):
        return {"schema": SCHEMA, "items": {}}
    data.setdefault("schema", SCHEMA)
    if not isinstance(data.get("items"), dict):
        data["items"] = {}
    return data


def _save(data: dict) -> None:
    _atomic_write_json(_queue_path(), data)


def list_items(*, day: str = "", states: tuple = (),
               profile: Optional[str] = None) -> list:
    """큐 항목 조회. `profile` 지정은 **읽기 전용**(활성 프로필을 바꾸지 않는다)."""
    data = load(profile=profile)
    items = list(data["items"].values())
    if day:
        items = [it for it in items if it.get("day") == day]
    if states:
        items = [it for it in items if it.get("publish_state") in states]
    items.sort(key=lambda it: (str(it.get("slot_at") or ""), str(it.get("item_id") or "")))
    return items


def get_item(item_id: str) -> Optional[dict]:
    data = load()
    return data["items"].get(item_id)


def upsert_item(item_id: str, **fields) -> dict:
    """없으면 생성(publish_state 기본 'selected'), 있으면 전달된 필드만 갱신."""
    with _locked():
        data = load()
        item = data["items"].get(item_id)
        now_iso = datetime.now().isoformat(timespec="seconds")
        if item is None:
            item = {
                "item_id": item_id,
                "publish_state": "selected",
                "reason": "",
                "lease": None,
                "history": [],
                "updated_at": now_iso,
            }
        item.update(fields)
        item["item_id"] = item_id
        item["updated_at"] = now_iso
        data["items"][item_id] = item
        _save(data)
        return item


def set_state(item_id: str, publish_state: str, **fields) -> dict:
    """write-ahead 기록. history 에 (시각, 이전상태, 새상태, reason) append.

    ⚠️ 역행 금지: live/reserved 를 selected/reserving/none 으로 되돌리려는 시도는
    무시(+ 경고 로그)하고 항목을 변경 없이 그대로 돌려준다.
    """
    if publish_state not in PUBLISH_STATES:
        raise ValueError(f"알 수 없는 publish_state: {publish_state!r}")

    with _locked():
        data = load()
        item = data["items"].get(item_id)
        now_iso = datetime.now().isoformat(timespec="seconds")
        if item is None:
            item = {
                "item_id": item_id,
                "publish_state": "none",
                "reason": "",
                "lease": None,
                "history": [],
                "updated_at": now_iso,
            }

        old_state = item.get("publish_state", "none")
        blocked = _REGRESSION_GUARD.get(old_state, set())
        if publish_state in blocked:
            log.warning(
                f"[work_queue] 상태 역행 시도 무시: {item_id} {old_state} → {publish_state}"
            )
            data["items"][item_id] = item
            return item

        for key, value in fields.items():
            item[key] = value
        item["publish_state"] = publish_state
        item["item_id"] = item_id
        item["updated_at"] = now_iso
        history = item.setdefault("history", [])
        history.append({
            "at": now_iso,
            "from": old_state,
            "to": publish_state,
            "reason": fields.get("reason", ""),
        })
        data["items"][item_id] = item
        _save(data)
        return item


# ─────────────────────────────────────────────────────
# 리스(lease) — 동시 프로세스 경합 방지
# ─────────────────────────────────────────────────────
def _parse_iso(raw) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def lease(item_id: str, *, owner_pid: Optional[int] = None, ttl_seconds: int = 1800) -> bool:
    """살아 있는 다른 PID 가 점유 중이면 False. TTL 만료 또는 소유 PID 사망이면 회수(steal).
    같은 PID 재진입은 True(갱신).
    """
    owner_pid = owner_pid if owner_pid is not None else os.getpid()
    with _locked():
        data = load()
        item = data["items"].get(item_id)
        if item is None:
            return False

        now = datetime.now()
        current = item.get("lease") or {}
        cur_owner = current.get("owner_pid")
        cur_expires = _parse_iso(current.get("expires_at"))

        if cur_owner and cur_owner != owner_pid:
            expired = cur_expires is None or now >= cur_expires
            alive = _pid_alive(cur_owner)
            if alive and not expired:
                return False  # 다른 살아있는 프로세스가 점유 중

        item["lease"] = {
            "owner_pid": owner_pid,
            "expires_at": (now + timedelta(seconds=ttl_seconds)).isoformat(timespec="seconds"),
        }
        item["updated_at"] = now.isoformat(timespec="seconds")
        data["items"][item_id] = item
        _save(data)
        return True


def release(item_id: str, *, owner_pid: Optional[int] = None) -> bool:
    owner_pid = owner_pid if owner_pid is not None else os.getpid()
    with _locked():
        data = load()
        item = data["items"].get(item_id)
        if item is None:
            return False
        current = item.get("lease") or {}
        if current.get("owner_pid") != owner_pid:
            return False
        item["lease"] = None
        item["updated_at"] = datetime.now().isoformat(timespec="seconds")
        data["items"][item_id] = item
        _save(data)
        return True


def stuck_items(*, now: Optional[datetime] = None) -> list:
    """publish_state 가 OPEN_STATES 이고 리스가 없거나 만료된 항목."""
    now = now or datetime.now()
    result = []
    for item in list_items(states=OPEN_STATES):
        current = item.get("lease") or {}
        owner = current.get("owner_pid")
        expires = _parse_iso(current.get("expires_at"))
        if not owner or expires is None or now >= expires:
            result.append(item)
    return result


# ─────────────────────────────────────────────────────
# 오늘 계획 (순수 함수)
# ─────────────────────────────────────────────────────
def plan_today(
    *,
    policy: dict,
    now: datetime,
    done_today: int = 0,
    budget_left: Optional[int] = None,
    catchup_max: Optional[int] = None,
) -> list:
    """오늘 처리할 슬롯 계획. **부수효과 없음(순수)** — 현재 큐 내용은 읽기만 한다.

    - 반환 [{"item_id","slot_at","publish_at","reason"}], slot_at 오름차순.
    - now 이전(도래한) 슬롯만 대상 — 그중 catchup_max(기본 policy['catchup_max'],
      절대 상한 2)를 넘는 만큼은 잘라낸다(가장 최근 슬롯 우선).
    - 아직 오지 않은 미래 슬롯은 포함하지 않는다(그 시각에 다시 호출된다).
    - 총 개수 <= min(daily_posts - done_today, budget_left(설정 시)).
    - 이미 큐에 있고 publish_state 가 live/reserved/failed 인 같은 슬롯은 제외(재실행 멱등).
    """
    from core import autonomy_policy as _ap

    day_str = now.date().isoformat()

    remaining = int(policy.get("daily_posts", 0)) - int(done_today)
    if remaining < 0:
        remaining = 0
    if budget_left is not None:
        remaining = min(remaining, int(budget_left))
    if remaining <= 0:
        return []

    if catchup_max is None:
        catchup_max = policy.get("catchup_max", 2)
    try:
        catchup_max = int(catchup_max)
    except (TypeError, ValueError):
        catchup_max = 0
    catchup_max = max(0, min(catchup_max, 2))
    if catchup_max == 0:
        return []

    windows = _ap.slots_for_day(policy, now.date())

    existing_by_slot: dict = {}
    for item in list_items(day=day_str):
        existing_by_slot.setdefault(item.get("slot_at"), []).append(item)

    due_slots = []
    for slot in windows:
        if slot > now:
            continue  # 미래 슬롯 — 그 시각에 다시 호출된다
        slot_iso = slot.isoformat(timespec="seconds")
        settled = any(
            it.get("publish_state") in _TERMINAL_STATES
            for it in existing_by_slot.get(slot_iso, [])
        )
        if settled:
            continue  # 이미 확정된 슬롯 — 재실행 멱등
        due_slots.append(slot)

    chosen = due_slots[-catchup_max:]
    chosen = chosen[:remaining]

    plan = []
    for slot in chosen:
        slot_iso = slot.isoformat(timespec="seconds")
        publish_at = _ap.publish_at_for(slot, policy, now=now)
        # 계획 단계엔 아직 제목이 없다(글감 선택은 이 함수 밖의 책임) — 슬롯 단위
        # 자리표시 id. 실제 항목은 제목이 정해진 뒤 make_item_id(day, title, slot_at) 로
        # 만들어 upsert_item 에 저장한다.
        placeholder_id = make_item_id(day_str, "", slot_iso)
        plan.append({
            "item_id": placeholder_id,
            "slot_at": slot_iso,
            "publish_at": publish_at.isoformat(timespec="seconds") if publish_at else None,
            "reason": "catchup" if slot < now else "due",
        })

    plan.sort(key=lambda p: p["slot_at"])
    return plan


def purge_old(*, keep_days: int = 30) -> int:
    """keep_days 보다 오래된 **종결 상태**(live/missing/failed) 항목만 정리한다.

    진행 중인 항목(선택/예약 시도/확인 대기)은 나이와 무관하게 보존한다 —
    조용히 사라지면 사용자가 문제를 놓친다.
    """
    with _locked():
        data = load()
        cutoff = datetime.now() - timedelta(days=keep_days)
        removed = 0
        for item_id in list(data["items"].keys()):
            item = data["items"][item_id]
            if item.get("publish_state") not in ("live", "missing", "failed"):
                continue
            ref = _parse_iso(item.get("updated_at"))
            if ref is not None and ref < cutoff:
                del data["items"][item_id]
                removed += 1
        if removed:
            _save(data)
        return removed


__all__ = [
    "SCHEMA",
    "PUBLISH_STATES",
    "OPEN_STATES",
    "make_item_id",
    "load",
    "list_items",
    "get_item",
    "upsert_item",
    "set_state",
    "lease",
    "release",
    "stuck_items",
    "plan_today",
    "purge_old",
]
