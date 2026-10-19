# scheduler/topic_report.py — 토픽 권위 지도 + 발행 기회 큐 리포트 (Phase 2)
"""232편 카탈로그의 토픽 구조를 진단하고 다음 발행 우선순위를 산출하는 운영 리포트.

- 토픽 권위 지도: 필러-클러스터(집중된 토픽) / 과분해 경고 / 고아 글(무수요 파편 후보)
- 내부링크 처방: 각 클러스터에 몇 개의 내부링크를 추가하면 좋은지
- 발행 기회 큐: opportunity_score 상위(수요×승산×권위적합 정렬)

부수효과: 읽기 + 리포트 파일 쓰기만. 발행·변경 없음. fail-open.

- 수동:   python -m scheduler.topic_report
- 정기:   register_topic_report_task() 로 주간 등록(선택).
"""
from __future__ import annotations

import os
import subprocess
import sys
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
    HARNESS_REPORTS = BASE_DIR.parent / "reports"     # 하네스 reports/
TASK_NAME = "NaverBlogTopicReport"

# 한 토픽에 이 이상 몰리면 과분해(자기잠식) 경고
OVER_CONCENTRATION = 25


def run_topic_report(*, top_opportunities: int = 25) -> Path | None:
    """토픽 권위 지도 + 기회 큐 리포트(md) 생성, 경로 반환."""
    from core import topic_cluster as tc

    # 공개 포스트목록으로 url 보정 시도(무인증) → 내부링크가 실제 링크를 만들 수 있게.
    try:
        from core.stats_fetcher import reconcile_urls_from_postlist
        reconcile_urls_from_postlist()
    except (ImportError, OSError):
        pass

    tm = tc.run_topic_map()
    if not tm.get("posts"):
        log.warning("[topic_report] 카탈로그 비어 있음 — performance_db.sync_from_history 먼저")
        return None

    # 내부링크 캐시 갱신(생성/리프레시가 소비) — url 보정 후라 실제 링크 반영.
    try:
        tc.build_and_cache()
    except (OSError, ValueError, KeyError):
        pass

    clusters = tm["clusters"]
    orphans = tm["orphans"]
    graph = tm["graph"]
    today = date.today().strftime("%Y-%m-%d")

    # 내부링크 처방 — 링크 0개인 글 수(연결 고립)
    linkless = sum(1 for links in graph.values() if not links)
    total_links = sum(len(v) for v in graph.values())

    over = [c for c in clusters if c["size"] >= OVER_CONCENTRATION]

    o = [
        "# 토픽 권위 지도 + 발행 기회 큐 (Phase 2)\n",
        f"> 생성: {today} · 카탈로그 {tm['posts']}편 → 클러스터 {len(clusters)} · 고아 {len(orphans)}\n",
        "## 요약\n",
        f"- **필러-클러스터 {len(clusters)}개** (토픽 집중 묶음) · **고아 {len(orphans)}편** (무수요 파편 후보)",
        f"- 내부링크 추천 총 {total_links}개 · 링크 고립 글 {linkless}편",
        f"- ⚠️ 과분해 경고 클러스터(≥{OVER_CONCENTRATION}편): {len(over)}개"
        + (f" — {', '.join(c['anchor'] for c in over)}" if over else ""),
        "\n## 토픽 권위 지도 (필러-클러스터)\n",
        "| 앵커 | 카테고리 | 편수 | 필러(허브) 글 | 상태 |\n|---|---|---|---|---|",
    ]
    for c in clusters:
        status = "⚠️과분해→통합·필러화" if c["size"] >= OVER_CONCENTRATION else \
                 ("✅권위" if c["size"] >= 8 else "🌱육성")
        o.append(f"| {c['anchor']} | {c['category']} | {c['size']} | "
                 f"{(c['pillar']['title'] or '')[:30]} | {status} |")

    o.append("\n## ⚠️ 과분해 토픽 — 구조 조정 우선\n")
    if over:
        o.append("한 주제에 글이 과도하게 몰리면 자기잠식으로 오히려 순위가 분산된다. "
                 "필러 1편(허브)로 권위를 모으고 나머지는 클러스터(내부링크로 필러 지지)로 재배치 권장.\n")
        for c in over:
            o.append(f"- **{c['anchor']}** ({c['category']}, {c['size']}편) → "
                     f"필러: {(c['pillar']['title'] or '')[:36]} · 나머지 {c['size']-1}편은 하위 클러스터로 내부링크 연결")
    else:
        o.append("과분해 클러스터 없음 — 분포 양호.")

    o.append("\n## 🗑️ 고아 글 (무수요 파편 후보 — 통합/비공개 검토)\n")
    if orphans:
        o.append("| 제목 | 카테고리 |\n|---|---|")
        for p in orphans[:30]:
            o.append(f"| {(p['title'] or '')[:44]} | {p.get('category','')} |")
        if len(orphans) > 30:
            o.append(f"\n> 외 {len(orphans)-30}편. 인접 필러가 있으면 통합, 없으면 비공개 후보.")
    else:
        o.append("고아 글 없음.")

    # 발행 기회 큐
    o.append("\n## 🎯 발행 기회 큐 (opportunity_score 상위)\n")
    o.append("> 수요×승산×권위적합×신선도×실측수요. 자기잠식·무수요는 자동 제외. "
             "Phase 1 유입 데이터가 쌓이면 정밀도 상승.\n")
    try:
        from core.opportunity_score import rank_publish_queue
        queue = rank_publish_queue(top_n=top_opportunities)
    except (ImportError, OSError, ValueError) as e:
        log.debug(f"[topic_report] 기회 큐 실패: {e}")
        queue = []
    if queue:
        o.append("| 점수 | 판정 | 키워드 | 볼륨 | 수요/승산/권위 |\n|---|---|---|---|---|")
        for r in queue:
            b = r["breakdown"]
            o.append(f"| {r['score']:.1f} | {r['verdict']} | {r['keyword']} | {r['volume']} | "
                     f"{b['demand']}/{b['winnable']}/{b['authority']} |")
    else:
        o.append("_기회 큐 없음 — question_seeds.json 또는 네트워크(자동완성) 확인._")

    o.append(f"\n---\n> 내부링크 그래프는 core.topic_cluster.links_for(제목) 로 글별 조회 가능. "
             f"발행 시 삽입에 사용.")

    HARNESS_REPORTS.mkdir(parents=True, exist_ok=True)
    out = HARNESS_REPORTS / f"{today}_topic_authority.md"
    out.write_text("\n".join(o), encoding="utf-8")
    log.info(f"[topic_report] 생성: {out.name} · 클러스터 {len(clusters)} · 고아 {len(orphans)} "
             f"· 기회 {len(queue)}")
    return out


def run_pillar_brief() -> Path | None:
    """과분해 토픽(≥OVER_CONCENTRATION)의 필러화 재구조화 브리프 생성.

    각 과분해 클러스터에 대해 필러(허브) 지정 + 멤버→필러 내부링크 배선표를 산출.
    실제 재발행은 사용자 트리거(원칙 불변) — 이 브리프는 실행 계획이다.
    """
    from core import topic_cluster as tc
    try:
        from core.stats_fetcher import reconcile_urls_from_postlist
        reconcile_urls_from_postlist()
    except (ImportError, OSError):
        pass

    tm = tc.run_topic_map()
    if not tm.get("posts"):
        return None
    over = [c for c in tm["clusters"] if c["size"] >= OVER_CONCENTRATION]
    graph = tm["graph"]
    today = date.today().strftime("%Y-%m-%d")

    o = [
        "# 과분해 토픽 필러화 — 재구조화 실행 브리프\n",
        f"> 생성: {today} · 대상: 과분해 클러스터 {len(over)}개 "
        f"(자기잠식 중 — 필러로 권위 집중 필요)\n",
        "## 원리\n",
        "한 주제에 글이 몰리면 C-Rank 권위가 분산돼 오히려 순위가 떨어진다. "
        "**필러 1편(허브)에 권위를 모으고, 나머지는 필러를 가리키는 내부링크로 지지**하면 "
        "필러가 상위로 오르고 클러스터 전체가 함께 끌려 올라간다.\n",
        "> ⚠️ 실제 재발행/수정은 사용자가 트리거. 아래는 실행 계획 + 내부링크 배선표. "
        "생성/리프레시 경로엔 필러링크 자동삽입이 이미 연결됨(url 보정 후 자동 반영).\n",
    ]
    url_missing = 0
    for c in over:
        pillar = c["pillar"]
        o.append(f"\n## 🏛️ {c['anchor']} ({c['category']}, {c['size']}편)\n")
        o.append(f"- **필러(허브) 지정:** {pillar['title']}")
        if pillar.get("url"):
            o.append(f"  - URL: {pillar['url']}")
        else:
            o.append("  - ⚠️ URL 미보정 — 공개 포스트목록/통계에서 url 채운 뒤 링크 생성 가능")
        o.append(f"- **작업:** 아래 {c['size']-1}편 각각의 본문 하단에 필러 링크 추가 → "
                 f"필러 본문엔 대표 자식 링크 추가. 신규 발행은 이 토픽 자제(기회 큐의 미개척 키워드로).")
        o.append("\n| 멤버 글 | 추가할 내부링크(→ 필러) |\n|---|---|")
        for m in c["members"]:
            if m.get("key") == pillar.get("key"):
                continue
            arrow = "→ (핵심) " + (pillar["title"][:24]) if pillar.get("url") else "→ 필러(url 대기)"
            if not m.get("url"):
                url_missing += 1
            o.append(f"| {(m['title'] or '')[:34]} | {arrow} |")

    o.append("\n---\n## 실행 체크리스트\n")
    o.append("1. url 보정: `python -m core.stats_fetcher` 또는 토픽리포트 실행(공개 포스트목록 자동 보정)")
    o.append("2. 필러 글 본문에 대표 자식 3~5개 링크 삽입")
    o.append("3. 멤버 글 리프레시 시 필러 링크 자동삽입됨(`post_refresher` + 필러-클러스터 캐시)")
    o.append("4. 신규 발행은 `scheduler/topic_report.py` 기회 큐 상단(미개척 고볼륨)에서 선택")
    if url_missing:
        o.append(f"\n> ℹ️ 현재 url 미보정 {url_missing}건 — 링크 자동생성은 url 채워진 뒤 활성.")

    HARNESS_REPORTS.mkdir(parents=True, exist_ok=True)
    out = HARNESS_REPORTS / f"{today}_pillar_restructure_brief.md"
    out.write_text("\n".join(o), encoding="utf-8")
    log.info(f"[topic_report] 필러화 브리프 생성: {out.name} · 과분해 {len(over)}토픽")
    return out


def _pythonw() -> str:
    p = Path(sys.executable).parent / "pythonw.exe"
    return str(p) if p.exists() else sys.executable


def register_topic_report_task(*, weekday: str = "TUE", at: str = "08:30") -> tuple:
    runner = f'"{_pythonw()}" "{BASE_DIR / "scheduler" / "topic_report.py"}"'
    try:  # 등록 시점 프로필 고정(격리 감사) — 발화 시점 .active 표류 방지
        runner += f' --profile {_nbaprofile.active_profile()}'
    except Exception:  # noqa: BLE001
        pass
    cmd = ["schtasks", "/create", "/tn", TASK_NAME, "/tr", runner,
           "/sc", "weekly", "/d", weekday, "/st", at, "/f", "/rl", "LIMITED"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode != 0:
            return False, f"등록 실패: {r.stderr.strip()}"
        return True, f"등록 완료: 매주 {weekday} {at} 토픽 리포트"
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"등록 오류: {e}"


def unregister_topic_report_task() -> tuple:
    try:
        r = subprocess.run(["schtasks", "/delete", "/tn", TASK_NAME, "/f"],
                           capture_output=True, text=True, encoding=CONSOLE_ENCODING, errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return (r.returncode == 0), ("해제 완료" if r.returncode == 0 else "미등록 상태")
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, f"해제 오류: {e}"


if __name__ == "__main__":
    run_topic_report()
    run_pillar_brief()
