# scheduler/rank_monitor.py — 순위 모니터 일간 루프 (P0, 2026-07-19)
"""데이터랩툴즈식 순위 모니터의 일간 실행 진입점.

    ① API 순위 추적(전량, rank_tracker) → ② 통합검색 실노출(워치리스트, serp_rank P1)
    → ③ 변동 감지·카카오 알림(rank_alerts) → ④ 승자 구조 학습(top_post_analyzer P2)
    → ⑤ 리포트(reports/{date}_rank_monitor.md)

각 단계 fail-open — 하나 실패해도 다음 진행(run_ops 패턴). 발행·변경 없음, 측정만.

- 수동:  python -m scheduler.rank_monitor
- 일간:  register_rank_task() (기본 매일 09:10 — 아침 발행 배치 후 관측)
- 주 1회 run_ops ⑥의 순위 추적과 중복돼도 무해 — rank_tracker 가 '오늘 기록
  없는 글 우선'이라 같은 날 재실행은 나머지 커버리지만 채운다.
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
TASK_NAME = "NaverBlogRankMonitor"


def _step(name: str, fn) -> dict:
    t0 = time.monotonic()
    try:
        result = fn()
        dt = time.monotonic() - t0
        log.info(f"  ✓ {name} ({dt:.1f}s) — {result}")
        return {"step": name, "ok": True, "detail": str(result), "sec": round(dt, 1)}
    except Exception as e:  # noqa: BLE001 — 모니터 루프는 어떤 실패도 전체를 막지 않음
        dt = time.monotonic() - t0
        log.warning(f"  ✗ {name} 실패 ({dt:.1f}s): {e}")
        return {"step": name, "ok": False, "detail": str(e)[:120], "sec": round(dt, 1)}


def collect_and_record_measurements() -> str:
    """성과 수집 → measurement 적재 → (표본 충분할 때만) 후보 가중치 환류.

    독립 함수로 분리한 이유: 일간 루프 안 클로저로 두면 이 결측 안전성(needs_login/
    unproven/error 는 0 을 적지 않고 스킵)을 단위 테스트가 직접 부를 수 없다.
    `run_rank_monitor` 의 "①e 성과 수집(자동)" 스텝이 이 함수를 그대로 호출한다.
    """
    from datetime import datetime

    from core import measurement, stats_fetcher
    try:
        from config import settings as _s
        blog_id = getattr(_s, "NAVER_ID", "") or ""
    except ImportError:
        blog_id = ""
    if not blog_id or blog_id == "your_naver_id":
        return "blog_id 미설정 — 스킵"

    result = stats_fetcher.collect_post_metrics_readonly(blog_id=blog_id)
    if not result.get("ok"):
        detail = f"수집 불가({result.get('reason', 'unknown')}) — 기록 0편"
        if result.get("action"):
            detail += f" · 조치: {result['action']}"
        return detail

    now_iso = datetime.now().isoformat(timespec="seconds")
    # 이 경로의 숫자가 누적인지 기간인지 확인된 바 없다 — 수집기가 적어 준 의미를
    # 그대로 옮기고(기본 unknown), 판단은 measurement 쪽 게이트가 한다.
    default_basis = str(result.get("basis") or "unknown")
    recorded = skipped = 0
    skip_reasons = {}
    for post in result.get("posts", []):
        if post.get("views") is None:
            skipped += 1
            skip_reasons["missing_value"] = skip_reasons.get("missing_value", 0) + 1
            continue  # 결측은 0 으로 대체하지 않고 스킵
        outcome = measurement.record(
            "post_views", post["views"], log_no=post.get("log_no", ""),
            window="d1", source=post.get("source") or "rank_monitor_auto",
            collected_at=post.get("observed_at") or now_iso,
            basis=str(post.get("basis") or default_basis),
            identity=str(post.get("identity") or "log_no"))
        if isinstance(outcome, dict) and outcome.get("saved"):
            recorded += 1
        else:
            skipped += 1
            reason = (str(outcome.get("reason") or "unknown")
                      if isinstance(outcome, dict) and outcome.get("skipped")
                      else "unconfirmed_result")
            skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    fw = measurement.selected_feedback_weights()
    if fw.get("held_reason"):
        feedback_note = f"환류 보류({fw['held_reason']})"
    else:
        feedback_note = f"환류 적용 {len(fw.get('weights') or {})}개 카테고리"
    reason_note = (" · 스킵사유=" + ", ".join(
        f"{reason}:{count}" for reason, count in sorted(skip_reasons.items()))
                   if skip_reasons else "")
    return (f"기록 {recorded}편(스킵 {skipped}편{reason_note}) · manual_required="
            f"{result.get('manual_required')} · 기간의미={default_basis} · {feedback_note}")


def run_rank_monitor(*, max_keywords: int = 150, do_serp: bool = True,
                     do_alerts: bool = True) -> dict:
    """순위 모니터 일간 루프. 반환 {steps, report, summary}."""
    log.info("═══ 순위 모니터 시작 ═══")
    steps = []

    def _api():
        from core.rank_tracker import run_rank_tracking
        r = run_rank_tracking(max_keywords=max_keywords)
        if r.get("reason") == "no_credentials":
            return "자격증명 없음 — 스킵"
        if r.get("reason") == "auth_failed":
            return "검색 API 미인증(Scope) — 콘솔에서 사용설정 필요"
        return (f"조회 {r.get('checked', 0)} · 노출 {r.get('ranked', 0)} · "
                f"TOP10 {r.get('top10', 0)}")
    steps.append(_step("① API 순위 추적", _api))

    def _api_watch():
        # 워치리스트 키워드는 글의 '대표 키워드'가 아니어도 매일 API 순위를 잰다
        # (rank_tracker 는 글당 대표 키워드만 추적 — 워치 커버리지 보장은 여기서).
        from core.rank_tracker import check_rank
        from core import rank_watchlist
        kws = rank_watchlist.keywords()
        if not kws:
            return "워치리스트 비어 있음"
        measured = found = 0
        for kw in kws:
            r = check_rank(kw)
            if r.get("reason") in ("no_credentials", "auth_failed"):
                return f"스킵: {r['reason']}"
            measured += 1
            if r.get("found"):
                found += 1
            rank_watchlist.record_measurement(kw, api_rank=int(r.get("rank", 0)))
            time.sleep(0.3)
        return f"측정 {measured} · 노출 {found}"
    steps.append(_step("①b 워치리스트 API 순위", _api_watch))

    def _news():
        # 뉴스 선행지표 — 워치 키워드의 기사 스파이크·변경 보도 측정(주제 무지,
        # 프로필 자산에서 키워드 자동 파생). 발행 큐 가산점·홈 탭이 이 캐시를 읽는다.
        from core.news_radar import run_news_radar
        r = run_news_radar()
        if r.get("reason"):
            return f"스킵: {r['reason']}"
        return (f"측정 {r.get('checked', 0)} · 스파이크 {r.get('spikes', 0)} · "
                f"변경 감지 {r.get('changes', 0)}")
    steps.append(_step("①c 뉴스 레이더", _news))

    def _knowledge():
        # 지식iN·백과 관심사 캐시 — 워치 키워드(news_radar 와 같은 파생)의 실측 사용자
        # 질문·정의를 미리 채운다. 생성 프롬프트는 이 캐시만 읽어 네트워크 0을 지킨다.
        # KNOWLEDGE_SOURCES_ENABLED=False 면 refresh_watch 가 즉시 스킵(네트워크 0).
        from core.knowledge_sources import refresh_watch
        r = refresh_watch()
        if r.get("reason") == "disabled":
            return "비활성(opt-in) — 스킵"
        return (f"갱신 +{r.get('refreshed', 0)} · 신선 스킵 {r.get('skipped', 0)} · "
                f"후보 {r.get('candidates', 0)}")
    steps.append(_step("①d 지식iN 관심사 캐시", _knowledge))

    steps.append(_step("①e 성과 수집(자동)", collect_and_record_measurements))

    if do_serp:
        def _serp():
            from core.serp_rank import track_watchlist
            r = track_watchlist()
            if r.get("reason"):
                return f"스킵: {r['reason']}"
            return (f"실노출 측정 {r.get('checked', 0)} · 노출 {r.get('found', 0)} · "
                    f"캐시 {r.get('skipped_cached', 0)}")
        steps.append(_step("② 통합검색 실노출(워치리스트)", _serp))

    if do_alerts:
        def _alerts():
            from core.rank_alerts import detect_and_notify
            extra = []
            try:        # 뉴스 신호를 같은 일간 묶음에 편승(별도 알림 없음 — 피로 방지)
                from core.news_radar import kakao_lines
                extra = kakao_lines()
            except Exception:  # noqa: BLE001
                extra = []
            r = detect_and_notify(extra_lines=extra)
            return (f"변동 {r.get('events', 0)}건"
                    + (f" · 뉴스 {len(extra)}건" if extra else "")
                    + (" · 카카오 발송" if r.get("sent") else ""))
        steps.append(_step("③ 변동 감지·알림", _alerts))

    def _winner():
        from core.top_post_analyzer import analyze_watchlist, render_report
        r = analyze_watchlist()          # 캐시 7일 — 만료분만 실조회(저빈도)
        name = render_report()
        return f"프로파일 분석 {r.get('analyzed', 0)} · 캐시 {r.get('cached', 0)} → {name}"
    steps.append(_step("④ 승자 구조 학습(P2)", _winner))

    report = ""
    def _report():
        nonlocal report
        report = _write_report(steps)
        return report
    steps.append(_step("⑤ 리포트", _report))

    ok = sum(1 for s in steps if s["ok"])
    summary = {"total": len(steps), "ok": ok, "fail": len(steps) - ok}
    log.info(f"═══ 순위 모니터 완료 — {ok}/{len(steps)} 성공 ═══")
    return {"steps": steps, "report": report, "summary": summary}


def _fmt_rank(r: int) -> str:
    return f"{r}위" if r else "—"


def _write_report(steps: list) -> str:
    """워치리스트 표 + 단계 요약을 마크다운으로. 반환: 파일명."""
    from core import rank_watchlist

    today = date.today().strftime("%Y-%m-%d")
    rows = rank_watchlist.rows(days=7)
    o = [f"# 📈 순위 모니터 — {today}\n"]

    if rows:
        o += ["## 워치리스트\n",
              "| 키워드 | 검색량(월) | 어제 | 오늘 | 변동 | 실노출(통합) | 최근 7일 |",
              "|---|---|---|---|---|---|---|"]
        for r in rows:
            if r["delta"] is None:
                delta = "—"
            elif r["delta"] > 0:
                delta = f"▲{r['delta']}"
            elif r["delta"] < 0:
                delta = f"▼{-r['delta']}"
            else:
                delta = "="
            o.append(f"| {r['keyword']} | {r['volume']:,} | "
                     f"{_fmt_rank(r['yesterday'])} | {_fmt_rank(r['today'])} | "
                     f"{delta} | {_fmt_rank(r['serp_today'])} | `{r['spark']}` |")
    else:
        o += ["## 워치리스트\n",
              "> 등록된 키워드가 없어요 — 성장 관제탑 탭 [📈 순위 모니터]에서 추가하거나,",
              "> 자동 제안(suggest_candidates)을 사용하세요."]

    o += ["\n## 실행 단계\n", "| 단계 | 결과 | 상세 | 시간 |", "|---|---|---|---|"]
    for s in steps:
        o.append(f"| {s['step']} | {'✅' if s['ok'] else '⚠️'} | {s['detail']} | {s['sec']}s |")
    o.append("\n> 실노출(통합검색)은 참고치 — API 순위와 크게 어긋나면(상위인데 실노출 없음) "
             "AI요약 가로챔 가능성 → AEO 리프레시 후보.")

    HARNESS_REPORTS.mkdir(parents=True, exist_ok=True)
    name = f"{today}_rank_monitor.md"
    (HARNESS_REPORTS / name).write_text("\n".join(o), encoding="utf-8")
    return name


# ─────────────────────────────────────────────────────
# Windows 작업 스케줄러 등록 (run_ops 패턴)
# ─────────────────────────────────────────────────────
def _pythonw() -> str:
    p = Path(sys.executable).parent / "pythonw.exe"
    return str(p) if p.exists() else sys.executable


def register_rank_task(*, at: str = "09:10") -> tuple:
    """매일 at 시각에 순위 모니터를 등록. 코드만 — 호출은 사용자/GUI 몫."""
    runner = f'"{_pythonw()}" "{BASE_DIR / "scheduler" / "rank_monitor.py"}"'
    try:  # 등록 시점 프로필 고정(격리 감사) — 발화 시점 .active 표류 방지
        runner += f' --profile {_nbaprofile.active_profile()}'
    except Exception:  # noqa: BLE001
        pass
    cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", runner,
           "/sc", "daily", "/st", at, "/f", "/rl", "LIMITED"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), (f"등록: 매일 {at}" if r.returncode == 0
                                     else f"실패: {r.stderr.strip()}")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"오류: {e}"


def check_rank_task() -> tuple:
    try:
        r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                           capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), ("등록됨" if r.returncode == 0 else "미등록")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"오류: {e}"


def unregister_rank_task() -> tuple:
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
    out = run_rank_monitor()
    print(f"\n순위 모니터: {out['summary']['ok']}/{out['summary']['total']} 성공")
    if out.get("report"):
        print(f"  리포트: reports/{out['report']}")
