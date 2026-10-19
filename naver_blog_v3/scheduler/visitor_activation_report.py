# scheduler/visitor_activation_report.py — 방문자 데이터 활성화 진단 + 클릭 가로챔 분석 (Phase 1 · L1)
"""방문자/글별 유입 데이터의 활성화 상태를 진단하고, 활성화되면 곧바로 쓸 수 있는
'고순위·저유입(AI요약 클릭 가로챔) 용의자' 분석을 한 리포트로 낸다.

- 데이터가 아직 없으면: 무엇을(②세션쿠키/③통계 인제스트) 어떻게 넣는지 정확히 안내.
- 데이터가 들어오면: rank_view_gap 이 곧바로 AEO 리프레시 최우선 타깃을 지목(폐루프 완성).

부수효과: 읽기 + 방문자 1회 조회 + reports/{date}_visitor_activation.md 쓰기. 발행·변경 없음. fail-open.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)
BASE_DIR = Path(__file__).resolve().parent.parent
try:  # 프로필별 리포트(2026-07-19 격리 감사) — 공유 폴더 혼입 방지
    from config import profile as _nbaprofile
    HARNESS_REPORTS = _nbaprofile.reports_dir()
except Exception:  # noqa: BLE001
    HARNESS_REPORTS = BASE_DIR.parent / "reports"

_STATUS_LABEL = {"active": "🟢 활성", "partial": "🟡 부분", "inactive": "🔴 미활성"}
_CHECK_LABEL = {
    "blog_id": "블로그 ID 설정",
    "session_cookie": "세션쿠키 존재",
    "visitor_fetch": "방문자 라이브 조회",
    "db_post_views": "글별 조회수 적재",
    "db_visitor_days": "방문자 일자 적재",
}
_CHECK_ICON = {"ok": "✅", "live": "✅", "absent": "❌", "zero": "⚠️0",
               "error": "⚠️오류", "skipped": "—"}


def gather() -> dict:
    from core import stats_fetcher as sf
    from core import performance_db as pdb
    diag = sf.diagnose_visitor_activation()
    gap = pdb.rank_view_gap(n=15)
    return {"diag": diag, "gap": gap}


def render_md(g: dict) -> str:
    diag, gap = g["diag"], g["gap"]
    o: list[str] = []
    o.append(f"# 방문자 데이터 활성화 진단 — {date.today().strftime('%Y-%m-%d')}\n")
    o.append(f"**종합 상태: {_STATUS_LABEL.get(diag.get('status'), diag.get('status'))}**\n")

    # 체크 표
    o.append("| 점검 | 상태 |")
    o.append("|---|---|")
    for k, label in _CHECK_LABEL.items():
        v = diag.get("checks", {}).get(k, "—")
        o.append(f"| {label} | {_CHECK_ICON.get(v, v)} `{v}` |")
    vs = diag.get("visitors_sample", {})
    o.append(f"\n- 라이브 방문자 표본: 오늘 {vs.get('today',0)} · 어제 {vs.get('yesterday',0)} · 누적 {vs.get('total',0)}")
    o.append(f"- performance_db: 조회수 있는 글 {diag.get('posts_with_views',0)}편 · 방문자 일자 {diag.get('visitor_days',0)}일 · 세션쿠키 {diag.get('cookie_count',0)}개")

    # 다음 행동(사용자 몫)
    o.append("\n## 다음 행동 (사용자 몫)")
    for i, a in enumerate(diag.get("next_actions", []), 1):
        o.append(f"{i}. {a}")

    # 클릭 가로챔 분석
    o.append("\n## 🎯 고순위·저유입 = AI요약 클릭 가로챔 용의자 (활성화되면 자동)")
    st = gap.get("status")
    if st == "needs_view_data":
        o.append(f"> **측정 대기** — 순위 데이터는 {gap.get('checked_ranked',0)}편(그중 TOP3 {gap.get('top_rank_posts',0)}편) "
                 "있으나 유입이 전부 0. 위 ②/③으로 방문자·글별 유입을 넣으면 이 분석이 곧바로 켜집니다.")
        o.append("> \n> **가설:** 검색 1위 비율은 93.5%(측정됨)인데 방문이 낮다면, "
                 "클릭이 검색결과가 아니라 **AI요약/스니펫에서 소진**되고 있을 가능성 — "
                 "이 경우 답을 더 자기완결적으로(AEO) 리프레시해 '요약에 인용되게' 만드는 게 최고 ROI.")
    elif st == "no_rank_data":
        o.append("> 순위 데이터 없음 — rank_tracker 먼저 가동(검색 API 스코프 필요).")
    elif st == "ok":
        sus = gap.get("suspects", [])
        o.append(f"동급 고순위 글 중앙 유입 {gap.get('peer_median')} · 임계 {gap.get('threshold')} 미만 = 용의자 {len(sus)}편.")
        o.append("이 글들이 **AEO 리프레시 1순위** — 답을 자기완결적으로 고쳐 AI요약에 인용되게.\n")
        if sus:
            o.append("| # | 순위 | 최근유입 | 검색어 | 제목 |")
            o.append("|---|---|---|---|---|")
            for i, s in enumerate(sus, 1):
                o.append(f"| {i} | {s['best_rank']}위 | {s['recent_views']} | {s['keyword'][:16]} | {(s['title'] or '')[:34]} |")
        else:
            o.append("- 용의자 없음 — 고순위 글의 유입이 고르게 나오는 중(건강).")

    o.append("\n---\n> 이 리포트는 읽기·1회 조회만 — 발행/변경 없음. "
             "용의자 글은 `post_refresher.preview_refresh_targets()` 와 교차 확인 권장(폐루프).")
    return "\n".join(o)


def run_visitor_activation_report() -> Path:
    g = gather()
    HARNESS_REPORTS.mkdir(parents=True, exist_ok=True)
    out = HARNESS_REPORTS / f"{date.today().strftime('%Y-%m-%d')}_visitor_activation.md"
    out.write_text(render_md(g), encoding="utf-8")
    log.info(f"[visitor_activation] 생성: {out.name} · 상태 {g['diag'].get('status')} · "
             f"gap {g['gap'].get('status')}")
    return out


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    print(run_visitor_activation_report())
