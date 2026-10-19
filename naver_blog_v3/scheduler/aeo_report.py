# scheduler/aeo_report.py
"""AEO(네이버 AI 요약) 준비도 리포트 — 정기 생성용.

발행물(previews HTML)을 aeo_scorer 로 채점해 평균/합격률/신호별 약점/최약체를
마크다운 리포트로 산출한다. 부수효과 0 — 채점(읽기)과 리포트 파일 쓰기만.

- 수동:   python -m scheduler.aeo_report   (또는 run_aeo_report() 호출)
- 정기:   register_aeo_report_task() 로 Windows 작업 스케줄러에 주간 등록.
"""
from __future__ import annotations

import os
import subprocess
import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if __name__ == "__main__":  # schtasks 직접 실행 — 임포트 전 프로필 고정(격리 감사)
    from config.profile_boot import pin_profile_from_argv
    pin_profile_from_argv()
from config.logger import get_logger
from config.win_console import CONSOLE_ENCODING

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent          # naver_blog_v3/
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

PREVIEWS_DIR = _DATA_DIR / "previews"
# 프로필별 리포트(2026-07-19 격리 감사) — 종전 앱루트 reports(제3의 위치) 하드코딩 교정.
REPORTS_DIR = _nbaprofile.reports_dir()
SAMPLE_SIZE = 80

TASK_NAME = "NaverBlogAEOReport"

_SIGNAL_KR = {
    "tldr": "TL;DR/결론부터", "lead_answer": "첫문장 직접답변", "stats_density": "수치밀도",
    "steps": "단계·절차", "faq": "FAQ Q&A", "comparison": "비교/대비",
    "conclusion": "결론 마무리", "sources": "1차 출처",
}
_WEAK = ("absent", "below_min")


def _bucket(s: float) -> str:
    return "80+" if s >= 80 else "70-79" if s >= 70 else "50-69" if s >= 50 else "<50"


def run_aeo_report(*, sample_size: int = SAMPLE_SIZE) -> Path | None:
    """previews 를 채점해 AEO 준비도 리포트(md)를 reports/ 에 저장, 경로 반환."""
    from core import aeo_scorer

    files = sorted(PREVIEWS_DIR.glob("*.html"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:sample_size]
    if not files:
        log.warning("[aeo_report] previews HTML 없음 — 리포트 스킵")
        return None

    scored, weak_counter = [], Counter()
    for f in files:
        try:
            html = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        r = aeo_scorer.score(html)
        weak = [k for k, g in r.grades.items() if g in _WEAK]
        scored.append((f.name, r.score, r.passed(), weak))
        for k in weak:
            weak_counter[k] += 1

    if not scored:
        log.warning("[aeo_report] 채점 가능한 파일 없음")
        return None

    n = len(scored)
    avg = sum(s for _, s, _, _ in scored) / n
    passed = sum(1 for _, _, p, _ in scored if p)
    buckets = Counter(_bucket(s) for _, s, _, _ in scored)
    today = date.today().strftime("%Y-%m-%d")

    o = [
        f"# AEO(네이버 AI 요약) 준비도 리포트\n",
        f"> 생성: {today} · 최근 발행물 {n}편(previews) 채점. 합격선 {aeo_scorer.PASS_THRESHOLD}점.\n",
        "## 요약\n",
        f"- **평균 AEO 점수: {avg:.1f} / 100**",
        f"- **합격(≥{aeo_scorer.PASS_THRESHOLD}): {passed}/{n}편 ({passed*100//n}%)**",
        "- 점수 분포: " + " · ".join(f"{k} {buckets.get(k,0)}편"
                                     for k in ["80+", "70-79", "50-69", "<50"]) + "\n",
        "## 신호별 약점 빈도 (미달 편수 = 개선 우선순위)\n",
        "| AEO 신호 | 미달 편수 | 미달률 |\n|---|---|---|",
    ]
    for k, c in weak_counter.most_common():
        o.append(f"| {_SIGNAL_KR.get(k, k)} | {c} | {c*100//n}% |")
    o.append("\n## AEO 최약체 15편 (리프레시 우선 대상)\n")
    o.append("| 점수 | 미달 신호 | 파일 |\n|---|---|---|")
    for name, sc, _p, weak in sorted(scored, key=lambda x: x[1])[:15]:
        wk = ", ".join(_SIGNAL_KR.get(k, k) for k in weak)
        o.append(f"| {sc:.0f} | {wk[:64]} | {name} |")
    o.append("\n> 최약체는 post_refresher.run_refresh_batch(max_count=15) 로 AEO 재생성 가능.")

    REPORTS_DIR.mkdir(exist_ok=True)
    out = REPORTS_DIR / f"{today}_aeo_readiness.md"
    out.write_text("\n".join(o), encoding="utf-8")
    log.info(f"[aeo_report] 생성: {out.name} · 평균 {avg:.1f} · 합격 {passed}/{n}")
    return out


# ─────────────────────────────────────────────────────
# Windows 작업 스케줄러 등록 (주간)
# ─────────────────────────────────────────────────────
def _pythonw() -> str:
    p = Path(sys.executable).parent / "pythonw.exe"
    return str(p) if p.exists() else sys.executable


def register_aeo_report_task(*, weekday: str = "MON", at: str = "08:30") -> tuple:
    """매주 지정 요일/시각에 AEO 리포트를 생성하도록 작업 스케줄러에 등록."""
    # 스크립트를 직접 실행 — aeo_report.py 의 __main__ 이 run_aeo_report() 호출.
    # (스크립트 상단 sys.path.append 로 config/core 임포트가 cwd 무관하게 동작)
    runner = f'"{_pythonw()}" "{BASE_DIR / "scheduler" / "aeo_report.py"}"'
    try:  # 등록 시점 프로필 고정(격리 감사) — 발화 시점 .active 표류 방지
        runner += f' --profile {_nbaprofile.active_profile()}'
    except Exception:  # noqa: BLE001
        pass
    cmd = [
        "schtasks", "/create", "/tn", TASK_NAME,
        "/tr", runner, "/sc", "weekly", "/d", weekday, "/st", at, "/f", "/rl", "LIMITED",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return False, f"등록 실패: {r.stderr.strip()}"
        log.info(f"[aeo_report] 작업 등록: 매주 {weekday} {at}")
        return True, f"등록 완료: 매주 {weekday} {at} AEO 리포트 생성"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"등록 오류: {e}"


def unregister_aeo_report_task() -> tuple:
    try:
        r = subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                           capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), ("해제 완료" if r.returncode == 0 else "미등록 상태")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"해제 오류: {e}"


if __name__ == "__main__":
    run_aeo_report()
