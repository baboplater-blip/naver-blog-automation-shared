"""autonomy_multi — **승인한 여러 계정**의 무인 운영을 한 번에 돌리는 진입점 (2026-09-19).

기존 `scheduler/multi_profile.py` 는 **자격증명이 있는 전체 계정**을 대상으로
`run_cli.py`(= 수동 발행 파이프라인)를 부른다. 그건 사람이 명시적으로 누르는
수동 경로이고, 지금도 그대로 남는다.

이 모듈은 다르다.

- 대상은 **사용자가 무인 운영을 켠 계정**뿐이다. 자격증명이 있다는 사실은
  승인으로 취급하지 않는다.
- 부르는 것은 `autonomy_cli.py --profile <name> --run` — 즉 계정 정책
  (범위·하루 편수·준비 시각·호출 상한)이 **유일한 판정자**다.
- 계정마다 **별도 프로세스**로 돈다(한 프로세스 = 한 프로필 규약). 프로필은
  `--profile` 인자와 `NBA_PROFILE` 환경변수 **둘 다**로 못박아, 자식이 어떤
  config 를 임포트하기 전에 고정되게 한다.
- 전체 비상정지(estop)는 **루프 시작 전과 계정마다** 다시 확인한다 — 첫 계정이
  도는 동안 사람이 누른 정지가 다음 계정에 반영되지 않으면 '정지'가 아니다.
- 한 계정이 실패하거나 시간을 다 써도 **다음 계정은 그대로 돈다.**
- 타임아웃 회수는 우리가 띄운 프로세스의 **정확한 PID 트리만** 정리한다.
  커맨드라인 문자열 검색(부분 일치)으로 남의 프로세스를 죽이지 않는다.

중복 실행 방지는 세 겹이다: ① 이 러너의 머신 공용 락(두 개의 다계정 실행이
겹치지 않음) ② 계정별 `work_queue` 점유(lease) ③ 파이프라인의
`pipeline.lock`. 그래서 단일 계정 작업과 다계정 작업이 같은 시각에 떠도 같은
글이 두 번 나가지 않는다.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

APP_DIR = Path(__file__).resolve().parent.parent      # naver_blog_v3/
AUTONOMY_CLI = APP_DIR / "autonomy_cli.py"

#: 계정 하나가 쓸 수 있는 최대 시간(초). 넘으면 그 계정만 정리하고 다음으로 간다.
DEFAULT_TIMEOUT_SECONDS = 3600

#: 머신 공용 중복 실행 방지 락 파일 이름.
_LOCK_FILE = "autonomy_multi.lock"

#: 제외 사유 → 사람이 읽는 문구.
SKIP_LABELS = {
    "estop": "전체 비상정지 중",
    "master_off": "무인 운영 마스터가 꺼져 있음",
    "account_off": "이 계정의 무인 운영이 꺼져 있음",
    "pause": "이 계정이 일시중지 중",
    "policy_error": "설정을 읽지 못함",
    "cancelled": "실행 중 취소됨",
    "locked": "다른 다계정 실행이 이미 돌고 있음",
}


# ─────────────────────────────────────────────────────
# 대상 선정 — "켠 계정만"
# ─────────────────────────────────────────────────────
def _master_on() -> bool:
    try:
        from core import autonomy_guard as _guard
        return bool(_guard.master_enabled())
    except Exception as exc:  # noqa: BLE001 — 판정 불가는 꺼짐으로 본다(fail-closed)
        log.warning(f"[autonomy_multi] 마스터 스위치 확인 실패({type(exc).__name__}) — 꺼짐으로 처리")
        return False


def estop_active() -> bool:
    try:
        from core import kill_switch as _ks
        return bool(_ks.estop_active())
    except Exception as exc:  # noqa: BLE001 — 모듈 부재는 정지 아님(기존 동작)
        log.debug(f"[autonomy_multi] 비상정지 확인 생략({type(exc).__name__})")
        return False


def _paused(name: str, policy: dict) -> bool:
    try:
        from core import kill_switch as _ks
        return bool(_ks.pause_active(profile=name))
    except Exception:  # noqa: BLE001
        return bool((policy or {}).get("pause"))


def eligible_profiles(profiles: Optional[list] = None) -> list:
    """각 계정의 실행 가능 여부와 사유. **아무것도 실행하지 않는다.**

    Returns:
        [{"profile","eligible","scope","reason","label"}] — 이름 오름차순.
    """
    # ★ 같은 데이터 폴더를 가리키는 이름은 하나로 합친다. 이 저장소의 `default` 는
    #   `profiles/.default_data` 로 재배치될 수 있어 named 프로필과 **같은 블로그**일
    #   수 있다(실측). 합치지 않으면 한 블로그에 두 프로세스를 띄운다.
    try:
        from core.autonomy_overview import distinct_accounts
        names = distinct_accounts(profiles)
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[autonomy_multi] 계정 목록 조회 실패({type(exc).__name__})")
        names = sorted(set(n for n in (profiles or []) if n))

    master_on = _master_on()
    stopped = estop_active()

    out: list = []
    for name in names:
        row = {"profile": name, "eligible": False, "scope": "", "reason": "", "label": ""}
        try:
            from core import autonomy_policy as _pol
            policy = _pol.load_policy(name)
        except Exception as exc:  # noqa: BLE001
            row["reason"] = "policy_error"
            row["label"] = f"{SKIP_LABELS['policy_error']}({type(exc).__name__})"
            out.append(row)
            continue
        row["scope"] = str(policy.get("scope") or "")
        if stopped:
            row["reason"] = "estop"
        elif not master_on:
            row["reason"] = "master_off"
        elif not policy.get("enabled"):
            row["reason"] = "account_off"
        elif _paused(name, policy):
            row["reason"] = "pause"
        row["eligible"] = not row["reason"]
        row["label"] = SKIP_LABELS.get(row["reason"], "") if row["reason"] else "실행 대상"
        out.append(row)
    return out


# ─────────────────────────────────────────────────────
# 머신 공용 중복 실행 방지
# ─────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class MultiRunLock:
    """다계정 실행이 겹치지 않게 하는 머신 공용 PID 락(죽은 락은 회수)."""

    def __init__(self) -> None:
        self._path: Optional[Path] = None
        self._acquired = False

    def acquire(self) -> bool:
        try:
            from core.machine_paths import machine_file
            self._path = machine_file(_LOCK_FILE)
        except Exception as exc:  # noqa: BLE001 — 락 경로를 못 잡으면 락 없이 진행
            log.warning(f"[autonomy_multi] 락 경로 확인 실패({type(exc).__name__}) — 락 없이 진행")
            return True
        for _attempt in (1, 2):
            try:
                fd = os.open(str(self._path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                raw = ""
                try:
                    raw = self._path.read_text(encoding="utf-8").strip()
                except OSError:
                    pass
                try:
                    pid = int(raw) if raw else 0
                except ValueError:
                    pid = 0
                if pid and pid != os.getpid() and _pid_alive(pid):
                    return False
                try:
                    self._path.unlink()
                except OSError:
                    return False
                continue
            except OSError as exc:
                log.warning(f"[autonomy_multi] 락 생성 실패({type(exc).__name__}) — 락 없이 진행")
                return True
            try:
                os.write(fd, str(os.getpid()).encode("utf-8"))
            finally:
                os.close(fd)
            self._acquired = True
            return True
        return False

    def release(self) -> None:
        if not self._acquired or self._path is None:
            return
        try:
            raw = self._path.read_text(encoding="utf-8").strip()
            if raw and int(raw) == os.getpid():
                self._path.unlink()
        except (OSError, ValueError):
            pass
        self._acquired = False


# ─────────────────────────────────────────────────────
# 자식 프로세스
# ─────────────────────────────────────────────────────
def _default_spawn(args: list, *, env: dict, cwd: str):
    """실제 프로세스 기동. 테스트는 `spawn_fn` 주입으로 이 경로를 절대 타지 않는다."""
    kwargs: dict = {"cwd": cwd, "env": env,
                    "stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(args, **kwargs)  # noqa: S603


def _terminate_tree(pid: int) -> bool:
    """정확히 이 PID 가 뿌리인 트리만 종료한다(커맨드라인 문자열 검색 없음)."""
    try:
        from scheduler.multi_profile import _terminate_process_tree
        return bool(_terminate_process_tree(pid))
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[autonomy_multi] 프로세스 트리 종료 실패(PID {pid}): {type(exc).__name__}")
        return False


def _run_one(name: str, *, python_exe: str, timeout_seconds: int,
             spawn_fn: Callable) -> dict:
    """계정 하나 — 별도 프로세스로 `autonomy_cli --profile <name> --run`."""
    args = [python_exe, str(AUTONOMY_CLI), "--profile", name, "--run"]
    env = dict(os.environ, NBA_PROFILE=name)
    result = {"profile": name, "ok": False, "returncode": None,
              "timed_out": False, "error": "", "pid": None}
    try:
        proc = spawn_fn(args, env=env, cwd=str(APP_DIR))
    except Exception as exc:  # noqa: BLE001 — 기동 실패는 이 계정만의 실패
        result["error"] = f"{type(exc).__name__}: {exc}"
        log.warning(f"[autonomy_multi] ✗ '{name}' 실행 실패: {exc}")
        return result

    result["pid"] = getattr(proc, "pid", None)
    try:
        proc.communicate(timeout=timeout_seconds)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        result["timed_out"] = True
        result["error"] = "timeout"
        pid = getattr(proc, "pid", None)
        killed = _terminate_tree(pid) if pid else False
        result["killed"] = bool(killed)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001 — 이미 종료됐을 수 있다
            pass
        try:
            proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            pass
        log.warning(f"[autonomy_multi] ⏱ '{name}' 타임아웃({timeout_seconds}s) — "
                    f"PID {pid} 트리 종료 {'성공' if killed else '실패'}")
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["returncode"] = rc
    # autonomy_cli 종료 코드: 0 정상 / 1 오류 / 2 사람 확인 필요(실패 아님).
    result["ok"] = rc in (0, 2)
    result["needs_attention"] = rc == 2
    log.info(f"[autonomy_multi] ✓ '{name}' 종료 rc={rc}")
    return result


def run_autonomy_profiles(profiles: Optional[list] = None, *,
                          gap_seconds: int = 0, dry_run: bool = False,
                          python_exe: Optional[str] = None,
                          timeout_seconds: Optional[int] = None,
                          spawn_fn: Optional[Callable] = None,
                          cancel_cb: Optional[Callable] = None) -> dict:
    """승인된 계정들을 차례로 무인 실행한다.

    Args:
        profiles: 대상 후보(미지정이면 전체 계정). 후보라도 **켜지지 않은 계정은
            실행하지 않는다** — 승인은 계정 정책이 정한다.
        dry_run: 프로세스를 전혀 띄우지 않고 대상·사유만 돌려준다.
        spawn_fn: 프로세스 기동 함수 주입(테스트 전용). 기본은 `subprocess.Popen`.
        cancel_cb: 계정마다 호출해 True 면 이후 계정을 실행하지 않는다(사용자 취소).

    Returns:
        {"started","ran":[...],"skipped":[...],"stopped":"", "dry_run":bool}.
    """
    out = {"started": "", "ran": [], "skipped": [], "stopped": "",
           "dry_run": bool(dry_run), "locked": False}
    from datetime import datetime as _dt
    out["started"] = _dt.now().isoformat(timespec="seconds")

    rows = eligible_profiles(profiles)
    for row in rows:
        if not row["eligible"]:
            out["skipped"].append({"profile": row["profile"], "reason": row["reason"],
                                   "label": row["label"]})
    targets = [r["profile"] for r in rows if r["eligible"]]
    out["targets"] = list(targets)

    if dry_run:
        return out
    if not targets:
        out["stopped"] = "no_target"
        return out

    lock = MultiRunLock()
    if not lock.acquire():
        out["locked"] = True
        out["stopped"] = "locked"
        for name in targets:
            out["skipped"].append({"profile": name, "reason": "locked",
                                   "label": SKIP_LABELS["locked"]})
        log.warning("[autonomy_multi] 다른 다계정 실행이 이미 돌고 있어 이번 실행은 건너뜁니다")
        return out

    python_exe = python_exe or sys.executable
    if not timeout_seconds or timeout_seconds <= 0:
        timeout_seconds = _timeout_default()
    spawn = spawn_fn or _default_spawn

    try:
        total = len(targets)
        for index, name in enumerate(targets):
            # ★ 루프 **중에도** 정지·취소를 다시 확인한다.
            if estop_active():
                out["stopped"] = "estop"
                for rest in targets[index:]:
                    out["skipped"].append({"profile": rest, "reason": "estop",
                                           "label": SKIP_LABELS["estop"]})
                log.warning("[autonomy_multi] ⛔ 비상정지 감지 — 남은 계정을 실행하지 않습니다")
                break
            if cancel_cb is not None:
                try:
                    cancelled = bool(cancel_cb())
                except Exception:  # noqa: BLE001 — 취소 확인 실패는 계속 진행
                    cancelled = False
                if cancelled:
                    out["stopped"] = "cancelled"
                    for rest in targets[index:]:
                        out["skipped"].append({"profile": rest, "reason": "cancelled",
                                               "label": SKIP_LABELS["cancelled"]})
                    break

            log.info(f"[autonomy_multi] ▶ [{index + 1}/{total}] '{name}' 무인 사이클 시작")
            # 한 계정의 실패가 다음 계정을 막지 않는다.
            out["ran"].append(_run_one(name, python_exe=python_exe,
                                       timeout_seconds=timeout_seconds,
                                       spawn_fn=spawn))
            if gap_seconds and index < total - 1:
                time.sleep(gap_seconds)
    finally:
        lock.release()
    return out


def _timeout_default() -> int:
    try:
        import config.settings as settings
        value = int(getattr(settings, "AUTONOMY_PROFILE_TIMEOUT",
                            getattr(settings, "MULTI_PROFILE_TIMEOUT",
                                    DEFAULT_TIMEOUT_SECONDS)))
        return value if value > 0 else DEFAULT_TIMEOUT_SECONDS
    except Exception:  # noqa: BLE001
        return DEFAULT_TIMEOUT_SECONDS


# ─────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────
def _print(obj) -> None:
    text = json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    try:
        if sys.stdout is None:
            raise OSError("no stdout")
        print(text)
    except (OSError, ValueError, AttributeError, UnicodeEncodeError):
        log.info(text)


def main(argv=None) -> int:
    """종료 코드: 0 전부 성공(또는 대상 0건) / 1 일부 실패 / 2 전부 실패."""
    ap = argparse.ArgumentParser(
        description="승인된 여러 계정의 무인 운영 사이클을 차례로 실행")
    ap.add_argument("--profiles", default="",
                    help="쉼표구분 계정 목록(미지정 시 전체에서 켜진 계정만)")
    ap.add_argument("--gap", type=int, default=0, help="계정 사이 대기(초)")
    ap.add_argument("--dry-run", action="store_true",
                    help="실행하지 않고 대상·제외 사유만 출력")
    ap.add_argument("--status", action="store_true",
                    help="전체 계정 현황만 출력(외부 호출 0회)")
    ap.add_argument("--timeout", type=int, default=None,
                    help="계정당 타임아웃(초). 미지정 시 설정값(기본 3600)")
    args = ap.parse_args(argv)

    if args.status:
        try:
            from core.autonomy_overview import overview
            _print(overview())
            return 0
        except Exception as exc:  # noqa: BLE001
            log.error(f"현황을 읽지 못했습니다: {exc}")
            return 1

    names = [p.strip() for p in args.profiles.split(",") if p.strip()] or None
    result = run_autonomy_profiles(names, gap_seconds=args.gap,
                                   dry_run=args.dry_run,
                                   timeout_seconds=args.timeout)
    _print(result)
    if args.dry_run:
        return 0
    ran = result.get("ran") or []
    if not ran:
        return 0
    ok = sum(1 for r in ran if r.get("ok"))
    if ok == len(ran):
        return 0
    return 2 if ok == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
