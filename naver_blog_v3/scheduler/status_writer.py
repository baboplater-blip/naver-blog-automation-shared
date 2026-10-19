# scheduler/status_writer.py — status.md 자동 생성 (측정 폐루프 · 개선안 #8)
"""수기로 스테일해지던 docs/status.md 를 실데이터(collect_kpi)로 렌더한다.

지금까지 status.md 는 사람이 손으로 갱신해 실제 DB 와 어긋났다. 이 모듈은
growth_dashboard.collect_kpi() 의 실측 KPI 로 '한눈에' 지표 표와 진단을
결정론적으로 다시 쓴다. run_ops 마지막 스텝에 편입되어 매 운영 루프마다 갱신.

- 진실원본: growth_dashboard.collect_kpi() (performance_db·AEO·토픽·기회·A-B).
- 부수효과: docs/status.md 쓰기(atomic tmp→rename). 네트워크 없음.
- fail-open: KPI 수집이 비면 안전한 기본값으로 렌더, 예외로 루프를 막지 않음.
"""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent          # naver_blog_v3/
HARNESS_ROOT = BASE_DIR.parent                             # naver_blog_automation_v3/
try:  # 프로필별 status(2026-07-19 격리 감사) — 프로필끼리 서로 덮어쓰기 방지
    from config import profile as _nbaprofile
    STATUS_PATH = _nbaprofile.harness_data_root() / "docs" / "status.md"
except Exception:  # noqa: BLE001
    STATUS_PATH = HARNESS_ROOT / "docs" / "status.md"

GOAL_VISITORS = 10000


def _measured(kpi: dict) -> bool:
    """실측 루프가 켜졌는가 — 순위 또는 유입 데이터가 하나라도 있으면 True."""
    return bool(kpi.get("posts_with_views")) or bool(kpi.get("ranked"))


def _visitor_row(kpi: dict) -> str:
    visitors = int(kpi.get("today_visitors", 0) or 0)
    goal = int(kpi.get("goal", GOAL_VISITORS) or GOAL_VISITORS)
    if visitors > 0:
        state = "🟢 측정 가동" if visitors >= goal else "🟠 성장 중"
        current = f"{visitors:,}"
    else:
        state, current = "🔴 측정 미가동", "0 (측정 대기)"
    return f"| 일방문자 | {current} | {goal:,} | {state} |"


def _rank_row(kpi: dict) -> str:
    ranked = int(kpi.get("ranked", 0) or 0)
    if ranked > 0:
        return f"| 순위추적 키워드 | {ranked} | — | 🟢 추적 중 |"
    return "| 순위추적 키워드 | 0 | — | 🔴 rank_tracker 미가동 |"


def _expose_row(kpi: dict) -> str:
    rate = float(kpi.get("expose_rate", 0) or 0)
    top10 = int(kpi.get("top10", 0) or 0)
    ranked = int(kpi.get("ranked", 0) or 0)
    if ranked <= 0:
        return "| 상위노출율(TOP10) | 0% | ↑ | ⬜ 데이터 대기 |"
    state = "🟢" if rate >= 20 else "🟠"
    return f"| 상위노출율(TOP10) | {rate}% ({top10}/{ranked}) | ↑ | {state} |"


def _per_post_row(kpi: dict) -> str:
    per = float(kpi.get("per_post_visits", 0) or 0)
    with_views = int(kpi.get("posts_with_views", 0) or 0)
    if with_views <= 0:
        return "| 편당 유입(주) | 0 | 20~120 | ⬜ 데이터 대기 |"
    state = "🟢" if per >= 20 else "🟠"
    return f"| 편당 유입(주) | {per} ({with_views}편) | 20~120 | {state} |"


def _rate_row(label: str, rate_key: str, pass_key: str, total_key: str,
              kpi: dict, threshold: float = 50.0) -> str:
    rate = float(kpi.get(rate_key, 0) or 0)
    passed = int(kpi.get(pass_key, 0) or 0)
    total = int(kpi.get(total_key, 0) or 0)
    if total <= 0:
        return f"| {label} | 0% (표본 없음) | ≥{threshold:.0f}% | ⬜ 데이터 대기 |"
    state = "🟢" if rate >= threshold else "🟠 리프레시 필요"
    return f"| {label} | {rate}% ({passed}/{total}) | ≥{threshold:.0f}% | {state} |"


def _cluster_row(kpi: dict) -> str:
    clusters = int(kpi.get("clusters", 0) or 0)
    state = "🟢" if clusters > 0 else "⬜ 캐시 대기"
    return f"| 토픽 클러스터 | {clusters} | — | {state} |"


def _over_conc_row(kpi: dict) -> str:
    over = int(kpi.get("over_concentration", 0) or 0)
    state = "🟢" if over == 0 else "🟠 필러화 필요"
    return f"| 과분해 경고 | {over} | 0 | {state} |"


def _opportunity_row(kpi: dict) -> str:
    q = int(kpi.get("opportunity_queue", 0) or 0)
    state = "🟢" if q > 0 else "⬜ 시드 대기"
    return f"| 발행 기회 큐 | {q} | — | {state} |"


def render_status(kpi: dict) -> str:
    """collect_kpi() 결과 → status.md 전문. 순수 함수(파일 IO 없음)."""
    kpi = kpi or {}
    today = date.today().strftime("%Y-%m-%d")
    indexed = int(kpi.get("indexed", 0) or 0)

    # 다음 레버는 growth_dashboard 진단을 그대로 재사용(단일 진실원본).
    try:
        from scheduler.growth_dashboard import _weakest_lever
        lever = _weakest_lever(kpi)
    except (ImportError, KeyError, TypeError):
        lever = "측정 대기"

    rows = [
        _visitor_row(kpi),
        f"| 인덱싱 편수 | {indexed:,} | 240+ 유효 | "
        + ("🟢 편수 충분" if indexed >= 200 else "🟠 편수 확충") + " |",
        _rank_row(kpi),
        _expose_row(kpi),
        _per_post_row(kpi),
        _rate_row("AEO 합격률", "aeo_rate", "aeo_pass", "aeo_total", kpi),
        _rate_row("체류 합격률", "eng_rate", "eng_pass", "aeo_total", kpi),
        _cluster_row(kpi),
        _over_conc_row(kpi),
        _opportunity_row(kpi),
    ]

    lines = [
        "# STATUS — 현재 상태 스냅샷",
        "",
        f"> 최종 갱신: {today} (자동 생성 — `scheduler/status_writer.py`). "
        "실시간 수치는 `growth_dashboard.html`(관제탑) / `growth_review.md`(주간) 참조.",
        "",
        "## 한눈에",
        "| 지표 | 현재 | 목표 | 상태 |",
        "|---|---|---|---|",
        *rows,
        "",
        "## 지금 가장 약한 레버",
        lever,
        "",
        "## 로드맵 (Phase 1~4 코드 완료)",
        "- Phase 1 측정 · Phase 2 수요·권위 · Phase 3 관제·실험 · Phase 4 처리량·분산 — "
        "**전 단계 코드 완성.**",
        "- 남은 건 **운영 루프**(주간 growth_review → 분산 발행 → 실측 → 학습)와 Phase 1 실측 활성화.",
        "",
        "## 운영 (일/주 1회)",
        "```",
        "_runtime/python/python.exe -m scheduler.run_ops   "
        "# 통합: sync→url보정→순위→실험보상→캐시→토픽→대시보드→리뷰→status 갱신",
        "```",
        "→ `reports/{date}_ops_summary.md` + 대시보드·리뷰·토픽 리포트 일괄 생성 + 이 문서 자동 갱신. "
        "`register_ops_task()` 로 스케줄 등록 가능.",
        "",
    ]
    return "\n".join(lines)


def write_status(*, kpi: Optional[dict] = None) -> Path:
    """collect_kpi() → docs/status.md 렌더·기록(atomic). 반환: 기록 경로.

    kpi 를 넘기면 그 값으로 렌더(테스트·재사용용). 없으면 실집계.
    """
    if kpi is None:
        kpi = _collect_kpi_safe()
    STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    text = render_status(kpi)
    _atomic_write_text(STATUS_PATH, text)
    log.info(f"[status_writer] status.md 갱신 — 방문자 {kpi.get('today_visitors', 0)} · "
             f"인덱싱 {kpi.get('indexed', 0)} · 순위 {kpi.get('ranked', 0)} · "
             f"측정 {'가동' if _measured(kpi) else '대기'}")
    return STATUS_PATH


def _collect_kpi_safe() -> dict:
    """collect_kpi 안전 호출 — 실패해도 빈 dict(fail-open)."""
    try:
        from scheduler.growth_dashboard import collect_kpi
        return collect_kpi() or {}
    except (ImportError, OSError, ValueError, KeyError, TypeError) as e:
        log.warning(f"[status_writer] KPI 집계 실패, 기본값 렌더: {e}")
        return {}


def _atomic_write_text(path: Path, text: str) -> None:
    """tmp→rename 원자적 텍스트 기록(부분 기록으로 문서 깨짐 방지)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = write_status()
    print(f"status 갱신: {p}")
