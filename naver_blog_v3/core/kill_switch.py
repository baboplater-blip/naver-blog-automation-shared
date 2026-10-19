"""kill_switch — 전체 비상정지(estop) + 계정 일시중지(pause) (M1~M4 무인 운영, 2026-09-18).

**fail-closed**: 플래그 파일이 존재하거나 읽기 자체가 실패하면 "정지됨"으로 본다.
내용이 깨져 있어도 존재만으로 활성 — 판독 실패를 "정지 아님"으로 해석하는 순간
무인 파이프라인이 멈추지 않는 사고가 난다.

- estop(전체 비상정지) — `machine_paths.machine_file("estop.flag")`. 계정 무관, 머신 전체.
  `check_before_external_action()` 이 이것만 막는다(예약 등록·발행 제출 직전).
- pause(계정 일시중지) — `profile.logs_dir(profile)/pause.flag` **또는**
  `core.autonomy_policy.load_policy(profile)["pause"]`. 진행 중인 글은 끝까지 두고
  다음 글부터 막는다 — `check_before_new_item()` 이 이것도 함께 본다.

⚠️ 경로는 항상 함수 안에서 조회한다(모듈 상수로 굳히지 않음).
"""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional


class Stopped(Exception):
    """estop 으로 외부 확정 행동이 차단됐을 때 raise."""


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────────
# 경로
# ─────────────────────────────────────────────────────────────────────────
def estop_path() -> Path:
    from core.machine_paths import machine_file
    return machine_file("estop.flag")


def pause_path(profile: Optional[str] = None) -> Path:
    from config import profile as _pf
    return Path(_pf.logs_dir(profile)) / "pause.flag"


# ─────────────────────────────────────────────────────────────────────────
# 파일 I/O — 존재 자체가 신호, 내용은 부가 정보(reason/by/since)일 뿐.
# ─────────────────────────────────────────────────────────────────────────
def _path_exists(path: Path) -> bool:
    """`os.path.exists` 경유 — 테스트가 이 함수만 monkeypatch 해 '읽기 실패'를 흉내낼 수 있다."""
    return os.path.exists(str(path))


def _read_flag(path: Path) -> tuple:
    """(active, data). active=True 면 정지/중지 상태(파일 존재 또는 확인 자체가 실패).
    data 는 파싱 가능했을 때만 dict, 아니면 빈 dict(⚠️ 내용 깨짐도 active=True 는 불변)."""
    try:
        exists = _path_exists(path)
    except OSError:
        return True, {}
    if not exists:
        return False, {}
    data: dict = {}
    try:
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            data = parsed
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        data = {}
    return True, data


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".killswitch_", suffix=".tmp", dir=str(path.parent))
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


def _policy_pause(profile: Optional[str] = None) -> bool:
    """정책 파일의 pause=True 도 반영한다. 모듈이 없거나 로드 실패하면 그냥 False
    (파일 플래그만으로 판단 — fail-safe, autonomy_policy 는 다른 작업자 소유 모듈)."""
    try:
        from core import autonomy_policy
    except Exception:  # noqa: BLE001 — 모듈 미존재/미완성이어도 kill_switch 는 살아야 한다
        return False
    try:
        policy = autonomy_policy.load_policy(profile)
        return bool(policy.get("pause"))
    except Exception:  # noqa: BLE001
        return False


# ─────────────────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────────────────
def estop_active() -> bool:
    active, _ = _read_flag(estop_path())
    return active


def pause_active(profile: Optional[str] = None) -> bool:
    active, _ = _read_flag(pause_path(profile))
    if active:
        return True
    return _policy_pause(profile)


def set_estop(on: bool, *, reason: str = "", by: str = "") -> bool:
    path = estop_path()
    try:
        if on:
            _atomic_write_json(path, {"reason": reason, "by": by, "since": _now_iso()})
        else:
            if path.exists():
                path.unlink()
        return True
    except OSError:
        return False


def set_pause(on: bool, *, reason: str = "", profile: Optional[str] = None) -> bool:
    path = pause_path(profile)
    try:
        if on:
            _atomic_write_json(path, {"reason": reason, "by": "", "since": _now_iso()})
        else:
            if path.exists():
                path.unlink()
        return True
    except OSError:
        return False


def status(profile: Optional[str] = None) -> dict:
    """{"estop","estop_reason","estop_since","pause","pause_reason","pause_since"}"""
    e_active, e_data = _read_flag(estop_path())
    p_file_active, p_data = _read_flag(pause_path(profile))
    p_policy_active = _policy_pause(profile)
    pause_active_final = p_file_active or p_policy_active

    if p_file_active:
        pause_reason = str(p_data.get("reason", "") or "")
        pause_since = p_data.get("since")
    elif p_policy_active:
        pause_reason = "정책(autonomy.json) pause=True"
        pause_since = None
    else:
        pause_reason = ""
        pause_since = None

    return {
        "estop": e_active,
        "estop_reason": str(e_data.get("reason", "") or "") if e_active else "",
        "estop_since": e_data.get("since") if e_active else None,
        "pause": pause_active_final,
        "pause_reason": pause_reason,
        "pause_since": pause_since,
    }


def check_before_external_action(*, profile: Optional[str] = None) -> None:
    """다음 외부 확정 행동(예약 등록·발행 제출) 직전에 부른다. estop 이면 Stopped.
    pause 는 여기서 막지 않는다(진행 중인 글은 끝까지)."""
    if estop_active():
        raise Stopped("전체 비상정지(estop) 활성 — 외부 확정 행동을 진행할 수 없습니다.")


def check_before_new_item(*, profile: Optional[str] = None) -> str:
    """글 경계에서 부른다. 계속 가능하면 "", 아니면 "estop"|"pause"."""
    if estop_active():
        return "estop"
    if pause_active(profile):
        return "pause"
    return ""


__all__ = [
    "Stopped",
    "estop_path", "pause_path",
    "estop_active", "pause_active",
    "set_estop", "set_pause",
    "status",
    "check_before_external_action", "check_before_new_item",
]
