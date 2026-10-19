# core/metrics_contract.py
# 📏 성과 수치의 단일 정규화 계약 (2026-09-19)
# ─────────────────────────────────────────────────────────────────────────────
# 이 모듈이 존재하는 이유
#   성과 값이 세 경로(자동 수집 · 레거시 CSV 인제스트 · 검증 CSV 인제스트)로 들어오는데
#   각자 다른 방식으로 숫자를 만들고 있었다. 특히 `int(raw or 0)` 계열은
#     · 값이 없을 때 0
#     · True/False 를 1/0
#     · "조회수 없음" 같은 문자열에서 0
#   을 만들어, **결측을 "0회 조회"로 학습**시킬 수 있었다. 0 과 결측은 다른 사실이다.
#
#   여기서 정의하는 것은 세 가지다.
#     ① 수치 강제(coerce_count) — 결측/무효/진짜 0 을 구분한다.
#     ② 귀속 증거(identity)와 지표 의미(basis) — "이 숫자가 어느 글의 무엇인가".
#     ③ 수집 실패 분류(classify_fetch_failure) — 401/403/429/5xx/로그인/파싱.
#
# 정직성 계약
#   · 없는 데이터를 만들어 성공시키지 않는다. 판단이 안 서면 invalid/unknown 이다.
#   · 의미가 확인되지 않은 수치(basis=unknown)는 **비교·환류에 쓰지 않는다**.
#     기록은 남긴다 — 관측은 하되 판단하지 않는다.
from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional, Tuple

# ── 값 상태 ──────────────────────────────────────────
STATUS_OK = "ok"            # 실제 수치(0 포함)
STATUS_MISSING = "missing"  # 칸/필드 자체가 없거나 빈 값 — 0 이 아니다
STATUS_INVALID = "invalid"  # 있는데 수치로 볼 수 없다(불리언·음수·NaN·문자)

# ── 지표 의미(기간) ──────────────────────────────────
BASIS_PERIOD = "period"          # 지정한 창(window) 안의 합계 — 창끼리 비교 가능
BASIS_CUMULATIVE = "cumulative"  # 발행 이후 누적 총계 — 기간값과 섞으면 안 된다
BASIS_UNKNOWN = "unknown"        # 의미 미확인 — 관측만 하고 비교에 쓰지 않는다

# 다음 기획(환류)에 쓸 수 있는 의미는 기간값뿐이다.
COMPARABLE_BASIS = frozenset({BASIS_PERIOD})

# ── 귀속 증거 ────────────────────────────────────────
IDENTITY_NONE = ""
IDENTITY_LOG_NO = "log_no"                  # 원격 글 번호 일치 — 가장 강함
IDENTITY_TITLE_EXACT = "title_exact"        # 정규화 제목 완전 일치
IDENTITY_TITLE_SUBSTRING = "title_substring"
IDENTITY_TITLE_FUZZY = "title_fuzzy"        # 토큰 유사도 — 단독으로는 신뢰하지 않는다

# 성과를 **귀속해도 되는** 증거. 유사도만으로 다른 글에 값을 붙이지 않는다.
TRUSTED_IDENTITY = frozenset({IDENTITY_LOG_NO, IDENTITY_TITLE_EXACT})

# ── 수집 실패 분류 ───────────────────────────────────
FAILURE_NONE = ""
FAILURE_UNAUTHORIZED = "unauthorized"    # HTTP 401
FAILURE_FORBIDDEN = "forbidden"          # HTTP 403
FAILURE_RATE_LIMITED = "rate_limited"    # HTTP 429
FAILURE_SERVER = "server_error"          # HTTP 5xx
FAILURE_HTTP = "http_error"              # 그 밖의 4xx
FAILURE_NETWORK = "network_error"        # 연결·타임아웃
FAILURE_LOGIN_PAGE = "login_required"    # 로그인 HTML 이 돌아옴(세션 만료)
FAILURE_PARSE = "parse_error"            # JSON 이 아님
FAILURE_SHAPE = "shape_mismatch"         # JSON 이지만 글별 지표 형식이 아님

_MINUTE = 60
_HOUR = 60 * _MINUTE
_DAY = 24 * _HOUR

# 실패별 재시도 대기(초). 사람이 조치해야 풀리는 실패일수록 길게 둔다 —
# 영구 403 을 매 실행마다 두드리면 계정에 부담만 주고 얻는 정보가 없다.
RETRY_AFTER_SEC: Dict[str, int] = {
    FAILURE_UNAUTHORIZED: 6 * _HOUR,
    FAILURE_FORBIDDEN: 24 * _HOUR,
    FAILURE_RATE_LIMITED: 1 * _HOUR,
    FAILURE_SERVER: 15 * _MINUTE,
    FAILURE_HTTP: 6 * _HOUR,
    FAILURE_NETWORK: 10 * _MINUTE,
    FAILURE_LOGIN_PAGE: 30 * _MINUTE,
    FAILURE_PARSE: 6 * _HOUR,
    FAILURE_SHAPE: 24 * _HOUR,
}

# 연속 실패마다 대기를 2배로 늘리되(최대 8배) 이 상한을 넘지 않는다.
MAX_RETRY_AFTER_SEC: int = 7 * _DAY
MAX_BACKOFF_MULTIPLIER: int = 8

# 사용자가 실제로 할 수 있는 조치. "권한 헤더를 추가하세요" 같은 **출처 없는 추정은 넣지 않는다**.
USER_ACTION: Dict[str, str] = {
    FAILURE_UNAUTHORIZED: "네이버 로그인(쿠키 갱신) 후 다시 확인하세요.",
    FAILURE_FORBIDDEN: ("이 계정·경로로는 글별 지표를 자동으로 읽지 못합니다. "
                        "네이버 블로그 통계 화면에서 파일을 내려받아 '통계 파일 불러오기'로 넣어 주세요."),
    FAILURE_RATE_LIMITED: "요청이 많아 잠시 막혔어요. 시간을 두고 다시 확인하세요.",
    FAILURE_SERVER: "네이버 쪽 일시 오류로 보입니다. 잠시 후 다시 확인하세요.",
    FAILURE_HTTP: "자동 수집 경로가 응답을 거부했어요. 통계 파일 불러오기를 사용하세요.",
    FAILURE_NETWORK: "네트워크 연결을 확인한 뒤 다시 시도하세요.",
    FAILURE_LOGIN_PAGE: "세션이 만료돼 로그인 화면이 돌아왔어요. 네이버 로그인(쿠키 갱신)이 필요합니다.",
    FAILURE_PARSE: "자동 수집 응답을 해석하지 못했어요. 통계 파일 불러오기를 사용하세요.",
    FAILURE_SHAPE: "자동 수집 응답에 글별 지표가 없어요. 통계 파일 불러오기를 사용하세요.",
}

FAILURE_LABEL: Dict[str, str] = {
    FAILURE_UNAUTHORIZED: "인증 필요(401)",
    FAILURE_FORBIDDEN: "권한 거부(403)",
    FAILURE_RATE_LIMITED: "요청 제한(429)",
    FAILURE_SERVER: "서버 오류(5xx)",
    FAILURE_HTTP: "응답 거부",
    FAILURE_NETWORK: "연결 실패",
    FAILURE_LOGIN_PAGE: "로그인 화면 반환",
    FAILURE_PARSE: "응답 해석 실패",
    FAILURE_SHAPE: "지표 형식 아님",
}

# 수치 방어 — 조회수가 이 값을 넘으면 파싱 사고로 본다(개인 블로그 기준 비현실적).
MAX_COUNT: int = 1_000_000_000

_LOG_NO_RE = re.compile(r"^\d{1,20}$")
_INT_RE = re.compile(r"^[+-]?\d+$")
# 숫자 뒤에 붙는 단위만 허용한다 — "조회수 없음" 처럼 숫자가 섞인 문장은 통과하지 못한다.
_UNIT_SUFFIX_RE = re.compile(r"(회|명|건|뷰|views?|view)\s*$", re.IGNORECASE)
_THOUSAND_SEP_RE = re.compile(r"[,\s ]")

# 로그인 화면이 돌아왔는지 — 응답 본문의 특징적인 신호만 본다(쿠키 값은 보지 않는다).
_LOGIN_HTML_MARKERS = ("nid.naver.com", "nidlogin", "/nidlogin.login",
                       "login.naver.com", "로그인이 필요")


# ─────────────────────────────────────────────────────
# ① 수치 강제
# ─────────────────────────────────────────────────────
def coerce_count(raw: Any) -> Tuple[Optional[int], str]:
    """성과 수치 하나를 정규화한다.

    Returns:
        `(value, status)` — status 는 `ok`/`missing`/`invalid`.
        `ok` 일 때만 value 가 int 다(진짜 0 도 `ok`). 나머지는 항상 None.

    거부하는 것(전부 의도적):
        · `True`/`False` — 불리언은 수치가 아니다(파이썬에서 int 의 서브클래스라 그냥 두면 1/0 이 된다).
        · 음수 — 조회수에 음수는 없다. 파싱 사고로 본다.
        · `NaN`/`inf` — 비교가 성립하지 않는다.
        · 부분 숫자("2026년 조회 없음" 등) — 전체가 수치일 때만 받는다.
    """
    if raw is None:
        return None, STATUS_MISSING
    if isinstance(raw, bool):
        return None, STATUS_INVALID
    if isinstance(raw, int):
        return (raw, STATUS_OK) if 0 <= raw <= MAX_COUNT else (None, STATUS_INVALID)
    if isinstance(raw, float):
        if math.isnan(raw) or math.isinf(raw) or not float(raw).is_integer():
            return None, STATUS_INVALID
        value = int(raw)
        return (value, STATUS_OK) if 0 <= value <= MAX_COUNT else (None, STATUS_INVALID)

    text = str(raw).strip()
    if not text:
        return None, STATUS_MISSING
    text = _UNIT_SUFFIX_RE.sub("", text).strip()
    text = _THOUSAND_SEP_RE.sub("", text)
    if text.endswith(".0"):
        text = text[:-2]
    if not text:
        return None, STATUS_MISSING
    if not _INT_RE.match(text):
        return None, STATUS_INVALID
    try:
        value = int(text)
    except ValueError:      # 방어 — 정규식을 통과한 값은 여기 오지 않는다
        return None, STATUS_INVALID
    return (value, STATUS_OK) if 0 <= value <= MAX_COUNT else (None, STATUS_INVALID)


def normalize_log_no(raw: Any) -> str:
    """원격 글 번호. 숫자 1~20자리만 인정하고 나머지는 ""(식별자 없음)."""
    if raw is None or isinstance(raw, bool):
        return ""
    text = str(raw).strip()
    return text if _LOG_NO_RE.match(text) else ""


# ─────────────────────────────────────────────────────
# ② 표준 행
# ─────────────────────────────────────────────────────
def normalize_metric_row(raw: Any, *, source: str, collected_at: str,
                         basis: str = BASIS_UNKNOWN,
                         identity: str = IDENTITY_LOG_NO) -> Dict[str, Any]:
    """수집기 응답 한 줄 → 표준 행. 판단 근거를 전부 동반한다.

    `valid` 는 "이 행이 어느 글인지 확실한가"(엄격한 log_no)이고,
    `has_metric` 은 "쓸 수 있는 수치가 있는가"다. 둘은 다른 질문이다 —
    글은 알지만 조회수는 모를 수 있고, 그때 0 을 적으면 사고다.
    """
    row = raw if isinstance(raw, dict) else {}
    log_no = normalize_log_no(row.get("log_no") if "log_no" in row else row.get("logNo"))
    if not log_no:
        log_no = normalize_log_no(row.get("contentId"))
    title = str(row.get("title") or row.get("contentTitle") or "").strip()

    views_raw = _first_present(row, ("views", "readCount", "cv"))
    inflow_raw = _first_present(row, ("search_inflow", "searchInflow", "inflow"))
    views, views_status = coerce_count(views_raw)
    inflow, inflow_status = coerce_count(inflow_raw)

    invalid_reason = ""
    if not log_no:
        invalid_reason = "no_log_no"
    elif views_status == STATUS_INVALID:
        invalid_reason = "views_invalid"

    return {
        "log_no": log_no,
        "title": title,
        "views": views,
        "views_status": views_status,
        "search_inflow": inflow,
        "search_inflow_status": inflow_status,
        "basis": basis if basis in (BASIS_PERIOD, BASIS_CUMULATIVE, BASIS_UNKNOWN) else BASIS_UNKNOWN,
        "source": str(source or ""),
        "collected_at": str(collected_at or ""),
        "identity": identity if log_no else IDENTITY_NONE,
        "valid": bool(log_no) and views_status != STATUS_INVALID,
        "has_metric": bool(log_no) and views_status == STATUS_OK,
        "invalid_reason": invalid_reason,
    }


def _first_present(row: dict, names: tuple) -> Any:
    """이름 후보 중 **실제로 존재하는 첫 키**의 값. 전부 없으면 None(=결측).

    `row.get(a) or row.get(b)` 로 쓰면 값이 0 일 때 다음 후보로 넘어가 버린다 —
    0 은 유효한 조회수이므로 키 존재 여부로 판단한다.
    """
    for name in names:
        if name in row:
            return row[name]
    return None


def is_comparable(basis: Any, identity: Any = IDENTITY_LOG_NO) -> bool:
    """이 값을 **다른 글과 비교하고 다음 기획에 반영해도 되는가.**

    기간 의미가 확정된 값(period)이고, 귀속 증거가 신뢰 범위일 때만 참이다.
    """
    return (str(basis or BASIS_UNKNOWN) in COMPARABLE_BASIS
            and str(identity or IDENTITY_LOG_NO) in TRUSTED_IDENTITY)


# ─────────────────────────────────────────────────────
# ③ 수집 실패 분류
# ─────────────────────────────────────────────────────
def classify_http_status(code: Any) -> str:
    try:
        status = int(code)
    except (TypeError, ValueError):
        return FAILURE_HTTP
    if status == 401:
        return FAILURE_UNAUTHORIZED
    if status == 403:
        return FAILURE_FORBIDDEN
    if status == 429:
        return FAILURE_RATE_LIMITED
    if 500 <= status <= 599:
        return FAILURE_SERVER
    return FAILURE_HTTP


def classify_exception(exc: BaseException) -> str:
    """예외 → 실패 분류. HTTP 상태가 있으면 그것을 우선한다."""
    code = getattr(exc, "code", None)
    if code is not None:
        return classify_http_status(code)
    if isinstance(exc, (TimeoutError, OSError)):
        return FAILURE_NETWORK
    if isinstance(exc, ValueError):
        return FAILURE_PARSE
    return FAILURE_NETWORK


def looks_like_login_page(text: Any) -> bool:
    """응답이 로그인 화면(HTML)인지. 쿠키·자격증명은 보지 않는다."""
    body = str(text or "")
    if not body:
        return False
    lowered = body[:4000].lower()
    if "<html" not in lowered and "<!doctype" not in lowered:
        return False
    return any(marker.lower() in lowered for marker in _LOGIN_HTML_MARKERS)


def retry_after_seconds(failure: Any, attempts: int = 1) -> int:
    """이 실패에서 다음 시도까지 기다릴 초. 연속 실패마다 2배(최대 8배·7일 상한)."""
    base = RETRY_AFTER_SEC.get(str(failure or ""), 0)
    if base <= 0:
        return 0
    try:
        count = max(1, int(attempts))
    except (TypeError, ValueError):
        count = 1
    multiplier = min(2 ** (count - 1), MAX_BACKOFF_MULTIPLIER)
    return min(base * multiplier, MAX_RETRY_AFTER_SEC)


def user_action(failure: Any) -> str:
    """이 실패에서 사람이 할 수 있는 조치(한국어). 모르면 빈 문자열."""
    return USER_ACTION.get(str(failure or ""), "")


def failure_label(failure: Any) -> str:
    """화면에 그대로 쓸 수 있는 실패 이름. 모르면 원문."""
    key = str(failure or "")
    return FAILURE_LABEL.get(key, key)


__all__ = [
    "STATUS_OK", "STATUS_MISSING", "STATUS_INVALID",
    "BASIS_PERIOD", "BASIS_CUMULATIVE", "BASIS_UNKNOWN", "COMPARABLE_BASIS",
    "IDENTITY_NONE", "IDENTITY_LOG_NO", "IDENTITY_TITLE_EXACT",
    "IDENTITY_TITLE_SUBSTRING", "IDENTITY_TITLE_FUZZY", "TRUSTED_IDENTITY",
    "FAILURE_NONE", "FAILURE_UNAUTHORIZED", "FAILURE_FORBIDDEN", "FAILURE_RATE_LIMITED",
    "FAILURE_SERVER", "FAILURE_HTTP", "FAILURE_NETWORK", "FAILURE_LOGIN_PAGE",
    "FAILURE_PARSE", "FAILURE_SHAPE",
    "RETRY_AFTER_SEC", "MAX_RETRY_AFTER_SEC", "MAX_COUNT",
    "coerce_count", "normalize_log_no", "normalize_metric_row", "is_comparable",
    "classify_http_status", "classify_exception", "looks_like_login_page",
    "retry_after_seconds", "user_action", "failure_label",
]
