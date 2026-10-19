# scheduler/run_ops.py — 통합 운영 루프 (주 1회 이 명령 하나로)
"""흩어진 운영 모듈을 순서대로 실행하는 단일 진입점.

    카탈로그 동기화 → url 보정 → 순위 추적 → 실험 보상 회수 → 내부링크 캐시
    → 토픽 리포트 → 성장 대시보드 → 방문자 진단 → 주간 성장 리뷰 → status.md 갱신

되먹임 루프(측정 폐루프): ④ 실험 보상 회수가 Phase1 실측 유입을 A-B 밴딧 보상으로
회수하고, ⑨ 주간 리뷰가 실측 유입 검색어(전용 글 없는 고ROI)를 발행 계획 1순위로
편입하며, ⑩ status.md 를 실데이터로 자동 갱신한다(수기 스테일 제거).

각 단계는 fail-open(하나 실패해도 다음 진행). 마지막에 요약 + 산출 리포트 경로.
발행·변경(자동 라이브 발행)은 하지 않는다 — 측정·집계·리포트만.

- 수동:      python -m scheduler.run_ops
- 정기(주간): register_ops_task() 로 등록(기본 주간, sc="daily" 로 일간 가능).
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if __name__ == "__main__":  # schtasks 직접 실행 — 임포트 전 프로필 고정(격리 감사)
    from config.profile_boot import pin_profile_from_argv
    pin_profile_from_argv()
from config.logger import get_logger
from config.win_console import CONSOLE_ENCODING

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
try:  # 프로필별 리포트(2026-07-19 격리 감사) — 공유 폴더 혼입 방지
    from config import profile as _nbaprofile
    HARNESS_REPORTS = _nbaprofile.reports_dir()
except Exception:  # noqa: BLE001
    HARNESS_REPORTS = BASE_DIR.parent / "reports"
TASK_NAME = "NaverBlogOps"


def _step(name: str, fn) -> dict:
    t0 = time.monotonic()
    try:
        result = fn()
        dt = time.monotonic() - t0
        log.info(f"  ✓ {name} ({dt:.1f}s) — {result}")
        return {"step": name, "ok": True, "detail": str(result), "sec": round(dt, 1)}
    except Exception as e:  # 운영 루프는 어떤 단계 실패도 전체를 막지 않음
        dt = time.monotonic() - t0
        log.warning(f"  ✗ {name} 실패 ({dt:.1f}s): {e}")
        return {"step": name, "ok": False, "detail": str(e)[:120], "sec": round(dt, 1)}


def run_ops(*, do_rank: bool = True) -> dict:
    """전체 운영 루프 실행. 반환: {steps, reports, summary}."""
    log.info("═══ 통합 운영 루프 시작 ═══")
    steps = []

    def _sync():
        from core import performance_db as pdb
        return f"{pdb.sync_from_history()}편 카탈로그"
    steps.append(_step("① 카탈로그 동기화", _sync))

    def _reconcile():
        from core.stats_fetcher import reconcile_urls_from_postlist
        n = reconcile_urls_from_postlist()
        return f"url 보정 {n}편(누적)"
    steps.append(_step("② url 보정(공개 포스트목록)", _reconcile))

    if do_rank:
        def _rank():
            from core.rank_tracker import run_rank_tracking
            r = run_rank_tracking(max_keywords=120)
            if r.get("reason") == "auth_failed":
                return "검색 API 미인증(Scope) — 콘솔에서 검색 API 사용설정 필요"
            if r.get("reason") == "no_credentials":
                return "자격증명 없음 — 스킵"
            return f"조회 {r.get('checked',0)} 노출 {r.get('ranked',0)} TOP10 {r.get('top10',0)}"
        steps.append(_step("③ 순위 추적", _rank))

    def _absync():
        from core.ab_test import sync_rewards_from_performance
        return f"실험 보상 {sync_rewards_from_performance()}건 갱신"
    steps.append(_step("④ 실험 보상 회수", _absync))

    def _cache():
        from core.topic_cluster import build_and_cache
        build_and_cache()
        return "내부링크 캐시 갱신"
    steps.append(_step("⑤ 내부링크 캐시", _cache))

    reports = {}

    def _topic():
        from scheduler.topic_report import run_topic_report, run_pillar_brief
        p = run_topic_report()
        b = run_pillar_brief()
        if p:
            reports["topic"] = p.name
        if b:
            reports["pillar"] = b.name
        return f"{p.name if p else '—'} + {b.name if b else '—'}"
    steps.append(_step("⑥ 토픽 권위 리포트", _topic))

    def _dash():
        from scheduler.growth_dashboard import run_growth_dashboard
        p = run_growth_dashboard()
        reports["dashboard"] = p.name
        return p.name
    steps.append(_step("⑦ 성장 대시보드", _dash))

    def _visitor():
        from scheduler.visitor_activation_report import run_visitor_activation_report, gather
        g = gather()
        p = run_visitor_activation_report()
        reports["visitor"] = p.name
        gp = g["gap"]
        n_sus = len(gp.get("suspects", [])) if gp.get("status") == "ok" else 0
        return (f"상태 {g['diag'].get('status')} · gap {gp.get('status')}"
                + (f" · 가로챔 용의자 {n_sus}편" if gp.get("status") == "ok" else " · 유입 데이터 대기"))
    steps.append(_step("⑧ 방문자 활성화 진단", _visitor))

    def _review():
        from scheduler.growth_review import run_growth_review
        p = run_growth_review()
        reports["review"] = p.name
        return p.name
    steps.append(_step("⑨ 주간 성장 리뷰", _review))

    def _status():
        from scheduler.status_writer import write_status
        p = write_status()
        reports["status"] = p.name
        return f"docs/{p.name} 실데이터 갱신"
    steps.append(_step("⑩ status.md 자동 갱신", _status))

    def _competitor():
        # 게이트 ①: opt-in off 면 네트워크 없이 즉시 스킵(실운영 무영향).
        from config import settings as _s
        if not bool(getattr(_s, "COMPETITOR_ANALYSIS_ENABLED", False)):
            return "비활성(opt-in)"
        # 게이트 ②: 빠른 모드(do_rank=False, GUI '운영 루프 실행')는 스킵 — 경쟁분석은
        # 순위추적처럼 네트워크 heavy 라 full-run 에서만. TTL 7일 캐시가 빈도를 제어한다.
        if not do_rank:
            return "빠른 모드 스킵"
        from core import competitor_analyzer as ca
        # max_competitors 25: classify 가 후보를 순차 fetch(각 최대 15s)라 40 은 최악
        # ~600s. 25 로 낮춰 최악 지연 ~37%↓(신호 집계엔 25 표본이면 충분). 빈도는
        # TTL 7일 캐시가 제어(빈 마커 포함) — 스윕은 TTL 창당 1회.
        prof = ca.analyze(ca.default_keywords(), max_competitors=25, use_cache=True)
        name = ca.render_report()
        if name:
            reports["competitor"] = name
        return f"전문 {prof['specialist_count']}곳" if prof else "specialist 0"
    steps.append(_step("⑪ 경쟁 블로그 분석", _competitor))

    ok = sum(1 for s in steps if s["ok"])
    summary = {"total": len(steps), "ok": ok, "fail": len(steps) - ok}
    log.info(f"═══ 운영 루프 완료 — {ok}/{len(steps)} 성공 ═══")

    _write_ops_summary(steps, reports, summary)
    return {"steps": steps, "reports": reports, "summary": summary}


def _write_ops_summary(steps, reports, summary) -> None:
    today = date.today().strftime("%Y-%m-%d")
    o = [f"# 운영 루프 실행 요약 — {today}\n",
         f"> {summary['ok']}/{summary['total']} 단계 성공\n", "## 단계별\n",
         "| 단계 | 결과 | 상세 | 시간 |\n|---|---|---|---|"]
    for s in steps:
        o.append(f"| {s['step']} | {'✅' if s['ok'] else '⚠️'} | {s['detail']} | {s['sec']}s |")
    o.append("\n## 생성 리포트\n")
    for k, v in reports.items():
        subdir = "docs" if k == "status" else "reports"
        o.append(f"- {k}: `{subdir}/{v}`")
    o.append("\n> 다음: 주간 리뷰의 발행 계획대로 발행(사용자 트리거) · A-B 변형 적용 · 필러화 실행.")
    HARNESS_REPORTS.mkdir(parents=True, exist_ok=True)
    (HARNESS_REPORTS / f"{today}_ops_summary.md").write_text("\n".join(o), encoding="utf-8")


def _pythonw() -> str:
    p = Path(sys.executable).parent / "pythonw.exe"
    return str(p) if p.exists() else sys.executable


def register_ops_task(*, schedule: str = "weekly", weekday: str = "MON",
                      at: str = "07:30") -> tuple:
    """Windows 작업 스케줄러에 운영 루프를 등록. 코드만 — 호출은 사용자 몫.

    schedule: "weekly"(기본, weekday 지정) 또는 "daily"(매일 at 시각).
    일간 등록은 실측 폐루프를 매일 돌려 status·밴딧·발행계획을 최신으로 유지한다.
    """
    schedule = (schedule or "weekly").lower()
    if schedule not in ("weekly", "daily"):
        return False, f"알 수 없는 주기: {schedule} (weekly|daily)"
    runner = f'"{_pythonw()}" "{BASE_DIR / "scheduler" / "run_ops.py"}"'
    try:  # 등록 시점 프로필 고정(격리 감사) — 발화 시점 .active 표류 방지
        runner += f' --profile {_nbaprofile.active_profile()}'
    except Exception:  # noqa: BLE001
        pass
    cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", runner,
           "/sc", schedule]
    if schedule == "weekly":
        cmd += ["/d", weekday]
    cmd += ["/st", at, "/f", "/rl", "LIMITED"]
    when = f"매일 {at}" if schedule == "daily" else f"매주 {weekday} {at}"
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), (f"등록: {when}" if r.returncode == 0
                                     else f"실패: {r.stderr.strip()}")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"오류: {e}"


def check_ops_task() -> tuple:
    """운영 루프 작업의 등록 여부. (registered: bool, message: str) — 홈 탭 토글용 (UX P2)."""
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                           capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), ("등록됨" if r.returncode == 0 else "미등록")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"오류: {e}"


def unregister_ops_task() -> tuple:
    try:
        r = subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                           capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), ("해제 완료" if r.returncode == 0 else "미등록")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"오류: {e}"


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    out = run_ops()
    print(f"\n운영 루프: {out['summary']['ok']}/{out['summary']['total']} 성공")
    for k, v in out["reports"].items():
        print(f"  {k}: reports/{v}")
