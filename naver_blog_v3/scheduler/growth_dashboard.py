# scheduler/growth_dashboard.py — 성장 관제탑 (Phase 3 · L0)
"""일방문자 1만을 KPI 트리로 분해해 현재 위치와 다음 레버를 한 화면에 보여준다.

    일방문자 = 검색유입 + AI요약유입 + 추천유입 + 이웃유입
    검색유입 = 인덱싱 편수 × 상위노출율 × 편당 검색유입

각 가지의 현재 수치를 Phase 1(performance_db)·AEO·토픽·기회 데이터에서 집계하고,
가장 약한 가지(=다음에 밀어야 할 레버)를 지목한다. 측정 전이면 '측정 대기'로 표기.

부수효과: 읽기 + reports/{date}_growth_dashboard.html 쓰기. fail-open.
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
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

# 프로필별 리포트(2026-07-19 격리 감사) — growth 탭이 읽는 경로와 일치.
HARNESS_REPORTS = _nbaprofile.reports_dir()
PREVIEWS_DIR = _DATA_DIR / "previews"
GOAL_VISITORS = 10000

# 상위노출율·균형 판정을 신뢰하려면 검색량 보유(measured) 키워드 표본이 최소 이 정도는
# 있어야 한다. 검색량 미상(#7 unmeasured)이 다수인데 measured 표본이 이 미만이면
# "100% 노출·균형 양호"는 통계적 착시 → 표본 확대를 최우선 레버로 지목한다.
MIN_RANK_SAMPLE = 20


def _pct(a, b) -> float:
    return round(a / b * 100, 1) if b else 0.0


def collect_kpi() -> dict:
    """KPI 트리 수치 집계. 모든 소스 fail-open."""
    k = {"goal": GOAL_VISITORS, "today_visitors": 0, "weekly_visits_from_posts": 0,
         "indexed": 0, "ranked": 0, "top10": 0, "unmeasured": 0, "posts_with_views": 0,
         "expose_rate": 0.0, "per_post_visits": 0.0,
         "aeo_pass": 0, "aeo_total": 0, "aeo_rate": 0.0,
         "eng_pass": 0, "eng_rate": 0.0,
         "clusters": 0, "over_concentration": 0, "orphans": 0,
         "opportunity_queue": 0, "ab": {}}

    try:
        from core import performance_db as pdb
        data = pdb.load()
        s = pdb.get_summary(data=data)
        k["indexed"] = s["total_posts"]
        k["ranked"] = s["keywords_ranked"]
        k["top10"] = s["keywords_top10"]
        # #7: 검색량 미상(volume<임계)이라 상위노출율 표본에서 빠진 키워드 편수.
        # 화면에서 분모로 병기해 "표본 N"임을 드러낸다(fail-open — 없으면 0).
        k["unmeasured"] = s.get("keywords_unmeasured", 0)
        k["posts_with_views"] = s["posts_with_views"]
        k["weekly_visits_from_posts"] = s["weekly_visits_from_posts"]
        vis = pdb.visitor_series(days=1, data=data)
        k["today_visitors"] = vis[-1]["count"] if vis else 0
        k["expose_rate"] = _pct(s["keywords_top10"], s["keywords_ranked"])
        k["per_post_visits"] = round(
            s["weekly_visits_from_posts"] / s["posts_with_views"], 1) if s["posts_with_views"] else 0.0
    except (ImportError, OSError, ValueError, KeyError):
        pass

    # AEO(AI요약) + Engagement(체류) 합격률 — previews 표본 동시 채점
    try:
        from core import aeo_scorer
        try:
            from core import engagement_scorer as eng
        except ImportError:
            eng = None
        files = sorted(PREVIEWS_DIR.glob("*.html"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:40]
        passed = eng_pass = 0
        for f in files:
            html = f.read_text(encoding="utf-8", errors="ignore")
            if aeo_scorer.score(html).passed():
                passed += 1
            if eng and eng.score(html).passed():
                eng_pass += 1
        k["aeo_total"] = len(files)
        k["aeo_pass"] = passed
        k["aeo_rate"] = _pct(passed, len(files))
        k["eng_pass"] = eng_pass
        k["eng_rate"] = _pct(eng_pass, len(files))
    except (ImportError, OSError, ValueError):
        pass

    # 토픽 구조
    try:
        from core import topic_cluster as tc
        cache = tc._load_cache()
        if cache:
            clusters = cache["clusters"]
            k["clusters"] = len(clusters)
            k["over_concentration"] = sum(1 for c in clusters if len(c.get("members", [])) >= 25)
    except (ImportError, OSError, KeyError):
        pass

    # 발행 기회 큐
    try:
        from core.opportunity_score import rank_publish_queue
        k["opportunity_queue"] = len(rank_publish_queue(top_n=50, use_miner=False))
    except (ImportError, OSError, ValueError):
        pass

    # A-B 실험
    try:
        from core import ab_test
        k["ab"] = ab_test.recommend()
    except (ImportError, OSError):
        pass

    return k


def _weakest_lever(k: dict) -> str:
    """다음에 밀어야 할 가지 진단."""
    ranked = int(k.get("ranked", 0) or 0)
    unmeasured = int(k.get("unmeasured", 0) or 0)
    posts_with_views = int(k.get("posts_with_views", 0) or 0)
    expose_rate = float(k.get("expose_rate", 0) or 0)
    aeo_rate = float(k.get("aeo_rate", 0) or 0)
    per_post_visits = float(k.get("per_post_visits", 0) or 0)

    if posts_with_views == 0 and ranked == 0:
        return ("🔴 **측정 루프 미가동** — 최우선. 순위·유입 데이터가 0이면 나머지는 모두 추정치. "
                "rank_tracker(자격증명) + 통계 인제스트부터.")
    # 검색량 보유 표본이 너무 적으면 상위노출율(=100%라도)이 통계적 착시 → 표본 확대가 먼저.
    # expose_rate 게이트보다 앞서 판정해 '표본 7 위 균형 양호' 허위 자신감을 차단한다.
    if 0 < ranked < MIN_RANK_SAMPLE:
        total = ranked + unmeasured
        return (f"🟠 **측정 표본 부족(측정 {ranked}/전체 {total})** — 검색량 확인 키워드가 {ranked}개뿐이라 "
                "상위노출율은 통계적 착시. 검색 API 순위추적 + 검색량 인제스트 확대가 최우선.")
    if expose_rate < 20:
        return ("🟠 **상위노출율 저조** — 인덱싱은 됐으나 상위권 진입이 적음. 필러화(권위 집중) + "
                "AEO·리프레시로 순위 상승 집중.")
    if aeo_rate < 50:
        return ("🟠 **AI요약 준비 부족** — AEO 합격률이 낮음. answer-first·FAQ 구조 강화 + 약체 리프레시.")
    if per_post_visits < 20:
        return ("🟡 **편당 유입 저조** — 상위엔 있으나 편당 방문이 적음. 고수요 키워드로 발행 이동(기회 큐).")
    return "🟢 균형 양호 — 발행량·신선도 유지하며 스케일업."


def render_html(k: dict) -> str:
    import html as _h
    today = date.today().strftime("%Y-%m-%d")
    progress = _pct(k["today_visitors"], k["goal"])

    def node(label, value, sub="", warn=False):
        color = "#ff8fa3" if warn else "#5eead4"
        return (f'<div class="node"><div class="nlabel">{_h.escape(label)}</div>'
                f'<div class="nval" style="color:{color}">{value}</div>'
                f'<div class="nsub">{_h.escape(sub)}</div></div>')

    measured = k["posts_with_views"] > 0 or k["ranked"] > 0
    meas_tag = "" if measured else '<span class="pending">측정 대기</span>'

    ab_rows = ""
    for name, r in (k.get("ab") or {}).items():
        ab_rows += (f'<tr><td>{_h.escape(r["description"])}</td>'
                    f'<td>{_h.escape(str(r["current_best"] or "—"))}</td>'
                    f'<td>{_h.escape(str(r["next_try"] or "—"))}</td>'
                    f'<td>{r["samples"]} ({_h.escape(r["confidence"])})</td></tr>')
    ab_rows = ab_rows or '<tr><td colspan="4" style="color:#8b95a5">실험 데이터 없음 — 발행 시 변형 배정 후 축적</td></tr>'

    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="UTF-8">
<title>성장 관제탑 {today}</title><style>
body{{background:#0e1116;color:#e6edf3;font-family:'Pretendard',system-ui,sans-serif;margin:0;padding:0 0 60px}}
.c{{max-width:1000px;margin:0 auto;padding:28px 24px}}
h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:17px;color:#5eead4;margin:26px 0 12px}}
.sub{{color:#8b95a5;font-size:13px}}
.northstar{{background:#161a22;border:1px solid #2a3145;border-radius:14px;padding:20px 24px;margin:16px 0}}
.big{{font-size:40px;font-weight:800;color:#5eead4}} .goal{{color:#8b95a5;font-size:16px}}
.bar{{height:10px;background:#2a3145;border-radius:6px;overflow:hidden;margin:12px 0}}
.bar>div{{height:100%;background:linear-gradient(90deg,#5eead4,#3b82f6)}}
.tree{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:14px 0}}
.branch{{background:#161a22;border:1px solid #2a3145;border-radius:12px;padding:14px}}
.branch h3{{margin:0 0 10px;font-size:14px;color:#e6edf3}}
.node{{padding:8px 0;border-bottom:1px dashed #2a3145}} .node:last-child{{border:none}}
.nlabel{{font-size:11px;color:#8b95a5}} .nval{{font-size:20px;font-weight:700}} .nsub{{font-size:11px;color:#8b95a5}}
.pending{{background:#3a2a12;color:#f0b866;font-size:11px;padding:2px 8px;border-radius:10px;margin-left:8px}}
.lever{{background:#1c2230;border-left:4px solid #f0b866;padding:14px 18px;border-radius:0 8px 8px 0;margin:16px 0;font-size:14px}}
table{{width:100%;border-collapse:collapse;background:#161a22;border:1px solid #2a3145;border-radius:8px;overflow:hidden}}
th,td{{padding:8px 12px;border-bottom:1px solid #2a3145;font-size:13px;text-align:left}}
th{{background:#1c2230;color:#8b95a5;font-size:12px}}
</style></head><body><div class="c">
<h1>🎯 성장 관제탑 <span class="sub">{today} · 목표 일방문자 {k['goal']:,}</span></h1>

<div class="northstar">
  <div class="sub">오늘 방문자 {meas_tag}</div>
  <div class="big">{k['today_visitors']:,} <span class="goal">/ {k['goal']:,} ({progress}%)</span></div>
  <div class="bar"><div style="width:{min(progress,100)}%"></div></div>
</div>

<h2>KPI 트리 — 일방문자 = 검색 + AI요약 + 추천 + 이웃</h2>
<div class="tree">
  <div class="branch"><h3>🔍 검색 유입</h3>
    {node("인덱싱 편수", f"{k['indexed']:,}", "발행 카탈로그")}
    {node("순위추적 키워드", k['ranked'], f"측정 {k['ranked']} · 미측정 {k.get('unmeasured', 0)}(검색량 미상)", warn=(k['ranked']==0))}
    {node("상위노출율(TOP10)", f"{k['expose_rate']}%", f"측정 {k['top10']}/{k['ranked']} (전체 {k['ranked'] + k.get('unmeasured', 0)})")}
    {node("편당 유입(주)", k['per_post_visits'], f"조회수글 {k['posts_with_views']}편")}
  </div>
  <div class="branch"><h3>🤖 AI요약 유입</h3>
    {node("AEO 합격률", f"{k['aeo_rate']}%", f"{k['aeo_pass']}/{k['aeo_total']}편")}
    {node("AEO 표본", k['aeo_total'], "previews 채점")}
  </div>
  <div class="branch"><h3>📢 추천/구조</h3>
    {node("체류 합격률", f"{k.get('eng_rate',0)}%", f"{k.get('eng_pass',0)}/{k['aeo_total']}편 (경험신호)")}
    {node("토픽 클러스터", k['clusters'], "필러-클러스터")}
    {node("과분해 경고", k['over_concentration'], "자기잠식", warn=(k['over_concentration']>0))}
    {node("발행 기회 큐", k['opportunity_queue'], "미개척 고수요")}
  </div>
  <div class="branch"><h3>👥 이웃 유입</h3>
    {node("이웃 수", "—", "측정 예정(Phase 4)")}
    {node("주간 글유입 합", k['weekly_visits_from_posts'], "performance_db")}
  </div>
</div>

<div class="lever"><b>다음 레버:</b> {_weakest_lever(k)}</div>

<h2>🧪 A-B 실험 현황</h2>
<table><tr><th>실험</th><th>현재 최고</th><th>다음 시도(UCB)</th><th>표본</th></tr>{ab_rows}</table>

<p class="sub" style="margin-top:24px">measurement 활성화: reports/2026-07-04_phase1_traffic_loop.md ·
발행 전략: reports/2026-07-04_phase2_demand_authority.md</p>
</div></body></html>"""


def run_growth_dashboard() -> Path:
    k = collect_kpi()
    HARNESS_REPORTS.mkdir(parents=True, exist_ok=True)
    out = HARNESS_REPORTS / f"{date.today().strftime('%Y-%m-%d')}_growth_dashboard.html"
    out.write_text(render_html(k), encoding="utf-8")
    log.info(f"[growth_dashboard] 생성: {out.name} · 방문자 {k['today_visitors']}/{k['goal']}")
    return out


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    run_growth_dashboard()
