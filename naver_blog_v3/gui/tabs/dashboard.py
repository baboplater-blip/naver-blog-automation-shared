# gui/tabs/dashboard.py — 통계 (GUI 전면 개편 2026-09-02 / v3 2026-09-03)
"""통계 전용 화면. "오늘 실행" 트리거는 홈·포스팅 탭에만 두고(설계서 §2.4),
여기는 발행·방문·보관함 현황을 숫자·차트로 보여주기만 한다.

스레드 위생: `UiLifecycle` 믹스인(`gui/async_utils.py`) 적용 — 워커 스레드는
`self.ui_after(...)` 로만 화면을 건드린다. 기존 `_safe_after(delay, cb)` 헬퍼는
`self.ui_after(cb, delay=delay)` 로 통일했다(§0.3).

v3 상용급 디자인(auto-dev 8호, 2026-09-03, `docs/auto-dev/gui-design-system-v3.md`
§7-7): 이모지 전면 제거 → Segoe Fluent Icons 글리프(`gui.icons.glyph`)로 교체.
`Header`/`SectionCard` 의 `icon=` 은 이미 해석된 글리프 문자를 받고,
`Btn`/`GhostBtn`/`SecondaryBtn` 의 `icon=` 은 `gui.icons.ICONS` 의 이름을
받아 내부에서 해석한다 — 계약이 다르므로 호출부에 주석을 남겼다.
"""
import calendar
import logging
import re
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk
from typing import Optional

from config import safe_load_json
from gui import icons
from gui import theme as _theme
from gui.async_utils import UiLifecycle
from gui.theme import (C, FT, FB, FS, FM, FN, SP, F, Card, L, Sep, Btn,
                        GhostBtn, SecondaryBtn, SubtleBtn)
from gui.widgets import DataTable, EmptyState, Header, SectionCard, attach_empty_state
from gui.utils import today_logs, all_stats, get_error_guide

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent.parent.parent  # naver_blog_v3/
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()


def px(n: int) -> int:
    """고DPI 스케일 헬퍼(9호 Phase 1) — `gui.tabs.home.px()` 와 동일한 방어
    패턴. 코더 A 가 `theme.px(n)`(NBA_UI_SCALE 반영)을 만드는 중이라 있으면
    쓰고, 없으면(아직 없거나 시그니처가 다르면) 항등 함수로 폴백한다. Canvas
    그리기(7일 차트·카테고리 막대·달력)는 폭을 `winfo_width()` 로 이미 실측
    하므로 그 결과 자체는 스케일하지 않고, 고정 여백·오프셋·캔버스 폰트
    크기만 이 함수를 거친다."""
    fn = getattr(_theme, "px", None)
    if callable(fn):
        try:
            return fn(n)
        except Exception:  # noqa: BLE001 — px() 구현이 아직 불안정해도 화면은 떠야 한다
            return n
    return n


# examplebrand 카테고리 — draft_store._examplebrand_CATEGORIES 와 동기.
# settings 가 비어 있어도 대시보드 그리드 칸 수를 유지하도록 한 곳에 박아둔다.
# (2026-05-26) 13개 — 4열 그리드 → 4행(마지막 행 1칸). examplebrand(default) 기본값.
_DEFAULT_DASHBOARD_CATEGORIES = (
    "청년", "복지", "교육", "취업", "주거", "의료",
    "바우처", "부동산", "절세", "금융", "노후", "디지털",
    "환경",
)
# 활성 프로필의 카테고리로 라우팅(default=위 하드코딩, named=브랜드팩 카테고리).
# 한 프로세스=한 프로필이라 import 시점 확정이 옳다.
try:
    from config import brand as _brand
    _DASHBOARD_CATEGORIES = tuple(_brand.categories() or _DEFAULT_DASHBOARD_CATEGORIES)
except Exception:  # noqa: BLE001
    _DASHBOARD_CATEGORIES = _DEFAULT_DASHBOARD_CATEGORIES
_DASHBOARD_GRID_COLS = 4   # 4열 그리드

# 방문자 KPI 2종의 "실측 데이터 있음" 색 — 측정 전에는 muted 로 시작하고
# 실제 값이 오면 이 색으로 전환한다(3차 캡처 실측 🟡, _apply_refresh 참조).
_VISITOR_KEY_COLORS = {"today_visitors": C["yellow"], "total_visitors": C["orange"]}


def _format_queue_line(summary: dict) -> str:
    """preflight summary 의 queue 메타를 한 줄 안내로 변환.

    예:
        "큐: 6/3까지 12편 등록됨 → 새 예약 6/4부터 (+8일 이월)"
        "큐: 비어 있음 → 새 예약 5/27부터"

    Args:
        summary: scheduler.auto_scheduler.preflight_check 의 반환.

    Returns:
        문자열. queue 메타가 없으면 빈 문자열.
    """
    if not summary or not isinstance(summary, dict):
        return ""
    queue = summary.get("queue") or {}
    if not isinstance(queue, dict):
        return ""
    next_start = queue.get("next_start_date")
    if not next_start:
        return ""
    queued = int(queue.get("queued_count", 0) or 0)
    last = queue.get("last_reserved_date")
    carry = int(queue.get("carryover_days", 0) or 0)

    def _md(d: str) -> str:
        # "2026-06-04" → "6/4"
        try:
            _, m, day = d.split("-")
            return f"{int(m)}/{int(day)}"
        except (ValueError, AttributeError):
            return d

    if queued <= 0:
        return f"큐: 비어 있음 → 새 예약 {_md(next_start)}부터"
    parts = [f"큐: {_md(last)}까지 {queued}편 등록됨"] if last else [f"큐: {queued}편 등록됨"]
    parts.append(f"새 예약 {_md(next_start)}부터")
    msg = " → ".join(parts)
    if carry > 0:
        msg += f" (+{carry}일 이월)"
    return msg


def _format_coverage_message(summary: dict, max_categories: int = 8) -> str:
    """pre-flight summary 를 사용자가 읽기 쉬운 다중 라인 문자열로 변환.

    Tk 없이 단위 테스트 가능하도록 순수 함수로 분리.

    Args:
        summary: scheduler.auto_scheduler.preflight_check 의 반환.
        max_categories: 표시할 부족 카테고리 최대 개수. 초과 시 '외 N건' 으로 축약.

    Returns:
        예:
            "큐: 6/3까지 12편 등록됨 → 새 예약 6/4부터 (+8일 이월)\n"
            "오늘 발행 후보 12편 중\n"
            "보관본이 없는 카테고리:\n"
            "  • 복지   3편 필요 / 0편 보관\n"
            "  • 교육   2편 필요 / 0편 보관\n"
        후보가 없고 큐도 비어 있으면 빈 문자열, 후보 없이 큐만 있으면 큐 한 줄만 반환.
    """
    if not summary or not isinstance(summary, dict):
        return ""
    queue_line = _format_queue_line(summary)
    total = summary.get("total_candidates", 0)
    if total <= 0:
        return queue_line  # 후보 없으면 큐 한 줄만 (또는 빈 문자열)
    missing = summary.get("missing_categories") or []
    by_cat = summary.get("by_category") or {}

    lines: list[str] = []
    if queue_line:
        lines.append(queue_line)

    if not missing:
        lines.append(f"오늘 발행 후보 {total}편 — 모든 카테고리 보관본 매칭")
        return "\n".join(lines)

    lines.append(f"오늘 발행 후보 {total}편 중")
    lines.append("보관본이 없는 카테고리:")
    for cat in missing[:max_categories]:
        info = by_cat.get(cat, {})
        needed = info.get("needed", 0)
        available = info.get("available", 0)
        lines.append(f"  • {cat:<4s} {needed}편 필요 / {available}편 보관")
    overflow = len(missing) - max_categories
    if overflow > 0:
        lines.append(f"  • 외 {overflow}개 카테고리 부족")
    return "\n".join(lines)


def format_migration_preview(dry_run_result: dict) -> str:
    """draft_store.migrate_missing_categories(dry_run=True) 결과를 사용자 확인용
    한국어 메시지로 변환. Tk 없이 단위 테스트 가능.

    Args:
        dry_run_result: {"scanned": N, "updated": M, "failed": K,
                         "by_category": {...}, "unmatched": L, "dry_run": bool}

    Returns:
        - updated == 0 인 경우: "정리할 항목 없음" 문구
        - updated > 0 인 경우: 카테고리별 카운트 + 확인 안내
    """
    if not isinstance(dry_run_result, dict):
        return "정리할 항목 없음 — 진단 결과를 읽지 못했습니다."

    scanned = int(dry_run_result.get("scanned", 0) or 0)
    updated = int(dry_run_result.get("updated", 0) or 0)
    failed = int(dry_run_result.get("failed", 0) or 0)
    unmatched = int(dry_run_result.get("unmatched", 0) or 0)
    by_cat = dry_run_result.get("by_category") or {}

    if updated <= 0:
        return (f"정리할 항목 없음 — 미분류 보관본을 찾지 못했습니다.\n"
                f"(스캔 {scanned}건, 카테고리 추정 실패 {unmatched}건)")

    # 카테고리별 카운트를 수가 많은 순으로 정렬
    ordered = sorted(by_cat.items(), key=lambda kv: (-int(kv[1]), kv[0]))
    parts = [f"{cat} {cnt}건" for cat, cnt in ordered if int(cnt) > 0]
    preview = ", ".join(parts) if parts else "(분류 없음)"

    msg = (f"미분류 {updated}건 발견 — {preview}.\n\n"
           f"스캔 {scanned}건 중 {updated}건이 자동 분류 가능합니다.")
    if unmatched > 0:
        msg += f"\n분류 추정 실패 {unmatched}건은 그대로 남습니다."
    if failed > 0:
        msg += f"\n읽기 실패 {failed}건은 건너뜁니다."
    msg += "\n\n지금 정리할까요?"
    return msg


class DashboardTab(UiLifecycle, tk.Frame):
    # 위젯 폭이 0(탭 미표시)일 때 그리기 재시도 상한. 탭이 한 번도 화면에
    # 표시되지 않으면 폭이 계속 0 이라 재시도가 영구 반복되며 타이머가 누적된다
    # → 상한을 두고, 상한 도달·미표시·위젯 파괴 시 조용히 중단한다.
    _WIDTH_RETRY_LIMIT = 10
    _WIDTH_RETRY_DELAY_MS = 200

    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self._init_lifecycle()
        self.app = app
        # 그리기 함수별(차트/카테고리/캘린더) 폭 재시도 횟수. 폭이 확보돼
        # 실제로 그려지면 0 으로 리셋된다.
        self._width_retry_counts: dict = {}
        self._build()

    # ── 토스트(있으면) / messagebox(폴백) — 정보성 알림 전용 ──────────
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

    def _build(self):
        # 스크롤 가능한 컨테이너
        outer = tk.Frame(self, bg=C["bg"])
        outer.pack(fill="both", expand=True)

        self._scroll_canvas = tk.Canvas(outer, bg=C["bg"], bd=0,
                                        highlightthickness=0)
        scrollbar = tk.Scrollbar(outer, orient="vertical",
                                 command=self._scroll_canvas.yview)
        self._scroll_inner = tk.Frame(self._scroll_canvas, bg=C["bg"])

        self._scroll_inner.bind(
            "<Configure>",
            lambda e: self._scroll_canvas.configure(
                scrollregion=self._scroll_canvas.bbox("all")))
        self._scroll_canvas.configure(yscrollcommand=scrollbar.set)

        self._scroll_canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        # 캔버스 폭 변경 시 내부 프레임도 맞춤
        self._inner_id = self._scroll_canvas.create_window(
            (0, 0), window=self._scroll_inner, anchor="nw")
        self._scroll_canvas.bind(
            "<Configure>",
            lambda e: self._scroll_canvas.itemconfigure(
                self._inner_id, width=e.width))

        # 마우스 휠 바인딩
        def _on_wheel(event):
            self._scroll_canvas.yview_scroll(
                int(-1 * (event.delta / 120)), "units")
        self._scroll_canvas.bind(
            "<Enter>",
            lambda e: self._scroll_canvas.bind_all("<MouseWheel>", _on_wheel))
        self._scroll_canvas.bind(
            "<Leave>",
            lambda e: self._scroll_canvas.unbind_all("<MouseWheel>"))

        container = self._scroll_inner

        # 헤더 — 통계 전용 화면이라 실행 트리거는 두지 않는다(설계서 §2.4,
        # "오늘 실행" 은 홈·포스팅 탭에만). 새로고침·보관함 미리보기만 액션으로.
        # Header(icon=) 은 이미 해석된 글리프 문자를 받는다(gui.icons.glyph) —
        # theme.Btn 류의 icon=(이름을 내부에서 해석)과 계약이 다르다.
        hdr = Header(container, "통계", subtitle="발행·방문·보관함 현황이에요.",
                    icon=icons.glyph("stats"))
        hdr.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))
        _weekdays = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
        _today = date.today()
        self.date_lbl = L(hdr, f"{_today.strftime('%Y년 %m월 %d일')}  {_weekdays[_today.weekday()]}",
                          FS, C["muted"])
        self.date_lbl.pack(anchor="w", pady=(SP["xs"], 0))
        SubtleBtn(hdr.actions, "새로고침", self.refresh, icon="refresh").pack(side="right")
        SecondaryBtn(hdr.actions, "보관함 미리보기", self._open_draft_preview, icon="folder")\
            .pack(side="right", padx=(0, SP["sm"]))
        Sep(container).pack(fill="x", pady=(SP["sm"], 0))

        # "오늘의 할 일" 액션카드 섹션은 삭제(홈 신호등이 같은 역할) — 다른
        # 코드가 이 속성을 참조해도 깨지지 않도록 빈 프레임만 남긴다.
        self._action_frame = tk.Frame(container, bg=C["bg"])
        self._action_cards_container = tk.Frame(self._action_frame, bg=C["bg"])

        # ── 지표 7카드 ──────────────────────────────────────
        stat_card = SectionCard(container, "핵심 지표")
        stat_card.pack(fill="x", padx=SP["xl"], pady=SP["lg"])
        stat_card.body.columnconfigure((0, 1, 2), weight=1)
        self._sv = {}
        self._sv_caption = {}
        defs = [
            ("today_posts",       "오늘 등록 완료",      "0", C["green"]),
            ("today_fail",        "오늘 실패",           "0", C["red"]),
            ("total_posts",       "누적 포스팅",         "0", C["accent"]),
            ("success_rate",      "성공률",              "—", C["purple"]),
            # 오늘/누적 방문자는 측정 전(아직 데이터 없음)일 수 있는 값이라
            # 노랑·주황(경고색) 대신 muted "—" + 캡션 "측정 전"으로 시작한다 —
            # 색깔 대시만 있으면 오류처럼 읽혔다(3차 캡처 실측 🟡).
            ("today_visitors",    "오늘 방문자",         "—", C["muted"]),
            ("total_visitors",    "누적 방문자",         "—", C["muted"]),
            ("draft_ready_count", "보관 대기",           "0", C["purple"]),
        ]
        for i, (key, title, val, color) in enumerate(defs):
            cell = Card(stat_card.body, padx=px(18), pady=px(14))
            cell.grid(row=i // 3, column=i % 3, padx=(0, SP["sm"]),
                      pady=(0, SP["sm"]), sticky="nsew")
            L(cell, title, FS, C["muted"]).pack(anchor="w")
            v = L(cell, val, FN, color)
            v.pack(anchor="w", pady=(px(3), 0))
            self._sv[key] = v
            caption = L(cell, "측정 전" if key in _VISITOR_KEY_COLORS else "",
                       FS, C["muted"])
            caption.pack(anchor="w")
            self._sv_caption[key] = caption

        # ── 최근 7일 방문자 ──────────────────────────────────
        chart_card = SectionCard(container, "최근 7일 방문자")
        chart_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        self.chart_canvas = tk.Canvas(chart_card.body, height=px(80),
                                       bg=C["card"], bd=0, highlightthickness=0)
        self.chart_canvas.pack(fill="x")
        self._chart_empty = EmptyState(
            chart_card.body, icons.glyph("info"), "아직 방문자 데이터가 없어요",
            hint="로그인 갱신 후 수집돼요.")

        # ── 카테고리별 성과 ──────────────────────────────────
        cat_card = SectionCard(container, "카테고리별 성과")
        cat_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        self.cat_canvas = tk.Canvas(cat_card.body, height=px(120),
                                    bg=C["card"], bd=0, highlightthickness=0)
        self.cat_canvas.pack(fill="x")

        # ── 보관함 현황 (12 카테고리 카운트 그리드) ─────────────
        self._coverage_section(container)

        # ── 파이프라인 상태 (보관함 다음: 자동화 흐름·결과를 연속 확인) ──
        self._pipeline_status_section(container)

        # ── 실패 포스트 ─────────────────────────────────────
        fail_card = SectionCard(container, "실패 포스트")
        fail_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        self._retry_btn = SecondaryBtn(fail_card.actions, "선택 재시도", self._retry_selected)
        self._retry_btn.pack(side="right")

        self.fail_tree = DataTable(
            fail_card.body, columns=("title", "date", "reason", "guide"),
            show="headings", height=4)
        self.fail_tree.heading("title", text="제목")
        self.fail_tree.heading("date", text="실패일시")
        self.fail_tree.heading("reason", text="사유")
        self.fail_tree.heading("guide", text="해결 가이드")
        self.fail_tree.column("title", width=px(250))
        self.fail_tree.column("date", width=px(120), anchor="center")
        self.fail_tree.column("reason", width=px(130))
        self.fail_tree.column("guide", width=px(200))
        self.fail_tree.pack(fill="x")
        self._fail_empty = EmptyState(
            fail_card.body, icons.glyph("success"), "아직 실패한 글이 없어요",
            hint="발행이 잘 되고 있어요.")
        self._fail_empty_check = attach_empty_state(
            self.fail_tree, self._fail_empty,
            is_empty_fn=lambda: len(self.fail_tree.get_children()) == 0)

        # ── 오늘 예약 타임라인 ───────────────────────────────
        tl_card = SectionCard(container, "오늘 예약 타임라인")
        tl_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        self.tl_frame = tk.Frame(tl_card.body, bg=C["card"])
        self.tl_frame.pack(fill="x")

        # ── 월간 캘린더 ─────────────────────────────────────
        cal_card = SectionCard(container, "이번 달 포스팅 캘린더")
        cal_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        self.cal_canvas = tk.Canvas(cal_card.body, height=px(160),
                                    bg=C["card"], bd=0, highlightthickness=0)
        self.cal_canvas.pack(fill="x")

        self.ui_after(self.refresh, delay=300)

    def refresh(self):
        """백그라운드 스레드에서 데이터를 로드한 후 메인 스레드에서 UI를 갱신합니다."""

        def _load_data() -> dict:
            """무거운 I/O + 파싱 작업을 백그라운드에서 수행합니다."""
            data = {}
            data["logs"] = today_logs()
            data["stats"] = all_stats()

            # 방문자 통계
            try:
                from core.stats_fetcher import get_dashboard_data
                data["dashboard"] = get_dashboard_data()
            except (ImportError, KeyError, OSError) as exc:
                log.debug("방문자 통계 로드 실패: %s", exc, exc_info=True)
                data["dashboard"] = None

            # 카테고리별 성과
            try:
                data["categories"] = self._aggregate_categories()
            except (ImportError, OSError) as exc:
                log.debug("카테고리 집계 실패: %s", exc, exc_info=True)
                data["categories"] = None

            # 실패 포스트
            try:
                data["failed"] = self._load_failed_posts()
            except OSError as exc:
                log.debug("실패 큐 로드 실패: %s", exc, exc_info=True)
                data["failed"] = None

            # 월간 캘린더용 날짜별 포스트 수
            data["day_counts"] = self._count_posts_by_day()

            # 파이프라인 상태
            try:
                data["pipeline_status"] = self._load_pipeline_status()
            except OSError as exc:
                log.debug("파이프라인 상태 로드 실패: %s", exc, exc_info=True)
                data["pipeline_status"] = None

            # 보관함 대기 편수 (draft_ready 상태)
            try:
                from core.draft_store import load_drafts
                data["draft_ready_count"] = len(load_drafts("draft_ready"))
            except (ImportError, OSError) as exc:
                log.debug("보관함 카운트 실패: %s", exc, exc_info=True)
                data["draft_ready_count"] = 0

            return data

        def _on_loaded(data: dict):
            try:
                self._apply_refresh(data)
            except tk.TclError:
                pass  # 위젯이 이미 파괴된 경우 (탭 전환 등)

        def _worker():
            data = _load_data()
            self.ui_after(_on_loaded, data)

        self.spawn(_worker)

    def _retry_draw_when_ready(self, key: str, redraw) -> bool:
        """폭 미확보(탭 미표시) 시 그리기를 재예약하되 무한 재시도를 막는다.

        캔버스 폭이 0 인 건 보통 탭이 아직 화면에 표시되지 않았기 때문이다.
        표시되면 폭이 잡혀 정상 그려지지만, 사용자가 한 번도 탭을 열지 않으면
        폭이 영구히 0 이라 200ms 재시도가 무한 반복돼 타이머가 쌓이고 창 종료
        시 콜백이 죽은 위젯을 건드린다. 그래서 (1) 위젯이 파괴됐거나 (2) 아직
        화면에 매핑되지 않았거나 (3) 재시도 상한(:attr:`_WIDTH_RETRY_LIMIT`)에
        도달하면 재예약하지 않고 조용히 멈춘다. 탭이 나중에 표시되면 그때의
        ``refresh``/``show_tab`` 흐름이 다시 그린다(카운터도 리셋).

        Args:
            key: 그리기 함수 식별자(차트/카테고리/캘린더별 카운터 분리).
            redraw: 재시도 시 호출할 무인자 콜러블(자기 자신을 다시 그림).

        Returns:
            재예약했으면 True, 상한/미표시/파괴로 중단했으면 False.
        """
        try:
            if not self.winfo_exists() or not self.winfo_ismapped():
                # 위젯 파괴 or 탭 미표시 — 폭이 잡힐 길이 없으니 조용히 중단.
                return False
        except tk.TclError:
            return False

        count = self._width_retry_counts.get(key, 0)
        if count >= self._WIDTH_RETRY_LIMIT:
            log.debug("폭 재시도 상한 도달(%s) — 그리기 보류", key)
            return False

        self._width_retry_counts[key] = count + 1
        self.ui_after(redraw, delay=self._WIDTH_RETRY_DELAY_MS)
        return True

    def _count_posts_by_day(self) -> dict:
        """이번 달 날짜별 포스트 수를 집계합니다."""
        today = date.today()
        year, month = today.year, today.month
        day_counts = {}
        try:
            for rec in self._load_post_history():
                posted_at = rec.get("posted_at", "")
                if not posted_at:
                    continue
                post_date = posted_at[:10]  # "YYYY-MM-DD"
                if post_date.startswith(f"{year:04d}-{month:02d}"):
                    day_num = int(post_date[8:10])
                    day_counts[day_num] = day_counts.get(day_num, 0) + 1
        except (ValueError, OSError) as exc:
            log.debug("캘린더 데이터 로드 실패: %s", exc, exc_info=True)
        return day_counts

    def _apply_refresh(self, data: dict):
        """메인 스레드에서 백그라운드 로드 결과로 UI를 갱신합니다."""
        logs = data["logs"]
        stats = data["stats"]
        done = sum(1 for entry in logs if entry.get("success"))
        fail = sum(1 for entry in logs
                   if not entry.get("success") and entry.get("detail") != "dry_run")

        self._sv["today_posts"].config(text=str(done))
        self._sv["today_fail"].config(text=str(fail))
        self._sv["total_posts"].config(text=str(stats["total"]))
        rate = f"{stats['success']/stats['total']*100:.0f}%" if stats["total"] else "—"
        self._sv["success_rate"].config(text=rate)
        self._sv["draft_ready_count"].config(
            text=str(data.get("draft_ready_count", 0)))

        # 방문자 통계 — 값이 없으면 muted "—" + "측정 전" 캡션(경고색 대시가
        # 오류처럼 보이던 문제 수정), 실측값이 오면 원래 색으로 전환.
        dd = data.get("dashboard")
        if dd is not None:
            self._apply_visitor_kpi("today_visitors", dd.get("today_visitors", 0))
            self._apply_visitor_kpi("total_visitors", dd.get("total_visitors", 0))
            self._draw_chart(dd.get("weekly_chart", []))
        else:
            self._apply_visitor_kpi("today_visitors", 0)
            self._apply_visitor_kpi("total_visitors", 0)
            self._draw_chart([])

        # 카테고리별 성과
        cat_data = data.get("categories")
        self._draw_category_chart(cat_data)

        # 타임라인
        for w in self.tl_frame.winfo_children():
            w.destroy()
        slots = self.app.cfg.get("reservation_slots",
            ["07:00","08:30","10:00","12:00","13:30","17:00","19:30","21:00"])

        # ── 슬롯-로그 매핑: reserve_time(HH:MM) 기준 ────────────
        # 같은 슬롯에 여러 기록이 있으면 성공 기록을 우선, 없으면 마지막 기록.
        slot_to_log: dict = {}
        for rec in logs:
            rt = rec.get("reserve_time", "") or ""
            # "YYYY-MM-DD HH:MM" 또는 "HH:MM" 모두 처리
            slot_key = rt[-5:] if len(rt) >= 5 else ""
            if not slot_key or ":" not in slot_key:
                continue
            existing = slot_to_log.get(slot_key)
            if existing is None or (not existing.get("success") and rec.get("success")):
                slot_to_log[slot_key] = rec

        for i, slot in enumerate(slots[:8]):
            row = tk.Frame(self.tl_frame, bg=C["card"])
            row.pack(fill="x", pady=px(3))
            status_color = C["border"]
            status_icon = "waiting"
            title_text = f"포스팅 {i+1} -- 대기 중"
            rec = slot_to_log.get(slot)
            if rec is not None:
                detail = rec.get("detail", "") or ""
                if rec.get("success"):
                    status_icon = "ok"
                    status_color = C["green"]
                elif "중복" in detail:
                    status_icon = "skip"
                    status_color = C["yellow"]
                else:
                    status_icon = "fail"
                    status_color = C["red"]
                title_text = rec.get("title", "")[:46]

            icon_map = {"ok": "[OK]", "fail": "[FAIL]", "skip": "[SKIP]",
                        "waiting": "[..]"}
            icon_text = icon_map.get(status_icon, "[..]")

            tk.Label(row, text="*", fg=status_color, bg=C["card"],
                     font=("맑은 고딕", px(11))).pack(side="left")
            tk.Label(row, text=slot, font=FM, fg=C["accent"],
                     bg=C["card"], width=6).pack(side="left", padx=(px(4), px(10)))
            tk.Label(row, text=f"{icon_text}  {title_text}",
                     font=FB, fg=status_color, bg=C["card"],
                     anchor="w").pack(side="left", fill="x", expand=True)

        # 실패 포스트 큐
        self._apply_failed_queue(data.get("failed"))

        # 파이프라인 상태
        self._apply_pipeline_status(data.get("pipeline_status"))

        # 월간 캘린더
        self._draw_calendar(data.get("day_counts", {}))

        # 보관함 현황 (별도 백그라운드 스레드로 디스크 I/O — refresh 와 독립)
        self._refresh_coverage()

    def _apply_visitor_kpi(self, key: str, value) -> None:
        """방문자 KPI 셀 갱신 — 값이 없으면 muted "—" + "측정 전" 캡션,
        실측값이 있으면 원래 경고색(노랑/주황)으로 전환한다. 색깔 대시만
        보이면 오류처럼 읽히던 문제 수정(3차 캡처 실측 🟡)."""
        try:
            if value:
                self._sv[key].config(text=f"{value:,}", fg=_VISITOR_KEY_COLORS[key])
                self._sv_caption[key].config(text="")
            else:
                self._sv[key].config(text="—", fg=C["muted"])
                self._sv_caption[key].config(text="측정 전")
        except tk.TclError:
            pass

    def _draw_chart(self, weekly: list):
        c = self.chart_canvas
        if not weekly:
            # 데이터가 아예 없으면 빈 캔버스 대신 EmptyState 로 안내한다
            # (3차 캡처 실측 🟡 — "왜 비어 있는지" 설명 없이 덩그러니 있었음).
            try:
                c.pack_forget()
            except tk.TclError:
                pass
            self._chart_empty.show()
            return
        self._chart_empty.hide()
        try:
            c.pack(fill="x")
        except tk.TclError:
            pass
        c.update_idletasks()
        c.delete("all")
        w = c.winfo_width()
        if w < 10:
            self._retry_draw_when_ready(
                "chart", lambda: self._draw_chart(weekly))
            return
        self._width_retry_counts["chart"] = 0  # 폭 확보 — 재시도 카운터 리셋
        h = px(80)
        maxv = max(d.get("count",0) for d in weekly) or 1
        bar_w = w // len(weekly) - px(4)
        for i, d in enumerate(weekly):
            x    = i * (w // len(weekly)) + px(4)
            val  = d.get("count", 0)
            bh   = int((val / maxv) * (h - px(22))) + px(2)
            y1   = h - px(18)
            y0   = y1 - bh
            is_today = d.get("date","") == date.today().strftime("%m-%d")
            color = C["accent"] if is_today else C["border"]
            c.create_rectangle(x, y0, x+bar_w, y1, fill=color, outline="")
            c.create_text(x+bar_w//2, h-px(8), text=d.get("date","")[-5:],
                          fill=C["muted"], font=("Consolas", px(8)))
            if val > 0:
                c.create_text(x+bar_w//2, y0-px(8), text=str(val),
                              fill=C["text"], font=("Consolas", px(8)))

    # ── 카테고리별 성과 차트 ─────────────────────────────
    def _load_post_history(self) -> list:
        """post_history.json을 안전하게 로드합니다."""
        history_path = _DATA_DIR / "logs" / "post_history.json"
        return safe_load_json(history_path, default=list)

    def _load_failed_posts(self) -> list:
        """failed_posts.json을 안전하게 로드합니다 (프로필별 데이터 루트).

        default 프로필은 _DATA_DIR=BASE_DIR(앱 루트)라 경로가 기존과 동일하고,
        named 프로필은 profiles/<name>/failed_posts.json 으로 격리된다.
        """
        failed_path = _DATA_DIR / "failed_posts.json"
        return safe_load_json(str(failed_path), default=list)

    def _aggregate_categories(self) -> dict:
        """post_history.json + failed_posts.json에서 카테고리별 성공/실패 집계."""
        from core.content_generator import detect_category

        counts = {}  # {category: {"success": int, "fail": int}}

        # 성공 기록 (post_history.json)
        for rec in self._load_post_history():
            title = rec.get("title", "")
            clean = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', title)
            cat = detect_category(clean)
            entry = counts.setdefault(cat, {"success": 0, "fail": 0})
            entry["success"] += 1

        # 실패 기록 (failed_posts.json)
        for rec in self._load_failed_posts():
            title = rec.get("title", "")
            clean = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', title)
            cat = detect_category(clean)
            entry = counts.setdefault(cat, {"success": 0, "fail": 0})
            entry["fail"] += 1

        return counts

    def _refresh_category_chart(self):
        """카테고리별 성과 차트를 동기적으로 갱신합니다 (하위 호환용)."""
        try:
            cat_data = self._aggregate_categories()
        except (ImportError, OSError) as exc:
            log.debug("카테고리 집계 실패: %s", exc, exc_info=True)
            return
        self._draw_category_chart(cat_data)

    def _draw_category_chart(self, cat_data):
        """카테고리별 성공/실패 가로 바 차트를 그립니다."""
        canvas = self.cat_canvas
        canvas.update_idletasks()
        canvas.delete("all")

        canvas_w = canvas.winfo_width()
        if canvas_w < 10:
            self._retry_draw_when_ready(
                "category", self._refresh_category_chart)
            return
        self._width_retry_counts["category"] = 0  # 폭 확보 — 카운터 리셋

        if not cat_data:
            canvas.create_text(canvas_w // 2, px(60), text="데이터 없음",
                               fill=C["muted"], font=FS)
            return

        # 총합 기준 내림차순 정렬
        sorted_cats = sorted(cat_data.items(),
                             key=lambda x: x[1]["success"] + x[1]["fail"],
                             reverse=True)

        row_count = min(len(sorted_cats), 6)  # 최대 6개 카테고리
        max_total = max((v["success"] + v["fail"]) for _, v in sorted_cats) or 1
        row_h = px(18)
        gap = px(2)
        label_w = px(60)
        bar_area = canvas_w - label_w - px(60)  # 오른쪽 숫자 여백

        # 캔버스 높이 조정
        needed_h = row_count * (row_h + gap) + px(10)
        canvas.config(height=max(needed_h, px(40)))

        for idx, (cat, vals) in enumerate(sorted_cats[:row_count]):
            y_top = idx * (row_h + gap) + px(5)
            total = vals["success"] + vals["fail"]

            # 카테고리 라벨
            canvas.create_text(label_w - px(4), y_top + row_h // 2,
                               text=cat, anchor="e",
                               fill=C["text"], font=FS)

            # 성공 바 (green)
            suc_w = int((vals["success"] / max_total) * bar_area) if vals["success"] else 0
            if suc_w > 0:
                canvas.create_rectangle(label_w, y_top,
                                        label_w + suc_w, y_top + row_h,
                                        fill=C["green"], outline="")

            # 실패 바 (red) — 성공 바 뒤에 이어붙임
            fail_w = int((vals["fail"] / max_total) * bar_area) if vals["fail"] else 0
            if fail_w > 0:
                canvas.create_rectangle(label_w + suc_w, y_top,
                                        label_w + suc_w + fail_w, y_top + row_h,
                                        fill=C["red"], outline="")

            # 숫자 라벨
            num_text = f"{vals['success']}/{total}"
            canvas.create_text(label_w + suc_w + fail_w + px(8),
                               y_top + row_h // 2,
                               text=num_text, anchor="w",
                               fill=C["muted"], font=("Consolas", px(9)))

    # ── 실패 포스트 큐 ───────────────────────────────────
    def _refresh_failed_queue(self):
        """failed_posts.json을 읽어 표에 표시합니다 (동기 버전)."""
        try:
            failed = self._load_failed_posts()
        except OSError as exc:
            log.debug("실패 큐 로드 실패: %s", exc, exc_info=True)
            failed = None
        self._apply_failed_queue(failed)

    def _apply_failed_queue(self, failed):
        """미리 로드된 실패 포스트 데이터로 표를 갱신합니다."""
        for item in self.fail_tree.get_children():
            self.fail_tree.delete(item)

        if failed is None:
            return

        # 최신 순으로 표시 (최대 20개)
        for rec in reversed(failed[-20:]):
            title = rec.get("title", "")[:50]
            fail_date = rec.get("date", "")
            reason = (rec.get("reason") or "")[:30]
            guide = get_error_guide(reason)[:40] if reason else ""
            self.fail_tree.insert("", "end",
                                  values=(title, fail_date, reason, guide))
        self._fail_empty_check()

    def _retry_selected(self):
        """표에서 선택된 실패 포스트를 재시도합니다.

        실제로 다시 발행을 시도하는 동작이라 확인 1회를 거친다(설계서
        §1.2 — 파괴적·되돌릴 수 없는 동작 확인은 messagebox.askyesno).
        """
        selection = self.fail_tree.selection()
        if not selection:
            return

        item_values = self.fail_tree.item(selection[0], "values")
        if not item_values:
            return

        selected_title = item_values[0]
        if not messagebox.askyesno(
                "재시도", f"'{selected_title}' 글을 다시 시도할까요?"):
            return

        # 원본 제목 복원 (표에서 50자로 잘렸을 수 있음)
        failed = self._load_failed_posts()
        full_title = selected_title
        for rec in failed:
            if rec.get("title", "").startswith(selected_title):
                full_title = rec["title"]
                break

        def _do_retry():
            ok = False
            try:
                from scheduler.auto_scheduler import retry_failed_post
                result = retry_failed_post(full_title)
                ok = bool(result.get("success"))
                if ok:
                    log.info("재시도 성공: %s", full_title[:46])
                else:
                    log.warning("재시도 실패: %s — %s",
                                full_title[:46], result.get("error", ""))
            except (ImportError, ValueError, OSError, RuntimeError) as exc:
                log.error("재시도 오류: %s", exc, exc_info=True)
            finally:
                self.ui_after(self._after_retry, ok)

        self._retry_btn.config(state="disabled", text="재시도 중...")
        self.spawn(_do_retry)

    def _after_retry(self, ok: bool) -> None:
        try:
            self._retry_btn.config(state="normal", text="선택 재시도")
        except tk.TclError:
            pass
        self._toast("재시도했어요 — 발행 성공" if ok else "재시도했지만 실패했어요",
                   kind="ok" if ok else "warn")
        self.refresh()

    # ── 보관함 현황 카드 (2026-05-16) ──────────────────────
    def _coverage_section(self, parent):
        """draft_ready 보관본 카테고리별 카운트 그리드.

        12 카테고리 × 4열 그리드. 각 셀에 카운트 + 색상으로 보유 수준을 표시.
        값 0=muted, 1~9=accent, 10~29=green, 30+=purple.
        """
        cv_card = SectionCard(parent, "보관함 현황", icon=icons.glyph("folder"))
        cv_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        cv_frame = cv_card.body
        self._coverage_total_lbl = L(cv_card.actions, "0편", FS, C["muted"])
        self._coverage_total_lbl.pack(side="right")

        # 카테고리 카운트 그리드 (4열 × 3행)
        grid_card = Card(cv_frame, padx=px(14), pady=px(10))
        grid_card.pack(fill="x")
        for col in range(_DASHBOARD_GRID_COLS):
            grid_card.columnconfigure(col, weight=1, uniform="cov")
        self._coverage_cells: dict = {}
        for idx, cat in enumerate(_DASHBOARD_CATEGORIES):
            row, col = divmod(idx, _DASHBOARD_GRID_COLS)
            cell = tk.Frame(grid_card, bg=C["card"])
            cell.grid(row=row, column=col, sticky="nsew", padx=px(4), pady=px(3))
            name = tk.Label(cell, text=cat, font=FS, fg=C["muted"], bg=C["card"],
                            anchor="w", padx=px(6))
            name.pack(side="left", fill="x", expand=True)
            count = tk.Label(cell, text="0", font=("맑은 고딕", px(13), "bold"),
                             fg=C["muted"], bg=C["card"], padx=px(6))
            count.pack(side="right")
            self._coverage_cells[cat] = count

        # 요약 + 액션 행
        summary_row = tk.Frame(cv_frame, bg=C["bg"])
        summary_row.pack(fill="x", pady=(px(6), 0))
        self._coverage_summary_lbl = L(
            summary_row, "데이터 로딩 중...", FS, C["muted"])
        self._coverage_summary_lbl.pack(side="left")

        action_row = tk.Frame(cv_frame, bg=C["bg"])
        action_row.pack(fill="x", pady=(px(8), 0))
        GhostBtn(action_row, "새로고침", self._refresh_coverage, 10,
                icon="refresh").pack(side="left")
        GhostBtn(action_row, "부족 카테고리 채우기",
                 self._open_fill_missing_dialog, 18, icon="add"
                 ).pack(side="left", padx=(px(8), 0))
        GhostBtn(action_row, "미분류 정리",
                 self._run_migrate_missing_categories, 14
                 ).pack(side="left", padx=(px(8), 0))

    def _refresh_coverage(self):
        """draft_store.count_by_category 를 다시 읽어 그리드 갱신.

        백그라운드 스레드에서 디스크 I/O 를 수행하고 메인 스레드에서 UI 갱신.
        탭 전환·종료로 위젯이 사라졌으면 tk.TclError 를 조용히 무시.
        """
        def _apply(payload):
            self._apply_coverage_payload(payload)

        def _worker():
            payload = self._load_coverage_payload()
            self.ui_after(self._safe_apply_coverage, payload, _apply)

        self.spawn(_worker)

    @staticmethod
    def _load_coverage_payload() -> dict:
        """보관함 카운트를 로드하고 첫 실행/정상/오류를 구분한 payload 를 반환.

        "데이터 파일이 아직 없는 정상 첫 실행"과 "예외로 못 읽은 오류"를
        호출 측이 구분할 수 있어야 첫 실행에 빨간 오류를 잘못 띄우지 않는다.
        ``count_by_category`` 자체는 로드 실패 시에도 0-dict 를 돌려주므로(예외
        미전파), 여기서 import/경로 예외만 ``error`` 로 잡고, 보관 데이터 디렉터리
        (harness ``runs/``)가 아직 없으면 ``first_run`` 으로 표시한다.

        Returns:
            ``{"status": "ok"|"first_run"|"error", "counts": dict}``.
            ``error`` 면 ``counts`` 는 빈 dict.
        """
        try:
            from core.draft_store import count_by_category
            counts = count_by_category("draft_ready")
        except (ImportError, OSError) as exc:
            log.debug("보관함 카운트 실패: %s", exc, exc_info=True)
            return {"status": "error", "counts": {}}

        total = sum(int(counts.get(cat, 0)) for cat in _DASHBOARD_CATEGORIES)
        if total == 0 and not DashboardTab._drafts_dir_exists():
            # 데이터가 하나도 없고 보관 디렉터리조차 생성 전 = 정상 첫 실행.
            return {"status": "first_run", "counts": counts}
        return {"status": "ok", "counts": counts}

    @staticmethod
    def _drafts_dir_exists() -> bool:
        """보관본 저장 디렉터리(harness ``runs/``)가 존재하는지. 판정 불가 시 True.

        첫 실행 판정 보조용. 경로 조회 자체가 실패하면(설정 미비 등) 첫 실행으로
        단정하지 않도록 True(=존재로 간주)를 돌려 정상 경로로 흐르게 한다.
        """
        try:
            runs_dir = _nbaprofile.harness_data_root() / "runs"
            return runs_dir.exists()
        except Exception:  # noqa: BLE001 — 경로 조회 실패는 첫 실행 단정 금지
            return True

    def _apply_coverage_payload(self, payload: dict) -> None:
        """_load_coverage_payload 결과로 보관함 현황 UI 를 갱신(메인 스레드)."""
        status = payload.get("status")
        if status == "error":
            self._coverage_summary_lbl.config(
                text="보관함 정보를 불러오지 못했습니다", fg=C["red"])
            return
        if status == "first_run":
            # 정상 첫 실행 — 빨강 오류가 아니라 중립 안내.
            for cat in _DASHBOARD_CATEGORIES:
                cell = self._coverage_cells.get(cat)
                if cell is not None:
                    cell.config(text="0", fg=self._coverage_color(0))
            self._coverage_total_lbl.config(text="합계 0편", fg=C["muted"])
            self._coverage_summary_lbl.config(
                text="아직 보관본이 없습니다 — 실행·보관함 채우기 후 표시됩니다",
                fg=C["muted"])
            return

        counts = payload.get("counts") or {}
        total = 0
        missing_cats = []
        for cat in _DASHBOARD_CATEGORIES:
            v = int(counts.get(cat, 0))
            total += v
            cell = self._coverage_cells.get(cat)
            if cell is not None:
                cell.config(text=str(v), fg=self._coverage_color(v))
            if v == 0:
                missing_cats.append(cat)
        self._coverage_total_lbl.config(text=f"합계 {total}편", fg=C["text"])
        if missing_cats:
            self._coverage_summary_lbl.config(
                text=f"미보관 카테고리 {len(missing_cats)}종: "
                     f"{', '.join(missing_cats[:6])}"
                     + ("..." if len(missing_cats) > 6 else ""),
                fg=C["yellow"] if len(missing_cats) < 12 else C["red"])
        else:
            self._coverage_summary_lbl.config(
                text="모든 카테고리 보관본 확보 완료",
                fg=C["green"])

    def _safe_apply_coverage(self, counts, apply_fn):
        """위젯 파괴 대비 안전 래퍼."""
        try:
            apply_fn(counts)
        except tk.TclError:
            pass

    @staticmethod
    def _coverage_color(count: int) -> str:
        """카운트에 따라 셀 텍스트 색상을 결정."""
        if count <= 0:
            return C["muted"]
        if count < 10:
            return C["accent"]
        if count < 30:
            return C["green"]
        return C["purple"]

    def _open_fill_missing_dialog(self):
        """부족 카테고리 채우기 다이얼로그 진입.

        현재 카운트를 읽고 0편 카테고리 콤보박스 + 편수 입력. 0 인 게 없으면
        사용자에게 알리고 종료.
        """
        try:
            from core.draft_store import count_by_category
            counts = count_by_category("draft_ready")
        except (ImportError, OSError) as exc:
            log.warning("부족 카테고리 다이얼로그: 카운트 로드 실패: %s", exc)
            messagebox.showerror(
                "부족 카테고리 채우기",
                "보관함 정보를 불러오지 못했습니다.\n잠시 후 다시 시도하세요.")
            return

        zero_cats = [c for c in _DASHBOARD_CATEGORIES if counts.get(c, 0) == 0]
        if not zero_cats:
            self._toast("0편인 카테고리가 없어요. 모든 카테고리에 보관본이 있어요.",
                       kind="info")
            return

        dlg = _FillCategoryDialog(self.winfo_toplevel(), zero_cats)
        self.winfo_toplevel().wait_window(dlg.top)
        result = dlg.result
        if not result:
            return
        cat = result.get("category")
        n = result.get("count")
        if not cat or not n:
            return
        # 실제 채우기 실행은 App 의 run_fill_drafts 에 위임.
        if hasattr(self.app, "run_fill_drafts"):
            self.app.run_fill_drafts(category=cat, n=n)

    # ── 미분류 보관본 자동 분류 (2026-05-16) ───────────────
    def _run_migrate_missing_categories(self):
        """category 누락 보관본을 detect_category 로 일괄 백필.

        흐름:
          1) dry_run=True 로 스캔 — 카테고리별 추정 카운트 미리보기
          2) messagebox 로 사용자 확인 (format_migration_preview)
          3) 확인 시 dry_run=False 로 실행
          4) 결과 토스트 + 보관함 현황 자동 새로고침
        """
        try:
            from core.draft_store import migrate_missing_categories
        except ImportError as exc:
            log.warning("미분류 정리: draft_store import 실패: %s", exc)
            messagebox.showerror(
                "미분류 정리",
                "보관함 정보를 불러오지 못했어요.")
            return

        # 1) dry-run 으로 미리보기 카운트 수집
        try:
            preview = migrate_missing_categories(dry_run=True)
        except (OSError, RuntimeError) as exc:
            log.error("미분류 정리 dry-run 실패: %s", exc, exc_info=True)
            messagebox.showerror("미분류 정리", "스캔에 실패했어요. 잠시 후 다시 시도하세요.")
            return

        if int(preview.get("updated", 0) or 0) <= 0:
            self._toast(format_migration_preview(preview), kind="info")
            return

        # 2) 사용자 확인
        confirm = messagebox.askyesno(
            "미분류 보관본 자동 분류",
            format_migration_preview(preview))
        if not confirm:
            return

        # 3) 실제 실행
        try:
            result = migrate_missing_categories(dry_run=False)
        except (OSError, RuntimeError) as exc:
            log.error("미분류 정리 실행 실패: %s", exc, exc_info=True)
            messagebox.showerror("미분류 정리", "실행에 실패했어요. 잠시 후 다시 시도하세요.")
            return

        updated = int(result.get("updated", 0) or 0)
        failed = int(result.get("failed", 0) or 0)
        msg = f"{updated}건을 자동 분류했어요."
        if failed > 0:
            msg += f" 읽기 실패 {failed}건은 그대로 남았어요."
        self._toast(msg, kind="ok")

        # 4) 보관함 현황 카드 새로고침
        try:
            self._refresh_coverage()
        except (tk.TclError, AttributeError) as exc:
            log.debug("미분류 정리 후 새로고침 실패: %s", exc)

    # ── 파이프라인 상태 ─────────────────────────────────
    _STEP_NAMES = {
        "deadline_tracker": "마감 감지",
        "post_refresher":   "포스트 갱신",
        "trend_updater":    "트렌드 업데이트",
        "stats_fetcher":    "통계 수집",
    }
    _STEP_LABEL_FONT = ("맑은 고딕", 10, "bold")
    _MAX_MSG_LEN = 48

    def _pipeline_status_section(self, parent):
        """파이프라인 상태 카드를 생성합니다."""
        ps_card = SectionCard(parent, "파이프라인 상태")
        ps_card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        self._pipeline_badge = L(ps_card.actions, "", FS, C["muted"])
        self._pipeline_badge.pack(side="right")

        # 초기 상태: 데이터 없음 표시
        self._pipeline_date_lbl = L(ps_card.body, "마지막 실행: --",
                                     FS, C["muted"])
        self._pipeline_date_lbl.pack(anchor="w", pady=(0, SP["sm"]))
        self._pipeline_rows_frame = tk.Frame(ps_card.body, bg=C["card"])
        self._pipeline_rows_frame.pack(fill="x")

    def _load_pipeline_status(self) -> dict:
        """logs/pipeline_status.json을 안전하게 로드합니다."""
        status_path = _DATA_DIR / "logs" / "pipeline_status.json"
        return safe_load_json(status_path)

    def _apply_pipeline_status(self, data):
        """파이프라인 상태 카드 UI를 갱신합니다."""
        # 기존 행 제거
        for widget in self._pipeline_rows_frame.winfo_children():
            widget.destroy()

        if not data:
            L(self._pipeline_rows_frame, "데이터 없음", FS, C["muted"]).pack(anchor="w")
            self._pipeline_date_lbl.config(text="마지막 실행: --")
            return

        run_date = data.get("date", "--")
        total = data.get("total", 0)
        success_cnt = data.get("success", 0)
        self._pipeline_date_lbl.config(
            text=f"마지막 실행: {run_date}")

        # 헤더 뱃지: 성공 비율 (전체 성공 시 green, 부분 실패 시 yellow, 전체 실패 시 red)
        if total > 0:
            if success_cnt == total:
                badge_color = C["green"]
            elif success_cnt == 0:
                badge_color = C["red"]
            else:
                badge_color = C["yellow"]
            self._pipeline_badge.config(
                text=f"{success_cnt}/{total} 성공", fg=badge_color)
        else:
            self._pipeline_badge.config(text="", fg=C["muted"])

        steps = data.get("steps", {})
        step_order = ["deadline_tracker", "post_refresher", "trend_updater", "stats_fetcher"]
        for step_key in step_order:
            row = tk.Frame(self._pipeline_rows_frame, bg=C["card"])
            row.pack(fill="x", pady=(px(3), px(3)))

            step_label = self._STEP_NAMES.get(step_key, step_key)

            if step_key in steps:
                info = steps[step_key]
                is_ok = info.get("success", False)
                icon_color = C["green"] if is_ok else C["red"]
                raw_msg = info.get("message", "")
            else:
                icon_color = C["border"]
                raw_msg = "미실행"

            # 메시지 길이 제한: 48자 초과 시 말줄임
            msg = (raw_msg[:self._MAX_MSG_LEN] + "…"
                   if len(raw_msg) > self._MAX_MSG_LEN else raw_msg)

            tk.Label(row, text="\u25cf", fg=icon_color, bg=C["card"],
                     font=("맑은 고딕", px(12))).pack(side="left", padx=(0, px(8)))
            tk.Label(row, text=step_label, fg=C["text"], bg=C["card"],
                     font=self._STEP_LABEL_FONT, width=14, anchor="w").pack(side="left")
            tk.Label(row, text=msg, fg=C["muted"], bg=C["card"],
                     font=FS, anchor="w").pack(side="left", fill="x", expand=True)

    # ── 월간 캘린더 ──────────────────────────────────────
    def _draw_calendar(self, day_counts=None):
        """이번 달 포스팅 캘린더를 Canvas에 그립니다."""
        if day_counts is None:
            day_counts = self._count_posts_by_day()

        canvas = self.cal_canvas
        canvas.update_idletasks()
        canvas.delete("all")

        canvas_w = canvas.winfo_width()
        if canvas_w < 10:
            self._retry_draw_when_ready(
                "calendar", lambda: self._draw_calendar(day_counts))
            return
        self._width_retry_counts["calendar"] = 0  # 폭 확보 — 카운터 리셋

        today = date.today()
        year, month = today.year, today.month

        # 달력 레이아웃 계산
        cal = calendar.monthcalendar(year, month)
        rows = len(cal)
        cols = 7

        cell_w = canvas_w // cols
        header_h = px(20)
        cell_h = (px(160) - header_h) // rows

        # 요일 헤더
        weekday_names = ["월", "화", "수", "목", "금", "토", "일"]
        for col_idx, name in enumerate(weekday_names):
            x_center = col_idx * cell_w + cell_w // 2
            canvas.create_text(x_center, header_h // 2, text=name,
                               fill=C["muted"], font=("맑은 고딕", px(8)))

        # 색상 계산 헬퍼
        def _post_color(count: int) -> str:
            """포스트 수에 따라 밝기가 다른 green 반환."""
            if count <= 0:
                return C["card"]  # 0건은 배경과 동일
            # green (#51e5a0) 기반으로 밝기 조절
            r, g, b = 0x51, 0xe5, 0xa0
            if count <= 2:
                factor = 0.4  # 1-2건: 어두운 green
            else:
                factor = 0.8  # 3+건: 진한 green
            bg_r, bg_g, bg_b = 0x1a, 0x1d, 0x28  # card 배경
            nr = int(bg_r + (r - bg_r) * factor)
            ng = int(bg_g + (g - bg_g) * factor)
            nb = int(bg_b + (b - bg_b) * factor)
            return f"#{nr:02x}{ng:02x}{nb:02x}"

        # 날짜 셀 그리기
        for row_idx, week in enumerate(cal):
            for col_idx, day_num in enumerate(week):
                x0 = col_idx * cell_w + px(1)
                y0 = header_h + row_idx * cell_h + px(1)
                x1 = x0 + cell_w - px(2)
                y1 = y0 + cell_h - px(2)

                if day_num == 0:
                    # 빈 셀
                    canvas.create_rectangle(x0, y0, x1, y1,
                                            fill=C["card"], outline="")
                    continue

                count = day_counts.get(day_num, 0)
                fill_color = _post_color(count)
                is_today = (day_num == today.day)
                outline_color = C["accent"] if is_today else C["border"]
                outline_width = 2 if is_today else 1

                canvas.create_rectangle(x0, y0, x1, y1,
                                        fill=fill_color,
                                        outline=outline_color,
                                        width=outline_width)

                # 날짜 숫자
                text_color = C["text"] if day_num == today.day else C["muted"]
                canvas.create_text(x0 + px(4), y0 + px(3), text=str(day_num),
                                   anchor="nw", fill=text_color,
                                   font=("Consolas", px(8)))

                # 포스트 수 (1건 이상)
                if count > 0:
                    canvas.create_text((x0 + x1) // 2, (y0 + y1) // 2 + px(4),
                                       text=str(count),
                                       fill=C["text"],
                                       font=("맑은 고딕", px(10), "bold"))

    def _open_draft_preview(self):
        """보관함 인덱스(runs/_index.html) 를 생성하고 Chrome 으로 강제 오픈.

        2단계: 별도 프로세스로 헬퍼 실행 시 webbrowser 가 OS 기본 브라우저(Edge 등)로
        보내고 콘솔과 분리돼 로드가 실패하는 경우를 회피한다. GUI 가 직접 인덱스를
        만들고 Chrome 실행 파일을 직접 호출.
        """
        import os
        import subprocess
        import sys

        harness_root = BASE_DIR.parent  # naver_blog_automation_v3/
        helper = harness_root / "_lib" / "preview_drafts.py"
        # 인덱스는 프로필 runs/ (2026-07-19 격리 감사) — 헬퍼(draft_store)와 동일
        # 라우팅. 헬퍼 subprocess 는 부모 env(NBA_PROFILE 고정)를 상속한다.
        try:
            from config import profile as _nbaprofile
            index_path = _nbaprofile.runs_dir() / "_index.html"
        except Exception:  # noqa: BLE001
            index_path = harness_root / "runs" / "_index.html"
        if not helper.exists():
            messagebox.showerror(
                "보관함 미리보기",
                "필요한 도구를 찾지 못했어요. 프로그램을 다시 설치해 보세요.")
            return

        # 1) 인덱스 파일 생성 — --no-open 으로 헬퍼는 파일만 쓰고 종료.
        try:
            result = subprocess.run(
                [sys.executable, str(helper), "--no-open"],
                cwd=str(harness_root),
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                log.warning("보관함 미리보기 인덱스 생성 실패(exit %s): %s",
                            result.returncode,
                            (result.stderr or result.stdout or "").strip()[:300])
                messagebox.showerror(
                    "보관함 미리보기",
                    "보관함 미리보기를 만들지 못했어요. 다시 시도해 보세요.")
                return
        except (OSError, subprocess.TimeoutExpired) as exc:
            log.warning("보관함 미리보기 인덱스 생성 실패: %s", exc)
            messagebox.showerror(
                "보관함 미리보기",
                "보관함 미리보기를 만들지 못했어요. 다시 시도해 보세요.")
            return

        if not index_path.exists():
            messagebox.showerror(
                "보관함 미리보기",
                "보관함 미리보기 파일이 만들어지지 않았어요. 다시 시도해 보세요.")
            return

        # 2) Chrome 우선 — 표준 설치 경로 3곳 탐색 후 직접 실행.
        chrome_candidates = [
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            os.path.expanduser(
                r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
        ]
        for cp in chrome_candidates:
            if os.path.exists(cp):
                try:
                    subprocess.Popen([cp, str(index_path)])
                    return
                except OSError:
                    continue

        # 3) Chrome 못 찾으면 webbrowser fallback (Edge 등 기본 브라우저).
        import webbrowser
        try:
            webbrowser.open(index_path.as_uri())
        except (OSError, RuntimeError) as exc:
            log.warning("보관함 미리보기 브라우저 열기 실패: %s", exc)
            messagebox.showerror("보관함 미리보기", "브라우저를 여는 데 실패했어요.")


# ──────────────────────────────────────────────────────────────────────────
# 모달 다이얼로그 — pre-flight / 부족 카테고리 채우기 (2026-05-16)
# 모든 위젯은 부모 toplevel 의 자식이며 transient + grab_set 로 부모 위에 떠 있다.
# ESC 또는 X 버튼 = 취소, 결과는 self.result 에 담아 호출자가 wait_window 후 읽는다.
# ──────────────────────────────────────────────────────────────────────────


class _FillCategoryDialog:
    """0편 카테고리 콤보 + 편수 입력 다이얼로그.

    Usage:
        dlg = _FillCategoryDialog(parent_root, zero_categories=["복지", "교육"])
        parent_root.wait_window(dlg.top)
        if dlg.result:
            cat, n = dlg.result["category"], dlg.result["count"]
    """

    def __init__(self, parent, zero_categories: list):
        self.result: dict = {}
        self.top = tk.Toplevel(parent)
        self.top.title("부족 카테고리 채우기")
        self.top.configure(bg=C["bg"])
        self.top.transient(parent)
        self.top.resizable(False, False)
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.top.bind("<Escape>", lambda _e: self._cancel())

        # 부모 중앙 정렬
        self.top.update_idletasks()
        try:
            px = parent.winfo_rootx() + parent.winfo_width() // 2 - 180
            py = parent.winfo_rooty() + parent.winfo_height() // 2 - 120
            self.top.geometry(f"360x230+{max(0,px)}+{max(0,py)}")
        except tk.TclError:
            self.top.geometry("360x230")

        body = tk.Frame(self.top, bg=C["bg"], padx=px(18), pady=px(16))
        body.pack(fill="both", expand=True)

        L(body, "어느 카테고리를 채울까요?", FT, C["text"]).pack(anchor="w")
        L(body, "(아래는 현재 0편인 카테고리)",
          FS, C["muted"]).pack(anchor="w", pady=(0, px(8)))

        self._cat_var = tk.StringVar(value=zero_categories[0])
        cat_combo = ttk.Combobox(body, textvariable=self._cat_var,
                                  values=list(zero_categories),
                                  state="readonly", width=20, font=FB)
        cat_combo.pack(anchor="w", fill="x", pady=(0, px(12)))

        L(body, "편수 (1~50):", FB, C["text"]).pack(anchor="w")
        self._n_var = tk.IntVar(value=5)
        n_spin = ttk.Spinbox(body, from_=1, to=50, textvariable=self._n_var,
                              width=8, font=FB)
        n_spin.pack(anchor="w", pady=(px(4), px(12)))

        btn_row = tk.Frame(body, bg=C["bg"])
        btn_row.pack(fill="x")
        Btn(btn_row, "보관함 채우기 시작", self._confirm, 18, icon="save").pack(side="right")
        GhostBtn(btn_row, "취소", self._cancel, 8, icon="close").pack(side="right", padx=(0, px(8)))

        # 부모 잠금 — 다이얼로그가 떠 있는 동안 부모 윈도우 입력 차단
        try:
            self.top.grab_set()
        except tk.TclError:
            pass
        self.top.focus_set()

    def _confirm(self):
        try:
            n = int(self._n_var.get())
        except (tk.TclError, ValueError):
            n = 0
        cat = self._cat_var.get().strip()
        if not cat or n <= 0:
            messagebox.showwarning(
                "입력 오류", "카테고리와 1 이상의 편수를 입력하세요.",
                parent=self.top)
            return
        self.result = {"category": cat, "count": n}
        self.top.destroy()

    def _cancel(self):
        self.result = {}
        self.top.destroy()


class _PreflightDialog:
    """pre-flight 결과 모달 다이얼로그.

    세 가지 결과 중 하나를 self.result['action'] 에 담는다:
      - "fill": 부족 카테고리 채우기 (+ category, count)
      - "matched": 매칭되는 카테고리만 발행
      - "cancel": 취소
    """

    def __init__(self, parent, summary: dict):
        self.result: dict = {"action": "cancel"}
        self.summary = summary or {}

        self.top = tk.Toplevel(parent)
        self.top.title("보관함 점검 결과")
        self.top.configure(bg=C["bg"])
        self.top.transient(parent)
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)
        self.top.bind("<Escape>", lambda _e: self._cancel())

        self.top.update_idletasks()
        try:
            px = parent.winfo_rootx() + parent.winfo_width() // 2 - 260
            py = parent.winfo_rooty() + parent.winfo_height() // 2 - 200
            self.top.geometry(f"520x420+{max(0,px)}+{max(0,py)}")
        except tk.TclError:
            self.top.geometry("520x420")
        self.top.resizable(False, False)

        body = tk.Frame(self.top, bg=C["bg"], padx=px(20), pady=px(18))
        body.pack(fill="both", expand=True)

        L(body, f"{icons.glyph('folder')} 보관함 점검 결과", FT, C["accent"]).pack(anchor="w")

        # 본문: format helper 결과를 그대로 표시
        msg = _format_coverage_message(self.summary, max_categories=10)
        msg_lbl = tk.Label(
            body, text=msg or "후보 없음",
            bg=C["card"], fg=C["text"], font=FS,
            justify="left", anchor="nw",
            padx=px(14), pady=px(12), wraplength=px(460),
        )
        msg_lbl.pack(fill="both", expand=True, pady=(px(10), px(12)))

        # 매칭되는 카테고리 미리보기 (매칭만 실행 가능 여부 안내)
        by_cat = self.summary.get("by_category") or {}
        covered = [c for c, info in by_cat.items()
                   if info.get("covered") and info.get("needed", 0) > 0]
        if covered:
            L(body,
              f"매칭 가능 카테고리: {', '.join(covered)} "
              f"({sum(info.get('needed',0) for c, info in by_cat.items() if c in covered)}편)",
              FS, C["green"]).pack(anchor="w", pady=(0, px(8)))
        else:
            L(body, "매칭 가능 카테고리 없음 — 보관함을 먼저 채워야 발행 가능",
              FS, C["yellow"]).pack(anchor="w", pady=(0, px(8)))

        # 버튼 행
        btn_row = tk.Frame(body, bg=C["bg"])
        btn_row.pack(fill="x", pady=(px(4), 0))
        # 우측부터 pack
        Btn(btn_row, "보관함 채우기", self._on_fill, 16, icon="save").pack(
            side="left", padx=(0, px(8)))
        if covered:
            Btn(btn_row, "매칭만 실행", self._on_matched, 14, C["green"],
               icon="run").pack(side="left", padx=(0, px(8)))
        GhostBtn(btn_row, "취소", self._cancel, 8, icon="close").pack(side="right")

        try:
            self.top.grab_set()
        except tk.TclError:
            pass
        self.top.focus_set()

    def _on_fill(self):
        """부족 카테고리 채우기 — 부족 목록에서 1개 선택 + 편수 입력."""
        missing = self.summary.get("missing_categories") or []
        if not missing:
            # 이론상 도달 불가 (fully_covered 면 이 다이얼로그가 안 뜬다)
            messagebox.showinfo("안내", "부족 카테고리가 없습니다.", parent=self.top)
            return
        # 부족 카테고리 다이얼로그 재사용
        sub = _FillCategoryDialog(self.top, list(missing))
        self.top.wait_window(sub.top)
        if not sub.result:
            return
        self.result = {
            "action": "fill",
            "category": sub.result.get("category"),
            "count": sub.result.get("count"),
        }
        self.top.destroy()

    def _on_matched(self):
        self.result = {"action": "matched"}
        self.top.destroy()

    def _cancel(self):
        self.result = {"action": "cancel"}
        self.top.destroy()
