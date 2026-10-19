"""machine_arbiter — 머신 전역 브라우저 자원 중재 (M1~M4 무인 운영, 2026-09-18).

무인 파이프라인과 사용자가 손으로 연 크롬(attach 모드)이 같은 머신에서 동시에
브라우저를 건드리면 세션이 꼬인다. `browser_slot()` 은 `machine_paths.machine_file
("browser.lock")` 하나로 머신 전체를 배타한다.

절대 규칙: **살아 있는 PID 의 락은 절대 뺏지 않는다** — 사용자가 열어 둔 창을
보호하기 위해서다. 죽은 PID 가 남긴 stale 락만 회수한다.

`settings.BROWSER_ARBITER_ENABLED`(기본 False) 가 꺼져 있으면 `browser_slot()` 은
파일을 만들지 않는 **no-op 컨텍스트**(항상 진입)다 — 기존 발행 경로가 이 모듈을
몰라도 그대로 동작해야 한다.
"""
from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.logger import get_logger

log = get_logger(__name__)


class BrowserBusy(Exception):
    """browser_slot() 획득 실패(다른 프로세스가 점유 중, 또는 timeout 경과)."""


def _enabled() -> bool:
    """설정으로 켜져 있거나, 무인 운영 사이클 세션 안이면 항상 켜진다.

    브라우저 중재는 선택 기능이 아니라 무인 운영의 필수 보호다(2026-09-18 마감
    결함 B) — `settings.BROWSER_ARBITER_ENABLED` 를 켜지 않은 설치라도
    `autonomy_guard.session()` 안에서는 실제로 배타를 강제한다. 세션을 열지
    않은 수동 경로는 이전과 완전히 동일하게 설정값만 본다.
    """
    try:
        from config import settings as _s
        if bool(getattr(_s, "BROWSER_ARBITER_ENABLED", False)):
            return True
    except Exception:  # noqa: BLE001 — settings 미로드 환경이면 무시하고 가드만 본다
        pass
    try:
        from core import autonomy_guard as _guard
        return _guard.guards_active()
    except Exception:  # noqa: BLE001 — 가드 모듈 미로드 환경이면 기존 동작(설정값만)
        return False


def _lock_path() -> Path:
    from core.machine_paths import machine_file
    return machine_file("browser.lock")


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _pid_alive(pid: int) -> bool:
    """PID 생존 확인(Windows/POSIX 공통 — auto_scheduler.PipelineLock._pid_alive 규칙)."""
    if pid <= 0:
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


def _read_holder_raw(path: Path) -> Optional[dict]:
    """락 파일 내용을 읽는다. 없거나 깨졌으면 None(판독 불가)."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _holder_pid(data: Optional[dict]) -> int:
    if not data:
        return 0
    try:
        return int(data.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


# ─────────────────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────────────────
def holder() -> Optional[dict]:
    """{"pid","owner","since"} — 현재 살아 있는 점유자. 없거나 죽었으면 None."""
    path = _lock_path()
    try:
        if not path.exists():
            return None
    except OSError:
        return None
    data = _read_holder_raw(path)
    pid = _holder_pid(data)
    if not pid or not _pid_alive(pid):
        return None
    return {"pid": pid, "owner": (data or {}).get("owner", ""), "since": (data or {}).get("since")}


def force_release(*, only_if_dead: bool = True) -> bool:
    """락 강제 해제. only_if_dead=True(기본)면 살아 있는 PID 의 락은 건드리지 않고 False."""
    path = _lock_path()
    try:
        exists = path.exists()
    except OSError:
        return False
    if not exists:
        return True
    data = _read_holder_raw(path)
    pid = _holder_pid(data)
    if only_if_dead and pid and _pid_alive(pid):
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


@contextmanager
def browser_slot(timeout_seconds: int = 0, *, owner: str = ""):
    """머신 전역 배타 컨텍스트. 게이트 OFF 면 파일 접촉 없는 no-op(항상 진입)."""
    if not _enabled():
        yield
        return

    path = _lock_path()
    deadline = time.monotonic() + max(0, timeout_seconds)
    acquired = False

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrowserBusy(f"lock 디렉터리 생성 실패: {exc}") from exc

    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            payload = json.dumps(
                {"pid": os.getpid(), "owner": owner, "since": _now_iso()},
                ensure_ascii=False,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(payload)
            acquired = True
            break
        except FileExistsError:
            data = _read_holder_raw(path)
            owner_pid = _holder_pid(data)
            if owner_pid and _pid_alive(owner_pid):
                # ★살아 있는 PID 의 락은 절대 뺏지 않는다 — timeout 까지만 대기.
                if time.monotonic() >= deadline:
                    raise BrowserBusy(f"브라우저 자원이 다른 프로세스(pid={owner_pid})에 점유됨")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())) or 0.01)
                continue
            # 죽은 PID/판독 불가 락 → stale 회수.
            try:
                path.unlink()
            except OSError:
                pass
            continue
        except OSError as exc:
            raise BrowserBusy(f"lock 파일 생성 실패: {exc}") from exc

    try:
        yield
    finally:
        if acquired:
            try:
                data = _read_holder_raw(path)
                # 자기 락만 해제(남의 락은 지우지 않는다).
                if data is None or _holder_pid(data) == os.getpid():
                    if path.exists():
                        path.unlink()
            except (OSError, TypeError, ValueError) as exc:  # noqa: BLE001
                log.warning(f"[machine_arbiter] 락 해제 실패({type(exc).__name__})")


__all__ = ["BrowserBusy", "browser_slot", "holder", "force_release"]
