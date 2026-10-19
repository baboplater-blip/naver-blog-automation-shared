# scheduler/growth_review.py — 주간 성장 리뷰 (Phase 3 · L0/L6)
"""측정을 다음 주 행동으로 바꾸는 두뇌. 매주 실행해 KPI 이동을 읽고 배분을 제안한다.

집계 → 결정:
- 발행 계획: opportunity_score 기회 큐 상위(무개척 고수요)
- 구조 조정: 과분해 클러스터 통합·필러화, 고아 정리
- 리프레시: 순위 하락 + AEO 약체
- 실험: A-B 밴딧 다음 시도
- 한 줄 액션: growth_dashboard 의 최약 레버

부수효과: 읽기 + reports/{date}_growth_review.md 쓰기. 발행·변경 없음. fail-open.
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
NEXT_WEEK_TARGET = 15     # 주간 발행 권장 편수(기회 큐에서)


def _safe(fn, default):
    try:
        return fn()
    except (ImportError, OSError, ValueError, KeyError, TypeError) as e:
        log.debug(f"[growth_review] 집계 스킵: {e}")
        return default


def _real_demand_queue(top_n: int) -> list:
    """실측 유입 검색어(전용 글 없음) → 발행 큐 형태로 점수화. 없으면 빈 리스트(폴백 유도).

    performance_db.adjacent_keyword_opportunities() = '유입은 되는데 전용 글이 없는
    검색어'(최고 ROI). 이를 opportunity_score 로 점수화해 정적 큐와 동일한 스키마
    (keyword·category·score·volume·verdict)로 반환한다. 실측이 최우선 소스.

    유입 데이터가 아직 없으면 [] → 호출부가 정적 opportunity_score 큐로 폴백(fail-open).
    """
    from core import performance_db as pdb
    from core import opportunity_score as opp

    opportunities = pdb.adjacent_keyword_opportunities(n=top_n * 2)
    if not opportunities:
        return []

    # 유입량이 큰 검색어부터 전용 글 후보로 — score_candidates 가 점수·카테고리·판정 부여.
    candidates = [{"keyword": o["keyword"]} for o in opportunities if o.get("keyword")]
    scored = opp.score_candidates(candidates)
    # 잠식/무수요는 배제(정적 큐와 동일 기준) 하되, 실측 유입이 있으니 '보류(약)'까지 허용.
    publishable = [r for r in scored
                   if r.get("verdict") not in ("잠식위험(스킵)", "무수요(스킵)", "스킵")]
    visits_by_kw = {o["keyword"]: o.get("visits", 0) for o in opportunities}
    for r in publishable:
        r["real_visits"] = visits_by_kw.get(r["keyword"], 0)
        r["source"] = "measured"
    return publishable[:top_n]


def _build_publish_queue(top_n: int) -> tuple:
    """발행 큐 생성 — 실측 유입 검색어 1순위, opportunity_score 폴백.

    반환: (queue, source) — source ∈ {"measured", "static"}.
    실측 데이터가 비어 있으면 기존 정적 큐로 그대로 폴백(fail-open, 동작 불변).
    """
    real = _safe(lambda: _real_demand_queue(top_n), [])
    if real:
        return real, "measured"
    static = _safe(lambda: __import__("core.opportunity_score", fromlist=["rank_publish_queue"])
                   .rank_publish_queue(top_n=top_n, use_miner=False), [])
    return static, "static"


def gather() -> dict:
    from scheduler.growth_dashboard import collect_kpi, _weakest_lever

    kpi = _safe(collect_kpi, {})
    lever = _weakest_lever(kpi) if kpi else "측정 대기"

    queue, queue_source = _build_publish_queue(NEXT_WEEK_TARGET)

    def _consolidation():
        from core import topic_cluster as tc
        cache = tc._load_cache()
        clusters = cache["clusters"] if cache else tc.run_topic_map()["clusters"]
        norm = []
        for c in clusters:
            size = len(c.get("members", c.get("member_idx", [])))
            norm.append({"anchor": c.get("anchor", ""), "category": c.get("category", ""),
                         "size": size,
                         "pillar": (c.get("pillar", {}) or {}).get("title", "")})
        return [c for c in norm if c["size"] >= 25]
    consolidate = _safe(_consolidation, [])

    def _refresh_targets():
        from core import performance_db as pdb
        moves = pdb.rank_movement(days=21)
        return [m for m in moves if (m.get("delta") or 0) < 0][:10]
    refresh = _safe(_refresh_targets, [])

    ab = _safe(lambda: __import__("core.ab_test", fromlist=["recommend"]).recommend(), {})

    # Phase 4 — 발행 큐를 일 단위로 분산(배치 덤프 방지)
    cadence = _safe(lambda: __import__("core.cadence_planner", fromlist=["plan_cadence"])
                    .plan_cadence([{"keyword": r["keyword"], "category": r.get("category", ""),
                                    "score": r["score"]} for r in queue]), {})

    return {"kpi": kpi, "lever": lever, "queue": queue, "queue_source": queue_source,
            "cadence": cadence, "consolidate": consolidate, "refresh": refresh, "ab": ab}


def render_md(g: dict) -> str:
    today = date.today().strftime("%Y-%m-%d")
    kpi = g["kpi"] or {}
    o = [
        f"# 주간 성장 리뷰 — {today}\n",
        f"> 일방문자 목표 {kpi.get('goal', 10000):,} · 현재 {kpi.get('today_visitors', 0):,} "
        f"· 측정 {'가동' if (kpi.get('posts_with_views') or kpi.get('ranked')) else '대기'}\n",
        "## 0. 이번 주 한 줄 액션\n",
        f"{g['lever']}\n",
        "## 1. KPI 스냅샷\n",
        f"- 인덱싱 {kpi.get('indexed',0):,}편 · 순위추적 {kpi.get('ranked',0)}키워드 "
        f"· 상위노출율 {kpi.get('expose_rate',0)}% · 편당유입 {kpi.get('per_post_visits',0)}",
        f"- AEO 합격 {kpi.get('aeo_pass',0)}/{kpi.get('aeo_total',0)} ({kpi.get('aeo_rate',0)}%) "
        f"· 클러스터 {kpi.get('clusters',0)} · 과분해 {kpi.get('over_concentration',0)}",
    ]

    cad = g.get("cadence") or {}
    src = g.get("queue_source", "static")
    src_note = ("🎯 실측 유입 검색어 우선(전용 글 없는 고ROI)" if src == "measured"
                else "opportunity_score 기회 큐(실측 유입 대기 — 폴백)")
    o.append(f"\n## 2. 다음 주 발행 계획 — 일 단위 분산 ({cad.get('days', 0)}일)\n")
    o.append(f"> 소스: {src_note}\n")
    if cad.get("schedule"):
        o.append(f"> 배치 덤프 대신 **하루 평균 {cad['summary'].get('avg_per_day', 0)}편** 분산 "
                 f"(총 {cad['summary'].get('total', 0)}편). C-Rank 신선도 유지. "
                 f"**과분해 3토픽엔 새 글 자제** — 미개척 고수요 우선.\n")
        o.append("| 날짜 | 시각 | 카테고리 | 키워드 | 점수 |\n|---|---|---|---|---|")
        for s in cad["schedule"][:21]:
            o.append(f"| {s['date']} | {s['time']} | {s['category']} | {s['keyword']} | {s.get('score',0):.0f} |")
    elif g["queue"]:
        o.append("| 점수 | 키워드 | 볼륨 | 판정 |\n|---|---|---|---|")
        for r in g["queue"]:
            o.append(f"| {r['score']:.0f} | {r['keyword']} | {r.get('volume',0):,} | {r['verdict']} |")
    else:
        o.append("_기회 큐 비어있음 — question_seeds.json / 네트워크 확인._")

    o.append("\n## 3. 구조 조정 (과분해 통합·필러화)\n")
    if g["consolidate"]:
        for c in g["consolidate"]:
            o.append(f"- **{c['anchor']}** ({c['category']}, {c['size']}편) → 필러 「{(c['pillar'] or '')[:30]}」로 "
                     f"권위 집중, 나머지 내부링크 연결 (브리프: reports/*_pillar_restructure_brief.md)")
    else:
        o.append("과분해 없음 — 분포 양호.")

    o.append("\n## 4. 리프레시 대상 (순위 하락)\n")
    if g["refresh"]:
        o.append("| 키워드 | 현재순위 | 변동 |\n|---|---|---|")
        for m in g["refresh"]:
            o.append(f"| {m.get('keyword','')} | {m.get('rank','—')} | ▼{-(m.get('delta') or 0)} |")
        o.append("\n> post_refresher.run_refresh_batch 로 AEO 재생성.")
    else:
        o.append("_순위 하락 데이터 없음 — rank_tracker 축적 후 표시._")

    o.append("\n## 5. 실험 지침 (A-B 밴딧 다음 시도)\n")
    if g["ab"]:
        o.append("| 실험 | 현재 최고 | 다음 시도 | 표본(신뢰) |\n|---|---|---|---|")
        for name, r in g["ab"].items():
            o.append(f"| {r['description']} | {r['current_best'] or '—'} | "
                     f"{r['next_try'] or '—'} | {r['samples']}({r['confidence']}) |")
        o.append("\n> 다음 발행 시 '다음 시도' 변형 적용 → 실측 후 UCB 재평가.")
    else:
        o.append("_실험 데이터 없음._")

    o.append("\n---\n> 관제탑(시각): reports/*_growth_dashboard.html · "
             "이 리뷰는 매주 실행 권장(register 가능).")
    return "\n".join(o)


def run_growth_review() -> Path:
    g = gather()
    HARNESS_REPORTS.mkdir(parents=True, exist_ok=True)
    out = HARNESS_REPORTS / f"{date.today().strftime('%Y-%m-%d')}_growth_review.md"
    out.write_text(render_md(g), encoding="utf-8")
    log.info(f"[growth_review] 생성: {out.name} · 발행계획 {len(g['queue'])} · "
             f"통합 {len(g['consolidate'])} · 리프레시 {len(g['refresh'])}")
    return out


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    run_growth_review()
