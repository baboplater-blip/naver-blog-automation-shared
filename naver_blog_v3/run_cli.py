# run_cli.py — GUI 없이 파이프라인을 실행하는 CLI 엔트리포인트
# Windows Task Scheduler에서 호출: pythonw.exe run_cli.py [--profile <name>]

import argparse
import os
import sys
from pathlib import Path

# 프로젝트 루트를 sys.path에 추가
sys.path.insert(0, str(Path(__file__).parent))

# ── 프로필 고정(한 프로세스=한 프로필, 2026-07-19 격리 감사) ─────────────
# logger 를 포함한 **어떤 config import 보다 먼저** 프로필을 env 로 못박는다.
# --profile 지정 시 그 값, 미지정 시 기동 시점의 .active — 이후 .active 가
# 바뀌어도 이 프로세스(와 자식)는 흔들리지 않는다. main() 의 argparse 는
# --help 표기용으로 유지(같은 값 재설정이라 무해).
_pre = argparse.ArgumentParser(add_help=False)
_pre.add_argument("--profile", default="")
_pre_args, _ = _pre.parse_known_args()
if _pre_args.profile:
    os.environ["NBA_PROFILE"] = _pre_args.profile
else:
    try:
        from config import profile as _pf_boot
        os.environ.setdefault("NBA_PROFILE", _pf_boot.active_profile())
    except Exception:  # noqa: BLE001 — 프로필 레이어 부재 시 기존 동작
        pass

from config.logger import get_logger

log = get_logger("run_cli")


def main() -> int:
    """파이프라인 실행 후 exit code 반환 (0=성공, 1=실패)"""
    # ⚠️ 프로필은 auto_scheduler import 전에 확정해야 데이터·계정 경로가 그 프로필로 고정된다.
    ap = argparse.ArgumentParser(description="파이프라인 CLI 실행")
    ap.add_argument("--profile", default="", help="활성 프로필(멀티 블로그). 미지정 시 .active/default")
    ap.add_argument("--publish", action="store_true", help="실제 발행을 명시적으로 허용")
    args = ap.parse_args()
    if args.profile:
        os.environ["NBA_PROFILE"] = args.profile

    from config import profile as _pf
    _desc = _pf.describe()
    log.info("=" * 54)
    log.info(f"CLI 모드 파이프라인 시작 — 프로필 [{_desc['profile']}] "
             f"계정 {_desc['naver_id_masked'] or '(자격증명 없음)'}")
    log.info("=" * 54)

    try:
        from scheduler.auto_scheduler import run_pipeline
        result = run_pipeline(save_only=not args.publish)

        total   = result.get("total", 0)
        success = result.get("success", 0)
        error   = result.get("error")

        if error:
            log.error(f"파이프라인 오류: {error}")
            return 1

        if total == 0:
            log.info("포스팅할 항목 없음 — 정상 종료")
            return 0

        if success == 0:
            log.error(f"전체 실패: {total}건 시도, 성공 0건")
            return 1

        log.info(f"파이프라인 완료: {success}/{total}건 성공")
        return 0

    except Exception as e:
        log.error(f"파이프라인 예외 발생: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
