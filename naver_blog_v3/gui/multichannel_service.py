# gui/multichannel_service.py — 멀티채널 팩(쇼츠 mp4+캡션) 순수 로직
"""app.py 의 ``run_multichannel_build`` 와 settings.py 의 옛
``_run_multichannel_build`` 가 공유하던 헬퍼 spawn·후보 수집·완료 감지
로직의 단일 진실원본(auto-dev 5호 §1). **Tk 의존 0** — 슬러그 선택
다이얼로그(``_pick_slug_dialog``)처럼 화면이 필요한 부분은 여기 두지 않고
app.py 에 남긴다.

공개 API(코더 D 가 settings.py 에서 그대로 import — 시그니처 고정):
    CANDIDATE_FILES, harness_root(), list_candidate_slugs(), start_build()
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

# 슬러그 폴더 안에 이 중 하나라도 있으면 "처리 가능한 글"로 본다.
# settings.py 의 MULTICHANNEL_CANDIDATE_FILES 와 동일 값 — 그쪽이 이 상수를
# 그대로 import 해 쓴다(값이 두 곳에서 어긋나지 않게).
CANDIDATE_FILES: Tuple[str, ...] = (
    "post_data.json", "draft.md", "draft_path.txt", "publish_brief.yaml",
)

_MAX_CANDIDATES = 20


def harness_root() -> Path:
    """naver_blog_automation_v3/ 디렉토리 경로(헬퍼 스크립트 실행 기준)."""
    return Path(__file__).resolve().parents[2]


def list_candidate_slugs(root: Optional[Path] = None) -> List[str]:
    """runs/ 아래에서 영상으로 만들 수 있는 슬러그 후보를 찾는다.

    ``CANDIDATE_FILES`` 중 하나라도 있는 폴더만 후보로 치며, 디렉토리 이름
    역순(최신 우선, 기존 app.py 규칙 그대로) 최대 :data:`_MAX_CANDIDATES` 개.
    ``runs/`` 자체가 없으면 빈 리스트(예외 없음).
    """
    root = root or harness_root()
    runs_dir = root / "runs"
    if not runs_dir.exists():
        return []
    candidates: List[str] = []
    try:
        children = sorted(runs_dir.iterdir(), reverse=True)
    except OSError:
        return []
    for child in children:
        if not child.is_dir():
            continue
        if any((child / fname).exists() for fname in CANDIDATE_FILES):
            candidates.append(child.name)
        if len(candidates) >= _MAX_CANDIDATES:
            break
    return candidates


def helper_script_path(root: Optional[Path] = None) -> Path:
    """``_lib/build_multichannel_pack.py`` 경로(존재 여부는 호출부가 확인)."""
    root = root or harness_root()
    return root / "_lib" / "build_multichannel_pack.py"


def _spawn_helper(args: List[str], cwd: Path) -> Optional[subprocess.Popen]:
    """하네스 ``_lib`` 헬퍼 스크립트를 별도 콘솔로 실행한다.

    자식 프로세스에 UTF-8 강제 환경변수를 주입해 한글 print 시 cp949
    콘솔에서 UnicodeEncodeError 가 나지 않게 한다.

    Returns:
        성공 시 Popen, 실패(OSError) 시 None. 예외를 삼키는 이유는 이 모듈이
        Tk 의존 0 이라 messagebox 로 알릴 수 없기 때문 — 실패는 반환값(None)
        으로만 전달하고, 호출부(app.py)가 사용자에게 안내한다.
    """
    child_env = os.environ.copy()
    child_env["PYTHONUTF8"] = "1"
    child_env["PYTHONIOENCODING"] = "utf-8"
    try:
        creationflags = (subprocess.CREATE_NEW_CONSOLE
                         if sys.platform == "win32" else 0)
        return subprocess.Popen(
            [sys.executable] + args,
            cwd=str(cwd),
            env=child_env,
            creationflags=creationflags)
    except OSError:
        return None


def start_build(
    slug: str,
    root: Optional[Path] = None,
    on_line: Optional[Callable[[str], None]] = None,
    on_done: Optional[Callable[[bool, Optional[Path]], None]] = None,
    *,
    poll_interval_s: float = 1.0,
    timeout_s: float = 600.0,
    should_stop: Optional[Callable[[], bool]] = None,
) -> Optional[subprocess.Popen]:
    """헬퍼 프로세스를 spawn 하고, 완료를 내부 워커 스레드에서 감지한다.

    ``on_line``/``on_done`` 콜백은 **워커 스레드에서 호출된다** — 이 모듈은
    Tk 의존 0 원칙이라 메인 스레드 재위임은 호출부의 몫이다(예:
    ``App.ui_after``). 완료 감지는 산출 폴더(``runs/<slug>/multichannel``)의
    mtime 이 시작 시점보다 갱신됐는지로 판단한다(기존 app.py 규칙 그대로).

    Args:
        slug: 대상 글 폴더 이름.
        root: 하네스 루트. None 이면 :func:`harness_root`.
        on_line: 사람이 읽는 진행 안내 문구 한 줄(선택). 헬퍼 프로세스의
            실제 stdout 은 별도 콘솔에 뜨므로 여기서 캡처하지 않는다 —
            시작 시 1회만 호출된다.
        on_done: ``(produced, out_dir)`` — 산출물이 새로 생겼는지와 그
            경로(못 만들었으면 ``(False, None)``).
        poll_interval_s: 완료 감지 폴링 간격(초).
        timeout_s: 최대 대기 시간(초) — 넘으면 미완료로 간주하고 종료.
        should_stop: 매 폴링 tick 마다 확인하는 조기 종료 신호(선택). True
            를 돌려주면 그 자리에서 조용히 폴링을 멈추고 **``on_done`` 을
            부르지 않는다** — 이미 파괴된 창(App)에 결과를 재위임하다
            "invalid command name" 이 새는 걸 막는 용도(예:
            ``App.is_destroyed`` — AppLifecycle). 헬퍼 프로세스 자체(별도
            콘솔)는 계속 실행되며, 이 함수는 감시만 멈춘다.

    Returns:
        성공적으로 spawn 됐으면 Popen, 헬퍼 스크립트가 없거나 spawn 자체가
        실패했으면 None(이 경우 ``on_done(False, None)`` 도 호출된다).
    """
    root = root or harness_root()
    helper = helper_script_path(root)
    if not helper.exists():
        if on_done is not None:
            on_done(False, None)
        return None

    out_dir = root / "runs" / slug / "multichannel"
    try:
        baseline_mtime = out_dir.stat().st_mtime
    except OSError:
        baseline_mtime = 0.0

    proc = _spawn_helper([str(helper), "--slug", slug], cwd=root)
    if proc is None:
        if on_done is not None:
            on_done(False, out_dir)
        return None

    if on_line is not None:
        on_line(f"별도 창에서 '{slug}' 영상을 만들고 있어요…")

    def _watch() -> None:
        deadline = time.time() + timeout_s
        while True:
            if should_stop is not None and should_stop():
                return  # 창이 이미 파괴됨 — on_done 호출 안 함(죽은 위젯을 건드릴 콜백 방지)
            finished = proc.poll() is not None
            try:
                produced = out_dir.stat().st_mtime > baseline_mtime
            except OSError:
                produced = False
            if finished or produced or time.time() >= deadline:
                if on_done is not None:
                    on_done(produced, out_dir if produced else None)
                return
            time.sleep(poll_interval_s)

    threading.Thread(target=_watch, daemon=True, name="multichannel-watch").start()
    return proc
