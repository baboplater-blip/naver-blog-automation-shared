# core/comfy_launcher.py — GUI 시작 시 로컬 ComfyUI 자동 기동(무인 로컬 생성)
"""GUI 실행 때 로컬 ComfyUI(Krea 2 백엔드)가 안 떠 있으면 자동으로 띄운다.

- 이미 떠 있으면 아무것도 안 함(중복 실행·포트 충돌 방지).
- Pinokio 로 설치된 ComfyUI 를 그대로 쓰되, 실행은 그 venv 의 python 으로 main.py 를
  **detached**(콘솔창 없음·부모 GUI 종료 후에도 유지)로 직접 띄운다 — Pinokio API 불필요,
  실검증된 방식(2026-07-06). Pinokio 로 관리하고 싶으면 IMAGE_COMFY_AUTOSTART=False 로 끈다.
- 실패해도 무해 — 하네스가 자동으로 클라우드(Pollinations→NVIDIA)로 폴백한다.
- 서버 기동은 가볍다(모델은 첫 생성 때 지연 로드). 자동 기동해도 VRAM 즉시 점유 아님.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

from config.logger import get_logger

log = get_logger(__name__)

_DEFAULT_PORT = 8188
# Pinokio 표준 설치 경로 후보(사용자 환경: comfy-mirror.git 확인됨).
_CANDIDATE_APP_DIRS = (
    r"C:/pinokio/api/comfy-mirror.git/app",
    r"C:/pinokio/api/comfyui.git/app",
    r"C:/pinokio/api/comfy.git/app",
)
_VENV_RELS = ("env/Scripts/python.exe", "env/python.exe", "venv/Scripts/python.exe")


def _cfg(name: str, default):
    try:
        from config import settings as s
        return getattr(s, name, default)
    except Exception:  # noqa: BLE001
        return default


def _base_url() -> str:
    return str(_cfg("IMAGE_COMFY_URL", "") or f"http://127.0.0.1:{_DEFAULT_PORT}").rstrip("/")


def _port() -> int:
    try:
        return int(_base_url().rsplit(":", 1)[-1])
    except Exception:  # noqa: BLE001
        return _DEFAULT_PORT


def is_running(ping_fn: Optional[Callable[[], bool]] = None) -> bool:
    """ComfyUI 서버가 응답하는가(/system_stats 200)."""
    if ping_fn is not None:
        try:
            return bool(ping_fn())
        except Exception:  # noqa: BLE001
            return False
    import requests
    try:
        r = requests.get(f"{_base_url()}/system_stats",
                         timeout=float(_cfg("IMAGE_COMFY_PING_TIMEOUT", 2)))
        return r.status_code == 200
    except Exception:  # noqa: BLE001
        return False


def _venv_python(app_dir: Path) -> Optional[Path]:
    for rel in _VENV_RELS:
        p = app_dir / rel
        if p.is_file():
            return p
    return None


def find_install() -> Optional[Path]:
    """ComfyUI 설치 앱 디렉토리(main.py + venv python 존재)를 찾는다. 없으면 None."""
    cands = []
    cfg_dir = str(_cfg("IMAGE_COMFY_APP_DIR", "") or "").strip()
    if cfg_dir:
        cands.append(cfg_dir)
    cands.extend(_CANDIDATE_APP_DIRS)
    up = os.environ.get("USERPROFILE", "")
    if up:
        cands.append(str(Path(up) / "pinokio" / "api" / "comfy-mirror.git" / "app"))
    for c in cands:
        d = Path(c)
        try:
            if (d / "main.py").is_file() and _venv_python(d) is not None:
                return d
        except Exception:  # noqa: BLE001
            continue
    return None


def _spawn_detached(cmd: list, cwd: Path) -> None:
    """콘솔창 없이·부모와 독립적으로 실행(부모 GUI 종료 후에도 서버 유지)."""
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — 콘솔 없이 독립 실행.
        creationflags = 0x00000008 | 0x00000200
    log_path = Path(cwd) / "comfy_autostart.log"
    out = open(log_path, "ab", buffering=0)  # noqa: SIM115 — detached 프로세스가 계속 씀
    subprocess.Popen(
        cmd, cwd=str(cwd), stdout=out, stderr=out,
        stdin=subprocess.DEVNULL, creationflags=creationflags, close_fds=True)


def ensure_running(*, spawn_fn: Optional[Callable[[list, Path], None]] = None,
                   ping_fn: Optional[Callable[[], bool]] = None) -> str:
    """ComfyUI 가 안 떠 있으면 detached 로 자동 기동. 상태 문자열 반환:
    'disabled' | 'already' | 'started' | 'not_found' | 'error'. 예외 없음."""
    if not bool(_cfg("IMAGE_COMFY_AUTOSTART", True)):
        return "disabled"
    if not bool(_cfg("IMAGE_COMFY_ENABLED", True)):
        return "disabled"
    if is_running(ping_fn):
        return "already"
    app_dir = find_install()
    if app_dir is None:
        log.info("[comfy_launcher] ComfyUI 설치를 못 찾음 — 자동 기동 생략(클라우드 폴백)")
        return "not_found"
    py = _venv_python(app_dir)
    if py is None:
        return "not_found"
    cmd = [str(py), "main.py", "--port", str(_port()), "--listen", "127.0.0.1"]
    try:
        (spawn_fn or _spawn_detached)(cmd, app_dir)
        log.info(f"[comfy_launcher] ComfyUI 자동 기동: {app_dir} (port {_port()})")
        return "started"
    except Exception as exc:  # noqa: BLE001 — 실패해도 무해(클라우드 폴백)
        log.warning(f"[comfy_launcher] ComfyUI 자동 기동 실패: {exc}")
        return "error"
