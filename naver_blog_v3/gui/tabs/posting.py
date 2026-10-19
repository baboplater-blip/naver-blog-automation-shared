# gui/tabs/posting.py — 발행·진행 탭 (GUI 개편 2026-09-02)
"""오늘 실행/테스트/보관함 채우기의 실행 제어 + 진행 상황 + 실시간 로그 +
미리보기. `App.bus` 를 구독해 진행 상황을 받는다(설계서 §3.3 — 워커는
`app.bus.*` 만 호출하고 이 탭을 직접 만지지 않는다)."""
import tkinter as tk
from tkinter import ttk, scrolledtext, filedialog, messagebox
from datetime import datetime
from pathlib import Path
from typing import Optional
import os, re, webbrowser, logging, threading, json
from html import escape as html_escape

log = logging.getLogger(__name__)

from gui import theme as _theme_module
from gui.theme import (C, FT, FB, FS, FM, SP, Sep, GhostBtn, SecondaryBtn,
                        DangerBtn, PrimaryBtn, strip_emoji)
from gui.async_utils import UiLifecycle
from gui.data_table import DataTable
from gui.widgets import (EmptyState, Header, IconButton, ProgressRow,
                          ScrollArea, SectionCard)
from gui import icons

# 고DPI(9호 Phase 1) — app.py 와 동일한 관례(getattr 폴백, 이유는 그쪽
# 주석 참조). SP/R 토큰 자체에는 절대 씌우지 않는다(코더 A 의
# `set_ui_scale()` 이 완성되면 토큰이 알아서 커진다 — 이중 스케일 방지).
_px = getattr(_theme_module, "px", lambda n: n)


class PostingTab(UiLifecycle, tk.Frame):
    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self.app = app
        self._init_lifecycle()
        self._build()
        subscribe = getattr(getattr(app, "bus", None), "subscribe", None)
        if callable(subscribe):
            subscribe(self)

    def _build(self):
        # v4(2026-09-21) 헤더 재편 — 예전엔 액션 5개(오늘 발행·편수 스핀박스·
        # 채우기·테스트·중지)를 헤더 한 줄에 몰아 넣어 900×620 에서 "테스트"·
        # "중지" 가 통째로 잘렸고 1080×720 에서도 "중지" 가 반 잘렸다(개편 전
        # 캡처로 실증). 이제 헤더에는 **실제로 발행하는 주 버튼 1개 + 중지**만
        # 둔다. 위험하지 않은 준비 동작(보관함 채우기·테스트 실행)은 아래
        # "준비 동작" 줄로 내려 발행 버튼과 시각적으로 분리했다.
        hdr = Header(self, "발행·진행", "실행 상황을 지켜보고 중간에 멈출 수 있어요",
                     icon="posting")
        hdr.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))
        actions = hdr.actions
        self.run_btn = PrimaryBtn(actions, "오늘 발행", self.app.run_today, icon="publish")
        self.run_btn.pack(side="left", padx=(0, SP["sm"]))
        self.stop_btn = DangerBtn(actions, "중지", self.app.stop_run, icon="stop")
        self.stop_btn.pack(side="left")
        self.stop_btn.config(state="disabled")
        self._style_stop_btn(running=False)

        # ── 준비 동작(발행하지 않음) ─────────────────────────────────
        prep = tk.Frame(self, bg=C["bg"])
        prep.pack(fill="x", padx=SP["xl"], pady=(SP["sm"], SP["sm"]))
        tk.Label(prep, text="준비 동작 · 발행하지 않아요", font=FS, fg=C["muted"],
                 bg=C["bg"]).pack(side="left", padx=(0, SP["md"]))
        self.fill_n_var = tk.IntVar(value=10)
        tk.Spinbox(prep, from_=1, to=50, textvariable=self.fill_n_var,
                   width=3, font=FB, fg=C["text"], bg=C["surface_2"],
                   relief="flat", bd=0, highlightthickness=_px(1),
                   highlightbackground=C["border_strong"],
                   highlightcolor=C["focus"]).pack(side="left", padx=(0, SP["xs"]))
        SecondaryBtn(prep, "보관함 채우기", self._on_fill_drafts, icon="save").pack(
            side="left", padx=(0, SP["sm"]))
        SecondaryBtn(prep, "테스트 실행", self.app.run_test, icon="test").pack(side="left")
        SecondaryBtn(prep, "저장 글 보기",
                     lambda: self.app.show_tab("archive"), icon="folder").pack(
            side="right")

        Sep(self).pack(fill="x", padx=SP["xl"])

        # 진행 카드(+인증 배너)는 표 행 수·인증 배너 유무에 따라 높이가
        # 크게 요동친다(실측 유휴 282px → 실행중 표 8행 346px → 인증배너까지
        # 뜨면 475px). 이 요동이 그대로 전파되면 아래 로그·미리보기 Paned 가
        # 받는 "남는 공간"이 들쭉날쭉해져, 900×620 최소 창의 실행 중(표+진행행)
        # 케이스에서 Paned 자체 minsize 합(80+120=200, 2026-09-03 캡처 심사
        # 🟡4 이후 로그 minsize 120 으로 상향돼 현재는 120+120=240)보다 남는 공간(576-424=
        # 152)이 작아져 창 밖으로 밀렸다(2026-09-02 검증조 실측 🔴2). 처방:
        # 이 영역을 **고정 높이 260px 상자**(ScrollArea 안전장치 내장)에
        # 가둬 Paned 가 항상 예측 가능한 나머지 공간을 받게 한다 — 상자
        # 안에서 못 다 보이는 부분(예: 인증배너+8행 표)만 그 상자 안에서
        # 스크롤한다(잘림 없음). 실측(2026-09-02, 헤드리스 App() 로 실제
        # winfo_height 측정) — 900×620 최소 창: 고정부(헤더+상자+구분선×2)
        # 338px, Paned 실할당 198px(자체 minsize 선언 80+120=200 과 거의
        # 일치), **유휴/실행중/인증배너 상태와 무관하게 항상 동일**(예전엔
        # 상태에 따라 -757px 까지 마이너스로 치솟아 창 밖으로 밀렸다 —
        # 지금은 상자가 흡수해 Paned 몫이 흔들리지 않는다). 1080×720에선
        # Paned 가 더 여유 있게 받는다(고정부는 동일 338, 남는 공간이 큼).
        # (2026-09-03 재검증) 로그 minsize 를 80→120 으로 올린 뒤 900×620
        # 최소 창에서 재측정: Paned 실할당 222px, 로그 120px(선언 그대로)·
        # 미리보기 94px(선언 120 보다 작음). Tk PanedWindow 는 minsize 를
        # 하드 하한이 아니라 사용자가 새시를 그 이상 못 줄이게 하는 힌트로만
        # 쓴다 — 전체 공간이 minsize 합보다 작으면 음수·크래시 없이 비례
        # 축소될 뿐이라 여전히 안전하다(핵심 불변식 "0보다 크고 상태 무관
        # 항상 동일"은 유지 — 아래 회귀 가드 테스트로 확인).
        # (v4, 2026-09-21) 260 → 216 — 헤더 아래 "준비 동작" 줄이 새로 생긴
        # 만큼을 여기서 돌려줘 로그·미리보기 Paned 몫이 줄지 않게 한다.
        # 상자 안 내용이 넘치면 ScrollArea 가 그대로 흡수한다(잘림 없음).
        self._region_height = _px(216)
        region = tk.Frame(self, bg=C["bg"], height=self._region_height)
        region.pack(fill="x")
        region.pack_propagate(False)
        scroll = ScrollArea(region, bg=C["bg"])
        scroll.pack(fill="both", expand=True)
        body = scroll.inner

        # ── 진행 상황 ──
        # 표(Treeview)와 빈 상태가 동시에 보이면 카드가 너무 커지므로 상호
        # 배타(한쪽만 pack)로 바꿔 카드 높이를 유휴 시엔 작게, 실행 중엔
        # 표만 자라게 한다.
        prog_card = SectionCard(body, "진행 상황")
        prog_card.pack(fill="x", padx=SP["xl"], pady=SP["md"])

        # 상태 줄 전용 안정 컨테이너 — idle_lbl/progress_row 를 이 안에서만
        # 토글해야 pack 순서가 항상 카드 맨 위로 고정된다(바깥에서 번갈아
        # pack/pack_forget 하면 나중에 다시 pack 된 쪽이 표 뒤로 밀려날 수
        # 있음 — Tk pack 은 새로 추가된 위젯을 맨 뒤에 놓는다).
        status_row = tk.Frame(prog_card.body, bg=C["card"])
        status_row.pack(fill="x")
        # 진행행은 실행 중에만 보이고, 유휴 시엔 한 줄 "대기 중" 라벨로
        # 대체한다 — 빈 진행바가 "작은 검은 상자"로 보이던 문제 처방.
        self._idle_lbl = tk.Label(
            status_row, text="대기 중 · 위 '오늘 발행'을 누르면 시작해요",
            font=FS, fg=C["muted"], bg=C["card"], anchor="w")
        self._idle_lbl.pack(fill="x")
        self._progress_row = ProgressRow(status_row)
        # 실행 중일 때만 pack — on_run_state 가 idle_lbl 과 번갈아 토글한다.

        # 표/빈 상태도 같은 이유로 안정 컨테이너 안에서만 토글한다.
        list_area = tk.Frame(prog_card.body, bg=C["card"])
        list_area.pack(fill="x", pady=(SP["sm"], 0))
        self.post_list = DataTable(
            list_area, columns=("status", "title", "time"), height=8)
        self.post_list.heading("status", text="상태")
        self.post_list.heading("title",  text="제목")
        self.post_list.heading("time",   text="시각")
        self.post_list.column("status", width=_px(70),  anchor="center", stretch=False)
        self.post_list.column("title",  width=_px(420), anchor="w")
        self.post_list.column("time",   width=_px(80),  anchor="center", stretch=False)
        self.post_list.tag_configure("waiting", foreground=C["muted"])
        self.post_list.tag_configure("running", foreground=C["accent"])
        self.post_list.tag_configure("ok",      foreground=C["green"])
        self.post_list.tag_configure("fail",    foreground=C["red"])
        self.post_list.tag_configure("skip",    foreground=C["yellow"])
        # 표는 초기 상태(0행)에선 pack 하지 않는다 — _update_post_list_visibility
        # 가 표/빈 상태 중 하나만(list_area 안에서) 보이게 한다.
        self._post_list_empty = EmptyState(
            list_area, "folder", "실행하면 글 목록이 여기 나와요",
            hint="위 '오늘 발행' 또는 '테스트'를 눌러 시작하세요.")
        # title -> iid 매핑 (선등록 + 동적 추가 모두 지원)
        self._post_list_iids: dict = {}
        # 행 iid 순번(단조 증가·충돌 0) — 매핑을 비워도 리셋해 재사용 iid 방지
        self._post_row_seq: int = 0
        self._update_post_list_visibility()

        # ── 보안 인증 대기 배너 (초기 숨김, warn 톤) ──
        # 인증은 사용자가 즉시 처리해야 하므로 스크롤 영역 밖에 둔다.
        self._auth_before = region
        self.auth_frame = tk.Frame(self, bg=C["warn_bg"])
        tk.Frame(self.auth_frame, width=SP["xs"], bg=C["yellow"]).pack(side="left", fill="y")
        auth_body = tk.Frame(self.auth_frame, bg=C["warn_bg"])
        auth_body.pack(side="left", fill="both", expand=True,
                       padx=SP["sm"], pady=SP["sm"])
        tk.Label(auth_body, text="로그인·인증 대기 중", font=FT,
                 fg=C["yellow"], bg=C["warn_bg"], anchor="w").pack(anchor="w")
        tk.Label(auth_body,
                 text="발행용 크롬에서 로그인·보안 인증을 마친 뒤 인증 완료를 눌러 주세요.",
                 font=FS, fg=C["text_2"], bg=C["warn_bg"], anchor="w",
                 justify="left", wraplength=_px(560)).pack(anchor="w", pady=(SP["xs"], SP["sm"]))
        self.auth_btn = SecondaryBtn(auth_body, "인증 완료", self._on_auth_complete,
                                     icon="success")
        self.auth_btn.pack(anchor="w")
        # 프레임은 생성만 하고 pack하지 않음 (숨김 상태)

        Sep(self).pack(fill="x", padx=SP["xl"])

        # ── 로그 + 미리보기: PanedWindow (드래그로 크기 조절) ──
        # `region`(고정 260px 상자) 의 형제로 self 에 직접 붙인다 — 그래야
        # `region` 이 얼마나 자랐든(표 8행+인증배너 등) Paned 는 항상
        # "남은 전체 공간"을 expand=True 로 안정적으로 받는다.
        pw = tk.PanedWindow(self, orient="vertical", bg=C["border"],
                            sashwidth=_px(6), sashrelief="flat")
        pw.pack(fill="both", expand=True, padx=SP["lg"], pady=SP["sm"])

        # ── 상단 패널: 실시간 로그 ──
        log_panel = tk.Frame(pw, bg=C["bg"])
        log_hdr = tk.Frame(log_panel, bg=C["bg"])
        log_hdr.pack(fill="x", padx=SP["sm"], pady=(SP["xs"], SP["xs"]))
        tk.Label(log_hdr, text="실시간 로그", font=FT, fg=C["text"],
                 bg=C["bg"]).pack(side="left")
        GhostBtn(log_hdr, "지우기", self._clear, 8).pack(side="right", padx=_px(3))
        GhostBtn(log_hdr, "저장",   self._save,  8).pack(side="right", padx=_px(3))
        self.log_box = scrolledtext.ScrolledText(
            log_panel, font=FM, bg=C["surface"], fg=C["text"],
            insertbackground=C["accent"], relief="flat", bd=0,
            highlightthickness=_px(1), highlightbackground=C["border"],
            state="disabled", wrap="word", height=12)
        self.log_box.pack(fill="both", expand=True, padx=SP["sm"], pady=(0, SP["xs"]))
        for tag, color in [("ok",C["green"]),("err",C["red"]),
                            ("info",C["accent"]),("warn",C["yellow"]),
                            ("muted",C["muted"]),("dup",C["orange"])]:
            self.log_box.tag_config(tag, foreground=color)
        pw.add(log_panel, minsize=_px(120))

        # ── 하단 패널: 미리보기 ──
        pv_panel = tk.Frame(pw, bg=C["bg"])
        # v4(2026-09-21): 예전엔 제목·콤보·버튼 6개를 한 줄에 몰아 넣어
        # 900×620 에서 '열기' 뒤 버튼들이 전부 화면 밖으로 밀렸다(개편 전
        # 캡처 실증). 제목·액션 줄과 선택 줄을 2행으로 나눈다.
        pv_hdr = tk.Frame(pv_panel, bg=C["bg"])
        pv_hdr.pack(fill="x", padx=SP["sm"], pady=(SP["xs"], SP["xs"]))
        tk.Label(pv_hdr, text="생성된 글 미리보기", font=FT, fg=C["text"],
                 bg=C["bg"]).pack(side="left")
        SecondaryBtn(pv_hdr, "영상 만들기", self._run_multichannel_build).pack(
            side="right", padx=(SP["xs"], 0))
        SecondaryBtn(pv_hdr, "열기", self._open_preview).pack(side="right", padx=_px(2))
        IconButton(pv_hdr, "folder", self._open_preview_folder,
                   tooltip="폴더 열기").pack(side="right", padx=_px(2))
        IconButton(pv_hdr, "refresh", self._refresh_previews,
                   tooltip="새로고침").pack(side="right", padx=_px(2))

        pv_pick = tk.Frame(pv_panel, bg=C["bg"])
        pv_pick.pack(fill="x", padx=SP["sm"], pady=(0, SP["xs"]))
        GhostBtn(pv_pick, "전체삭제", self._delete_all_previews, 8).pack(
            side="right", padx=(_px(2), 0))
        GhostBtn(pv_pick, "삭제", self._delete_preview, 6).pack(side="right", padx=_px(2))
        self.preview_combo = ttk.Combobox(pv_pick, state="readonly", font=FS)
        self.preview_combo.pack(side="left", fill="x", expand=True,
                                padx=(0, SP["sm"]))
        self.preview_text = scrolledtext.ScrolledText(
            pv_panel, font=FM, bg=C["surface"], fg=C["text"],
            relief="flat", bd=0, highlightthickness=_px(1),
            highlightbackground=C["border"], state="disabled",
            wrap="word", height=12)
        self.preview_text.pack(fill="both", expand=True, padx=SP["sm"], pady=(0, SP["xs"]))
        self.preview_combo.bind("<<ComboboxSelected>>", self._on_preview_select)
        # 미지원 포맷 폴백 버튼('브라우저로 열기') 프레임 — 필요 시 동적 생성.
        self._preview_fallback_frame = None

        # ── 발행 기록 관리 행 ──
        hist_row = tk.Frame(pv_panel, bg=C["bg"])
        hist_row.pack(fill="x", padx=SP["sm"], pady=(0, SP["xs"]))
        DangerBtn(hist_row, "발행기록 초기화", self._clear_post_history).pack(side="left")
        self.history_count_lbl = tk.Label(hist_row, text="", font=FS,
                                          fg=C["muted"], bg=C["bg"])
        self.history_count_lbl.pack(side="left", padx=(SP["sm"], 0))

        pw.add(pv_panel, minsize=_px(120))

        self._refresh_previews()
        self._update_history_count()

        # 초기 비율: 로그 30%, 미리보기 70%
        def _set_sash():
            h = pw.winfo_height()
            if h > 10:
                pw.sash_place(0, 0, int(h * 0.3))
            else:
                self.ui_after(_set_sash, delay=200)
        self.ui_after(_set_sash, delay=100)

    def _on_fill_drafts(self):
        """편수 스핀박스 값으로 보관함 채우기 — askyesno 1회만(simpledialog 스킵)."""
        try:
            n = int(self.fill_n_var.get())
        except (tk.TclError, ValueError):
            n = 10
        self.app.run_fill_drafts(n=n)

    def _toast(self, text: str, kind: str = "info") -> None:
        """정보성 알림 — 토스트 우선, 없으면 messagebox 폴백(설계서 §1.2)."""
        toast = getattr(self.app, "toast", None)
        if toast is not None:
            try:
                toast.show(text, kind=kind)
                return
            except Exception:  # noqa: BLE001 — 토스트 실패가 안내 자체를 막으면 안 됨
                pass
        if kind == "error":
            messagebox.showerror("알림", text)
        elif kind == "warn":
            messagebox.showwarning("알림", text)
        else:
            messagebox.showinfo("알림", text)

    # ── RunBus 구독(설계서 §1.4 — 워커는 app.bus.* 만 호출) ──
    def on_log(self, msg: str, tag: str = "") -> None:
        self.log(msg, tag)

    def on_progress(self, cur: int, total: int, text: str = "") -> None:
        self.set_progress(cur, total, text)

    def on_auth(self, state: str) -> None:
        if state == "waiting":
            self.show_auth_waiting()
        elif state == "done":
            self.hide_auth_waiting()

    def _style_stop_btn(self, running: bool) -> None:
        """중지 버튼 색 — 실행 중에만 실제 위험색(red), 유휴엔 muted.

        디자이너 심사 3차 🟡 — `DangerBtn` 은 `state="disabled"` 를 줘도
        CTkButton 이 자동으로 색을 죽이지 않아(customtkinter 6.0 실측:
        비활성은 클릭만 막을 뿐 `fg_color` 는 그대로), 유휴 상태에서도
        항상 활성 빨강으로 보였다. `bg`/`fg` 는 `_CtkButtonAdapter.config`
        가 `fg_color`/`text_color` 로 리맵해주므로(CTk·순정 tk 폴백 둘 다
        같은 호출로 동작) 실행 상태 전환마다 직접 다시 칠한다. 아이콘
        글리프(`stop`)는 별도 `CTkLabel`이라 어댑터가 못 건드리므로
        `_icon_label` private 속성에 직접 접근한다(app.py 의 `_theme_pair`
        cross-module 관례와 동일선상 — 있으면 쓰고 없으면 조용히 스킵).
        """
        try:
            if running:
                self.stop_btn.config(bg=C["red"], fg=C.get("on_accent", C["bg"]))
            else:
                self.stop_btn.config(bg=C["btn_secondary"], fg=C["muted"])
        except tk.TclError:
            pass
        icon_lbl = getattr(self.stop_btn, "_icon_label", None)
        if icon_lbl is not None:
            try:
                icon_lbl.configure(
                    text_color=(C.get("on_accent", C["bg"]) if running
                               else C["muted"]))
            except Exception:  # noqa: BLE001 — 아이콘 재도색 실패는 무해
                pass

    def on_run_state(self, running: bool, label: str) -> None:
        try:
            self.stop_btn.config(state="normal" if running else "disabled")
        except tk.TclError:
            pass
        self._style_stop_btn(running)
        try:
            if running:
                self._idle_lbl.pack_forget()
                self._progress_row.pack(fill="x")
            else:
                self._progress_row.pack_forget()
                self._progress_row.reset()
                self._idle_lbl.pack(fill="x")
        except tk.TclError:
            pass

    # ── 보안 인증 대기 UI 제어 ──
    def show_auth_waiting(self):
        """보안 인증 대기 배너를 표시하고 이 탭으로 전환한다.

        배너는 **스크롤 영역 밖**(진행 상황 상자 위)에 붙는다 — 사용자가
        즉시 처리해야 하는 일이라 스크롤해야 보이면 안 된다.

        그 대가로 배너 높이만큼 아래 공간을 먹는데, 그대로 두면 로그·
        미리보기 PanedWindow 몫이 통째로 줄어든다(실측 900×620: 193 → 64px
        — "Paned 높이는 실행 상태와 무관하게 일정" 불변식 위반, 로그창이
        거의 안 보임). 그래서 배너가 뜬 동안에는 **진행 상황 상자를 그만큼
        줄인다** — 그 안은 ScrollArea 라 내용이 잘리지 않고 스크롤될 뿐이다.
        """
        self.auth_frame.pack(before=self._auth_before, fill="x",
                             padx=SP["xl"], pady=(0, SP["sm"]))
        self.auth_btn.config(state="normal")
        self._shrink_region_for_auth()
        self.app.show_tab("posting")

    def hide_auth_waiting(self):
        """보안 인증 대기 배너를 숨기고 진행 상황 상자 높이를 되돌린다."""
        self.auth_frame.pack_forget()
        try:
            self._auth_before.configure(height=self._region_height)
        except tk.TclError:
            pass

    def _shrink_region_for_auth(self) -> None:
        """배너가 차지한 높이만큼 진행 상황 상자를 줄인다(최소 높이 보장)."""
        try:
            self.update_idletasks()
            banner = self.auth_frame.winfo_reqheight() + SP["sm"]
            self._auth_before.configure(
                height=max(_px(84), self._region_height - banner))
        except tk.TclError:
            pass

    def _on_auth_complete(self):
        """인증완료 버튼 클릭 핸들러."""
        self.auth_btn.config(state="disabled")
        if hasattr(self.app, 'auth_event') and self.app.auth_event:
            self.app.auth_event.set()
        self.log("사용자가 보안 인증 완료를 확인했습니다.", "ok")

    def log(self, msg: str, tag: str = ""):
        """실시간 로그 박스에 한 줄을 추가한다.

        발행/로그인 워커 스레드가 직접 호출해도 안전하도록, 메인 스레드가
        아니면 재위임한다(Tkinter 는 thread-safe 하지 않음). 창이 이미
        파괴된 뒤 호출돼도 무해하도록 ``tk.TclError`` 는 삼킨다.

        캡처 심사 3차 — `RunBus.log()` 가 fan-out 전에 이미 이모지를
        씻지만, 이 메서드는 `_on_auth_complete` 처럼 버스를 거치지 않고
        직접 호출되는 경로도 있어 여기서도 한 번 더 씻는다(멱등이라
        이중 적용해도 무해). 태그가 `_LOG_TAG_ICON` 에 있으면 이모지 대신
        통일 아이콘 글리프를 접두한다.
        """
        if threading.current_thread() is not threading.main_thread():
            self.ui_after(self.log, msg, tag)
            return
        try:
            clean = strip_emoji(msg)
            icon_name = self._LOG_TAG_ICON.get(tag)
            if icon_name and icons.has_icon_font():
                glyph = icons.glyph(icon_name)
                if glyph and glyph != "•":
                    clean = f"{glyph} {clean}"
            self.log_box.config(state="normal")
            ts = datetime.now().strftime("%H:%M:%S")
            self.log_box.insert("end", f"[{ts}] {clean}\n", tag)
            self.log_box.see("end")
            self.log_box.config(state="disabled")
        except tk.TclError:
            pass

    def set_progress(self, cur: int, total: int, status: str = ""):
        """진행행을 갱신한다.

        ``log()`` 와 동일한 계약(설계서 §0.2) — 워커 스레드가 직접 호출해도
        안전하도록 메인 스레드가 아니면 재위임한다(2026-09-02 수정 라운드
        🟠3, 리뷰어 지적: 이전엔 이 메서드만 가드가 빠져 있었다).
        """
        if threading.current_thread() is not threading.main_thread():
            self.ui_after(self.set_progress, cur, total, status)
            return
        try:
            self._progress_row.set(cur, total, status)
        except tk.TclError:
            pass

    def _update_post_list_visibility(self) -> None:
        """post_list(표)와 _post_list_empty(빈 상태)를 상호 배타로 토글한다.

        2026-09-02 수정 라운드 🔴2 — 둘 다 동시에 pack 되면(기존 공용
        `attach_empty_state` 관례) 카드가 너무 커져 아래 로그/미리보기가
        화면 밖으로 밀렸다. 이 탭만은 표를 숨기고 빈 상태로 완전히 대체한다.
        """
        try:
            is_empty = len(self.post_list.get_children()) == 0
        except tk.TclError:
            return
        if is_empty:
            self.post_list.pack_forget()
            self._post_list_empty.show()
        else:
            self._post_list_empty.hide()
            if not self.post_list.winfo_manager():
                self.post_list.pack(fill="x")

    # 실시간 로그 태그 → 아이콘 글리프 키(캡처 심사 3차 — 이모지 접두
    # 대신 통일 아이콘 폰트 글리프로). tag 는 `log_box.tag_config` 6종과
    # 일치(위 `_build` 참조). muted 는 구분선·잡음성 문구용이라 접두 없음.
    _LOG_TAG_ICON = {
        "ok":    "success",
        "err":   "warning",
        "warn":  "warning",
        "info":  "info",
        "dup":   "warning",
        "muted": None,
    }

    # ── 글별 진행 상태 체크리스트 ──
    _STATUS_ICON = {
        "waiting": "대기",
        "running": "진행",
        "ok":      "완료",
        "fail":    "실패",
        "verify":  "확인 필요",
        "skip":    "중복",
    }

    @staticmethod
    def _trim_title(title: str, length: int = 30) -> str:
        if not title:
            return ""
        return title if len(title) <= length else title[:length] + "…"

    def init_post_list(self, titles: list):
        """배치 시작 시 글 목록을 '대기' 상태로 초기화합니다."""
        try:
            self.post_list.delete(*self.post_list.get_children())
        except tk.TclError:
            return
        self._post_list_iids = {}
        self._post_row_seq = 0   # 목록을 비웠으니 순번도 0부터(트리 iid 재사용 방지)
        for title in titles or []:
            self._add_post_row(title, status="waiting")
        self._update_post_list_visibility()

    def _add_post_row(self, title: str, status: str = "waiting") -> str:
        """post_list에 한 행을 추가하고 iid를 반환합니다.

        iid 는 **행 추가 순번**으로만 만든다(``post_0``, ``post_1``…). 문자열
        ``hash()`` 는 프로세스마다 랜덤 시드가 달라 재현 불가하고, 16비트로 접으면
        충돌 여지도 있었다. 순번 기반은 충돌이 0이고 단조 증가한다.
        제목→iid 매핑(``_post_list_iids``)은 **최신 행**을 가리킨다: 동일 제목이 두 번
        들어오면 각각 별도 행으로 추가되고, 갱신은 마지막(최신) 행에 반영된다.
        """
        iid = f"post_{self._post_row_seq}"
        self._post_row_seq += 1
        icon = self._STATUS_ICON.get(status, status)
        self.post_list.insert(
            "", "end", iid=iid,
            values=(icon, self._trim_title(title), ""),
            tags=(status,))
        self._post_list_iids[title] = iid   # 최신 행 매핑(중복 제목은 최신을 갱신)
        self._update_post_list_visibility()
        return iid

    def update_post_status(self, title: str, status: str):
        """progress_cb에서 호출 — 매칭 행 갱신, 없으면 새 행 추가.

        제목으로 행을 못 찾으면(매핑 부재·행 소실) 새 행을 추가해 진행을 계속한다
        (fail-open — 상태 표시가 배치 진행을 막지 않는다).
        """
        if not title:
            return
        try:
            iid = self._post_list_iids.get(title)
            if iid is None or not self.post_list.exists(iid):
                iid = self._add_post_row(title, status=status)
            icon = self._STATUS_ICON.get(status, status)
            now  = datetime.now().strftime("%H:%M:%S")
            self.post_list.item(
                iid,
                values=(icon, self._trim_title(title), now),
                tags=(status,))
            # 진행 중인 행을 보이도록 스크롤
            self.post_list.see(iid)
        except tk.TclError:
            pass

    def _clear(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0","end")
        self.log_box.config(state="disabled")

    def _save(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", filetypes=[("텍스트","*.txt")])
        if path:
            Path(path).write_text(self.log_box.get("1.0","end"), encoding="utf-8")
            self._toast("로그를 저장했어요", kind="ok")

    # ── 미리보기 기능 ──
    def _get_preview_dir(self):
        """미리보기 HTML 폴더 — 활성 프로필별로 격리(계정 격리상 중요).

        default(examplebrand)는 naver_blog_v3/previews(기존 경로, 무변화),
        named 프로필은 profiles/<name>/previews 를 사용한다.
        """
        try:
            from config import profile as _profile
            return _profile.previews_dir()
        except Exception:  # noqa: BLE001 — 프로필 모듈 부재 시 레거시 경로
            return Path(__file__).parent.parent.parent / "previews"

    # ── 저장본(draft_store) 미리보기 ─────────────────────────────
    # "생성된 글 미리보기" 는 이름 그대로 **생성된 글**을 보여야 하는데, 예전엔
    # 발행 직전에만 쓰이는 previews/*.html 만 읽었다. 그래서 보관함 채우기로
    # 글을 계속 만들어도 이 목록은 마지막 '발행' 시점(실측: 2026-07-14)에서
    # 멈춰 있었다. 이제 draft_store(=원본)를 1순위로 읽고, 옛 발행 미리보기
    # 파일은 지우지 않고 '발행본' 으로 구분해 함께 보여준다.
    _DRAFT_VIEW_SUBDIR = "_draft_view"

    def _draft_view_dir(self) -> Path:
        return self._get_preview_dir() / self._DRAFT_VIEW_SUBDIR

    def _recent_drafts(self, limit: int = 20) -> list:
        """활성 프로필 runs/ 의 최근 저장본 — **`gui.draft_library` 에 위임**.

        2026-09-21 개편: 저장 글 전용 탭(`gui/tabs/archive.py`)이 같은 목록을
        보여주므로, 스캔·정렬·제외 규칙을 두 벌로 두지 않고 공용 조회 계층
        하나만 쓴다(한쪽만 고쳐져 숫자가 어긋나는 사고 차단). 반환 dict 의
        기존 키(slug/dir/post/status/ts)는 그대로라 아래 소비자 코드는 무변경.
        """
        from gui import draft_library
        return draft_library.list_drafts(limit=limit)

    def _write_draft_view(self, entry: dict) -> Optional[str]:
        """저장본 1편을 미리보기 HTML 로 굳힌다(기존 포맷 그대로 → 열기·텍스트
        미리보기·영상 만들기가 코드 변경 없이 그대로 동작).

        이미 최신이면 다시 쓰지 않는다. 실패하면 None.
        """
        post = entry["post"]
        out_dir = self._draft_view_dir()
        target = out_dir / f"preview_draft_{entry['slug'][:80]}.html"
        try:
            if target.exists() and target.stat().st_mtime >= entry["ts"]:
                return str(target)
        except OSError:
            pass

        title = post.get("title") or entry["status"].get("title") or "(제목 없음)"
        content = post.get("content_html") or post.get("content") or ""
        images = [p for p in (post.get("image_paths") or []) if p]
        img_html = "".join(
            f'<div style="margin:12px 0;"><img src="file:///{str(p).replace(os.sep, "/")}"'
            f' style="max-width:100%;"></div>' for p in images)
        created = entry["status"].get("created_at") or "—"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "<!DOCTYPE html><html lang=\"ko\"><head><meta charset=\"UTF-8\">"
                f"<title>[저장본] {html_escape(title)}</title></head><body>"
                "<div class=\"meta\">"
                f"<div><strong>제목:</strong> {html_escape(title)}</div>"
                # 요약은 비어도 한 글자는 둔다 — 기존 파서가 `(.+?)</div>` 라
                # 빈 값이면 다음 <div> 까지 삼켜 '생성/상태' 줄이 요약으로 보인다.
                f"<div>생성: {html_escape(str(created))} · 상태: "
                f"{html_escape(str(entry['status'].get('status') or ''))} · "
                f"이미지 {len(images)}장</div>"
                f"<div><strong>요약:</strong> "
                f"{html_escape(str(post.get('summary') or '—'))}</div>"
                + "".join(f'<span class="tag">#{html_escape(str(t))}</span>'
                          for t in (post.get("tags") or [])[:15])
                + "</div>"
                f"<article>{img_html}{content}</article></body></html>",
                encoding="utf-8")
            return str(target)
        except OSError as e:
            log.debug("저장본 미리보기 쓰기 실패 %s: %s", entry["slug"], e)
            return None

    def _refresh_previews(self):
        """미리보기 목록 로드 — 저장본(원본) + 옛 발행 미리보기 파일."""
        items: list = []
        self._preview_paths = []
        self._preview_kinds = []

        # 1) 저장본 — draft_store 가 원본. 실제 생성·수정 시각으로 표시한다.
        for entry in self._recent_drafts(limit=20):
            path = self._write_draft_view(entry)
            if not path:
                continue
            imgs = len([p for p in (entry["post"].get("image_paths") or []) if p])
            title = (entry["post"].get("title")
                     or entry["status"].get("title") or entry["slug"])
            stamp = datetime.fromtimestamp(entry["ts"]).strftime("%m월 %d일 %H:%M")
            items.append((entry["ts"],
                          f"{stamp} · 저장본 · 이미지 {imgs}장 · {str(title)[:34]}",
                          path, "draft"))

        # 2) 옛 발행 미리보기 파일 — 지우지 않고 구분만 한다.
        pv_dir = self._get_preview_dir()
        if pv_dir.exists():
            for f in pv_dir.glob("preview_*.html"):
                try:
                    ts = f.stat().st_mtime
                except OSError:
                    continue
                items.append((ts, f"{self._preview_display_label(f)} · 발행본",
                              str(f), "legacy"))

        if not items:
            self.preview_combo["values"] = ["(미리보기 파일 없음)"]
            self.preview_combo.current(0)
            return

        items.sort(key=lambda t: t[0], reverse=True)
        items = items[:40]
        labels = [it[1] for it in items]
        self._preview_paths = [it[2] for it in items]
        self._preview_kinds = [it[3] for it in items]
        self.preview_combo["values"] = labels
        items = labels
        if items:
            self.preview_combo.current(0)
            self._show_preview_content(self._preview_paths[0])
        elif hasattr(self, 'preview_text'):
            self._clear_preview_fallback()
            self.preview_text.config(state="normal")
            self.preview_text.delete("1.0", "end")
            self.preview_text.config(state="disabled")

    @staticmethod
    def _preview_display_label(f: Path) -> str:
        """preview_0902_01_163405.html → "9월 2일 · 제목…"(제목 못 찾으면 번호)."""
        name = f.stem  # preview_0902_01_163405
        parts = name.replace("preview_", "").split("_")
        month = day = None
        if len(parts) >= 2 and len(parts[0]) == 4 and parts[0].isdigit():
            month, day = int(parts[0][:2]), int(parts[0][2:])
        num_part = parts[1] if len(parts) >= 2 else ""
        base = f"{month}월 {day}일" if month else f.stem
        display = f"{base} · #{num_part}" if num_part else base
        try:
            text = f.read_text(encoding="utf-8")
            m = re.search(r'<strong>제목:</strong>\s*(.+?)</div>', text)
            if m:
                title = m.group(1).strip()[:40]
                display = f"{base} · {title}"
        except (OSError, UnicodeDecodeError):
            pass
        return display

    def _on_preview_select(self, event=None):
        """콤보박스 선택 변경 시 본문 미리보기 업데이트"""
        idx = self.preview_combo.current()
        if idx < 0 or not hasattr(self, '_preview_paths') or idx >= len(self._preview_paths):
            return
        path = self._preview_paths[idx]
        self._show_preview_content(path)

    def _clear_preview_fallback(self):
        """폴백 안내 버튼('브라우저로 열기')이 떠 있으면 제거한다.

        매 미리보기 렌더 시작 시 호출해 이전 폴백 잔여 위젯이 남지 않게 한다.
        """
        frame = getattr(self, "_preview_fallback_frame", None)
        if frame is not None:
            try:
                frame.destroy()
            except tk.TclError:
                pass
            self._preview_fallback_frame = None

    def _show_preview_fallback(self, path: str):
        """고정 포맷 파싱이 실패한 HTML — 텍스트 미리보기 대신 브라우저 열기 안내.

        앱이 만든 preview 포맷(``<strong>제목:</strong>``·``<article>``)이 아니면
        정규식으로 제목·본문을 뽑을 수 없다. '(제목 없음)'·빈 본문으로 오해하게 두지
        않고, 이 형식은 텍스트 미리보기가 없음을 명시하고 브라우저 열기를 제공한다.
        """
        self.preview_text.config(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert(
            "1.0",
            "이 형식은 텍스트 미리보기를 지원하지 않아요.\n"
            "아래 '브라우저로 열기' 버튼으로 원본을 확인하세요.")
        self.preview_text.config(state="disabled")
        # 텍스트 영역 아래에 폴백 버튼 프레임을 동적으로 붙인다(재호출 시 정리됨).
        frame = tk.Frame(self.preview_text.master, bg=C["bg"])
        frame.pack(fill="x", padx=SP["sm"], pady=(0, SP["xs"]))
        SecondaryBtn(frame, "브라우저로 열기",
                    lambda p=path: self._open_preview(p)).pack(side="left")
        self._preview_fallback_frame = frame

    def _show_preview_content(self, path: str):
        """HTML 파일에서 텍스트를 추출하여 미리보기 영역에 표시.

        앱 생성 포맷(제목·``<article>`` 본문)을 못 찾으면 텍스트 파싱을 포기하고
        브라우저 열기 안내로 폴백한다('(제목 없음)'·빈 본문 방지).
        """
        self._clear_preview_fallback()
        self.preview_text.config(state="normal")
        self.preview_text.delete("1.0", "end")
        try:
            html = Path(path).read_text(encoding="utf-8")
            # 제목 추출
            m_title = re.search(r'<strong>제목:</strong>\s*(.+?)</div>', html)
            # 본문 추출 (<article> 내용)
            m_body = re.search(r'<article>(.*?)</article>', html, re.DOTALL)
            # 앱 포맷 판별 — 제목·본문 둘 다 못 찾으면 미지원 포맷으로 폴백.
            if m_title is None and m_body is None:
                self._show_preview_fallback(path)
                return
            title = m_title.group(1).strip() if m_title else "(제목 없음)"
            # 요약 추출
            m_summary = re.search(r'<strong>요약:</strong>\s*(.+?)</div>', html)
            summary = m_summary.group(1).strip() if m_summary else ""
            # 태그 추출
            tags = re.findall(r'<span class="tag">(#.+?)</span>', html)
            body_html = m_body.group(1) if m_body else ""
            # HTML 태그 제거하여 텍스트 추출
            body_text = re.sub(r'<br\s*/?>', '\n', body_html)
            body_text = re.sub(r'</?(p|div|h[1-6]|li|tr)[^>]*>', '\n', body_text)
            body_text = re.sub(r'<[^>]+>', '', body_text)
            body_text = re.sub(r'&nbsp;', ' ', body_text)
            body_text = re.sub(r'&lt;', '<', body_text)
            body_text = re.sub(r'&gt;', '>', body_text)
            body_text = re.sub(r'&amp;', '&', body_text)
            body_text = re.sub(r'\n{3,}', '\n\n', body_text).strip()
            # 조합하여 표시
            display = f"[ 제목 ]\n{title}\n"
            if summary:
                display += f"\n[ 요약 ]\n{summary}\n"
            if tags:
                display += f"\n[ 태그 ]\n{' '.join(tags)}\n"
            display += f"\n[ 본문 ] ({len(body_text)}자)\n{'─'*50}\n{body_text}"
            self.preview_text.insert("1.0", display)
        except Exception as e:
            log.debug("미리보기 로드 오류", exc_info=True)
            self.preview_text.insert("1.0", f"미리보기 로드 실패: {e}")
        self.preview_text.config(state="disabled")

    def _delete_preview(self):
        """선택된 미리보기 파일 삭제"""
        idx = self.preview_combo.current()
        if idx < 0 or not hasattr(self, '_preview_paths') or idx >= len(self._preview_paths):
            self._toast("삭제할 미리보기를 먼저 선택해 주세요", kind="warn")
            return
        kinds = getattr(self, "_preview_kinds", [])
        if idx < len(kinds) and kinds[idx] == "draft":
            self._toast("저장본은 여기서 지울 수 없어요 — 보관함에서 관리하세요",
                        kind="warn")
            return
        path = self._preview_paths[idx]
        name = os.path.basename(path)
        if not messagebox.askyesno("삭제 확인", f"미리보기 파일을 삭제할까요?\n{name}"):
            return
        try:
            os.remove(path)
            self._refresh_previews()
        except OSError as e:
            messagebox.showerror("삭제 실패", f"삭제하지 못했어요: {e}")

    def _open_preview(self, path: Optional[str] = None):
        """미리보기 HTML 을 브라우저로 연다.

        ``path`` 를 주면 그 파일을(폴백 안내의 '브라우저로 열기' 버튼이 재사용),
        생략하면 콤보박스에서 선택된 파일을 연다.
        """
        if path is None:
            idx = self.preview_combo.current()
            if idx < 0 or not hasattr(self, '_preview_paths') or idx >= len(self._preview_paths):
                self._toast("미리보기를 먼저 선택해 주세요", kind="warn")
                return
            path = self._preview_paths[idx]
        if os.path.isfile(path):
            webbrowser.open(f"file:///{path.replace(os.sep, '/')}")
        else:
            messagebox.showerror("열기 실패", "파일을 찾을 수 없어요.")

    def _open_preview_folder(self):
        """미리보기 폴더 열기"""
        pv_dir = self._get_preview_dir()
        pv_dir.mkdir(exist_ok=True)
        os.startfile(str(pv_dir))

    def _run_multichannel_build(self):
        """미리보기 헤더에서 멀티채널 팩(영상) 생성 진입점.

        `app.run_multichannel_build()` 가 있으면 그걸 쓰고(설계서 §1.5),
        없으면 옛 폴백(설정 탭 직접 위임)을 그대로 쓴다.
        """
        run_mc = getattr(self.app, "run_multichannel_build", None)
        if callable(run_mc):
            run_mc()
            return
        settings_tab = self.app.pages.get("settings")
        fn = getattr(settings_tab, "_run_multichannel_build", None)
        if callable(fn):
            fn()
        else:
            messagebox.showerror(
                "영상 만들기",
                "설정 탭이 로드되지 않아 영상 만들기로 들어갈 수 없어요.\n"
                "프로그램을 다시 시작하고 다시 시도해 주세요.")

    def _delete_all_previews(self):
        """previews 폴더 내 모든 미리보기 HTML 파일 일괄 삭제"""
        pv_dir = self._get_preview_dir()
        files = list(pv_dir.glob("preview_*.html")) if pv_dir.exists() else []
        if not files:
            self._toast("삭제할 미리보기가 없어요", kind="info")
            return
        if not messagebox.askyesno(
                "전체 삭제 확인",
                f"미리보기 파일 {len(files)}개를 모두 삭제할까요?"):
            return
        failed = 0
        for f in files:
            try:
                f.unlink()
            except OSError:
                failed += 1
        self._refresh_previews()
        if failed:
            self._toast(f"{len(files)-failed}개 삭제 완료, {failed}개는 실패했어요", kind="warn")
        else:
            self._toast(f"미리보기 {len(files)}개를 삭제했어요", kind="ok")

    def _post_history_path(self) -> Path:
        """발행 기록 경로 — 활성 프로필별 격리(계정 격리상 중요).

        default(examplebrand)는 naver_blog_v3/logs/post_history.json(기존 경로, 무변화),
        named 프로필은 profiles/<name>/logs/post_history.json 을 사용한다.
        """
        try:
            from config import profile as _profile
            return _profile.logs_dir() / "post_history.json"
        except Exception:  # noqa: BLE001 — 프로필 모듈 부재 시 레거시 경로
            return Path(__file__).parent.parent.parent / "logs" / "post_history.json"

    def _clear_post_history(self):
        """post_history.json 발행 기록 전체 초기화 — 되돌릴 수 없음, 확인 1회.

        (설계서 §3.3 — 기존 2단 확인을 1단으로 통합하되 "되돌릴 수 없음"을
        본문에 명시해 안전성은 유지한다.)
        """
        history_path = self._post_history_path()
        if not history_path.exists():
            self._toast("발행 기록 파일이 없어요", kind="info")
            return
        try:
            import json
            records = json.loads(history_path.read_text(encoding="utf-8"))
            count = len(records) if isinstance(records, list) else 0
        except (json.JSONDecodeError, OSError):
            count = 0
        if count == 0:
            self._toast("발행 기록이 비어 있어요", kind="info")
            return
        if not messagebox.askyesno(
                "발행 기록 초기화",
                f"발행 기록 {count}편을 초기화하면 중복 검사 기준이 모두 사라져요.\n\n"
                f"이 작업은 되돌릴 수 없어요. 이후 같은 키워드로 다시 써도 "
                f"중복으로 걸러지지 않아요.\n\n"
                f"정말 초기화할까요?",
                icon="warning"):
            return
        try:
            from config import atomic_write_json
            atomic_write_json(history_path, [])
        except (ImportError, OSError) as e:
            messagebox.showerror("초기화 실패", f"초기화하지 못했어요: {e}")
            return
        self._update_history_count()
        self._toast(
            f"발행 기록 {count}편을 초기화했어요 · 중복 검사 기준이 비었어요", kind="ok")

    def _update_history_count(self):
        """발행 기록 건수 라벨 갱신"""
        history_path = self._post_history_path()
        count = 0
        if history_path.exists():
            try:
                import json
                records = json.loads(history_path.read_text(encoding="utf-8"))
                count = len(records) if isinstance(records, list) else 0
            except (json.JSONDecodeError, OSError):
                pass
        self.history_count_lbl.config(text=f"현재 {count}건")
