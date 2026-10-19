# core/rank_alerts.py — 순위 변동 감지·알림 (순위 모니터 P0, 2026-07-19)
"""rank_history 의 전일 대비 변동을 3종 이벤트로 감지해 카카오 한 줄로 알린다.

이벤트 3종만(알림 피로 방지, 설계 원칙):
  🎉 enter_top10 — TOP10 밖(또는 미노출)에서 TOP10 진입
  ⚠️ drop        — 3계단 이상 하락
  🚨 exit_top10  — TOP10 이탈

원칙:
- 두 날짜 모두 '측정된' 기록이 있을 때만 비교 — 미측정일을 이탈로 오인하지 않는다.
- 비교는 API(blog 탭) 기록만 — 실노출(total)은 참고치라 알림 소스로 쓰지 않는다.
- 워치리스트가 있으면 그 키워드만, 비어 있으면 전체 추적 키워드 대상.
- 알림은 하루 1회 묶음 한 줄(core.notifier.send_kakao — 미설정이면 조용히 스킵).
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

DROP_STEPS = 3      # 이만큼 하락하면 ⚠️
TOP_N = 10          # 진입/이탈 기준


def _measured(hist: list, day: str) -> int | None:
    """해당 날짜의 blog 탭 측정값. 기록 없으면 None(미측정), 0=측정했지만 미노출."""
    for h in hist:
        if h.get("date") == day and h.get("tab", "blog") == "blog":
            return int(h.get("rank", 0) or 0)
    return None


def detect_changes(*, data: dict | None = None, day: str = "",
                   prev_day: str = "") -> list[dict]:
    """전일 대비 변동 이벤트 목록.

    반환: [{type, keyword, title, prev, cur}] — type: enter_top10|drop|exit_top10
    """
    from core import performance_db as pdb
    from core import rank_watchlist

    data = data or pdb.load()
    day = day or date.today().strftime("%Y-%m-%d")
    prev_day = prev_day or (
        date.fromisoformat(day) - timedelta(days=1)).strftime("%Y-%m-%d")

    watch = set(rank_watchlist.keywords())
    events = []

    def _judge(kw: str, title: str, prev, cur):
        if cur is None or prev is None:
            return    # 한쪽이라도 미측정이면 판단 보류(오탐 방지)
        in_now = 0 < cur <= TOP_N
        in_prev = 0 < prev <= TOP_N
        if in_now and not in_prev:
            events.append({"type": "enter_top10", "keyword": kw,
                           "title": title, "prev": prev, "cur": cur})
        elif in_prev and not in_now:
            events.append({"type": "exit_top10", "keyword": kw,
                           "title": title, "prev": prev, "cur": cur})
        elif cur and prev and (cur - prev) >= DROP_STEPS:
            events.append({"type": "drop", "keyword": kw,
                           "title": title, "prev": prev, "cur": cur})

    for rec in data.get("posts", {}).values():
        for kw, hist in (rec.get("rank_history") or {}).items():
            if watch and kw not in watch:
                continue
            title = rec.get("clean_title") or rec.get("title") or ""
            _judge(kw, title, _measured(hist, prev_day), _measured(hist, day))

    # 워치 자체 이력(글 앵커 없는 키워드도 알림 대상 — 2026-07-19 보강)
    seen_kw = {e["keyword"] for e in events}
    for it in rank_watchlist.items():
        kw = it.get("keyword", "")
        if not kw or kw in seen_kw:
            continue
        _judge(kw, it.get("title", ""),
               rank_watchlist._item_day(it, prev_day, "api"),
               rank_watchlist._item_day(it, day, "api"))
    return events


def _fmt_rank(r: int) -> str:
    return f"{r}위" if r else "미노출"


def format_events(events: list[dict], *, max_lines: int = 5) -> str:
    """묶음 알림 본문(사람 말 한 줄 원칙 — 항목당 한 줄, 상위 max_lines)."""
    icons = {"enter_top10": "🎉", "drop": "⚠️", "exit_top10": "🚨"}
    labels = {"enter_top10": "TOP10 진입", "drop": "하락", "exit_top10": "TOP10 이탈"}
    lines = []
    order = {"exit_top10": 0, "drop": 1, "enter_top10": 2}   # 나쁜 소식 먼저
    for e in sorted(events, key=lambda x: order.get(x["type"], 9))[:max_lines]:
        lines.append(f"{icons[e['type']]} {e['keyword']} "
                     f"{_fmt_rank(e['prev'])}→{_fmt_rank(e['cur'])} "
                     f"({labels[e['type']]})")
    if len(events) > max_lines:
        lines.append(f"… 외 {len(events) - max_lines}건")
    return "\n".join(lines)


def detect_and_notify(*, data: dict | None = None,
                      extra_lines: list[str] | None = None) -> dict:
    """변동 감지 → 있으면 카카오 한 번 묶음 발송. {events, sent}.

    extra_lines: 같은 일간 묶음에 편승할 추가 한 줄들(예: news_radar 뉴스 신호).
    순위 이벤트가 없어도 extra_lines 가 있으면 발송한다 — 별도 알림을 만들지
    않는 것(하루 1회 묶음)이 알림 피로 원칙이다.
    """
    events = detect_changes(data=data)
    extra_lines = [ln for ln in (extra_lines or []) if ln]
    if not events and not extra_lines:
        log.info("[rank_alerts] 변동 이벤트 없음")
        return {"events": 0, "sent": False}
    parts = []
    if events:
        parts.append(format_events(events))
    parts += extra_lines
    msg = "\n".join(parts)
    sent = False
    try:
        from core.notifier import send_kakao
        send_kakao("📈 순위·뉴스 신호" if extra_lines else "📈 순위 변동", msg)
        sent = True
    except Exception as e:  # noqa: BLE001 — 알림 실패는 루프에 무영향
        log.warning(f"[rank_alerts] 카카오 발송 실패(무시): {e}")
    log.info(f"[rank_alerts] 변동 {len(events)}건 · 편승 {len(extra_lines)}건\n{msg}")
    return {"events": len(events), "sent": sent}
