# core/draft_triage.py — 보관본 백로그 선별 분류 (전량 소진 → 선별 소진)
"""수천 편의 draft_ready 백로그를 '품질 × 수요'로 분류해 발행/업그레이드/폐기 버킷을 낸다.

배경: 발행 232편이 이미 검색 TOP3 176편인데 일방문자 ~160(장기저유입). 저수요 rank-1
글은 1위여도 유입이 없다(실측). 따라서 백로그를 전량 발행하면 몇 달간 개선 콘텐츠가
묻히고 트래픽은 안 늘고 C-Rank만 희석된다. → 발행 가치 있는 소수만 남기고 나머지는 폐기/동결.

분류 축(둘 다 부수효과 0·Claude 미호출):
  · 품질  = aeo_scorer + engagement_scorer (본문 마크다운 채점)
  · 수요  = 제목이 '핵심 제도 + answer형(지급일·금액·자격·비교)'인가 (실측 승자 패턴)

버킷:
  publish  수요 有 + 품질 OK      → 지금 발행 가치
  upgrade  수요 有 + 품질 낮음     → 개선 파이프라인으로 재생성 후 발행(소수만)
  retire   수요 낮음(저수요 롱테일) → 발행해도 유입 0 → 폐기/동결(발행 슬롯 낭비 방지)

읽기 전용 — 실제 발행·삭제는 사용자 트리거. 이 모듈은 버킷·리포트만 산출.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)


def _reports_dir() -> Path:
    """리포트 산출 폴더 — 프로필별(2026-07-19 격리 감사), 실패 시 레거시 하네스."""
    try:
        from config import profile as _nbaprofile
        p = _nbaprofile.reports_dir()
    except Exception:  # noqa: BLE001
        p = Path(__file__).resolve().parent.parent.parent / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p

# 실측 승자 패턴 — 잘 알려진 고수요 제도(검색량 큼)
_CORE_TOPICS = (
    "생계급여", "주거급여", "교육급여", "의료급여", "긴급복지", "기초연금", "기초수급",
    "기초생활", "청년도약계좌", "청년내일저축", "내일채움", "보금자리론", "디딤돌", "버팀목",
    "전세자금", "청년월세", "월세지원", "근로장려금", "자녀장려금", "국민연금", "실업급여",
    "구직급여", "국민취업지원", "햇살론", "바우처", "출산", "육아휴직", "부모급여", "아동수당",
    "청년수당", "청년내일", "노령연금", "장애인연금", "한부모", "차상위",
)
# answer형 마커 — "언제·얼마·자격·비교" 검색 의도
_ANSWER = (
    "지급일", "얼마", "금액", "만원", "자격", "조건", "대상", "신청", "방법", "서류",
    "언제", "기간", "비교", "vs", "차이", "한도", "기준", "수수료", "금리", "나이",
    "소득", "얼마나", "혜택", "우선순위",
)
# 저성과 롱테일 마커(감점) — 좁은 오류대응·후기·형식적 정리
_LOW = ("오류", "안될", "안 될", "실패", "후기", "루틴", "총정리", "완벽정리", "완벽 가이드",
        "잔액조회", "결제 실패", "메시지 템플릿")


def _demand_score(title: str) -> int:
    """제목 기반 수요 추정(실측 승자 패턴). 핵심 제도 +2, answer 마커 각 +1(최대 +3), 롱테일 -1씩."""
    t = (title or "").replace(" ", "")
    score = 0
    if any(c.replace(" ", "") in t for c in _CORE_TOPICS):
        score += 2
    score += min(3, sum(1 for a in _ANSWER if a.replace(" ", "") in t))
    score -= sum(1 for w in _LOW if w.replace(" ", "") in t)
    return score


def triage_drafts(status: str = "draft_ready", *, aeo_min: int = 55,
                  demand_min: int = 2, limit: int | None = None) -> dict:
    """백로그를 품질×수요로 분류. 반환: {total, buckets, by_category, thresholds}."""
    from core.draft_store import load_drafts
    from core.aeo_scorer import score as aeo_score
    try:
        from core.engagement_scorer import score as eng_score
    except ImportError:
        eng_score = None

    drafts = load_drafts(status)
    if limit:
        drafts = drafts[:limit]
    buckets: dict[str, list] = {"publish": [], "upgrade": [], "retire": []}
    by_cat: dict[str, dict] = {}

    for d in drafts:
        pd = d.get("post_data") or {}
        title = pd.get("title") or d.get("title") or ""
        content = pd.get("content") or ""
        cat = pd.get("category") or "_uncat"
        aeo = round(aeo_score(content).score) if content else 0
        eng = round(eng_score(content).score) if (eng_score and content) else 0
        dem = _demand_score(title)

        if dem < demand_min:
            b = "retire"
        elif aeo >= aeo_min:
            b = "publish"
        else:
            b = "upgrade"

        buckets[b].append({"slug": d.get("slug"), "title": title, "category": cat,
                           "aeo": aeo, "eng": eng, "demand": dem})
        cc = by_cat.setdefault(cat, {"publish": 0, "upgrade": 0, "retire": 0})
        cc[b] += 1

    return {"total": len(drafts), "buckets": buckets, "by_category": by_cat,
            "thresholds": {"aeo_min": aeo_min, "demand_min": demand_min}}


def summarize(result: dict) -> dict:
    """버킷 카운트 요약."""
    b = result["buckets"]
    return {"total": result["total"], "publish": len(b["publish"]),
            "upgrade": len(b["upgrade"]), "retire": len(b["retire"])}


def render_report(result: dict, *, per_day: int = 8, backlog_share: int = 4) -> str:
    """선별 소진 리포트(한국어). per_day=하루 발행량, backlog_share=그중 백로그 elite 몫."""
    from datetime import date
    s = summarize(result)
    tot = s["total"] or 1
    pct = lambda n: f"{n / tot * 100:.0f}%"
    o: list[str] = []
    o.append(f"# 보관본 백로그 선별 소진 계획 — {date.today()}\n")
    o.append(f"> 총 **{s['total']:,}편**. 전량 발행 시 하루 {per_day}편 기준 "
             f"**{s['total'] // per_day}일(~{s['total'] // per_day // 30}개월)** — 그동안 개선 콘텐츠가 묻힘.\n")
    o.append("## 분류 결과 (품질 aeo × 수요 answer형)")
    o.append("| 버킷 | 편수 | 비율 | 처방 |")
    o.append("|---|---:|---:|---|")
    o.append(f"| 🟢 발행 | {s['publish']:,} | {pct(s['publish'])} | 지금 올릴 가치(수요·품질 OK) |")
    o.append(f"| 🟡 업그레이드 | {s['upgrade']:,} | {pct(s['upgrade'])} | 개선 파이프라인 재생성 후 발행(상위만) |")
    o.append(f"| 🔴 폐기/동결 | {s['retire']:,} | {pct(s['retire'])} | 저수요 롱테일 — 1위여도 유입 0, 큐에서 제외 |")
    o.append("")
    o.append("## 카테고리별 (발행 / 업그레이드 / 폐기)")
    o.append("| 카테고리 | 발행 | 업그레이드 | 폐기 |")
    o.append("|---|---:|---:|---:|")
    for cat, c in sorted(result["by_category"].items(), key=lambda x: -sum(x[1].values())):
        o.append(f"| {cat} | {c['publish']} | {c['upgrade']} | {c['retire']} |")
    o.append("")
    o.append("## 발행 버킷 상위 20 (수요·품질순)")
    o.append("| 수요 | AEO | 체류 | 카테고리 | 제목 |")
    o.append("|---:|---:|---:|---|---|")
    for x in sorted(result["buckets"]["publish"], key=lambda x: (-x["demand"], -x["aeo"]))[:20]:
        o.append(f"| {x['demand']} | {x['aeo']} | {x['eng']} | {x['category']} | {(x['title'] or '')[:40]} |")
    o.append("")
    days_pub = (s["publish"] + backlog_share - 1) // max(1, backlog_share)
    o.append("## 권장 실행 (병행 소진 — 개선 콘텐츠를 기다리게 하지 않음)")
    o.append(f"1. **🔴 폐기/동결 {s['retire']:,}편 즉시 큐 제외** → 백로그가 {s['total']:,}→{s['publish'] + s['upgrade']:,}편으로 반감. 발행 슬롯이 열림.")
    o.append(f"2. **하루 {per_day}편을 쪼갠다**: 백로그 elite {backlog_share}편 + 개선 신규/업그레이드 {per_day - backlog_share}편. "
             f"→ 개선 콘텐츠가 **오늘부터** 올라가고, 발행 버킷 {s['publish']}편은 ~{days_pub}일에 소진.")
    o.append(f"3. **🟡 업그레이드는 최상위 수요만**(예: 수요≥4) 개선 파이프라인으로 재생성 → 발행 버킷 승격. 전량 재생성 금지(비용).")
    o.append(f"4. **개선 신규는 기회 큐(고수요 answer형)로** 계속 생성 — 백로그와 병행. 순위 포화 상태이므로 '편수'보다 '수요 있는 편'.")
    o.append("\n> 실제 발행·삭제·동결은 사용자 트리거. 이 리포트는 분류·계획만 산출(부수효과 0).")
    return "\n".join(o)


def apply_freeze(result: dict, *, dry_run: bool = True, limit: int | None = None) -> dict:
    """retire 버킷을 'frozen' 상태로 마킹해 발행 큐(draft_ready)에서 제외한다.

    되돌릴 수 있음 — restore_frozen() 으로 draft_ready 복구(post_data 보존). dry_run 기본 True.
    반환: {"target","frozen","failed","dry_run"}.
    """
    from core.draft_store import mark_status
    retire = result["buckets"]["retire"]
    if limit:
        retire = retire[:limit]
    frozen = failed = 0
    for item in retire:
        slug = item.get("slug")
        if not slug:
            continue
        if dry_run:
            frozen += 1
            continue
        if mark_status(slug, "frozen"):
            frozen += 1
        else:
            failed += 1
    log.info(f"[draft_triage] {'(dry-run) ' if dry_run else ''}동결 {frozen}편 · 실패 {failed}")
    return {"target": len(retire), "frozen": frozen, "failed": failed, "dry_run": dry_run}


def restore_frozen(*, limit: int | None = None) -> dict:
    """frozen 보관본을 draft_ready 로 되돌린다(동결 취소)."""
    from core.draft_store import load_drafts, mark_status
    frozen = load_drafts("frozen")
    if limit:
        frozen = frozen[:limit]
    restored = 0
    for d in frozen:
        if mark_status(d.get("slug"), "draft_ready"):
            restored += 1
    log.info(f"[draft_triage] 동결 취소 — {restored}편 draft_ready 복구")
    return {"restored": restored}


def build_publish_plan(result: dict, *, per_day: int = 4, skip_weekends: bool = True) -> dict:
    """발행 버킷을 cadence 로 날짜×슬롯 분산(수요·품질순 → 카테고리 인터리브). 실제 예약은 사용자.

    반환: {"schedule":[{date,time,category,title,slug,score}], "days","per_day","total"}.
    """
    from core.cadence_planner import plan_cadence
    pub = sorted(result["buckets"]["publish"], key=lambda x: (-x["demand"], -x["aeo"]))
    items = [{"keyword": x["title"], "category": x["category"],
              "score": x["demand"] * 20 + x["aeo"]} for x in pub]
    title2slug = {x["title"]: x["slug"] for x in pub}
    plan = plan_cadence(items, posts_per_day=per_day, skip_weekends=skip_weekends)
    for s in plan["schedule"]:
        s["title"] = s.pop("keyword", "")
        s["slug"] = title2slug.get(s["title"], "")
    plan["total"] = len(plan["schedule"])
    return plan


def render_publish_plan(plan: dict, *, preview_days: int = 10, per_day: int = 4) -> str:
    from datetime import date
    o: list[str] = []
    o.append(f"# 발행 elite 분산 계획 (백로그 소진) — {date.today()}\n")
    o.append(f"> 발행 버킷 **{plan['total']}편**을 하루 {plan['per_day']}편으로 분산 → "
             f"**{plan['days']}일**에 소진(주말 제외). 나머지 발행 쿼터는 개선 신규에 배정.\n")
    dates = sorted({s["date"] for s in plan["schedule"]})[:preview_days]
    o.append(f"## 처음 {len(dates)}일 미리보기")
    o.append("| 날짜 | 시각 | 카테고리 | 제목 |")
    o.append("|---|---|---|---|")
    for s in [s for s in plan["schedule"] if s["date"] in dates]:
        o.append(f"| {s['date']} | {s['time']} | {s['category']} | {(s['title'] or '')[:38]} |")
    bycat = plan["summary"].get("by_category", {})
    o.append("\n## 카테고리 분포")
    o.append(" · ".join(f"{k} {v}" for k, v in sorted(bycat.items(), key=lambda x: -x[1])))
    o.append("\n> 예약 brief 는 to_reservation_briefs(plan) 로 생성. 실제 예약·발행은 사용자 트리거.")
    return "\n".join(o)


def run_publish_plan(*, per_day: int = 4, skip_weekends: bool = True):
    """발행 계획 → reports/{date}_publish_plan.md. 반환: (Path, plan)."""
    from datetime import date
    plan = build_publish_plan(triage_drafts(), per_day=per_day, skip_weekends=skip_weekends)
    harness_reports = _reports_dir()
    harness_reports.mkdir(parents=True, exist_ok=True)
    out = harness_reports / f"{date.today().strftime('%Y-%m-%d')}_publish_plan.md"
    out.write_text(render_publish_plan(plan, per_day=per_day), encoding="utf-8")
    log.info(f"[draft_triage] {out.name} — {plan['total']}편 / {plan['days']}일")
    return out, plan


def upgrade_draft(slug: str, *, min_gain: int = 5) -> dict:
    """보관본 1편을 개선된 content_generator 프롬프트로 재생성 → 재채점 → 제자리 저장.

    ⚠️ 저장 조건(둘 다 만족해야 덮어씀 — 아니면 원본 보존):
      1. 재생성 결과가 **발행 차단(QA/금지어/깡통) 아님**(_failed 플래그 없음). haiku 재생성이
         금지어로 QA 실패하는 경우가 있어 이 게이트가 필수(안 그러면 발행 불가 콘텐츠로 덮어씀).
      2. AEO 가 min_gain 이상 상승.
    슬러그·이미지 경로 보존(제자리 업데이트, draft_ready 유지). Claude 호출.
    """
    from core.draft_store import load_drafts, save_draft
    from core.content_generator import generate_blog_post
    from core.aeo_scorer import score as aeo_score
    try:
        from core.engagement_scorer import score as eng_score
    except ImportError:
        eng_score = None

    d = {x["slug"]: x for x in load_drafts("draft_ready")}.get(slug)
    if not d:
        return {"ok": False, "error": "not_found", "slug": slug}
    pd = d.get("post_data") or {}
    title = pd.get("title") or d.get("title") or ""
    before = round(aeo_score(pd.get("content") or "").score)
    new = generate_blog_post(title)
    if not new or not new.get("content"):
        return {"ok": False, "error": "gen_failed", "slug": slug, "title": title}
    # QA 게이트 — 발행 차단(_failed) 콘텐츠는 절대 저장하지 않음(원본 보존).
    if new.get("_failed"):
        return {"ok": False, "error": f"qa_failed:{new.get('_failed_reason') or ''}",
                "before": before, "after": round(aeo_score(new['content']).score),
                "slug": slug, "title": title}
    after = round(aeo_score(new["content"]).score)
    after_eng = round(eng_score(new["content"]).score) if eng_score else 0
    if after - before < min_gain:
        return {"ok": False, "error": "no_gain", "before": before, "after": after,
                "slug": slug, "title": title}
    new.setdefault("category", pd.get("category"))
    save_draft(slug, new, pd.get("image_paths") or [], d.get("schedule_at"))
    return {"ok": True, "before": before, "after": after, "eng": after_eng,
            "slug": slug, "title": title, "new_title": new.get("title")}


def upgrade_targets(*, demand_min: int = 4, max_count: int | None = None) -> list:
    """업그레이드 우선 대상(수요≥demand_min, 저품질 먼저)."""
    up = triage_drafts()["buckets"]["upgrade"]
    tg = sorted([x for x in up if x["demand"] >= demand_min],
                key=lambda x: (-x["demand"], x["aeo"]))
    return tg[:max_count] if max_count else tg


def run_upgrade_batch(max_count: int = 10, *, demand_min: int = 4,
                      dry_run: bool = False) -> dict:
    """수요 최상위 업그레이드 대상 N편을 개선 재생성(dry_run 이면 대상 목록만). Claude 호출."""
    targets = upgrade_targets(demand_min=demand_min, max_count=max_count)
    if dry_run:
        return {"count": len(targets), "dry_run": True,
                "targets": [{"slug": t["slug"], "title": t["title"],
                             "aeo": t["aeo"], "demand": t["demand"]} for t in targets]}
    results = [upgrade_draft(t["slug"]) for t in targets]
    ok = sum(1 for x in results if x.get("ok"))
    return {"count": len(targets), "upgraded": ok, "results": results}


def run_triage_report(*, per_day: int = 8, backlog_share: int = 4):
    """분류 → reports/{date}_draft_triage.md 생성. 반환: (Path, summary)."""
    from datetime import date
    result = triage_drafts()
    harness_reports = _reports_dir()
    harness_reports.mkdir(parents=True, exist_ok=True)
    out = harness_reports / f"{date.today().strftime('%Y-%m-%d')}_draft_triage.md"
    out.write_text(render_report(result, per_day=per_day, backlog_share=backlog_share),
                   encoding="utf-8")
    log.info(f"[draft_triage] {out.name} — {summarize(result)}")
    return out, summarize(result)
