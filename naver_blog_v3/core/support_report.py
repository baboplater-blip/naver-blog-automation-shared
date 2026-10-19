# core/support_report.py — 원격 지원 진단 리포트 (매니지드 서비스 enabler · 2026-07-15)
"""매니지드(정수기) 운영자가 고객 PC를 원격으로 진단하도록, 비밀 값 없이 '건강 상태'만
한 장으로 모은다.

절대 원칙: 자격증명·쿠키·토큰의 '값'을 담지 않는다. 계정 ID는 마스킹, 쿠키는 존재/신선도,
API 키는 유무만. 운영자가 "무엇이 막혔는지"를 파악하기에 충분하되, 이 파일이 유출돼도
계정이 위험하지 않게 한다. 전부 fail-open(어떤 수집 실패도 리포트를 막지 않음).
"""
from __future__ import annotations

import logging
import platform
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)


def _mask_account(value: str) -> str:
    """계정 ID 마스킹 — 앞 2자 + *** (유출 시에도 식별 최소화)."""
    v = (value or "").strip()
    if not v or v == "your_naver_id":
        return "(미설정)"
    if len(v) <= 2:
        return v[0] + "*"
    return v[:2] + "*" * max(len(v) - 3, 1) + v[-1]


def _account_line() -> str:
    try:
        from config.settings import NAVER_ID
        return _mask_account(NAVER_ID)
    except Exception:  # noqa: BLE001
        return "(확인 불가)"


def _profile_name() -> str:
    try:
        from config import brand as _brand
        return _brand.display()
    except Exception:  # noqa: BLE001
        return "default"


def _onboarding_lines() -> list[str]:
    try:
        from core.onboarding_status import check_onboarding
        data = check_onboarding()
    except Exception as e:  # noqa: BLE001
        return [f"- (온보딩 진단 실패: {e})"]
    out = []
    for it in data.get("items", []):
        ok = it.get("ok")
        icon = "✅" if ok is True else ("➖" if ok is None else "⭕")
        out.append(f"- {icon} {it.get('label', it.get('key'))}")
    return out


def _claude_line() -> str:
    try:
        from core.claude_connect import status
        return {"connected": "✅ 연결됨", "installed": "⭕ 설치됨·로그인 필요",
                "missing": "❌ 미설치"}.get(status(), "확인 불가")
    except Exception:  # noqa: BLE001
        return "확인 불가"


def _cookie_line() -> str:
    """쿠키 존재/신선도만 — 값은 절대 노출 안 함."""
    try:
        from config import profile as _p
        ck = _p.cookie_path()
        if not ck.exists():
            return "❌ 없음 (네이버 로그인 필요)"
        age_h = (time.time() - ck.stat().st_mtime) / 3600
        fresh = "🟢 신선" if age_h < 24 else "🟡 오래됨(재로그인 권장)"
        return f"{fresh} · {age_h:.1f}시간 경과"
    except Exception:  # noqa: BLE001
        return "확인 불가"


def _system_lines() -> list[str]:
    out = []
    out.append(f"- Claude CLI: {'✅ 있음' if shutil.which('claude') else '❌ 없음'}")
    out.append(f"- Python: {sys.version.split()[0]}")
    out.append(f"- OS: {platform.system()} {platform.release()}")
    try:
        from config.settings import NAVER_NO_PASSWORD_LOGIN
        out.append(f"- 무비번 로그인: {'ON (수동 로그인 모드)' if NAVER_NO_PASSWORD_LOGIN else 'OFF (무인 지원)'}")
    except Exception:  # noqa: BLE001
        pass
    return out


def _run_summary_lines() -> list[str]:
    """오늘/어제 발행·스킵 요약 — humanize 재사용(사람 말)."""
    try:
        from config import profile as _p
        from config import safe_load_json
        from gui.humanize import summarize_log_entries
        root = _p.app_data_root()
    except Exception as e:  # noqa: BLE001
        return [f"- (실행 요약 불가: {e})"]
    out = []
    for label, d in (("오늘", date.today()),
                     ("어제", date.today() - timedelta(days=1))):
        try:
            entries = safe_load_json(
                root / "logs" / f"{d.strftime('%Y-%m-%d')}.json", default=list)
            s = summarize_log_entries(entries)
            line = f"- {label}: 발행 {s.get('published', 0)} · 안 나간 글 {s.get('skipped', 0)}"
            reasons = s.get("reasons", [])
            if reasons:
                line += " (" + ", ".join(f"{m} {n}" for m, n in reasons[:3]) + ")"
            out.append(line)
        except Exception:  # noqa: BLE001
            out.append(f"- {label}: 요약 불가")
    return out


def build_support_report() -> str:
    """비밀 값 없는 원격 지원 진단 마크다운을 만든다."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        f"# 🛟 원격 지원 진단 리포트",
        f"생성: {now} · 프로필: {_profile_name()} · 계정: {_account_line()}",
        "",
        "> 이 리포트에는 비밀번호·쿠키·토큰의 값이 들어있지 않습니다(존재/상태만).",
        "",
        "## 발행 선행조건",
        *_onboarding_lines(),
        "",
        "## 연결 상태",
        f"- Claude(글 생성 AI): {_claude_line()}",
        f"- 네이버 세션 쿠키: {_cookie_line()}",
        "",
        "## 시스템",
        *_system_lines(),
        "",
        "## 최근 실행",
        *_run_summary_lines(),
        "",
        "## 운영자 다음 행동",
        *_next_actions(),
    ]
    return "\n".join(lines) + "\n"


def _next_actions() -> list[str]:
    """진단 결과로부터 운영자가 할 일을 자동 도출(비밀 값 없이)."""
    actions = []
    try:
        from core.onboarding_status import check_onboarding
        missing = check_onboarding().get("missing", [])
    except Exception:  # noqa: BLE001
        missing = []
    hints = {
        "claude": "Claude 연결 — 설정 탭 '🤖 Claude 연결 → 연결하기'로 로그인",
        "account": "네이버 계정 ID 입력 (설정 탭)",
        "excel": "발행 라인(키워드 엑셀) 지정 (설정 탭)",
        "cookie": "네이버 로그인 — 세션 쿠키 갱신",
        "search_api": "검색 API 자격증명 입력 (선택)",
    }
    for k in missing:
        if k in hints:
            actions.append(f"- ⚠️ {hints[k]}")
    if not actions:
        actions.append("- ✅ 특이사항 없음 — 정상 운영 가능 상태")
    return actions


def save_support_report(out_path: "Path | None" = None) -> Path:
    """리포트를 파일로 저장하고 경로를 반환한다. 기본: 앱 데이터 logs/ 아래."""
    text = build_support_report()
    if out_path is None:
        try:
            from config import profile as _p
            base = _p.app_data_root() / "logs"
        except Exception:  # noqa: BLE001
            base = Path(__file__).resolve().parents[1] / "logs"
        base.mkdir(parents=True, exist_ok=True)
        out_path = base / f"support_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
    out_path.write_text(text, encoding="utf-8")
    log.info("[support_report] 저장: %s", out_path)
    return out_path
