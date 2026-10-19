# core/ai_tell_calibrate.py
# "AI 티 스코어러 Phase B — 캘리브레이션" (읽기 전용 배치 스캔 + 리포트).
#
# Phase A(core/ai_tell_scorer.score_ai_tell)의 임계·가중 상수는 전부
# `# CALIBRATION-PENDING` placeholder 다. 이 모듈은 **우리 실제 글 코퍼스**의
# 점수 분포를 실측해서 임계값 기준선을 데이터에서 도출한다(영어 원본 수치 직수입 금지).
#
# ★ 읽기 전용: 보관본·발행글·DB를 절대 수정하지 않는다. 유일한 쓰기는
#   reports/{date}_ai_tell_calibration.html 리포트 1건. 발행 트리거 0, 네트워크 0.
# ★ fail-open: 한 글 스코어링 실패가 배치 전체를 죽이면 안 된다 — 글별 try/except
#   로 건너뛰고 카운트한다.
# ★ 프로필 격리 존중: 데이터 경로 하드코딩 금지. draft_store.load_drafts() 가
#   활성 프로필 runs dir 을 자동 해석하므로 그걸 그대로 쓴다.
# ★ 표준 라이브러리 + 기존 모듈만.
#
# ⚠️ 본문 전처리 주의: 저장된 보관본 post_data["content"] 는 HTML 이지만 라이브
#    스코어링은 생성 시점 마크다운에 걸린다. 캘리브레이션 유효성을 위해 코퍼스 본문을
#    스코어링 전 HTML 태그 제거(<[^>]+>) + 엔티티 언이스케이프로 평문 근사한다.
#    → **절대 수치보다 집단 간 상대 분포가 신뢰 축이다**(리포트에 caveat 명시).
from __future__ import annotations

import html
import math
import re
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

from config.logger import get_logger
from core import ai_tell_scorer
from core import draft_store
from core import draft_restyle

log = get_logger(__name__)

# HTML → 평문 근사: 태그를 공백으로 치환(단어 결합 방지) 후 엔티티 언이스케이프.
_TAG_RE = re.compile(r"<[^>]+>")

# 신호 percentile 집계에서 제외할 키(점수 그 자체·파생 메타라 분포 의미 없음).
# raw=soft raw, fp_raw=기계 지문 raw, density=100어절당 밀도(전부 파생 메타).
_SKIP_SIGNALS = frozenset({"raw", "norm", "fp_raw", "density"})


# ─────────────────────────────────────────────────────
# 경로
# ─────────────────────────────────────────────────────
def _reports_dir() -> Path:
    """리포트 산출 폴더 — 프로필별(2026-07-19 격리 감사), 실패 시 레거시 하네스."""
    try:
        from config import profile as _nbaprofile
        p = _nbaprofile.reports_dir()
    except Exception:  # noqa: BLE001 — 경로 해석 실패도 리포트를 막지 않게
        p = Path(__file__).resolve().parent.parent.parent / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p


# ─────────────────────────────────────────────────────
# 순수 헬퍼
# ─────────────────────────────────────────────────────
def _percentile(values: List[float], q: float) -> Optional[float]:
    """선형 보간 percentile(numpy 기본식). q 는 0~100. 표본 0 이면 None.

    표본이 적을 때도 안전하게 동작한다(1개면 그 값, 2개 이상은 보간).
    """
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n == 1:
        return round(float(ordered[0]), 4)
    rank = (q / 100.0) * (n - 1)
    low = int(math.floor(rank))
    high = int(math.ceil(rank))
    if low == high:
        return round(float(ordered[low]), 4)
    frac = rank - low
    interp = ordered[low] * (1.0 - frac) + ordered[high] * frac
    return round(float(interp), 4)


def _percentile_block(values: List[float]) -> dict:
    """점수 분포 요약 블록 {p50,p75,p90,p95,max}. 표본 0 이면 전부 None."""
    if not values:
        return {"p50": None, "p75": None, "p90": None, "p95": None, "max": None}
    return {
        "p50": _percentile(values, 50),
        "p75": _percentile(values, 75),
        "p90": _percentile(values, 90),
        "p95": _percentile(values, 95),
        "max": round(float(max(values)), 4),
    }


def _html_to_prose(body: str) -> str:
    """HTML 본문을 평문으로 근사(태그 제거 + 엔티티 언이스케이프).

    태그를 공백으로 치환해 단어가 붙지 않게 하고, &amp; 등 엔티티를 원문자로 되돌린다.
    score_ai_tell 내부의 표/코드/FAQ 전처리는 그대로 살아 있다(이 함수는 태그만 벗긴다).
    """
    if not isinstance(body, str) or not body:
        return ""
    text = _TAG_RE.sub(" ", body)
    return html.unescape(text)


def _draft_body(draft: dict) -> str:
    """draft 항목에서 본문 문자열을 꺼낸다 — post_data.content 우선, 없으면 상위 content."""
    if not isinstance(draft, dict):
        return ""
    post = draft.get("post_data")
    if isinstance(post, dict):
        body = post.get("content")
        if isinstance(body, str) and body.strip():
            return body
    body = draft.get("content")
    return body if isinstance(body, str) else ""


# ─────────────────────────────────────────────────────
# 코퍼스 스캔
# ─────────────────────────────────────────────────────
def scan_corpus(drafts: list, label: str) -> dict:
    """draft 리스트를 스코어링해 점수·레벨·신호 분포를 집계한다(읽기 전용·fail-open).

    각 draft 본문(HTML)을 평문으로 근사 → score_ai_tell → 점수·레벨·신호값 수집.

    Args:
        drafts: draft_store.load_drafts(...) 형식의 dict 리스트.
        label:  집단 라벨("old"/"published" 등, 리포트 표기용).

    Returns:
        {
          "label", "n",
          "scores": [float...],
          "percentiles": {p50,p75,p90,p95,max},
          "level_counts": {low,mid,high},
          "signal_percentiles": {신호명: {p50,p90,p95,n}},
          "p0_hits": int,        # 기계 지문(p0) 걸린 글 수
          "errors": int,         # 스코어링 실패(예외/error 반환) 글 수
          "skipped_empty": int,  # 본문이 비어 스코어링 대상이 아니던 글 수
        }
        표본 0 이어도 크래시 없이 정직한 빈 요약을 반환한다.
    """
    scores: List[float] = []
    level_counts = {"low": 0, "mid": 0, "high": 0}
    signal_values: dict = {}
    p0_hits = 0
    errors = 0
    skipped_empty = 0

    for draft in (drafts or []):
        try:
            body = _draft_body(draft)
            if not body or not body.strip():
                skipped_empty += 1
                continue
            prose = _html_to_prose(body)
            if not prose.strip():
                skipped_empty += 1
                continue
            result = ai_tell_scorer.score_ai_tell(prose)
            if not isinstance(result, dict) or result.get("error"):
                errors += 1
                continue

            score = float(result.get("score", 0.0))
            scores.append(score)

            level = result.get("level")
            if level in level_counts:
                level_counts[level] += 1

            if result.get("p0"):
                p0_hits += 1

            signals = result.get("signals")
            if isinstance(signals, dict):
                for key, val in signals.items():
                    if key in _SKIP_SIGNALS:
                        continue
                    if isinstance(val, bool):  # bool 은 int 하위형이라 명시 배제
                        continue
                    if isinstance(val, (int, float)):
                        signal_values.setdefault(key, []).append(float(val))
        except Exception as exc:  # noqa: BLE001 — 글별 실패 격리(배치 전체 보호)
            errors += 1
            log.debug("[ai_tell_calibrate] scan_corpus 글 스코어링 실패(무시): %s", exc)

    signal_percentiles: dict = {}
    for key in sorted(signal_values):
        vals = signal_values[key]
        signal_percentiles[key] = {
            "p50": _percentile(vals, 50),
            "p90": _percentile(vals, 90),
            "p95": _percentile(vals, 95),
            "n": len(vals),
        }

    return {
        "label": label,
        "n": len(scores),
        "scores": scores,
        "percentiles": _percentile_block(scores),
        "level_counts": level_counts,
        "signal_percentiles": signal_percentiles,
        "p0_hits": p0_hits,
        "errors": errors,
        "skipped_empty": skipped_empty,
    }


def scan_restyle_pairs() -> dict:
    """리스타일 전/후(백업=before, 현재=after) 같은 글의 AI 티 점수 델타를 측정.

    draft_restyle.has_backup(slug) 인 슬러그는 post_data.prestyle.json(리스타일 전)과
    현재 post_data.json(리스타일 후)이 둘 다 있다. 두 본문을 평문 근사 후 스코어링해
    delta = before - after (양수 = AI 티 감소 = 개선)를 계산한다.

    "우리 변주 시스템이 실제로 AI 티를 낮추는가"의 직접 증거이자 임계 설정 근거.

    Returns:
        {"n", "before_p50", "after_p50", "delta_mean",
         "improved_count", "worsened_count", "errors", "pairs":[{slug,before,after,delta}...]}.
        표본 0 이면 통계값 None·빈 pairs(크래시 금지).
    """
    pairs: List[dict] = []
    befores: List[float] = []
    afters: List[float] = []
    deltas: List[float] = []
    improved = 0
    worsened = 0
    errors = 0

    # 백업(prestyle) 파일이 있는 슬러그 열거 — draft_store._RUNS_DIR 기준(프로필 격리).
    try:
        runs_dir = draft_store._RUNS_DIR
        children = list(runs_dir.iterdir()) if runs_dir.exists() else []
    except OSError as exc:
        log.warning("[ai_tell_calibrate] runs 스캔 실패: %s", exc)
        children = []

    for child in children:
        slug = child.name
        try:
            if not child.is_dir() or not draft_restyle.has_backup(slug):
                continue

            before_json = draft_store._read_json(draft_restyle._backup_path(slug))
            after_json = draft_store._read_json(draft_restyle._post_path(slug))
            if not isinstance(before_json, dict) or not isinstance(after_json, dict):
                errors += 1
                continue

            before_body = before_json.get("content") or ""
            after_body = after_json.get("content") or ""
            if not str(before_body).strip() or not str(after_body).strip():
                errors += 1
                continue

            before_res = ai_tell_scorer.score_ai_tell(_html_to_prose(before_body))
            after_res = ai_tell_scorer.score_ai_tell(_html_to_prose(after_body))
            if (not isinstance(before_res, dict) or before_res.get("error")
                    or not isinstance(after_res, dict) or after_res.get("error")):
                errors += 1
                continue

            before = round(float(before_res.get("score", 0.0)), 2)
            after = round(float(after_res.get("score", 0.0)), 2)
            delta = round(before - after, 2)
            befores.append(before)
            afters.append(after)
            deltas.append(delta)
            if delta > 0:
                improved += 1
            elif delta < 0:
                worsened += 1
            pairs.append({"slug": slug, "before": before, "after": after, "delta": delta})
        except Exception as exc:  # noqa: BLE001 — 쌍별 실패 격리
            errors += 1
            log.debug("[ai_tell_calibrate] restyle 쌍 실패(무시) %s: %s", slug, exc)

    pairs.sort(key=lambda p: p["delta"], reverse=True)
    return {
        "n": len(pairs),
        "before_p50": _percentile(befores, 50) if befores else None,
        "after_p50": _percentile(afters, 50) if afters else None,
        "delta_mean": round(statistics.mean(deltas), 3) if deltas else None,
        "improved_count": improved,
        "worsened_count": worsened,
        "errors": errors,
        "pairs": pairs,
    }


# ─────────────────────────────────────────────────────
# 권장 임계 도출 — 데이터에서(영어 원본 수치 직수입 금지)
# ─────────────────────────────────────────────────────
def _recommend_thresholds(old: dict, published: dict, restyle: dict) -> dict:
    """옛 보관본 분포에서 임계 기준선을 도출한다.

    - pass_ceiling  = old p50 이하  → "통과"(코퍼스 절반이 이 아래)
    - review_floor  = old p75 이상  → "재검토" 후보
    - high_floor    = old p90 이상  → "high" AI 티 후보(상위 10%)
    표본이 없으면 해당 값 None(정직).
    """
    op = old.get("percentiles") or {}
    return {
        "basis": old.get("label", "old"),
        "basis_n": old.get("n", 0),
        "pass_ceiling": op.get("p50"),
        "review_floor": op.get("p75"),
        "high_floor": op.get("p90"),
        "restyle_after_p50": restyle.get("after_p50"),
        "restyle_before_p50": restyle.get("before_p50"),
        "restyle_delta_mean": restyle.get("delta_mean"),
    }


# ─────────────────────────────────────────────────────
# 리포트(HTML)
# ─────────────────────────────────────────────────────
def _fmt(value: Optional[float]) -> str:
    """None 안전 수치 포맷(리포트 셀용)."""
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.2f}"
    return str(value)


def _corpus_table_row(group: dict) -> str:
    p = group.get("percentiles") or {}
    lv = group.get("level_counts") or {}
    return (
        "<tr>"
        f"<td class='lbl'>{html.escape(str(group.get('label', '')))}</td>"
        f"<td class='num'>{group.get('n', 0)}</td>"
        f"<td class='num'>{_fmt(p.get('p50'))}</td>"
        f"<td class='num'>{_fmt(p.get('p75'))}</td>"
        f"<td class='num'>{_fmt(p.get('p90'))}</td>"
        f"<td class='num'>{_fmt(p.get('p95'))}</td>"
        f"<td class='num'>{_fmt(p.get('max'))}</td>"
        f"<td class='num'>{lv.get('low', 0)}</td>"
        f"<td class='num'>{lv.get('mid', 0)}</td>"
        f"<td class='num'>{lv.get('high', 0)}</td>"
        f"<td class='num'>{group.get('p0_hits', 0)}</td>"
        f"<td class='num'>{group.get('errors', 0)}</td>"
        f"<td class='num'>{group.get('skipped_empty', 0)}</td>"
        "</tr>"
    )


def _signal_table(group: dict) -> str:
    sig = group.get("signal_percentiles") or {}
    if not sig:
        return "<p class='muted'>수집된 신호 없음(표본 부족).</p>"
    rows = []
    for key in sorted(sig):
        blk = sig[key]
        rows.append(
            "<tr>"
            f"<td class='lbl'>{html.escape(str(key))}</td>"
            f"<td class='num'>{blk.get('n', 0)}</td>"
            f"<td class='num'>{_fmt(blk.get('p50'))}</td>"
            f"<td class='num'>{_fmt(blk.get('p90'))}</td>"
            f"<td class='num'>{_fmt(blk.get('p95'))}</td>"
            "</tr>"
        )
    return (
        "<table><thead><tr><th>신호</th><th>표본(present)</th>"
        "<th>p50</th><th>p90</th><th>p95</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _restyle_section(restyle: dict) -> str:
    n = restyle.get("n", 0)
    if not n:
        return ("<p class='muted'>리스타일 A/B 쌍이 없습니다"
                "(post_data.prestyle.json 백업이 있는 슬러그 0건).</p>")
    head = (
        "<ul class='kv'>"
        f"<li>쌍 수 <b>{n}</b></li>"
        f"<li>before p50 <b>{_fmt(restyle.get('before_p50'))}</b></li>"
        f"<li>after p50 <b>{_fmt(restyle.get('after_p50'))}</b></li>"
        f"<li>평균 델타(before−after) <b>{_fmt(restyle.get('delta_mean'))}</b> "
        "<span class='muted'>(양수 = AI 티 감소)</span></li>"
        f"<li>개선 <b>{restyle.get('improved_count', 0)}</b> / "
        f"악화 <b>{restyle.get('worsened_count', 0)}</b> / "
        f"오류 <b>{restyle.get('errors', 0)}</b></li>"
        "</ul>"
    )
    # 델타가 음수면(=리스타일 후 점수가 되레 올라감) 해석 caveat 을 붙인다.
    dm = restyle.get("delta_mean")
    if isinstance(dm, (int, float)) and dm < 0:
        head += (
            "<div class='caveat'>"
            "<b>⚠️ 평균 델타 음수 — 두 시스템은 서로 다른 축을 최적화합니다.</b><br>"
            "설계도 주도 변주(리스타일)는 <b>도입·구조·말투의 획일성</b>을 겨냥하지, "
            "이 스코어러가 잡는 <b>반전 구문(‘~가 아니라 ~입니다’)·어휘 티어</b>를 낮추지 "
            "않습니다. 오히려 ‘판단 흔적’ 주입이 반전 구문을 <b>추가</b>해 점수가 오른 "
            "사례가 관측됐습니다(before inversion 0 → after 2~4). 이는 스코어러 버그가 "
            "아니라, 변주 시스템에 되먹임할 실측 신호입니다(반전 구문 남용 억제 검토)."
            "</div>"
        )
    pairs = restyle.get("pairs") or []
    sample = pairs[:20]
    rows = []
    for pr in sample:
        cls = "up" if pr["delta"] > 0 else ("down" if pr["delta"] < 0 else "")
        rows.append(
            "<tr>"
            f"<td class='lbl'>{html.escape(str(pr['slug']))}</td>"
            f"<td class='num'>{_fmt(pr['before'])}</td>"
            f"<td class='num'>{_fmt(pr['after'])}</td>"
            f"<td class='num {cls}'>{_fmt(pr['delta'])}</td>"
            "</tr>"
        )
    tbl = (
        "<table><thead><tr><th>슬러그</th><th>before</th><th>after</th>"
        "<th>델타</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    more = (f"<p class='muted'>상위 20건 표시(총 {len(pairs)}건, 델타 내림차순).</p>"
            if len(pairs) > 20 else "")
    return head + tbl + more


def _render_html(old: dict, published: dict, restyle: dict,
                 thresholds: dict, today: str) -> str:
    corpus_rows = _corpus_table_row(old) + _corpus_table_row(published)
    rec_rows = (
        "<ul class='kv'>"
        f"<li>기준 집단 <b>{html.escape(str(thresholds.get('basis')))}</b> "
        f"(표본 {thresholds.get('basis_n', 0)})</li>"
        f"<li>통과 상한 (old p50 이하) <b>{_fmt(thresholds.get('pass_ceiling'))}</b></li>"
        f"<li>재검토 하한 (old p75 이상) <b>{_fmt(thresholds.get('review_floor'))}</b></li>"
        f"<li>high 후보 하한 (old p90 이상) <b>{_fmt(thresholds.get('high_floor'))}</b></li>"
        "</ul>"
    )
    return f"""<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI 티 스코어러 캘리브레이션 — {today}</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font-family: -apple-system, "Segoe UI", "Malgun Gothic", sans-serif;
  max-width: 960px; margin: 0 auto; padding: 24px; line-height: 1.6;
  background: #fafafa; color: #1a1a1a; }}
@media (prefers-color-scheme: dark) {{
  body {{ background: #16181c; color: #e6e6e6; }}
  table {{ background: #1e2126; }}
  th {{ background: #262a30; }}
  .card {{ background: #1e2126; border-color: #333; }}
  code {{ background: #262a30; }}
}}
h1 {{ font-size: 1.5rem; margin-bottom: 4px; }}
h2 {{ font-size: 1.15rem; margin-top: 28px; border-bottom: 2px solid #4a7; padding-bottom: 4px; }}
.sub {{ color: #888; font-size: 0.9rem; margin-top: 0; }}
.card {{ background: #fff; border: 1px solid #e2e2e2; border-radius: 8px;
  padding: 12px 16px; margin: 12px 0; }}
.caveat {{ border-left: 4px solid #e6a817; background: rgba(230,168,23,0.08);
  padding: 10px 14px; border-radius: 4px; margin: 12px 0; font-size: 0.92rem; }}
table {{ border-collapse: collapse; width: 100%; margin: 10px 0;
  font-size: 0.88rem; background: #fff; overflow-x: auto; display: block; }}
@media (min-width: 720px) {{ table {{ display: table; }} }}
th, td {{ border: 1px solid #dcdcdc; padding: 5px 8px; text-align: left; }}
@media (prefers-color-scheme: dark) {{ th, td {{ border-color: #333; }} }}
th {{ background: #f0f2f4; font-weight: 600; }}
td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
td.lbl {{ font-weight: 500; }}
.up {{ color: #2a9d4a; font-weight: 600; }}
.down {{ color: #d1443b; font-weight: 600; }}
.muted {{ color: #999; font-size: 0.88rem; }}
ul.kv {{ list-style: none; padding-left: 0; }}
ul.kv li {{ padding: 3px 0; border-bottom: 1px dashed #ddd; }}
@media (prefers-color-scheme: dark) {{ ul.kv li {{ border-color: #333; }} }}
code {{ background: #eef; padding: 1px 5px; border-radius: 3px; font-size: 0.85em; }}
</style></head><body>

<h1>🎚️ AI 티 스코어러 캘리브레이션 (Phase B)</h1>
<p class="sub">{today} · 읽기 전용 배치 스캔 · 발행/네트워크 0</p>

<div class="caveat">
<b>⚠️ 신뢰 축 = 집단 간 상대 분포, 절대 수치 아님.</b><br>
저장된 보관본 본문은 <b>HTML</b> 이라 스코어링 전 태그 제거(<code>&lt;[^&gt;]+&gt;</code>)로
<b>평문 근사</b>했습니다. 라이브 스코어링은 생성 시점 <b>마크다운</b>에 걸리므로
전처리 경로가 다릅니다. 따라서 점수의 <b>절대값</b>보다 old vs published,
리스타일 before vs after 같은 <b>상대 비교</b>를 임계 설정의 신뢰 축으로 삼으세요.
(score_ai_tell 자체의 표/코드/FAQ 전처리는 평문 근사 후에도 그대로 작동합니다.)
</div>

<h2>1. 집단별 점수 분포</h2>
<p class="muted">A = 옛 보관본(draft_ready, 개선 이전) · B = 발행됨(uploaded).
발행글은 업로드 시 post_data.json 이 정리(prune)되므로 본문이 없어
<code>skipped_empty</code> 로 집계될 수 있습니다(정상).</p>
<table><thead><tr>
<th>집단</th><th>n</th><th>p50</th><th>p75</th><th>p90</th><th>p95</th><th>max</th>
<th>low</th><th>mid</th><th>high</th><th>p0글수</th><th>err</th><th>본문없음</th>
</tr></thead><tbody>{corpus_rows}</tbody></table>

<h2>2. 신호별 분포 (A 집단 · present 표본 기준)</h2>
<p class="muted">각 신호가 실제로 관측된 글에서의 percentile 입니다(부재는 미포함).
count 계열 신호의 절대 빈도가 아니라 "걸린 글들에서 얼마나 셌나"의 분포입니다.</p>
{_signal_table(old)}

<h2>3. 리스타일 A/B 델타 (before − after)</h2>
<p class="muted">post_data.prestyle.json 백업(리스타일 전)과 현재 본문(리스타일 후)의
같은 글 점수 차. 양수 = 변주 시스템이 AI 티를 낮췄다는 직접 증거.</p>
{_restyle_section(restyle)}

<h2>4. 권장 임계 제안 (데이터 도출)</h2>
<div class="card">{rec_rows}</div>
<div class="caveat">
이 값은 <b>옛 보관본(A) 분포에서 도출</b>했습니다 — 영어 원본 수치를 직수입하지
않았습니다. A 는 "개선 이전" 코퍼스라 AI 티가 높은 쪽이 기준선이므로,
실제 게이트로 승격하기 전 <b>리스타일 후(after) 분포</b>와 대조해
목표 밴드를 확정하세요. 발행 차단 게이트로의 승격은 여전히 사용자 판단 몫입니다
(Phase A 는 관측 전용).
</div>

<p class="sub">생성: <code>python -m core.ai_tell_calibrate</code></p>
</body></html>"""


# ─────────────────────────────────────────────────────
# 종합 실행
# ─────────────────────────────────────────────────────
def run_calibration() -> dict:
    """A(옛 보관본)·B(발행됨)·C(리스타일 쌍) 전부 스캔 + 리포트 작성(읽기 전용).

    reports/{date}_ai_tell_calibration.html 를 1건 쓴다(유일한 쓰기). draft 데이터를
    절대 mutate 하지 않는다.

    Returns:
        {"report_path", "old", "published", "restyle", "thresholds"} 요약 dict.
        old/published 는 scan_corpus 요약(scores 리스트 제외 — 요약만), restyle 는
        scan_restyle_pairs 요약, thresholds 는 권장 임계.
    """
    old = scan_corpus(draft_store.load_drafts("draft_ready"), "old")
    published = scan_corpus(draft_store.load_drafts("uploaded"), "published")
    restyle = scan_restyle_pairs()
    thresholds = _recommend_thresholds(old, published, restyle)

    today = date.today().strftime("%Y-%m-%d")
    report_path: Optional[str] = None
    try:
        out = _reports_dir() / f"{today}_ai_tell_calibration.html"
        out.write_text(_render_html(old, published, restyle, thresholds, today),
                       encoding="utf-8")
        report_path = str(out)
        log.info("[ai_tell_calibrate] 리포트: %s (old n=%d, published n=%d, restyle n=%d)",
                 out.name, old["n"], published["n"], restyle["n"])
    except OSError as exc:  # 리포트 쓰기 실패도 요약 반환은 살린다
        log.warning("[ai_tell_calibrate] 리포트 쓰기 실패: %s", exc)

    def _summary(group: dict) -> dict:
        # scores 리스트는 요약에서 제외(대용량) — 분포만 남긴다.
        return {k: v for k, v in group.items() if k != "scores"}

    return {
        "report_path": report_path,
        "old": _summary(old),
        "published": _summary(published),
        "restyle": restyle,
        "thresholds": thresholds,
    }


# ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import json as _json

    summary = run_calibration()
    path = summary.get("report_path")
    # scores 는 이미 요약에서 빠졌고, restyle.pairs 는 유용하므로 유지.
    print(_json.dumps(summary, ensure_ascii=False, indent=2))
    if path:
        print(f"\n리포트: {path}")
    else:
        print("\n리포트 쓰기 실패(로그 참조).", file=sys.stderr)
