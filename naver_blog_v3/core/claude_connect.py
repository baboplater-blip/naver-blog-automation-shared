# core/claude_connect.py — Claude Code CLI 연결(로그인) 관리 (온보딩 관문 ② 제거)
"""검은 CMD 창 없이 앱 안에서 Claude(글 생성 AI)를 연결한다.

기존 온보딩은 사용자가 `claude_login.bat`(별도 CMD 창)을 직접 더블클릭해
`node cli.js login` 을 돌려야 했다 — 컴맹·초보의 최대 이탈 지점(관문 ②).
이 모듈은 그 로그인을 **콘솔 창 없이**(CREATE_NO_WINDOW) 서브프로세스로 띄우고,
로그인 완료를 `~/.claude.json` 의 ``oauthAccount`` 존재로 **오프라인 감지**한다.

설계 원칙:
  - 감지(is_authenticated)가 핵심이며 가장 신뢰도 높다 — 어떤 경로로 로그인했든
    결과 상태를 정확히 반영한다.
  - 창 없는 로그인 실행은 best-effort 개선이다. 브라우저 OAuth 는 로컬 콜백으로
    완료되므로 콘솔이 없어도 동작하지만, 만약 완료가 감지되지 않으면 호출자는
    검증된 `claude_login.bat`(open_manual_login) 폴백을 쓸 수 있다 →
    **어떤 경우에도 기존 동작보다 나빠지지 않는다.**
  - 전부 fail-open: 어떤 검사 실패도 예외를 던지지 않고 보수적 값을 반환한다.
  - 자격증명 '값'(이메일·토큰)은 절대 반환/로그하지 않는다(존재 여부만).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

# Windows 서브프로세스를 콘솔 창 없이 띄우는 플래그.
_CREATE_NO_WINDOW = 0x08000000

_PKG_ROOT = Path(__file__).resolve().parents[1]  # naver_blog_v3/


# ──────────────────────────────────────────────────────────────────────
# 상태 감지 (네트워크 없음, 오프라인)
# ──────────────────────────────────────────────────────────────────────
def _claude_config_path() -> Path:
    """Claude Code 가 로그인 정보를 기록하는 사용자 홈의 설정 파일."""
    return Path(os.path.expanduser("~")) / ".claude.json"


def is_authenticated() -> bool:
    """Claude Max 로그인이 돼 있는지.

    ``~/.claude.json`` 이 존재하고 ``oauthAccount`` 가 채워져 있으면 로그인된 것으로
    본다(로그인 시 Claude Code 가 이 키를 기록). 네트워크를 타지 않는다.
    값(이메일 등)은 읽되 반환/로그하지 않는다.

    Returns:
        로그인돼 있으면 True. 파일 부재·파싱 실패·미로그인은 모두 False(fail-open).
    """
    try:
        cfg = _claude_config_path()
        if not cfg.exists():
            return False
        data = json.loads(cfg.read_text(encoding="utf-8"))
        return bool(data.get("oauthAccount"))
    except (OSError, ValueError):  # ValueError ⊇ JSONDecodeError
        return False


def is_installed() -> bool:
    """claude CLI(또는 BlogAutoNode cli.js) 가 설치돼 있는지."""
    return _resolve_login_command() is not None


def status() -> str:
    """온보딩 UI 신호용 상태 문자열.

    Returns:
        ``"connected"`` (설치+로그인) · ``"installed"`` (설치됐으나 미로그인) ·
        ``"missing"`` (CLI 미설치).
    """
    if not is_installed():
        return "missing"
    return "connected" if is_authenticated() else "installed"


# ──────────────────────────────────────────────────────────────────────
# 실행 파일 해석
# ──────────────────────────────────────────────────────────────────────
def _find_claude() -> Optional[str]:
    """claude 실행 파일 경로. PATH → npm 전역 설치 위치 순.

    content_generator._find_claude_cmd 와 같은 규칙(단, 무거운 모듈 임포트를 피해
    온보딩에서 값싸게 쓰도록 자체 구현). 실패 시 None.
    """
    found = shutil.which("claude")
    if found:
        return found
    appdata = os.environ.get("APPDATA", "")
    fallbacks = [
        os.path.join(appdata, "npm", "claude.cmd") if appdata else "",
        os.path.join(appdata, "npm", "claude") if appdata else "",
        os.path.expanduser("~/AppData/Roaming/npm/claude.cmd"),
    ]
    for p in fallbacks:
        if p and os.path.exists(p):
            return p
    return None


def _find_node() -> Optional[str]:
    """node 실행 파일. 번들(_runtime/nodejs) → PATH → Program Files 순. 실패 시 None."""
    bundled = _PKG_ROOT / "_runtime" / "nodejs" / "node.exe"
    if bundled.exists():
        return str(bundled)
    found = shutil.which("node")
    if found:
        return found
    for c in (r"C:\Program Files\nodejs\node.exe",
              r"C:\Program Files (x86)\nodejs\node.exe"):
        if Path(c).exists():
            return c
    return None


def _find_cli_js() -> Optional[str]:
    """setup.ps1 이 설치하는 BlogAutoNode 의 claude-code cli.js 경로. 없으면 None."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    p = (Path(appdata) / "BlogAutoNode" / "node_modules" /
         "@anthropic-ai" / "claude-code" / "cli.js")
    return str(p) if p.exists() else None


def _resolve_login_command() -> Optional[Tuple[str, object]]:
    """`... login` 실행 사양을 해석한다.

    Returns:
        ``(kind, cmd)`` — kind 는 ``"cmd_shell"``(claude.cmd, shell 실행) ·
        ``"exe"``(claude.exe 등, 리스트 실행) · ``"node"``(node cli.js, 리스트 실행).
        해석 불가 시 None.
    """
    claude = _find_claude()
    if claude:
        if claude.lower().endswith(".cmd"):
            # .cmd 는 shell 로 실행해야 안정적(_call_claude 와 동일 패턴).
            return ("cmd_shell", claude)
        return ("exe", [claude, "login"])
    cli = _find_cli_js()
    node = _find_node()
    if cli and node:
        return ("node", [node, cli, "login"])
    return None


# ──────────────────────────────────────────────────────────────────────
# 로그인 실행
# ──────────────────────────────────────────────────────────────────────
def _login_env() -> dict:
    """로그인 서브프로세스용 환경. 중첩 세션 방지 + UTF-8 강제."""
    env = os.environ.copy()
    for var in ("CLAUDECODE", "CLAUDE_CODE"):
        env.pop(var, None)
    env["PYTHONUTF8"] = "1"
    return env


def start_login() -> Optional[subprocess.Popen]:
    """콘솔 창 없이 Claude 로그인 서브프로세스를 띄운다.

    브라우저가 열려 OAuth 를 진행한다(로컬 콜백으로 완료). 호출자는 이 핸들의
    ``poll()`` 또는 :func:`is_authenticated` 폴링으로 완료를 감지한다.

    Returns:
        Popen 핸들, 또는 CLI 미설치/실행 실패 시 None.
    """
    spec = _resolve_login_command()
    if spec is None:
        log.warning("[claude_connect] 로그인 커맨드 해석 실패 — CLI 미설치")
        return None
    kind, cmd = spec
    creationflags = _CREATE_NO_WINDOW if sys.platform == "win32" else 0
    env = _login_env()
    try:
        if kind == "cmd_shell":
            return subprocess.Popen(
                f'"{cmd}" login',
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=True,
                creationflags=creationflags, env=env)
        return subprocess.Popen(
            cmd,  # list: exe/node
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags, env=env)
    except OSError as exc:
        log.error("[claude_connect] 로그인 프로세스 시작 실패: %s", exc)
        return None


def open_manual_login() -> bool:
    """검증된 폴백 — 기존 claude_login.bat 을 일반 창으로 실행한다.

    창 없는 start_login 이 완료를 못 낼 때의 안전망. 어떤 경우에도 기존 동작으로
    돌아갈 수 있게 한다.

    Returns:
        실행을 시작했으면 True, bat 부재/실행 실패 시 False.
    """
    bat = _PKG_ROOT / "claude_login.bat"
    if not bat.exists():
        log.warning("[claude_connect] claude_login.bat 부재 — 수동 로그인 폴백 불가")
        return False
    try:
        os.startfile(str(bat))  # type: ignore[attr-defined]  # Windows 전용
        return True
    except (OSError, AttributeError) as exc:
        log.error("[claude_connect] 수동 로그인 실행 실패: %s", exc)
        return False


def signup_url() -> str:
    """Claude Max 가입 안내 링크(미가입 사용자용)."""
    return "https://claude.ai/upgrade"
