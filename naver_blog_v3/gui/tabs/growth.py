# gui/tabs/growth.py — 성장 탭 (Phase 3 관제탑을 앱 안 한 화면으로)
"""일방문자 1만 목표 + 지표 + 다음 레버 + 클릭 가로챔 글 + 유입 성과 +
자동 실험 + 방문자 활성화 상태를 한 화면에 통합.

읽기 전용 — 발행/변경 없음. '운영 루프 실행'은 측정·집계·리포트만(run_ops).
데이터 로드는 백그라운드 스레드, UI 갱신은 UiLifecycle.ui_after 로만.
"""
import logging
import os
import re
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import messagebox, ttk

from gui.theme import (C, SP, FT, FB, FS, FM, F_NUM, L, Card, PrimaryBtn,
                       SecondaryBtn, GhostBtn, HAS_CTK, ctk,
                       run_async, bind_mousewheel)
from gui.theme import _pair as _theme_pair  # noqa: PLC2701 — theme<->tabs 상호참조 기존 관례(widgets.py 와 동일)
from gui.theme import _CtkFrameAdapter as _theme_ctk_frame_adapter  # noqa: PLC2701 — 위와 동일 관례
from gui.widgets import DataTable, Header, SectionCard, EmptyState, HelpIcon, IconButton
from gui.async_utils import UiLifecycle
from gui import glossary
from gui import icons
from gui import theme as _theme


def _px(n):
    """`theme.px(n)` 가 있으면 그걸(NBA_UI_SCALE 고DPI 스케일 반영), 없으면
    그대로 돌려준다(auto-dev 9호 — 코더 A 가 동시에 theme.px 를 만드는 중이라
    방어적으로 getattr 폴백). 모듈 임포트 시점에 한 번 캡처하지 않고 매번
    theme 모듈을 다시 조회한다 — 그래야 theme.px 가 나중에(또는 테스트에서
    monkeypatch 로) 생기면 재시작 없이 바로 반영된다."""
    scale_fn = getattr(_theme, "px", None)
    return scale_fn(n) if scale_fn else n

log = logging.getLogger(__name__)
BASE_DIR = Path(__file__).parent.parent.parent          # naver_blog_v3/

# 방문자 최신 측정일이 오늘 기준 이 일수를 넘으면 north-star note 를 '측정 라이브'(green)에서
# 'N일 전 데이터'(yellow)로 강등한다. 어제분(1일 지연)까지는 정상 수집 지연으로 본다.
VISITOR_STALE_DAYS = 2


class _StatsImportOptionsDialog(tk.Toplevel):
    """Explicitly collect the selected measurement window and period meaning."""
    _WINDOW_VALUES = {"1일": "d1", "7일": "d7", "28일": "d28"}
    _BASIS_VALUES = {"기간 합계": "period", "누적": "cumulative", "미확인": "unknown"}

    def __init__(self, parent):
        super().__init__(parent)
        self.result = None
        self.title("통계 기간 선택")
        self.resizable(False, False)
        self.transient(parent.winfo_toplevel())
        self.grab_set()
        body = tk.Frame(self, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="파일의 숫자가 가리키는 기간을 선택하세요.").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 10))
        tk.Label(body, text="측정 창").grid(row=1, column=0, sticky="w", padx=(0, 12))
        self.window_box = ttk.Combobox(body, state="readonly", values=tuple(self._WINDOW_VALUES), width=18)
        self.window_box.set("7일")
        self.window_box.grid(row=1, column=1, sticky="ew", pady=4)
        tk.Label(body, text="숫자의 의미").grid(row=2, column=0, sticky="w", padx=(0, 12))
        self.basis_box = ttk.Combobox(body, state="readonly", values=tuple(self._BASIS_VALUES), width=18)
        self.basis_box.set("미확인")
        self.basis_box.grid(row=2, column=1, sticky="ew", pady=4)
        tk.Label(body, text="미확인·누적 값은 검색 기획 환류에 사용하지 않아요.").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(7, 12))
        buttons = tk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="계속", command=self._accept).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def _accept(self):
        window = self._WINDOW_VALUES.get(self.window_box.get())
        basis = self._BASIS_VALUES.get(self.basis_box.get())
        if window and basis:
            self.result = {"window": window, "basis": basis}
            self.destroy()

# 방문자 실시간 조회 상태 원문(diagnose_visitor_activation 의 내부 값) → 사람 말.
# 모르는 값은 원문을 그대로 보여준다(fail-open — 새 상태값이 추가돼도 안 죽음).
_VISITOR_FETCH_LABELS = {"live": "됨", "zero": "안 됨"}

# "다음에 밀 레버" 문구는 scheduler.growth_dashboard._weakest_lever() 가 HTML
# 리포트용으로 만든 문자열을 그대로 받는다(마크다운 **볼드**·이모지 포함) —
# 순정 tk.Label 은 마크다운을 렌더링하지 않으므로 별표가 그대로 노출됐다
# (디자이너 실측). core 모듈은 손대지 않고(파일 경계 밖) 렌더 직전에 걷어낸다.
_MD_HEADER_RE = re.compile(r"(?m)^#+\s*")
_MD_BOLD_RE = re.compile(r"\*\*|__")
_LEVER_EMOJI_RE = re.compile("[🌀-🫿☀-➿⬀-⯿️]")


def _clean_lever_text(text: str) -> str:
    """마크다운 헤더(#)·볼드(**,__)·이모지를 제거한 표시용 문자열."""
    if not text:
        return text
    text = _MD_HEADER_RE.sub("", text)
    text = _MD_BOLD_RE.sub("", text)
    text = _LEVER_EMOJI_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()

# 활성 프로필의 리포트 디렉토리로 라우팅.
#   default(examplebrand) → naver_blog_automation_v3/reports (기존 경로, 무변화)
#   named 프로필        → profiles/<name>/reports (계정별 격리)
# 한 프로세스=한 프로필이라 import 시점 확정이 옳다.
try:
    from config import profile as _profile
    HARNESS_REPORTS = _profile.reports_dir()
except Exception:  # noqa: BLE001 — 프로필 모듈 부재 시 레거시 경로
    HARNESS_REPORTS = BASE_DIR.parent / "reports"  # naver_blog_automation_v3/reports


class GrowthTab(UiLifecycle, tk.Frame):
    # 핵심 지표 6카드 숫자 폰트 — v3 디자인 시스템 §3 은 KPI 숫자를 F_NUM(28
    # Bold, 위계 원칙의 유일한 크기 예외)으로 고정한다. 다만 900×620 처럼 좁은
    # 창에서는 두 줄 분리(§ _set_kpi)로도 여유가 빠듯해 안전판으로 폭이 좁을 때만
    # 한 단계 줄인다(디자이너 재실측 가드 — auto-dev 5호에서 확정한 완화책 유지).
    _KPI_FONT_WIDE = F_NUM
    _KPI_FONT_NARROW = (F_NUM[0], 22, "bold")
    _KPI_NARROW_THRESHOLD_BASE = 900  # 매 호출마다 _px() 로 다시 스케일한다(테스트에서
                                       # theme.px 를 나중에 patch 해도 즉시 반영되도록).

    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self.app = app
        self._init_lifecycle()
        self._build()

    # ── 알림/스케줄 헬퍼 ──────────────────────────────
    def _notify(self, text: str, kind: str = "info") -> None:
        """정보성 알림은 토스트, app.toast 가 없으면 messagebox 로 폴백한다."""
        toast = getattr(self.app, "toast", None)
        if toast is not None:
            try:
                toast.show(text, kind=kind)
                return
            except Exception:  # noqa: BLE001 — 토스트 실패는 폴백으로 흡수
                pass
        if kind == "error":
            messagebox.showerror("알림", text)
        elif kind == "warn":
            messagebox.showwarning("알림", text)
        else:
            messagebox.showinfo("알림", text)

    def _safe_after(self, delay_ms: int, callback) -> None:
        """구버전 호출부 호환용 별칭 — UiLifecycle.ui_after 로 위임."""
        self.ui_after(callback, delay=delay_ms)

    # ── 레이아웃 ──────────────────────────────────────
    def _build(self):
        header = Header(self, "성장", "방문자를 늘리기 위해 지금 할 일을 골라 줘요",
                        icon=icons.glyph("growth"))
        header.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["sm"]))
        # pack(side="right") 는 먼저 넣은 위젯이 가장 오른쪽에 남는다 — 화면 왼쪽부터
        # Primary·Secondary·새로고침 순으로 보이게 하려면 새로고침→Secondary→Primary
        # 순서로 패킹해야 한다(마지막에 넣은 것이 가장 왼쪽으로 밀려남).
        IconButton(header.actions, "refresh", self.refresh,
                  tooltip="새로고침").pack(side="right")
        self._inject_btn = SecondaryBtn(header.actions, "발행풀 주입", self._inject_queue,
                                        icon="add")
        self._inject_btn.pack(side="right", padx=(0, SP["sm"]))
        self._run_ops_btn = PrimaryBtn(header.actions, "운영 루프 실행", self._run_ops,
                                       icon="run")
        self._run_ops_btn.pack(side="right", padx=(0, SP["sm"]))

        outer = tk.Frame(self, bg=C["bg"])
        outer.pack(fill="both", expand=True)
        self._canvas = tk.Canvas(outer, bg=C["bg"], bd=0, highlightthickness=0)
        sb = tk.Scrollbar(outer, orient="vertical", command=self._canvas.yview)
        self._inner = tk.Frame(self._canvas, bg=C["bg"])
        self._inner.bind("<Configure>",
                         lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas.configure(yscrollcommand=sb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self._win = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfigure(self._win, width=e.width))
        bind_mousewheel(self._canvas)

        c = self._inner

        self._build_topic_mode(c)

        # ── 하루 방문자 히어로 카드 ───────────────────────
        ns_card = self._section(c, glossary.label("north_star"), "목표는 10,000명이에요.",
                                icon=icons.glyph("growth"))
        row = tk.Frame(ns_card.body, bg=C["card"])
        row.pack(fill="x")
        self._ns_val = L(row, "—", F_NUM, C["green"])
        self._ns_val.pack(side="left", anchor="s")
        self._ns_goal = L(row, "/ 10,000", FT, C["muted"])
        self._ns_goal.pack(side="left", anchor="s", padx=(8, 0), pady=(0, 6))
        if HAS_CTK:
            # CTkProgressBar 는 corner_radius 를 자체 지원해 알약(pill) 형태를
            # 그대로 그려준다 — tk.Canvas 수동 사각형보다 v3 디자인 시스템에
            # 맞는 실제 라운드 진행바(§5-9).
            self._ns_bar = ctk.CTkProgressBar(
                ns_card.body, orientation="horizontal", mode="determinate",
                height=_px(10), corner_radius=999,
                progress_color=_theme_pair("green"), fg_color=_theme_pair("surface"))
            self._ns_bar.set(0)
            self._ns_bar.pack(fill="x", pady=(8, 0))
        else:
            self._ns_bar = tk.Canvas(ns_card.body, height=_px(10), bg=C["surface"], bd=0,
                                     highlightthickness=0)
            self._ns_bar.pack(fill="x", pady=(8, 0))
        self._ns_note = L(ns_card.body, "", FS, C["muted"])
        self._ns_note.pack(anchor="w", pady=(4, 0))

        # ── 핵심 지표 6카드 ───────────────────────────────
        # 폰트 축소·uniform 그리드만으로는 좁은 카드에서 "측정 7/201" 같은 보조
        # 정보가 큰 숫자 뒤에 한 줄로 붙어 끝 글자가 잘렸다(2026-09-02 디자이너
        # 재실측, 900×620). 큰 값(짧게)과 보조 정보를 아예 두 줄로 분리한다.
        kpi_card = self._section(c, "핵심 지표", icon=icons.glyph("stats"))
        kf = tk.Frame(kpi_card.body, bg=C["card"])
        kf.pack(fill="x")
        # uniform= 없이 weight 만 주면 카드마다 내용 길이에 따라 열 폭이 들쭉날쭉해져
        # 옆 카드 숫자를 밀어내며 잘리게 된다 — 세 열을 강제로 동일 폭으로 묶는다.
        kf.columnconfigure((0, 1, 2), weight=1, uniform="kpi")
        self._kpi = {}            # key -> 큰 값 라벨(F_NUM, 짧은 텍스트만)
        self._kpi_sub = {}        # key -> 보조 정보 라벨(F_SMALL, text_2)
        self._kpi_cards = {}      # key -> 카드 Frame(폭 실측용)
        self._kpi_value_labels = []
        # 색은 hex 가 아니라 팔레트 토큰 이름으로 — Card() 는 HAS_CTK 면
        # CTkFrame 이라 retheme() 가 내부를 순회하지 않는다(_themed_label
        # 참고). 값을 C["green"] 처럼 미리 resolve 해 버리면 그 시점 팔레트
        # hex 로 굳어버려 이후 팔레트가 바뀌어도 안 따라간다.
        defs = [
            ("expose",   "검색 상위 노출율", None, "green"),
            ("aeo",      glossary.label("aeo"), "aeo", "accent"),
            ("eng",      "체류·경험 점수", "engagement", "purple"),
            ("perpost",  "편당 방문자", None, "yellow"),
            ("ranked",   "순위 추적 키워드", "rank_watch", "orange"),
            ("views",    "방문이 있는 글", None, "green"),
        ]
        for i, (key, title, help_key, color_token) in enumerate(defs):
            card = Card(kf, padx=_px(16), pady=_px(12))
            card.grid(row=i // 3, column=i % 3, padx=(0, _px(10)), pady=(0, _px(8)), sticky="nsew")
            title_row = self._themed_subframe(card)
            title_row.pack(anchor="w", fill="x")
            self._themed_label(title_row, title, FS, "muted").pack(side="left")
            if help_key:
                HelpIcon(title_row, glossary.help(help_key)).pack(side="left", padx=(4, 0))
            v = self._themed_label(card, "—", self._KPI_FONT_WIDE, color_token)
            v.pack(anchor="w", fill="x", pady=(3, 0))
            sub = self._themed_label(card, "", FS, "text_2", justify="left")
            sub.pack(anchor="w", fill="x")
            self._kpi[key] = v
            self._kpi_sub[key] = sub
            self._kpi_cards[key] = card
            self._kpi_value_labels.append(v)
            self._bind_kpi_sub_wrap(card, sub)
        self._bind_kpi_responsive_font()

        # ── 다음에 밀 레버 ────────────────────────────────
        lever_card = self._section(c, "다음에 밀 레버", "자동으로 진단한 다음 행동이에요.",
                                   icon=icons.glyph("info"))
        self._lever_card = lever_card
        self._lever_lbl = L(lever_card.body, "분석 중...", FB, C["text"],
                            wraplength=_px(760), justify="left")
        self._lever_lbl.pack(anchor="w")

        # ── 클릭 가로챔 용의자(다시 쓰기 1순위) ──────────────
        gap_card = self._section(c, glossary.label("click_gap"),
                                 glossary.help("click_gap"), icon=icons.glyph("warning"))
        gh = tk.Frame(gap_card.body, bg=C["card"])
        gh.pack(fill="x", pady=(0, 6))
        self._gap_badge = L(gh, "", FS, C["muted"])
        self._gap_badge.pack(side="right")
        self._gap_tree_wrap = tk.Frame(gap_card.body, bg=C["card"])
        self._gap_tree = DataTable(self._gap_tree_wrap, columns=("rank", "views", "kw", "title"),
                                   show="headings", height=6)
        for col, txt, w, anc in (("rank", "순위", 50, "center"), ("views", "최근방문", 70, "center"),
                                 ("kw", "검색어", 150, "w"), ("title", "제목", 380, "w")):
            self._gap_tree.heading(col, text=txt)
            self._gap_tree.column(col, width=_px(w), anchor=anc)
        self._gap_tree.pack(fill="both", expand=True)
        self._gap_empty = EmptyState(
            gap_card.body, icons.glyph("warning"), "아직 확인된 후보가 없어요",
            "순위·방문 데이터가 쌓이면 다시 쓸 글 후보가 여기 나와요.")
        self._sync_empty(self._gap_tree, self._gap_tree_wrap, self._gap_empty)
        self._gap_hint = L(gap_card.body, "", FS, C["muted"], wraplength=_px(760), justify="left")
        self._gap_hint.pack(anchor="w", pady=(4, 0))

        # ── 순위 모니터 ───────────────────────────────────
        rm_card = self._section(c, "순위 모니터", "등록한 검색어의 순위·실노출을 매일 확인해요.",
                                icon=icons.glyph("trend"))
        rmh = tk.Frame(rm_card.body, bg=C["card"])
        rmh.pack(fill="x", pady=(0, 6))
        self._rm_badge = L(rmh, "", FS, C["muted"])
        self._rm_badge.pack(side="right")
        self._rm_tree_wrap = tk.Frame(rm_card.body, bg=C["card"])
        self._rm_tree = DataTable(
            self._rm_tree_wrap, columns=("kw", "vol", "y", "t", "d", "serp", "spark"),
            show="headings", height=6, selectmode="extended")
        for col, txt, w, anc in (("kw", "키워드", 190, "w"), ("vol", "검색량/월", 90, "e"),
                                 ("y", "어제", 60, "center"), ("t", "오늘", 60, "center"),
                                 ("d", "변동", 60, "center"), ("serp", "실노출", 70, "center"),
                                 ("spark", "최근 7일", 110, "center")):
            self._rm_tree.heading(col, text=txt)
            self._rm_tree.column(col, width=_px(w), anchor=anc)
        self._rm_tree.pack(fill="both", expand=True)
        self._rm_empty = EmptyState(
            rm_card.body, icons.glyph("trend"), "아직 추적 중인 키워드가 없어요",
            "'키워드 추가'로 직접 등록하거나 '자동 제안'을 눌러 보세요.",
            action_text="자동 제안", action_cmd=lambda: self._rm_suggest())
        self._sync_empty(self._rm_tree, self._rm_tree_wrap, self._rm_empty)
        rm_btns = tk.Frame(rm_card.body, bg=C["card"])
        rm_btns.pack(fill="x", pady=(6, 0))
        PrimaryBtn(rm_btns, "키워드 추가", self._rm_add).pack(side="left")
        SecondaryBtn(rm_btns, "선택 해제", self._rm_remove).pack(side="left", padx=(8, 0))
        SecondaryBtn(rm_btns, "자동 제안", self._rm_suggest).pack(side="left", padx=(8, 0))
        self._rm_run_btn = SecondaryBtn(rm_btns, "지금 추적", self._rm_run)
        self._rm_run_btn.pack(side="right")
        self._rm_daily_btn = GhostBtn(rm_btns, "매일 자동: 확인 중", self._rm_toggle_daily, 18)
        self._rm_daily_btn.pack(side="right", padx=(0, 8))
        self._rm_hint = L(rm_card.body, "", FS, C["muted"], wraplength=_px(760), justify="left")
        self._rm_hint.pack(anchor="w", pady=(4, 0))

        # ── 유입 성과 (TOP 글 · 기회 키워드) ─────────────────
        perf_card = self._section(c, "유입 성과", "최근 7일 기준이에요.", icon=icons.glyph("stats"))
        perf = tk.Frame(perf_card.body, bg=C["card"])
        perf.pack(fill="x")
        perf.columnconfigure((0, 1), weight=1)
        tp = Card(perf, padx=_px(14), pady=_px(12))
        tp.grid(row=0, column=0, padx=(0, 8), sticky="nsew")
        self._themed_label(tp, "방문 TOP 글", FS, "accent").pack(anchor="w", pady=(0, 6))
        self._top_posts_frame = self._themed_subframe(tp)
        self._top_posts_frame.pack(fill="x")
        op = Card(perf, padx=_px(14), pady=_px(12))
        op.grid(row=0, column=1, padx=(8, 0), sticky="nsew")
        self._themed_label(op, "기회 키워드 (방문은 있는데 전용 글이 없어요)", FS,
                           "orange").pack(anchor="w", pady=(0, 6))
        self._opp_frame = self._themed_subframe(op)
        self._opp_frame.pack(fill="x")

        # ── 아래 4개(자동 실험·방문자 활성화·리포트·지원 리포트)는 접힘 기본 ──
        ab_card = self._section(c, glossary.label("ucb1"), glossary.help("ucb1"),
                                icon=icons.glyph("test"), collapsible=True)
        self._ab_tree_wrap = tk.Frame(ab_card.body, bg=C["card"])
        self._ab_tree = DataTable(self._ab_tree_wrap, columns=("exp", "best", "next", "samples"),
                                  show="headings", height=3)
        for col, txt, w, anc in (("exp", "실험", 200, "w"), ("best", "현재 우세", 160, "w"),
                                 ("next", "다음 시도", 160, "w"), ("samples", "표본", 120, "center")):
            self._ab_tree.heading(col, text=txt)
            self._ab_tree.column(col, width=_px(w), anchor=anc)
        self._ab_tree.pack(fill="both", expand=True)
        self._ab_empty = EmptyState(
            ab_card.body, icons.glyph("test"), "아직 실험 데이터가 없어요",
            "글을 발행하면 자동으로 변형이 배정되고 결과가 쌓여요.")
        self._sync_empty(self._ab_tree, self._ab_tree_wrap, self._ab_empty)

        va_card = self._section(c, "방문자 데이터 활성화", icon=icons.glyph("user"),
                                collapsible=True)
        vh = tk.Frame(va_card.body, bg=C["card"])
        vh.pack(fill="x", pady=(0, 6))
        self._va_badge = L(vh, "—", FS, C["muted"])
        self._va_badge.pack(side="right")
        self._va_card = Card(va_card.body, padx=_px(14), pady=_px(12))
        self._va_card.pack(fill="x")
        # 검색 API 스코프 점검 행 — 순위·유입이 0인 진짜 원인(스코프 401)을 최상단에 노출.
        self._va_scope = self._themed_label(
            self._va_card, "검색 API 스코프: 확인 중...", FS, "muted",
            wraplength=_px(740), justify="left")
        self._va_scope.pack(anchor="w", pady=(0, 6))
        # 성과 수집 상태 — 지금 근거가 무엇인지 / 마지막으로 유효하게 들어온 때 /
        # 실패했다면 무엇이 막고 있는지 / 다음 기획 반영이 왜 보류인지 / 할 일.
        self._perf_status = self._themed_label(
            self._va_card, "성과 수집 상태: 확인 중...", FS, "muted",
            wraplength=_px(740), justify="left")
        self._perf_status.pack(anchor="w", pady=(0, 2))
        self._perf_detail = self._themed_label(
            self._va_card, "", FS, "muted", wraplength=_px(740), justify="left")
        self._perf_detail.pack(anchor="w", pady=(0, 2))
        self._perf_action = self._themed_label(
            self._va_card, "", FS, "orange", wraplength=_px(740), justify="left")
        self._perf_action.pack(anchor="w", pady=(0, 6))
        measurement_row = tk.Frame(self._va_card, bg=C["card"])
        measurement_row.pack(fill="x", pady=(0, 3))
        try:
            from core import measurement as _measurement
            profile_enabled = bool(_measurement.profile_recording_enabled())
        except (ImportError, AttributeError, OSError, ValueError, TypeError):
            profile_enabled = False
        self._measurement_enabled_var = tk.BooleanVar(value=profile_enabled)
        self._measurement_saved_value = profile_enabled
        ttk.Checkbutton(measurement_row, text="이 계정의 성과 기록 사용",
                        variable=self._measurement_enabled_var).pack(side="left")
        SecondaryBtn(measurement_row, "저장", self._save_measurement_preference,
                     icon="save").pack(side="left", padx=(SP["sm"], 0))
        self._measurement_pref_status = self._themed_label(
            self._va_card, "성과 기록 설정을 확인 중...", FS, "muted",
            wraplength=_px(740), justify="left")
        self._measurement_pref_status.pack(anchor="w", pady=(0, 6))
        self._refresh_measurement_preference_status()
        self._va_actions = self._themed_subframe(self._va_card)
        self._va_actions.pack(fill="x")
        va_btns = tk.Frame(va_card.body, bg=C["card"])
        va_btns.pack(fill="x", pady=(8, 0))
        self._login_btn = PrimaryBtn(va_btns, "네이버 로그인(방문자 데이터 갱신)", self._refresh_login)
        self._login_btn.pack(side="left")
        self._ingest_btn = SecondaryBtn(va_btns, "통계 파일 불러오기", self._ingest_csv)
        self._ingest_btn.pack(side="left", padx=(8, 0))

        reports_card = self._section(c, "리포트", "지금까지 쌓인 데이터를 문서로 열어 볼 수 있어요.",
                                     icon=icons.glyph("log"), collapsible=True)
        bf = tk.Frame(reports_card.body, bg=C["card"])
        bf.pack(fill="x")
        SecondaryBtn(bf, "성장 현황 요약", lambda: self._open_report("_growth_dashboard.html")).pack(
            side="left")
        SecondaryBtn(bf, "주간 성장 리뷰", lambda: self._open_report("_growth_review.md")).pack(
            side="left", padx=(8, 0))
        SecondaryBtn(bf, "방문자 활성화", lambda: self._open_report("_visitor_activation.md")).pack(
            side="left", padx=(8, 0))
        self._status_lbl = L(reports_card.body, "", FS, C["muted"])
        self._status_lbl.pack(anchor="w", pady=(8, 0))

        support_card = self._section(
            c, "원격 지원 리포트",
            "문제 해결을 도와줄 사람에게 그대로 보낼 수 있는 진단 파일이에요 — 비밀번호·토큰 값은 담기지 않아요.",
            icon=icons.glyph("help"), collapsible=True)
        self._support_btn = SecondaryBtn(support_card.body, "지원 리포트 만들기", self._open_support_report)
        self._support_btn.pack(side="left")

        self.ui_after(self.refresh, delay=300)

    # ── 섹션/빈 상태 헬퍼 ─────────────────────────────
    _TOPIC_MODE_LABELS = {
        "자동": "auto", "일반": "general", "정부지원·정책": "government_policy",
        "IT·기술": "it", "여행": "travel", "후기·리뷰": "review", "학습·교육": "learning",
    }

    def _build_topic_mode(self, parent):
        """Expose the active profile's common topic mode without touching global settings."""
        card = self._section(
            parent, "이 계정의 글 주제",
            "자동은 제목·카테고리로 판단해요. 계정마다 선택을 저장할 수 있어요.",
            icon=icons.glyph("content"))
        try:
            from core import editorial_profile
            current = editorial_profile.load().get("topic_mode") or "auto"
        except (ImportError, AttributeError, OSError, ValueError, TypeError):
            current = "auto"
        self._topic_mode_var = tk.StringVar(value=next(
            (label for label, mode in self._TOPIC_MODE_LABELS.items() if mode == current), "자동"))
        row = tk.Frame(card.body, bg=C["card"])
        row.pack(fill="x")
        ttk.Combobox(row, textvariable=self._topic_mode_var, state="readonly",
                     values=tuple(self._TOPIC_MODE_LABELS), width=20).pack(side="left")
        SecondaryBtn(row, "주제 저장", self._save_topic_mode, icon="save").pack(
            side="left", padx=(SP["sm"], 0))
        self._topic_mode_status = L(card.body, "주제 분야는 이 계정에만 적용돼요.", FS, C["muted"])
        self._topic_mode_status.pack(anchor="w", pady=(SP["xs"], 0))
        L(card.body,
          "브랜드 장르 선택은 우선 유지돼요. 정부지원 선택은 정책형 안내를 쓰고, 실제 주제의 사실 확인은 계속 적용돼요.",
          FS, C["text_2"], wraplength=_px(760), justify="left").pack(anchor="w", pady=(SP["xs"], 0))

    def _save_topic_mode(self):
        """Persist the selected topic mode in the active profile's editorial file."""
        try:
            from core import editorial_profile
            data = editorial_profile.load()
            data["topic_mode"] = self._TOPIC_MODE_LABELS.get(self._topic_mode_var.get(), "auto")
            result = editorial_profile.save(data)
            if not result.get("saved"):
                raise OSError(str(result.get("reason") or "profile_write_failed"))
            self._topic_mode_status.config(text="저장했어요 — 새 글과 QA 재작성부터 적용돼요.")
        except (ImportError, AttributeError, OSError, ValueError, TypeError, tk.TclError) as exc:
            log.error("계정 주제 분야 저장 실패", exc_info=True)
            try:
                self._topic_mode_status.config(text=f"저장하지 못했어요 ({type(exc).__name__}).")
            except tk.TclError:
                pass

    def _refresh_measurement_preference_status(self) -> None:
        """Describe profile preference separately from OR activation sources."""
        try:
            from core.measurement import recording_status
            status = recording_status()
            if status.get("profile_preference_status") == "invalid":
                profile_text = "이 계정 설정 확인 필요(읽기 실패 또는 값 손상)"
            elif status.get("profile_inherits_global"):
                profile_text = "이 계정 설정 미지정(공통 설정 상속)"
            else:
                profile_text = "이 계정 설정 켬" if status["profile_enabled"] else "이 계정 설정 끔"
            effective_text = "현재 기록 활성" if status["effective"] else "현재 기록 비활성"
            causes = []
            if status.get("global_effective"):
                causes.append("공통 설정")
            if status["autonomy_active"]:
                causes.append("무인 운영 안전 규칙")
            suffix = f"; {' · '.join(causes)} 때문에 설정을 꺼도 기록될 수 있어요" if causes else ""
            self._set_themed_label(self._measurement_pref_status,
                                   f"성과 기록: {profile_text} · {effective_text}{suffix}", "muted")
        except (ImportError, AttributeError, OSError, ValueError, TypeError) as exc:
            log.debug("성과 기록 설정 상태 확인 실패(%s)", type(exc).__name__)
            self._set_themed_label(self._measurement_pref_status,
                                   "성과 기록 설정 상태를 확인하지 못했어요.", "muted")

    def _save_measurement_preference(self) -> None:
        """Save this profile's non-secret toggle and restore its visible value on failure."""
        requested = bool(self._measurement_enabled_var.get())
        previous = bool(getattr(self, "_measurement_saved_value", not requested))
        try:
            from core import measurement
            result = measurement.set_profile_recording_enabled(requested)
            if not result.get("saved"):
                raise OSError(str(result.get("reason") or "save_failed"))
            self._measurement_saved_value = requested
        except (ImportError, AttributeError, OSError, TypeError, ValueError) as exc:
            log.error("성과 기록 설정 저장 실패", exc_info=True)
            self._measurement_enabled_var.set(previous)
            self._set_themed_label(self._measurement_pref_status,
                                   f"저장 실패 — 기존 값으로 되돌렸어요 ({type(exc).__name__}).", "orange")
            return
        self._refresh_measurement_preference_status()

    def _section(self, parent, title, subtitle=None, icon=None, collapsible=False,
                 default_open=False):
        """SectionCard 를 만들어 packing 까지 마친다. collapsible=True 면 기본 접힘."""
        card = SectionCard(parent, title, subtitle=subtitle, icon=icon)
        card.pack(fill="x", padx=SP["xl"], pady=(0, SP["lg"]))
        if not collapsible:
            return card

        # v3 디자인 시스템 §7-5 — 접힘 섹션 토글은 화살표 텍스트가 아니라
        # IconButton chevron(펼침/접힘 방향에 따라 아이콘 자체가 바뀐다).
        toggle = IconButton(card.actions, "collapse" if default_open else "expand",
                            lambda: None, tooltip="펼치기/접기")

        def _apply_state(open_now: bool) -> None:
            if open_now:
                card.body.pack(fill="both", expand=True, padx=card._pad_x,
                               pady=(SP["sm"], SP["md"]))
                toggle.configure(text=icons.glyph("collapse"))
            else:
                card.body.pack_forget()
                toggle.configure(text=icons.glyph("expand"))

        state = {"open": default_open}

        def _toggle():
            state["open"] = not state["open"]
            _apply_state(state["open"])

        toggle.configure(command=_toggle)
        _apply_state(default_open)
        return card

    @staticmethod
    def _sync_empty(tree, wrap, empty: EmptyState) -> None:
        """표를 wrap 프레임에 넣어 두고, 항목이 0개면 EmptyState 로 교체한다."""
        if len(tree.get_children()) == 0:
            wrap.pack_forget()
            empty.show()
        else:
            empty.hide()
            wrap.pack(fill="both", expand=True)

    def _bind_kpi_responsive_font(self) -> None:
        """창 폭이 900px 미만이면 핵심 지표 숫자 폰트를 한 단계 줄인다.

        `kf.columnconfigure(uniform=)` 로 3열을 강제 동일폭으로 묶어도, 900×620
        같은 좁은 창에서는 20pt 볼드 숫자가 열 폭을 넘길 수 있어(디자이너 실측)
        폰트 자체를 줄이는 안전판을 둔다. 창 크기를 기준으로 삼아야 하므로
        toplevel 의 <Configure> 를 구독한다(내부 프레임 폭은 사이드바 때문에
        항상 900 보다 작아 기준으로 쓸 수 없다).
        """
        def _apply(event=None) -> None:
            try:
                win_w = self.winfo_toplevel().winfo_width()
            except tk.TclError:
                return
            narrow = bool(win_w) and win_w <= _px(self._KPI_NARROW_THRESHOLD_BASE)
            font = self._KPI_FONT_NARROW if narrow else self._KPI_FONT_WIDE
            for lbl in self._kpi_value_labels:
                try:
                    lbl.configure(font=font)
                except (tk.TclError, AttributeError):
                    pass

        try:
            self.winfo_toplevel().bind("<Configure>", lambda e: _apply(), add="+")
        except tk.TclError:
            pass
        _apply()

    _KPI_SUB_WRAP_THRESHOLD_BASE = 300  # 위와 동일 이유로 매 호출마다 _px() 재적용.

    def _set_kpi(self, key: str, big_text: str, small_text: str = "") -> None:
        """핵심 지표 한 칸을 갱신한다 — `self._kpi[key]` 는 항상 큰 값 라벨을 가리킨다.

        큰 값은 "0%"·"측정 7"처럼 짧게 유지하고, "측정 7/201"·"3/40 통과" 같은
        분모·세부 정보는 보조 줄(`self._kpi_sub[key]`)로 분리한다 — 한 줄에
        몰아넣으면 좁은 카드에서 뒷부분이 잘렸다(디자이너 재실측).

        `.configure()` 를 쓴다 — HAS_CTK 면 이 라벨들은 raw CTkLabel 이라
        `.config()` 별칭이 없다("'config' is not implemented for CTk
        widgets", 실측 확인. 채널 탭 통계 카드와 동일한 함정).
        """
        big = self._kpi.get(key)
        if big is not None:
            big.configure(text=big_text)
        sub = self._kpi_sub.get(key)
        if sub is not None:
            sub.configure(text=small_text)

    def _bind_kpi_sub_wrap(self, card, sub) -> None:
        """카드 폭이 `_KPI_SUB_WRAP_THRESHOLD_BASE` (스케일 적용) 미만이면 보조 줄에
        wraplength 를 건다.

        카드 실제 폭은 grid 배치가 끝나야 정해지므로 `<Configure>` 로 구독한다.
        """
        def _on_configure(event) -> None:
            w = event.width
            try:
                if w and w < _px(self._KPI_SUB_WRAP_THRESHOLD_BASE):
                    sub.configure(wraplength=max(w - _px(8), _px(40)))
                else:
                    sub.configure(wraplength=0)
            except (tk.TclError, AttributeError):
                pass
        card.bind("<Configure>", _on_configure)

    def _themed_label(self, parent, text, font, color_token=None, **kw):
        """`Card()`(CTkFrame) 안에 넣는 라벨 전용 헬퍼.

        `theme._retheme_widget_tree` 는 CTk 컨테이너를 만나면 하위로 내려가지
        않는다(문서화된 설계 — set_appearance_mode 가 (light,dark) 페어로
        만들어진 CTk 위젯만 자동으로 다시 그린다는 전제). 그 안에 plain
        tk.Label 을 단일 hex(C[token])로 넣으면 팔레트가 바뀌어도 그 라벨만
        계속 예전 색으로 남는다(디자이너 실측: "채널 탭 전체가 다크로
        남음") — 그래서 HAS_CTK 면 CTkLabel + `_theme_pair(token)` 페어로,
        아니면 기존 tk.Label(단일 hex)로 만든다.
        """
        if HAS_CTK:
            text_color = _theme_pair(color_token) if color_token else _theme_pair("text")
            return ctk.CTkLabel(parent, text=text, font=font, text_color=text_color,
                                fg_color="transparent", **kw)
        fg = C[color_token] if color_token else C["text"]
        return tk.Label(parent, text=text, font=font, fg=fg, bg=C["card"], **kw)

    @staticmethod
    def _themed_subframe(parent, **kw):
        """`Card()` 안에서 순수 레이아웃(자체 색 없음)용 컨테이너 — 위와 동일한
        이유로 HAS_CTK 면 CTkFrame(카드와 같은 페어 색, 테두리 없음).

        fg_color="transparent" 는 쓰지 않는다 — `_CtkFrameAdapter.cget("bg")`
        가 그 값을 그대로 돌려주는데, "transparent" 는 plain tk 위젯의
        bg=... 에 넘기면 곧장 TclError("unknown color name")로 죽는다(실측
        확인 — HelpIcon 처럼 이 프레임 아래 plain tk.Label 을 더 넣는 코드가
        있으면 크래시로 이어진다). 카드와 같은 실색 페어를 명시해 시각 결과는
        동일하게 "부모 카드 색과 구분 안 됨"을 유지하면서 크래시를 피한다.
        어댑터로 감싸는 이유도 같다 — raw CTkFrame 은 `.cget("bg")` 자체가
        ValueError 를 던진다(theme.py 문서화된 CTk cget 우회 특성).
        """
        if HAS_CTK:
            frame = ctk.CTkFrame(parent, fg_color=_theme_pair("card"),
                                 border_width=0, corner_radius=0, **kw)
            return _theme_ctk_frame_adapter(frame)
        return tk.Frame(parent, bg=C["card"], **kw)

    @staticmethod
    def _set_themed_label(label, text, color_token=None) -> None:
        """`_themed_label` 로 만든 라벨의 텍스트/색을 나중에 바꿀 때 쓴다 —
        CTkLabel 은 `text_color=`, tk.Label 은 `fg=` 로 옵션 이름이 다르다."""
        kwargs = {"text": text}
        if color_token is not None:
            if HAS_CTK:
                kwargs["text_color"] = _theme_pair(color_token)
            else:
                kwargs["fg"] = C[color_token]
        try:
            label.configure(**kwargs)
        except (tk.TclError, AttributeError):
            pass

    # ── 데이터 로드/적용 ──────────────────────────────
    def refresh(self):
        def _load() -> dict:
            d = {}
            try:
                from scheduler.growth_dashboard import collect_kpi, _weakest_lever
                d["kpi"] = collect_kpi()
                d["lever"] = _weakest_lever(d["kpi"])
            except Exception as e:
                log.debug("성장 KPI 로드 실패: %s", e, exc_info=True)
                d["kpi"], d["lever"] = None, ""
            try:
                from core import performance_db as pdb
                d["gap"] = pdb.rank_view_gap(n=12)
                s = pdb.get_summary()
                d["top_posts"] = s.get("top_posts", [])
                d["opportunities"] = s.get("opportunities", [])
                d["vseries"] = pdb.visitor_series(days=7)
            except Exception as e:
                log.debug("성과 요약 로드 실패: %s", e, exc_info=True)
                d["gap"], d["top_posts"], d["opportunities"] = None, [], []
            try:
                from core import stats_fetcher as sf
                d["diag"] = sf.diagnose_visitor_activation()
            except Exception as e:
                log.debug("방문자 진단 실패: %s", e, exc_info=True)
                d["diag"] = None
            # 성과 수집·환류 상태 — 저장된 증거만 읽는다(네트워크 0회).
            try:
                from core import measurement as _ms
                d["perf"] = _ms.status_summary()
            except Exception as e:  # noqa: BLE001
                log.debug("성과 수집 상태 로드 실패: %s", e, exc_info=True)
                d["perf"] = None
            # 검색 API 스코프 진단 — 순위·유입이 계속 0인 진짜 원인(콘솔 2분 작업)을
            # 노출한다. 네트워크 호출이라 fail-open + 타임아웃(verify 내부 API_REQUEST_TIMEOUT).
            d["scope"] = self._verify_search_scope()
            # 순위 모니터 — 워치리스트 행 + 일간 작업 등록 상태
            try:
                from core import rank_watchlist
                d["watch_rows"] = rank_watchlist.rows(days=7)
            except Exception as e:  # noqa: BLE001
                log.debug("워치리스트 로드 실패: %s", e, exc_info=True)
                d["watch_rows"] = None
            try:
                from scheduler.rank_monitor import check_rank_task
                reg, _msg = check_rank_task()
                d["rank_task"] = bool(reg)
            except Exception:  # noqa: BLE001
                d["rank_task"] = None
            return d

        def _worker():
            data = _load()
            self.ui_after(self._safe_apply, data)

        self.spawn(_worker, name="growth-refresh")

    @staticmethod
    def _verify_search_scope() -> dict:
        """검색 API 스코프 상태를 진단해 {"status": ...} 반환(값 노출 없음).

        status: ok | scope | auth | no_credentials | no_network | unknown

        - 자격증명이 비어 있으면(placeholder) 네트워크를 타지 않고 no_credentials.
        - 그 외에는 rank_tracker.verify() 로 검색 API 를 1회 조회(내부 타임아웃).
          verify() 는 stdout 에 안내를 출력하므로 GUI 에서는 억제한다.
        - 어떤 예외/타임아웃도 흡수해 unknown 으로 두고 GUI 를 막지 않는다(fail-open).
        """
        import contextlib
        import io

        try:
            from core.onboarding_status import _search_api_credentials_present
            if _search_api_credentials_present() is False:
                return {"status": "no_credentials"}
        except Exception as e:  # noqa: BLE001 — 자격증명 프리체크 실패는 무시하고 verify 로 진행
            log.debug("스코프 자격증명 프리체크 실패: %s", e, exc_info=True)
        try:
            from core.rank_tracker import verify
            with contextlib.redirect_stdout(io.StringIO()):
                result = verify()
            status = (result or {}).get("status", "unknown")
            return {"status": status}
        except Exception as e:  # noqa: BLE001 — 네트워크/임포트 등 모든 실패를 흡수(fail-open)
            log.debug("검색 API 스코프 verify 실패: %s", e, exc_info=True)
            return {"status": "unknown"}

    def _safe_apply(self, data):
        try:
            self._apply(data)
        except tk.TclError:
            pass  # 탭 전환/종료로 위젯 파괴됨

    def _apply(self, data):
        kpi = data.get("kpi")
        if kpi:
            goal = kpi.get("goal", 10000) or 10000
            tv = kpi.get("today_visitors", 0)
            self._ns_val.config(text=f"{tv:,}")
            self._ns_goal.config(text=f"/ {goal:,}")
            self._draw_progress(tv, goal)
            measured = kpi.get("posts_with_views", 0) > 0 or kpi.get("ranked", 0) > 0
            note_text, note_fg = self._northstar_note(data.get("vseries", []), measured)
            self._ns_note.config(text=note_text, fg=note_fg)
            ranked = kpi.get("ranked", 0)
            unmeasured = kpi.get("unmeasured", 0)
            sample_total = ranked + unmeasured
            # 상위노출율: 100%가 '완벽'이 아니라 '표본 N'임을 드러내려 분모(측정/전체)를
            # 보조 줄로 병기한다(큰 값은 짧게 유지 — 안 그러면 좁은 카드에서 잘린다).
            self._set_kpi("expose", f"{kpi.get('expose_rate',0):.0f}%",
                          f"측정 {ranked}/{sample_total}")
            self._set_kpi("aeo", f"{kpi.get('aeo_rate',0):.0f}%",
                          f"{kpi.get('aeo_pass',0)}/{kpi.get('aeo_total',0)} 통과")
            self._set_kpi("eng", f"{kpi.get('eng_rate',0):.0f}%", "")
            self._set_kpi("perpost", f"{kpi.get('per_post_visits',0):.0f}", "")
            # 순위추적: 검색량 미상(미측정)을 화면에서 사라지게 두지 않는다 — 보조 줄로.
            self._set_kpi("ranked", f"측정 {ranked}", f"미측정 {unmeasured}")
            self._set_kpi("views", f"{kpi.get('posts_with_views',0)}편", "")
        if data.get("lever"):
            self._lever_lbl.config(text=_clean_lever_text(data["lever"]))

        scope = data.get("scope")
        # KPI 의 '측정 키워드'(검색량 확인, 예: 7)를 함께 넘겨 gap 쪽 '순위기록'(예: 203)과
        # 같은 '순위' 단어로 두 값을 혼동시키지 않도록 라벨에서 구분한다.
        # 스코프도 함께 넘겨, gap 이 '순위 데이터 없음'일 때 진짜 원인(스코프 미등록)을 노출.
        self._apply_gap(data.get("gap"), kpi.get("ranked", 0) if kpi else 0, scope)
        self._apply_list(self._top_posts_frame, data.get("top_posts", []),
                         fmt=lambda r: (f"{r.get('views',0):>4} · " + (r.get('title') or '')[:30]),
                         empty="아직 방문 데이터가 없어요")
        self._apply_list(self._opp_frame, data.get("opportunities", []),
                         fmt=lambda r: (f"{r.get('visits',0):>4} · " + (r.get('keyword') or '')[:24]),
                         empty="아직 기회 키워드가 없어요")
        self._apply_ab(kpi.get("ab") if kpi else None)
        self._apply_diag(data.get("diag"))
        self._apply_perf_status(data.get("perf"))
        self._apply_watch(data.get("watch_rows"), data.get("rank_task"))
        self._apply_scope(scope)

    def _northstar_note(self, vseries, measured):
        """north-star 방문자 문맥 라벨 (text, fg) 산출.

        방문자 데이터가 오래되면(마지막 측정일이 오늘 대비 VISITOR_STALE_DAYS 초과)
        '측정 라이브'로 오인시키지 않고 'N일 전 데이터(마지막 YYYY-MM-DD)'로 강등한다.
        '어제 X'는 상대어 대신 실제 측정일 라벨로 정직화. 전부 fail-open.
        """
        vs = [r for r in (vseries or []) if r.get("count", 0) > 0]
        if not vs:
            if measured:
                return "측정 라이브 — 실측 기반", C["green"]
            return "측정 대기 — 방문자 데이터를 넣으면 실측으로 전환돼요", C["yellow"]

        last = vs[-1]
        last_date_str = last.get("date", "")
        avg = round(sum(r["count"] for r in vs) / len(vs))
        stale_days = self._days_since(last_date_str)

        # 스테일: 최신 측정일이 오늘 대비 임계 초과 → 라이브 아님(노랑 강등).
        if stale_days is not None and stale_days > VISITOR_STALE_DAYS:
            return (f"{stale_days}일 전 데이터 (마지막 측정 {last_date_str} · {last['count']:,}명) — "
                    f"최근 {len(vs)}일 평균 {avg:,}/일. 방문자 데이터를 다시 넣어 주세요",
                    C["yellow"])

        # 라이브: '어제'라는 상대어 대신 실제 측정일로 라벨링(오늘은 진행중일 수 있음).
        label = f"최근 측정 {last_date_str}" if last_date_str else "최근 측정"
        return (f"측정 라이브 · {label} {last['count']:,} · 최근 {len(vs)}일 평균 {avg:,}/일 (오늘은 진행중)",
                C["green"])

    @staticmethod
    def _days_since(date_str):
        """YYYY-MM-DD 와 오늘의 일수 차. 파싱 실패 시 None(스테일 판정 보류)."""
        if not date_str:
            return None
        try:
            return (date.today() - date.fromisoformat(date_str)).days
        except (ValueError, TypeError):
            return None

    def _draw_progress(self, val, goal):
        pct = max(0.0, min(1.0, (val / goal) if goal else 0))
        if HAS_CTK:
            try:
                self._ns_bar.set(pct)
            except tk.TclError:
                pass
            return
        c = self._ns_bar
        c.update_idletasks()
        c.delete("all")
        w = c.winfo_width()
        if w < 10:
            self.ui_after(self._draw_progress, val, goal, delay=200)
            return
        c.create_rectangle(0, 0, int(w * pct), 10, fill=C["green"], outline="")

    def _apply_gap(self, gap, kpi_ranked=0, scope=None):
        for it in self._gap_tree.get_children():
            self._gap_tree.delete(it)
        if not gap:
            self._gap_badge.config(text="불러오지 못했어요", fg=C["red"])
            self._gap_hint.config(text="")
            self._sync_empty(self._gap_tree, self._gap_tree_wrap, self._gap_empty)
            return
        st = gap.get("status")
        if st == "ok":
            sus = gap.get("suspects", [])
            self._gap_badge.config(text=f"후보 {len(sus)}편 · 임계 {gap.get('threshold')}",
                                   fg=C["orange"] if sus else C["green"])
            for s in sus:
                self._gap_tree.insert("", "end", values=(
                    f"{s['best_rank']}위", s["recent_views"], (s.get("keyword") or "")[:18],
                    (s.get("title") or "")[:44]))
            self._gap_hint.config(
                text=("이 글들은 검색 상위인데 방문이 적어요 — AI 요약이 클릭을 가져갔을 수 있어요. "
                      "다시 쓰기 후보로 가장 먼저 확인해 보세요."
                      if sus else "고순위 글의 방문이 고르게 나오는 중이에요(건강해요)."))
        elif st == "needs_view_data":
            # checked_ranked(순위기록, 예: 203)와 KPI ranked(검색량 확인, 예: 7)는 정의가 다르다.
            # 같은 '순위' 단어로 부르지 않도록 라벨을 명시 구분한다.
            self._gap_badge.config(text="측정 대기", fg=C["yellow"])
            self._gap_hint.config(
                text=(f"순위 기록 {gap.get('checked_ranked',0)}편"
                      f"(TOP3 {gap.get('top_rank_posts',0)}편 · 검색량 확인 {kpi_ranked}편) 있으나 방문이 0이에요. "
                      "아래 '방문자 데이터 활성화'로 데이터를 넣으면 후보가 자동으로 채워져요."))
        else:
            # 순위 데이터 자체가 없다 = rank_tracker 가 순위를 못 채웠다는 뜻인데,
            # 그 진짜 원인이 '검색 API 스코프 미등록(401)'인 경우가 많다.
            scope_status = (scope or {}).get("status")
            if scope_status in ("scope", "auth", "no_credentials"):
                self._gap_badge.config(text="스코프 미등록", fg=C["red"])
                self._gap_hint.config(
                    text=("순위 데이터가 없어요 — 진짜 원인은 검색 API 스코프 미등록이에요. "
                          "아래 '검색 API 스코프' 안내대로 developers.naver.com 에서 "
                          "'검색' API 를 사용설정(2분)하면 순위·방문 측정이 켜져요."))
            else:
                self._gap_badge.config(text=st or "—", fg=C["muted"])
                self._gap_hint.config(text="순위 데이터가 없어요 — 검색 API 연동을 먼저 켜 주세요.")
        self._sync_empty(self._gap_tree, self._gap_tree_wrap, self._gap_empty)

    def _apply_list(self, frame, rows, *, fmt, empty):
        for w in frame.winfo_children():
            w.destroy()
        if not rows:
            self._themed_label(frame, empty, FS, "muted").pack(anchor="w")
            return
        for r in rows[:8]:
            self._themed_label(frame, fmt(r), FM, "text").pack(anchor="w", fill="x")

    def _apply_ab(self, ab):
        for it in self._ab_tree.get_children():
            self._ab_tree.delete(it)
        if ab:
            for name, r in ab.items():
                self._ab_tree.insert("", "end", values=(
                    (r.get("description") or name)[:26], str(r.get("current_best") or "—"),
                    str(r.get("next_try") or "—"), f"{r.get('samples',0)} ({r.get('confidence','')})"))
        self._sync_empty(self._ab_tree, self._ab_tree_wrap, self._ab_empty)

    def _apply_diag(self, diag):
        for w in self._va_actions.winfo_children():
            w.destroy()
        if not diag:
            # _va_badge 는 va_card.body(SectionCard, plain tk) 바로 안이라
            # Card() 경계 밖 — 기존 .config() 그대로 안전하다.
            self._va_badge.config(text="불러오지 못했어요", fg=C["red"])
            return
        status = diag.get("status")
        label = {"active": "활성", "partial": "부분", "inactive": "미활성"}.get(status, status)
        self._va_badge.config(text=label,
                              fg={"active": C["green"], "partial": C["yellow"]}.get(status, C["red"]))
        chk = diag.get("checks", {})
        fetch_label = _VISITOR_FETCH_LABELS.get(
            chk.get("visitor_fetch"), chk.get("visitor_fetch", "—"))
        summary = (f"쿠키 {diag.get('cookie_count',0)}개 · 실시간 조회 {fetch_label} · "
                   f"방문 있는 글 {diag.get('posts_with_views',0)}편 · 방문자 데이터 {diag.get('visitor_days',0)}일")
        # _va_actions 는 Card() 안이라 _themed_label 로(위 _apply_scope 와 동일 이유).
        self._themed_label(self._va_actions, summary, FS, "muted").pack(anchor="w", pady=(0, 4))
        for a in diag.get("next_actions", []):
            self._themed_label(self._va_actions, f"• {a}", FS, "text",
                               wraplength=_px(740), justify="left").pack(anchor="w")

    # 검색 API 스코프 점검 행별 문구·색상 — verify status 를 사용자 언어로 번역.
    #   ok            : 초록 — 정상, 순위 추적 가동 가능
    #   scope/auth    : 빨강 — 콘솔 2분 작업 안내(순위·유입 0의 진짜 원인)
    #   no_credentials: 주황 — 검색 API 키부터 입력
    #   no_network    : muted — 일시 오류, 재시도
    #   unknown/기타  : muted — 확인 불가(fail-open, 중립)
    _SCOPE_ROWS = {
        "ok": ("검색 API 스코프: 정상 — 순위·방문 측정을 가동할 수 있어요.", "green"),
        "scope": ("검색 API 스코프: 미등록(401) — developers.naver.com → 내 애플리케이션 "
                  "→ API 설정 → '검색' 사용설정(2분)이 필요해요. 순위·방문이 0인 진짜 원인이에요.", "red"),
        "auth": ("검색 API 스코프: 인증/스코프 오류(401) — developers.naver.com → 내 애플리케이션 "
                 "→ API 설정 → '검색' 사용설정(2분) 또는 검색 API 키를 다시 확인해 주세요.", "red"),
        "no_credentials": ("검색 API 스코프: 자격증명 없음 — 설정 탭에서 검색 API "
                           "키를 먼저 입력해 주세요.", "orange"),
        "no_network": ("검색 API 스코프: 네트워크 오류 — 잠시 후 새로고침으로 다시 확인해 주세요.", "muted"),
    }

    def _apply_scope(self, scope):
        """검색 API 스코프 진단을 방문자 활성화 카드의 첫 행에 표시.

        scope 가 None(진단 부재)이거나 알 수 없는 status 면 중립(muted) 표기 —
        fail-open 으로 화면을 절대 막지 않는다.
        """
        status = (scope or {}).get("status", "unknown")
        text, color_key = self._SCOPE_ROWS.get(
            status, ("검색 API 스코프: 확인할 수 없어요 (새로고침으로 다시 시도해 보세요)", "muted"))
        # _va_scope 는 Card()(CTkFrame) 안의 라벨이라 _set_themed_label 로 —
        # 일반 .config(fg=...) 는 HAS_CTK 면 라벨이 raw CTkLabel 이라 실패한다.
        self._set_themed_label(self._va_scope, text, color_key)

    def _apply_perf_status(self, perf):
        """성과 수집 상태 3줄 — 출처/마지막 유효 수집 · 마지막 시도·보류 사유 · 할 일.

        `perf` 는 `measurement.status_summary()` 결과(네트워크 0회). None 이면
        중립 문구로 두고 화면을 막지 않는다(fail-open).
        """
        if not perf:
            self._set_themed_label(self._perf_status,
                                   "성과 수집 상태: 확인할 수 없어요.", "muted")
            self._set_themed_label(self._perf_detail, "", "muted")
            self._set_themed_label(self._perf_action, "", "orange")
            return

        last_ok = perf.get("last_success_at") or ""
        head = (f"성과 수집: {perf.get('source_label', '근거 없음')} · "
                f"마지막 유효 수집 {last_ok[:16].replace('T', ' ') if last_ok else '없음'}")
        profile = perf.get("profile") or ""
        if profile:
            head += f" · 계정 {profile}"
        self._set_themed_label(
            self._perf_status, head,
            "green" if perf.get("feedback_active") else
            ("text" if last_ok else "muted"))

        parts = []
        attempt = perf.get("last_attempt_at") or ""
        if attempt:
            line = f"마지막 시도 {attempt[:16].replace('T', ' ')}"
            if perf.get("failure_label"):
                line += f" — {perf['failure_label']}"
            if perf.get("next_retry_at"):
                line += f" (다음 자동 재시도 {perf['next_retry_at'][:16].replace('T', ' ')})"
            parts.append(line)
        cov = perf.get("coverage") or {}
        if cov.get("posts_with_log_no"):
            parts.append(f"표본 {cov.get('posts_measured', 0)}/{cov['posts_with_log_no']}편")
        if perf.get("feedback_active"):
            parts.append("다음 기획에 반영 중")
        elif perf.get("held_label"):
            parts.append(f"다음 기획 반영 보류 — {perf['held_label']}")
        self._set_themed_label(self._perf_detail, " · ".join(parts), "muted")
        self._set_themed_label(
            self._perf_action,
            f"할 일: {perf['action']}" if perf.get("action") else "", "orange")

    def _apply_watch(self, rows, rank_task):
        """순위 모니터 섹션 갱신 — 워치리스트 표 + 일간 자동 상태."""
        for it in self._rm_tree.get_children():
            self._rm_tree.delete(it)
        if rows is None:
            self._rm_badge.config(text="불러오지 못했어요", fg=C["red"])
        elif not rows:
            self._rm_badge.config(text="0개 — 키워드를 추가해 주세요", fg=C["muted"])
            self._rm_hint.config(
                text="'키워드 추가'로 직접 등록하거나 '자동 제안'(상위 노출·다시 쓰기 후보)을 써 보세요. "
                     "등록하면 매일 순위·통합검색 실노출을 추적하고 변동을 카카오로 알려드려요.")
        else:
            self._rm_badge.config(text=f"{len(rows)}개 추적 중", fg=C["green"])
            def _r(v):
                return f"{v}위" if v else "—"
            for r in rows:
                if r["delta"] is None:
                    d = "—"
                elif r["delta"] > 0:
                    d = f"▲{r['delta']}"
                elif r["delta"] < 0:
                    d = f"▼{-r['delta']}"
                else:
                    d = "="
                self._rm_tree.insert("", "end", values=(
                    r["keyword"], f"{r['volume']:,}" if r["volume"] else "—",
                    _r(r["yesterday"]), _r(r["today"]), d,
                    _r(r["serp_today"]), r["spark"]))
            self._rm_hint.config(
                text="실노출=통합검색 화면 기준(참고치). API 순위는 상위인데 실노출이 "
                     "비면 AI 요약이 클릭을 가져갔을 수 있어요 — 다시 쓰기 후보예요.")
        self._sync_empty(self._rm_tree, self._rm_tree_wrap, self._rm_empty)
        if rank_task is None:
            self._rm_daily_btn.config(text="매일 자동: 확인 실패")
        elif rank_task:
            self._rm_daily_btn.config(text="매일 자동: 켜짐 (끄기)")
        else:
            self._rm_daily_btn.config(text="매일 자동: 꺼짐 (켜기)")
        self._rank_task_registered = rank_task

    # ── 순위 모니터 액션 ──────────────────────────────
    def _rm_add(self):
        from tkinter import simpledialog
        kw = simpledialog.askstring(
            "키워드 추가", "추적할 검색 키워드를 입력하세요:", parent=self)
        if not kw:
            return
        from core import rank_watchlist
        ok, msg = rank_watchlist.add(kw)
        if not ok:
            self._notify(msg, "warn")
        self.refresh()

    def _rm_remove(self):
        sel = self._rm_tree.selection()
        if not sel:
            self._notify("해제할 키워드를 표에서 선택해 주세요", "warn")
            return
        from core import rank_watchlist
        for iid in sel:
            kw = self._rm_tree.item(iid, "values")[0]
            rank_watchlist.remove(kw)
        self.refresh()

    def _rm_suggest(self):
        """자동 제안 상위 5개를 워치리스트에 추가(백그라운드 — pdb 로드가 무거움)."""
        def _task():
            added, names = 0, []
            try:
                from core import rank_watchlist
                for c in rank_watchlist.suggest_candidates(5):
                    ok, _ = rank_watchlist.add(
                        c["keyword"], title=c.get("title", ""),
                        volume=c.get("volume"))
                    if ok:
                        added += 1
                        names.append(c["keyword"])
            except Exception as e:  # noqa: BLE001
                log.warning(f"자동 제안 실패: {e}")
            def _done():
                if added:
                    self._notify(f"{added}개 추가했어요: " + ", ".join(names), "ok")
                else:
                    self._notify("추가할 후보가 없어요 — 순위 데이터가 쌓이면 제안이 생겨요.", "info")
                self.refresh()
            self.ui_after(_done)
        self.spawn(_task, name="growth-rm-suggest")

    def _rm_run(self):
        """지금 추적 — API+실노출+알림+리포트를 백그라운드 1회 실행."""
        try:
            self._rm_run_btn.config(state="disabled", text="추적 중…")
        except tk.TclError:
            pass

        def _task():
            try:
                from scheduler.rank_monitor import run_rank_monitor
                run_rank_monitor(max_keywords=60)
            except Exception as e:  # noqa: BLE001
                log.warning(f"순위 모니터 실행 실패: {e}")
            def _done():
                try:
                    self._rm_run_btn.config(state="normal", text="지금 추적")
                except tk.TclError:
                    return
                self.refresh()
            self.ui_after(_done)
        self.spawn(_task, name="growth-rm-run")

    def _rm_toggle_daily(self):
        """매일 자동 추적 켜기/끄기 — 켤 때만 확인(홈 탭 알아서 운영 패턴)."""
        registered = getattr(self, "_rank_task_registered", None)
        turning_on = not registered
        if turning_on and not messagebox.askyesno(
                "매일 자동 추적",
                "매일 09:10 에 순위·실노출을 자동으로 추적하고\n"
                "변동이 있으면 카카오 한 줄로 알려드려요.\n\n켤까요?"):
            return
        try:
            self._rm_daily_btn.config(text="적용 중…")
        except tk.TclError:
            pass

        def _task():
            try:
                from scheduler.rank_monitor import (register_rank_task,
                                                    unregister_rank_task)
                ok, msg = (register_rank_task() if turning_on
                           else unregister_rank_task())
                log.info(f"[growth] 순위 모니터 일간 {'ON' if turning_on else 'OFF'}: "
                         f"{ok} {msg}")
            except Exception as e:  # noqa: BLE001
                log.warning(f"일간 등록 토글 실패: {e}")
            self.ui_after(self.refresh)
        self.spawn(_task, name="growth-rm-toggle-daily")

    # ── 액션 ──────────────────────────────────────────
    def _run_ops(self):
        if not messagebox.askyesno(
                "운영 루프 실행",
                "측정·집계·리포트 루프를 실행합니다(발행/변경 없음).\n"
                "순위 추적을 포함하면 수십 초~수 분 걸릴 수 있습니다.\n\n지금 실행할까요?"):
            return

        def _task():
            try:
                from scheduler.run_ops import run_ops
                out = run_ops(do_rank=False)  # GUI 트리거는 빠르게: 순위추적 제외(별도 스케줄)
                s = out.get("summary", {})
                self.ui_after(lambda: self._status_lbl.config(
                    text=f"운영 루프 완료 {s.get('ok',0)}/{s.get('total',0)}", fg=C["green"]))
                self.ui_after(self.refresh, delay=300)
            except Exception as e:
                log.error("운영 루프 실행 오류", exc_info=True)
                self.ui_after(lambda: self._status_lbl.config(text=f"오류: {str(e)[:40]}", fg=C["red"]))

        run_async(self._run_ops_btn, _task, "실행 중...")

    def _inject_queue(self):
        """기회 큐(큐레이션 세트)를 엑셀 발행 풀에 주입 — 발행기가 다음 실행에 픽업.

        계획 수립 → 3버튼 확인([실제 주입]/[미리보기만]/[취소]) → 분기 실행.
        발행 풀에 넣기만 함(실발행은 사용자 트리거). [취소]는 아무 파일도 쓰지 않는다.
        """
        def _task():
            try:
                from core.queue_injector import build_injection_plan
                result = build_injection_plan(source="curated")
                plan = result["plan"]
                if not plan:
                    self.ui_after(lambda: self._notify(
                        f"새로 주입할 항목이 없어요 (제외 {len(result['skipped'])}편 = "
                        "이미 발행풀·보관함에 있어요)", "info"))
                    return
                self.ui_after(lambda: self._confirm_injection(result))
            except Exception as e:
                log.error("큐 주입 오류", exc_info=True)
                self.ui_after(lambda: self._status_lbl.config(
                    text=f"오류: {str(e)[:40]}", fg=C["red"]))

        run_async(self._inject_btn, _task, "계획 수립 중...")

    def _confirm_injection(self, result: dict) -> None:
        """3버튼 확인 다이얼로그를 띄우고 사용자 선택에 따라 분기 실행(메인 스레드).

        [실제 주입] → inject_to_excel(dry_run=False) + 리포트 저장 + 다음 동선 안내.
        [미리보기만] → inject_to_excel(dry_run=True)(엑셀 미변경) + 리포트 저장.
        [취소] → 아무 파일도 쓰지 않는다(inject_to_excel 미호출·리포트 미저장).
        """
        plan = result["plan"]
        dates = [p["date"] for p in plan]
        head = "\n".join(f"  • {p['title'][:42]}" for p in plan[:4])
        summary = (
            f"글 {len(plan)}편을 엑셀 발행 풀에 주입합니다.\n"
            f"발행일: {dates[0]} ~ {dates[-1]} (하루 4편·주말 스킵)\n"
            f"제외(중복): {len(result['skipped'])}편\n\n"
            f"{head}\n\n"
            "발행 풀에 '준비'만 함 — 실제 발행은 발행 화면에서 사용자가 직접 눌러요.\n\n"
            "[실제 주입] 엑셀 발행 풀 최상단에 추가\n"
            "[미리보기만] 엑셀은 그대로, 계획 리포트만 저장\n"
            "[취소] 아무 것도 하지 않음(파일 미기록)")

        choice = _InjectConfirmDialog.ask(self, summary)
        self._run_injection(choice, result)

    def _run_injection(self, choice: str, result: dict) -> None:
        """확인 다이얼로그 선택(choice)에 따른 실제 주입/미리보기/취소 분기.

        choice: 'inject'(실주입) | 'preview'(미리보기 저장) | 'cancel'(무동작).
        분리 이유: 취소가 어떤 파일도 쓰지 않음을 헤드리스로 검증 가능하게(순수 분기).
        """
        if choice == "cancel":
            self._status_lbl.config(text="주입을 취소했어요 (변경 없음)", fg=C["muted"])
            return

        from core.queue_injector import inject_to_excel, render_report
        plan = result["plan"]
        dry_run = (choice != "inject")
        io_r = inject_to_excel(plan, dry_run=dry_run)
        try:
            today = date.today().strftime("%Y-%m-%d")
            md = render_report(result, io_r, date_str=f"({today})")
            (HARNESS_REPORTS / f"{today}_queue_injection.md").write_text(
                md, encoding="utf-8")
        except Exception as e:  # noqa: BLE001 — 리포트 저장 실패는 주입 결과에 영향 없음
            log.debug("주입 리포트 저장 실패: %s", e)

        if io_r.get("error"):
            self._status_lbl.config(text=f"주입 오류: {io_r['error'][:30]}", fg=C["red"])
            self._notify(f"주입 중 오류가 났어요: {io_r['error'][:60]}", "error")
        elif choice == "inject":
            injected = io_r.get("injected", 0)
            if injected:
                # 다음 동선: 발행 화면 어디서 픽업하는지 명시(멱등 재주입은 위 안내로 대체).
                self._status_lbl.config(
                    text=f"발행풀 최상단 {injected}편 추가 — '발행·진행'에서 오늘 실행 시 픽업돼요",
                    fg=C["green"])
                self._notify(f"발행풀에 {injected}편을 추가했어요", "ok")
            else:
                self._status_lbl.config(
                    text="추가된 글이 없어요 (이미 발행풀에 있어요)", fg=C["muted"])
        else:
            self._status_lbl.config(
                text=f"미리보기 {len(plan)}편 (엑셀은 그대로, 리포트만 저장했어요)", fg=C["muted"])

    def _refresh_login(self):
        """발행 없이 네이버 로그인만 수행해 방문자 데이터 조회용 세션을 갱신한다."""
        app = self.app
        confirm_title = "네이버 로그인 (방문자 데이터 갱신)"
        confirm_body = (
            "브라우저 창을 열어 네이버에 로그인하고 방문자 데이터 조회용 세션을 갱신합니다.\n"
            "발행은 하지 않습니다.\n"
            "보안 인증(2차)이 뜨면 브라우저에서 완료한 뒤 '발행·진행' 화면의 '인증 완료'를 눌러 주세요.\n\n"
            "지금 진행할까요?")

        start_login_refresh = getattr(app, "start_login_refresh", None)
        if callable(start_login_refresh):
            if getattr(app, "_running", False):
                self._notify("다른 작업이 진행 중이에요. 끝나면 다시 시도해 주세요.", "warn")
                return
            if not messagebox.askyesno(confirm_title, confirm_body):
                return
            start_login_refresh(on_done=self.refresh)
            return

        # ── 폴백: App 서비스가 아직 없는 환경(FakeApp 포함)에서도 동작해야 한다 ──
        if getattr(app, "_running", False):
            self._notify("다른 작업이 진행 중이에요. 끝나면 다시 시도해 주세요.", "warn")
            return
        if not messagebox.askyesno(confirm_title, confirm_body):
            return
        if hasattr(app, "show_tab"):
            app.show_tab("posting")        # 인증 대기 UI + 로그를 재사용
        if hasattr(app, "_stop"):
            app._stop.clear()
        if hasattr(app, "auth_event"):
            app.auth_event.clear()
        self.spawn(self._login_worker_fallback, name="growth-login-fallback")

    def _login_worker_fallback(self):
        """App 에 start_login_refresh 서비스가 없을 때만 쓰는 폴백 로그인 워커.

        posting 탭이 있으면 그 로그·인증대기 UI 를 재사용하고, 없으면(FakeApp)
        조용히 상태만 갱신한다 — 모든 위젯 접근은 ui_after 를 거친다.
        """
        app = self.app
        app._running = True
        pt = app.pages.get("posting") if hasattr(app, "pages") else None

        def auth_cb(state):
            if pt is None:
                return
            if state == "waiting":
                self.ui_after(pt.show_auth_waiting)
                self.ui_after(lambda: pt.log(
                    "보안 인증이 필요해요 — 브라우저에서 인증 후 '인증 완료'를 눌러 주세요", "warn"))
                if hasattr(app, "set_status"):
                    self.ui_after(lambda: app.set_status("보안 인증 대기 중", C["orange"]))
            elif state == "done":
                self.ui_after(pt.hide_auth_waiting)
                if hasattr(app, "set_status"):
                    self.ui_after(lambda: app.set_status("● 대기 중", C["muted"]))

        def _log(msg, tag=""):
            if pt is not None:
                self.ui_after(lambda: pt.log(msg, tag))

        try:
            _log("네이버 로그인(방문자 데이터 갱신) 시작 — 발행 없음", "info")
            if hasattr(app, "set_status"):
                self.ui_after(lambda: app.set_status("네이버 로그인 중...", C["accent"]))
            from core.naver_uploader import refresh_session_cookies
            res = refresh_session_cookies(
                auth_event=getattr(app, "auth_event", None), auth_cb=auth_cb)
            if res.get("ok"):
                _log(f"로그인 성공 — 세션 {res.get('count', 0)}개 갱신", "ok")
                try:
                    from core.stats_fetcher import collect_stats
                    v = collect_stats() or {}
                    _log(f"  방문자 조회: 오늘 {v.get('today', 0)} / 누적 {v.get('total', 0)}"
                         + ("  (0이면 통계 파일 불러오기가 확실해요)" if not v.get("total") else ""), "info")
                except Exception as e:
                    log.debug("로그인 후 방문자 조회 실패: %s", e)
                if hasattr(app, "set_status"):
                    self.ui_after(lambda: app.set_status("방문자 데이터 갱신 완료", C["green"]))
                self.ui_after(lambda: self._notify("방문자 데이터를 갱신했어요", "ok"))
            else:
                _log(f"로그인 실패: {res.get('error', '')}", "err")
                if hasattr(app, "set_status"):
                    self.ui_after(lambda: app.set_status("로그인 실패", C["red"]))
                self.ui_after(lambda: self._notify(f"로그인에 실패했어요: {res.get('error','')}", "error"))
        except Exception as e:
            log.error("방문자 데이터 갱신 오류", exc_info=True)
            _log(f"오류: {e}", "err")
            if hasattr(app, "set_status"):
                self.ui_after(lambda: app.set_status(f"{str(e)[:30]}", C["red"]))
        finally:
            app._running = False
            if hasattr(app, "auth_event"):
                app.auth_event.clear()
            if pt is not None:
                self.ui_after(pt.hide_auth_waiting)
            self.ui_after(self.refresh, delay=400)   # 성장 화면 방문자 상태 갱신

    def _ingest_csv(self):
        """네이버 블로그 '통계 내보내기'(CSV/XLSX)를 골라 글별 방문 데이터를 적재한다."""
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="네이버 통계 내보내기 파일 선택 (CSV/XLSX)",
            filetypes=[("통계 파일", "*.csv *.xlsx *.xlsm"), ("모든 파일", "*.*")])
        if not path:
            return
        options_dialog = _StatsImportOptionsDialog(self.winfo_toplevel())
        self.wait_window(options_dialog)
        if options_dialog.result is None:
            return
        selected_window = options_dialog.result["window"]
        selected_basis = options_dialog.result["basis"]

        def _task():
            try:
                # 검색어·유입경로와 측정값은 같은 충돌 판정을 통과한 한 경로에서 적재한다.
                from core import stats_csv
                r = stats_csv.import_file(path, window=selected_window, basis=selected_basis)
                referral_rows = int(r.get("referral_rows") or 0)
                if not r.get("ok"):
                    reason = str(r.get("reason") or "")
                    human = {
                        "file_missing": "파일을 찾을 수 없어요.",
                        "no_rows": "파일에 읽을 행이 없어요.",
                        "no_identifier_column": "'제목' 또는 '글번호' 칸이 필요해요.",
                        "no_metric_column": "'조회수' 또는 '검색 유입' 칸이 필요해요.",
                        "no_matched_rows": "이 파일의 글을 발행 이력에서 찾지 못했어요.",
                    }.get(reason, f"조회수를 받아들이지 못했어요({reason}).")
                    if r.get("rows_conflicting"):
                        details = r.get("conflict_details") or []
                        groups = "; ".join(
                            f"{item.get('title')} / {item.get('day')} ({', '.join(item.get('metrics') or [])}, "
                            f"행 {', '.join(str(i) for i in item.get('rows') or [])})"
                            for item in details[:4])
                        detail = (f"상충하는 글·날짜 {r['rows_conflicting']}묶음만 보류했어요. "
                                  f"나머지 검색어 {referral_rows}행은 저장했어요. {groups}")
                    else:
                        detail = (f"검색어 {referral_rows}행은 저장했어요. {human}")
                    self.ui_after(lambda: self._notify(detail, "warn"))
                    self.ui_after(self.refresh, delay=300)
                    return
                metrics = []
                if r.get("matched_views"):
                    metrics.append(f"조회수 {r['matched_views']}편")
                if r.get("matched_search_inflow"):
                    metrics.append(f"검색 유입 {r['matched_search_inflow']}편")
                metric_text = " · ".join(metrics) or "유효 수치 없음"
                msg = (f"{metric_text} · 기간 {selected_window} / 의미 {selected_basis} · 검색어 "
                       f"{referral_rows}행 저장 "
                       f"(중복 {r.get('rows_duplicate', 0)} · 형식 오류 "
                       f"{r.get('rows_invalid', 0)} · 못 찾음 {r.get('unmatched', 0)})")
                if r.get("rows_conflicting"):
                    details = r.get("conflict_details") or []
                    groups = "; ".join(
                        f"{item.get('title')} {item.get('day')} ({', '.join(item.get('metrics') or [])}, "
                        f"행 {', '.join(str(i) for i in item.get('rows') or [])})"
                        for item in details[:4])
                    msg += f" · 상충한 {r['rows_conflicting']}묶음만 보류: {groups}"
                # 제목 유사도로만 붙인 행은 숨기지 않는다 — 신뢰 범위 밖이라 환류에 안 쓰인다.
                if r.get("matched_by_fuzzy"):
                    msg += f" · 제목 유사로 추정 {r['matched_by_fuzzy']}편(기획 반영 제외)"
                if r.get("rejected_log_no"):
                    msg += f" · 글번호를 이력에서 못 찾음 {r['rejected_log_no']}행"
                held = str(r.get("held_reason") or "")
                if held:
                    try:
                        from core.measurement import held_label as _held_label
                        held_human = f"다음 기획 반영은 보류예요 — {_held_label(held)}."
                    except Exception:  # noqa: BLE001
                        held_human = f"다음 기획 반영은 보류예요({held})."
                    msg = f"{msg} — {held_human}"
                if r.get("measured_search_inflow"):
                    msg += (f" · 기획 지표: 검색 유입/{selected_window}/{selected_basis} "
                            f"({r['measured_search_inflow']}편 기록)")
                elif r.get("measurements_skipped"):
                    msg += " · 측정 기록 건너뜀: " + ", ".join(
                        f"{reason} {count}건" for reason, count in r["measurements_skipped"].items())
                measurement_count = (int(r.get("measured_views") or 0)
                                     + int(r.get("measured_search_inflow") or 0))
                if r.get("measurements_skipped") and not measurement_count:
                    msg += " · 지표 측정값은 저장되지 않았어요."
                self.ui_after(lambda: self._notify(
                    msg, "warn" if (r.get("rows_conflicting")
                                    or (r.get("measurements_skipped") and not measurement_count))
                    else "ok"))
                self.ui_after(self.refresh, delay=300)   # 후보 분석 즉시 갱신
            except Exception as e:
                log.error("통계 인제스트 오류", exc_info=True)
                self.ui_after(lambda: self._notify(f"불러오기 중 오류가 났어요: {str(e)[:200]}", "error"))

        run_async(self._ingest_btn, _task, "불러오는 중...")

    def _open_report(self, suffix: str):
        today = date.today().strftime("%Y-%m-%d")
        target = HARNESS_REPORTS / f"{today}{suffix}"
        if not target.exists():
            # 오늘자 없으면 같은 접미사 최신본
            cands = sorted(HARNESS_REPORTS.glob(f"*{suffix}"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            target = cands[0] if cands else None
        if not target or not target.exists():
            self._notify("아직 이 리포트가 없어요 — '운영 루프 실행'으로 만들어 보세요.", "info")
            return
        try:
            os.startfile(str(target))  # noqa: S606 (Windows 전용 GUI)
        except OSError as e:
            self._notify(f"열기에 실패했어요: {e}", "error")

    def _open_support_report(self):
        """비밀 값 없는 원격 지원 진단 리포트를 생성해 연다.

        문제 해결을 도와줄 사람에게 그대로 보낼 수 있는 '건강 상태' 한 장 —
        계정은 마스킹, 쿠키는 존재/신선도만, 비밀번호·토큰의 값은 담지 않는다
        (support_report 가 보장). 생성은 백그라운드, 열기·상태 갱신은 메인 스레드.
        """
        def _task():
            try:
                from core.support_report import save_support_report
                path = save_support_report()
            except Exception as e:  # noqa: BLE001 — 생성 실패는 알리고 흡수(fail-open)
                log.error("원격 지원 리포트 생성 오류", exc_info=True)
                self.ui_after(lambda: self._notify(f"생성에 실패했어요: {str(e)[:120]}", "error"))
                return
            self.ui_after(lambda: self._after_support_report(path))

        run_async(self._support_btn, _task, "생성 중...")

    def _after_support_report(self, path):
        """생성된 리포트를 열고 '비밀값 없음'을 상태 라벨에 명시(메인 스레드)."""
        try:
            os.startfile(str(path))  # noqa: S606 (Windows 전용 GUI)
        except OSError as e:
            self._notify(f"저장은 됐지만 열기에 실패했어요: {e}", "warn")
        try:
            self._status_lbl.config(
                text="지원 리포트를 만들었어요 — 비밀값은 담기지 않아요",
                fg=C["green"])
        except tk.TclError:
            pass


class _InjectConfirmDialog(tk.Toplevel):
    """발행 풀 주입 확인 — [실제 주입] / [미리보기만] / [취소] 3버튼 모달.

    askyesno('아니오=미리보기 저장')의 혼란(취소가 없어 창을 띄운 이상 항상 파일이 써짐)을
    제거한다. 명시적 [취소]는 choice='cancel' 로 아무 동작도 유발하지 않는다.

    사용: choice = _InjectConfirmDialog.ask(parent, summary)  # 'inject'|'preview'|'cancel'
    창을 닫기(X)/Esc 로 종료하면 '취소'로 간주(무동작 기본값).
    """

    def __init__(self, parent, summary: str):
        super().__init__(parent)
        self.choice = "cancel"          # 닫기/Esc 기본값 = 무동작
        self.title("발행 풀 주입")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(parent)
        self._build(summary)
        # 창 관리자 닫기(X) 와 Esc 는 취소로 수렴.
        self.protocol("WM_DELETE_WINDOW", lambda: self._choose("cancel"))
        self.bind("<Escape>", lambda e: self._choose("cancel"))

    def _build(self, summary: str) -> None:
        pad = tk.Frame(self, bg=C["bg"], padx=22, pady=18)
        pad.pack(fill="both", expand=True)
        L(pad, "발행 풀 주입", FT, C["accent"]).pack(anchor="w", pady=(0, 8))
        L(pad, summary, FB, C["text"], justify="left", wraplength=_px(440)).pack(anchor="w")
        bar = tk.Frame(pad, bg=C["bg"])
        bar.pack(fill="x", pady=(18, 0))
        # 오른쪽 정렬: [취소] [미리보기만] [실제 주입] (주 액션이 가장 오른쪽·강조색).
        GhostBtn(bar, "취소", lambda: self._choose("cancel"), 8).pack(side="right", padx=(6, 0))
        GhostBtn(bar, "미리보기만", lambda: self._choose("preview"), 12).pack(side="right", padx=(6, 0))
        PrimaryBtn(bar, "실제 주입", lambda: self._choose("inject")).pack(side="right")

    def _choose(self, choice: str) -> None:
        self.choice = choice
        self.destroy()

    @classmethod
    def ask(cls, parent, summary: str) -> str:
        """모달로 띄우고 사용자 선택('inject'|'preview'|'cancel')을 반환(블로킹)."""
        dlg = cls(parent, summary)
        try:
            dlg.grab_set()              # 모달 — 부모 상호작용 차단
        except tk.TclError:
            pass                        # 헤드리스/미표시 상태에서는 무해하게 건너뜀
        parent.wait_window(dlg)
        return dlg.choice
