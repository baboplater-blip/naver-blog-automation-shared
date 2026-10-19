"""scheduler/weekly_report.py — 주간 통계 HTML 리포트.

매주 일요일 23:00 Windows 작업 스케줄러가 호출하면 직전 7일(월~일)의 발행
활동을 집계해 `{harness_root}/reports/weekly_{YYYY-W##}.html` 로 저장한다.

집계 소스:
- naver_blog_v3/logs/{date}.json   — 일별 발행 로그 (시도·성공·실패·예약시각)
- runs/{slug}/post_data.json       — 생성된 글의 카테고리·제목
- briefs/{date}_*.yaml              — 라우팅 브리프 (참조용, 비필수)

리포트 섹션:
1. 요약 카드 — 발행 시도·성공·실패·성공률·신규 글 수
2. 카테고리별 발행 분포 (테이블)
3. 일별 발행 곡선 (간단 막대)
4. 실패 로그 상세 (있을 때만)
5. 다음 주 권장 (성공률 낮은 카테고리 우선)
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

_PKG_ROOT = Path(__file__).resolve().parent.parent  # naver_blog_v3/
_HARNESS_ROOT = _PKG_ROOT.parent
# ⚠️ sys.path 삽입은 config import **이전**이어야 한다 — 종전엔 뒤에 있어
# schtasks 직접 실행이 ImportError 로 죽던 잠복 버그(2026-07-19 격리 감사에서 발견).
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
if __name__ == "__main__":  # schtasks 직접 실행 — 임포트 전 프로필 고정
    from config.profile_boot import pin_profile_from_argv
    pin_profile_from_argv()
from config import profile as _nbaprofile
_DATA_HARNESS = _nbaprofile.harness_data_root()


if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

log = logging.getLogger(__name__)

# 일별 발행로그는 프로필별(2026-07-19 격리 감사) — 레거시 고정이면 named 프로필
# 주간 실적이 default 로그로 오집계된다. auto_scheduler 가 쓰는 곳과 일치시킨다.
LOGS_DIR = _nbaprofile.logs_dir()
RUNS_DIR = _DATA_HARNESS / "runs"
REPORTS_DIR = _DATA_HARNESS / "reports"

examplebrand_CATEGORIES = (
    "청년", "복지", "교육", "취업", "주거", "의료",
    "바우처", "부동산", "절세", "금융", "노후", "디지털",
    "환경",
)


def _iso_week_label(d: date) -> str:
    iso_year, iso_week, _ = d.isocalendar()
    return f"{iso_year}-W{iso_week:02d}"


def _date_range(end: date, days: int) -> list[date]:
    return [end - timedelta(days=i) for i in range(days - 1, -1, -1)]


def _read_daily_log(day: date) -> list[dict]:
    path = LOGS_DIR / f"{day.isoformat()}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
        return []
    except (OSError, json.JSONDecodeError) as exc:
        log.debug(f"[weekly_report] {path} 읽기 실패: {exc}")
        return []


def _read_post_metadata() -> dict[str, dict]:
    """runs/{slug}/post_data.json 을 모두 읽어 title → metadata dict 매핑.

    제목으로 카테고리 lookup 용 (로그에 카테고리가 없어서).
    """
    out: dict[str, dict] = {}
    if not RUNS_DIR.exists():
        return out
    try:
        children = list(RUNS_DIR.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_dir():
            continue
        post = child / "post_data.json"
        if not post.exists():
            continue
        try:
            payload = json.loads(post.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        title = payload.get("title") or ""
        if title:
            out[title.strip()] = {
                "category": payload.get("category"),
                "slug": child.name,
            }
    return out


def _detect_category_for_title(title: str, lookup: dict) -> str:
    """title 매칭으로 카테고리 추정. 못 찾으면 '?'."""
    # 정확 매치 우선
    meta = lookup.get(title.strip())
    if meta and meta.get("category") in examplebrand_CATEGORIES:
        return meta["category"]
    # 일부 매치 (글 제목 앞에 날짜 prefix 가 다를 수 있음)
    for key, meta in lookup.items():
        if key in title or title in key:
            cat = meta.get("category")
            if cat in examplebrand_CATEGORIES:
                return cat
    # 폴백: 키워드 기반 (가벼운 detect)
    try:
        from core.content_generator import detect_category
        guess = detect_category(title)
        if guess in examplebrand_CATEGORIES:
            return guess
    except (ImportError, AttributeError, TypeError, ValueError):
        pass
    return "?"


def aggregate_week(end: date, *, days: int = 7) -> dict:
    """end 까지 days 일간의 발행 활동을 집계.

    Returns:
        {
            "range": (start_date, end_date),
            "totals": {"attempts": N, "success": M, "fail": K, "success_rate": float},
            "by_category": Counter({카테고리: 성공 건수}),
            "by_day": {date: {"attempts": N, "success": M, "fail": K}},
            "failures": [{"date": d, "time": t, "title": s, "detail": s}, ...],
        }
    """
    days_list = _date_range(end, days)
    lookup = _read_post_metadata()

    totals_attempts = 0
    totals_success = 0
    totals_fail = 0
    by_category: Counter = Counter()
    by_day: dict = {}
    failures: list[dict] = []

    for day in days_list:
        rows = _read_daily_log(day)
        day_attempts = len(rows)
        day_success = sum(1 for r in rows if r.get("success") is True)
        day_fail = day_attempts - day_success
        by_day[day] = {
            "attempts": day_attempts,
            "success": day_success,
            "fail": day_fail,
        }
        totals_attempts += day_attempts
        totals_success += day_success
        totals_fail += day_fail

        for r in rows:
            title = (r.get("title") or "").strip()
            if r.get("success") is True and title:
                cat = _detect_category_for_title(title, lookup)
                by_category[cat] += 1
            elif r.get("success") is False:
                failures.append({
                    "date": day.isoformat(),
                    "time": r.get("time") or "",
                    "title": title or "(제목 없음)",
                    "detail": (r.get("detail") or "")[:200],
                })

    success_rate = (totals_success / totals_attempts * 100) if totals_attempts else 0.0

    # 2026-05-25 — D.I.A.+ / AEO 점수 추적 (qa.json 에 기록된 경우만 집계)
    dia_scores: list = []
    aeo_scores: list = []
    dia_by_category: dict = {}
    aeo_by_category: dict = {}
    if RUNS_DIR.exists():
        cutoff = days_list[0]
        for child in RUNS_DIR.iterdir():
            if not child.is_dir():
                continue
            qa = child / "qa.json"
            if not qa.exists():
                continue
            try:
                payload = json.loads(qa.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            recorded = payload.get("recorded_at") or ""
            try:
                rec_date = date.fromisoformat(recorded[:10])
            except (ValueError, TypeError):
                continue
            if rec_date < cutoff or rec_date > end:
                continue
            qa_block = payload.get("qa") or {}
            d_s = qa_block.get("dia_score")
            a_s = qa_block.get("aeo_score")
            cat = payload.get("category") or "default"
            if isinstance(d_s, (int, float)):
                dia_scores.append(d_s)
                dia_by_category.setdefault(cat, []).append(d_s)
            if isinstance(a_s, (int, float)):
                aeo_scores.append(a_s)
                aeo_by_category.setdefault(cat, []).append(a_s)

    def _avg(seq: list) -> float:
        return round(sum(seq) / len(seq), 1) if seq else 0.0

    scores_summary = {
        "dia_count": len(dia_scores),
        "aeo_count": len(aeo_scores),
        "dia_avg": _avg(dia_scores),
        "aeo_avg": _avg(aeo_scores),
        "dia_by_category": {k: _avg(v) for k, v in dia_by_category.items()},
        "aeo_by_category": {k: _avg(v) for k, v in aeo_by_category.items()},
    }

    # 2026-07-04 — Phase 1 유입 결과(성과) 집계. performance_db 우선.
    traffic = _collect_traffic(days=days)

    return {
        "range": (days_list[0], days_list[-1]),
        "totals": {
            "attempts": totals_attempts,
            "success": totals_success,
            "fail": totals_fail,
            "success_rate": round(success_rate, 1),
        },
        "by_category": by_category,
        "by_day": by_day,
        "failures": failures,
        "scores": scores_summary,
        "traffic": traffic,
    }


def _collect_traffic(*, days: int = 7) -> dict:
    """performance_db 에서 유입 성과(방문자·유입글·검색어·순위·기회) 집계. fail-open."""
    try:
        from core import performance_db as pdb
    except ImportError:
        return {"available": False}
    try:
        data = pdb.load()
        if not data.get("posts"):
            return {"available": False}
        summary = pdb.get_summary(data=data)
        vis = pdb.visitor_series(days=days, data=data)
        moves = pdb.rank_movement(days=max(days * 2, 14), data=data)
        risers = [m for m in moves if m.get("delta", 0) > 0][:8]
        fallers = [m for m in moves if m.get("delta", 0) < 0]
        fallers.sort(key=lambda m: m["delta"])
        top_ranked = [m for m in moves if m.get("rank")][:10]
        weekly_visits = sum(v["count"] for v in vis[-days:]) if vis else 0
        return {
            "available": True,
            "posts_with_views": summary["posts_with_views"],
            "weekly_visits_from_posts": summary["weekly_visits_from_posts"],
            "keywords_ranked": summary["keywords_ranked"],
            "keywords_top10": summary["keywords_top10"],
            "visitor_series": vis,
            "weekly_visitors": weekly_visits,
            "top_posts": summary["top_posts"][:10],
            "top_referrals": summary["top_referrals"][:12],
            "opportunities": summary["opportunities"][:12],
            "top_ranked": top_ranked,
            "risers": risers,
            "fallers": fallers[:6],
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        log.debug(f"[weekly_report] 유입 집계 실패: {exc}")
        return {"available": False}


def _bar_svg(values: list[int], width: int = 560, height: int = 120) -> str:
    """간단 SVG 막대 차트. 라벨 없이 비교용으로만."""
    if not values:
        return ""
    n = len(values)
    bw = width / n
    vmax = max(values + [1])
    bars = []
    for i, v in enumerate(values):
        h = int((v / vmax) * (height - 16))
        x = int(i * bw + 4)
        y = height - h - 2
        bars.append(
            f'<rect x="{x}" y="{y}" width="{int(bw - 6)}" height="{h}" '
            f'fill="#5eead4" rx="3" />'
            f'<text x="{x + int((bw - 6) / 2)}" y="{height - 4}" '
            f'fill="#8b95a5" font-size="10" text-anchor="middle">{v}</text>'
        )
    return (
        f'<svg viewBox="0 0 {width} {height}" '
        f'style="width:100%;max-width:{width}px;background:#161a22;border-radius:8px;">'
        + "".join(bars) + "</svg>"
    )


def render_html(agg: dict) -> str:
    start, end = agg["range"]
    totals = agg["totals"]
    by_category = agg["by_category"]
    by_day = agg["by_day"]
    failures = agg["failures"]
    traffic = agg.get("traffic", {})

    week_label = _iso_week_label(end)

    cat_rows = "".join(
        f'<tr><td>{html.escape(cat)}</td><td style="text-align:right">{n}</td></tr>'
        for cat, n in by_category.most_common()
    ) or '<tr><td colspan="2" style="color:#8b95a5">발행 없음</td></tr>'

    day_rows = "".join(
        f'<tr><td>{d.isoformat()} ({"월화수목금토일"[d.weekday()]})</td>'
        f'<td style="text-align:right">{stats["attempts"]}</td>'
        f'<td style="text-align:right">{stats["success"]}</td>'
        f'<td style="text-align:right;color:#ff8fa3">{stats["fail"]}</td></tr>'
        for d, stats in by_day.items()
    )
    day_values = [stats["success"] for stats in by_day.values()]

    fail_rows = "".join(
        f'<tr><td>{html.escape(f["date"])} {html.escape(f["time"])}</td>'
        f'<td>{html.escape(f["title"])}</td>'
        f'<td style="color:#ff8fa3">{html.escape(f["detail"])}</td></tr>'
        for f in failures
    ) if failures else ""
    fail_section = (
        f'<h2>실패 상세 ({len(failures)} 건)</h2>'
        f'<table>{fail_rows}</table>'
    ) if failures else '<p style="color:#8b95a5">실패 없음 — 양호.</p>'

    # 다음 주 권장 — 카테고리별 성공 0 인 항목 우선
    silent_cats = [c for c in examplebrand_CATEGORIES if by_category.get(c, 0) == 0]
    if silent_cats:
        reco = (
            f"<p>이번 주 발행 0편 카테고리: "
            f"<b>{', '.join(silent_cats)}</b> — 다음 주 우선 보강 권장.</p>"
        )
    else:
        reco = "<p>모든 카테고리에서 발행 진행 — 분포 양호.</p>"

    # D.I.A.+ / AEO 점수 섹션 — qa.json 에 점수가 기록된 글이 있을 때만 표시
    scores = agg.get("scores", {})
    if scores.get("dia_count") or scores.get("aeo_count"):
        cat_score_rows = []
        cats = sorted(
            set(list(scores.get("dia_by_category", {}).keys())
                + list(scores.get("aeo_by_category", {}).keys()))
        )
        for cat in cats:
            d_avg = scores["dia_by_category"].get(cat, "—")
            a_avg = scores["aeo_by_category"].get(cat, "—")
            cat_score_rows.append(
                f'<tr><td>{html.escape(cat)}</td>'
                f'<td style="text-align:right">{d_avg}</td>'
                f'<td style="text-align:right">{a_avg}</td></tr>'
            )
        cat_score_html = "".join(cat_score_rows) or '<tr><td colspan="3" style="color:#8b95a5">카테고리 데이터 없음</td></tr>'
        scores_section = f"""
  <h2>D.I.A.+ / AEO 점수 추적 ({scores['dia_count']}편)</h2>
  <p style="color:#8b95a5; font-size:13px">자동 보완 활성 카테고리에서 발행된 글의 D.I.A.+ (검색 가산) / AEO (AI 답변 인용) 평균 점수. 70+ 가 PASS 라인.</p>
  <div class="stat-grid">
    <div class="stat"><div class="label">D.I.A.+ 평균</div><div class="value">{scores['dia_avg']}</div><div class="sub">{scores['dia_count']}편</div></div>
    <div class="stat"><div class="label">AEO 평균</div><div class="value">{scores['aeo_avg']}</div><div class="sub">{scores['aeo_count']}편</div></div>
  </div>
  <table>
    <tr><th>카테고리</th><th style="text-align:right">D.I.A.+ 평균</th><th style="text-align:right">AEO 평균</th></tr>
    {cat_score_html}
  </table>
"""
    else:
        scores_section = (
            '<h2>D.I.A.+ / AEO 점수 추적</h2>'
            '<p style="color:#8b95a5">이번 주 자동 보완 활성 카테고리에서 발행된 글이 없습니다 — '
            'settings.DIA_AEO_AUTOFIX_BY_CATEGORY 확인 후 디지털 카테고리부터 활성화 권장.</p>'
        )

    traffic_section = _render_traffic(traffic)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<title>주간 리포트 {week_label}</title>
<style>
  body {{ background:#0e1116; color:#e6edf3; font-family:'Pretendard',system-ui,sans-serif;
         margin:0; padding:0 0 60px 0; }}
  .container {{ max-width:980px; margin:0 auto; padding:28px 24px; }}
  h1 {{ font-size:24px; margin:0 0 6px; }}
  h2 {{ font-size:17px; margin:28px 0 12px; color:#5eead4; }}
  .sub {{ color:#8b95a5; font-size:13px; }}
  .stat-grid {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:18px 0; }}
  .stat {{ background:#161a22; border:1px solid #2a3145; border-radius:10px;
          padding:12px 16px; }}
  .stat .label {{ font-size:11px; color:#8b95a5; }}
  .stat .value {{ font-size:22px; font-weight:700; margin-top:2px; color:#5eead4; }}
  .stat.warn .value {{ color:#ff8fa3; }}
  table {{ width:100%; border-collapse:collapse; background:#161a22;
          border:1px solid #2a3145; border-radius:8px; overflow:hidden; }}
  th, td {{ padding:8px 12px; border-bottom:1px solid #2a3145; font-size:13px; }}
  th {{ background:#1c2230; color:#8b95a5; text-align:left; font-weight:600; font-size:12px; }}
  tr:last-child td {{ border-bottom:none; }}
</style>
</head>
<body>
<div class="container">
  <h1>주간 리포트 <span class="sub">{week_label} · {start.isoformat()} ~ {end.isoformat()}</span></h1>
  <p class="sub">naver-blog-automation 자동 생성 — 발행 활동 7일 집계</p>

  <div class="stat-grid">
    <div class="stat"><div class="label">발행 시도</div><div class="value">{totals['attempts']}</div></div>
    <div class="stat"><div class="label">성공</div><div class="value">{totals['success']}</div></div>
    <div class="stat {'warn' if totals['fail'] else ''}"><div class="label">실패</div><div class="value">{totals['fail']}</div></div>
    <div class="stat"><div class="label">성공률</div><div class="value">{totals['success_rate']}%</div></div>
  </div>

  {traffic_section}

  <h2>일별 발행 곡선</h2>
  {_bar_svg(day_values)}
  <table style="margin-top:14px">
    <tr><th>날짜</th><th style="text-align:right">시도</th>
        <th style="text-align:right">성공</th><th style="text-align:right">실패</th></tr>
    {day_rows}
  </table>

  <h2>카테고리별 발행 분포</h2>
  <table>
    <tr><th>카테고리</th><th style="text-align:right">성공 발행 수</th></tr>
    {cat_rows}
  </table>

  {scores_section}

  <h2>다음 주 권장</h2>
  {reco}

  {fail_section}
</div>
</body>
</html>
"""


def _render_traffic(t: dict) -> str:
    """유입 결과(성과) 섹션 HTML. performance_db 데이터 없으면 측정 시작 안내."""
    if not t or not t.get("available"):
        return (
            '<h2>📈 유입 결과 (실측)</h2>'
            '<p style="color:#8b95a5">아직 유입 실측 데이터가 없습니다 — Phase 1 측정 루프를 돌리면 채워집니다.<br>'
            '① <b>순위</b>: <code>python -m core.rank_tracker</code> (네이버 검색 API 자격증명 필요) · '
            '② <b>방문자</b>: 네이버 로그인(세션쿠키) 후 <code>collect_stats()</code> · '
            '③ <b>글별 조회수·유입검색어</b>: 네이버 블로그 통계 내보내기(CSV/XLSX)를 '
            '<code>stats_fetcher.ingest_stats_export(경로)</code> 로 적재.</p>'
        )

    def _tbl(rows, cols, empty="데이터 없음"):
        if not rows:
            return f'<tr><td colspan="{len(cols)}" style="color:#8b95a5">{empty}</td></tr>'
        out = []
        for r in rows:
            tds = "".join(
                f'<td style="{st}">{html.escape(str(r.get(k, "")))}</td>' for k, st in cols)
            out.append(f"<tr>{tds}</tr>")
        return "".join(out)

    vis_vals = [v["count"] for v in t.get("visitor_series", [])]
    top_posts = "".join(
        f'<tr><td>{html.escape((p.get("title") or "")[:44])}</td>'
        f'<td style="text-align:right;color:#5eead4">{p.get("views", 0)}</td>'
        f'<td style="color:#8b95a5">{html.escape(p.get("category", ""))}</td></tr>'
        for p in t.get("top_posts", [])
    ) or '<tr><td colspan="3" style="color:#8b95a5">조회수 데이터 없음 — 통계 내보내기 인제스트 필요</td></tr>'

    top_ref = "".join(
        f'<tr><td>{html.escape(r.get("keyword", ""))}</td>'
        f'<td style="text-align:right;color:#5eead4">{r.get("visits", 0)}</td></tr>'
        for r in t.get("top_referrals", [])
    ) or '<tr><td colspan="2" style="color:#8b95a5">유입 검색어 데이터 없음</td></tr>'

    opp = "".join(
        f'<tr><td>{html.escape(r.get("keyword", ""))}</td>'
        f'<td style="text-align:right">{r.get("visits", 0)}</td></tr>'
        for r in t.get("opportunities", [])
    ) or '<tr><td colspan="2" style="color:#8b95a5">기회 키워드 없음 (유입 검색어 축적 후 표시)</td></tr>'

    ranked = "".join(
        f'<tr><td>{html.escape(m.get("keyword", ""))}</td>'
        f'<td style="text-align:right">{m.get("rank", "—")}</td>'
        f'<td style="text-align:right;color:{"#5eead4" if (m.get("delta") or 0) > 0 else "#ff8fa3" if (m.get("delta") or 0) < 0 else "#8b95a5"}">'
        f'{("▲" + str(m["delta"])) if (m.get("delta") or 0) > 0 else ("▼" + str(-m["delta"])) if (m.get("delta") or 0) < 0 else "—"}</td></tr>'
        for m in t.get("top_ranked", [])
    ) or '<tr><td colspan="3" style="color:#8b95a5">순위 데이터 없음 — rank_tracker 실행 필요</td></tr>'

    return f"""
  <h2>📈 유입 결과 (실측) — Phase 1</h2>
  <div class="stat-grid">
    <div class="stat"><div class="label">주간 방문자(글 유입)</div><div class="value">{t.get('weekly_visits_from_posts', 0)}</div></div>
    <div class="stat"><div class="label">조회수 집계된 글</div><div class="value">{t.get('posts_with_views', 0)}</div></div>
    <div class="stat"><div class="label">순위 추적 키워드</div><div class="value">{t.get('keywords_ranked', 0)}</div></div>
    <div class="stat"><div class="label">TOP10 노출</div><div class="value">{t.get('keywords_top10', 0)}</div></div>
  </div>
  <p class="sub">방문자 추이(최근):</p>
  {_bar_svg(vis_vals)}

  <h2>유입 TOP 글</h2>
  <table><tr><th>제목</th><th style="text-align:right">조회수</th><th>카테고리</th></tr>{top_posts}</table>

  <h2>유입 검색어 TOP <span class="sub">(실제 수요의 진실원본)</span></h2>
  <table><tr><th>검색어</th><th style="text-align:right">유입</th></tr>{top_ref}</table>

  <h2>🎯 기회 키워드 <span class="sub">(유입되는데 전용 글 없음 → Phase 2 발행 1순위)</span></h2>
  <table><tr><th>검색어</th><th style="text-align:right">유입</th></tr>{opp}</table>

  <h2>순위 현황 <span class="sub">(▲상승 ▼하락)</span></h2>
  <table><tr><th>키워드</th><th style="text-align:right">현재 순위</th><th style="text-align:right">변동</th></tr>{ranked}</table>
"""


def run_weekly_report(*, end: date | None = None) -> Path:
    """직전 7일 집계 HTML 을 reports/weekly_{ISO주차}.html 로 저장."""
    end = end or (datetime.now().date())
    agg = aggregate_week(end, days=7)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORTS_DIR / f"weekly_{_iso_week_label(end)}.html"
    out_path.write_text(render_html(agg), encoding="utf-8")
    log.info(f"[weekly_report] 저장 — {out_path}")
    return out_path


def _setup_cli_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="주간 통계 HTML 리포트")
    parser.add_argument("--end", help="집계 종료일 (YYYY-MM-DD), 기본 오늘")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    if not args.quiet:
        _setup_cli_logging()

    end = date.fromisoformat(args.end) if args.end else datetime.now().date()
    out = run_weekly_report(end=end)
    if not args.quiet:
        print(f"리포트: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
