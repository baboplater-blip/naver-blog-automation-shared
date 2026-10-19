# gui/utils.py — 로그 로더 유틸리티 + 에러 해결 가이드
import json
import re
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent  # naver_blog_v3/
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()



def today_logs() -> list:
    p = _DATA_DIR / "logs" / f"{date.today().strftime('%Y-%m-%d')}.json"
    if p.exists():
        try: return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError): pass
    return []


def all_stats() -> dict:
    logs_dir = _DATA_DIR / "logs"
    total = success = fail = 0
    if logs_dir.exists():
        for f in logs_dir.glob("*.json"):
            if f.name == "post_history.json": continue
            try:
                for item in json.loads(f.read_text(encoding="utf-8")):
                    if not isinstance(item, dict): continue
                    total += 1
                    if item.get("success"): success += 1
                    else: fail += 1
            except (json.JSONDecodeError, FileNotFoundError): pass
    return {"total": total, "success": success, "fail": fail}


# ─────────────────────────────────────────────────────
# 에러별 해결 가이드
# ─────────────────────────────────────────────────────
_ERROR_GUIDES = [
    # (패턴, 사용자 친화적 해결 가이드)
    (re.compile(r"login|로그인|인증", re.IGNORECASE),
     "브라우저에서 네이버 보안 인증을 완료한 후 '인증완료' 버튼을 클릭하세요"),

    (re.compile(r"timeout|시간\s*초과|timed?\s*out", re.IGNORECASE),
     "네트워크 연결을 확인하세요. Claude CLI 타임아웃이면 설정에서 타임아웃 값을 늘려보세요"),

    (re.compile(r"rate\s*limit|usage\s*limit|429|과금|quota", re.IGNORECASE),
     "API 사용량 제한에 도달했습니다. 잠시 후 다시 시도하세요"),

    (re.compile(r"claude|cli|anthropic", re.IGNORECASE),
     "Claude CLI가 설치되어 있는지 확인하세요. 'claude --version' 명령으로 확인 가능합니다"),

    (re.compile(r"excel|엑셀|xlsx|파일.*열|openpyxl", re.IGNORECASE),
     "엑셀 파일이 다른 프로그램에서 열려있지 않은지 확인하세요"),

    (re.compile(r"selenium|driver|chrome|webdriver|브라우저", re.IGNORECASE),
     "Chrome 브라우저와 ChromeDriver 버전이 일치하는지 확인하세요"),

    (re.compile(r"network|connection|연결|socket|dns", re.IGNORECASE),
     "인터넷 연결 상태를 확인하세요. VPN 사용 시 VPN 연결도 확인하세요"),

    (re.compile(r"permission|권한|access denied|접근\s*거부", re.IGNORECASE),
     "파일 또는 폴더 접근 권한을 확인하세요. 관리자 권한으로 실행해 보세요"),
]


def get_error_guide(error_msg: str) -> str:
    """에러 메시지를 분석하여 사용자 친화적 해결 가이드를 반환합니다.

    Args:
        error_msg: 에러 메시지 문자열.

    Returns:
        매칭되는 해결 가이드. 매칭 없으면 빈 문자열.
    """
    if not error_msg:
        return ""
    for pattern, guide in _ERROR_GUIDES:
        if pattern.search(error_msg):
            return guide
    return ""


def format_error_with_guide(error_msg: str) -> str:
    """에러 메시지에 해결 가이드를 붙여 반환합니다.

    Args:
        error_msg: 원본 에러 메시지.

    Returns:
        해결 가이드가 포함된 포맷된 문자열.
        가이드가 없으면 원본 메시지 그대로 반환.
    """
    guide = get_error_guide(error_msg)
    if guide:
        return f"{error_msg}\n  [해결 가이드] {guide}"
    return error_msg
