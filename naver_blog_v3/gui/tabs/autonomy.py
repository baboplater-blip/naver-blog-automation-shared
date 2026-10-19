# gui/tabs/autonomy.py — 무인 운영 탭 (M1~M4, 2026-09-18)
"""계정별 무인 운영 정책 설정 + 시작/정지 + 오늘 진행 현황 + 문제 확인 + 성과
수집 상태 + 작업 스케줄러 등록을 한 화면에 모은다.

계약 원본: ``.auto-dev/impl-contract-m1m4.md`` §2.11. 이 탭이 의존하는
core/scheduler 모듈(``core.autonomy_policy``·``core.work_queue``·
``core.kill_switch``·``core.draft_store``·``scheduler.task_register``·
``config.profile``)은 **다른 작업자가 동시에 구현 중**이라, 이 파일이
작성되는 시점엔 존재하지 않을 수 있다. 그래서 모든 core 접근은 지연
임포트 접근자 함수(`_mod_*`)를 거치고, `_load()` 안에서 블록별로
개별 try/except 로 감싼다 — 어떤 모듈이 없거나 예외를 던져도 그 블록만
EmptyState + 사유로 대체되고 탭 자체는 죽지 않는다(fail-open, §0.5).

⚠️ **탭이 열리는 것만으로는 아무것도 등록·발행·정지되지 않는다.**
- 정책 활성화(ON)·작업 스케줄러 등록·전체 비상정지는 전부 사용자 클릭 +
  확인 모달(`messagebox.askyesno`)을 거친 뒤에만 core 함수를 호출한다.
- 스케줄러 등록은 **항상** `dry_run=True` 미리보기를 먼저 보여주고, 사용자가
  그 미리보기 내용을 확인 모달에서 승인한 뒤에만 `dry_run=False` 로 실제
  등록한다.
- 문제 해결/수동 확인 큐에는 자동 재발행 버튼이 없다(설계 원칙 §3 —
  ``reserving``/``unconfirmed``/``missing`` 은 어떤 경로로도 자동
  재발행되지 않는다). "제목 복사" + "네이버에서 직접 확인" 안내에 더해,
  **사람이 확인한 결말을 닫는 출구**만 둔다 — "올라가 있음"(완료로 닫음) /
  "안 올라감"(다시 후보로 되돌림). 둘 다 확인 모달을 거치며, 앱이 스스로
  누르는 경로는 없다. 이 출구가 없으면 한 번 미확정이 된 글이 영원히
  막힌 채 남는다(차단만 있고 해제가 없는 상태).

스레드 위생은 이 저장소의 표준 패턴(`gui/async_utils.UiLifecycle`)을
그대로 따른다 — 워커 스레드(`self.spawn(...)`)는 core 모듈만 호출하고,
화면 반영은 전부 `self.ui_after(...)` 로 메인 스레드에 재위임한다.
"""
from __future__ import annotations

import logging
import tkinter as tk
from datetime import date, datetime, timedelta
from tkinter import messagebox
from typing import Optional

from config import safe_load_json
from gui import icons
from gui import theme as _theme
from gui.async_utils import UiLifecycle
from gui.theme import (C, FB, FS, FT, SP, DangerBtn, L, PrimaryBtn,
                       SecondaryBtn, Sep)
from gui.widgets import (EmptyState, FormField, Header, IconButton,
                         ScrollArea, SectionCard, Segmented, StatusPill,
                         Switch)

log = logging.getLogger(__name__)


def px(n: int) -> int:
    """고DPI 스케일 헬퍼 — `theme.px` 가 있으면 그걸, 없으면 항등 함수로
    폴백한다(`gui/tabs/home.py` 의 동일 이름 헬퍼와 같은 방어적 관례)."""
    fn = getattr(_theme, "px", None)
    if callable(fn):
        try:
            return fn(n)
        except Exception:  # noqa: BLE001 — px() 자체가 불안정해도 화면은 떠야 한다
            return n
    return n


# ── core/scheduler 모듈 접근자 (지연 임포트) ────────────────────────────
# 각각 함수 하나뿐이라 테스트가 `monkeypatch.setattr(autonomy, "_mod_xxx",
# lambda: fake_module)` 로 개별 교체할 수 있다. 실제 모듈이 없으면
# ImportError 가 그대로 올라가고, 호출부(`_load` 등)가 잡아서 EmptyState
# 로 바꾼다.
def _mod_autonomy_policy():
    from core import autonomy_policy
    return autonomy_policy


def _mod_work_queue():
    from core import work_queue
    return work_queue


def _mod_kill_switch():
    from core import kill_switch
    return kill_switch


def _mod_draft_store():
    from core import draft_store
    return draft_store


def _mod_task_register():
    from scheduler import task_register
    return task_register


def _mod_profile():
    from config import profile
    return profile


def _mod_autonomy_runner():
    from core import autonomy_runner
    return autonomy_runner


def _mod_autonomy_guard():
    from core import autonomy_guard
    return autonomy_guard


def _mod_autonomy_overview():
    from core import autonomy_overview
    return autonomy_overview


# ── 범위(scope) ↔ 표시 라벨 ──────────────────────────────────────────
_SCOPE_VALUES = ("off", "draft_only", "publish")
_SCOPE_LABELS = {"off": "비활성", "draft_only": "보관만", "publish": "예약 발행"}
_SCOPE_LABEL_TO_VALUE = {v: k for k, v in _SCOPE_LABELS.items()}

# 오늘 큐 3분류(§2.11-3) — "완료" 는 대조로 확보된 확정 상태만.
_DONE_STATES = ("live", "reserved")
_SCHEDULED_STATES = ("none", "selected")
# 나머지(reserving/unconfirmed/verifying/missing/failed) 는 전부 "보류".

# 문제 해결/수동 확인 큐(§2.11-5) — 자동 재발행 절대 금지 대상.
_MANUAL_CHECK_STATES = ("unconfirmed", "missing", "reserving")
_MANUAL_STATE_LABEL = {
    "unconfirmed": "확인 필요(응답 불명확)",
    "missing": "확인 필요(글이 안 보여요)",
    "reserving": "예약 처리 중",
}


# ── 순수 함수 (Tk 의존 0 — 단위 테스트 대상) ─────────────────────────
def _scope_value(policy: Optional[dict]) -> str:
    """정책 dict 로부터 현재 화면에 보여줄 범위 값(off/draft_only/publish).

    ``enabled`` 가 거짓이면 무조건 "off" — scope 필드 값과 무관하다(꺼진
    상태에서도 scope 문자열 자체는 마지막 값을 그대로 보관하고 있을 수
    있기 때문에, 화면 판정은 항상 enabled 를 먼저 본다)."""
    if not policy or not policy.get("enabled"):
        return "off"
    scope = policy.get("scope") or "draft_only"
    return scope if scope in ("draft_only", "publish") else "off"


def _categorize_queue(items: Optional[list]) -> dict:
    """work_queue 항목을 예정/완료/보류 3분류(순수 함수).

    - 완료: live/reserved(원격 대조로 확보된 확정 상태만)
    - 예정: none/selected(아직 처리 전)
    - 보류: 그 외 전부 — 사람 확인이 필요한 중간 상태(reserving·
      unconfirmed·verifying)와 실패류(missing·failed) 모두 포함.
    """
    scheduled, done, pending = [], [], []
    for it in (items or []):
        state = (it or {}).get("publish_state") or "none"
        if state in _DONE_STATES:
            done.append(it)
        elif state in _SCHEDULED_STATES:
            scheduled.append(it)
        else:
            pending.append(it)
    return {"scheduled": scheduled, "done": done, "pending": pending}


def _stats_banner_needed(capability: Optional[dict]) -> bool:
    """성과 수집이 완전히 증명되지 않았으면 True — "성과 환류 미완" 배너를
    보여줘야 한다는 뜻. 아직 한 번도 확인 안 함/전체 실패/일부 실패 모두
    True(관대하게 판정 — 증명된 것만 False로 인정)."""
    if not capability:
        return True
    if not capability.get("ok"):
        return True
    for check in (capability.get("checks") or []):
        if (check or {}).get("result") not in (True, "ok", "pass", "success"):
            return True
    return False


def _format_remaining_minutes(total_minutes: int) -> str:
    total_minutes = max(0, int(total_minutes or 0))
    if total_minutes < 60:
        return f"{total_minutes}분 후"
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours}시간 {minutes}분 후" if minutes else f"{hours}시간 후"


#: 계정이 지금 돌 수 없는 사유 → 사람이 읽는 짧은 라벨.
_ACCOUNT_BLOCK_LABEL = {
    "estop": "전체 정지 중",
    "master_off": "무인 운영 꺼짐",
    "account_off": "이 계정 꺼짐",
    "pause": "일시중지 중",
    "policy_error": "설정 확인 필요",
    "overview_error": "현황 확인 실패",
}


def _format_next_run(raw: str, note: str, *, now: Optional[datetime] = None) -> str:
    """다음 실행 표시. 값이 없으면 **사유를 그대로** 보여 준다(가짜 예약 금지)."""
    if not raw:
        return note or "예정 없음"
    try:
        when = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return note or "예정 없음"
    now = now or datetime.now()
    if when.date() == now.date():
        return f"오늘 {when.strftime('%H:%M')}"
    if when.date() == (now.date() + timedelta(days=1)):
        return f"내일 {when.strftime('%H:%M')}"
    return when.strftime("%m/%d %H:%M")


def _format_inventory(inv: Optional[dict], target, deficit) -> str:
    """재고 한 줄. 집계된 적 없으면 0 이라고 **말하지 않는다**."""
    inv = inv or {}
    ready = inv.get("ready")
    if ready is None:
        return "재고: 아직 집계 안 됨"
    text = f"재고 {ready}편"
    if isinstance(target, int) and target > 0:
        text += f" / 목표 {target}편"
        if isinstance(deficit, int) and deficit > 0:
            text += f" (부족 {deficit}편)"
    partial = inv.get("partial")
    if isinstance(partial, int) and partial > 0:
        text += f" · 이미지 일부만 {partial}편"
    if inv.get("source") == "snapshot":
        stamp = str(inv.get("as_of") or "")[5:16].replace("T", " ")
        text += f" · {stamp} 집계" + (" (오래됨)" if inv.get("stale") else "")
    return text


def _format_account_line(acc: dict) -> str:
    """계정 한 줄 요약(순수 함수 — 단위 테스트 대상)."""
    parts = [f"다음 실행: {_format_next_run(acc.get('next_run') or '', acc.get('next_run_note') or '')}"]
    parts.append(_format_inventory(acc.get("inventory"), acc.get("inventory_target"),
                                   acc.get("inventory_deficit")))
    budget = acc.get("budget") or {}
    left = budget.get("write_left")
    if left is not None:
        parts.append(f"남은 집필 호출 {left}회")
    manual = acc.get("manual_checks")
    if manual:
        parts.append(f"확인 필요 {manual}편")
    stats = acc.get("stats") or {}
    if stats.get("proven") is not True:
        parts.append("성과 수집 보류" + (f"({stats['reason']})" if stats.get("reason") else ""))
    last = acc.get("last_run") or {}
    if last.get("at"):
        stamp = str(last["at"])[5:16].replace("T", " ")
        mark = "정상" if last.get("ok") else "실패"
        parts.append(f"마지막 실행 {stamp} {mark}")
    else:
        parts.append("마지막 실행 기록 없음")
    return " · ".join(parts)


def _format_activation_confirm(masked: str, policy: dict, new_scope: str) -> str:
    """무인 운영을 켤 때 보여줄 확인 모달 본문(순수 함수 — 단위 테스트 대상).

    대상 계정·하루 편수·준비 시각·일일 호출 상한·정지 방법을 전부 담는다
    (§2.11-1 요구사항 그대로)."""
    policy = policy or {}
    daily = policy.get("daily_posts", 2)
    windows = ", ".join(policy.get("windows") or []) or "(설정 안 됨)"
    budget = policy.get("budget") or {}
    write_calls = budget.get("write_calls", 0)
    image_calls = budget.get("image_calls", 0)
    return (
        f"계정: {masked}\n"
        f"범위: {_SCOPE_LABELS.get(new_scope, new_scope)}\n"
        f"하루 편수: {daily}편\n"
        f"준비 시각: {windows}\n"
        f"일일 호출 상한: 집필 {write_calls}회 · 이미지 {image_calls}회\n\n"
        "켜는 순간 일일 호출 상한·브라우저 중재·중복 발행 차단이 자동으로 함께 적용돼요.\n\n"
        "정지 방법: 아래 '운영 시작/정지' 섹션의 스위치로 언제든 멈출 수 있어요.\n\n"
        "이 설정으로 무인 운영을 켤까요?"
    )


class AutonomyTab(UiLifecycle, tk.Frame):
    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self._init_lifecycle()
        self.app = app

        self._policy: dict = {}
        self._masked_id: Optional[str] = None
        self._profile_name: str = ""

        self._scope_seg: Optional[Segmented] = None
        self._pause_switch: Optional[Switch] = None
        self._estop_switch: Optional[Switch] = None

        self._daily_posts_var = tk.IntVar(value=2)
        self._windows_var = tk.StringVar(value="")
        self._inventory_var = tk.IntVar(value=5)
        self._write_budget_var = tk.IntVar(value=8)
        self._image_budget_var = tk.IntVar(value=12)

        self._build_ui()
        self.ui_after(self.refresh, delay=200)

    # ─────────────────────────────────────────────
    # 골격
    # ─────────────────────────────────────────────
    def _build_ui(self) -> None:
        hdr = Header(self, "무인 운영", icon=icons.glyph("run"))
        hdr.pack(fill="x", padx=SP["xl"], pady=(SP["md"], 0))
        IconButton(hdr.actions, "refresh", self.refresh,
                  tooltip="새로고침").pack(side="right")
        Sep(self).pack(fill="x", pady=(SP["sm"], 0))

        scroll = ScrollArea(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.inner

        account_card = SectionCard(body, "계정별 설정", icon=icons.glyph("user"))
        account_card.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["sm"]))
        self._account_body = account_card.body

        kill_card = SectionCard(body, "운영 시작/정지", icon=icons.glyph("stop"))
        kill_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        self._kill_body = kill_card.body

        queue_card = SectionCard(body, "오늘 예정 · 완료 · 보류", icon=icons.glyph("clock"))
        queue_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        self._queue_today_body = queue_card.body

        next_card = SectionCard(body, "다음 준비", icon=icons.glyph("clock"))
        next_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        self._next_body = next_card.body

        accounts_card = SectionCard(body, "전체 계정 운영 현황",
                                    icon=icons.glyph("user"))
        accounts_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        self._accounts_body = accounts_card.body

        manual_card = SectionCard(body, "문제 해결 · 수동 확인 큐", icon=icons.glyph("warning"))
        manual_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        self._manual_body = manual_card.body

        stats_card = SectionCard(body, "성과 수집 상태", icon=icons.glyph("stats"))
        stats_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["sm"]))
        self._stats_body = stats_card.body

        scheduler_card = SectionCard(body, "작업 스케줄러 등록", icon=icons.glyph("save"))
        scheduler_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        self._scheduler_body = scheduler_card.body

    # ─────────────────────────────────────────────
    # 알림 헬퍼(홈 탭과 같은 관례 — toast 우선, 없으면 messagebox 폴백)
    # ─────────────────────────────────────────────
    def _toast(self, text: str, kind: str = "info") -> None:
        toast = getattr(self.app, "toast", None)
        if toast is not None and hasattr(toast, "show"):
            try:
                toast.show(text, kind=kind)
                return
            except Exception:  # noqa: BLE001 — 토스트 실패는 messagebox 로 폴백
                pass
        try:
            if kind in ("error", "warn"):
                messagebox.showwarning("알림", text)
            else:
                messagebox.showinfo("알림", text)
        except tk.TclError:
            pass

    # ─────────────────────────────────────────────
    # 데이터 적재(백그라운드) — 블록별 개별 try/except, 실패는 EmptyState 로.
    # ─────────────────────────────────────────────
    def refresh(self) -> None:
        self.spawn(self._load)

    def _load(self) -> None:
        d: dict = {}

        # 프로필/계정 마스킹
        profile_name = ""
        try:
            pmod = _mod_profile()
            profile_name = pmod.active_profile()
            info = pmod.describe(profile_name)
            d["masked_id"] = info.get("naver_id_masked") or "(계정 없음)"
        except Exception as e:  # noqa: BLE001
            d["masked_id"] = None
            d["profile_error"] = str(e)
        d["profile_name"] = profile_name

        # 정책
        policy: Optional[dict] = None
        try:
            apol = _mod_autonomy_policy()
            policy = apol.load_policy(profile=profile_name)
            d["policy"] = policy
        except Exception as e:  # noqa: BLE001
            d["policy"] = None
            d["policy_error"] = str(e)

        # 준비 상태(필수 보호 포함) — 원장 왕복 없이(probe=False) 가볍게만 확인한다.
        # 이미 로드한 `policy` 를 그대로 넘겨 `autonomy_guard.readiness()` 가 정책을
        # 다시 읽지 않게 한다(그러면 GUI 접근자가 fake 로 바뀐 테스트에서도 실제
        # `config.profile` 을 다시 타는 일이 없다).
        if policy is not None:
            try:
                guard = _mod_autonomy_guard()
                d["readiness"] = guard.readiness(profile=profile_name, policy=policy,
                                                 probe=False)
            except Exception as e:  # noqa: BLE001
                d["readiness"] = None
                d["readiness_error"] = str(e)
        else:
            d["readiness"] = None

        # 다음 준비 슬롯(정책이 있어야 계산 가능)
        if policy is not None:
            try:
                apol = _mod_autonomy_policy()
                slots = apol.slots_for_day(policy, date.today()) or []
                now = datetime.now()
                upcoming = sorted(s for s in slots if s > now)
                d["next_slot"] = upcoming[0] if upcoming else None
            except Exception as e:  # noqa: BLE001
                d["next_slot"] = None
                d["next_slot_error"] = str(e)
        else:
            d["next_slot"] = None

        # 정지 스위치 상태
        try:
            ks = _mod_kill_switch()
            d["kill_status"] = ks.status(profile=profile_name)
        except Exception as e:  # noqa: BLE001
            d["kill_status"] = None
            d["kill_error"] = str(e)

        # 오늘 작업 큐
        try:
            wq = _mod_work_queue()
            today_str = date.today().strftime("%Y-%m-%d")
            d["queue_items"] = wq.list_items(day=today_str)
        except Exception as e:  # noqa: BLE001
            d["queue_items"] = None
            d["queue_error"] = str(e)

        # 완성 재고 — 판정을 여기서 복제하지 않고 발행 게이트와 같은 함수를 쓴다
        # (2026-09-18: 예전엔 image_paths 가 한 줄이라도 있으면 완성으로 셌다).
        inv_status: Optional[dict] = None
        try:
            from core import autonomy_runner as _ar
            inv_status = _ar.inventory_status() or {}
            d["inventory"] = int(inv_status.get("ready_with_images") or 0)
            d["inventory_partial"] = int(inv_status.get("partial_images") or 0)
        except Exception as e:  # noqa: BLE001
            d["inventory"] = None
            d["inventory_error"] = str(e)

        # 전체 계정 현황 — 파일만 읽는다(네트워크·브라우저·모델 호출 0회).
        # 활성 계정의 재고는 방금 센 값을 그대로 넘겨 **두 번 세지 않는다**.
        try:
            ov = _mod_autonomy_overview()
            live_fn = (lambda: dict(inv_status)) if isinstance(inv_status, dict) else None
            d["overview"] = ov.overview(live_inventory_fn=live_fn)
        except Exception as e:  # noqa: BLE001
            d["overview"] = None
            d["overview_error"] = str(e)

        # 성과 수집 capability 증거
        try:
            pmod = _mod_profile()
            logs_dir = pmod.logs_dir(profile_name)
            d["stats_capability"] = safe_load_json(
                logs_dir / "stats_capability.json", default=None)
        except Exception as e:  # noqa: BLE001
            d["stats_capability"] = None
            d["stats_error"] = str(e)

        # 작업 스케줄러 등록 상태
        try:
            tr = _mod_task_register()
            d["task_status"] = tr.check_autonomy_tasks()
        except Exception as e:  # noqa: BLE001
            d["task_status"] = None
            d["task_error"] = str(e)

        self.ui_after(self._apply, d)

    # ─────────────────────────────────────────────
    # 화면 반영(메인 스레드)
    # ─────────────────────────────────────────────
    def _apply(self, d: dict) -> None:
        self._masked_id = d.get("masked_id")
        self._profile_name = d.get("profile_name") or self._profile_name
        self._render_account_section(d)
        self._render_kill_switch_section(d)
        self._render_queue_today_section(d)
        self._render_next_prep_section(d)
        self._render_accounts_section(d)
        self._render_manual_queue_section(d)
        self._render_stats_section(d)
        self._render_scheduler_section(d)

    def _clear(self, frame) -> None:
        for w in frame.winfo_children():
            w.destroy()

    # ── ① 계정별 설정 ─────────────────────────────
    def _render_account_section(self, d: dict) -> None:
        body = self._account_body
        self._clear(body)
        policy = d.get("policy")
        if policy is None:
            EmptyState(body, "warning", "무인 운영 설정을 불러올 수 없어요",
                      hint=(d.get("policy_error")
                            or "core.autonomy_policy 모듈을 찾을 수 없어요.")).pack(fill="x")
            self._scope_seg = None
            return

        self._policy = policy
        masked = d.get("masked_id") or "(계정 없음)"
        L(body, f"현재 계정: {masked}", FB, C["text_2"]).pack(anchor="w", pady=(0, SP["sm"]))

        readiness = d.get("readiness")
        if readiness is None:
            L(body, "준비 상태를 확인하지 못했어요.", FS, C["muted"]).pack(
                anchor="w", pady=(0, SP["sm"]))
        elif readiness.get("ok"):
            StatusPill(body, "무인 운영 준비됨", kind="ok").pack(
                anchor="w", pady=(0, SP["sm"]))
        else:
            StatusPill(body, "아직 준비되지 않음", kind="warn").pack(anchor="w")
            for blocker in (readiness.get("blockers") or [])[:3]:
                text = blocker.get("detail") or ""
                if blocker.get("fix"):
                    text = f"{text} — {blocker['fix']}"
                if text:
                    L(body, text, FS, C["text_2"], anchor="w", justify="left",
                      wraplength=px(560)).pack(anchor="w", pady=(px(2), SP["xs"]))

        seg_row = tk.Frame(body, bg=C["card"])
        seg_row.pack(anchor="w", pady=(0, SP["sm"]))
        L(seg_row, "운영 범위", FB, C["text"]).pack(side="left", padx=(0, SP["sm"]))
        self._scope_seg = Segmented(
            seg_row, [_SCOPE_LABELS[v] for v in _SCOPE_VALUES],
            command=self._on_scope_change)
        self._scope_seg.pack(side="left")
        self._scope_seg.set(_SCOPE_LABELS[_scope_value(policy)])

        self._daily_posts_var.set(int(policy.get("daily_posts", 2) or 0))
        self._windows_var.set(", ".join(policy.get("windows") or []))
        self._inventory_var.set(int(policy.get("inventory_days", 5) or 0))
        budget = policy.get("budget") or {}
        self._write_budget_var.set(int(budget.get("write_calls", 8) or 0))
        self._image_budget_var.set(int(budget.get("image_calls", 12) or 0))

        FormField(body, "하루 편수", self._daily_posts_var, kind="spin",
                 values=(0, 20), width=6).pack(fill="x", pady=SP["xs"])
        FormField(body, "예약 준비 시각", self._windows_var, kind="entry",
                 hint="쉼표로 구분해서 입력해요. 예: 07:30, 19:00",
                 width=32).pack(fill="x", pady=SP["xs"])
        FormField(body, "완성 재고 일수", self._inventory_var, kind="spin",
                 values=(0, 30), width=6).pack(fill="x", pady=SP["xs"])
        FormField(body, "일일 호출 상한 · 집필", self._write_budget_var, kind="spin",
                 values=(0, 50), width=6).pack(fill="x", pady=SP["xs"])
        FormField(body, "일일 호출 상한 · 이미지", self._image_budget_var, kind="spin",
                 values=(0, 50), width=6).pack(fill="x", pady=SP["xs"])

        btn_row = tk.Frame(body, bg=C["card"])
        btn_row.pack(anchor="e", pady=(SP["sm"], 0))
        PrimaryBtn(btn_row, "저장", self._on_save_fields_click,
                  icon="save").pack(side="right")

    def _render_scope_only(self) -> None:
        """확인 모달 취소·저장 실패 시 세그먼트 표시만 실제 정책 값으로 되돌린다
        (전체 섹션을 다시 그리지 않는다 — 입력 중인 다른 필드 값을 건드리지
        않기 위함)."""
        if self._scope_seg is None:
            return
        try:
            self._scope_seg.set(_SCOPE_LABELS[_scope_value(self._policy)])
        except tk.TclError:
            pass

    def _on_scope_change(self, label: str) -> None:
        new_value = _SCOPE_LABEL_TO_VALUE.get(label, "off")
        current = _scope_value(self._policy)
        if new_value == current:
            return
        if new_value == "off":
            self.spawn(self._deactivate_worker)
            return
        masked = self._masked_id or "(계정 없음)"
        msg = _format_activation_confirm(masked, self._policy or {}, new_value)
        if not messagebox.askyesno("무인 운영 켜기", msg):
            self._render_scope_only()
            return
        self.spawn(self._activate_worker, new_value)

    def _deactivate_worker(self) -> None:
        try:
            mod = _mod_autonomy_policy()
            policy = mod.deactivate(profile=self._profile_name)
            ok, err = True, ""
        except Exception as e:  # noqa: BLE001
            policy, ok, err = None, False, str(e)
        self.ui_after(self._after_scope_change, ok, policy, err)

    def _activate_worker(self, new_value: str) -> None:
        try:
            mod = _mod_autonomy_policy()
            policy = mod.activate(scope=new_value, by="gui", profile=self._profile_name)
            ok, err = True, ""
            if not policy.get("master_ok", True):
                # 계정 정책은 저장됐지만 마스터 스위치를 못 켰다 — "화면엔 켜짐인데
                # 실제로는 안 도는" 상태를 만들지 않도록 계정 쪽도 함께 되돌린다.
                err = policy.get("master_error") or "무인 운영 마스터 스위치를 켜지 못했어요."
                ok = False
                try:
                    policy = mod.deactivate(profile=self._profile_name)
                except Exception:  # noqa: BLE001 — 되돌리기 실패해도 실패 사유는 그대로 알린다
                    pass
        except Exception as e:  # noqa: BLE001
            policy, ok, err = None, False, str(e)
        self.ui_after(self._after_scope_change, ok, policy, err)

    def _after_scope_change(self, ok: bool, policy: Optional[dict], err: str) -> None:
        if not ok:
            log.warning(f"[autonomy] 범위 전환 실패: {err}")
            self._toast(f"무인 운영 설정을 바꾸지 못했어요: {err}", kind="warn")
            self._render_scope_only()
            return
        if policy:
            self._policy = policy
        self._render_scope_only()
        self._toast("무인 운영 설정을 저장했어요.", kind="info")

    def _on_save_fields_click(self) -> None:
        try:
            daily = int(self._daily_posts_var.get())
            inventory = int(self._inventory_var.get())
            write_budget = int(self._write_budget_var.get())
            image_budget = int(self._image_budget_var.get())
        except (tk.TclError, ValueError):
            self._toast("숫자 항목을 확인해 주세요.", kind="warn")
            return
        windows = [w.strip() for w in self._windows_var.get().split(",") if w.strip()]
        self.spawn(self._save_fields_worker, daily, windows, inventory,
                   write_budget, image_budget)

    def _save_fields_worker(self, daily: int, windows: list, inventory: int,
                            write_budget: int, image_budget: int) -> None:
        try:
            mod = _mod_autonomy_policy()
            base = dict(self._policy) if self._policy else {}
            base["daily_posts"] = daily
            base["windows"] = windows
            base["inventory_days"] = inventory
            budget = dict(base.get("budget") or {})
            budget["write_calls"] = write_budget
            budget["image_calls"] = image_budget
            base["budget"] = budget
            mod.save_policy(base, profile=self._profile_name)
            ok, err = True, ""
        except Exception as e:  # noqa: BLE001
            ok, err = False, str(e)
        self.ui_after(self._after_save_fields, ok, err)

    def _after_save_fields(self, ok: bool, err: str) -> None:
        if not ok:
            log.warning(f"[autonomy] 설정 저장 실패: {err}")
            self._toast(f"설정을 저장하지 못했어요: {err}", kind="warn")
            return
        self._toast("설정을 저장했어요.", kind="info")
        self.refresh()

    # ── ② 운영 시작/정지 ──────────────────────────
    def _render_kill_switch_section(self, d: dict) -> None:
        body = self._kill_body
        self._clear(body)
        status = d.get("kill_status")
        if status is None:
            EmptyState(body, "warning", "정지 상태를 불러올 수 없어요",
                      hint=(d.get("kill_error")
                            or "core.kill_switch 모듈을 찾을 수 없어요.")).pack(fill="x")
            self._pause_switch = None
            self._estop_switch = None
            return

        estop = bool(status.get("estop"))
        pause = bool(status.get("pause"))
        if estop:
            StatusPill(body, "비상정지 중", kind="error").pack(anchor="w")
        elif pause:
            StatusPill(body, "일시중지 중", kind="warn").pack(anchor="w")
        else:
            StatusPill(body, "정상 운영", kind="ok").pack(anchor="w")

        self._pause_switch = Switch(body, text="이 계정 일시중지",
                                    command=self._on_pause_toggle)
        self._pause_switch.set(pause)
        self._pause_switch.pack(anchor="w", pady=(SP["sm"], 0))

        self._estop_switch = Switch(body, text="전체 비상정지",
                                    command=self._on_estop_toggle)
        self._estop_switch.set(estop)
        self._estop_switch.pack(anchor="w", pady=(SP["xs"], 0))

    def _on_pause_toggle(self) -> None:
        if self._pause_switch is None:
            return
        val = self._pause_switch.get()
        self.spawn(self._set_pause_worker, val)

    def _set_pause_worker(self, val: bool) -> None:
        try:
            mod = _mod_kill_switch()
            mod.set_pause(val, reason="gui" if val else "", profile=self._profile_name)
            ok = True
        except Exception as e:  # noqa: BLE001
            log.warning(f"[autonomy] 일시중지 전환 실패: {e}")
            ok = False
        self.ui_after(self._after_kill_switch_change, ok)

    def _on_estop_toggle(self) -> None:
        if self._estop_switch is None:
            return
        val = self._estop_switch.get()
        if val:
            if not messagebox.askyesno(
                    "전체 비상정지",
                    "모든 계정의 무인 운영을 즉시 멈춰요. 계속할까요?"):
                self._estop_switch.set(False)
                return
        self.spawn(self._set_estop_worker, val)

    def _set_estop_worker(self, val: bool) -> None:
        try:
            mod = _mod_kill_switch()
            mod.set_estop(val, reason="gui" if val else "", by="gui")
            ok = True
        except Exception as e:  # noqa: BLE001
            log.warning(f"[autonomy] 비상정지 전환 실패: {e}")
            ok = False
        self.ui_after(self._after_kill_switch_change, ok)

    def _after_kill_switch_change(self, ok: bool) -> None:
        if not ok:
            self._toast("정지 스위치 상태를 바꾸지 못했어요.", kind="warn")
        self.refresh()

    # ── ③ 오늘 예정/완료/보류 ─────────────────────
    def _render_queue_today_section(self, d: dict) -> None:
        body = self._queue_today_body
        self._clear(body)
        items = d.get("queue_items")
        if items is None:
            EmptyState(body, "warning", "오늘 큐를 불러올 수 없어요",
                      hint=(d.get("queue_error")
                            or "core.work_queue 모듈을 찾을 수 없어요.")).pack(fill="x")
            return

        cats = _categorize_queue(items)
        row = tk.Frame(body, bg=C["card"])
        row.pack(fill="x")
        for key, label_text, kind in (("scheduled", "예정", "neutral"),
                                      ("done", "완료", "ok"),
                                      ("pending", "보류", "warn")):
            cell = tk.Frame(row, bg=C["card"])
            cell.pack(side="left", padx=(0, SP["lg"]), anchor="n")
            L(cell, label_text, FS, C["muted"]).pack(anchor="w")
            StatusPill(cell, f"{len(cats[key])}건", kind=kind).pack(
                anchor="w", pady=(px(2), SP["xs"]))
            for it in cats[key][:5]:
                title = (it or {}).get("title") or "(제목 없음)"
                L(cell, title[:24], FS, C["text_2"], anchor="w").pack(anchor="w")
            if len(cats[key]) > 5:
                L(cell, f"+{len(cats[key]) - 5}건", FS, C["muted"]).pack(anchor="w")

        if not items:
            L(body, "오늘 예정된 항목이 없어요.", FS, C["text_2"]).pack(
                anchor="w", pady=(SP["sm"], 0))

    # ── ④ 다음 준비 ────────────────────────────────
    def _render_next_prep_section(self, d: dict) -> None:
        body = self._next_body
        self._clear(body)

        next_slot = d.get("next_slot")
        if next_slot is not None:
            remaining_min = int(max(0, (next_slot - datetime.now()).total_seconds() // 60))
            L(body, f"다음 준비: {next_slot.strftime('%H:%M')} · "
                    f"{_format_remaining_minutes(remaining_min)}",
              FB, C["text"]).pack(anchor="w")
        elif d.get("policy") is None:
            L(body, "설정을 불러오지 못해 다음 준비를 계산할 수 없어요.",
              FS, C["text_2"]).pack(anchor="w")
        else:
            L(body, "오늘 이후 예정된 준비가 없어요.", FB, C["text_2"]).pack(anchor="w")

        inv = d.get("inventory")
        if inv is None:
            L(body, "완성 재고를 확인하지 못했어요.", FS, C["muted"]).pack(
                anchor="w", pady=(SP["xs"], 0))
        elif inv <= 0:
            row = tk.Frame(body, bg=C["card"])
            row.pack(anchor="w", pady=(SP["xs"], 0))
            StatusPill(row, "재고 부족", kind="warn").pack(side="left", padx=(0, SP["sm"]))
            L(row, "이미지가 전부 채워진 완성 글이 없어요.", FS, C["text_2"]).pack(side="left")
            _partial = int(d.get("inventory_partial") or 0)
            if _partial:
                L(body, f"이미지가 일부만 있는 글 {_partial}편 — 빠진 자리를 채우면 재고가 돼요.",
                  FS, C["muted"]).pack(anchor="w")
        else:
            L(body, f"완성 재고 {inv}편(이미지 전부 채워짐)", FB, C["text_2"]).pack(
                anchor="w", pady=(SP["xs"], 0))
            _partial = int(d.get("inventory_partial") or 0)
            if _partial:
                L(body, f"이미지가 일부만 있는 글 {_partial}편은 재고에 넣지 않았어요.",
                  FS, C["muted"]).pack(anchor="w")

    # ── ④-b 전체 계정 운영 현황 ───────────────────
    def _render_accounts_section(self, d: dict) -> None:
        body = self._accounts_body
        self._clear(body)
        data = d.get("overview")
        if data is None:
            EmptyState(body, "warning", "전체 계정 현황을 불러올 수 없어요",
                      hint=(d.get("overview_error")
                            or "core.autonomy_overview 모듈을 찾을 수 없어요.")).pack(fill="x")
            return

        accounts = data.get("accounts") or []
        if not accounts:
            EmptyState(body, "info", "등록된 계정이 없어요",
                      hint="계정 관리에서 블로그 계정을 먼저 추가하세요.").pack(fill="x")
            return

        totals = data.get("totals") or {}
        head = (f"계정 {totals.get('accounts', 0)}개 · 켜짐 {totals.get('enabled', 0)}개 · "
                f"지금 돌 수 있음 {totals.get('eligible', 0)}개")
        if totals.get("needs_user_action"):
            head += f" · 확인 필요 {totals['needs_user_action']}개"
        L(body, head, FB, C["text"]).pack(anchor="w", pady=(0, SP["xs"]))
        if data.get("estop"):
            StatusPill(body, "전체 비상정지 중", kind="error").pack(anchor="w",
                                                              pady=(0, SP["xs"]))
        elif not data.get("master"):
            StatusPill(body, "무인 운영 꺼짐", kind="neutral").pack(anchor="w",
                                                              pady=(0, SP["xs"]))

        for acc in accounts[:12]:
            self._render_one_account(body, acc)
        if len(accounts) > 12:
            L(body, f"+{len(accounts) - 12}개 계정 더", FS, C["muted"]).pack(anchor="w")

    def _render_one_account(self, parent, acc: dict) -> None:
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=(0, SP["sm"]))

        head = tk.Frame(row, bg=C["card"])
        head.pack(fill="x")
        if acc.get("eligible"):
            kind, pill = "ok", _SCOPE_LABELS.get(acc.get("scope") or "", "켜짐")
        elif acc.get("block_reason") in ("policy_error", "overview_error"):
            kind, pill = "error", "설정 확인 필요"
        else:
            kind, pill = "neutral", _ACCOUNT_BLOCK_LABEL.get(
                acc.get("block_reason") or "", "비활성")
        StatusPill(head, pill, kind=kind).pack(side="left", padx=(0, SP["sm"]))
        L(head, acc.get("profile") or "(이름 없음)", FB, C["text"]).pack(side="left")
        if acc.get("is_active_profile"):
            L(head, "  (지금 보고 있는 계정)", FS, C["muted"]).pack(side="left")

        L(row, _format_account_line(acc), FS, C["text_2"], anchor="w",
          justify="left", wraplength=px(620)).pack(anchor="w")

        if acc.get("user_action"):
            L(row, f"할 일: {acc['user_action']}", FS, C["orange"], anchor="w",
              justify="left", wraplength=px(620)).pack(anchor="w")
        elif acc.get("auto_recovers"):
            L(row, f"자동 처리: {acc['auto_recovers']}", FS, C["muted"], anchor="w",
              justify="left", wraplength=px(620)).pack(anchor="w")
        for err in (acc.get("errors") or [])[:2]:
            L(row, f"문제: {err}", FS, C["orange"], anchor="w", justify="left",
              wraplength=px(620)).pack(anchor="w")

    # ── ⑤ 문제 해결 / 수동 확인 큐 ────────────────
    def _render_manual_queue_section(self, d: dict) -> None:
        body = self._manual_body
        self._clear(body)
        items = d.get("queue_items")
        if items is None:
            EmptyState(body, "warning", "수동 확인 큐를 불러올 수 없어요",
                      hint=(d.get("queue_error")
                            or "core.work_queue 모듈을 찾을 수 없어요.")).pack(fill="x")
            return

        manual = [it for it in items
                 if ((it or {}).get("publish_state") or "") in _MANUAL_CHECK_STATES]
        if not manual:
            EmptyState(body, "success", "확인이 필요한 항목이 없어요").pack(fill="x")
            return

        for it in manual[:20]:
            title = it.get("title") or "(제목 없음)"
            state = it.get("publish_state") or ""
            reason = it.get("reason") or _MANUAL_STATE_LABEL.get(state, state)
            row = tk.Frame(body, bg=C["card"])
            row.pack(fill="x", pady=(0, SP["xs"]))
            L(row, title, FB, C["text"], anchor="w", justify="left",
              wraplength=px(480)).pack(anchor="w")
            L(row, f"{_MANUAL_STATE_LABEL.get(state, state)} · {reason} · "
                   "네이버에서 직접 확인해 주세요.",
              FS, C["text_2"], anchor="w", justify="left",
              wraplength=px(480)).pack(anchor="w")
            actions = tk.Frame(row, bg=C["card"])
            actions.pack(anchor="w", pady=(px(2), 0))
            SecondaryBtn(actions, "제목 복사",
                         lambda t=title: self._copy_title(t)).pack(side="left")
            # 사람이 네이버에서 확인한 뒤 결말을 닫는 **유일한 출구**.
            # 자동 재발행 버튼이 아니다 — 사람이 확인 결과를 알려주는 것이며,
            # '올라가지 않음'을 고른 글만 다시 후보로 돌아간다.
            item_id = it.get("item_id") or ""
            SecondaryBtn(actions, "올라가 있음",
                         lambda i=item_id, t=title:
                         self._resolve_manual(i, t, "published")).pack(
                side="left", padx=(SP["xs"], 0))
            SecondaryBtn(actions, "안 올라감",
                         lambda i=item_id, t=title:
                         self._resolve_manual(i, t, "not_published")).pack(
                side="left", padx=(SP["xs"], 0))
        if len(manual) > 20:
            L(body, f"+{len(manual) - 20}건 더", FS, C["muted"]).pack(anchor="w")

    def _resolve_manual(self, item_id: str, title: str, outcome: str) -> None:
        """사람이 확인한 결말을 닫는다(수동 확인 큐의 출구)."""
        if not item_id:
            self._toast("항목을 찾을 수 없어요.", kind="error")
            return
        if outcome == "published":
            msg = (f"'{title[:40]}' 글이 네이버에 올라가 있다고 확인하셨나요?\n\n"
                   "확인 완료로 닫고 발행된 글로 기록합니다.")
        else:
            msg = (f"'{title[:40]}' 글이 네이버에 올라가지 않았다고 확인하셨나요?\n\n"
                   "확인 완료로 닫고, 이 글을 다시 후보로 되돌립니다"
                   "(다음 실행에서 다시 시도될 수 있습니다).")
        if not messagebox.askyesno("확인 결과 기록", msg):
            return
        try:
            runner = _mod_autonomy_runner()
            result = runner.resolve_manual(item_id, outcome=outcome, by="gui")
        except Exception as exc:  # noqa: BLE001
            self._toast(f"기록하지 못했어요({type(exc).__name__}).", kind="error")
            return
        if result.get("ok"):
            self._toast("확인 결과를 기록했어요.", kind="success")
            self._load()
        else:
            self._toast(result.get("reason") or "기록하지 못했어요.", kind="error")

    def _copy_title(self, title: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(title)
        except tk.TclError:
            pass
        self._toast("제목을 복사했어요.", kind="info")

    # ── ⑥ 성과 수집 상태 ──────────────────────────
    def _render_stats_section(self, d: dict) -> None:
        body = self._stats_body
        self._clear(body)
        cap = d.get("stats_capability")

        if _stats_banner_needed(cap):
            L(body, "성과 환류 미완(수집 일부 수동)", FB, C["orange"], anchor="w",
              justify="left", wraplength=px(680)).pack(anchor="w", pady=(0, SP["xs"]))

        if not cap:
            L(body, "아직 성과 수집 상태를 확인하지 않았어요.", FS,
              C["text_2"]).pack(anchor="w")
        else:
            checks = cap.get("checks") or []
            if not checks:
                L(body, "확인된 항목이 없어요.", FS, C["text_2"]).pack(anchor="w")
            else:
                needs_login = False
                for chk in checks:
                    ok = chk.get("result") in (True, "ok", "pass", "success")
                    if chk.get("result") == "needs_login":
                        needs_login = True
                    row = tk.Frame(body, bg=C["card"])
                    row.pack(fill="x", pady=(0, px(2)))
                    StatusPill(row, "확인됨" if ok else "미확인",
                              kind="ok" if ok else "warn").pack(side="left")
                    L(row, f"  {chk.get('name', '')} · {chk.get('detail', '')}",
                      FS, C["text_2"]).pack(side="left")
                if needs_login:
                    L(body, "네이버 로그인(쿠키 갱신)이 필요해요.", FB, C["orange"],
                      anchor="w").pack(anchor="w", pady=(SP["xs"], 0))

        btn_row = tk.Frame(body, bg=C["card"])
        btn_row.pack(anchor="w", pady=(SP["sm"], 0))
        SecondaryBtn(btn_row, "지금 다시 확인", self._on_refresh_stats_click,
                    icon="refresh").pack(side="left")

    def _on_refresh_stats_click(self) -> None:
        self.spawn(self._refresh_stats_worker)

    def _refresh_stats_worker(self) -> None:
        blog_id = ""
        try:
            runner = _mod_autonomy_runner()
            blog_id = runner.blog_id_for_stats()
        except Exception as e:  # noqa: BLE001
            log.warning(f"[autonomy] 블로그 ID 조회 실패: {e}")

        needs_login = False
        error = ""
        try:
            from core import stats_fetcher as _sf
            # 사람이 명시로 누른 재확인이므로 직전 실패의 재시도 대기를 무시하고
            # 실제로 한 번 조회한다(자동 경로는 계속 대기를 존중한다).
            cap = _sf.probe_stats_capability(blog_id=blog_id, force=True)
            needs_login = any(
                (chk or {}).get("result") == "needs_login"
                for chk in (cap.get("checks") or []))
        except Exception as e:  # noqa: BLE001
            error = str(e)
            log.warning(f"[autonomy] 성과 수집 상태 확인 실패: {e}")
        self.ui_after(self._after_refresh_stats, needs_login, error)

    def _after_refresh_stats(self, needs_login: bool, error: str) -> None:
        if error:
            self._toast(f"확인하지 못했어요({error}).", kind="warn")
        elif needs_login:
            self._toast("네이버 로그인(쿠키 갱신)이 필요해요.", kind="warn")
        else:
            self._toast("성과 수집 상태를 다시 확인했어요.", kind="info")
        self.refresh()

    # ── ⑦ 작업 스케줄러 등록/해제 ─────────────────
    def _render_scheduler_section(self, d: dict) -> None:
        body = self._scheduler_body
        self._clear(body)
        status = d.get("task_status")
        if status is None:
            EmptyState(body, "warning", "작업 스케줄러 상태를 불러올 수 없어요",
                      hint=(d.get("task_error")
                            or "scheduler.task_register 모듈을 찾을 수 없어요.")).pack(fill="x")
        elif not status:
            L(body, "등록된 작업이 없어요.", FS, C["text_2"]).pack(anchor="w")
        else:
            for name, info in status.items():
                reg = bool((info or {}).get("registered"))
                row = tk.Frame(body, bg=C["card"])
                row.pack(fill="x", pady=(0, px(2)))
                StatusPill(row, "등록됨" if reg else "미등록",
                          kind="ok" if reg else "neutral").pack(side="left")
                L(row, f"  {name} · {(info or {}).get('detail', '')}",
                  FS, C["text_2"]).pack(side="left")

        btn_row = tk.Frame(body, bg=C["card"])
        btn_row.pack(fill="x", pady=(SP["sm"], 0))
        SecondaryBtn(btn_row, "이 계정 등록", self._on_register_click,
                    icon="add").pack(side="left", padx=(0, SP["sm"]))
        SecondaryBtn(btn_row, "켠 계정 전체 등록", self._on_register_all_click,
                    icon="add").pack(side="left", padx=(0, SP["sm"]))
        DangerBtn(btn_row, "해제", self._on_unregister_click).pack(side="left")
        L(body, "‘이 계정 등록’과 ‘켠 계정 전체 등록’은 서로 다른 작업이라 "
                "덮어쓰지 않아요. 둘 다 등록돼 있어도 같은 글이 두 번 나가지 않아요.",
          FS, C["muted"], anchor="w", justify="left",
          wraplength=px(620)).pack(anchor="w", pady=(SP["xs"], 0))

    def _on_register_click(self) -> None:
        times = list((self._policy or {}).get("windows") or [])
        if not times:
            self._toast("먼저 예약 준비 시각을 설정하고 저장하세요.", kind="warn")
            return
        self.spawn(self._register_preview_worker, times)

    def _register_preview_worker(self, times: list) -> None:
        try:
            mod = _mod_task_register()
            ok, detail = mod.register_autonomy_task(
                times=times, profile=self._profile_name, dry_run=True)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        self.ui_after(self._after_register_preview, ok, detail, times)

    def _after_register_preview(self, ok: bool, detail: str, times: list) -> None:
        if not ok:
            self._toast(f"스케줄러 등록 미리보기에 실패했어요: {detail}", kind="warn")
            return
        msg = (f"다음 시각에 등록해요: {', '.join(times)}\n\n{detail}\n\n"
               "계속할까요?")
        if not messagebox.askyesno("작업 스케줄러 등록", msg):
            return
        self.spawn(self._register_worker, times)

    def _register_worker(self, times: list) -> None:
        try:
            mod = _mod_task_register()
            ok, detail = mod.register_autonomy_task(
                times=times, profile=self._profile_name, dry_run=False)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        self.ui_after(self._after_register, ok, detail)

    def _after_register(self, ok: bool, detail: str) -> None:
        self._toast(detail or ("등록했어요." if ok else "등록하지 못했어요."),
                   kind="info" if ok else "warn")
        self.refresh()

    # ── 켠 계정 전체 등록(미리보기 → 사용자 승인 → 실제 등록) ──────
    def _on_register_all_click(self) -> None:
        self.spawn(self._register_all_preview_worker)

    def _register_all_preview_worker(self) -> None:
        """대상 계정·시각을 계산하고 **미리보기 XML** 만 만든다(등록 0회)."""
        try:
            mod = _mod_task_register()
            times = list(mod.autonomy_multi_times() or [])
        except Exception as e:  # noqa: BLE001
            self.ui_after(self._after_register_all_preview, False, str(e), [], [])
            return
        rows = []
        try:
            from scheduler.autonomy_multi import eligible_profiles
            rows = [r for r in eligible_profiles() if r.get("eligible")]
        except Exception as e:  # noqa: BLE001
            log.warning(f"[autonomy] 대상 계정 조회 실패: {e}")
        if not times or not rows:
            self.ui_after(
                self._after_register_all_preview, False,
                "켜져 있고 준비 시각이 설정된 계정이 없어요.", [], [])
            return
        try:
            ok, detail = mod.register_autonomy_multi_task(times=times, dry_run=True)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        self.ui_after(self._after_register_all_preview, ok, detail, times,
                      [r["profile"] for r in rows])

    def _after_register_all_preview(self, ok: bool, detail: str, times: list,
                                    names: list) -> None:
        if not ok:
            self._toast(f"전체 등록 미리보기에 실패했어요: {detail}", kind="warn")
            return
        msg = (f"대상 계정 {len(names)}개: {', '.join(names)}\n"
               f"깨어날 시각: {', '.join(times)}\n\n"
               "깨어난 뒤에는 계정마다 설정을 다시 읽어, 그때 켜져 있고 멈추지 않은 "
               "계정만 각자 따로 실행해요.\n\n"
               f"{detail}\n\n계속할까요?")
        if not messagebox.askyesno("켠 계정 전체 등록", msg):
            return
        self.spawn(self._register_all_worker, times)

    def _register_all_worker(self, times: list) -> None:
        try:
            mod = _mod_task_register()
            ok, detail = mod.register_autonomy_multi_task(times=times, dry_run=False)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, str(e)
        self.ui_after(self._after_register, ok, detail)

    def _on_unregister_click(self) -> None:
        if not messagebox.askyesno(
                "작업 스케줄러 해제",
                "무인 운영 작업 스케줄러 등록을 모두 해제해요"
                "(이 계정 작업·켠 계정 전체 작업 둘 다). 계속할까요?"):
            return
        self.spawn(self._unregister_worker)

    def _unregister_worker(self) -> None:
        messages = []
        ok = True
        for fn_name in ("unregister_autonomy_task", "unregister_autonomy_multi_task"):
            try:
                mod = _mod_task_register()
                fn = getattr(mod, fn_name, None)
                if fn is None:
                    continue
                one_ok, detail = fn()
                ok = ok and bool(one_ok)
                if detail:
                    messages.append(str(detail))
            except Exception as e:  # noqa: BLE001
                ok = False
                messages.append(str(e))
        self.ui_after(self._after_unregister, ok, " / ".join(messages))

    def _after_unregister(self, ok: bool, detail: str) -> None:
        self._toast(detail or ("해제했어요." if ok else "해제하지 못했어요."),
                   kind="info" if ok else "warn")
        self.refresh()
