"""autonomy_guard — 무인 운영 **마스터 스위치 지속화 + 필수 보호 자동 적용** (2026-09-18 보완).

이 모듈이 해결하는 두 가지 마감 결함:

1. **"GUI 에서 켰는데 아무 일도 안 한다"** — 예전에는 `settings.AUTONOMY_ENABLED` 를
   사용자가 직접 `config/settings.py` 에서 편집해야 무인 운영이 살아났다. GUI 는
   계정 정책(`autonomy.json`)만 켜므로 화면에는 "켜짐"인데 실제로는 마스터가 꺼져
   있어 재시작·CLI 어디서도 돌지 않는 상태가 나올 수 있었다.
   → `set_master()` 가 **머신 공용 파일**(`autonomy_master.json`)에 기록한다.
   GUI·CLI·스케줄러 어느 프로세스에서 읽어도 같은 값이고, 재부팅해도 남는다.
   `settings.AUTONOMY_ENABLED = True` 는 **레거시 강제 ON** 으로 계속 존중한다
   (기존 설치가 손으로 켜 둔 값이 무효화되지 않는다).
   기본값은 여전히 **꺼짐** — 파일이 없으면 False 이고, 계정 정책도 기본 False 다.

2. **"필수 보호가 별도 토글이라 꺼진 채로 무인 운영이 돈다"** — 비용 원장·브라우저
   중재·미확인 재시도 금지·발행 상태 기록은 무인 운영의 **선택 기능이 아니라 안전
   장치**다. 그런데 각자 `settings.*_ENABLED`(기본 False)를 보고 no-op 이 됐다.
   → 무인 사이클이 도는 동안에만 `session()` 안에서 프로세스 전역 플래그를 세우고,
   각 보호 모듈의 게이트가 `guards_active()` 를 OR 로 본다. **세션을 열지 않은 수동
   경로(GUI 버튼·레거시 배치·테스트)는 코드 경로가 이전과 완전히 동일하다.**

경로: `core.machine_paths.machine_file("autonomy_master.json")`
      (테스트는 `NBA_MACHINE_ROOT` 로 격리된다 — conftest 가 이미 tmp 로 고정한다.)
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

from config.logger import get_logger

log = get_logger(__name__)

SCHEMA = "nba/autonomy_master@1"
_FILE_NAME = "autonomy_master.json"

# 무인 사이클이 열어 둔 세션 수(중첩 허용). 프로세스 전역 — 이미지 워커 스레드처럼
# 다른 스레드에서 도는 엔진 호출도 같은 보호를 받아야 하므로 thread-local 이 아니다.
_SESSION_DEPTH = 0
_SESSION_PROFILE = ""
_SESSION_LOCK = threading.Lock()

# 무인 운영에서 **반드시** 켜져 있어야 하는 보호 목록(사람이 읽는 이름).
REQUIRED_GUARDS = (
    ("cost_ledger", "일일 호출 상한 원장"),
    ("browser_arbiter", "브라우저 배타 중재"),
    ("upload_reconcile", "확정 후 재시도 금지(중복 발행 차단)"),
    ("publish_state", "발행 상태 write-ahead 기록"),
)


# ─────────────────────────────────────────────────────
# 마스터 스위치 (머신 공용 파일)
# ─────────────────────────────────────────────────────
def master_path() -> Path:
    from core.machine_paths import machine_file

    return machine_file(_FILE_NAME)


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".autonomy_master_", suffix=".tmp", dir=str(path.parent))
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


def _settings_master() -> bool:
    """레거시 경로 — `settings.AUTONOMY_ENABLED = True` 는 계속 강제 ON 으로 존중한다."""
    try:
        from config import settings as _s

        return bool(getattr(_s, "AUTONOMY_ENABLED", False))
    except Exception:  # noqa: BLE001 — settings 미로드 환경이면 파일만 본다
        return False


def master_state() -> dict:
    """{"enabled", "source", "updated_at", "by", "path"}. 파일이 없으면 enabled=False."""
    out = {"enabled": False, "source": "default", "updated_at": "", "by": "",
           "path": ""}
    if _settings_master():
        out.update(enabled=True, source="settings")
    try:
        path = master_path()
        out["path"] = str(path)
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return out
    except (OSError, json.JSONDecodeError) as exc:
        log.warning(f"[autonomy_guard] 마스터 스위치 파일을 읽지 못했습니다: {type(exc).__name__}")
        return out
    if isinstance(data, dict) and bool(data.get("enabled")):
        out.update(enabled=True, source="gui" if out["source"] == "default" else out["source"],
                   updated_at=str(data.get("updated_at") or ""),
                   by=str(data.get("by") or ""))
    return out


def master_enabled() -> bool:
    """무인 운영 마스터가 켜져 있는가 = 머신 파일 OR 레거시 settings 값."""
    return bool(master_state()["enabled"])


def set_master(on: bool, *, by: str = "") -> bool:
    """마스터 스위치를 머신 공용 파일에 기록한다. 성공하면 True.

    ⚠️ `settings.py` 는 건드리지 않는다 — 전역 설정 파일 수정 없이도 재시작·CLI 에
    이어지는 것이 이 함수의 존재 이유다.
    """
    payload = {
        "schema": SCHEMA,
        "enabled": bool(on),
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "by": by or "",
    }
    try:
        _atomic_write_json(master_path(), payload)
        return True
    except (OSError, TypeError, ValueError) as exc:
        log.error(f"[autonomy_guard] 마스터 스위치 저장 실패: {type(exc).__name__}: {exc}")
        return False


# ─────────────────────────────────────────────────────
# 필수 보호 세션
# ─────────────────────────────────────────────────────
def guards_active() -> bool:
    """지금 이 프로세스가 무인 사이클 안인가(= 필수 보호를 강제 적용해야 하는가)."""
    return _SESSION_DEPTH > 0


def session_profile() -> str:
    return _SESSION_PROFILE


@contextmanager
def session(profile: str = ""):
    """무인 사이클 구간. 안에서는 필수 보호가 설정과 무관하게 켜진다."""
    global _SESSION_DEPTH, _SESSION_PROFILE
    with _SESSION_LOCK:
        _SESSION_DEPTH += 1
        if profile:
            _SESSION_PROFILE = profile
    try:
        yield
    finally:
        with _SESSION_LOCK:
            _SESSION_DEPTH = max(0, _SESSION_DEPTH - 1)
            if _SESSION_DEPTH == 0:
                _SESSION_PROFILE = ""


# ─────────────────────────────────────────────────────
# 준비 상태(readiness) — 막힌 것이 있으면 실행을 차단한다
# ─────────────────────────────────────────────────────
def _probe_cost_ledger(profile: str) -> Optional[dict]:
    """원장이 실제로 쓰기 가능한지 — 세션 안에서 예약·취소를 1회 왕복해 본다."""
    try:
        from core import cost_ledger as _cl
    except Exception as exc:  # noqa: BLE001
        return {"code": "cost_ledger_missing",
                "detail": f"비용 원장 모듈을 불러오지 못했습니다({type(exc).__name__})",
                "fix": "설치가 손상됐을 수 있습니다 — 프로그램을 다시 설치하세요."}
    try:
        with session(profile):
            if not _cl.acquire("write", 1, profile=profile):
                return {"code": "cost_ledger_denied",
                        "detail": "원장이 시험 예약을 거절했습니다(상한 도달 또는 원장 손상)",
                        "fix": "무인 운영 탭에서 일일 호출 상한을 확인하거나 "
                               "logs/cost_ledger.json 을 지우고 다시 시도하세요."}
            _cl.release("write", 1, profile=profile)
    except Exception as exc:  # noqa: BLE001
        return {"code": "cost_ledger_error",
                "detail": f"원장 접근 실패({type(exc).__name__})",
                "fix": "logs 폴더 쓰기 권한과 남은 디스크 공간을 확인하세요."}
    return None


def _probe_machine_root() -> Optional[dict]:
    try:
        from core.machine_paths import machine_file

        probe = machine_file(".write_probe")
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return {"code": "machine_root_unwritable",
                "detail": f"머신 공용 폴더에 쓸 수 없습니다({type(exc).__name__})",
                "fix": "%LOCALAPPDATA%\\NaverBlogAuto 폴더의 쓰기 권한을 확인하세요."}
    except Exception as exc:  # noqa: BLE001
        return {"code": "machine_root_error",
                "detail": f"머신 공용 폴더 확인 실패({type(exc).__name__})", "fix": ""}
    return None


def readiness(*, profile: str = "", policy: Optional[dict] = None,
              probe: bool = True) -> dict:
    """무인 운영을 **실제로 돌려도 되는지** 판정. 막는 것이 하나라도 있으면 ok=False.

    Args:
        probe: False 면 비용 원장 왕복 예약·취소(부수효과 있는 실측)를 건너뛴다 —
            `autonomy_runner.status()`처럼 표시 목적으로 반복 호출되는 경로가 매번
            원장 파일을 왕복하지 않게 하기 위함. 머신 공용 폴더 쓰기 확인은 가벼워
            probe 값과 무관하게 항상 수행한다.

    Returns:
        {"ok": bool, "master": bool, "blockers": [{"code","detail","fix"}],
         "guards": [{"name","label","applied_by"}]}
    """
    blockers: list = []
    state = master_state()

    if policy is None:
        try:
            from core import autonomy_policy as _pol

            policy = _pol.load_policy(profile or None)
        except Exception:  # noqa: BLE001
            policy = {}

    if not state["enabled"]:
        blockers.append({
            "code": "master_off",
            "detail": "무인 운영 마스터 스위치가 꺼져 있습니다.",
            "fix": "무인 운영 탭에서 범위를 고르고 '무인 운영 켜기'를 누르세요."})
    if not bool((policy or {}).get("enabled")):
        blockers.append({
            "code": "account_off",
            "detail": "이 계정의 무인 운영이 꺼져 있습니다.",
            "fix": "무인 운영 탭에서 범위를 '보관만' 또는 '예약 발행'으로 고르세요."})

    issue = _probe_machine_root()
    if issue:
        blockers.append(issue)
    if probe:
        issue = _probe_cost_ledger(profile)
        if issue:
            blockers.append(issue)

    guards = [{"name": name, "label": label, "applied_by": "autonomy_session"}
              for name, label in REQUIRED_GUARDS]
    return {"ok": not blockers, "master": state["enabled"],
            "blockers": blockers, "guards": guards}


__all__ = [
    "SCHEMA", "REQUIRED_GUARDS",
    "master_path", "master_state", "master_enabled", "set_master",
    "guards_active", "session", "session_profile", "readiness",
]
