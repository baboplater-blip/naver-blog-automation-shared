# gui/profile_actions.py — GUI 프로필 액션 (Tk 비의존·테스트 가능, Phase 3, 2026-07-05)
"""GUI 프로필 탭/마법사가 쓰는 순수 로직. Tkinter 의존 없음(단위 테스트 가능).

- add_blog: 새 블로그 프로필 생성 + 자격증명 암호화 저장(+선택 이관) + 브랜드 스캐폴드.
- relaunch_args / relaunch_with_profile: 그 프로필로 앱을 **새 프로세스**로 기동
  (한 프로세스=한 프로필 원칙 — 전환은 재기동). 현재 창은 호출측이 유지/종료 결정.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from config import profile as pf

# GUI 진입점 — 재기동 대상.
_APP_ROOT = Path(__file__).resolve().parent.parent      # naver_blog_v3/
GUI_ENTRY = _APP_ROOT / "gui_app.py"


def slugify(text: str) -> str:
    """임의 문자열 → 안전한 프로필 폴더 ID(영문 소문자·숫자·하이픈).

    폴더 ID 는 실제 폴더명·환경변수·작업 스케줄러 이름으로 쓰여 ASCII 여야 안전하다
    (이 환경은 한글 경로에서 렌더·분석 도구가 깨진 전례가 있다). 한글 등 비ASCII 는
    제거되므로 결과가 빈 문자열일 수 있고, 그때는 호출측이 다른 소스로 폴백한다.
    """
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").strip().lower())
    return re.sub(r"-{2,}", "-", s).strip("-")[:40]


def _resolve_folder_id(typed: str, label: str, naver_id: str) -> tuple[str, bool]:
    """폴더 ID 결정. 사용자가 유효 영문 ID 를 직접 주면 그대로(수동), 아니면
    (입력값→표시명→네이버 아이디) 순으로 자동 슬러그. 반환: (id, 자동유도여부).
    id 가 "" 면 유도 실패(순수 비ASCII)."""
    typed = (typed or "").strip()
    if typed and pf.is_valid_name(typed) and not pf.is_default(typed):
        return typed, False
    pid = slugify(typed) or slugify(label) or slugify(naver_id)
    if pf.is_default(pid):
        pid = ""
    return pid, True


def add_blog(name: str, naver_id: str, naver_pw: str, *,
             migrate: bool = False, brand_name: str = "") -> dict:
    """새 블로그 프로필 생성 + 자격증명(암호화) + 브랜드 스캐폴드(+선택 레거시 이관).

    `name` 은 폴더 ID(영문·선택). 비우거나 한글이면 표시명·네이버 아이디에서 자동 생성한다.
    `brand_name` 은 화면 표기명(한글 가능) — 목록·영상 브랜딩에 쓰인다.
    반환: {"ok", "error"?, "root", "folder", "display", "account"(마스킹), "migrated"?}.
    자격증명은 Fernet 암호화로 저장(평문 미저장). named 전용.
    """
    label = (brand_name or "").strip()
    if not naver_id or not naver_pw:
        return {"ok": False, "error": "네이버 아이디·비밀번호가 필요합니다"}

    pid, auto = _resolve_folder_id(name, label, naver_id)
    if not pid:
        return {"ok": False,
                "error": "폴더 ID를 만들 수 없습니다 — 영문 폴더 ID를 직접 입력해 주세요"}
    if not pf.is_valid_name(pid) or pf.is_default(pid):
        return {"ok": False, "error": "이름 규칙 위반(영문·숫자·하이픈·언더스코어, 'default' 불가)"}
    if pf.profile_exists(pid):
        if not auto:                      # 사용자가 직접 준 ID 가 충돌 → 알린다
            return {"ok": False, "error": f"이미 존재하는 프로필: {pid}"}
        base = pid[:38]                   # 자동 유도 충돌 → -2, -3… 로 유일화
        for n in range(2, 100):
            cand = f"{base}-{n}"
            if not pf.profile_exists(cand):
                pid = cand
                break
        else:
            return {"ok": False, "error": f"프로필 이름 충돌 해소 실패: {pid}"}

    root = pf.create_profile(pid)
    # 브랜드 스캐폴드 — 정체성 + 안전 플래그. 도메인 프롬프트 필드(writer_role 등)는
    # 비워두어도 brand.py 가 '중립 기본값'을 쓰므로 정부 문구가 새지 않는다(doctor 가 안내).
    bj = root / "brand" / "brand.json"
    if not bj.exists():
        disp = label or pid.upper()
        scaffold = {
            "name": pid,
            "display": disp,
            "channel": pid,
            "blog_name": disp,
            "web_search": True,        # 최신 정보 반영(일반 블로그 기본 ON)
            "deadline_tracker": False,  # 정부 공고 발행큐 주입 차단(정부 블로그만 true)
        }
        bj.write_text(json.dumps(scaffold, ensure_ascii=False, indent=2), encoding="utf-8")
    pf.set_account(pid, naver_id, naver_pw)
    migrated = pf.migrate_legacy_to_profile(pid) if migrate else None
    # 생성 직후 발행 준비 점검(오류 사전 차단) — 값 비노출.
    try:
        from config import profile_doctor
        health = profile_doctor.diagnose(pid)
        health_report = profile_doctor.report(pid)
    except Exception as e:  # noqa: BLE001 — 점검 실패가 생성 성공을 무르게 하지 않음
        health, health_report = None, f"(점검 생략: {e})"
    return {"ok": True, "root": str(root), "folder": pid, "display": label or pid,
            "account": pf.describe(pid)["naver_id_masked"], "migrated": migrated,
            "health": health, "health_report": health_report}


def update_blog(name: str, *, display: str = "",
                naver_id: str = "", naver_pw: str = "") -> dict:
    """기존 named 프로필 수정 — 표시명(브랜드) 그리고/또는 계정(암호화 재저장).

    빈 값은 '유지'로 해석한다(표시명 비움=그대로, 아이디·비번 둘 다 비움=계정 유지).
    계정은 아이디·비밀번호를 **함께** 줘야 바뀐다(하나만 오면 오류).
    반환: {"ok", "error"?, "changed":[...], "display", "account"(마스킹)}.
    """
    if pf.is_default(name):
        return {"ok": False, "error": "default 프로필은 여기서 수정할 수 없습니다(‘설정’ 탭에서 계정 변경)"}
    if not pf.profile_exists(name):
        return {"ok": False, "error": f"존재하지 않는 프로필: {name}"}
    display = (display or "").strip()
    naver_id = (naver_id or "").strip()
    naver_pw = (naver_pw or "").strip()
    changed: list[str] = []

    if display:
        root = pf.profile_root(name)
        bj = root / "brand" / "brand.json"
        data: dict = {}
        if bj.exists():
            try:
                data = json.loads(bj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        if not isinstance(data, dict):
            data = {}
        data.setdefault("name", name)
        data.setdefault("channel", name)
        data["display"] = display
        bj.parent.mkdir(parents=True, exist_ok=True)
        bj.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        changed.append("표시명")

    if naver_id or naver_pw:
        if not (naver_id and naver_pw):
            return {"ok": False,
                    "error": "계정 변경은 아이디·비밀번호를 함께 입력하세요(둘 다 비우면 유지)"}
        pf.set_account(name, naver_id, naver_pw)
        changed.append("계정")

    if not changed:
        return {"ok": False, "error": "변경할 내용이 없습니다"}
    from config import brand as _br
    return {"ok": True, "changed": changed, "display": _br.display(name),
            "account": pf.describe(name)["naver_id_masked"]}


def delete_blog(name: str) -> dict:
    """named 프로필 삭제(계정·쿠키·데이터 통째). default·현재 사용 중 프로필은 불가.

    ⚠️ 되돌릴 수 없다 — GUI 가 사용자 확인을 받은 뒤 호출한다.
    반환: {"ok", "error"?}.
    """
    if pf.is_default(name):
        return {"ok": False, "error": "default 프로필은 삭제할 수 없습니다"}
    if name == pf.active_profile():
        return {"ok": False,
                "error": "현재 이 창에서 사용 중인 프로필입니다 — 다른 프로필 창에서 삭제하세요"}
    if not pf.profile_exists(name):
        return {"ok": False, "error": f"존재하지 않는 프로필: {name}"}
    try:
        pf.delete_profile(name)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def relaunch_args(name: str, *, python_exe: str = "",
                  gui_entry: Path | None = None) -> tuple[list[str], dict]:
    """그 프로필로 GUI 를 새로 띄울 (args, env). 테스트 가능(실행 안 함).

    자식은 새 콘솔(``CREATE_NEW_CONSOLE``)에서 뜨는데, Windows 기본 코드페이지가
    cp949 라 한글 로그·print 가 ``UnicodeEncodeError`` 를 낸다. ``PYTHONUTF8``·
    ``PYTHONIOENCODING`` 을 주입해 자식의 stdio 를 UTF-8 로 강제한다.
    """
    python_exe = python_exe or sys.executable
    entry = gui_entry or GUI_ENTRY
    env = dict(os.environ,
               NBA_PROFILE=name,
               PYTHONUTF8="1",
               PYTHONIOENCODING="utf-8")
    return [python_exe, str(entry)], env


def relaunch_with_profile(name: str, *, gui_entry: Path | None = None):
    """그 프로필로 앱을 **새 인스턴스**로 기동. 반환한 Popen 으로 기동 성공을 확인한 뒤
    호출측이 현재 창을 닫으면 '한 창에서 계정만 바꿔 끼우기'가 된다."""
    args, env = relaunch_args(name, gui_entry=gui_entry)
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    return subprocess.Popen(args, env=env, cwd=str(_APP_ROOT), close_fds=True,
                            creationflags=flags)


def profile_badge_text(name: str | None = None) -> str:
    """사이드바 배지 문자열 — '🔖 프로필 · 마스킹계정'(오발행 방지 상시 표기)."""
    d = pf.describe(name)
    acct = d["naver_id_masked"] or ("계정 없음" if not d["has_credentials"] else "")
    tail = f" · {acct}" if acct else ""
    return f"🔖 {d['profile']}{tail}"
