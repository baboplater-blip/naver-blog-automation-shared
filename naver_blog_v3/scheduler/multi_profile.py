"""여러 블로그 프로필 순차(직렬) 발행 러너 (Phase 4, 2026-07-05).

**설계(페이블5 운영 결론)**: 단일 PC·단일 Chrome/CUDA/MeloTTS 자원에서는 프로필별
작업을 동시에 띄우면 자원이 충돌한다 → **하나의 러너가 프로필을 직렬로 순회**한다.
각 프로필은 **별도 subprocess**(`run_cli.py --profile <name>`)로 실행한다:
  · 경로 상수가 import-시점에 프로필로 고정되므로(한 프로세스=한 프로필) 한 프로세스
    루프로 여러 프로필을 돌리면 첫 프로필 경로에 얼어붙는다 → subprocess 로 분리.
  · 프로필 사이 자연스러운 간격(gap)은 봇 탐지 위험도 낮춘다.
  · PipelineLock 은 프로필별 logs/pipeline.lock(데이터 루트 스위치로 이미 격리)이라
    프로필 간 상호 차단 없이 각자 이중 실행만 방지된다.

Windows 작업 스케줄러엔 이 러너 **하나**를 등록한다(task_register.register_multi_profile_task).

**2026-09-18 무인 운영 M1~M4 — 타임아웃 강화(계약 §2.9)**:
자식이 멈추면 이 러너가 무한 대기해 스케줄러가 "실행 중"으로 영원히 남는 사고를 막기
위해 `subprocess.run(..., timeout=)` 을 도입했다. 다만 표준 라이브러리의
`subprocess.run()` 은 타임아웃이 나도 **내부 Popen 객체를 호출자에게 노출하지 않아**
PID 를 알 수 없고, 그 결과 죽은 프로세스의 **자식(그랜드차일드, 예: 크롬)** 까지는
못 죽인다(`process.kill()` 은 직계 자식 1개만). PID 를 얻으려 `subprocess.Popen` 으로
직접 바꾸면, 기존 테스트(`tests/test_multi_profile.py`)가 `subprocess.run` 을
monkeypatch 하는 구조라 그 목킹이 무력화되고 **테스트 중 실제 서브프로세스가 뜨는
사고 위험**이 생긴다(절대 금지 §0-1 위반). 그래서 `subprocess.run` 호출 방식은
그대로 유지하고, 타임아웃 시엔 **커맨드라인 서명(스크립트 경로+프로필명)으로 살아있는
프로세스를 찾아 `taskkill /T /F`(Windows) 로 트리를 종료**한다 — 이 탐색·종료 호출도
전부 `subprocess.run` 을 거치므로 기존 목킹 계약과 100% 호환된다.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
from config import profile as pf

log = get_logger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent      # naver_blog_v3/
RUN_CLI = APP_DIR / "run_cli.py"

_DEFAULT_TIMEOUT_SECONDS = 3600


def list_runnable_profiles() -> list[str]:
    """발행 준비된 프로필(자격증명 있는 것만). default(주 블로그) 포함, 정렬."""
    out: list[str] = []
    for name in pf.list_profiles():
        try:
            if pf.describe(name).get("has_credentials"):
                out.append(name)
        except Exception:  # noqa: BLE001 — describe 실패는 스킵
            continue
    return out


def _multi_profile_timeout_default() -> int:
    """settings.MULTI_PROFILE_TIMEOUT (없으면 3600초). settings.py 는 수정하지 않으므로
    getattr 기본값으로 읽는다."""
    try:
        import config.settings as settings
        value = int(getattr(settings, "MULTI_PROFILE_TIMEOUT", _DEFAULT_TIMEOUT_SECONDS))
        return value if value > 0 else _DEFAULT_TIMEOUT_SECONDS
    except Exception:  # noqa: BLE001 — settings 로드 실패는 기본값으로
        return _DEFAULT_TIMEOUT_SECONDS


def _estop_is_active() -> bool:
    """core.kill_switch.estop_active() 지연 import. 모듈이 아직 없으면(다른 작업자가
    구현 중) 무시하고 False(정지 아님)로 취급한다 — fail-open for absence, 존재할 때의
    판정 자체는 kill_switch 내부 정책(fail-closed)을 그대로 따른다."""
    try:
        from core import kill_switch
    except Exception:  # noqa: BLE001 — 모듈 부재/임포트 실패는 무시
        return False
    try:
        return bool(kill_switch.estop_active())
    except Exception:  # noqa: BLE001 — 판정 실패도 무시(다른 작업자 모듈의 책임)
        return False


def _find_pids_by_commandline(needles: list[str]) -> list[int]:
    """CommandLine 에 needles 문자열을 전부 포함하는 살아있는 프로세스의 PID 목록.

    `subprocess.run()` 은 타임아웃 시 내부 Popen 의 PID 를 넘겨주지 않으므로, 우리가
    직접 넘긴 커맨드라인 서명(스크립트 절대경로 + 프로필명)으로 역으로 찾는다.
    최선노력 — 조회 실패는 빈 리스트(예외 전파 금지).
    """
    if not needles:
        return []
    try:
        if os.name == "nt":
            conditions = " -and ".join(
                f"($_.CommandLine -like '*{_ps_escape(n)}*')" for n in needles
            )
            script = (
                "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
                + conditions + " } | Select-Object -ExpandProperty ProcessId"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            import re as _re
            pattern = ".*".join(_re.escape(n) for n in needles)
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15,
            )
        pids: list[int] = []
        for line in (result.stdout or "").splitlines():
            line = line.strip()
            if line.isdigit():
                pids.append(int(line))
        return pids
    except Exception as e:  # noqa: BLE001 — 조회 실패는 빈 목록(최선노력)
        log.warning(f"[multi] 프로세스 조회 실패: {e}")
        return []


def _ps_escape(value: str) -> str:
    """PowerShell 작은따옴표 문자열 리터럴 안전 이스케이프."""
    return value.replace("'", "''")


def _terminate_process_tree(pid: int) -> bool:
    """PID 가 뿌리인 프로세스 트리를 종료(Windows: taskkill /T /F, 그 외: 프로세스 그룹).
    최선노력 — 실패해도 예외를 던지지 않는다."""
    if not pid or pid == os.getpid():
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.returncode == 0
        except Exception as e:  # noqa: BLE001
            log.warning(f"[multi] taskkill 실패(PID {pid}): {e}")
            return False
    else:
        import signal
        try:
            os.killpg(pid, signal.SIGKILL)
            return True
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
                return True
            except Exception as e:  # noqa: BLE001
                log.warning(f"[multi] kill 실패(PID {pid}): {e}")
                return False


def _kill_matching_processes(needles: list[str]) -> list[int]:
    """needles 로 식별되는 프로세스를 찾아 트리 종료. 회수(종료 성공)한 PID 목록 반환."""
    killed: list[int] = []
    for pid in _find_pids_by_commandline(needles):
        if _terminate_process_tree(pid):
            killed.append(pid)
    return killed


def run_profiles_serial(profiles: list[str] | None = None, *,
                        gap_seconds: int = 0, dry_run: bool = False,
                        python_exe: str | None = None,
                        timeout_seconds: int | None = None) -> list[dict]:
    """프로필들을 순차 발행. 각 프로필 = 별도 subprocess(run_cli.py --profile).

    profiles=None 이면 자격증명 있는 전체. 반환: 프로필별 결과 리스트.
    한 프로필 실패가 다음 프로필을 막지 않는다(각각 격리).

    timeout_seconds=None 이면 settings.MULTI_PROFILE_TIMEOUT(기본 3600초) 사용.
    타임아웃이 나면 자식 프로세스 트리를 종료하고
    {"profile","ok":False,"error":"timeout","returncode":None,"timed_out":True} 를 기록한다.

    비상정지(kill_switch.estop_active())가 걸려 있으면 **아무 것도 시작하지 않고**
    빈 리스트를 반환한다.
    """
    if _estop_is_active():
        log.warning("[multi] ⛔ 비상정지(estop) 활성 — 프로필 실행을 시작하지 않습니다")
        return []

    names = profiles if profiles is not None else list_runnable_profiles()
    python_exe = python_exe or sys.executable
    if timeout_seconds is None or timeout_seconds <= 0:
        timeout_seconds = _multi_profile_timeout_default()
    results: list[dict] = []
    total = len(names)
    for i, name in enumerate(names):
        if dry_run:
            results.append({"profile": name, "dry_run": True})
            continue
        args = [python_exe, str(RUN_CLI), "--profile", name]
        env = dict(os.environ, NBA_PROFILE=name)   # 자식 프로필 고정(이중 안전)
        log.info(f"[multi] ▶ [{i + 1}/{total}] 프로필 '{name}' 발행 시작")
        try:
            proc = subprocess.run(args, cwd=str(APP_DIR), env=env,
                                  capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout_seconds)
            results.append({"profile": name, "returncode": proc.returncode,
                            "ok": proc.returncode == 0})
            log.info(f"[multi] ✓ 프로필 '{name}' 종료 rc={proc.returncode}")
        except subprocess.TimeoutExpired:
            log.warning(
                f"[multi] ⏱ 프로필 '{name}' 타임아웃({timeout_seconds}s) — "
                f"자식 프로세스 트리 종료 시도"
            )
            killed = _kill_matching_processes([str(RUN_CLI), "--profile", name])
            results.append({"profile": name, "ok": False, "error": "timeout",
                            "returncode": None, "timed_out": True,
                            "killed_pids": killed})
            log.warning(f"[multi] ✗ 프로필 '{name}' 타임아웃 종료 — 회수 PID {killed}")
        except OSError as e:
            results.append({"profile": name, "error": str(e), "ok": False})
            log.warning(f"[multi] ✗ 프로필 '{name}' 실행 실패: {e}")
        # 마지막 프로필 뒤엔 대기 안 함
        if gap_seconds and i < total - 1:
            time.sleep(gap_seconds)
    return results


def main(argv=None) -> int:
    """종료 코드: 대상 0건 → 0 / 전부 성공 → 0 / 일부 실패 → 1 / 전부 실패 → 2."""
    import argparse
    ap = argparse.ArgumentParser(description="여러 블로그 프로필 순차 발행")
    ap.add_argument("--profiles", default="",
                    help="쉼표구분 프로필 목록(미지정 시 자격증명 있는 전체)")
    ap.add_argument("--gap", type=int, default=0, help="프로필 사이 대기(초)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--timeout", type=int, default=None,
                    help="프로필당 타임아웃(초). 미지정 시 설정값(기본 3600)")
    args = ap.parse_args(argv)
    profiles = [p.strip() for p in args.profiles.split(",") if p.strip()] or None
    names = profiles if profiles is not None else list_runnable_profiles()
    if not names:
        log.info("[multi] 순차 발행 대상 없음 - 종료")
        return 0
    log.info(f"[multi] 순차 발행 대상 {len(names)}개: {', '.join(names)}")
    results = run_profiles_serial(profiles, gap_seconds=args.gap, dry_run=args.dry_run,
                                  timeout_seconds=args.timeout)
    if args.dry_run:
        log.info(f"[multi] dry-run 완료 - {len(results)}건 미리보기")
        return 0
    if not results:
        # estop 등으로 시작 자체를 안 함(대상은 있었으나 0건 실행)
        log.warning("[multi] 실행된 프로필 0건(비상정지 등으로 시작하지 못함)")
        return 0
    ok = sum(1 for r in results if r.get("ok"))
    total = len(results)
    fail = total - ok
    log.info(f"[multi] 완료 - 성공 {ok}/{total} (실패 {fail})")
    if fail == 0:
        return 0
    if ok == 0:
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
