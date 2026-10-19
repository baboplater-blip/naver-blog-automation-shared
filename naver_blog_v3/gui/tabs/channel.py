# gui/tabs/channel.py — 외부 채널 (카카오 + 카페) 탭
import tkinter as tk
from tkinter import ttk, messagebox
import json
import re
import logging
from datetime import datetime
from typing import Optional

log = logging.getLogger(__name__)

from gui.theme import (C, SP, FB, FS, F_NUM, L, Card, Ent, bind_mousewheel,
                       PrimaryBtn, SecondaryBtn, DangerBtn, GhostBtn,
                       HAS_CTK, ctk)
from gui.theme import _pair as _theme_pair  # noqa: PLC2701 — theme<->tabs 상호참조 기존 관례
from gui.widgets import Header, SectionCard, StatusPill, FormField, DataTable
from gui.async_utils import UiLifecycle
from gui import icons
from gui import theme as _theme


def _px(n):
    """`theme.px(n)` 가 있으면 그걸(고DPI 스케일 반영), 없으면 그대로 —
    auto-dev 9호, growth.py 의 동일 헬퍼와 같은 방어적 폴백 패턴."""
    scale_fn = getattr(_theme, "px", None)
    return scale_fn(n) if scale_fn else n


class ChannelTab(UiLifecycle, tk.Frame):
    """카카오 오픈채팅 알림 + 네이버 카페 반자동 배포"""

    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self.app        = app
        self._queue     = []
        self._cafe_targets_vars = []   # 카페 설정 동적 위젯
        self._init_lifecycle()
        self._build()

    # ── 알림 헬퍼 ─────────────────────────────────────
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

    # ─────────────────────────────────────────────────
    def _build(self):
        header = Header(self, "외부 채널", "카카오 알림·네이버 카페 배포를 관리해요.",
                        icon=icons.glyph("channel"))
        header.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["sm"]))

        # 스크롤 컨테이너 — 안쪽 프레임의 폭을 캔버스(=보이는 뷰포트) 폭에
        # 묶지 않으면 긴 줄(카카오 가이드의 URL 등)이 프레임을 실제 창보다
        # 넓게 밀어붙여, KPI 카드 반응형 그리드가 "화면에 보이는 폭"이 아니라
        # 그 부풀려진 폭을 기준으로 계산하게 된다(디자이너 실측 근본 원인 —
        # 4번째 카드가 화면 밖으로 밀리는 현상은 이 폭 불일치가 진짜 원인이었다).
        canvas = tk.Canvas(self, bg=C["bg"], bd=0, highlightthickness=0)
        vsb    = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        self._sf = tk.Frame(canvas, bg=C["bg"])
        self._sf.bind("<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=self._sf, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        bind_mousewheel(canvas)

        self._build_stats_bar()
        self._build_kakao_section()
        self._build_cafe_settings_section()
        self._build_queue_section()
        self._build_log_section()
        self._wire_table_tag_colors()
        self._load_all()

    # ── 상단 통계 바 ─────────────────────────────────
    def _build_stats_bar(self):
        bar = SectionCard(self._sf, "채널 현황", icon=icons.glyph("stats"))
        bar.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))
        self._stats_row = tk.Frame(bar.body, bg=C["card"])
        self._stats_row.pack(fill="x")

        self._stat_labels = {}
        self._stat_cards = []
        # 색은 hex 값이 아니라 팔레트 토큰 이름으로 들고 있는다 — Card() 는
        # HAS_CTK 면 CTkFrame 이고, retheme() 는 CTk 컨테이너 내부를 순회하지
        # 않는다(theme.py 에 문서화된 설계: 라이트/다크 전환은 set_appearance_mode
        # 가 (light,dark) 페어로 만들어진 CTk 위젯만 자동으로 다시 그린다).
        # 안에 넣는 라벨을 단일 hex(C[token])로 만들면 팔레트가 바뀌어도 그
        # 카드만 계속 예전 색으로 남는다(디자이너 실측: "channel 탭 전체가
        # 다크로 남음") — 그래서 카드 내부는 CTkLabel + _theme_pair(token) 로.
        stats_def = [
            ("kakao_total",   "카카오 발송",   "yellow"),
            ("cafe_total",    "카페 등록",     "green"),
            ("queue_pending", "승인 대기",     "orange"),
            ("queue_sent",    "배포 완료",     "accent"),
        ]
        for key, label, color_token in stats_def:
            box, num = self._build_kpi_card(self._stats_row, label, color_token)
            self._stat_cards.append(box)
            self._stat_labels[key] = num
        # 900×620 최소 창에서 고정 1×4 그리드는 4번째 카드("배포 완료")를
        # 화면 밖으로 밀어냈다(디자이너 실측) — 가용 폭 기준 반응형 재배치.
        self._bind_kpi_grid(self._stats_row, self._stat_cards,
                            threshold=_px(720), wide_cols=4, narrow_cols=2)

    @staticmethod
    def _build_kpi_card(parent, label_text: str, color_token: str):
        """KPI 카드 1개(라벨 Caption + 숫자 F_NUM) — (card, value_label) 반환.

        HAS_CTK 면 카드도 값 라벨도 전부 CTk 위젯 + 페어 색(위 _build_stats_bar
        주석 참고). tk 폴백 경로는 기존 방식(단일 hex) 그대로 유지한다.
        """
        box = Card(parent, padx=SP["lg"], pady=SP["md"])
        if HAS_CTK:
            ctk.CTkLabel(box, text=label_text, font=FS,
                        text_color=_theme_pair("text_2"),
                        fg_color="transparent").pack(anchor="w")
            num = ctk.CTkLabel(box, text="—", font=F_NUM,
                               text_color=_theme_pair(color_token),
                               fg_color="transparent")
            num.pack(anchor="w", pady=(SP["xs"], 0))
        else:
            tk.Label(box, text=label_text, font=FS, fg=C["text_2"],
                     bg=C["card"], anchor="w").pack(anchor="w")
            num = tk.Label(box, text="—", font=F_NUM, fg=C[color_token],
                           bg=C["card"], anchor="w")
            num.pack(anchor="w", pady=(SP["xs"], 0))
        return box, num

    @staticmethod
    def _bind_kpi_grid(container, cards, *, threshold: int, wide_cols: int,
                       narrow_cols: int) -> None:
        """container 실제 폭이 threshold 미만이면 narrow_cols 열, 아니면
        wide_cols 열로 카드를 재배치한다(좁은 창에서 카드가 화면 밖으로
        밀리는 문제의 근본 수정 — 디자이너 실측 가드)."""
        state = {"cols": None}
        max_cols = max(wide_cols, narrow_cols)

        def _layout(cols: int) -> None:
            if state["cols"] == cols:
                return
            state["cols"] = cols
            for idx in range(max_cols):
                container.columnconfigure(
                    idx, weight=1 if idx < cols else 0,
                    uniform="channel_kpi" if idx < cols else "")
            for c in cards:
                c.grid_forget()
            for i, c in enumerate(cards):
                r, col = divmod(i, cols)
                c.grid(row=r, column=col,
                      padx=(SP["sm"] if col > 0 else 0, 0),
                      pady=(SP["sm"] if r > 0 else 0, 0), sticky="nsew")

        def _on_configure(event) -> None:
            w = event.width
            _layout(narrow_cols if w and w < threshold else wide_cols)

        container.bind("<Configure>", _on_configure)
        _layout(wide_cols)

    # ── 카카오 설정 섹션 ─────────────────────────────
    def _build_kakao_section(self):
        sec = SectionCard(self._sf, "카카오 알림 설정", "글 발행 시 카카오톡으로 자동 알림을 보내요.",
                          icon=icons.glyph("channel"))
        sec.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))

        # 활성화 토글
        tog_row = tk.Frame(sec.body, bg=C["card"])
        tog_row.pack(fill="x", pady=(0, 10))
        self._kk_enabled = tk.BooleanVar(value=False)
        tk.Checkbutton(tog_row, text="카카오 알림 활성화",
                       variable=self._kk_enabled,
                       font=FB, fg=C["text"], bg=C["card"],
                       activebackground=C["card"],
                       selectcolor=C["surface"],
                       relief="flat", cursor="hand2").pack(side="left")

        # 토큰 입력 — 평문 노출 방지를 위해 기본 마스킹 + 필요 시만 표시 토글.
        self._kk_token_var = tk.StringVar()
        self._kk_ch_token_var = tk.StringVar()
        FormField(sec.body, "액세스 토큰", self._kk_token_var, kind="password",
                  hint="나에게 보내기·채널 공통으로 쓰는 토큰이에요.",
                  show_toggle=True, width=36).pack(fill="x", pady=(4, 4))
        FormField(sec.body, "채널 토큰", self._kk_ch_token_var, kind="password",
                  hint="채널 브로드캐스트를 쓸 때만 필요해요.",
                  show_toggle=True, width=36).pack(fill="x", pady=(4, 4))

        # 발송 방식 체크박스
        mode_row = tk.Frame(sec.body, bg=C["card"]); mode_row.pack(fill="x", pady=(6, 6))
        self._kk_send_me  = tk.BooleanVar(value=True)
        self._kk_send_ch  = tk.BooleanVar(value=False)
        tk.Checkbutton(mode_row, text="나에게 보내기",
                       variable=self._kk_send_me, font=FB,
                       fg=C["text"], bg=C["card"],
                       activebackground=C["card"],
                       selectcolor=C["surface"], relief="flat").pack(side="left", padx=(0, 20))
        tk.Checkbutton(mode_row, text="채널 브로드캐스트",
                       variable=self._kk_send_ch, font=FB,
                       fg=C["text"], bg=C["card"],
                       activebackground=C["card"],
                       selectcolor=C["surface"], relief="flat").pack(side="left")

        # 메시지 템플릿
        L(sec.body, "발송 메시지 템플릿 (제목·요약·링크·태그 자동으로 채워져요):",
          FS, C["muted"]).pack(anchor="w", pady=(8, 2))
        self._kk_tpl_var = tk.StringVar(
            value="새 정보 발행!\n\n{title}\n\n{summary}\n\n{url}\n\n{tags}")
        tpl_frame = tk.Frame(sec.body, bg=C["card"])
        tpl_frame.pack(fill="x")
        self._kk_tpl_text = tk.Text(
            tpl_frame, height=4, font=FB, bg=C["surface"],
            fg=C["text"], insertbackground=C["accent"],
            relief="flat", bd=0, highlightthickness=1,
            highlightbackground=C["border"], wrap="word")
        self._kk_tpl_text.pack(fill="x", padx=0, pady=0)
        self._kk_tpl_text.insert("1.0", self._kk_tpl_var.get())

        # 버튼 행
        btn_row = tk.Frame(sec.body, bg=C["card"]); btn_row.pack(fill="x", pady=(10, 0))
        PrimaryBtn(btn_row, "저장", self._save_kakao).pack(side="left")
        SecondaryBtn(btn_row, "연결 테스트", self._test_kakao).pack(side="left", padx=8)
        SecondaryBtn(btn_row, "테스트 발송", self._test_send_kakao).pack(side="left")
        self._kk_status = StatusPill(btn_row, "", kind="neutral")
        self._kk_status.pack(side="right", padx=4)

        # 설정 가이드
        guide = tk.Frame(sec.body, bg=C["surface"]); guide.pack(fill="x", pady=(12, 0))
        guide_lines = [
            "카카오 토큰 발급 방법:",
            "  1. https://developers.kakao.com → 내 애플리케이션 → 앱 생성",
            "  2. 플랫폼 → Web 플랫폼 등록 → http://localhost",
            "  3. 제품 설정 → 카카오 로그인 활성화 → 동의항목: 카카오 말하기 체크",
            "  4. REST API 키 발급 → https://kauth.kakao.com/oauth/authorize?...",
            "     → code 파라미터로 토큰 발급 (1회 설정 후 자동 갱신)",
        ]
        for line in guide_lines:
            tk.Label(guide, text=line, font=FS, fg=C["muted"],
                     bg=C["surface"], anchor="w").pack(fill="x", padx=10, pady=1)

    # ── 카페 타겟 설정 섹션 ──────────────────────────
    def _build_cafe_settings_section(self):
        sec = SectionCard(self._sf, "카페 배포 설정", "수동 승인 후 자동 게시해요 — 스팸 방지용이에요.",
                          icon=icons.glyph("channel"))
        sec.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))

        # 활성화 + 자동승인
        tog = tk.Frame(sec.body, bg=C["card"]); tog.pack(fill="x", pady=(0, 10))
        self._cafe_enabled = tk.BooleanVar(value=False)
        self._cafe_auto    = tk.BooleanVar(value=False)
        tk.Checkbutton(tog, text="카페 배포 활성화",
                       variable=self._cafe_enabled, font=FB,
                       fg=C["text"], bg=C["card"],
                       activebackground=C["card"],
                       selectcolor=C["surface"], relief="flat").pack(side="left", padx=(0, 20))
        tk.Checkbutton(tog, text="자동 승인 (주의: 스팸 위험)",
                       variable=self._cafe_auto, font=FB,
                       fg=C["red"], bg=C["card"],
                       activebackground=C["card"],
                       selectcolor=C["surface"], relief="flat").pack(side="left")

        # 타겟 카페 목록 헤더
        col_hdr = tk.Frame(sec.body, bg=C["border"]); col_hdr.pack(fill="x", pady=(8, 0))
        for txt, w in [("카페명", 130), ("카페 URL", 220),
                        ("게시판 ID", 70), ("카테고리 (쉼표 구분)", 160), ("", 60)]:
            tk.Label(col_hdr, text=txt, font=FS, fg=C["muted"],
                     bg=C["border"], width=w//8, anchor="w",
                     padx=4).pack(side="left")

        # 타겟 카페 동적 행
        self._cafe_rows_frame = tk.Frame(sec.body, bg=C["card"])
        self._cafe_rows_frame.pack(fill="x")

        btn_row = tk.Frame(sec.body, bg=C["card"]); btn_row.pack(fill="x", pady=(8, 0))
        SecondaryBtn(btn_row, "카페 추가", self._add_cafe_row).pack(side="left")
        PrimaryBtn(btn_row, "카페 설정 저장", self._save_cafe_settings).pack(side="left", padx=10)
        self._cafe_status = StatusPill(btn_row, "", kind="neutral")
        self._cafe_status.pack(side="right", padx=4)

    @staticmethod
    def _default_cafe_category() -> str:
        """새 카페 행의 기본 카테고리 — 활성 프로필 첫 카테고리(default=examplebrand '복지')."""
        try:
            from config import brand as _brand
            cats = _brand.categories()
            if cats:
                return str(cats[0])
        except Exception:  # noqa: BLE001 — 브랜드 헬퍼 부재 시 레거시 기본값
            pass
        return "복지"

    def _add_cafe_row(self, data: dict = None):
        """카페 타겟 행 동적 추가 — URL 이 비어 있으면 즉시 빨간 힌트를 보여준다."""
        row = tk.Frame(self._cafe_rows_frame, bg=C["surface"]); row.pack(fill="x", pady=2)
        row_vars = {}
        url_var = None
        for key, default, w in [
            ("name",       data.get("name", "") if data else "",         14),
            ("cafe_url",   data.get("cafe_url", "") if data else "",     28),
            ("board_id",   data.get("board_id", "1") if data else "1",   6),
            ("categories", ",".join(data.get("categories", [])) if data
                           else self._default_cafe_category(),           18),
        ]:
            var = tk.StringVar(value=default)
            row_vars[key] = var
            Ent(row, var, w=w).pack(side="left", padx=2, ipady=3)
            if key == "cafe_url":
                url_var = var

        def _remove():
            row.destroy()
            self._cafe_targets_vars.remove(row_vars)
        GhostBtn(row, "", _remove, 3).pack(side="left", padx=2)
        self._cafe_targets_vars.append(row_vars)

        # 빈 URL 즉시 힌트 — 저장 시점까지 기다리지 않고 입력하는 동안 바로 알려준다.
        hint = tk.Label(self._cafe_rows_frame, text="카페 URL을 입력해 주세요",
                        font=FS, fg=C["red"], bg=C["surface"], anchor="w")

        def _sync_hint(*_args):
            if url_var is not None and not url_var.get().strip():
                hint.pack(fill="x", after=row, padx=(2, 0), pady=(0, 2))
            else:
                hint.pack_forget()

        if url_var is not None:
            url_var.trace_add("write", _sync_hint)
        _sync_hint()

    # ── 승인 큐 섹션 ─────────────────────────────────
    def _build_queue_section(self):
        sec = SectionCard(self._sf, "배포 승인 큐",
                          "새 글 발행 후 카페 배포 대기 목록이에요 — 내용을 확인한 뒤 승인·거부해요.",
                          icon=icons.glyph("channel"))
        sec.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))

        # 필터 탭
        flt = tk.Frame(sec.body, bg=C["card"]); flt.pack(fill="x", pady=(0, 8))
        self._queue_filter = tk.StringVar(value="pending")
        for val, lbl, clr in [
            ("pending",  "대기 중",   C["orange"]),
            ("approved", "승인됨",    C["green"]),
            ("sent",     "발송 완료", C["accent"]),
            ("rejected", "거부됨",    C["muted"]),
            ("all",      "전체",      C["text"]),
        ]:
            tk.Radiobutton(flt, text=lbl, variable=self._queue_filter,
                           value=val, font=FS, fg=clr, bg=C["card"],
                           activebackground=C["card"], selectcolor=C["surface"],
                           relief="flat",
                           command=self._refresh_queue).pack(side="left", padx=(0, 8))

        # 큐 표
        q_cols = ("글 제목", "카페", "상태", "생성일시")
        self.q_tree = DataTable(sec.body, columns=q_cols, show="headings",
                                 height=7)
        q_widths = [280, 140, 80, 130]
        for col, w in zip(q_cols, q_widths):
            self.q_tree.heading(col, text=col)
            self.q_tree.column(col, width=_px(w), anchor="w")
        q_vsb = ttk.Scrollbar(sec.body, orient="vertical", command=self.q_tree.yview)
        self.q_tree.configure(yscrollcommand=q_vsb.set)
        q_vsb.pack(side="right", fill="y")
        self.q_tree.pack(fill="x")
        self.q_tree.bind("<<TreeviewSelect>>", self._on_queue_select)

        # 초안 미리보기 + 편집
        L(sec.body, "초안 미리보기 (편집 후 승인 가능):", FS, C["muted"]).pack(anchor="w", pady=(10, 2))
        self.draft_text = tk.Text(
            sec.body, height=6, font=FB, bg=C["surface"], fg=C["text"],
            insertbackground=C["accent"], relief="flat", bd=0,
            highlightthickness=1, highlightbackground=C["border"], wrap="word")
        self.draft_text.pack(fill="x")

        # 액션 버튼
        act = tk.Frame(sec.body, bg=C["card"]); act.pack(fill="x", pady=(10, 0))
        PrimaryBtn(act, "승인 & 즉시 게시", self._approve_selected).pack(side="left")
        SecondaryBtn(act, "승인 (나중에 게시)", self._approve_only).pack(side="left", padx=6)
        DangerBtn(act, "거부", self._reject_selected, icon="close").pack(side="left")
        GhostBtn(act, "새로고침", self._refresh_queue, 10).pack(side="left", padx=8)
        self._q_status = L(act, "", FS, C["muted"])
        self._q_status.pack(side="right", padx=4)

    # ── 발송 기록 섹션 ──────────────────────────────
    def _build_log_section(self):
        sec = SectionCard(self._sf, "최근 발송 기록", icon=icons.glyph("log"))
        sec.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["xl"]))

        l_cols = ("채널", "글 제목", "결과", "일시")
        self.log_tree = DataTable(sec.body, columns=l_cols, show="headings",
                                   height=5)
        l_widths = [90, 300, 60, 150]
        for col, w in zip(l_cols, l_widths):
            self.log_tree.heading(col, text=col)
            self.log_tree.column(col, width=_px(w), anchor="w")
        l_vsb = ttk.Scrollbar(sec.body, orient="vertical", command=self.log_tree.yview)
        self.log_tree.configure(yscrollcommand=l_vsb.set)
        l_vsb.pack(side="right", fill="y")
        self.log_tree.pack(fill="x")

        GhostBtn(sec.body, "기록 새로고침", self._load_log_display, 14).pack(anchor="w", pady=(8, 0))

    def _wire_table_tag_colors(self) -> None:
        """상태별 tag 전경색을 지금 칠하고, 팔레트 전환마다 다시 칠하도록
        `DataTable._on_retheme` 훅에 걸어 둔다.

        `DataTable.tag_configure()` 가 저장하는 색은 호출 시점 리터럴
        hex(``gui/data_table.py`` — 이 앱 공용 모듈이라 여기서 손대지
        않는다)라, 한 번만 칠하면 라이트 전환 후에도 다크 시절 색이
        굳어 대비가 죽는다(예: 다크 orange 는 라이트 카드 배경에서도
        보이지만, 이 훅이 없으면 팔레트가 바뀌어도 값 자체는 안 바뀐다
        — 재현·수정 확인은 test_gui_channel_trend_tables.py 참고)."""
        def _paint() -> None:
            self.q_tree.tag_configure("pending",  foreground=C["orange"])
            self.q_tree.tag_configure("sent",     foreground=C["green"])
            self.q_tree.tag_configure("rejected", foreground=C["muted"])
            self.q_tree.tag_configure("failed",   foreground=C["red"])
            self.log_tree.tag_configure("ok",   foreground=C["green"])
            self.log_tree.tag_configure("fail", foreground=C["red"])

        for table in (self.q_tree, self.log_tree):
            base_on_retheme = table._on_retheme

            def _hook(base=base_on_retheme) -> None:
                _paint()
                base()

            table._on_retheme = _hook
        _paint()

    # ─────────────────────────────────────────────────
    # 데이터 로드
    # ─────────────────────────────────────────────────
    def _load_all(self):
        try:
            from core.channel_dispatcher import _load_channel_cfg, get_channel_stats
            cfg   = _load_channel_cfg()
            kk    = cfg.get("kakao", {})
            cafe  = cfg.get("cafe", {})

            # 카카오 설정 반영
            self._kk_enabled.set(kk.get("enabled", False))
            self._kk_token_var.set(kk.get("access_token", ""))
            self._kk_ch_token_var.set(kk.get("channel_token", ""))
            self._kk_send_me.set(kk.get("send_to_me", True))
            self._kk_send_ch.set(kk.get("send_channel", False))
            tpl = kk.get("template", "")
            if tpl:
                self._kk_tpl_text.delete("1.0", "end")
                self._kk_tpl_text.insert("1.0", tpl)

            # 카페 설정 반영
            self._cafe_enabled.set(cafe.get("enabled", False))
            self._cafe_auto.set(cafe.get("auto_approve", False))
            self._cafe_targets_vars.clear()
            for w in self._cafe_rows_frame.winfo_children():
                w.destroy()
            for target in cafe.get("targets", []):
                self._add_cafe_row(target)

            # 통계 — .configure() 사용(HAS_CTK 면 값 라벨이 raw CTkLabel 이라
            # .config() 별칭이 없다: "'config' is not implemented for CTk
            # widgets" 로 즉시 예외 — 실측 확인).
            stats = get_channel_stats()
            for key, lbl in self._stat_labels.items():
                lbl.configure(text=str(stats.get(key, "—")))

        except (ImportError, json.JSONDecodeError, FileNotFoundError, KeyError) as e:
            log.debug("채널 설정 로드 실패", exc_info=True)
            self._kk_status.set("설정을 불러오지 못했어요", "error")
            self.app.set_status(f"채널 설정 로드 실패: {e}", C["red"])

        self._refresh_queue()
        self._load_log_display()

    def _refresh_queue(self):
        try:
            from core.channel_dispatcher import _load_queue
            flt   = self._queue_filter.get()
            queue = _load_queue()
            items = queue if flt == "all" else [q for q in queue if q.get("status") == flt]
            self.q_tree.delete(*self.q_tree.get_children())
            for item in reversed(items[-100:]):
                st  = item.get("status", "")
                tag = st if st in ("pending", "sent", "rejected", "failed") else ""
                self.q_tree.insert("", "end", iid=item["id"],
                    values=(
                        item.get("post_title", "")[:40],
                        item.get("cafe_name", ""),
                        {"pending": "대기", "approved": "승인", "sent": "완료",
                         "rejected": "거부", "failed": "실패"}.get(st, st),
                        item.get("created_at", "")[:16],
                    ), tags=(tag,))
            cnt = sum(1 for q in queue if q.get("status") == "pending")
            self._q_status.config(
                text=f"  승인 대기: {cnt}건",
                fg=C["orange"] if cnt > 0 else C["muted"])
        except Exception as e:
            log.debug("채널 탭 오류", exc_info=True)
            self._q_status.config(text=f"  {e}", fg=C["red"])

    def _on_queue_select(self, event):
        sel = self.q_tree.selection()
        if not sel:
            return
        iid = sel[0]
        try:
            from core.channel_dispatcher import _load_queue
            queue = _load_queue()
            item  = next((q for q in queue if q["id"] == iid), None)
            if item:
                self.draft_text.delete("1.0", "end")
                self.draft_text.insert("1.0",
                    f"{item.get('post_title','')}\n"
                    f"{item.get('cafe_name','')} | 게시판 {item.get('board_id','')}\n"
                    f"{item.get('post_url','')}\n\n"
                    f"{item.get('draft','')}")
        except (ImportError, json.JSONDecodeError, FileNotFoundError, KeyError, StopIteration):
            log.debug("큐 항목 미리보기 로드 실패", exc_info=True)
            self._q_status.config(text="  미리보기 로드 실패", fg=C["red"])

    def _load_log_display(self):
        try:
            from core.channel_dispatcher import _load_log
            logs = list(reversed(_load_log()[-50:]))
            self.log_tree.delete(*self.log_tree.get_children())
            for log_item in logs:
                ch   = log_item.get("channel", "")
                ok   = log_item.get("success", True)
                tag  = "ok" if ok else "fail"
                ch_label = {"kakao": "카카오", "naver_cafe": "카페"}.get(ch, ch)
                self.log_tree.insert("", "end",
                    values=(
                        ch_label,
                        log_item.get("post_title", log_item.get("title", ""))[:42],
                        "" if ok else "",
                        log_item.get("logged_at", "")[:16],
                    ), tags=(tag,))
        except (ImportError, json.JSONDecodeError, FileNotFoundError, KeyError) as e:
            log.debug("발송 기록 로드 실패", exc_info=True)
            self.app.set_status(f"발송 기록 로드 실패: {e}", C["red"])

    # ─────────────────────────────────────────────────
    # 카카오 액션
    # ─────────────────────────────────────────────────
    def _save_kakao(self):
        try:
            from core.channel_dispatcher import update_kakao_settings
            update_kakao_settings(
                enabled       = self._kk_enabled.get(),
                access_token  = self._kk_token_var.get(),
                channel_token = self._kk_ch_token_var.get(),
                send_to_me    = self._kk_send_me.get(),
                send_channel  = self._kk_send_ch.get(),
                template      = self._kk_tpl_text.get("1.0", "end").strip(),
            )
            self._kk_status.set("저장됨", "ok")
            self._notify("카카오 설정을 저장했어요", "ok")
            self.app.set_status("카카오 설정 저장", C["green"])
        except Exception as e:
            log.debug("카카오 설정 오류", exc_info=True)
            self._kk_status.set("저장 실패", "error")
            self._notify(f"카카오 설정 저장에 실패했어요: {e}", "error")

    def _test_kakao(self):
        def _task():
            try:
                from core.channel_dispatcher import test_kakao_connection
                token  = self._kk_token_var.get().strip()
                result = test_kakao_connection(token)
                if result.get("ok"):
                    text = f"연결됨 · {result.get('nickname','')}"
                    kind = "ok"
                else:
                    text = f"실패 · {result.get('error','')}"
                    kind = "error"
                self.ui_after(lambda: self._kk_status.set(text, kind))
                self.ui_after(lambda: self._notify(
                    "카카오 연결에 성공했어요" if kind == "ok"
                    else f"카카오 연결 실패: {result.get('error','')}",
                    kind))
                self.app.set_status(text, C["green"] if kind == "ok" else C["red"])
            except Exception as e:  # noqa: BLE001 — 네트워크/파싱 등 모든 실패를 화면에 표기
                log.debug("카카오 연결 테스트 오류", exc_info=True)
                self.ui_after(lambda: self._kk_status.set("연결 테스트 오류", "error"))
                self.app.set_status(f"카카오 연결 테스트 오류: {e}", C["red"])
        self.spawn(_task, name="channel-test-kakao")

    def _test_send_kakao(self):
        def _task():
            try:
                from core.channel_dispatcher import _kakao_send_to_me
                token = self._kk_token_var.get().strip()
                if not token:
                    self.ui_after(lambda: self._notify("먼저 액세스 토큰을 입력해 주세요", "warn"))
                    return
                ok = _kakao_send_to_me(
                    token,
                    "블로그 자동화 테스트",
                    "테스트 메시지입니다!\n이 메시지가 도착하면 카카오 알림 설정이 완료된 것입니다.",
                    "https://blog.naver.com"
                )
                self.ui_after(lambda: self._notify(
                    "테스트 메시지를 보냈어요" if ok else "발송에 실패했어요",
                    "ok" if ok else "error"))
            except Exception as e:
                log.debug("카카오 테스트 오류", exc_info=True)
                self.ui_after(lambda: self._notify(f"발송 오류: {e}", "error"))
        self.spawn(_task, name="channel-test-send-kakao")

    # ─────────────────────────────────────────────────
    # 카페 설정 액션
    # ─────────────────────────────────────────────────
    def _save_cafe_settings(self):
        try:
            from core.channel_dispatcher import update_cafe_settings
            targets = []
            import uuid as _uuid
            empty_urls = 0
            for row_vars in self._cafe_targets_vars:
                name  = row_vars["name"].get().strip()
                url   = row_vars["cafe_url"].get().strip()
                brd   = row_vars["board_id"].get().strip()
                cats  = [c.strip() for c in row_vars["categories"].get().split(",") if c.strip()]
                if name and not url:
                    empty_urls += 1
                if name and url:
                    targets.append({
                        "id":         re.sub(r"[^a-z0-9]", "", name.lower())[:12]
                                      or _uuid.uuid4().hex[:8],
                        "name":       name,
                        "cafe_url":   url,
                        "board_id":   brd or "1",
                        "categories": cats or ["default"],
                        "intro":      "",
                    })
            update_cafe_settings(
                enabled      = self._cafe_enabled.get(),
                targets      = targets,
                auto_approve = self._cafe_auto.get(),
            )
            if empty_urls:
                self._cafe_status.set(f"저장됨 · URL 없는 {empty_urls}개 제외", "warn")
                self._notify(f"카페 {len(targets)}개를 저장했어요 (URL 없는 {empty_urls}개는 제외됨)", "warn")
            else:
                self._cafe_status.set(f"저장됨 · {len(targets)}개", "ok")
                self._notify(f"카페 {len(targets)}개를 저장했어요", "ok")
            self.app.set_status(f"카페 설정 저장 ({len(targets)}개)", C["green"])
        except Exception as e:
            log.debug("카페 설정 오류", exc_info=True)
            self._cafe_status.set("저장 실패", "error")
            self._notify(f"카페 설정 저장에 실패했어요: {e}", "error")

    # ─────────────────────────────────────────────────
    # 큐 액션
    # ─────────────────────────────────────────────────
    def _get_selected_id(self) -> Optional[str]:
        sel = self.q_tree.selection()
        return sel[0] if sel else None

    def _approve_selected(self):
        iid = self._get_selected_id()
        if not iid:
            self._notify("승인할 항목을 표에서 선택해 주세요", "warn")
            return

        # ── 즉시 게시 전 확인(파괴적·되돌리기 어려움 — messagebox 유지) ──
        post_title, cafe_name = "", ""
        try:
            from core.channel_dispatcher import _load_queue
            item = next((q for q in _load_queue() if q["id"] == iid), None)
            if item:
                post_title = item.get("post_title", "")
                cafe_name  = item.get("cafe_name", "")
        except (ImportError, json.JSONDecodeError, FileNotFoundError, KeyError, StopIteration):
            log.debug("승인 확인용 큐 로드 실패", exc_info=True)
        if not messagebox.askyesno(
            "카페 게시 확인",
            f"선택한 글을 네이버 카페에 즉시 게시합니다.\n\n"
            f"제목: {post_title or '(미상)'}\n"
            f"카페: {cafe_name or '(미상)'}\n\n"
            f"게시 후에는 카페에서 수동으로 삭제해야 합니다. 계속할까요?",
        ):
            return

        edited = self.draft_text.get("1.0", "end").strip()
        # 편집된 내용에서 초안 부분만 추출 (첫 3줄 헤더 제거)
        lines = edited.split("\n")
        draft = "\n".join(lines[4:]).strip() if len(lines) > 4 else edited

        def _task():
            try:
                from core.channel_dispatcher import approve_and_post
                self.app.set_status("카페 게시 중...", C["yellow"])
                # 자격증명은 GUI 클로저로 붙잡지 않는다 — approve_and_post 가
                # 활성 프로필에서 자체 복호화한다(평문 PW 유출 경로 제거).
                result = approve_and_post(iid, draft)
                if result.get("success"):
                    self.ui_after(lambda: self._notify("카페 게시를 완료했어요", "ok"))
                    self.app.set_status("카페 등록 완료", C["green"])
                else:
                    err = result.get("error", "")
                    self.ui_after(lambda: self._notify(f"카페 게시 실패: {err[:60]}", "error"))
                    self.app.set_status(f"실패: {err[:40]}", C["red"])
                self.ui_after(self._refresh_queue, delay=200)
                self.ui_after(self._load_log_display, delay=200)
                self.ui_after(self._load_all, delay=200)
            except Exception as e:
                log.debug("카페 게시 오류", exc_info=True)
                self.app.set_status(f"{e}", C["red"])
        self.spawn(_task, name="channel-approve-post")

    def _approve_only(self):
        """게시 없이 승인 상태만 변경 (나중에 수동 게시)"""
        iid = self._get_selected_id()
        if not iid:
            self._notify("승인할 항목을 표에서 선택해 주세요", "warn")
            return
        try:
            from core.channel_dispatcher import update_queue_item
            update_queue_item(iid, {
                "status":      "approved",
                "approved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._q_status.config(text="  승인됨 (미발송)", fg=C["green"])
            self._notify("승인했어요 (아직 게시되지 않았어요)", "ok")
            self._refresh_queue()
        except Exception as e:
            log.debug("채널 탭 오류", exc_info=True)
            self._q_status.config(text=f"  {e}", fg=C["red"])

    def _reject_selected(self):
        iid = self._get_selected_id()
        if not iid:
            self._notify("거부할 항목을 표에서 선택해 주세요", "warn")
            return
        try:
            from core.channel_dispatcher import reject_queue_item
            reject_queue_item(iid, "사용자 거부")
            self._refresh_queue()
            self._q_status.config(text="  거부됨", fg=C["muted"])
        except Exception as e:
            log.debug("채널 탭 오류", exc_info=True)
            self._q_status.config(text=f"  {e}", fg=C["red"])
