# gui/tabs/log.py — 실행 기록 탭
"""날짜별 발행 로그(JSON)를 표로 보여준다.

로그 파일 로드는 `UiLifecycle.spawn` 워커에서 수행하고, 결과 반영은
`ui_after` 로만 한다(설계서 §0.3 스레드 위생 — 워커가 Tk 위젯을 직접
만지지 않는다). 파괴 후 도착하는 콜백은 `ui_after` 가 조용히 무시한다.

auto-dev 8호 Phase 2(2026-09-03, `docs/auto-dev/gui-design-system-v3.md`) —
상용 서비스급 리스킨. 이모지를 걷어내고 `gui.icons` 글리프로, 날짜 입력을
FormField 로, [오늘]/[어제] 를 GhostBtn 으로 바꿨다. 결과 컬럼도 이모지 대신
글자(완료/중복/실패)로 표기한다.
"""
import json
import logging
import os
import tkinter as tk
from datetime import date, timedelta
from pathlib import Path
from tkinter import messagebox, ttk

from gui import icons
from gui import theme as _theme
from gui.async_utils import UiLifecycle
from gui.data_table import DataTable
from gui.theme import (C, FB, FS, SP, F, L, Sep, GhostBtn,
                        SecondaryBtn, SubtleBtn)
from gui.widgets import EmptyState, FormField, Header, attach_empty_state

px = getattr(_theme, "px", lambda n: n)

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent.parent  # naver_blog_v3/
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

HARNESS_ROOT = BASE_DIR.parent                   # naver_blog_automation_v3/

_HARNESS_FOLDERS = [
    ("briefs 폴더", "briefs"),
    ("runs 폴더", "runs"),
    ("reports 폴더", "reports"),
    ("errors 폴더", "errors"),
]


class LogTab(UiLifecycle, tk.Frame):
    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self.app = app
        self._init_lifecycle()
        self._build()

    def destroy(self) -> None:  # UiLifecycle이 파괴 플래그를 먼저 세운 뒤 실제 파괴
        UiLifecycle.destroy(self)

    def _build(self):
        header = Header(self, "실행 기록", icon=icons.glyph("log"))
        header.pack(fill="x", padx=SP["lg"], pady=(SP["lg"], SP["sm"]))
        SecondaryBtn(header.actions, "새로고침", self.refresh,
                    icon="refresh").pack(side="right")
        Sep(self).pack(fill="x", padx=SP["lg"])

        dr = F(self, padx=SP["lg"], pady=px(10))
        dr.pack(fill="x")
        self.date_var = tk.StringVar(value=date.today().strftime("%Y-%m-%d"))
        FormField(dr, "날짜", self.date_var, kind="entry", width=14).pack(
            side="left", fill="x")
        SecondaryBtn(dr, "조회", self._load_date).pack(side="left", padx=(SP["sm"], SP["sm"]))
        GhostBtn(dr, "오늘", self._show_today).pack(side="left")
        GhostBtn(dr, "어제", self._show_yesterday).pack(side="left", padx=(px(4), 0))
        Sep(self).pack(fill="x")

        tf = F(self, padx=SP["lg"], pady=px(12))
        tf.pack(fill="both", expand=True)

        self._tree_wrap = F(tf)
        self._tree_wrap.pack(fill="both", expand=True)
        cols = ("시간", "제목", "예약시간", "결과", "상세")
        self.tree = DataTable(self._tree_wrap, columns=cols, show="headings")
        for col, w in zip(cols, [px(70), px(300), px(110), px(60), px(140)]):
            self.tree.heading(col, text=col)
            self.tree.column(col, width=w, anchor="w")
        vsb = ttk.Scrollbar(self._tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.tag_configure("ok", foreground=C["green"])
        self.tree.tag_configure("err", foreground=C["red"])
        self.tree.tag_configure("dup", foreground=C["orange"])

        self._empty = EmptyState(
            tf, icons.glyph("log"), "이 날짜엔 실행 기록이 없어요",
            "발행·진행 탭에서 실행해 보세요.")
        # attach_empty_state 가 "표 항목이 0개인지"의 단일 판정 기준이다 —
        # 반환된 checker 를 _sync_empty_state() 가 표 갱신마다 다시 부른다
        # (디자이너 심사 🟡 — 빈 표만 덩그러니 보이던 버그).
        self._check_empty = attach_empty_state(self.tree, self._empty)
        self._sync_empty_state()  # 초기값 — 표가 실제로 비어 있으니 비동기 결과를
                                   # 기다리지 않고 지금 바로 보여준다.

        self.sum_lbl = L(self, "", FS, C["text_2"])
        self.sum_lbl.pack(anchor="w", padx=SP["lg"], pady=px(6))

        # ── 하네스 폴더 빠른 열기 버튼 ──
        folder_row = F(self)
        folder_row.pack(fill="x", padx=SP["lg"], pady=(0, px(12)))
        L(folder_row, "폴더 열기", FS, C["text_2"]).pack(side="left", padx=(0, px(6)))
        for label, rel in _HARNESS_FOLDERS:
            SubtleBtn(
                folder_row, label, lambda r=rel: self._open_harness_folder(r)
            ).pack(side="left", padx=px(4))

        # 생성자에서 refresh() 를 곧장 부르면 백그라운드 스레드가 앱의
        # mainloop() 가 시작되기 전에 self.after(0, ...) 를 걸게 될 수 있다
        # — CPython 3.11 tkinter 는 이때 "main thread is not in main loop"
        # RuntimeError 를 던지고, ui_after() 는 그 예외를 조용히 삼켜서 표가
        # 영영 안 채워진다(2026-09-03 실측 확인 — 디자이너 심사 버그의 근본
        # 원인). ui_after(delay=0) 로 한 틱 미뤄 mainloop 가 확실히 돈 뒤에
        # 스레드를 스폰하게 한다.
        self.ui_after(self.refresh)

    def _show_today(self) -> None:
        self.date_var.set(date.today().strftime("%Y-%m-%d"))
        self._load_date()

    def _show_yesterday(self) -> None:
        self.date_var.set((date.today() - timedelta(days=1)).strftime("%Y-%m-%d"))
        self._load_date()

    def _open_harness_folder(self, rel: str):
        """하네스 루트 기준 상대 폴더를 탐색기로 엽니다."""
        target = HARNESS_ROOT / rel
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("폴더 생성 실패: %s", exc, exc_info=True)
            messagebox.showerror("폴더 열기", "폴더를 만들지 못했어요. 다시 시도해 주세요.")
            return
        try:
            os.startfile(str(target))
        except OSError as exc:
            log.error("폴더 열기 실패: %s", exc, exc_info=True)
            messagebox.showerror("폴더 열기", "폴더를 여는 데 실패했어요.")

    def _load_date(self):
        """선택 날짜의 로그 JSON을 백그라운드에서 읽고, 결과만 메인 스레드에 반영한다."""
        target_date = self.date_var.get()
        self.spawn(self._read_log_file, target_date, name="log-load")

    def _read_log_file(self, target_date: str) -> None:
        """워커 스레드 — 파일 I/O만 하고 위젯은 절대 만지지 않는다."""
        p = _DATA_DIR / "logs" / f"{target_date}.json"
        if not p.exists():
            self.ui_after(self._apply_missing, target_date)
            return
        try:
            logs = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.error("로그 로드 실패: %s", exc, exc_info=True)
            self.ui_after(self._apply_error, target_date)
            return
        self.ui_after(self._apply_logs, target_date, logs)

    def _sync_empty_state(self) -> None:
        """표/빈 상태 안내를 상호 배타로 토글한다(같은 부모에 둘 다 pack 되면
        겹친다). 판정 기준은 ``attach_empty_state`` 가 만든 checker
        (``len(tree.get_children())==0``) 하나뿐이다 — 이전엔 호출부마다
        불리언을 따로 넘겨(``_toggle_empty(True/False)``) 실제 표 상태와
        어긋날 여지가 있었다(디자이너 심사 🟡 — 표만 비어 있고 안내가 안
        뜨던 버그의 한 갈래)."""
        is_empty = len(self.tree.get_children()) == 0
        if is_empty:
            self._tree_wrap.pack_forget()
        elif not self._tree_wrap.winfo_manager():
            self._tree_wrap.pack(fill="both", expand=True)
        self._check_empty()

    def _apply_missing(self, target_date: str) -> None:
        if target_date != self.date_var.get():
            return  # 사용자가 그 사이 다른 날짜로 다시 조회함 — 낡은 결과는 버린다.
        self.tree.delete(*self.tree.get_children())
        self.sum_lbl.config(text="")
        self._sync_empty_state()

    def _apply_error(self, target_date: str) -> None:
        if target_date != self.date_var.get():
            return
        self.tree.delete(*self.tree.get_children())
        self._sync_empty_state()
        self.sum_lbl.config(text="로그 파일을 읽지 못했어요. 다시 시도해 주세요.")

    def _apply_logs(self, target_date: str, logs: list) -> None:
        if target_date != self.date_var.get():
            return
        self.tree.delete(*self.tree.get_children())
        if not logs:
            self.sum_lbl.config(text="")
            self._sync_empty_state()
            return
        ok = fail = dup = 0
        for item in logs:
            detail = item.get("detail", "")
            is_dup = "중복" in detail
            success = item.get("success", False)
            if is_dup:
                result = "중복"; dup += 1; tag = "dup"
            elif success:
                result = "완료"; ok += 1; tag = "ok"
            else:
                result = "실패"; fail += 1; tag = "err"
            self.tree.insert(
                "", "end",
                values=(item.get("time", ""), item.get("title", "")[:42],
                        item.get("reserve_time", ""), result, detail[:30]),
                tags=(tag,))
        self._sync_empty_state()  # 행을 다 넣은 뒤에 판정해야 정확하다
        self.sum_lbl.config(
            text=f"총 {ok + fail + dup}건 · 성공 {ok} · 실패 {fail} · 중복 {dup}")

    def refresh(self):
        self.date_var.set(date.today().strftime("%Y-%m-%d"))
        self._load_date()
