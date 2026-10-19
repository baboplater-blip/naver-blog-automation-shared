# core/chrome_session.py — CDP attach 발행 세션 크롬 런처 (P0-① Phase 1, 2026-07-20)
"""사용자가 "직접 로그인한" 크롬 창을 원격 디버깅 포트로 띄우고 재사용한다.

발행을 두 경로로 나누는 하이브리드의 준비 단계:
  · spawn(기본) — 매 파이프라인마다 새 크롬 + naver_login (현행, 무인 발행).
  · attach      — 이미 로그인된 이 세션 크롬에 Selenium 이 debuggerAddress 로
                  붙어 naver_login 을 건너뛴다(로그인 개편·쿠키 24h·2FA 비적용).

이 모듈은 **세션 크롬을 여는 런처만** 담당한다(attach 자체는
`core.naver_uploader.attach_to_session`). comfy_launcher / shorts_studio_launcher 와
동일한 규약을 따른다:
  · 이미 떠 있으면 아무것도 안 함(중복 실행·포트 충돌 방지).
  · detached spawn(콘솔창 없음·부모 GUI 종료 후에도 유지).
  · **모든 함수는 실패해도 예외를 전파하지 않고 상태 문자열/None 을 반환**한다.
  · 표준 라이브러리만 사용(서드파티 의존성 0).

특정 프로필·계정 하드코딩 없음 — `session_profile_dir` 가 프로필 규약으로 경로를
해석하므로 어느 프로필에서도 코드 변경 0 으로 동작한다.
"""
from __future__ import annotations

import errno
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

from config.logger import get_logger

log = get_logger(__name__)

_DEFAULT_PORT = 9222
# Chrome 실행 파일 표준 Windows 설치 경로 후보.
_CHROME_CANDIDATES = (
    r"C:/Program Files/Google/Chrome/Application/chrome.exe",
    r"C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
)


def _cfg(name: str, default):
    """settings 값을 안전하게 읽는다(미로드/속성부재 시 default)."""
    try:
        from config import settings as s
        return getattr(s, name, default)
    except Exception:  # noqa: BLE001 — settings 미로드 환경이면 default
        return default


def find_chrome() -> Optional[Path]:
    """chrome.exe 경로를 찾는다(표준 Windows 경로 → PATH). 없으면 None."""
    candidates = []
    local_appdata = os.environ.get("LOCALAPPDATA", "")
    if local_appdata:
        # 사용자 단위 설치(관리자 권한 없이 설치한 크롬).
        candidates.append(
            str(Path(local_appdata) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    candidates.extend(_CHROME_CANDIDATES)
    for cand in candidates:
        try:
            if Path(cand).is_file():
                return Path(cand)
        except OSError:
            continue
    # PATH 탐색(비표준 설치·다른 OS 대비).
    for name in ("chrome", "chrome.exe", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def session_profile_dir(profile: Optional[str] = None) -> Path:
    """attach 발행 세션용 **영구** Chrome user-data-dir 를 해석한다.

    우선순위(높은 순):
      1. `settings.CHROME_SESSION_PROFILE_DIR` 명시 오버라이드 — 사용자가 굳이 지정한
         값은 항상 이긴다(명시 설정 > 파생 기본값 원칙). 기본값 None 이면 건너뛴다.
      2. named 프로필의 기존 영구 chrome dir(`config.profile.chrome_profile_dir`)
         — 오버라이드가 없을 때(일반 케이스). spawn 발행이 쓰는 그 프로필을 그대로
         재사용해 로그인 세션을 공유한다.
      3. 프로필별 격리 폴백 경로(`<프로필 데이터 루트>/_runtime/chrome_session`).

    항상 존재 여부와 무관하게 Path 를 반환한다(디렉터리 생성은 `ensure_session` 이 담당).
    특정 프로필/계정을 하드코딩하지 않으므로 어느 프로필에서도 코드 변경 없이 동작한다.

    Args:
        profile: 프로필 이름 오버라이드. None 이면 활성 프로필.
    """
    # 1) 사용자 명시 오버라이드 — 지정했으면 항상 최우선(명시 설정 > 파생 기본값).
    override = str(_cfg("CHROME_SESSION_PROFILE_DIR", None) or "").strip()
    if override:
        return Path(override)

    # 2) named 프로필의 기존 영구 chrome dir 재사용(default 프로필은 None → 폴백).
    try:
        from config import profile as _p
        chrome_dir = _p.chrome_profile_dir(profile)
        if chrome_dir is not None:
            return Path(chrome_dir)
    except Exception:  # noqa: BLE001 — profile 미로드 환경이면 아래 폴백
        pass

    # 3) 프로필별 격리 폴백(재시작 생존 · 계정 교차 오염 방지).
    try:
        from config import profile as _p
        return Path(_p.app_data_root(profile)) / "_runtime" / "chrome_session"
    except Exception:  # noqa: BLE001 — 최후 폴백(앱 루트 기준)
        return Path(__file__).resolve().parent.parent / "_runtime" / "chrome_session"


def is_running(port: int = _DEFAULT_PORT) -> bool:
    """원격 디버깅 포트가 응답하는가(`/json/version` 200). 예외 시 False."""
    url = f"http://127.0.0.1:{int(port)}/json/version"
    timeout = float(_cfg("CHROME_ATTACH_PING_TIMEOUT", 1.5))
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — 로컬호스트 고정
            status = getattr(resp, "status", None)
            if status is None:
                status = resp.getcode()
            return 200 <= int(status) < 300
    except Exception:  # noqa: BLE001 — 미기동·거부·타임아웃 모두 '안 떠 있음'
        return False


def _is_connection_refused(reason: object) -> bool:
    """`URLError.reason`(또는 그 자체 예외)이 '확실한 연결 거부'인지 판정.

    연결 거부는 OS 가 그 포트에 아무도 듣고 있지 않다고 즉시 알려준 경우다
    (크롬이 그 포트에 확실히 없다는 뜻) — 타임아웃·DNS 실패 등 '알 수 없음'과
    구분해야 CDP 접근 실패를 전부 '닫힘'으로 뭉개는 사고를 막을 수 있다.
    """
    if isinstance(reason, ConnectionRefusedError):
        return True
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in (
            errno.ECONNREFUSED, 10061):  # 10061 = WinError 대상 거부(로컬호스트)
        return True
    return False


def session_status(
    port: int = _DEFAULT_PORT,
    *,
    timeout: Optional[float] = None,
    opener: Optional[Callable[[str, float], object]] = None,
) -> dict:
    """원격 디버깅 포트 상태를 3값으로 판정한다(예외 무전파).

    `is_running()`(bool 하나)은 "떠 있다/모르겠다"를 한 값으로 뭉개 화면에서
    "CDP 접근 실패=닫힘"으로 오인되는 원인이었다. 이 함수는 그 위에 얹는
    더 정밀한 판정 계층으로, 세 값을 구분해 반환한다:

      · 'open'    — `/json/version` 이 2xx 로 응답. 크롬이 그 포트에서 확실히
                    떠 있다.
      · 'closed'  — 연결 자체가 거부됨(`ConnectionRefusedError` 계열). 그
                    포트에 아무것도 듣고 있지 않다는 확실한 신호.
      · 'unknown' — 타임아웃·DNS 실패·기타 예기치 못한 오류. CDP 접근은
                    실패했지만 "닫혀 있다"고 단정할 근거가 없다(방화벽·일시적
                    지연 등일 수 있다) — **여기를 'closed' 로 표시하지 않는다.**

    반환: ``{"state": "open"|"closed"|"unknown", "detail": str}``.

    Args:
        port: 원격 디버깅 포트.
        timeout: 요청 타임아웃(초). 기본 `CHROME_ATTACH_PING_TIMEOUT`.
        opener: 저수준 HTTP 오프너(테스트 주입용) — ``opener(url, timeout)`` 이
            `urllib.request.urlopen` 과 같은 방식으로 응답 객체를 반환하거나
            예외를 던져야 한다. 생략하면 실제 네트워크 호출을 한다.
    """
    url = f"http://127.0.0.1:{int(port)}/json/version"
    t = float(timeout if timeout is not None else _cfg("CHROME_ATTACH_PING_TIMEOUT", 1.5))
    op = opener or (lambda u, to: urllib.request.urlopen(u, timeout=to))  # noqa: S310 — 로컬호스트 고정

    try:
        resp = op(url, t)
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        if _is_connection_refused(reason):
            return {"state": "closed", "detail": "연결 거부"}
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return {"state": "unknown", "detail": "타임아웃"}
        return {"state": "unknown", "detail": type(exc).__name__}
    except ConnectionRefusedError:
        return {"state": "closed", "detail": "연결 거부"}
    except (socket.timeout, TimeoutError):
        return {"state": "unknown", "detail": "타임아웃"}
    except OSError as exc:
        if _is_connection_refused(exc):
            return {"state": "closed", "detail": "연결 거부"}
        return {"state": "unknown", "detail": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001 — 예기치 못한 오류는 unknown(닫힘 단정 금지)
        return {"state": "unknown", "detail": type(exc).__name__}

    try:
        status = getattr(resp, "status", None)
        if status is None:
            status = resp.getcode()
        status = int(status)
    except Exception:  # noqa: BLE001 — 응답 판독 실패는 unknown
        return {"state": "unknown", "detail": "응답 판독 실패"}
    finally:
        closer = getattr(resp, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001 — 응답 정리 실패는 무해
                pass

    if 200 <= status < 300:
        return {"state": "open", "detail": f"HTTP {status}"}
    return {"state": "unknown", "detail": f"HTTP {status}"}


def wait_until_running(
    port: int = _DEFAULT_PORT,
    timeout: float = 10.0,
    interval: float = 0.3,
    *,
    probe_fn: Optional[Callable[[int], bool]] = None,
) -> bool:
    """디버깅 포트가 열릴 때까지 짧은 간격으로 폴링. 열리면 True, 타임아웃이면 False.

    `ensure_session` 이 방금 크롬을 띄운('started') 직후엔 디버깅 포트가 아직 안 열려
    바로 attach 하면 콜드 스타트 레이스로 실패한다. 이 헬퍼로 최대 `timeout` 초까지
    `interval` 간격으로 포트를 확인한다('already' 면 호출 불필요). 예외 무전파.

    Args:
        port: 원격 디버깅 포트.
        timeout: 최대 대기(초).
        interval: 폴링 간격(초).
        probe_fn: 프로브 함수(테스트 주입용). 기본 `is_running`.
    """
    probe = probe_fn or is_running
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        try:
            if probe(port):
                return True
        except Exception:  # noqa: BLE001 — 프로브 예외는 '아직 안 열림'
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(max(0.05, float(interval)))


def _spawn_detached(cmd: list, cwd: Path) -> None:
    """콘솔창 없이·부모와 독립적으로 크롬을 실행(부모 GUI 종료 후에도 창 유지)."""
    creationflags = 0
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP — 콘솔 없이 독립 실행.
        creationflags = 0x00000008 | 0x00000200
    subprocess.Popen(  # noqa: S603 — cmd 는 내부 조립(사용자 입력 아님)
        cmd, cwd=str(cwd),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL, creationflags=creationflags, close_fds=True)


def ensure_session(
    port: int = _DEFAULT_PORT,
    profile: Optional[str] = None,
    *,
    spawn_fn: Optional[Callable[[list, Path], None]] = None,
    probe_fn: Optional[Callable[[int], bool]] = None,
) -> str:
    """발행 세션 크롬이 안 떠 있으면 detached 로 기동한다.

    반환(상태 문자열, 예외 없음):
      · 'already'   — 이미 그 포트에 크롬이 떠 있음(재사용).
      · 'started'   — 새로 기동함(사용자가 그 창에서 네이버에 로그인해야 함).
      · 'not_found' — chrome.exe 를 못 찾음.
      · 'error'     — 기동 시도 중 오류(무해 — 발행은 attach 실패 시 fail-closed).

    Args:
        port: 원격 디버깅 포트.
        profile: 프로필 이름 오버라이드(None=활성 프로필).
        spawn_fn: 실제 프로세스 기동 함수(테스트 주입용). 기본 `_spawn_detached`.
        probe_fn: 기동 여부 프로브 함수(테스트 주입용). 기본 `is_running`.
    """
    port = int(port)
    probe = probe_fn or is_running
    try:
        if probe(port):
            return "already"
    except Exception:  # noqa: BLE001 — 프로브 실패는 '안 떠 있음'으로 간주하고 진행
        pass

    chrome = find_chrome()
    if chrome is None:
        log.info("[chrome_session] chrome.exe 를 못 찾음 — 발행 세션 자동 기동 생략")
        return "not_found"

    user_data_dir = session_profile_dir(profile)
    try:
        user_data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning(f"[chrome_session] user-data-dir 생성 실패(계속 시도): {exc}")

    cmd = [
        str(chrome),
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        # 영구 프로필 비정상 종료 후 "세션 복원?" 버블 억제(발행 세션 창 방해 방지).
        "--disable-session-crashed-bubble",
        "--hide-crash-restore-bubble",
    ]
    try:
        (spawn_fn or _spawn_detached)(cmd, user_data_dir)
        log.info(f"[chrome_session] 발행 세션 크롬 기동(port {port}) — "
                 "그 창에서 네이버에 한 번 로그인하세요")
        return "started"
    except Exception as exc:  # noqa: BLE001 — 기동 실패는 무해(attach 시 fail-closed 처리)
        log.warning(f"[chrome_session] 발행 세션 크롬 기동 실패: {exc}")
        return "error"
