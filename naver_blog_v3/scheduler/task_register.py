# scheduler/task_register.py — Windows Task Scheduler 자동 등록/해제/확인
# schtasks 명령을 사용하여 작업 스케줄러에 등록합니다.
from __future__ import annotations

import re
import subprocess
import sys
import os
import tempfile
import datetime as _dt
from pathlib import Path
from xml.sax.saxutils import escape as _xml_escape

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
from config.win_console import CONSOLE_ENCODING

log = get_logger(__name__)

TASK_NAME = "NaverBlogAutomation"
BACKUP_TASK_NAME = "NaverBlogAutomation_DailyBackup"
WEEKLY_REPORT_TASK_NAME = "NaverBlogAutomation_WeeklyReport"
REFRESH_TRIGGER_TASK_NAME = "NaverBlogAutomation_RefreshTrigger"
TREND_DIVERSITY_TASK_NAME = "NaverBlogAutomation_TrendDiversity"
MULTI_PROFILE_TASK_NAME = "NaverBlogAutomation_MultiProfile"
# 2026-09-18 무인 운영 M1~M4(계약 §2.10) — 준비 시각마다 autonomy_cli.py 를 깨우는
# 작업 + 로그온 시 1회 recover_cli.py 를 돌리는 작업.
AUTONOMY_TASK_NAME = "NaverBlogAutomation_Autonomy"
RECOVER_TASK_NAME = "NaverBlogAutomation_Recover"
# 2026-09-19 — 승인한 **여러 계정**을 한 번에 도는 작업(별도 이름이라 위의 단일 계정
# 작업을 덮어쓰지 않는다). 둘이 동시에 떠도 계정별 점유(lease)·파이프라인 락·다계정
# 러너의 머신 공용 락이 중복 발행을 막는다.
AUTONOMY_MULTI_TASK_NAME = "NaverBlogAutomation_MultiAutonomy"
BASE_DIR  = Path(__file__).parent.parent
# 하네스 외곽 폴더 — _lib/ 등 운영 도구가 위치.
HARNESS_ROOT = BASE_DIR.parent

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def _get_schedule_hours() -> list:
    """settings.py에서 SCHEDULE_HOURS를 읽어옵니다."""
    try:
        import config.settings as s
        from config import reload_settings
        reload_settings()          # 재로드 정책은 config.reload_settings 한 곳.
        return s.SCHEDULE_HOURS
    except Exception as e:
        log.warning(f"SCHEDULE_HOURS 로드 실패, 기본값 사용: {e}")
        return ["06:00", "11:30"]


def _find_pythonw() -> str:
    """현재 Python 환경의 pythonw.exe 경로를 반환합니다."""
    python_dir = Path(sys.executable).parent
    pythonw = python_dir / "pythonw.exe"
    if pythonw.exists():
        return str(pythonw)
    # fallback: python.exe 사용
    return sys.executable


def register_task() -> tuple:
    """
    Windows 작업 스케줄러에 NaverBlogAutomation 작업을 등록합니다.
    SCHEDULE_HOURS의 각 시간에 대해 매일 반복 트리거를 설정합니다.

    Returns:
        (success: bool, message: str)
    """
    hours = _get_schedule_hours()
    if not hours:
        return False, "SCHEDULE_HOURS가 비어 있습니다."

    pythonw   = _find_pythonw()
    run_cli   = str(BASE_DIR / "run_cli.py")
    work_dir  = str(BASE_DIR)

    # 등록 시점의 활성 프로필을 커맨드라인에 못박는다(2026-07-19 격리 감사).
    # 미명시 시 run_cli 가 발화 시점의 .active(마지막 전환 계정)를 따라가
    # 등록해 둔 계정이 아닌 다른 블로그로 발행되는 크로스 계정 위험이 있다.
    try:
        from config import profile as _nbaprofile
        _profile_arg = f' --profile {_nbaprofile.active_profile()}'
    except Exception:  # noqa: BLE001 — 프로필 레이어 부재 시 기존 동작
        _profile_arg = ""

    # 기존 작업 삭제 (재등록을 위해)
    _delete_task_silent()

    errors = []
    for i, hour in enumerate(hours):
        # 첫 번째 시간은 /create, 나머지는 별도 작업명으로 등록
        task_name = TASK_NAME if i == 0 else f"{TASK_NAME}_{i+1}"

        cmd = [
            "schtasks", "/create",
            "/tn", task_name,
            "/tr", f'"{pythonw}" "{run_cli}"{_profile_arg}',
            "/sc", "daily",
            "/st", hour,
            "/f",                   # 이미 있으면 덮어쓰기
            "/rl", "LIMITED",       # 일반 권한
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                errors.append(f"{hour}: {result.stderr.strip()}")
                log.error(f"작업 등록 실패 [{task_name}]: {result.stderr.strip()}")
            else:
                log.info(f"작업 등록 성공 [{task_name}]: 매일 {hour}")
        except subprocess.TimeoutExpired:
            errors.append(f"{hour}: 시간 초과")
        except Exception as e:
            errors.append(f"{hour}: {e}")

    if errors:
        return False, "일부 등록 실패:\n" + "\n".join(errors)

    return True, f"등록 완료: 매일 {', '.join(hours)} 실행"


def unregister_task() -> tuple:
    """
    등록된 NaverBlogAutomation 작업을 모두 해제합니다.

    Returns:
        (success: bool, message: str)
    """
    hours = _get_schedule_hours()
    task_names = [TASK_NAME] + [f"{TASK_NAME}_{i+1}" for i in range(1, len(hours))]

    errors = []
    deleted = 0
    for name in task_names:
        try:
            result = subprocess.run(
                ["schtasks", "/delete", "/tn", name, "/f"],
                capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                deleted += 1
                log.info(f"작업 해제 성공: {name}")
            # returncode != 0이면 이미 없는 작업이므로 무시
        except Exception as e:
            errors.append(f"{name}: {e}")

    if errors:
        return False, "일부 해제 실패:\n" + "\n".join(errors)

    if deleted == 0:
        return True, "해제할 작업이 없습니다 (미등록 상태)."

    return True, f"{deleted}개 작업 해제 완료."


def check_task() -> tuple:
    """
    NaverBlogAutomation 작업의 등록 상태를 확인합니다.

    Returns:
        (registered: bool, message: str)
    """
    try:
        result = subprocess.run(
            ["schtasks", "/query", "/tn", TASK_NAME, "/fo", "LIST"],
            capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode == 0:
            # 등록된 시간 목록 수집
            hours = _get_schedule_hours()
            task_names = [TASK_NAME] + [f"{TASK_NAME}_{i+1}" for i in range(1, len(hours))]
            registered = []
            for name in task_names:
                try:
                    r = subprocess.run(
                        ["schtasks", "/query", "/tn", name, "/fo", "LIST"],
                        capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=10,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
                    if r.returncode == 0:
                        registered.append(name)
                except Exception:
                    pass

            return True, f"등록됨 ({len(registered)}개 작업)"
        else:
            return False, "미등록"
    except Exception as e:
        return False, f"확인 실패: {e}"


def _delete_task_silent():
    """기존 작업을 조용히 삭제합니다 (재등록용)."""
    hours = _get_schedule_hours()
    task_names = [TASK_NAME] + [f"{TASK_NAME}_{i+1}" for i in range(1, len(hours))]
    for name in task_names:
        try:
            subprocess.run(
                ["schtasks", "/delete", "/tn", name, "/f"],
                capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=15,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass


def _register_one_task(task_name: str, script_path: Path, hour: str,
                        sc: str = "daily", extra: list | None = None,
                        script_args: str = "", pin_profile: bool = True) -> tuple:
    """공통 등록 헬퍼 — schtasks /create 한 번 호출.

    Args:
        task_name: 작업 이름.
        script_path: 실행할 .py 파일 경로.
        hour: HH:MM.
        sc: schtasks /sc 인자 (daily | weekly | once 등).
        extra: 추가 schtasks 인자 (예: ["/d", "SUN"]).
        script_args: 스크립트에 넘길 인자 문자열(예: "--gap 180").
        pin_profile: True(기본)면 등록 시점 활성 프로필을 --profile 로 박는다
            (격리 감사 — 발화 시점 .active 표류 방지). multi_profile 처럼
            스스로 전 프로필을 순회하는 스크립트만 False.
    """
    pythonw = _find_pythonw()
    _tr = f'"{pythonw}" "{script_path}"'
    if script_args:
        _tr += f" {script_args}"
    if pin_profile:
        try:
            from config import profile as _nbaprofile
            _tr += f" --profile {_nbaprofile.active_profile()}"
        except Exception:  # noqa: BLE001
            pass
    cmd = [
        "schtasks", "/create",
        "/tn", task_name,
        "/tr", _tr,
        "/sc", sc,
        "/st", hour,
        "/f", "/rl", "LIMITED",
    ]
    if extra:
        cmd.extend(extra)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0:
            log.error(f"{task_name} 등록 실패: {result.stderr.strip()}")
            return False, result.stderr.strip()
        log.info(f"{task_name} 등록 성공: {sc} {hour}")
        return True, f"{task_name} 등록 완료 ({sc} {hour})"
    except subprocess.TimeoutExpired:
        return False, "schtasks 시간 초과"
    except Exception as e:
        return False, str(e)


def _delete_one_task(task_name: str) -> bool:
    """공통 삭제 헬퍼 — silent 실패 허용."""
    try:
        result = subprocess.run(
            ["schtasks", "/delete", "/tn", task_name, "/f"],
            capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return result.returncode == 0
    except Exception:
        return False


def register_backup_task(hour: str = "02:00") -> tuple:
    """매일 hour 시각에 backup_job.py 를 실행하도록 등록.

    Args:
        hour: 백업 실행 시각 HH:MM. 기본 02:00 (저전력 시간대).

    Returns:
        (success, message)
    """
    script = BASE_DIR / "scheduler" / "backup_job.py"
    _delete_one_task(BACKUP_TASK_NAME)
    return _register_one_task(BACKUP_TASK_NAME, script, hour, sc="daily")


def unregister_backup_task() -> tuple:
    ok = _delete_one_task(BACKUP_TASK_NAME)
    return (True, "백업 작업 해제 완료") if ok else (True, "해제할 작업 없음")


def register_weekly_report_task(hour: str = "23:00") -> tuple:
    """매주 일요일 hour 시각에 weekly_report.py 를 실행하도록 등록.

    schtasks /sc weekly /d SUN 사용.
    """
    script = BASE_DIR / "scheduler" / "weekly_report.py"
    _delete_one_task(WEEKLY_REPORT_TASK_NAME)
    return _register_one_task(
        WEEKLY_REPORT_TASK_NAME, script, hour,
        sc="weekly", extra=["/d", "SUN"],
    )


def unregister_weekly_report_task() -> tuple:
    ok = _delete_one_task(WEEKLY_REPORT_TASK_NAME)
    return (True, "주간 리포트 작업 해제 완료") if ok else (True, "해제할 작업 없음")


def register_refresh_trigger_task(hour: str = "01:30") -> tuple:
    """매일 hour 시각에 refresh_trigger.py 를 실행하도록 등록.

    리프레시 큐만 적재하고 실제 재생성은 사용자 트리거 — 자동 발행 금지선 준수.
    """
    script = BASE_DIR / "scheduler" / "refresh_trigger.py"
    _delete_one_task(REFRESH_TRIGGER_TASK_NAME)
    return _register_one_task(REFRESH_TRIGGER_TASK_NAME, script, hour, sc="daily")


def unregister_refresh_trigger_task() -> tuple:
    ok = _delete_one_task(REFRESH_TRIGGER_TASK_NAME)
    return (True, "리프레시 트리거 해제 완료") if ok else (True, "해제할 작업 없음")


def register_trend_diversity_task(hour: str = "07:00",
                                   weekday: str = "MON") -> tuple:
    """매주 weekday hour 시각에 _lib/cluster_trend_keywords.py 를 실행하도록 등록.

    트렌드/후보 키워드 변형 묶음(P2-#09) 을 주 1회 자동 점검 — 결과는
    reports/{date}_trend_diversity.{html,json} 에 떨어지고, 운영자는 변형 묶음별
    대표 1개만 유지하고 나머지를 발행 후보에서 제거해 다양성을 확보한다.

    Args:
        hour: 실행 시각 HH:MM. 기본 07:00 (주간 리포트가 일요일 23:00 이라 결과
            검토 흐름이 자연스럽게 이어지도록 월요일 아침으로 설정).
        weekday: schtasks /d 인자. MON|TUE|WED|THU|FRI|SAT|SUN.

    Returns:
        (success, message)
    """
    script = HARNESS_ROOT / "_lib" / "cluster_trend_keywords.py"
    _delete_one_task(TREND_DIVERSITY_TASK_NAME)
    return _register_one_task(
        TREND_DIVERSITY_TASK_NAME, script, hour,
        sc="weekly", extra=["/d", weekday],
    )


def unregister_trend_diversity_task() -> tuple:
    ok = _delete_one_task(TREND_DIVERSITY_TASK_NAME)
    return (True, "트렌드 다양성 점검 해제 완료") if ok else (True, "해제할 작업 없음")


def register_multi_profile_task(hour: str = "07:00",
                                gap_seconds: int = 180) -> tuple:
    """매일 hour 에 **모든 프로필(자격증명 있는)을 순차 발행**하는 단일 작업 등록.

    하나의 작업이 프로필을 직렬 순회한다(단일 PC의 Chrome/CUDA/MeloTTS 자원 충돌 회피,
    프로필 사이 gap_seconds 간격은 봇 탐지 위험도 낮춤). 기존 단일 블로그 register_task
    (run_cli.py) 대신/함께 쓸 수 있다 — 멀티 블로그 운영 시 이걸 등록.
    """
    script = BASE_DIR / "scheduler" / "multi_profile.py"
    _delete_one_task(MULTI_PROFILE_TASK_NAME)
    return _register_one_task(
        MULTI_PROFILE_TASK_NAME, script, hour, sc="daily",
        script_args=f"--gap {int(gap_seconds)}",
        pin_profile=False,   # 스스로 전 프로필을 순회 — 단일 프로필 고정 부적합
    )


def unregister_multi_profile_task() -> tuple:
    ok = _delete_one_task(MULTI_PROFILE_TASK_NAME)
    return (True, "멀티 프로필 순차 발행 해제 완료") if ok else (True, "해제할 작업 없음")


def check_aux_tasks() -> dict:
    """백업·주간 리포트·리프레시 트리거·트렌드 다양성 작업 등록 상태 조회.
    {task_name: registered_bool}
    """
    out: dict = {}
    for name in (BACKUP_TASK_NAME, WEEKLY_REPORT_TASK_NAME,
                 REFRESH_TRIGGER_TASK_NAME, TREND_DIVERSITY_TASK_NAME,
                 MULTI_PROFILE_TASK_NAME):
        try:
            r = subprocess.run(
                ["schtasks", "/query", "/tn", name, "/fo", "LIST"],
                capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            out[name] = (r.returncode == 0)
        except Exception:
            out[name] = False
    return out


# ═══════════════════════════════════════════════════════════════════
# 무인 운영 M1~M4 (계약 §2.10, 2026-09-18)
#
# 일반 register_task() 는 /sc daily 만 써서 PC 가 꺼져 있던 준비 시각을 그냥
# 건너뛴다(StartWhenAvailable 없음). 무인 운영은 "놓친 슬롯을 PC 켜졌을 때
# 따라잡는다"가 핵심 요구라 Task Scheduler 2.0 XML 로 직접 등록한다.
# ═══════════════════════════════════════════════════════════════════

def _current_user_id() -> str:
    """현재 로그인 사용자(DOMAIN\\User). InteractiveToken 로그온 — 비밀번호 저장 불필요,
    현재 PC 사용자 로그인 상태에서만 실행된다."""
    domain = os.environ.get("USERDOMAIN", "")
    user = os.environ.get("USERNAME", "")
    if not user:
        return ""
    return f"{domain}\\{user}" if domain else user


def _autonomy_settings_xml_block() -> str:
    """무인 운영 작업 공통 <Settings> — 계약 요구사항 그대로:
    StartWhenAvailable(놓친 슬롯 따라잡기)·MultipleInstances=IgnoreNew·
    배터리 무관 실행·절전 깨우기 안 함(WakeToRun=false)."""
    return (
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <AllowHardTerminate>true</AllowHardTerminate>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>\n"
        "    <WakeToRun>false</WakeToRun>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>false</Hidden>\n"
        "    <ExecutionTimeLimit>PT6H</ExecutionTimeLimit>\n"
        "    <Priority>7</Priority>\n"
        "  </Settings>"
    )


def _autonomy_principal_xml_block() -> str:
    """<Principal> — InteractiveToken(현재 로그인 세션에서만 실행)·LeastPrivilege."""
    user_id = _current_user_id()
    user_id_xml = f"\n    <UserId>{_xml_escape(user_id)}</UserId>" if user_id else ""
    # ★ 2026-09-18 실측 — <Principal> 을 <Principals> 로 감싸지 않아 schtasks /xml 이
    #   "작업 XML에 예기치 않은 노드가 있습니다. (16,5):Principal:" 으로 항상 거부했고,
    #   그때마다 /sc daily 폴백으로 등록돼 StartWhenAvailable(놓친 슬롯 따라잡기)·
    #   IgnoreNew 가 빠진 작업이 만들어졌다. 스키마대로 래퍼를 씌운다.
    return (
        "  <Principals>\n"
        '    <Principal id="Author">'
        f"{user_id_xml.replace(chr(10) + '    ', chr(10) + '      ')}\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>"
    )


def _autonomy_actions_xml_block(command: str, arguments: str, work_dir: str) -> str:
    """<Actions> — Command 와 Arguments 를 분리해 한글·공백 경로가 한 토큰으로
    뭉개지지 않게 한다(전부 XML 이스케이프)."""
    return (
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{_xml_escape(command)}</Command>\n"
        f"      <Arguments>{_xml_escape(arguments)}</Arguments>\n"
        f"      <WorkingDirectory>{_xml_escape(work_dir)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>"
    )


def _coerce_not_before(not_before) -> "_dt.date | None":
    """not_before 를 date 로 정규화. None·빈 값·해석 불가는 None(= 제약 없음)."""
    if not not_before:
        return None
    if isinstance(not_before, _dt.datetime):
        return not_before.date()
    if isinstance(not_before, _dt.date):
        return not_before
    try:
        return _dt.date.fromisoformat(str(not_before).strip())
    except ValueError:
        return None


def build_autonomy_task_xml(*, times: list, command: str, arguments: str,
                            work_dir: str, author: str = "",
                            not_before=None) -> str:
    """준비 시각마다 매일 반복되는 <CalendarTrigger> 여러 개를 가진 Task Scheduler 2.0
    XML 을 만든다. times 의 각 항목은 "HH:MM" — 형식이 잘못된 항목은 건너뛰고,
    유효한 시각이 하나도 없으면 ValueError.

    not_before: 첫 실행을 이 날짜(YYYY-MM-DD 문자열 또는 date) 이전으로 잡지 않는다.
    None(기본)이면 예전과 동일 — 지정해도 주기(매일 1회)·나머지 설정은 바뀌지 않고
    StartBoundary 의 **날짜만** 뒤로 밀린다.
    """
    valid_times = [t.strip() for t in (times or [])
                  if isinstance(t, str) and _TIME_RE.match(t.strip())]
    if not valid_times:
        raise ValueError(f"유효한 시각이 없습니다(HH:MM 형식만 허용): {times!r}")

    floor_date = _coerce_not_before(not_before)
    today = _dt.date.today().isoformat()
    # ★ 2026-09-18 — StartBoundary 를 무조건 '오늘'로 잡으면 등록 시점이 이미 그 시각을
    #   지난 경우 `StartWhenAvailable=true` 가 그 슬롯을 '놓친 실행'으로 보고 등록 직후
    #   바로 한 번 돌 수 있다(등록한 날 예정에 없던 편수가 나간다). 이미 지난 시각은
    #   **다음 날**부터 시작하게 한다 — 미래 시각은 예전과 동일(오늘부터).
    _now = _dt.datetime.now()
    triggers = []
    for t in valid_times:
        hh, mm = (int(x) for x in t.split(":"))
        start_date = _dt.date.today()
        if _dt.datetime.combine(start_date, _dt.time(hh, mm)) <= _now:
            start_date = start_date + _dt.timedelta(days=1)
        if floor_date is not None and start_date < floor_date:
            start_date = floor_date
        start = f"{start_date.isoformat()}T{t}:00"
        triggers.append(
            "    <CalendarTrigger>\n"
            f"      <StartBoundary>{_xml_escape(start)}</StartBoundary>\n"
            "      <Enabled>true</Enabled>\n"
            "      <ScheduleByDay>\n"
            "        <DaysInterval>1</DaysInterval>\n"
            "      </ScheduleByDay>\n"
            "    </CalendarTrigger>"
        )

    xml_doc = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Author>{_xml_escape(author)}</Author>\n"
        f"    <Date>{today}T00:00:00</Date>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        + "\n".join(triggers) + "\n"
        "  </Triggers>\n"
        + _autonomy_principal_xml_block() + "\n"
        + _autonomy_settings_xml_block() + "\n"
        + _autonomy_actions_xml_block(command, arguments, work_dir) + "\n"
        "</Task>\n"
    )
    return xml_doc


def _build_logon_task_xml(*, command: str, arguments: str, work_dir: str,
                          author: str = "") -> str:
    """로그온 시 1회 실행되는 <LogonTrigger> XML(recover_cli.py 용). 전용 시그니처가
    계약에 없어 내부 헬퍼로만 둔다 — register_recover_task 만 호출."""
    xml_doc = (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        f"    <Author>{_xml_escape(author)}</Author>\n"
        f"    <Date>{_dt.date.today().isoformat()}T00:00:00</Date>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers>\n"
        "    <LogonTrigger>\n"
        "      <Enabled>true</Enabled>\n"
        "    </LogonTrigger>\n"
        "  </Triggers>\n"
        + _autonomy_principal_xml_block() + "\n"
        + _autonomy_settings_xml_block() + "\n"
        + _autonomy_actions_xml_block(command, arguments, work_dir) + "\n"
        "</Task>\n"
    )
    return xml_doc


def _save_task_xml_utf16(xml_str: str, tag: str) -> Path:
    """schtasks /xml 는 UTF-16 파일만 받는다. 'utf-16' 코덱은 쓰기 시 BOM 을 자동으로
    붙인다(플랫폼 기본 바이트순서) — 명시적으로 BOM 을 더할 필요 없음."""
    fd, tmp_name = tempfile.mkstemp(prefix=f"{tag}_", suffix=".xml")
    os.close(fd)
    path = Path(tmp_name)
    path.write_text(xml_str, encoding="utf-16")
    return path


def _cleanup_temp_xml(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _active_profile_or_blank() -> str:
    try:
        from config import profile as _nbaprofile
        return _nbaprofile.active_profile()
    except Exception:  # noqa: BLE001
        return ""


def _register_task_via_xml(task_name: str, xml_str: str) -> tuple:
    """schtasks /create /xml 로 1회 등록 시도. (ok, message|None, stderr|None)."""
    try:
        xml_path = _save_task_xml_utf16(xml_str, task_name)
    except OSError as e:
        return False, None, f"XML 파일 저장 실패: {e}"
    try:
        result = subprocess.run(
            ["schtasks", "/create", "/tn", task_name, "/xml", str(xml_path), "/f"],
            capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace",
            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as e:  # noqa: BLE001
        return False, None, str(e)
    finally:
        _cleanup_temp_xml(xml_path)

    if result.returncode != 0:
        return False, None, (result.stderr or "").strip() or "알 수 없는 오류"
    return True, f"{task_name} 등록 완료(XML)", None


def _register_daily_fallback(task_name: str, times: list, command: str,
                             arguments: str) -> tuple:
    """XML 등록이 실패했을 때의 폴백 — 기존 /sc daily 방식(시각마다 번호 붙은 작업)."""
    errors = []
    for i, hour in enumerate(times):
        name = task_name if i == 0 else f"{task_name}_{i + 1}"
        cmd = [
            "schtasks", "/create", "/tn", name,
            "/tr", f'"{command}" {arguments}',
            "/sc", "daily", "/st", hour, "/f", "/rl", "LIMITED",
        ]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, encoding=CONSOLE_ENCODING,
                errors="replace", timeout=30, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode != 0:
                errors.append(f"{hour}: {(result.stderr or '').strip()}")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{hour}: {e}")
    if errors:
        return False, "폴백(/sc daily)도 일부 실패:\n" + "\n".join(errors)
    return True, f"/sc daily 방식으로 폴백 등록 완료({len(times)}개 시각)"


def register_autonomy_task(*, times: list, profile: str = "",
                           dry_run: bool = False, not_before=None) -> tuple:
    """준비 시각마다 autonomy_cli.py --profile <name> --run 을 깨우는 작업 등록.

    - XML(schtasks /create /xml) 경로 우선(StartWhenAvailable=true 로 놓친 슬롯도
      PC 켜지면 따라잡는다).
    - XML 등록이 실패하면 기존 /sc daily 방식으로 폴백하고, 폴백했음을 메시지에 남긴다.
    - 같은 이름 작업이 있으면 등록 전에 지운다(중복 등록 회피).
    - dry_run=True 면 schtasks 를 전혀 호출하지 않고 생성될 XML 을 그대로 반환한다
      (등록 전 확인용 — 실제 등록 경로와 동일한 XML 을 만든다).
    - not_before 를 주면 **첫 실행 날짜만** 그 날 이후로 미룬다(주기·설정 불변).
    """
    pythonw = _find_pythonw()
    script = BASE_DIR / "autonomy_cli.py"
    prof = profile or _active_profile_or_blank()
    flags = f"--profile {prof} --run" if prof else "--run"
    arguments = f'"{script}" {flags}'

    xml_str = build_autonomy_task_xml(
        times=times, command=pythonw, arguments=arguments,
        work_dir=str(BASE_DIR), author="NaverBlogAutomation",
        not_before=not_before,
    )

    if dry_run:
        return True, xml_str

    unregister_autonomy_task()   # 중복 등록 회피

    ok, msg, err = _register_task_via_xml(AUTONOMY_TASK_NAME, xml_str)
    if ok:
        log.info(f"{AUTONOMY_TASK_NAME} XML 등록 성공: {len(times)}개 시각")
        return True, msg
    log.warning(f"{AUTONOMY_TASK_NAME} XML 등록 실패({err}) — /sc daily 폴백 시도")
    fb_ok, fb_msg = _register_daily_fallback(
        AUTONOMY_TASK_NAME, [t for t in times if _TIME_RE.match(str(t).strip())],
        pythonw, arguments,
    )
    return fb_ok, f"XML 등록 실패({err}) — {fb_msg}"


def autonomy_multi_times(profiles: list = None) -> list:
    """켜진 계정들의 준비 시각 합집합(중복 제거·정렬). 대상이 없으면 빈 리스트.

    다계정 작업은 "어느 계정의 준비 시각이든 그 시각에 한 번 깨어난다"가 맞다 —
    깨어난 러너가 계정마다 정책을 다시 읽어 지금 돌 계정만 고른다.
    """
    try:
        from scheduler.autonomy_multi import eligible_profiles
        from core import autonomy_policy as _pol
    except Exception as e:  # noqa: BLE001
        log.warning(f"다계정 준비 시각 계산 실패: {e}")
        return []
    times: set = set()
    for row in eligible_profiles(profiles):
        if not row.get("eligible"):
            continue
        try:
            policy = _pol.load_policy(row["profile"])
        except Exception:  # noqa: BLE001
            continue
        for raw in (policy.get("windows") or []):
            text = str(raw).strip()
            if _TIME_RE.match(text):
                times.add(text)
    return sorted(times)


def register_autonomy_multi_task(*, times: list, dry_run: bool = False,
                                 not_before=None) -> tuple:
    """준비 시각마다 `scheduler/autonomy_multi.py` 를 깨우는 작업 등록.

    단일 계정 작업(`AUTONOMY_TASK_NAME`)과 **이름이 다르므로 서로 덮어쓰지 않는다.**
    `dry_run=True` 면 schtasks 를 전혀 호출하지 않고 생성될 XML 을 그대로 반환한다.
    """
    pythonw = _find_pythonw()
    script = BASE_DIR / "scheduler" / "autonomy_multi.py"
    arguments = f'"{script}"'

    xml_str = build_autonomy_task_xml(
        times=times, command=pythonw, arguments=arguments,
        work_dir=str(BASE_DIR), author="NaverBlogAutomation",
        not_before=not_before,
    )
    if dry_run:
        return True, xml_str

    unregister_autonomy_multi_task()   # 자기 이름만 정리(단일 계정 작업 보존)

    ok, msg, err = _register_task_via_xml(AUTONOMY_MULTI_TASK_NAME, xml_str)
    if ok:
        log.info(f"{AUTONOMY_MULTI_TASK_NAME} XML 등록 성공: {len(times)}개 시각")
        return True, msg
    log.warning(f"{AUTONOMY_MULTI_TASK_NAME} XML 등록 실패({err}) — /sc daily 폴백 시도")
    fb_ok, fb_msg = _register_daily_fallback(
        AUTONOMY_MULTI_TASK_NAME, [t for t in times if _TIME_RE.match(str(t).strip())],
        pythonw, arguments,
    )
    return fb_ok, f"XML 등록 실패({err}) — {fb_msg}"


def unregister_autonomy_multi_task() -> tuple:
    names = [AUTONOMY_MULTI_TASK_NAME] + [
        f"{AUTONOMY_MULTI_TASK_NAME}_{i}" for i in range(2, 11)]
    deleted = sum(1 for name in names if _delete_one_task(name))
    if deleted == 0:
        return True, "해제할 작업 없음"
    return True, f"다계정 무인 운영 작업 {deleted}개 해제 완료"


def unregister_autonomy_task() -> tuple:
    """XML 경로·폴백 경로(번호 붙은 작업) 전부 정리."""
    names = [AUTONOMY_TASK_NAME] + [f"{AUTONOMY_TASK_NAME}_{i}" for i in range(2, 11)]
    deleted = sum(1 for name in names if _delete_one_task(name))
    if deleted == 0:
        return True, "해제할 작업 없음"
    return True, f"무인 운영 작업 {deleted}개 해제 완료"


def register_recover_task(*, profile: str = "") -> tuple:
    """로그온 시 1회 recover_cli.py --profile <name> 을 실행하는 작업 등록."""
    pythonw = _find_pythonw()
    script = BASE_DIR / "recover_cli.py"
    prof = profile or _active_profile_or_blank()
    arguments = f'"{script}" --profile {prof}' if prof else f'"{script}"'

    xml_str = _build_logon_task_xml(
        command=pythonw, arguments=arguments, work_dir=str(BASE_DIR),
        author="NaverBlogAutomation",
    )

    unregister_recover_task()

    ok, msg, err = _register_task_via_xml(RECOVER_TASK_NAME, xml_str)
    if ok:
        log.info(f"{RECOVER_TASK_NAME} 등록 성공(로그온 트리거)")
        return True, "복구 작업(로그온 시 1회) 등록 완료"
    log.warning(f"{RECOVER_TASK_NAME} 등록 실패: {err}")
    return False, f"복구 작업 등록 실패: {err}"


def unregister_recover_task() -> tuple:
    ok = _delete_one_task(RECOVER_TASK_NAME)
    return (True, "복구 작업 해제 완료") if ok else (True, "해제할 작업 없음")


def check_autonomy_tasks() -> dict:
    """무인 운영 + 복구 작업 등록 상태. {task_name: {"registered": bool, "detail": str}}"""
    out: dict = {}
    for name in (AUTONOMY_TASK_NAME, AUTONOMY_MULTI_TASK_NAME, RECOVER_TASK_NAME):
        try:
            result = subprocess.run(
                ["schtasks", "/query", "/tn", name, "/fo", "LIST"],
                capture_output=True, text=True, encoding=CONSOLE_ENCODING,
                errors="replace", timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode == 0:
                out[name] = {"registered": True, "detail": (result.stdout or "").strip()}
            else:
                out[name] = {"registered": False,
                             "detail": (result.stderr or "").strip() or "미등록"}
        except Exception as e:  # noqa: BLE001
            out[name] = {"registered": False, "detail": f"확인 실패: {e}"}
    return out


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="NaverBlogAutomation Task Scheduler 관리")
    parser.add_argument(
        "action",
        choices=["register", "unregister", "check",
                 "register_backup", "unregister_backup",
                 "register_weekly", "unregister_weekly",
                 "register_refresh", "unregister_refresh",
                 "register_trend", "unregister_trend",
                 "register_multi", "unregister_multi",
                 "check_aux"],
        help="수행할 동작",
    )
    parser.add_argument("--hour", default=None,
                        help="백업·주간 리포트·트렌드·멀티 시각 HH:MM 오버라이드")
    parser.add_argument("--weekday", default="MON",
                        help="트렌드 다양성 점검 요일 (MON|TUE|...|SUN)")
    parser.add_argument("--gap", type=int, default=180,
                        help="멀티 프로필 순차 발행 시 프로필 사이 대기(초)")
    args = parser.parse_args()

    if args.action == "register":
        ok, msg = register_task()
    elif args.action == "unregister":
        ok, msg = unregister_task()
    elif args.action == "check":
        ok, msg = check_task()
    elif args.action == "register_backup":
        ok, msg = register_backup_task(args.hour or "02:00")
    elif args.action == "unregister_backup":
        ok, msg = unregister_backup_task()
    elif args.action == "register_weekly":
        ok, msg = register_weekly_report_task(args.hour or "23:00")
    elif args.action == "unregister_weekly":
        ok, msg = unregister_weekly_report_task()
    elif args.action == "register_refresh":
        ok, msg = register_refresh_trigger_task(args.hour or "01:30")
    elif args.action == "unregister_refresh":
        ok, msg = unregister_refresh_trigger_task()
    elif args.action == "register_trend":
        ok, msg = register_trend_diversity_task(
            args.hour or "07:00", args.weekday)
    elif args.action == "unregister_trend":
        ok, msg = unregister_trend_diversity_task()
    elif args.action == "register_multi":
        ok, msg = register_multi_profile_task(args.hour or "07:00", args.gap)
    elif args.action == "unregister_multi":
        ok, msg = unregister_multi_profile_task()
    else:  # check_aux
        status = check_aux_tasks()
        lines = [f"  {k}: {'등록됨' if v else '미등록'}" for k, v in status.items()]
        print("부가 작업 상태:\n" + "\n".join(lines))
        sys.exit(0)

    print(f"{'성공' if ok else '실패'}: {msg}")
    sys.exit(0 if ok else 1)
