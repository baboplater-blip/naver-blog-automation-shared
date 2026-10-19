"""cost_ledger — 계정·머신 전역 **호출 수** 상한 원장 (M1~M4 무인 운영, 2026-09-18).

무인 운영이 하루에 얼마나 많은 LLM/이미지 호출을 쓸지 스스로 제한하기 위한 원장이다.
단위는 **호출 수**뿐이다(시간·토큰은 판정에 안 쓰는 관측 지표로 따로 쌓는다).

정책(계약 §0):
    - `settings.COST_LEDGER_ENABLED`(기본 False)가 꺼져 있으면 이 모듈은 아무 파일도
      만들지 않고 `acquire()`는 항상 True 를 반환한다(기존 동작과 동일).
    - 켜져 있을 때 `acquire()`는 **원자적 예약**이다 — 판정과 차감이 한 임계구역
      안에서 일어나 병렬 실행이 상한을 넘기지 못한다.
    - 계정 원장과 머신 전역 원장 **둘 다**를 통과해야 하고, 전역 예약 성공 후 계정
      예약이 실패하면 전역 예약을 되돌린다(부분 차감 금지).
    - 원장 파일이 손상됐거나 락을 못 잡으면 판정 불가 → **거절(False)**. fail-open 아님.

경로:
    계정 원장   profile.logs_dir(profile) / "cost_ledger.json"
    전역 원장   machine_paths.machine_file("cost_ledger.json")

⚠️ 경로/설정은 항상 함수 안에서 조회한다(모듈 상수로 굳히지 않음) — 테스트가
`NBA_MACHINE_ROOT` 환경변수와 `config.profile.logs_dir` monkeypatch 로 격리할 수 있어야
한다.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Optional

from config.logger import get_logger

log = get_logger(__name__)

SCHEMA = "nba/cost_ledger@1"
KINDS = ("write", "image", "vision")

# 파일 락 재시도 예산 — 테스트가 monkeypatch 로 줄여 빠르게 timeout 을 검증할 수 있다.
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_POLL_SECONDS = 0.02

# 같은 프로세스 안 여러 스레드의 동시 acquire/release/record_observation 을 완전히
# 직렬화한다(파일 락은 프로세스 간 경합용, 이 락은 프로세스 내부 경합용).
_RESERVE_LOCK = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────
# 게이트·경로
# ─────────────────────────────────────────────────────────────────────────
def _enabled() -> bool:
    """설정으로 켜져 있거나, 무인 운영 사이클 세션 안이면 항상 켜진다.

    비용 원장은 선택 기능이 아니라 무인 운영의 필수 보호다(2026-09-18 마감
    결함 B) — `settings.COST_LEDGER_ENABLED` 를 켜지 않은 설치라도
    `autonomy_guard.session()` 안에서는 이 원장이 실제로 상한을 강제한다.
    세션을 열지 않은 수동 경로(GUI 버튼·레거시 배치·테스트)는 이전과 완전히
    동일하게 설정값만 본다.
    """
    try:
        from config import settings as _s
        if bool(getattr(_s, "COST_LEDGER_ENABLED", False)):
            return True
    except Exception:  # noqa: BLE001 — settings 미로드 환경이면 무시하고 가드만 본다
        pass
    try:
        from core import autonomy_guard as _guard
        return _guard.guards_active()
    except Exception:  # noqa: BLE001 — 가드 모듈 미로드 환경이면 기존 동작(설정값만)
        return False


def _today_str() -> str:
    return date.today().isoformat()


def _profile_ledger_path(profile: str = "") -> Path:
    from config import profile as _pf
    return Path(_pf.logs_dir(profile or None)) / "cost_ledger.json"


def _global_ledger_path() -> Path:
    from core.machine_paths import machine_file
    return machine_file("cost_ledger.json")


def _global_limit_for(kind: str) -> Optional[int]:
    try:
        from config import settings as _s
        limits = getattr(_s, "COST_LEDGER_GLOBAL_LIMITS", {}) or {}
        if not isinstance(limits, dict):
            return None
        val = limits.get(kind)
        return int(val) if val is not None else None
    except Exception:  # noqa: BLE001 — 설정 조회 실패는 "미설정"과 동일하게 취급
        return None


def _kind_check(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"cost_ledger: 알 수 없는 kind {kind!r} (허용: {KINDS})")


# ─────────────────────────────────────────────────────────────────────────
# PID 생존 확인 (Windows/POSIX 공통 — auto_scheduler.PipelineLock._pid_alive 와 동일 규칙)
# ─────────────────────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
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


class _FileLock:
    """O_CREAT|O_EXCL 기반 임계구역 락. 죽은 PID 는 회수하고, 살아 있는 PID 는 timeout
    까지 기다리다 실패(False)한다 — 경합에서 밀리면 이 모듈 정책상 거절로 이어진다."""

    def __init__(self, target: Path, *, timeout: Optional[float] = None) -> None:
        self._lock_path = Path(str(target) + ".lock")
        self._held = False
        self._timeout = _LOCK_TIMEOUT_SECONDS if timeout is None else timeout

    def acquire(self) -> bool:
        deadline = time.monotonic() + max(0.0, self._timeout)
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        while True:
            try:
                fd = os.open(str(self._lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(str(os.getpid()))
                self._held = True
                return True
            except FileExistsError:
                owner_pid = self._read_owner()
                if owner_pid and _pid_alive(owner_pid):
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(_LOCK_POLL_SECONDS)
                    continue
                # 죽은 프로세스/판독 불가 락 → 회수 시도.
                try:
                    self._lock_path.unlink()
                except OSError:
                    if time.monotonic() >= deadline:
                        return False
                    time.sleep(_LOCK_POLL_SECONDS)
                continue
            except OSError:
                return False

    def _read_owner(self) -> int:
        try:
            raw = self._lock_path.read_text(encoding="utf-8").strip()
            return int(raw) if raw else 0
        except (OSError, ValueError):
            return 0

    def release(self) -> None:
        if not self._held:
            return
        try:
            if self._lock_path.exists():
                self._lock_path.unlink()
        except OSError:
            pass
        self._held = False


# ─────────────────────────────────────────────────────────────────────────
# 원장 파일 I/O — .bak 자동복원이 있는 config.safe_load_json 은 일부러 쓰지 않는다.
# 손상을 "복원 성공"으로 감추면 fail-closed 판정이 불가능해진다.
# ─────────────────────────────────────────────────────────────────────────
def _default_ledger() -> dict:
    return {"schema": SCHEMA, "limits": {}, "days": {}}


def _load_ledger(path: Path) -> tuple:
    """(ok, data). ok=False 는 "존재하지만 손상되어 판정 불가"(fail-closed 대상).
    파일이 아예 없으면 ok=True + 빈 기본 원장(정상 최초 상태)."""
    try:
        exists = path.exists()
    except OSError:
        return False, {}
    if not exists:
        return True, _default_ledger()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return False, {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False, {}
    if not isinstance(data, dict):
        return False, {}
    data.setdefault("schema", SCHEMA)
    if not isinstance(data.get("limits"), dict):
        data["limits"] = {}
    if not isinstance(data.get("days"), dict):
        data["days"] = {}
    return True, data


def _save_ledger(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cost_ledger_", suffix=".tmp", dir=str(path.parent))
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


def _day_bucket(data: dict, day: str) -> dict:
    bucket = data["days"].get(day)
    if not isinstance(bucket, dict):
        bucket = {}
        data["days"][day] = bucket
    if not isinstance(bucket.get("used"), dict):
        bucket["used"] = {}
    if not isinstance(bucket.get("observed"), dict):
        bucket["observed"] = {}
    return bucket


# ─────────────────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────────────────
def acquire(kind: str, n: int = 1, *, profile: str = "", day: str = "") -> bool:
    """계정·전역 한도를 둘 다 통과하면 원자적으로 예약(차감)하고 True.

    게이트 OFF → 항상 True(기록 없음). 손상/락 실패 → False(fail-closed).
    """
    _kind_check(kind)
    if n <= 0:
        return True
    if not _enabled():
        return True

    day = day or _today_str()
    profile_path = _profile_ledger_path(profile)
    global_path = _global_ledger_path()

    with _RESERVE_LOCK:
        g_lock = _FileLock(global_path)
        if not g_lock.acquire():
            log.warning("[cost_ledger] 전역 원장 락 획득 실패 — 거절")
            return False
        try:
            p_lock = _FileLock(profile_path)
            if not p_lock.acquire():
                log.warning("[cost_ledger] 계정 원장 락 획득 실패 — 거절")
                return False
            try:
                ok_g, gdata = _load_ledger(global_path)
                ok_p, pdata = _load_ledger(profile_path)
                if not ok_g or not ok_p:
                    log.warning("[cost_ledger] 원장 손상 감지 — 예약 거절(fail-closed)")
                    return False

                global_limit = _global_limit_for(kind)
                profile_limit = pdata.get("limits", {}).get(kind)

                g_bucket = _day_bucket(gdata, day)
                p_bucket = _day_bucket(pdata, day)
                g_used = int(g_bucket["used"].get(kind, 0) or 0)
                p_used = int(p_bucket["used"].get(kind, 0) or 0)

                if global_limit is not None and g_used + n > int(global_limit):
                    return False
                if profile_limit is not None and p_used + n > int(profile_limit):
                    return False

                # 전역을 먼저 예약한다 — 실패하면 아직 아무 것도 확정 안 됐으니 되돌릴 게 없다.
                g_bucket["used"][kind] = g_used + n
                try:
                    _save_ledger(global_path, gdata)
                except OSError:
                    log.warning("[cost_ledger] 전역 원장 저장 실패 — 거절")
                    return False

                p_bucket["used"][kind] = p_used + n
                try:
                    _save_ledger(profile_path, pdata)
                except OSError:
                    # 부분 차감 방지 — 전역 예약을 되돌린다.
                    g_bucket["used"][kind] = g_used
                    try:
                        _save_ledger(global_path, gdata)
                    except OSError:
                        log.error("[cost_ledger] 전역 원장 롤백 실패 — 원장 불일치 가능성")
                    log.warning("[cost_ledger] 계정 원장 저장 실패 — 전역 예약 롤백")
                    return False
                return True
            finally:
                p_lock.release()
        finally:
            g_lock.release()


def release(kind: str, n: int = 1, *, profile: str = "", day: str = "") -> None:
    """예약 취소(실제로 쓰지 않았을 때). 음수로 내려가지 않는다."""
    _kind_check(kind)
    if n <= 0 or not _enabled():
        return

    day = day or _today_str()
    profile_path = _profile_ledger_path(profile)
    global_path = _global_ledger_path()

    with _RESERVE_LOCK:
        g_lock = _FileLock(global_path)
        if not g_lock.acquire():
            return
        try:
            p_lock = _FileLock(profile_path)
            if not p_lock.acquire():
                return
            try:
                ok_g, gdata = _load_ledger(global_path)
                if ok_g:
                    bucket = _day_bucket(gdata, day)
                    used = int(bucket["used"].get(kind, 0) or 0)
                    bucket["used"][kind] = max(0, used - n)
                    try:
                        _save_ledger(global_path, gdata)
                    except OSError:
                        log.warning("[cost_ledger] release: 전역 원장 저장 실패")

                ok_p, pdata = _load_ledger(profile_path)
                if ok_p:
                    bucket = _day_bucket(pdata, day)
                    used = int(bucket["used"].get(kind, 0) or 0)
                    bucket["used"][kind] = max(0, used - n)
                    try:
                        _save_ledger(profile_path, pdata)
                    except OSError:
                        log.warning("[cost_ledger] release: 계정 원장 저장 실패")
            finally:
                p_lock.release()
        finally:
            g_lock.release()


def remaining(kind: str, *, profile: str = "") -> dict:
    """{"profile_left": int|None, "global_left": int|None}. 한도 미설정이면 None."""
    _kind_check(kind)
    day = _today_str()
    profile_left: Optional[int] = None
    global_left: Optional[int] = None

    ok_p, pdata = _load_ledger(_profile_ledger_path(profile))
    if ok_p:
        limit = pdata.get("limits", {}).get(kind)
        if limit is not None:
            used = int(_day_bucket(pdata, day)["used"].get(kind, 0) or 0)
            profile_left = max(0, int(limit) - used)

    ok_g, gdata = _load_ledger(_global_ledger_path())
    if ok_g:
        limit = _global_limit_for(kind)
        if limit is not None:
            used = int(_day_bucket(gdata, day)["used"].get(kind, 0) or 0)
            global_left = max(0, int(limit) - used)

    return {"profile_left": profile_left, "global_left": global_left}


def limits(*, profile: str = "") -> dict:
    """현재 적용 한도. {"profile": {kind: int|None, ...}, "global": {kind: int|None, ...}}"""
    ok_p, pdata = _load_ledger(_profile_ledger_path(profile))
    profile_limits = pdata.get("limits", {}) if ok_p else {}
    return {
        "profile": {k: profile_limits.get(k) for k in KINDS},
        "global": {k: _global_limit_for(k) for k in KINDS},
    }


def set_limits(*, profile: str = "", **kinds) -> dict:
    """계정 한도 저장(정책의 budget 반영). 게이트 OFF 면 파일을 만들지 않고 입력만 반영해 반환."""
    if not _enabled():
        return {k: (int(v) if v is not None else None) for k, v in kinds.items()}

    path = _profile_ledger_path(profile)
    with _RESERVE_LOCK:
        lock = _FileLock(path)
        if not lock.acquire():
            raise RuntimeError("cost_ledger.set_limits: 계정 원장 락 획득 실패")
        try:
            ok, data = _load_ledger(path)
            if not ok:
                data = _default_ledger()
            for k, v in kinds.items():
                if v is None:
                    data["limits"].pop(k, None)
                else:
                    data["limits"][k] = int(v)
            _save_ledger(path, data)
            return dict(data["limits"])
        finally:
            lock.release()


def record_observation(kind: str, *, seconds: Optional[float] = None,
                        tokens: Optional[int] = None, profile: str = "",
                        day: str = "") -> None:
    """상한과 무관한 관측 지표(소요 시간·토큰)를 누적. 측정 안 된 값은 None 으로 둔다(0 아님)."""
    _kind_check(kind)
    if not _enabled():
        return
    if seconds is None and tokens is None:
        return

    day = day or _today_str()
    path = _profile_ledger_path(profile)
    with _RESERVE_LOCK:
        lock = _FileLock(path)
        if not lock.acquire():
            log.warning("[cost_ledger] record_observation: 락 획득 실패 — 스킵")
            return
        try:
            ok, data = _load_ledger(path)
            if not ok:
                data = _default_ledger()
            bucket = _day_bucket(data, day)
            obs = bucket["observed"].get(kind)
            if not isinstance(obs, dict):
                obs = {}
                bucket["observed"][kind] = obs
            if seconds is not None:
                obs["seconds_sum"] = float(obs.get("seconds_sum", 0.0) or 0.0) + float(seconds)
                obs["seconds_n"] = int(obs.get("seconds_n", 0) or 0) + 1
            if tokens is not None:
                obs["tokens_sum"] = int(obs.get("tokens_sum", 0) or 0) + int(tokens)
                obs["tokens_n"] = int(obs.get("tokens_n", 0) or 0) + 1
            try:
                _save_ledger(path, data)
            except OSError:
                log.warning("[cost_ledger] record_observation: 저장 실패")
        finally:
            lock.release()


def snapshot(*, profile: str = "") -> dict:
    """GUI 표시용 {"day","used","limits","observed"}."""
    day = _today_str()
    used: dict = {}
    observed: dict = {}

    ok, data = _load_ledger(_profile_ledger_path(profile))
    if ok:
        bucket = _day_bucket(data, day)
        used = dict(bucket["used"])
        for k, v in bucket["observed"].items():
            if not isinstance(v, dict):
                continue
            seconds_n = int(v.get("seconds_n", 0) or 0)
            tokens_n = int(v.get("tokens_n", 0) or 0)
            observed[k] = {
                "seconds_sum": v.get("seconds_sum"),
                "seconds_n": v.get("seconds_n"),
                "seconds_avg": (v.get("seconds_sum", 0.0) / seconds_n) if seconds_n else None,
                "tokens_sum": v.get("tokens_sum"),
                "tokens_n": v.get("tokens_n"),
                "tokens_avg": (v.get("tokens_sum", 0) / tokens_n) if tokens_n else None,
            }

    return {"day": day, "used": used, "limits": limits(profile=profile), "observed": observed}


__all__ = [
    "SCHEMA", "KINDS",
    "acquire", "release", "remaining", "limits", "set_limits",
    "record_observation", "snapshot",
]
