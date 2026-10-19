"""scheduler/backup_job.py — 매일 자동 백업.

매일 02:00 Windows 작업 스케줄러가 호출하면 다음 폴더의 핵심 산출물을
{harness_root}/backups/{YYYY-MM-DD}.zip 로 묶어 보관한다.

대상:
- naver_blog_v3/config/         (settings.py, user_settings.json — 평문 자격증명은
                                 user_settings.json 에 암호화 상태로만 존재)
- naver_blog_v3/logs/           (일별 발행 로그, blog_stats.json)
- runs/                          (post_data.json, _status.json — 미발행 보관본)
- briefs/                        (라우팅 브리프)
- reports/                       (운영 리포트)
- errors/                        (에러 트리아지)

대상에서 제외:
- naver_blog_v3/output/, previews/, screenshots/   (이미지·미리보기 — 용량 큼)
- naver_blog_v3/image_inbox/                       (생성 중간물)
- naver_blog_v3/_runtime/                          (포터블 파이썬)
- .py.bak.* 파일                                    (config_io 자동 백업)

보관 정책:
- 일별 zip 30일 유지 (`MAX_BACKUP_DAYS = 30`).
- 일요일은 별도로 `weekly_{ISO주차}.zip` 로 8주 유지.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import zipfile
from datetime import datetime, timedelta
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parent.parent  # naver_blog_v3/
_HARNESS_ROOT = _PKG_ROOT.parent                     # naver_blog_automation_v3/
# ⚠️ sys.path 삽입은 config import **이전**이어야 한다 — 종전엔 뒤에 있어
# schtasks 직접 실행이 ImportError 로 죽던 잠복 버그(2026-07-19 격리 감사에서 발견).
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
if __name__ == "__main__":  # schtasks 직접 실행 — 임포트 전 프로필 고정
    from config.profile_boot import pin_profile_from_argv
    pin_profile_from_argv()
from config import profile as _nbaprofile
_DATA_HARNESS = _nbaprofile.harness_data_root()


if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

log = logging.getLogger(__name__)

# 프로필별 백업(2026-07-19 격리 감사 후속) — 공유 폴더면 프로필끼리 같은
# 날짜 zip 이름(2026-07-19.zip)을 서로 덮어쓴다(라이브 실측로 확인).
BACKUP_ROOT = _DATA_HARNESS / "backups"
MAX_BACKUP_DAYS = 30
MAX_WEEKLY_BACKUPS = 8

# 백업 대상 (소스 절대경로, archive 내부 경로)
# config·logs 도 프로필 라우팅(2026-07-19 격리 감사) — 레거시 고정이면 named
# 프로필의 자격증명·발행로그가 백업에서 빠지고 default 것이 섞여 들어간다.
_BACKUP_TARGETS: tuple[tuple[Path, str], ...] = (
    (_nbaprofile.config_dir(),  "naver_blog_v3/config"),
    (_nbaprofile.logs_dir(),    "naver_blog_v3/logs"),
    (_DATA_HARNESS / "runs",    "runs"),
    (_DATA_HARNESS / "briefs",  "briefs"),
    (_DATA_HARNESS / "reports", "reports"),
    (_HARNESS_ROOT / "errors",  "errors"),
)

# 파일/폴더 제외 패턴 (이름 끝매칭)
_EXCLUDE_SUFFIXES: tuple[str, ...] = (
    ".pyc",
    ".bak",
    ".log",  # 로테이션 활성 중인 현재 로그 — 잠금 충돌 방지
)
# 폴더 이름 제외
_EXCLUDE_DIR_NAMES: tuple[str, ...] = (
    "__pycache__",
    ".pytest_cache",
)


def _should_skip(path: Path) -> bool:
    """파일·디렉토리가 백업에서 제외되어야 하는지."""
    name = path.name
    if any(name == d for d in _EXCLUDE_DIR_NAMES):
        return True
    if any(name.endswith(suf) for suf in _EXCLUDE_SUFFIXES):
        return True
    # config_io 자동 백업: settings.py.bak.YYYYMMDD_HHMMSS
    if ".py.bak." in name:
        return True
    return False


def _iter_files(root: Path):
    """root 하위 모든 파일을 _should_skip 필터링으로 순회."""
    if not root.exists():
        return
    for dirpath, dirnames, filenames in os.walk(root):
        # 디렉토리도 제외 필터 적용 — in-place 수정으로 os.walk 가 들어가지 않게
        dirnames[:] = [d for d in dirnames if not _should_skip(Path(d))]
        for fname in filenames:
            fp = Path(dirpath) / fname
            if _should_skip(fp):
                continue
            yield fp


def _build_zip(zip_path: Path) -> dict:
    """현재 시점의 백업 zip 을 생성. 결과 통계 dict 반환."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    stats = {"files": 0, "bytes": 0, "skipped": 0, "errors": 0}

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for source_root, arc_root in _BACKUP_TARGETS:
            if not source_root.exists():
                stats["skipped"] += 1
                continue
            for fp in _iter_files(source_root):
                try:
                    rel = fp.relative_to(source_root)
                    arcname = f"{arc_root}/{rel.as_posix()}"
                    zf.write(fp, arcname=arcname)
                    stats["files"] += 1
                    stats["bytes"] += fp.stat().st_size
                except (OSError, PermissionError) as exc:
                    stats["errors"] += 1
                    log.debug(f"[backup] {fp} 읽기 실패: {exc}")
    return stats


def _prune_old_backups() -> int:
    """일별 zip 30일 + 주별 zip 8주 보관 정책 적용. 삭제한 개수 반환."""
    if not BACKUP_ROOT.exists():
        return 0

    now = datetime.now()
    daily_cutoff = now - timedelta(days=MAX_BACKUP_DAYS)
    weekly_cutoff = now - timedelta(weeks=MAX_WEEKLY_BACKUPS)

    removed = 0
    for child in BACKUP_ROOT.iterdir():
        if not child.is_file() or not child.suffix == ".zip":
            continue
        name = child.stem
        try:
            mtime = datetime.fromtimestamp(child.stat().st_mtime)
        except OSError:
            continue
        if name.startswith("weekly_"):
            if mtime < weekly_cutoff:
                _safe_unlink(child)
                removed += 1
        else:
            if mtime < daily_cutoff:
                _safe_unlink(child)
                removed += 1
    return removed


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
        log.info(f"[backup] 만료 백업 삭제: {path.name}")
    except OSError as exc:
        log.warning(f"[backup] 삭제 실패 {path.name}: {exc}")


def run_backup(*, now: datetime | None = None) -> dict:
    """일별 백업 실행 + 일요일이면 주간 백업 사본 추가 + 보관 정책 적용.

    Returns:
        {"daily_zip": Path, "weekly_zip": Path|None, "stats": {...}, "pruned": N}
    """
    now = now or datetime.now()
    date_tag = now.strftime("%Y-%m-%d")
    daily_zip = BACKUP_ROOT / f"{date_tag}.zip"
    log.info(f"[backup] 시작 — {daily_zip}")
    stats = _build_zip(daily_zip)

    weekly_zip = None
    # weekday(): Monday=0..Sunday=6 — 일요일에 주간 사본 만든다
    if now.weekday() == 6:
        iso_year, iso_week, _ = now.isocalendar()
        weekly_zip = BACKUP_ROOT / f"weekly_{iso_year}W{iso_week:02d}.zip"
        # 단순 복사보다 한 번 더 빌드(시점 일관성 유지)
        weekly_stats = _build_zip(weekly_zip)
        stats["weekly_files"] = weekly_stats["files"]

    pruned = _prune_old_backups()
    log.info(f"[backup] 완료 — files={stats['files']}, bytes={stats['bytes']:,}, "
             f"pruned={pruned}")
    return {
        "daily_zip": daily_zip,
        "weekly_zip": weekly_zip,
        "stats": stats,
        "pruned": pruned,
    }


def _setup_cli_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="naver-blog-automation 자동 백업")
    parser.add_argument("--quiet", action="store_true", help="로그 출력 최소화")
    args = parser.parse_args(argv)

    if not args.quiet:
        _setup_cli_logging()

    try:
        result = run_backup()
    except Exception as exc:  # noqa: BLE001 — 백업은 어떤 예외도 메인 흐름 안 막음
        log.exception(f"[backup] 실패: {exc}")
        return 1

    if not args.quiet:
        print(f"daily:   {result['daily_zip']}")
        if result["weekly_zip"]:
            print(f"weekly:  {result['weekly_zip']}")
        s = result["stats"]
        print(f"files:   {s['files']}")
        print(f"bytes:   {s['bytes']:,}")
        print(f"pruned:  {result['pruned']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
