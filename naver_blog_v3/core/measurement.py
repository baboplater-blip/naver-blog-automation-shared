# core/measurement.py — 성과 측정 v2 (M4, 2026-09-18)
"""글 단위 성과(조회수·검색유입·방문자)를 결측을 숨기지 않고 기록·비교한다.

설계 원본: docs/autonomous-blog-design-2026-09-18.md §3.9·§4. 계약: .auto-dev/impl-contract-m1m4.md §2.8.

핵심 원칙(위반하면 사고):
  - **결측은 value=None** — 0 으로 대체하지 않는다. 쿠키 만료·통계 미인제스트를
    "0회 유입"으로 오인해 자동 판정을 내리면 근거 없는 최적화가 된다.
  - **원격 log_no 가 없는 글은 표본에서 제외**한다(제목 일치로 성과를 붙이지 않는다).
    제외된 만큼 커버리지가 낮게 나오는 것이 의도된 결과다.
  - **변경 전후 기간 비교 함수는 두지 않는다** — 계절·발행량·알고리즘 변동이 섞인
    인과 주장을 코드가 만들 수 없게 한다. 비교는 항상 같은 창(window) 안의 다른
    묶음(cohort)끼리만.
  - `settings.MEASUREMENT_V2_ENABLED`(기본 False) OFF 면 `record`/`ingest_from_views`
    는 no-op(파일도 만들지 않는다). 읽기 함수(get_measurements·compare_cohorts·
    coverage_report·feedback_weights)는 이미 저장된 데이터가 있으면 그대로 계산한다.

저장 위치: `core/performance_db.py` 가 관리하는 `posts[key]["measurements"]`
**additive 영역**만 사용한다. 이 모듈은 `performance_db.load()/save()` 를 호출해
얻은 dict 를 직접 조작할 뿐, `performance_db.py` 파일 자체는 건드리지 않는다.
기존 `views`/`referral_keywords`/`rank_history` 영역은 읽지도 쓰지도 않는다.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Optional

from config.logger import get_logger

log = get_logger(__name__)

# 저장 스키마에서 허용하는 값 — 임의 문자열이 coverage_report 의 고정 버킷을
# 오염시키지 않도록 record() 단계에서 걸러낸다.
_METRICS = frozenset({"post_views", "post_search_inflow", "blog_visitors"})
_WINDOWS = frozenset({"d1", "d7", "d28"})
_WINDOW_DAYS = {"d1": 1, "d7": 7, "d28": 28}

_METRIC_SEARCH_INFLOW = "post_search_inflow"
_METRIC_POST_VIEWS = "post_views"

# feedback_weights 가 요구하는 capability 증거 — stats_fetcher.probe_stats_capability
# 가 남기는 checks[].name 과 반드시 일치해야 한다(이름이 갈라지면 영구 held).
_CAPABILITY_CHECK_NAME = "post_metrics"

# feedback_weights 자체 표본/커버리지 게이트(compare_cohorts 와 같은 기본값을 쓴다).
_MIN_SAMPLE_FOR_WEIGHTS = 8
_MIN_COVERAGE_FOR_WEIGHTS = 0.7
_PREFERENCE_KEY = "measurement_v2_enabled"
_PREFERENCE_LOCK = threading.RLock()


# ─────────────────────────────────────────────────────
# 게이트
# ─────────────────────────────────────────────────────
def profile_recording_preference() -> dict:
    """Read profile setting with explicit missing/valid/invalid state.

    Missing is the only state that inherits the existing global switch. An unreadable
    or malformed profile file fails closed for normal measurement writes.
    """
    try:
        from config import profile
        path = profile.credentials_path()
    except (ImportError, AttributeError, OSError, ValueError, TypeError):
        return {"status": "invalid", "value": None}
    try:
        with _PREFERENCE_LOCK:
            raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("profile_settings_not_object")
        if _PREFERENCE_KEY not in raw:
            return {"status": "missing", "value": None}
        value = raw.get(_PREFERENCE_KEY)
        if not isinstance(value, bool):
            raise ValueError("profile_preference_not_boolean")
        return {"status": "valid", "value": value}
    except FileNotFoundError:
        return {"status": "missing", "value": None}
    except (OSError, ValueError, TypeError) as exc:
        log.warning("[measurement] 프로필 설정을 읽지 못했습니다(%s)", type(exc).__name__)
        return {"status": "invalid", "value": None}


def profile_recording_enabled() -> Optional[bool]:
    """Return an explicit profile preference; raise when its value is untrusted."""
    result = profile_recording_preference()
    if result["status"] == "invalid":
        raise OSError("profile_preference_unreadable_or_invalid")
    return result["value"]


def set_profile_recording_enabled(enabled: bool) -> dict:
    """Save the active profile preference while preserving all existing raw fields."""
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be bool")
    from config import atomic_write_json, profile
    path = Path(profile.credentials_path())
    with _PREFERENCE_LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError) as exc:
            log.error("[measurement] 프로필 설정을 읽지 못해 저장을 중단합니다", exc_info=True)
            raise OSError("profile_settings_unreadable") from exc
        if not isinstance(raw, dict):
            raise ValueError("profile_settings_invalid")
        raw[_PREFERENCE_KEY] = enabled
        atomic_write_json(path, raw)
    return {"saved": True, "profile": profile.active_profile(),
            "profile_enabled": enabled}


def recording_status() -> dict:
    """Report profile, global and autonomy switches separately and their OR result."""
    preference = profile_recording_preference()
    profile_preference = preference["value"]
    try:
        from config import settings
        global_on = bool(getattr(settings, "MEASUREMENT_V2_ENABLED", False))
    except (ImportError, AttributeError):
        global_on = False
    try:
        from core import autonomy_guard
        autonomy_on = bool(autonomy_guard.guards_active())
    except (ImportError, AttributeError, OSError, ValueError, TypeError):
        autonomy_on = False
    profile_on = bool(profile_preference)
    profile_base = (global_on if preference["status"] == "missing"
                    else bool(profile_preference))
    return {"profile_enabled": profile_on, "profile_preference": profile_preference,
            "profile_preference_status": preference["status"],
            "profile_inherits_global": preference["status"] == "missing",
            "global_enabled": global_on,
            "global_effective": global_on and preference["status"] == "missing",
            "autonomy_active": autonomy_on,
            "effective": profile_base or autonomy_on}


def _enabled() -> bool:
    """Profile preference OR existing global setting OR active autonomy guards.

    무인 운영이 도는 동안에는 전역 설정을 켜지 않아도 측정이 살아야 한다(설계
    §4 — 자동 사이클 안에서 관측이 항상 서 있어야 환류가 가능하다). 임포트 실패
    시(순환 참조·미설치 등)에는 설정값만으로 판단해 조용히 후퇴한다.
    """
    return recording_status()["effective"]


# ─────────────────────────────────────────────────────
# 쓰기 — record()
# ─────────────────────────────────────────────────────
def record(metric: str, value: Optional[int], *, post_key: str = "", log_no: str = "",
           window: str, source: str, collected_at: str,
           coverage: Optional[float] = None, basis: str = "",
           identity: str = "") -> dict:
    """단일 성과 관측치를 기록한다. 결측은 value=None 그대로 남긴다.

    - 게이트(settings.MEASUREMENT_V2_ENABLED) OFF 면 아무 파일도 건드리지 않고 반환한다.
    - metric/window 가 허용 목록 밖이면 경고 로그만 남기고 스킵한다(스키마 오염 방지).
    - **값은 단일 정규화 계약(core.metrics_contract)으로 검증한다** — 불리언·음수·NaN·
      문자열은 저장하지 않고 스킵한다(0 으로 대체하지 않는다).
    - post_key 가 주어지면 그 키의 performance_db 레코드에만 붙인다. 없으면 log_no 로
      기존 레코드를 찾는다. 어느 쪽으로도 기존 레코드를 찾지 못하면 **새 레코드를
      만들지 않고** 스킵한다(제목 없이 유령 레코드를 만들지 않기 위함 — 카탈로그는
      항상 발행 경로가 채운다). 제목 유사성만으로 귀속하는 경로는 여기 없다.
    - `basis` 는 이 수치가 기간 합계(period)인지 누적(cumulative)인지 미확인(unknown)인지다.
      비워 두면 기간값으로 본다(레거시 호환). **미확인 값은 비교·환류에서 제외**된다.
    """
    def result(saved: bool, reason: str, key: str = "") -> dict:
        return {"saved": saved, "skipped": not saved, "reason": reason,
                "metric": metric, "window": window, "post_key": key}

    if not _enabled():
        return result(False, "disabled")
    if (not isinstance(metric, str) or not isinstance(window, str)
            or metric not in _METRICS or window not in _WINDOWS):
        log.warning(f"[measurement] 알 수 없는 metric/window 무시 — metric={metric!r} window={window!r}")
        return result(False, "invalid_metric_or_window")

    from core import metrics_contract as mc
    from core import performance_db as pdb

    if value is None:
        clean, status = None, mc.STATUS_MISSING
    else:
        clean, status = mc.coerce_count(value)
        if status != mc.STATUS_OK:
            log.warning(f"[measurement] 수치로 볼 수 없는 값 무시 — metric={metric!r} "
                        f"status={status} type={type(value).__name__}")
            return result(False, f"invalid_value:{status}")

    # 귀속은 **명시 키 또는 글 번호 일치**로만 한다(제목 유사도 경로는 여기 없다).
    log_no = str(log_no or "").strip()
    strict_log_no = bool(mc.normalize_log_no(log_no))
    data = pdb.load()
    key = post_key or _find_key_by_log_no(data, log_no)
    rec = data["posts"].get(key) if key else None
    if rec is None:
        log.debug(f"[measurement] 대상 글을 찾지 못해 기록 스킵 — post_key={post_key!r} log_no={log_no!r}")
        return result(False, "unmatched_post")

    entry = {
        "metric": metric,
        "value": clean,
        "post_key": key,
        "log_no": log_no or rec.get("log_no", ""),
        "window": window,
        "source": source,
        "collected_at": collected_at,
        "coverage": coverage,
        "basis": basis or mc.BASIS_PERIOD,
        "identity": identity or (mc.IDENTITY_LOG_NO if strict_log_no
                                 else mc.IDENTITY_TITLE_EXACT),
        "value_status": status,
    }
    rec.setdefault("measurements", []).append(entry)
    try:
        pdb.save(data)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        log.error("[measurement] 저장 실패 metric=%s reason=%s", metric,
                  type(exc).__name__, exc_info=True)
        return result(False, f"save_error:{type(exc).__name__}", key)
    return result(True, "saved", key)


def _find_key_by_log_no(data: dict, log_no: str) -> str:
    if not log_no:
        return ""
    for key, rec in data.get("posts", {}).items():
        if rec.get("log_no") == log_no:
            return key
    return ""


# ─────────────────────────────────────────────────────
# 어댑터 — 기존 수동 CSV 인제스트(stats_fetcher.ingest_stats_export)가 채운
# performance_db posts[key]["views"] 를 measurement 레코드로 승격한다.
# stats_fetcher.py 는 건드리지 않는다(그쪽은 그대로 "views" 딕셔너리만 채운다).
# ─────────────────────────────────────────────────────
def ingest_from_views(*, window: str = "d7", source: str = "stats_export_csv") -> int:
    """performance_db 의 레거시 `views` 딕셔너리를 이 창(window)의 measurement 로 변환한다.

    log_no 가 없는 글은 스킵한다(성과 귀속 대상이 아니므로). 해당 창 안에 실제로
    기록된 날짜가 하루도 없으면 value=None(결측)으로 남긴다 — `views` 딕셔너리에
    날짜 키가 아예 없는 것과 "0 으로 기록된 날"을 구분하기 위함.
    반환: measurement 를 남긴 글 수.
    """
    if not _enabled():
        return 0
    days = _WINDOW_DAYS.get(window)
    if not days:
        log.warning(f"[measurement] 알 수 없는 window 무시 — window={window!r}")
        return 0

    from datetime import datetime

    from core import metrics_contract as mc
    from core import performance_db as pdb

    data = pdb.load()
    now_iso = datetime.now().isoformat(timespec="seconds")
    n = 0
    for key, rec in data["posts"].items():
        log_no = rec.get("log_no") or ""
        if not log_no:
            continue
        value, cov = _sum_recent_days(rec.get("views") or {}, days)
        entry = {
            "metric": _METRIC_POST_VIEWS,
            "value": value,
            "post_key": key,
            "log_no": log_no,
            "window": window,
            "source": source,
            "collected_at": now_iso,
            "coverage": cov,
            # 날짜별 조회수를 창 길이만큼 합친 값 = 기간값이다(누적 총계가 아니다).
            "basis": mc.BASIS_PERIOD,
            "identity": _views_identity(rec, days),
        }
        entries = rec.setdefault("measurements", [])
        # 같은 날 같은 창·같은 출처로 다시 적재하면 **덮어쓴다** — 같은 CSV 를 두 번
        # 가져와도 레코드가 불어나지 않는다(중복 처리). 다른 날 수집분은 이력으로 쌓인다.
        replaced = False
        for index, old in enumerate(entries):
            if (isinstance(old, dict) and old.get("metric") == _METRIC_POST_VIEWS
                    and old.get("window") == window and old.get("source") == source
                    and str(old.get("collected_at") or "")[:10] == now_iso[:10]):
                entries[index] = entry
                replaced = True
                break
        if not replaced:
            entries.append(entry)
        n += 1
    if n:
        pdb.save(data)
    return n


def _views_identity(rec: dict, days: int) -> str:
    """이 글의 창(window) 안 조회수가 **어떤 증거로** 붙었는지 중 가장 약한 것.

    한 날이라도 제목 유사도로 붙었으면 그 합계 전체를 신뢰 범위 밖으로 본다 —
    섞인 값에 강한 증거의 이름을 붙이지 않는다. 기록이 없으면(레거시) 글 번호 일치로 본다.
    """
    from datetime import date, timedelta

    from core import metrics_contract as mc

    marks = rec.get("views_identity")
    if not isinstance(marks, dict) or not marks:
        return mc.IDENTITY_LOG_NO
    cutoff = date.today() - timedelta(days=days - 1)
    rank = {mc.IDENTITY_LOG_NO: 0, mc.IDENTITY_TITLE_EXACT: 1,
            mc.IDENTITY_TITLE_SUBSTRING: 2, mc.IDENTITY_TITLE_FUZZY: 3,
            mc.IDENTITY_NONE: 3}
    worst = mc.IDENTITY_LOG_NO
    for day, identity in marks.items():
        try:
            if date.fromisoformat(str(day)) < cutoff:
                continue
        except (TypeError, ValueError):
            continue
        if rank.get(str(identity), 3) > rank.get(worst, 0):
            worst = str(identity) or mc.IDENTITY_NONE
    return worst


def _sum_recent_days(views: dict, days: int) -> tuple:
    """views(YYYY-MM-DD→int) 중 최근 `days`일 안에 실제로 기록된 날만 합산.

    반환: (합계|None, 커버리지). 기록된 날이 하나도 없으면 (None, 0.0) — 0 으로
    대체하지 않는다.
    """
    from datetime import date, timedelta

    if not views:
        return None, 0.0
    cutoff = date.today() - timedelta(days=days - 1)
    total = 0
    present = 0
    for d, v in views.items():
        try:
            dt = date.fromisoformat(str(d))
        except (ValueError, TypeError):
            continue
        if dt >= cutoff:
            total += int(v or 0)
            present += 1
    if present == 0:
        return None, 0.0
    return total, round(present / days, 4)


# ─────────────────────────────────────────────────────
# 읽기 — get_measurements
# ─────────────────────────────────────────────────────
def get_measurements(post_key: str) -> list:
    """이 글에 기록된 모든 measurement 엔트리(원본 순서)."""
    from core import performance_db as pdb

    data = pdb.load()
    rec = data["posts"].get(post_key) or {}
    return list(rec.get("measurements") or [])


# ─────────────────────────────────────────────────────
# 묶음 통계 (compare_cohorts / feedback_weights 공용)
# ─────────────────────────────────────────────────────
def entry_basis(entry: dict) -> str:
    """이 관측치의 기간 의미. 미기재 레거시는 기간값으로 본다(회귀 방지).

    자동 수집처럼 **의미가 확인되지 않은 경로는 명시적으로 `unknown`** 을 적는다 —
    그래야 누적값이 기간값 중앙값 비교에 섞이지 않는다.
    """
    from core import metrics_contract as mc
    return str((entry or {}).get("basis") or mc.BASIS_PERIOD)


def entry_identity(entry: dict) -> str:
    """이 관측치가 어느 글의 것인지에 대한 증거. 미기재 레거시는 글 번호 일치로 본다."""
    from core import metrics_contract as mc
    return str((entry or {}).get("identity") or mc.IDENTITY_LOG_NO)


def is_usable_for_planning(entry: dict) -> bool:
    """이 관측치를 **다른 글과 비교하고 다음 기획에 넣어도 되는가.**

    값이 실제로 있고(결측 아님), 기간 의미가 확정됐고, 귀속 증거가 신뢰 범위일 때만 참.
    """
    from core import metrics_contract as mc
    if not isinstance(entry, dict) or entry.get("value") is None:
        return False
    return mc.is_comparable(entry_basis(entry), entry_identity(entry))


def _latest_matching(entries: list, metric: str, window: str) -> Optional[dict]:
    matches = [e for e in entries if e.get("metric") == metric and e.get("window") == window]
    if not matches:
        return None
    return max(matches, key=lambda e: e.get("collected_at") or "")


def _median(nums: list) -> float:
    s = sorted(nums)
    m = len(s) // 2
    if len(s) % 2:
        return float(s[m])
    return (s[m - 1] + s[m]) / 2.0


def _cohort_stats(keys, data: dict, *, window: str, metric: str = _METRIC_SEARCH_INFLOW) -> dict:
    """묶음 통계. **원격 log_no 없는 글은 n 에서 제외**하고, 결측(value=None)은
    median/total 계산에서 제외한다(0 으로 채우지 않는다).

    2026-09-19: 기간 의미가 확정되지 않은 값(basis=unknown/cumulative)과 제목 유사도로
    귀속된 값은 **median/total 에 넣지 않는다** — 누적값이 기간 비교에 섞이면 편집
    방식이 아니라 글의 나이를 재게 된다. 제외된 수는 `unusable` 로 보고한다(조용히
    사라지지 않는다).
    """
    values, coverages = [], []
    n = 0
    unusable = 0
    seen = set()
    for key in keys or ():
        if key in seen:
            continue
        seen.add(key)
        rec = data.get("posts", {}).get(key)
        if not rec or not rec.get("log_no"):
            continue  # 원격 log_no 없는 글은 표본 제외 — 의도된 손실
        n += 1
        entry = _latest_matching(rec.get("measurements") or [], metric, window)
        # 커버리지는 "얼마나 수집됐나"라 값 유무와 별개로 그대로 읽는다.
        coverages.append(float(entry.get("coverage") or 0.0) if entry else 0.0)
        if entry is None or entry.get("value") is None:
            continue
        if is_usable_for_planning(entry):
            values.append(float(entry["value"]))
        else:
            unusable += 1   # 값은 있지만 기간 의미·귀속 증거가 신뢰 범위 밖
    coverage = (sum(coverages) / len(coverages)) if coverages else 0.0
    median = _median(values) if values else None
    total = sum(values) if values else None
    inflow_ratio = (sum(1 for v in values if v > 0) / n) if n else None
    return {
        "n": n,
        "coverage": round(coverage, 4),
        "median": median,
        "total": total,
        "unusable": unusable,
        "inflow_ratio": (round(inflow_ratio, 4) if inflow_ratio is not None else None),
    }


def _verdict(median_a: Optional[float], median_b: Optional[float]) -> str:
    a = median_a or 0.0
    b = median_b or 0.0
    if a <= 0 and b <= 0:
        return "no_difference"
    if a <= 0:
        return "improved" if b > 0 else "no_difference"
    ratio = b / a
    if ratio >= 1.1:
        return "improved"
    if ratio <= 0.9:
        return "worse"
    return "no_difference"


def compare_cohorts(group_a: list, group_b: list, *, window: str,
                    min_sample: int = 8, min_coverage: float = 0.7) -> dict:
    """같은 블로그 안 두 글 묶음을 같은 창(window) 길이로만 비교한다.

    묶음당 n(원격 log_no 확보 글 수) >= min_sample **그리고** coverage(평균 수집
    커버리지) >= min_coverage 를 **모두** 만족해야 판정하고, 아니면 무조건
    "insufficient_data" + held_reason 을 돌려준다. 이 게이트를 우회하는 인자는 없다.
    """
    from core import performance_db as pdb

    data = pdb.load()
    a = _cohort_stats(group_a, data, window=window)
    b = _cohort_stats(group_b, data, window=window)
    n = {"a": a["n"], "b": b["n"]}
    coverage = {"a": a["coverage"], "b": b["coverage"]}

    gate_fail = (n["a"] < min_sample or n["b"] < min_sample
                 or coverage["a"] < min_coverage or coverage["b"] < min_coverage)
    # 값이 있어도 기간 의미·귀속 증거가 신뢰 범위 밖이면 비교가 성립하지 않는다.
    usable_fail = a["median"] is None or b["median"] is None
    if gate_fail or usable_fail:
        held = (
            f"n(a={n['a']},b={n['b']}) 또는 coverage(a={coverage['a']},b={coverage['b']}) 가 "
            f"게이트(min_sample={min_sample}, min_coverage={min_coverage}) 미달"
        ) if gate_fail else (
            f"비교 가능한 값 없음 — 기간 의미·귀속 증거 미충족"
            f"(제외 a={a['unusable']},b={b['unusable']})"
        )
        return {
            "verdict": "insufficient_data",
            "median_inflow": {"a": None, "b": None},
            "total_search_inflow": {"a": None, "b": None},
            "posts_with_inflow_ratio": {"a": None, "b": None},
            "coverage": coverage,
            "n": n,
            "held_reason": held,
        }

    return {
        "verdict": _verdict(a["median"], b["median"]),
        "median_inflow": {"a": a["median"], "b": b["median"]},
        "total_search_inflow": {"a": a["total"], "b": b["total"]},
        "posts_with_inflow_ratio": {"a": a["inflow_ratio"], "b": b["inflow_ratio"]},
        "coverage": coverage,
        "n": n,
        "held_reason": "",
    }


# ─────────────────────────────────────────────────────
# 커버리지 리포트
# ─────────────────────────────────────────────────────
def coverage_report(*, window: str = "d7", metric: str = _METRIC_SEARCH_INFLOW) -> dict:
    """이 창(window)에서 원격 log_no 확보 글 중 실제로 측정된 비율.

    표본이 없으면(분모 0) 0 이 아니라 None 을 돌려준다 — "측정 안 됨"과
    "커버리지 0%"를 구분한다.
    """
    from core import performance_db as pdb

    data = pdb.load()
    posts = data.get("posts", {})
    total = len(posts)
    with_log_no = 0
    measured = 0
    observed_only = 0
    coverages = []
    for rec in posts.values():
        if not rec.get("log_no"):
            continue
        with_log_no += 1
        entry = _latest_matching(rec.get("measurements") or [], metric, window)
        if entry is None:
            continue
        # 기간 의미가 확정되지 않은 관측은 '관측됨'이지 '측정됨'이 아니다 —
        # 환류 게이트가 이 비율을 보므로 여기서 부풀리면 판단이 잘못 열린다.
        if is_usable_for_planning(entry):
            measured += 1
            coverages.append(float(entry.get("coverage") or 0.0))
        else:
            observed_only += 1
    return {
        "window": window,
        "metric": metric,
        "total_posts": total,
        "posts_with_log_no": with_log_no,
        "posts_measured": measured,
        "posts_observed_unusable": observed_only,
        "measured_ratio": (round(measured / with_log_no, 4) if with_log_no else None),
        "avg_coverage": (round(sum(coverages) / len(coverages), 4) if coverages else None),
    }


# ─────────────────────────────────────────────────────
# capability 증거 소비 (stats_fetcher.probe_stats_capability 가 남긴 파일을 읽기만 함)
# ─────────────────────────────────────────────────────
MANUAL_SOURCE_FILE: str = "measurement_sources.json"
# 사람이 넣은 성과 입력이 유효하다고 보는 기간 — 이보다 오래되면 다시 넣어야 한다.
MANUAL_SOURCE_MAX_AGE_DAYS: int = 60


def _manual_source_path(profile: Optional[str] = None):
    from config import profile as _p
    return _p.logs_dir(profile) / MANUAL_SOURCE_FILE


def record_manual_source(*, kind: str, file_name: str = "", rows_valid: int = 0,
                         matched: int = 0, window: str = "", days: Optional[list] = None,
                         metric: str = "", basis: str = "unknown",
                         profile: Optional[str] = None) -> bool:
    """사람이 넣은 성과 입력(통계 CSV 등) 증거를 남긴다.

    **파일 이름만** 적는다(전체 경로·개인정보 없음). 이 증거가 있으면 `capability_status`
    가 "자동 수집은 미증명이지만 검증된 수동 입력이 있다"로 판정한다 — 자동 수집이 막혀
    있다는 사실을 덮지 않고, 환류에 쓸 근거가 무엇인지 분명히 한다.
    """
    if not _enabled():
        return False
    try:
        from config import atomic_write_json, safe_load_json
        from datetime import datetime as _dt

        path = _manual_source_path(profile)
        data = safe_load_json(path, default=dict)
        if not isinstance(data, dict):
            data = {}
        data[str(kind)] = {
            "recorded_at": _dt.now().isoformat(timespec="seconds"),
            "file": str(file_name or "")[:120],
            "rows_valid": int(rows_valid or 0),
            "matched": int(matched or 0),
            "window": str(window or ""),
            "metric": str(metric or ""),
            "basis": str(basis or "unknown"),
            "days": list(days or [])[:40],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
        return True
    except Exception as exc:  # noqa: BLE001 — 증거 기록 실패가 적재를 되돌리지 않는다
        log.warning(f"[measurement] 수동 입력 증거 기록 실패({type(exc).__name__})")
        return False


def manual_source_status(*, profile: Optional[str] = None) -> dict:
    """검증된 수동 성과 입력이 있는지. 없으면 available=False + 사유."""
    out = {"available": False, "reason": "no_manual_source", "kind": "",
           "recorded_at": "", "matched": 0, "age_days": None,
           "window": "", "metric": "", "basis": "unknown"}
    try:
        from config import safe_load_json
        from datetime import datetime as _dt

        data = safe_load_json(_manual_source_path(profile), default=dict)
    except Exception:  # noqa: BLE001
        out["reason"] = "manual_source_read_error"
        return out
    if not isinstance(data, dict) or not data:
        return out

    best = None
    for kind, rec in data.items():
        if not isinstance(rec, dict) or int(rec.get("matched") or 0) <= 0:
            continue
        if best is None or str(rec.get("recorded_at") or "") > str(best[1].get("recorded_at") or ""):
            best = (kind, rec)
    if best is None:
        out["reason"] = "manual_source_no_matched_rows"
        return out

    kind, rec = best
    try:
        recorded = _dt.fromisoformat(str(rec.get("recorded_at")))
        age = (_dt.now() - recorded).days
    except (TypeError, ValueError):
        age = None
    out.update(kind=kind, recorded_at=rec.get("recorded_at") or "",
               matched=int(rec.get("matched") or 0), age_days=age,
               window=str(rec.get("window") or ""),
               metric=str(rec.get("metric") or ""),
               basis=str(rec.get("basis") or "unknown"))
    if age is not None and age > MANUAL_SOURCE_MAX_AGE_DAYS:
        out["reason"] = f"manual_source_stale:{age}d"
        return out
    out.update(available=True, reason="")
    return out


# 자동 수집 증거의 유효 기간 — 이보다 오래된 ok 하나로 환류를 영구 승인하지 않는다.
CAPABILITY_MAX_AGE_DAYS: int = 14


def _evidence_age_days(collected_at: Any) -> Optional[int]:
    from datetime import datetime as _dt
    try:
        return (_dt.now() - _dt.fromisoformat(str(collected_at)[:19])).days
    except (TypeError, ValueError):
        return None


def _evidence_profile_matches(evidence_profile: Any, profile: Optional[str]) -> bool:
    """증거가 지금 보려는 프로필의 것인지.

    증거에 프로필이 안 적힌 구버전(schema@1)은 판단 불가라 **통과**시킨다(회귀 방지).
    적혀 있는데 다르면 계정 전환으로 상태가 섞인 것이므로 막는다.
    """
    recorded = str(evidence_profile or "").strip()
    if not recorded:
        return True
    try:
        from config import profile as _p
        current = str(profile or _p.active_profile() or "").strip()
    except Exception:  # noqa: BLE001
        return True
    return (not current) or recorded == current


def capability_status(*, profile: Optional[str] = None) -> dict:
    """성과 측정 근거의 상태 — 자동 수집 증거 우선, 없으면 검증된 수동 입력.

    쿠키 값은 증거 파일에 저장되지 않으므로(존재 여부만) 여기서도 노출되지 않는다.
    반환 dict 의 `source` 가 근거의 출처다:
        · "auto_probe"  — `stats_fetcher.probe_stats_capability()` 가 ok 를 남겼다
        · "manual_csv"  — 자동 수집은 미증명이지만 **검증된 통계 파일 입력**이 있다
        · ""            — 근거 없음(proven=False, held_reason 에 사유)
    자동 수집이 막혀 있다는 사실을 수동 입력이 덮지 않는다(`auto_proven` 으로 구분).
    """
    result = {"proven": False, "held_reason": "no_capability_evidence",
              "checks": [], "source": "", "auto_proven": False}
    try:
        from config import profile as _p
        from config import safe_load_json
    except ImportError:
        result["held_reason"] = "capability_check_error"
        return result

    try:
        path = _p.logs_dir(profile) / "stats_capability.json"
        data = safe_load_json(path, default=dict)
    except OSError:
        result["held_reason"] = "capability_check_error"
        return result

    checks = data.get("checks") if isinstance(data, dict) else None
    result["checks"] = checks if isinstance(checks, list) else []
    collected_at = data.get("collected_at") if isinstance(data, dict) else ""
    result["collected_at"] = str(collected_at or "")
    age = _evidence_age_days(collected_at)
    result["age_days"] = age
    same_profile = _evidence_profile_matches(
        (data.get("profile") if isinstance(data, dict) else ""), profile)
    result["evidence_profile"] = str((data.get("profile") if isinstance(data, dict) else "") or "")

    for chk in result["checks"]:
        if isinstance(chk, dict) and chk.get("name") == _CAPABILITY_CHECK_NAME:
            if chk.get("result") == "ok":
                # 오래된 ok 하나로 영구 승인하지 않는다. 계정이 바뀌었으면 남의 증거다.
                if not same_profile:
                    result["held_reason"] = "capability_evidence_other_profile"
                elif age is not None and age > CAPABILITY_MAX_AGE_DAYS:
                    result["held_reason"] = f"capability_evidence_stale:{age}d"
                else:
                    result.update(proven=True, held_reason="", source="auto_probe",
                                  auto_proven=True)
                    return result
            else:
                result["held_reason"] = f"capability_unproven:{chk.get('result', 'unknown')}"
                result["failure"] = str(chk.get("failure") or "")
                result["action"] = str(chk.get("action") or "")
            break

    # 자동 수집이 증명되지 않았을 때만 수동 입력을 본다(검증을 통과한 입력만 인정).
    manual = manual_source_status(profile=profile)
    if manual["available"]:
        result.update(proven=True, held_reason="", source="manual_csv")
        result["manual"] = manual
        return result
    result["manual"] = manual
    return result


# ─────────────────────────────────────────────────────
# 주제군 가중치 환류 (M4-4) — capability 미증명이면 항상 보류
# ─────────────────────────────────────────────────────
def feedback_weights(*, window: str = "d28") -> dict:
    """주제군(카테고리)별 가중치 환류 입력.

    글별 조회수/유입 검색어 자동 수집 capability 가 증명되지 않았거나(현재 저장소
    기준으로는 항상 미증명), 증명됐더라도 표본/커버리지가 게이트 미달이면
    {"weights": {}, "held_reason": str} 를 돌려준다 — 측정이 서지 않은 상태에서
    기획을 자동으로 바꾸지 않는다(설계 §4.1.1).
    """
    cap = capability_status()
    if not cap["proven"]:
        return {"weights": {}, "held_reason": cap["held_reason"]}

    cov = coverage_report(window=window, metric=_METRIC_SEARCH_INFLOW)
    if (not cov["posts_with_log_no"]
            or cov["posts_with_log_no"] < _MIN_SAMPLE_FOR_WEIGHTS
            or cov["measured_ratio"] is None
            or cov["measured_ratio"] < _MIN_COVERAGE_FOR_WEIGHTS):
        return {"weights": {}, "held_reason": "insufficient_data"}

    weights = _build_category_weights(window=window)
    if not weights:
        return {"weights": {}, "held_reason": "insufficient_data"}
    return {"weights": weights, "held_reason": ""}


def selected_feedback_weights() -> dict:
    """Use the latest explicitly selected CSV metric, window and basis for planning."""
    selection = manual_source_status()
    provenance = {"metric": selection.get("metric", ""),
                  "window": selection.get("window", ""),
                  "basis": selection.get("basis", "unknown")}
    if not selection.get("available"):
        return {"weights": {}, "held_reason": selection.get("reason") or "no_selected_input",
                "provenance": provenance}
    if provenance["metric"] != _METRIC_SEARCH_INFLOW:
        return {"weights": {}, "held_reason": "selected_metric_not_search_inflow",
                "provenance": provenance}
    if provenance["basis"] != "period":
        return {"weights": {}, "held_reason": f"selected_basis_{provenance['basis']}",
                "provenance": provenance}
    if provenance["window"] not in _WINDOWS:
        return {"weights": {}, "held_reason": "selected_window_unknown",
                "provenance": provenance}
    result = feedback_weights(window=provenance["window"])
    result["provenance"] = provenance
    return result


# ─────────────────────────────────────────────────────
# 편집 방식별 반응 (2026-09-18) — 비슷한 게시 경과기간끼리만 비교한다
# ─────────────────────────────────────────────────────
# 게시 후 경과일 묶음 — 갓 올린 글과 한 달 된 글의 조회수를 섞어 비교하면 편집 방식이
# 아니라 '나이'를 재게 된다.
AGE_BUCKETS: tuple = ((0, 7, "0-7일"), (8, 30, "8-30일"), (31, 3650, "31일+"))


def _age_bucket(posted_at: Any) -> str:
    """게시 후 경과일 묶음 라벨. 게시일을 못 읽으면 ""(비교 대상에서 제외)."""
    text = str(posted_at or "").strip()
    if not text:
        return ""
    from datetime import date as _date
    from datetime import datetime as _dt
    try:
        parsed = _dt.fromisoformat(text[:19])
    except ValueError:
        try:
            parsed = _dt.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return ""
    age = (_date.today() - parsed.date()).days
    if age < 0:
        return ""
    for low, high, label in AGE_BUCKETS:
        if low <= age <= high:
            return label
    return ""


def _approach_signature(brief: Any) -> str:
    """편집 방식 서명 — 브리프의 구성 선언에서 만든다. 브리프가 없으면 ""(제외)."""
    if not isinstance(brief, dict) or not brief:
        return ""
    structure = brief.get("structure") if isinstance(brief.get("structure"), dict) else {}
    parts = []
    if "table" in structure:
        parts.append("표있음" if structure.get("table") else "표없음")
    if "faq_count" in structure:
        try:
            parts.append("FAQ있음" if int(structure["faq_count"]) > 0 else "FAQ없음")
        except (TypeError, ValueError):
            pass
    if brief.get("reader_question"):
        parts.append("질문형")
    return "·".join(parts)


def _briefs_by_key() -> dict:
    """보관본에서 글 키 → 편집 브리프 맵을 만든다(로컬 조회만, 네트워크 0).

    성과 저장소(performance_db)에는 브리프가 없으므로, 제목 정규화 키로 보관본과 잇는다.
    브리프가 없는 글은 맵에 들어가지 않는다 → 편집 방식 비교에서 제외된다.
    """
    out: dict = {}
    try:
        from core import draft_store as ds
        from core import performance_db as pdb
    except Exception:  # noqa: BLE001
        return out
    try:
        drafts = ds.load_drafts() or {}
    except Exception as exc:  # noqa: BLE001 — 보관본 조회 실패는 비교 불가(보류)
        log.debug(f"[measurement] 보관본 조회 실패({type(exc).__name__})")
        return out
    for entry in (drafts.values() if isinstance(drafts, dict) else drafts):
        post = entry.get("post_data") if isinstance(entry, dict) else None
        if not isinstance(post, dict):
            post = entry if isinstance(entry, dict) else None
        if not isinstance(post, dict):
            continue
        brief = post.get("editorial_brief")
        title = post.get("title") or ""
        if not isinstance(brief, dict) or not brief or not title:
            continue
        key = pdb.normalize_key(title)
        if key:
            out[key] = brief
    return out


def editorial_feedback(*, window: str = "d28") -> dict:
    """편집 방식별 반응 — **근거가 설 때만** 값을 돌려준다.

    묶음은 (게시 경과기간 × 편집 방식 서명)이다. 같은 나이대끼리만 비교하고, 묶음마다
    `_MIN_SAMPLE_FOR_WEIGHTS` 이상·결측 제외 표본이 있어야 한다. 하나라도 못 서면
    `{"groups": {}, "held_reason": ...}` 로 **보류**한다 — 측정이 서지 않은 상태에서
    편집 방식을 자동으로 바꾸지 않는다.
    """
    cap = capability_status()
    if not cap["proven"]:
        return {"groups": {}, "held_reason": cap["held_reason"], "source": ""}

    briefs = _briefs_by_key()
    if not briefs:
        return {"groups": {}, "held_reason": "no_editorial_brief_history",
                "source": cap.get("source", "")}

    from core import performance_db as pdb

    data = pdb.load()
    buckets: dict = {}
    for key, rec in data.get("posts", {}).items():
        if not rec.get("log_no"):
            continue
        age = _age_bucket(rec.get("posted_at"))
        approach = _approach_signature(briefs.get(key))
        if not age or not approach:
            continue
        buckets.setdefault((age, approach), []).append(key)

    groups: dict = {}
    for (age, approach), keys in buckets.items():
        stats = _cohort_stats(keys, data, window=window, metric=_METRIC_POST_VIEWS)
        if stats["n"] < _MIN_SAMPLE_FOR_WEIGHTS or stats["median"] is None:
            continue
        groups.setdefault(age, {})[approach] = {
            "n": stats["n"], "median": stats["median"], "window": window}
    if not groups:
        return {"groups": {}, "held_reason": "insufficient_data",
                "source": cap.get("source", "")}
    return {"groups": groups, "held_reason": "", "source": cap.get("source", "")}


def editorial_prompt_hint(*, window: str = "d28") -> str:
    """집필 프롬프트에 넣을 '편집 방식 실측' 한 블록. 보류 중이면 ""(프롬프트 불변).

    보류 사유가 있으면 **아무 문장도 만들지 않는다** — 근거 없는 성과 단정을 프롬프트에
    흘리지 않기 위한 의도적 설계다.
    """
    try:
        result = editorial_feedback(window=window)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[measurement] 편집 방식 환류 조회 실패({type(exc).__name__})")
        return ""
    if result.get("held_reason") or not result.get("groups"):
        return ""
    lines = ["[편집 방식 실측 — 같은 게시 경과기간끼리 비교한 결과]"]
    for age, approaches in sorted(result["groups"].items()):
        ranked = sorted(approaches.items(), key=lambda kv: kv[1]["median"], reverse=True)
        best, stat = ranked[0]
        lines.append(f"- 게시 {age} 글: '{best}' 구성이 조회수 중앙값 {stat['median']:.0f} "
                     f"(표본 {stat['n']}편)로 가장 높았습니다.")
    lines.append("- 위 수치는 이 블로그의 실측이며, 주제에 맞지 않으면 따르지 마세요.")
    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────
# 화면용 상태 요약 (2026-09-19) — 네트워크 0회, 저장된 것만 읽는다
# ─────────────────────────────────────────────────────
SOURCE_LABEL = {
    "auto_probe": "자동 수집(검증됨)",
    "manual_csv": "통계 파일(사람이 넣음)",
    "": "근거 없음",
}

HELD_LABEL = {
    "": "",
    "insufficient_data": "표본·커버리지가 모자라요",
    "no_capability_evidence": "수집 상태를 아직 확인하지 않았어요",
    "no_editorial_brief_history": "편집 방향 이력이 없어요",
    "capability_check_error": "수집 증거 파일을 읽지 못했어요",
    "capability_evidence_other_profile": "다른 계정의 수집 기록이에요",
    "manual_source_read_error": "통계 파일 기록을 읽지 못했어요",
    "no_manual_source": "넣어 둔 통계 파일이 없어요",
    "manual_source_no_matched_rows": "통계 파일에서 우리 글을 찾지 못했어요",
}


def held_label(held_reason: Any) -> str:
    """보류 사유 → 사람 말. 모르는 사유는 접두어만 풀어서 원문을 남긴다."""
    reason = str(held_reason or "")
    if reason in HELD_LABEL:
        return HELD_LABEL[reason]
    if reason.startswith("capability_unproven:"):
        return f"자동 수집이 아직 증명되지 않았어요({reason.split(':', 1)[1]})"
    if reason.startswith("capability_evidence_stale:"):
        return f"수집 기록이 오래됐어요({reason.split(':', 1)[1]} 경과) — 다시 확인이 필요해요"
    if reason.startswith("manual_source_stale:"):
        return f"넣어 둔 통계 파일이 오래됐어요({reason.split(':', 1)[1]} 경과)"
    return reason


def status_summary(*, profile: Optional[str] = None, window: str = "d28") -> dict:
    """성과 수집·환류 상태 한 덩어리 — **읽기 전용**(네트워크·발행 0).

    화면이 알아야 하는 다섯 가지를 한 번에 돌려준다.
      ① 지금 근거가 무엇인가(`source_label`)
      ② 마지막으로 **유효하게** 수집된 때(`last_success_at`)
      ③ 마지막 시도와 그 실패 분류(`last_attempt_at`/`failure_label`)
      ④ 다음 기획 반영이 되고 있는가, 아니면 왜 보류인가(`feedback_active`/`held_label`)
      ⑤ 사용자가 할 일(`action`)
    어떤 실패도 화면을 막지 않는다(fail-open — 전부 기본값으로 채워 반환).
    """
    from core import metrics_contract as mc

    out = {
        "profile": "", "source": "", "source_label": SOURCE_LABEL[""],
        "auto_proven": False, "manual_available": False,
        "last_success_at": "", "last_attempt_at": "", "failure": "",
        "failure_label": "", "next_retry_at": "", "suppressed": False,
        "action": "", "held_reason": "", "held_label": "",
        "feedback_active": False, "coverage": {}, "window": window,
        "capability_collected_at": "", "capability_age_days": None,
        "thumbnail_effect": "노출 수를 수집할 수 없어 클릭률·썸네일 효과는 계산하지 않습니다.",
    }
    try:
        from config import profile as _p
        out["profile"] = str(profile or _p.active_profile() or "")
    except Exception:  # noqa: BLE001
        pass

    try:
        cap = capability_status(profile=profile)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[measurement] capability 조회 실패({type(exc).__name__})")
        cap = {"proven": False, "held_reason": "capability_check_error",
               "source": "", "auto_proven": False}
    out["source"] = str(cap.get("source") or "")
    out["source_label"] = SOURCE_LABEL.get(out["source"], out["source"])
    out["auto_proven"] = bool(cap.get("auto_proven"))
    out["capability_collected_at"] = str(cap.get("collected_at") or "")
    out["capability_age_days"] = cap.get("age_days")
    manual = cap.get("manual") if isinstance(cap.get("manual"), dict) else {}
    out["manual_available"] = bool(manual.get("available"))
    if out["manual_available"]:
        out["manual_recorded_at"] = str(manual.get("recorded_at") or "")
        out["manual_matched"] = int(manual.get("matched") or 0)

    try:
        from core import stats_fetcher as sf
        state = sf.load_collect_state(profile=profile).get("post_metrics") or {}
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[measurement] 수집 상태 조회 실패({type(exc).__name__})")
        state = {}
    if isinstance(state, dict):
        out["last_success_at"] = str(state.get("last_success_at") or "")
        out["last_attempt_at"] = str(state.get("last_attempt_at") or "")
        out["failure"] = str(state.get("failure") or "")
        out["failure_label"] = mc.failure_label(out["failure"]) if out["failure"] else ""
        out["next_retry_at"] = str(state.get("next_attempt_at") or "")
        out["suppressed"] = bool(out["failure"] and out["next_retry_at"])
    # 자동 수집이 증명되지 않았어도 **사람이 넣은 통계 파일**은 유효 수집이다.
    if not out["last_success_at"] and out.get("manual_recorded_at"):
        out["last_success_at"] = out["manual_recorded_at"]

    try:
        fw = feedback_weights(window=window)
        out["held_reason"] = str(fw.get("held_reason") or "")
        out["feedback_active"] = not out["held_reason"] and bool(fw.get("weights"))
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[measurement] 환류 상태 조회 실패({type(exc).__name__})")
        out["held_reason"] = "capability_check_error"
    out["held_label"] = held_label(out["held_reason"])

    try:
        out["coverage"] = coverage_report(window=window)
    except Exception:  # noqa: BLE001
        out["coverage"] = {}

    out["action"] = (mc.user_action(out["failure"]) if out["failure"] else "")
    if not out["action"] and out["held_reason"]:
        out["action"] = ("네이버 블로그 통계 화면에서 파일을 내려받아 "
                         "'통계 파일 불러오기'로 넣으면 다음 기획에 반영할 수 있어요.")
    return out


def thumbnail_effect(*, window: str = "d28") -> dict:
    """썸네일 인과효과 — **계산하지 않는다.** 항상 불가 사유를 돌려준다.

    썸네일이 클릭에 준 영향은 '노출 대비 클릭'으로만 말할 수 있는데, 우리가 확보할 수
    있는 값은 조회수(클릭 이후)뿐이고 **노출 수(분모)가 없다.** 분모 없이 조회수 차이를
    썸네일 효과라고 부르면 발행 리듬·주제 수요·검색 순위 변화를 전부 썸네일 공로로
    돌리게 된다. 그래서 이 함수는 값을 만들지 않고 왜 못 만드는지만 말한다.
    """
    return {"available": False, "reason": "no_impression_denominator",
            "ctr": None, "window": window,
            "detail": "노출 수를 수집할 수 없어 클릭률·썸네일 효과를 계산하지 않습니다."}


def _build_category_weights(*, window: str) -> dict:
    """카테고리별 중앙값 검색유입 비례 가중치(합계 1.0). 데이터 없으면 {}."""
    from core import performance_db as pdb

    data = pdb.load()
    by_category: dict = {}
    for key, rec in data.get("posts", {}).items():
        if not rec.get("log_no"):
            continue
        cat = rec.get("category") or "미분류"
        by_category.setdefault(cat, []).append(key)

    medians = {}
    for cat, keys in by_category.items():
        stats = _cohort_stats(keys, data, window=window)
        if stats["n"] == 0 or stats["median"] is None:
            continue
        medians[cat] = max(stats["median"], 0.0)

    if not medians:
        return {}
    total = sum(medians.values())
    if total <= 0:
        share = round(1.0 / len(medians), 4)
        return {cat: share for cat in medians}
    return {cat: round(v / total, 4) for cat, v in medians.items()}
