# core/stats_csv.py
# 📥 로컬 통계 CSV/XLSX → 검증된 성과 입력 (2026-09-18)
# ─────────────────────────────────────────────────────────────────────────────
# 이 모듈이 존재하는 이유
#   글별 조회수 **자동** 수집은 이 저장소에서 아직 증명되지 않았다(후보 API 가 HTTP 403 을
#   돌려준다 — 원인 미확정). 그래서 성과 환류는 `measurement.feedback_weights()` 에서
#   자동 보류 상태다. 그 상태를 우회하거나 0 으로 채우지 않고, **사람이 내려받은 통계
#   파일을 검증해 받아들이는 경로**를 정식으로 연결한다.
#
#   기존 `stats_fetcher.ingest_stats_export()` 는 파일 전체에 **하루 날짜 하나**를 적용한다.
#   여기서는 행마다 **측정일**을 읽고(없으면 명시 기준일), 행 단위로
#     · 글 식별자(제목 또는 글 번호)
#     · 측정일
#     · 조회수
#   를 검증하고 **중복 행을 집어낸다**. 검증을 통과한 행만 성과 입력이 된다.
#
# 정직성 계약
#   · 매칭 실패·형식 오류·중복은 **보고서에 그대로 남긴다**(조용히 0 으로 채우지 않는다).
#   · 노출·클릭 분모가 없으므로 이 경로로 들어온 값으로 **CTR·썸네일 인과효과를
#     계산하지 않는다**(`measurement.thumbnail_effect()` 가 항상 불가 사유를 돌려준다).
#   · 파일 이름 외의 경로·개인정보는 증거 파일에 남기지 않는다.
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001
    import logging
    log = logging.getLogger(__name__)


TITLE_COLUMNS: Tuple[str, ...] = ("제목", "글제목", "title", "콘텐츠", "포스트")
LOGNO_COLUMNS: Tuple[str, ...] = ("글번호", "포스트번호", "logno", "log_no", "post_id")
DATE_COLUMNS: Tuple[str, ...] = ("날짜", "일자", "측정일", "기준일", "date", "day")
VIEWS_COLUMNS: Tuple[str, ...] = ("조회수", "조회", "views", "read", "열람수")
SEARCH_INFLOW_COLUMNS: Tuple[str, ...] = (
    "검색유입", "검색 유입", "검색유입수", "검색 유입 수", "검색 유입 조회수",
    "search_inflow", "search inflow", "search_views", "organic_search_views",
)
REFERRAL_COLUMNS: Tuple[str, ...] = ("유입검색어", "검색어", "keyword", "쿼리", "query")
REFERRAL_COUNT_COLUMNS: Tuple[str, ...] = ("유입수", "방문수", "방문", "visits", "유입건수", "건수")

_DATE_CLEAN_RE = re.compile(r"[./]")
_DATE_RE = re.compile(r"(\d{4})[-.]?(\d{1,2})[-.]?(\d{1,2})")


def parse_day(value: Any) -> str:
    """측정일 문자열을 `YYYY-MM-DD` 로. 못 읽으면 ""(그 행은 무효 처리된다)."""
    text = str(value or "").strip()
    if not text:
        return ""
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    match = _DATE_RE.search(_DATE_CLEAN_RE.sub("-", text))
    if not match:
        return ""
    year, month, day = (int(g) for g in match.groups())
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return ""


def _read_rows(path: Path) -> List[dict]:
    """CSV/XLSX 읽기 — 기존 `stats_fetcher._read_tabular` 를 그대로 재사용한다."""
    from core.stats_fetcher import _read_tabular
    return _read_tabular(path) or []


def _pick(row: dict, names: Tuple[str, ...]) -> str:
    from core.stats_fetcher import _pick as pick
    return pick(row, names)


def _views(row: dict) -> Optional[int]:
    """조회수 칸 — 칸 자체가 없거나 숫자가 아니면 None(0 으로 채우지 않는다).

    단일 정규화 계약(`metrics_contract.coerce_count`)을 쓴다. 예전처럼 문자열에서
    숫자를 긁어오지 않으므로 "2026년 집계" 같은 값이 조회수로 둔갑하지 않는다.
    """
    from core.metrics_contract import STATUS_OK, coerce_count
    value, status = coerce_count(_pick(row, VIEWS_COLUMNS))
    return value if status == STATUS_OK else None


def _search_inflow(row: dict) -> Optional[int]:
    """Search-origin inflow count; it is never inferred from total views."""
    from core.metrics_contract import STATUS_OK, coerce_count
    value, status = coerce_count(_pick(row, SEARCH_INFLOW_COLUMNS))
    return value if status == STATUS_OK else None


def _has_column(row: dict, names: Tuple[str, ...]) -> bool:
    normalized = {str(key).strip().casefold() for key in row}
    return any(name.casefold() in normalized for name in names)


def _resolve_key(*, title: str, log_no: str, data: dict, db_index: list) -> Tuple[str, str]:
    """이 행이 가리키는 글의 (저장 키, 귀속 증거).

    **글 번호가 적혀 있으면 그것만 믿는다** — 번호가 우리 이력에 없으면 제목 유사도로
    폴백하지 않고 포기한다(행이 명시적으로 다른 글을 가리키고 있는데 비슷한 제목의
    글에 성과를 붙이면 그건 위조다).
    글 번호가 없을 때만 제목(정확 → 부분 → 토큰 퍼지)으로 찾고, 어떤 방법으로
    찾았는지를 함께 돌려준다(퍼지 매칭분은 신뢰 범위 밖으로 분류된다).
    """
    from core import metrics_contract as mc
    from core import performance_db as pdb
    from core.stats_fetcher import _fuzzy_best_key

    posts = data.get("posts", {})
    if log_no:
        for key, rec in posts.items():
            if str(rec.get("log_no") or "") == log_no:
                return key, mc.IDENTITY_LOG_NO
        return "", mc.IDENTITY_NONE      # 번호가 있는데 못 찾음 → 제목 폴백 금지
    if not title:
        return "", mc.IDENTITY_NONE
    key = pdb.normalize_key(title)
    if key in posts:
        return key, mc.IDENTITY_TITLE_EXACT
    sub = next((k for k in posts if key and (key in k or k in key)), "")
    if sub:
        return sub, mc.IDENTITY_TITLE_SUBSTRING
    fuzzy = _fuzzy_best_key(title, db_index)
    return (fuzzy, mc.IDENTITY_TITLE_FUZZY) if fuzzy else ("", mc.IDENTITY_NONE)


def _dedupe_measurement_rows(rows: list, *, data: dict, db_index: list,
                             fallback_day: str) -> Tuple[list, int, list]:
    """Hold only conflicting post/day groups; return accepted rows and details."""
    groups = {}
    passthrough = []
    duplicate_rows = 0
    conflicts = {}
    for row_index, row in enumerate(rows, start=2):
        if not isinstance(row, dict):
            passthrough.append(row)
            continue
        title = _pick(row, TITLE_COLUMNS)
        log_no = _pick(row, LOGNO_COLUMNS)
        views = _views(row)
        search_inflow = _search_inflow(row)
        day = parse_day(_pick(row, DATE_COLUMNS)) or fallback_day
        if ((not title and not log_no) or (views is None and search_inflow is None)
                or not day):
            passthrough.append(row)
            continue
        key, _identity = _resolve_key(title=title, log_no=log_no,
                                      data=data, db_index=db_index)
        if not key:
            passthrough.append(row)
            continue
        marker = (key, day)
        current = groups.get(marker)
        if current is None:
            groups[marker] = {"title": title, "log_no": log_no, "day": day,
                              "views": views, "search_inflow": search_inflow,
                              "row_indexes": [row_index]}
            continue
        duplicate_rows += 1
        current["row_indexes"].append(row_index)
        for metric in ("views", "search_inflow"):
            incoming = views if metric == "views" else search_inflow
            existing = current[metric]
            if existing is not None and incoming is not None and existing != incoming:
                conflicts.setdefault(marker, set()).add(metric)
            elif existing is None:
                current[metric] = incoming
    accepted = [
        {"title": item["title"], "log_no": item["log_no"], "date": item["day"],
         "views": item["views"], "search_inflow": item["search_inflow"]}
        for marker, item in groups.items() if marker not in conflicts
    ]
    details = [{"post_key": marker[0], "title": str(
                    (data.get("posts", {}).get(marker[0], {}) or {}).get("title") or marker[0]),
                "day": marker[1],
                "metrics": sorted(metrics),
                "rows": groups[marker]["row_indexes"]}
               for marker, metrics in sorted(conflicts.items())]
    return passthrough + accepted, duplicate_rows, details


def _collect_referrals(rows: list, *, data: dict, db_index: list,
                       fallback_day: str, conflicted_groups: set) -> Tuple[dict, int, list]:
    """Collect query snapshots and conflicts without mutating stored data."""
    snapshots = {}
    conflicts = []
    unmatched = 0
    for row_index, row in enumerate(rows, start=2):
        if not isinstance(row, dict):
            continue
        keyword = _pick(row, REFERRAL_COLUMNS)
        if not keyword:
            continue
        title = _pick(row, TITLE_COLUMNS)
        log_no = _pick(row, LOGNO_COLUMNS)
        day = parse_day(_pick(row, DATE_COLUMNS)) or fallback_day
        key, _identity = _resolve_key(title=title, log_no=log_no,
                                      data=data, db_index=db_index)
        if not key:
            unmatched += 1
            continue
        marker = (key, day)
        if marker in conflicted_groups:
            continue
        count_text = _pick(row, REFERRAL_COUNT_COLUMNS)
        if count_text:
            from core.metrics_contract import STATUS_OK, coerce_count
            count, status = coerce_count(count_text)
            if status != STATUS_OK:
                continue
        else:
            count = 1
        identity = (key, day, keyword)
        previous = snapshots.get(identity)
        if previous is not None and previous[0] != count:
            conflicts.append({"post_key": key, "day": day, "keyword": keyword,
                              "rows": [previous[1], row_index]})
        else:
            snapshots[identity] = (int(count), row_index)
    return snapshots, unmatched, conflicts


def inspect(path: Any) -> Dict[str, Any]:
    """파일을 **적재하지 않고** 형식만 본다(GUI 미리보기용, 부작용 0)."""
    report: Dict[str, Any] = {"ok": False, "reason": "", "file": "",
                               "rows": 0, "columns": [], "has_identifier": False,
                               "has_views": False, "has_search_inflow": False,
                               "has_metric": False, "has_referral": False,
                               "has_date": False}
    target = Path(str(path or ""))
    report["file"] = target.name
    if not target.is_file():
        report["reason"] = "file_missing"
        return report
    try:
        rows = _read_rows(target)
    except Exception as exc:  # noqa: BLE001 — 깨진 파일
        report["reason"] = f"read_error:{type(exc).__name__}"
        return report
    if not rows:
        report["reason"] = "no_rows"
        return report
    report["rows"] = len(rows)
    report["columns"] = [str(c) for c in (rows[0].keys() if isinstance(rows[0], dict) else [])]
    first = rows[0] if isinstance(rows[0], dict) else {}
    report["has_identifier"] = bool(_pick(first, TITLE_COLUMNS) or _pick(first, LOGNO_COLUMNS))
    report["has_views"] = _has_column(first, VIEWS_COLUMNS)
    report["has_search_inflow"] = _has_column(first, SEARCH_INFLOW_COLUMNS)
    report["has_referral"] = bool(_pick(first, REFERRAL_COLUMNS))
    report["has_metric"] = (report["has_views"] or report["has_search_inflow"]
                            or report["has_referral"])
    report["has_date"] = bool(parse_day(_pick(first, DATE_COLUMNS)))
    if not report["has_identifier"]:
        report["reason"] = "no_identifier_column"
    elif not report["has_metric"]:
        report["reason"] = "no_metric_column"
    else:
        report["ok"] = True
    return report


def import_file(path: Any, *, default_day: str = "",
                window: str = "d7", basis: str = "unknown") -> Dict[str, Any]:
    """통계 파일을 검증해 성과 입력으로 적재한다.

    Args:
        default_day: 파일에 측정일 칸이 없을 때 쓸 기준일(`YYYY-MM-DD`). 비우면 오늘.
        window: 적재할 명시 창(`d1`/`d7`/`d28`).
        basis: 숫자의 의미(`period`/`cumulative`/`unknown`). 미선택 시 환류에 쓰지 않는다.

    Returns:
        {"ok", "reason", "file", "rows_total", "rows_valid", "rows_invalid",
         "rows_duplicate", "matched", "unmatched", "measured", "days", "held_reason"}

        · `ok=False` 면 **아무것도 적재하지 않았다**. `reason` 이 사유다.
        · `unmatched` 가 남아도 실패가 아니다(그 행만 버린 것) — 숫자로 보고한다.
        · `held_reason` 이 있으면 적재는 됐지만 **환류는 아직 보류**라는 뜻이다
          (표본·커버리지 게이트 미달 등). 성공으로 포장하지 않는다.
    """
    report: Dict[str, Any] = {
        "ok": False, "reason": "", "file": "", "window": window,
        "rows_total": 0, "rows_valid": 0, "rows_invalid": 0, "rows_duplicate": 0,
        "rows_conflicting": 0, "conflict_details": [],
        "matched": 0, "unmatched": 0, "measured": 0, "days": [], "held_reason": "",
        "matched_views": 0, "matched_search_inflow": 0,
        "referral_rows": 0, "referral_unmatched": 0,
        "measured_views": 0, "measured_search_inflow": 0,
        "measurements_skipped": {}, "basis": basis,
        # 귀속 증거별 내역 — 제목 유사도로 붙인 행이 몇 개인지 숨기지 않는다.
        "matched_by_log_no": 0, "matched_by_title": 0, "matched_by_fuzzy": 0,
        "rejected_log_no": 0, "source": "manual_csv",
    }
    target = Path(str(path or ""))
    report["file"] = target.name

    pre = inspect(target)
    if not pre["ok"]:
        report["reason"] = pre["reason"] or "invalid_file"
        return report

    fallback_day = parse_day(default_day) or date.today().isoformat()

    try:
        from core import metrics_contract as mc
        from core import performance_db as pdb
        from core.stats_fetcher import _tokenize
    except Exception as exc:  # noqa: BLE001
        report["reason"] = f"import_error:{type(exc).__name__}"
        return report

    try:
        original_rows = _read_rows(target)
        data = pdb.load()
        db_index = [(k, _tokenize(rec.get("clean_title") or rec.get("title") or ""))
                    for k, rec in data.get("posts", {}).items()]
        report["rows_total"] = len(original_rows)
        rows, duplicate_count, conflicts = _dedupe_measurement_rows(
            original_rows, data=data, db_index=db_index, fallback_day=fallback_day)
        report["rows_duplicate"] += duplicate_count
        conflicted_groups = {(item["post_key"], item["day"]) for item in conflicts}
        referral_snapshots, referral_unmatched, referral_conflicts = _collect_referrals(
            original_rows, data=data, db_index=db_index, fallback_day=fallback_day,
            conflicted_groups=conflicted_groups)
        for item in referral_conflicts:
            marker = (item["post_key"], item["day"])
            conflicted_groups.add(marker)
            existing_detail = next((detail for detail in conflicts
                                    if (detail["post_key"], detail["day"]) == marker), None)
            if existing_detail is None:
                existing_detail = {"post_key": item["post_key"], "day": item["day"],
                                   "title": str((data.get("posts", {}).get(item["post_key"], {})
                                                 or {}).get("title") or item["post_key"]),
                                   "metrics": [], "rows": []}
                conflicts.append(existing_detail)
            if "referral" not in existing_detail["metrics"]:
                existing_detail["metrics"].append("referral")
            existing_detail["rows"] = sorted(set(existing_detail["rows"] + item["rows"]))
        report["rows_conflicting"] = len(conflicted_groups)
        report["conflict_details"] = conflicts
        report["referral_unmatched"] = referral_unmatched
        for (key, day, keyword), (_count, _row_index) in referral_snapshots.items():
            if (key, day) in conflicted_groups:
                continue
            from core import performance_db as _pdb
            _pdb.record_referral(key, keyword, _count, data=data)
            report["referral_rows"] += 1
        filtered_rows = []
        for row in rows:
            title = _pick(row, TITLE_COLUMNS) if isinstance(row, dict) else ""
            log_no = _pick(row, LOGNO_COLUMNS) if isinstance(row, dict) else ""
            day = parse_day(_pick(row, DATE_COLUMNS)) if isinstance(row, dict) else ""
            key, _identity = _resolve_key(title=title, log_no=log_no,
                                          data=data, db_index=db_index)
            marker = (key, day or fallback_day)
            if marker not in conflicted_groups:
                filtered_rows.append(row)
        rows = filtered_rows
        days: set = set()
        applied = 0

        for row in rows:
            if not isinstance(row, dict):
                report["rows_invalid"] += 1
                continue
            title = _pick(row, TITLE_COLUMNS)
            log_no = _pick(row, LOGNO_COLUMNS)
            views = _views(row)
            search_inflow = _search_inflow(row)
            day = parse_day(_pick(row, DATE_COLUMNS)) or fallback_day

            if (not title and not log_no) or (views is None and search_inflow is None) or not day:
                report["rows_invalid"] += 1
                continue
            report["rows_valid"] += 1

            key, identity = _resolve_key(title=title, log_no=log_no,
                                         data=data, db_index=db_index)
            if not key:
                report["unmatched"] += 1
                if log_no:
                    report["rejected_log_no"] += 1   # 번호가 우리 이력에 없는 행
                continue
            if identity == mc.IDENTITY_LOG_NO:
                report["matched_by_log_no"] += 1
            elif identity == mc.IDENTITY_TITLE_FUZZY:
                report["matched_by_fuzzy"] += 1
            else:
                report["matched_by_title"] += 1

            # 이미 같은 값이 적혀 있으면 재적재로 보지 않는다(같은 파일 재가져오기 안전).
            existing = (data["posts"].get(key, {}).get("views") or {}).get(day)
            views_new = (views is not None
                         and (existing is None or int(existing) != int(views)))
            if views is not None and not views_new:
                report["rows_duplicate"] += 1
            if views_new:
                pdb.record_views(key, int(views), day=day, data=data)
                # 이 날짜의 값을 어떤 증거로 붙였는지 남긴다(additive).
                rec = data["posts"].get(key)
                if isinstance(rec, dict):
                    rec.setdefault("views_identity", {})[day] = identity
            report["matched"] += 1
            if views is not None:
                report["matched_views"] += 1
                applied += int(views_new)
            if search_inflow is not None:
                report["matched_search_inflow"] += 1
            days.add(day)

        if applied or report["referral_rows"]:
            pdb.save(data)
        report["days"] = sorted(days)
    except Exception as exc:  # noqa: BLE001 — 적재 중 실패는 실패로 보고
        log.warning(f"[stats_csv] 적재 실패({type(exc).__name__}): {exc}")
        report["reason"] = f"ingest_error:{type(exc).__name__}"
        return report

    if not report["matched"] and not report["referral_rows"]:
        report["reason"] = "no_matched_rows"
        return report

    # 적재분을 measurement 창으로 승격하고, 사람이 넣은 입력이라는 증거를 남긴다.
    try:
        from core import measurement as ms
        from core import metrics_contract as mc
        measured_keys = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            day = parse_day(_pick(row, DATE_COLUMNS)) or fallback_day
            title = _pick(row, TITLE_COLUMNS)
            log_no = _pick(row, LOGNO_COLUMNS)
            key, identity = _resolve_key(title=title, log_no=log_no,
                                         data=data, db_index=db_index)
            if not key:
                continue
            values = (("post_views", _views(row)),
                      ("post_search_inflow", _search_inflow(row)))
            for metric, value in values:
                if value is None:
                    continue
                existing_entries = (data["posts"].get(key, {}).get("measurements") or [])
                if any(entry.get("metric") == metric and entry.get("window") == window
                       and entry.get("source") == "stats_export_csv"
                       and str(entry.get("collected_at") or "")[:10] == day
                       and entry.get("value") == value for entry in existing_entries):
                    continue
                outcome = ms.record(metric, value, post_key=key, log_no=log_no,
                                    window=window, source="stats_export_csv",
                                    collected_at=day, basis=basis, identity=identity)
                if outcome.get("saved"):
                    measured_keys.add((key, metric))
                    report["measured_views" if metric == "post_views"
                           else "measured_search_inflow"] += 1
                    if isinstance(data["posts"].get(key), dict):
                        data["posts"][key].setdefault("measurements", []).append({
                            "metric": metric, "window": window,
                            "source": "stats_export_csv", "collected_at": day,
                            "basis": basis, "value": value,
                        })
                elif outcome.get("skipped"):
                    reason = str(outcome.get("reason") or "unknown")
                    report["measurements_skipped"][reason] = (
                        report["measurements_skipped"].get(reason, 0) + 1)
        report["measured"] = len(measured_keys)
        ms.record_manual_source(kind="stats_export_csv", file_name=target.name,
                                rows_valid=report["rows_valid"],
                                matched=report["matched"], window=window,
                                days=report["days"], metric="post_search_inflow"
                                if report["matched_search_inflow"] else "post_views",
                                basis=basis)
        report["held_reason"] = str(ms.selected_feedback_weights().get("held_reason") or "")
    except Exception as exc:  # noqa: BLE001 — 승격 실패는 적재 성공을 지우지 않는다
        log.warning(f"[stats_csv] measurement 승격 실패({type(exc).__name__})")
        report["held_reason"] = f"promote_error:{type(exc).__name__}"

    report["ok"] = True
    log.info("[stats_csv] 적재 완료 — 유효 %d행 · 매칭 %d · 중복 %d · 무효 %d · 미매칭 %d",
             report["rows_valid"], report["matched"], report["rows_duplicate"],
             report["rows_invalid"], report["unmatched"])
    return report


__all__ = ["TITLE_COLUMNS", "LOGNO_COLUMNS", "DATE_COLUMNS", "VIEWS_COLUMNS",
           "SEARCH_INFLOW_COLUMNS",
           "parse_day", "inspect", "import_file"]
