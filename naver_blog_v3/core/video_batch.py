# core/video_batch.py — 발행 글 영상 통합 배치 (Phase②, 2026-07-04)
"""블로그 발행과 쇼츠 영상 생성을 잇는다.

발행된 글(runs/{slug}) 중 아직 영상(shorts.mp4)이 없는 것을 골라 멀티채널 팩을
생성한다. 영상 존재 여부(shorts.mp4)가 곧 큐이므로 별도 상태파일 불필요·멱등.

트리거:
  · 수동 배치      run_video_batch(limit)         — 순차 실행, 결과 반환(GUI/CLI)
  · 발행 후 자동    spawn_video_batch(limit)       — detached 백그라운드(발행 흐름 비차단)

무거운 실제 렌더는 _lib/build_multichannel_pack.py CLI(--pending N)에 위임한다
(캡션 Claude + edge-tts + faster-whisper + Remotion). 이 모듈은 선별·기동만.
발행 자체는 건드리지 않는다 — 영상은 발행의 부산물일 뿐, 업로드는 별개(Phase③).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent            # naver_blog_v3/
HARNESS_ROOT = BASE_DIR.parent                     # naver_blog_automation_v3/
from config import profile as _nbaprofile
_DATA_HARNESS = _nbaprofile.harness_data_root()

RUNS = _DATA_HARNESS / "runs"
BUILDER = HARNESS_ROOT / "_lib" / "build_multichannel_pack.py"

_SOURCE_FILES = ("post_data.json", "draft.md", "draft_path.txt", "publish_brief.yaml")


def _caption_only() -> bool:
    """캡션 전용 모드(2026-07-19) — 영상은 shorts_studio 자동 운행이 실발행 후 담당."""
    try:
        from config import settings as _s
        return bool(getattr(_s, "AUTO_VIDEO_CAPTION_ONLY", False))
    except ImportError:
        return False


def _has_video(slug: str) -> bool:
    return (RUNS / slug / "multichannel" / "shorts.mp4").exists()


def _has_captions(slug: str) -> bool:
    return (RUNS / slug / "multichannel" / "youtube.json").exists()


def _is_done(slug: str) -> bool:
    """멱등 완료 판별 — 영상이 있으면 항상 완료, 캡션 전용 모드에선 캡션도 완료."""
    if _has_video(slug):
        return True
    return _caption_only() and _has_captions(slug)


def _has_source(slug: str) -> bool:
    d = RUNS / slug
    return any((d / f).exists() for f in _SOURCE_FILES)


def find_pending_video_slugs(limit: int = 5) -> list[str]:
    """미완료(캡션 전용 모드: 캡션 없음 / 일반: shorts.mp4 없음) 최근 발행 글 N개."""
    if limit <= 0 or not RUNS.exists():
        return []
    dirs = sorted((d for d in RUNS.iterdir() if d.is_dir()),
                  key=lambda d: d.name, reverse=True)
    out: list[str] = []
    for d in dirs:
        if _is_done(d.name) or not _has_source(d.name):
            continue
        out.append(d.name)
        if len(out) >= limit:
            break
    return out


def _python(*, windowless: bool = False) -> str:
    """현재 런타임 파이썬. windowless 면 콘솔 없는 pythonw 우선."""
    exe = Path(sys.executable)
    if windowless:
        pw = exe.parent / "pythonw.exe"
        if pw.exists():
            return str(pw)
    return str(exe)


def run_video_batch(limit: int = 5, *, dry_run: bool = False) -> dict:
    """영상 없는 최근 발행 글 N개를 순차 생성(블로킹). 반환: 대상·결과.

    dry_run 이면 대상 슬러그만 반환(생성 안 함).
    """
    slugs = find_pending_video_slugs(limit)
    if dry_run or not slugs:
        return {"slugs": slugs, "count": len(slugs), "dry_run": dry_run,
                "generated": 0, "returncode": None}
    if not BUILDER.exists():
        return {"slugs": slugs, "count": len(slugs), "dry_run": False,
                "generated": 0, "error": f"builder 없음:{BUILDER}"}
    args = [_python(), str(BUILDER), "--pending", str(limit)]
    if _caption_only():
        args.append("--skip-tts")
    log.info(f"[video_batch] 배치 시작({'캡션 전용' if _caption_only() else '캡션+영상'}) "
             f"— 대상 {len(slugs)}편")
    try:
        proc = subprocess.run(args, cwd=str(HARNESS_ROOT), capture_output=True,
                              text=True, encoding="utf-8", errors="replace")
    except OSError as e:
        return {"slugs": slugs, "count": len(slugs), "dry_run": False,
                "generated": 0, "error": str(e)}
    made = sum(1 for s in slugs if _is_done(s))   # 실제 생성된 수(멱등 재확인)
    return {"slugs": slugs, "count": len(slugs), "dry_run": False,
            "generated": made, "returncode": proc.returncode}


def spawn_video_batch(limit: int = 5) -> dict:
    """발행 후 후크 — detached 백그라운드로 영상 배치 기동(발행 흐름 비차단·fire-and-forget).

    대상이 없으면 기동 안 함. 어떤 예외도 발행 흐름을 막지 않는다(항상 dict 반환).
    """
    try:
        slugs = find_pending_video_slugs(limit)
        if not slugs:
            return {"launched": False, "count": 0}
        if not BUILDER.exists():
            return {"launched": False, "count": len(slugs), "error": "builder 없음"}
        args = [_python(windowless=True), str(BUILDER), "--pending", str(limit)]
        if _caption_only():
            args.append("--skip-tts")
        flags = (getattr(subprocess, "CREATE_NO_WINDOW", 0)
                 | getattr(subprocess, "DETACHED_PROCESS", 0))
        subprocess.Popen(
            args, cwd=str(HARNESS_ROOT), creationflags=flags,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, close_fds=True,
        )
        log.info(f"[video_batch] 발행 후 영상 배치 기동(detached) — 대상 {len(slugs)}편")
        return {"launched": True, "count": len(slugs)}
    except (OSError, ValueError) as e:
        log.warning(f"[video_batch] 영상 배치 기동 실패(무시): {e}")
        return {"launched": False, "error": str(e)}


if __name__ == "__main__":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    pending = find_pending_video_slugs(10)
    print(f"영상 없는 최근 발행 글: {len(pending)}편")
    for s in pending:
        print("  ·", s)
