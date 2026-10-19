# core/shorts_studio_launcher.py — GUI 시작 시 shorts_studio(:8788) 자동 기동 (2026-07-12)
"""사용자가 아무것도 안 해도 "발행하면 쇼츠가 나온다"가 되도록, GUI 실행 때
쇼츠 서버가 안 떠 있으면 자동으로 띄운다. comfy_launcher 와 같은 패턴.

- 이미 떠 있으면 아무것도 안 함(중복 실행·포트 충돌 방지).
- 쇼츠스튜디오_시작.bat 과 동일하게 `python server.py` 를 detached(콘솔창 없음,
  부모 GUI 종료 후에도 유지)로 실행.
- 실패해도 무해 — 렌더 단계가 자동으로 기존 로컬 렌더러로 폴백한다.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from config.logger import get_logger

log = get_logger(__name__)


def _cfg(name: str, default):
    try:
        from config import settings as s
        return getattr(s, name, default)
    except Exception:  # noqa: BLE001
        return default


def find_install() -> Optional[Path]:
    """shorts_studio 설치 폴더(server.py 존재)를 찾는다. 없으면 None."""
    cands = []
    cfg_dir = str(_cfg("SHORTS_STUDIO_APP_DIR", "") or "").strip()
    if cfg_dir:
        cands.append(Path(cfg_dir))
    # 표준 위치: 하네스 루트 옆 폴더 (…\하네스\shorts_studio)
    cands.append(Path(__file__).resolve().parents[2].parent / "shorts_studio")
    for d in cands:
        try:
            if (d / "server.py").is_file():
                return d
        except OSError:
            continue
    return None


def _spawn_detached(cmd: list, cwd: Path) -> None:
    """콘솔창 없이·부모와 독립적으로 실행(부모 GUI 종료 후에도 서버 유지)."""
    creationflags = 0
    if os.name == "nt":
        creationflags = 0x00000008 | 0x00000200   # DETACHED | NEW_PROCESS_GROUP
    out = open(Path(cwd) / "shorts_autostart.log", "ab", buffering=0)  # noqa: SIM115
    subprocess.Popen(
        cmd, cwd=str(cwd), stdout=out, stderr=out,
        stdin=subprocess.DEVNULL, creationflags=creationflags, close_fds=True)


def ensure_running(*, spawn_fn: Optional[Callable[[list, Path], None]] = None,
                   ping_fn: Optional[Callable[[], bool]] = None) -> str:
    """서버가 안 떠 있으면 detached 로 자동 기동. 상태 문자열 반환:
    'disabled' | 'already' | 'started' | 'not_found' | 'error'. 예외 없음."""
    if not bool(_cfg("SHORTS_STUDIO_ENABLED", True)):
        return "disabled"
    if not bool(_cfg("SHORTS_STUDIO_AUTOSTART", True)):
        return "disabled"
    try:
        if ping_fn is not None:
            alive = bool(ping_fn())
        else:
            from core.shorts_studio_client import is_alive
            alive = is_alive()
    except Exception:  # noqa: BLE001
        alive = False
    if alive:
        return "already"
    app_dir = find_install()
    if app_dir is None:
        log.info("[shorts_launcher] shorts_studio 설치를 못 찾음 — 자동 기동 생략(로컬 렌더 폴백)")
        return "not_found"
    py = shutil.which("python") or shutil.which("python3")
    if not py:
        return "not_found"
    try:
        (spawn_fn or _spawn_detached)([py, "server.py"], app_dir)
        log.info(f"[shorts_launcher] shorts_studio 자동 기동: {app_dir}")
        return "started"
    except Exception as exc:  # noqa: BLE001 — 실패해도 무해(로컬 렌더 폴백)
        log.warning(f"[shorts_launcher] 자동 기동 실패: {exc}")
        return "error"
