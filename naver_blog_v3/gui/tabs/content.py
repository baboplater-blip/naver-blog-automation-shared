# gui/tabs/content.py — 콘텐츠 관리 (리프레시 + 시리즈 + 템플릿) 탭
import tkinter as tk
from tkinter import ttk, messagebox
import logging

log = logging.getLogger(__name__)

from gui.theme import (C, SP, FB, FS, L, Ent, bind_mousewheel,
                       PrimaryBtn, SecondaryBtn, DangerBtn, GhostBtn,
                       run_async)
from gui.widgets import Header, SectionCard, EmptyState, Segmented
from gui.data_table import DataTable
from gui.async_utils import UiLifecycle
from gui import icons
from gui import theme as _theme
from core.content_generator import load_templates, save_template, delete_template


def _px(n):
    """`theme.px(n)` 가 있으면 그걸(고DPI 스케일 반영), 없으면 그대로 —
    auto-dev 9호, growth.py 의 동일 헬퍼와 같은 방어적 폴백 패턴."""
    scale_fn = getattr(_theme, "px", None)
    return scale_fn(n) if scale_fn else n


def _active_excel_path(fallback: str) -> str:
    """활성 프로필의 키워드 엑셀 경로. brand.excel_path() 우선, 빈값이면 fallback.

    default(examplebrand)는 brand.excel_path() 가 KEYWORD_EXCEL_PATH 를 그대로 돌려주므로
    기존 경로와 동일(무변화). named 프로필은 자기 keywords/*.xlsx 를 쓴다.
    """
    try:
        from config import brand as _brand
        return _brand.excel_path() or fallback
    except Exception:  # noqa: BLE001 — 브랜드 헬퍼 부재 시 레거시 경로
        return fallback


# 세그먼트 정의 — (key, 라벨). 순서가 곧 SegmentedButton 노출 순서.
_SEGMENTS = [
    ("refresh",  "다시 쓰기"),
    ("series",   "시리즈"),
    ("template", "템플릿"),
]
_KEY_TO_LABEL = {key: label for key, label in _SEGMENTS}
_LABEL_TO_KEY = {label: key for key, label in _SEGMENTS}


class ContentTab(UiLifecycle, tk.Frame):
    """30일 리프레시 + 시리즈 구성 + 템플릿 통합 탭 — 한 화면엔 1기능만 보여준다."""

    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self.app = app
        self._series_list = []   # 로드된 시리즈 목록
        self._segments = {}      # key -> 세그먼트 컨테이너(SectionCard, 자체가 tk.Frame)
        self._active_segment = "refresh"
        self._init_lifecycle()
        self._build()

    def _ui_after(self, delay_ms, callback):
        """워커 스레드에서 UI 콜백을 메인 루프에 안전하게 예약한다(UiLifecycle 위임)."""
        self.ui_after(callback, delay=delay_ms)

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

    def _build(self):
        header = Header(self, "콘텐츠 관리", "오래된 글을 다시 쓰거나, 시리즈·템플릿을 관리해요.",
                        icon=icons.glyph("content"))
        header.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["sm"]))

        # v3 디자인 시스템 §5-6 — 3택 전환은 CTkSegmentedButton(있으면). 콜백은
        # 기존 _show_segment 계약을 그대로 호출해(설계서 지침) 화면 전환 로직은
        # 위젯 구현과 무관하게 단일 소스로 유지한다.
        self._segmented = Segmented(
            self, values=[label for _key, label in _SEGMENTS],
            command=lambda label: self._show_segment(_LABEL_TO_KEY[label]))
        self._segmented.pack(fill="x", padx=SP["xl"], pady=(0, SP["md"]))

        # ── 스크롤 영역 ──────────────────────────────────
        canvas = tk.Canvas(self, bg=C["bg"], bd=0, highlightthickness=0)
        vsb    = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        sf     = tk.Frame(canvas, bg=C["bg"])
        sf.bind("<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=sf, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        bind_mousewheel(canvas)

        self._segments["refresh"]  = self._build_refresh_segment(sf)
        self._segments["series"]   = self._build_series_segment(sf)
        self._segments["template"] = self._build_template_segment(sf)

        self._show_segment("refresh")

        # 초기 로드 — 세 세그먼트 모두 데이터는 미리 채워둔다(전환만으로 바로 보이게).
        self._load_refresh_candidates()
        self._load_series_list()
        self._load_templates()

    # ── 세그먼트 전환 ─────────────────────────────────
    def _show_segment(self, key: str) -> None:
        if key not in self._segments:
            return
        self._active_segment = key
        for k, frame in self._segments.items():
            if k == key:
                frame.pack(fill="both", expand=True, padx=SP["xl"], pady=(0, SP["xl"]))
            else:
                frame.pack_forget()
        label = _KEY_TO_LABEL.get(key)
        if label is not None:
            self._segmented.set(label)

    # ════════════════════════════════════════════════
    # 세그먼트: 다시 쓰기(30일 자동 리프레시)
    # ════════════════════════════════════════════════
    def _build_refresh_segment(self, parent):
        sec = SectionCard(parent, "오래된 글 다시 쓰기",
                          "발행 30일 이후 글을 최신 내용으로 갱신해 검색 순위를 다시 올려요.",
                          icon=icons.glyph("refresh"))

        # 통계 카드 행
        self.refresh_stats_frame = tk.Frame(sec.body, bg=C["card"])
        self.refresh_stats_frame.pack(fill="x", pady=(0, 12))
        self._refresh_stat_labels = {}
        for key, label, default in [
            ("total_posts",    "전체 포스팅",  "—"),
            ("total_refreshed","다시 쓰기 완료","—"),
            ("need_refresh",   "다시 쓰기 대상","—"),
        ]:
            box = tk.Frame(self.refresh_stats_frame, bg=C["surface"],
                           relief="flat", bd=0)
            box.pack(side="left", padx=(0, 8), ipadx=12, ipady=8)
            # "—" 를 20pt 볼드 + accent(채도 높은 파랑)로 그리면 숫자를 아직
            # 안 읽어온 상태가 "그냥 색깔 있는 가로선"처럼 보였다(디자이너
            # 실측). 값이 없을 때는 muted 로 중립 표시하고, 실제 숫자가
            # 들어오면(아래 _update) accent 로 되살린다.
            tk.Label(box, text=default, font=("맑은 고딕", 20, "bold"),
                     fg=C["muted"], bg=C["surface"]).pack()
            tk.Label(box, text=label,   font=FS,
                     fg=C["muted"],  bg=C["surface"]).pack()
            self._refresh_stat_labels[key] = box.winfo_children()[0]

        # 대상 글 표
        self._refresh_tree_wrap = tk.Frame(sec.body, bg=C["card"])
        r_cols = ("제목", "발행일", "경과일", "다시 쓴 횟수", "상태")
        self.refresh_tree = DataTable(self._refresh_tree_wrap, columns=r_cols, show="headings",
                                       height=6)
        r_widths = [270, 90, 60, 90, 70]
        r_minwidths = [160, 70, 50, 70, 60]
        for col, w, mw in zip(r_cols, r_widths, r_minwidths):
            self.refresh_tree.heading(col, text=col)
            self.refresh_tree.column(col, width=_px(w), minwidth=_px(mw), anchor="w")
        r_vsb = ttk.Scrollbar(self._refresh_tree_wrap, orient="vertical", command=self.refresh_tree.yview)
        self.refresh_tree.configure(yscrollcommand=r_vsb.set)
        r_vsb.pack(side="right", fill="y")
        self.refresh_tree.pack(fill="both", expand=True)
        self.refresh_tree.tag_configure("need",    foreground=C["orange"])
        self.refresh_tree.tag_configure("done",    foreground=C["green"])
        self.refresh_tree.tag_configure("pending", foreground=C["muted"])

        self._refresh_empty = EmptyState(
            sec.body, icons.glyph("refresh"), "다시 쓸 글이 아직 없어요",
            "'대상 목록 새로고침'을 눌러 30일이 지난 글을 확인해 보세요.",
            action_text="대상 목록 새로고침", action_cmd=lambda: self._load_refresh_candidates())
        self._sync_refresh_empty()

        # 주요 액션 행
        a_bot1 = tk.Frame(sec.body, bg=C["card"])
        a_bot1.pack(fill="x", pady=(10, 0))
        self._refresh_load_btn = SecondaryBtn(a_bot1, "대상 목록 새로고침", self._load_refresh_candidates)
        self._refresh_load_btn.pack(side="left")
        self._refresh_sel_btn = PrimaryBtn(a_bot1, "선택 글 리프레시", self._run_refresh_selected)
        self._refresh_sel_btn.pack(side="left", padx=8)
        # 보조 액션 행 — 전체 자동 리프레시는 여러 글을 한꺼번에 재작성하는
        # 되돌리기 어려운 작업이라 위험 버튼 + 확인을 그대로 유지한다.
        a_bot2 = tk.Frame(sec.body, bg=C["card"])
        a_bot2.pack(fill="x", pady=(6, 0))
        self._refresh_batch_btn = DangerBtn(a_bot2, "전체 자동 리프레시", self._run_refresh_batch,
                                            icon="refresh")
        self._refresh_batch_btn.pack(side="left")
        GhostBtn(a_bot2, "테스트로 먼저 보기", self._run_refresh_dry, 16).pack(side="left", padx=8)
        self.refresh_status = L(a_bot2, "", FS, C["muted"])
        self.refresh_status.pack(side="right", padx=4)
        return sec

    def _sync_refresh_empty(self) -> None:
        if len(self.refresh_tree.get_children()) == 0:
            self._refresh_tree_wrap.pack_forget()
            self._refresh_empty.show()
        else:
            self._refresh_empty.hide()
            self._refresh_tree_wrap.pack(fill="both", expand=True)

    def _load_refresh_candidates(self):
        def _task():
            try:
                from core.post_refresher import get_refresh_candidates, get_refresh_stats
                stats = get_refresh_stats()
                cands = get_refresh_candidates(50)

                def _update():
                    for key, lbl in self._refresh_stat_labels.items():
                        # 실제 값이 들어온 항목만 accent 로 살리고, 여전히
                        # 못 읽어온 항목은 muted "—" 로 중립 유지한다.
                        value = stats.get(key)
                        lbl.config(text=str(value) if value is not None else "—",
                                  fg=C["accent"] if value is not None else C["muted"])
                    self.refresh_tree.delete(*self.refresh_tree.get_children())
                    for rec in cands:
                        age   = rec.get("age_days", 0)
                        ref_c = rec.get("refresh_count", 0)
                        # "다시 쓰기 필요"/"대기 중"은 좁은 상태 열에서 잘려 보였다
                        # (디자이너 실측 — "다시 쓰기 필ㅇ") → 짧은 라벨로 교체.
                        stat  = "필요" if age >= 30 else "대기"
                        tag   = "need" if age >= 30 else "pending"
                        self.refresh_tree.insert("", "end", values=(
                            rec.get("clean_title", "")[:42],
                            rec.get("posted_at", "")[:10],
                            f"{age}일",
                            f"{ref_c}회",
                            stat,
                        ), tags=(tag,))
                    self.refresh_status.config(
                        text=f"  다시 쓰기 대상: {len(cands)}개",
                        fg=C["orange"] if cands else C["green"])
                    self._sync_refresh_empty()
                self._ui_after(0, _update)
            except Exception as e:
                log.debug("리프레시 대상 로딩 오류", exc_info=True)
                self._ui_after(0, lambda: self.refresh_status.config(
                    text=f"  {e}", fg=C["red"]))
        self.spawn(_task, name="content-load-refresh-candidates")

    def _run_refresh_selected(self):
        selected = self.refresh_tree.selection()
        if not selected:
            self._notify("다시 쓸 글을 표에서 선택해 주세요", "warn")
            return
        self._do_refresh(only_selected=True)

    def _run_refresh_batch(self):
        if not messagebox.askyesno("확인",
            "오래된 글 최대 5개를 다시 씁니다.\n"
            "Claude가 새 내용을 생성하고 네이버에 반영합니다. 진행할까요?"):
            return
        self._do_refresh(only_selected=False)

    def _run_refresh_dry(self):
        self._do_refresh(only_selected=False, dry_run=True)

    def _do_refresh(self, only_selected: bool = False, dry_run: bool = False):
        btn = self._refresh_sel_btn if only_selected else self._refresh_batch_btn
        def _task():
            try:
                from core.post_refresher import (
                    get_refresh_candidates, refresh_post, run_refresh_batch)

                self.app.set_status(
                    "테스트로 보는 중..." if dry_run else "♻️ 다시 쓰는 중...", C["yellow"])

                if only_selected:
                    sel_vals = [self.refresh_tree.item(iid, "values")
                                for iid in self.refresh_tree.selection()]
                    candidates = get_refresh_candidates(50)
                    sel_titles = {v[0][:42] for v in sel_vals if v}
                    to_process = [c for c in candidates
                                  if c.get("clean_title", "")[:42] in sel_titles]
                else:
                    to_process = None   # run_refresh_batch가 자체 선정

                if to_process is not None:
                    results = []
                    for rec in to_process:
                        r = refresh_post(rec, dry_run=dry_run)
                        results.append(r)
                    ok = sum(1 for r in results if r.get("success"))
                else:
                    res = run_refresh_batch(dry_run=dry_run)
                    ok = res.get("success", 0)

                label = "테스트로 보기" if dry_run else "다시 쓰기"
                self.app.set_status(f"{label} 완료", C["green"])
                self._ui_after(0, lambda: self._notify(f"{label}가 끝났어요 ({ok}편)", "ok"))
                self._ui_after(200, self._load_refresh_candidates)
            except Exception as e:
                log.debug("리프레시 실행 오류", exc_info=True)
                self.app.set_status(f"리프레시 오류: {e}", C["red"])
                self._ui_after(0, lambda: self.refresh_status.config(
                    text=f"  {e}", fg=C["red"]))
        run_async(btn, _task, "처리 중...")

    # ════════════════════════════════════════════════
    # 세그먼트: 시리즈
    # ════════════════════════════════════════════════
    def _build_series_segment(self, parent):
        sec = SectionCard(parent, "시리즈 글 자동 구성",
                          "연재형 콘텐츠는 알고리즘 우대와 재방문율에 도움이 돼요.",
                          icon=icons.glyph("content"))

        # 새 시리즈 생성 입력
        inp_row = tk.Frame(sec.body, bg=C["card"])
        inp_row.pack(fill="x", pady=(0, 10))
        L(inp_row, "시리즈 주제:", FB, C["muted"]).pack(side="left")
        self.series_kw_var = tk.StringVar()
        self.series_kw_entry = Ent(inp_row, self.series_kw_var, w=20)
        self.series_kw_entry.pack(side="left", padx=(8, 8), ipady=4)

        L(inp_row, "편수:", FB, C["muted"]).pack(side="left")
        self.series_ep_var = tk.StringVar(value="8")
        ep_spin = ttk.Spinbox(inp_row, from_=4, to=12, width=4,
                               textvariable=self.series_ep_var)
        ep_spin.pack(side="left", padx=(4, 10))
        PrimaryBtn(inp_row, "시리즈 계획 생성", self._create_series).pack(side="left")
        self.series_prog = L(sec.body, "", FS, C["yellow"])
        self.series_prog.pack(anchor="w", pady=(0, 8))

        # 시리즈 목록 표
        self._series_tree_wrap = tk.Frame(sec.body, bg=C["card"])
        s_cols = ("시리즈 제목", "카테고리", "총 편수", "발행 완료", "진행률")
        self.series_tree = DataTable(self._series_tree_wrap, columns=s_cols, show="headings",
                                      height=5)
        s_widths = [270, 70, 60, 70, 80]
        for col, w in zip(s_cols, s_widths):
            self.series_tree.heading(col, text=col)
            self.series_tree.column(col, width=_px(w), anchor="w")
        s_vsb = ttk.Scrollbar(self._series_tree_wrap, orient="vertical", command=self.series_tree.yview)
        self.series_tree.configure(yscrollcommand=s_vsb.set)
        s_vsb.pack(side="right", fill="y")
        self.series_tree.pack(fill="both", expand=True)
        self.series_tree.tag_configure("active",   foreground=C["green"])
        self.series_tree.tag_configure("complete", foreground=C["accent"])
        self.series_tree.tag_configure("empty",    foreground=C["muted"])

        self._series_empty = EmptyState(
            sec.body, icons.glyph("content"), "아직 만든 시리즈가 없어요",
            "주제를 입력하고 '시리즈 계획 생성'을 눌러 보세요.")
        self._sync_series_empty()

        # 에피소드 상세 표
        L(sec.body, "선택한 시리즈의 편 목록:", FS, C["muted"]).pack(anchor="w", pady=(12, 4))
        self._ep_tree_wrap = tk.Frame(sec.body, bg=C["card"])
        e_cols = ("편", "키워드", "발행일", "상태")
        self.ep_tree = DataTable(self._ep_tree_wrap, columns=e_cols, show="headings",
                                  height=5)
        e_widths = [40, 310, 90, 70]
        for col, w in zip(e_cols, e_widths):
            self.ep_tree.heading(col, text=col)
            self.ep_tree.column(col, width=_px(w), anchor="w")
        e_vsb = ttk.Scrollbar(self._ep_tree_wrap, orient="vertical", command=self.ep_tree.yview)
        self.ep_tree.configure(yscrollcommand=e_vsb.set)
        e_vsb.pack(side="right", fill="y")
        self.ep_tree.pack(fill="both", expand=True)
        self.ep_tree.tag_configure("published", foreground=C["green"])
        self.ep_tree.tag_configure("pending",   foreground=C["muted"])

        self._ep_empty = EmptyState(
            sec.body, icons.glyph("content"), "위에서 시리즈를 선택하면 편 목록이 여기 나와요")
        self._sync_ep_empty()

        self.series_tree.bind("<<TreeviewSelect>>", self._on_series_select)

        b_bot = tk.Frame(sec.body, bg=C["card"])
        b_bot.pack(fill="x", pady=(10, 0))
        SecondaryBtn(b_bot, "시리즈 전체 엑셀 추가", self._add_series_excel).pack(side="left")
        DangerBtn(b_bot, "시리즈 삭제", self._delete_series, icon="delete").pack(side="left", padx=8)
        self.series_status = L(b_bot, "", FS, C["muted"])
        self.series_status.pack(side="right", padx=4)
        return sec

    def _sync_series_empty(self) -> None:
        if len(self.series_tree.get_children()) == 0:
            self._series_tree_wrap.pack_forget()
            self._series_empty.show()
        else:
            self._series_empty.hide()
            self._series_tree_wrap.pack(fill="both", expand=True)

    def _sync_ep_empty(self) -> None:
        if len(self.ep_tree.get_children()) == 0:
            self._ep_tree_wrap.pack_forget()
            self._ep_empty.show()
        else:
            self._ep_empty.hide()
            self._ep_tree_wrap.pack(fill="both", expand=True)

    def _create_series(self):
        kw = self.series_kw_var.get().strip()
        if not kw:
            self._notify("시리즈 주제 키워드를 입력해 주세요", "warn")
            return
        try:
            ep_count = int(self.series_ep_var.get())
        except ValueError:
            ep_count = 8

        def _task():
            try:
                from core.series_planner import create_series_plan
                def prog(msg, cur, tot):
                    self._ui_after(0, lambda: self.series_prog.config(
                        text=f"  {msg}  ({cur}/{tot})"))
                self.app.set_status("시리즈 계획 생성 중...", C["yellow"])
                series = create_series_plan(kw, ep_count, progress_cb=prog)
                if series:
                    self._ui_after(100, self._load_series_list)
                    self._ui_after(100, lambda: self.series_prog.config(
                        text=f"  '{series['title']}' 생성 완료 ({len(series['episodes'])}편)",
                        fg=C["green"]))
                    self.app.set_status(
                        f"시리즈 생성: {series['title'][:30]}", C["green"])
                else:
                    self._ui_after(0, lambda: self.series_prog.config(
                        text="  시리즈 생성 실패", fg=C["red"]))
            except Exception as e:
                log.debug("시리즈 생성 오류", exc_info=True)
                self._ui_after(0, lambda: self.series_prog.config(
                    text=f"  {e}", fg=C["red"]))
        self.spawn(_task, name="content-create-series")

    def _load_series_list(self):
        try:
            from core.series_planner import get_all_series, get_series_progress
            self._series_list = get_all_series()
            self.series_tree.delete(*self.series_tree.get_children())
            for s in self._series_list:
                prog = get_series_progress(s["id"])
                pub  = prog.get("published", 0)
                tot  = prog.get("total", 0)
                pct  = prog.get("progress", 0)
                tag  = "complete" if pct == 100 else "active" if pub > 0 else "empty"
                self.series_tree.insert("", "end",
                    iid=s["id"],
                    values=(
                        s.get("title", "")[:38],
                        s.get("category", ""),
                        f"{tot}편",
                        f"{pub}편",
                        f"{pct}%",
                    ), tags=(tag,))
            self._sync_series_empty()
        except Exception as e:
            log.debug("시리즈 탭 오류", exc_info=True)
            self.series_status.config(text=f"  {e}", fg=C["red"])

    def _on_series_select(self, event):
        sel = self.series_tree.selection()
        if not sel:
            return
        sid = sel[0]
        try:
            from core.series_planner import _load_series_db
            db = _load_series_db()
            if sid not in db:
                return
            episodes = db[sid].get("episodes", [])
            self.ep_tree.delete(*self.ep_tree.get_children())
            for ep in episodes:
                status  = ep.get("status", "pending")
                tag     = "published" if status == "published" else "pending"
                kw_disp = ep.get("title") or ep.get("keyword", "")
                self.ep_tree.insert("", "end", values=(
                    f"{ep.get('ep',0)}편",
                    kw_disp[:44],
                    ep.get("posted_at", "")[:10],
                    "발행" if status == "published" else "대기",
                ), tags=(tag,))
            self._sync_ep_empty()
        except (ImportError, KeyError, TypeError) as e:
            # 시리즈 상세 로드 실패는 치명적이지 않지만 무반응은 금지 —
            # 로그를 남기고 사용자에게 실패를 조용히 표기한다.
            log.debug("시리즈 편 목록 로드 오류", exc_info=True)
            try:
                self.series_status.config(
                    text="  편 목록 로드 실패 (로그 확인)", fg=C["red"])
            except tk.TclError:
                pass    # 창이 이미 파괴된 경우 무해

    def _add_series_excel(self):
        sel = self.series_tree.selection()
        if not sel:
            self._notify("엑셀에 추가할 시리즈를 선택해 주세요", "warn")
            return
        sid = sel[0]
        def _task():
            try:
                from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_SHEET_NAME
                from core.series_planner import _load_series_db, add_series_to_excel
                db = _load_series_db()
                if sid not in db:
                    return
                added = add_series_to_excel(
                    db[sid], _active_excel_path(KEYWORD_EXCEL_PATH), KEYWORD_SHEET_NAME)
                msg = f"  {added}편 엑셀 추가 완료"
                self._ui_after(0, lambda: self.series_status.config(text=msg, fg=C["green"]))
                self._ui_after(0, lambda: self._notify(f"엑셀에 {added}편을 추가했어요", "ok"))
                self.app.set_status(f"시리즈 {added}편 엑셀에 추가됨", C["green"])
            except Exception as e:
                log.debug("시리즈 엑셀 추가 오류", exc_info=True)
                self._ui_after(0, lambda: self.series_status.config(
                    text=f"  {e}", fg=C["red"]))
        self.spawn(_task, name="content-add-series-excel")

    def _delete_series(self):
        sel = self.series_tree.selection()
        if not sel:
            self._notify("삭제할 시리즈를 선택해 주세요", "warn")
            return
        sid = sel[0]
        if not messagebox.askyesno("삭제 확인", "이 시리즈를 삭제할까요?\n발행된 글은 삭제되지 않습니다."):
            return
        try:
            from core.series_planner import delete_series
            delete_series(sid)
            self._load_series_list()
            self.ep_tree.delete(*self.ep_tree.get_children())
            self._sync_ep_empty()
            self.series_status.config(text="  삭제됨", fg=C["green"])
        except Exception as e:
            log.debug("시리즈 탭 오류", exc_info=True)
            self.series_status.config(text=f"  {e}", fg=C["red"])

    # ════════════════════════════════════════════════
    # 세그먼트: 템플릿
    # ════════════════════════════════════════════════
    def _build_template_segment(self, parent):
        sec = SectionCard(parent, "콘텐츠 템플릿",
                          "인트로·아웃로·CTA를 미리 정해 두면 브랜드 톤이 일관돼요.",
                          icon=icons.glyph("content"))

        # 템플릿 목록 표
        self._tpl_tree_wrap = tk.Frame(sec.body, bg=C["card"])
        t_cols = ("이름", "카테고리", "인트로 미리보기")
        self.tpl_tree = DataTable(self._tpl_tree_wrap, columns=t_cols, show="headings",
                                   height=5)
        t_widths = [160, 80, 360]
        for col, w in zip(t_cols, t_widths):
            self.tpl_tree.heading(col, text=col)
            self.tpl_tree.column(col, width=_px(w), anchor="w")
        t_vsb = ttk.Scrollbar(self._tpl_tree_wrap, orient="vertical", command=self.tpl_tree.yview)
        self.tpl_tree.configure(yscrollcommand=t_vsb.set)
        t_vsb.pack(side="right", fill="y")
        self.tpl_tree.pack(fill="both", expand=True)
        self.tpl_tree.bind("<<TreeviewSelect>>", self._on_template_select)

        self._tpl_empty = EmptyState(
            sec.body, icons.glyph("content"), "아직 만든 템플릿이 없어요",
            "아래에서 이름·인트로·아웃로를 채우고 '새 템플릿 저장'을 눌러 보세요.")
        self._sync_tpl_empty()

        # 입력 폼
        form = tk.Frame(sec.body, bg=C["card"])
        form.pack(fill="x", pady=(12, 0))

        # 이름 + 카테고리 (한 줄)
        row1 = tk.Frame(form, bg=C["card"])
        row1.pack(fill="x", pady=(0, 6))
        L(row1, "이름:", FB, C["muted"]).pack(side="left")
        self.tpl_name_var = tk.StringVar()
        Ent(row1, self.tpl_name_var, w=20).pack(side="left", padx=(8, 16), ipady=4)
        L(row1, "카테고리:", FB, C["muted"]).pack(side="left")
        self.tpl_cat_var = tk.StringVar(value="default")
        Ent(row1, self.tpl_cat_var, w=15).pack(side="left", padx=(8, 0), ipady=4)

        # 인트로 (Text)
        row2 = tk.Frame(form, bg=C["card"])
        row2.pack(fill="x", pady=(0, 6))
        L(row2, "인트로:", FB, C["muted"]).pack(anchor="nw")
        self.tpl_intro_text = tk.Text(
            row2, bg=C["card"], fg=C["text"], insertbackground=C["text"],
            height=3, width=50, font=FS, relief="solid", bd=1,
            highlightbackground=C["border"], highlightthickness=1)
        self.tpl_intro_text.pack(fill="x", pady=(4, 0))

        # 아웃로 (Text)
        row3 = tk.Frame(form, bg=C["card"])
        row3.pack(fill="x", pady=(0, 6))
        L(row3, "아웃로:", FB, C["muted"]).pack(anchor="nw")
        self.tpl_outro_text = tk.Text(
            row3, bg=C["card"], fg=C["text"], insertbackground=C["text"],
            height=3, width=50, font=FS, relief="solid", bd=1,
            highlightbackground=C["border"], highlightthickness=1)
        self.tpl_outro_text.pack(fill="x", pady=(4, 0))

        # CTA (Entry)
        row4 = tk.Frame(form, bg=C["card"])
        row4.pack(fill="x", pady=(0, 6))
        L(row4, "CTA:", FB, C["muted"]).pack(side="left")
        self.tpl_cta_var = tk.StringVar()
        Ent(row4, self.tpl_cta_var, w=50).pack(side="left", padx=(8, 0), ipady=4)

        # 버튼 행
        c_bot = tk.Frame(sec.body, bg=C["card"])
        c_bot.pack(fill="x", pady=(10, 0))
        PrimaryBtn(c_bot, "새 템플릿 저장", self._save_template).pack(side="left")
        SecondaryBtn(c_bot, "수정", self._update_template).pack(side="left", padx=8)
        DangerBtn(c_bot, "삭제", self._delete_template, icon="delete").pack(side="left")
        GhostBtn(c_bot, "초기화", self._clear_template_form, 10).pack(side="left", padx=8)
        self.tpl_status = L(c_bot, "", FS, C["muted"])
        self.tpl_status.pack(side="right", padx=4)
        return sec

    def _sync_tpl_empty(self) -> None:
        if len(self.tpl_tree.get_children()) == 0:
            self._tpl_tree_wrap.pack_forget()
            self._tpl_empty.show()
        else:
            self._tpl_empty.hide()
            self._tpl_tree_wrap.pack(fill="both", expand=True)

    def _load_templates(self):
        """templates/ 디렉토리에서 템플릿을 로드하여 트리뷰를 갱신합니다."""
        try:
            templates = load_templates()
            self.tpl_tree.delete(*self.tpl_tree.get_children())
            for tpl in templates:
                intro_preview = tpl.get("intro", "")[:60]
                if len(tpl.get("intro", "")) > 60:
                    intro_preview += "..."
                self.tpl_tree.insert("", "end", values=(
                    tpl.get("name", ""),
                    tpl.get("category", ""),
                    intro_preview,
                ))
            self.tpl_status.config(
                text=f"  {len(templates)}개 템플릿 로드됨", fg=C["muted"])
            self._sync_tpl_empty()
        except (OSError, ValueError) as exc:
            log.debug("템플릿 로딩 오류", exc_info=True)
            self.tpl_status.config(text=f"  {exc}", fg=C["red"])

    def _on_template_select(self, event):
        """트리뷰에서 템플릿 선택 시 입력 폼에 데이터를 로드합니다."""
        sel = self.tpl_tree.selection()
        if not sel:
            return
        values = self.tpl_tree.item(sel[0], "values")
        if not values:
            return

        name = values[0]
        # 전체 데이터를 load_templates()에서 다시 가져옴 (미리보기는 잘려 있으므로)
        templates = load_templates()
        matched = None
        for tpl in templates:
            if tpl.get("name") == name:
                matched = tpl
                break

        if not matched:
            return

        self.tpl_name_var.set(matched.get("name", ""))
        self.tpl_cat_var.set(matched.get("category", "default"))
        self.tpl_cta_var.set(matched.get("cta", ""))

        self.tpl_intro_text.delete("1.0", "end")
        self.tpl_intro_text.insert("1.0", matched.get("intro", ""))

        self.tpl_outro_text.delete("1.0", "end")
        self.tpl_outro_text.insert("1.0", matched.get("outro", ""))

    def _gather_template_form(self) -> dict:
        """입력 폼에서 템플릿 데이터를 수집합니다."""
        return {
            "name": self.tpl_name_var.get().strip(),
            "category": self.tpl_cat_var.get().strip() or "default",
            "intro": self.tpl_intro_text.get("1.0", "end-1c").strip(),
            "outro": self.tpl_outro_text.get("1.0", "end-1c").strip(),
            "cta": self.tpl_cta_var.get().strip(),
        }

    def _save_template(self):
        """입력 폼의 데이터로 새 템플릿을 저장합니다."""
        data = self._gather_template_form()
        if not data["name"]:
            self._notify("템플릿 이름을 입력해 주세요", "warn")
            return

        # 동명 템플릿 존재 시 덮어쓰기 확인(기존 템플릿을 지우는 파괴적 동작 — 유지)
        existing = load_templates()
        if any(t.get("name") == data["name"] for t in existing):
            if not messagebox.askyesno("덮어쓰기",
                    f"'{data['name']}' 템플릿이 이미 있습니다.\n덮어쓰시겠습니까?"):
                return

        try:
            save_template(data)
            self._load_templates()
            self.tpl_status.config(
                text=f"  '{data['name']}' 저장됨", fg=C["green"])
            self._notify(f"'{data['name']}' 템플릿을 저장했어요", "ok")
        except (ValueError, OSError) as exc:
            log.debug("템플릿 저장 오류", exc_info=True)
            self.tpl_status.config(text=f"  {exc}", fg=C["red"])

    def _update_template(self):
        """선택된 템플릿을 수정합니다."""
        sel = self.tpl_tree.selection()
        if not sel:
            self._notify("수정할 템플릿을 선택해 주세요", "warn")
            return

        data = self._gather_template_form()
        if not data["name"]:
            self._notify("템플릿 이름을 입력해 주세요", "warn")
            return

        # 기존 이름이 변경된 경우 이전 파일 삭제
        old_name = self.tpl_tree.item(sel[0], "values")[0]
        if old_name and old_name != data["name"]:
            delete_template(old_name)

        try:
            save_template(data)
            self._load_templates()
            self.tpl_status.config(
                text=f"  '{data['name']}' 수정됨", fg=C["green"])
            self._notify(f"'{data['name']}' 템플릿을 수정했어요", "ok")
        except (ValueError, OSError) as exc:
            log.debug("템플릿 수정 오류", exc_info=True)
            self.tpl_status.config(text=f"  {exc}", fg=C["red"])

    def _delete_template(self):
        """선택된 템플릿을 삭제합니다."""
        sel = self.tpl_tree.selection()
        if not sel:
            self._notify("삭제할 템플릿을 선택해 주세요", "warn")
            return
        name = self.tpl_tree.item(sel[0], "values")[0]
        if not messagebox.askyesno("삭제 확인",
                f"'{name}' 템플릿을 삭제할까요?"):
            return

        if delete_template(name):
            self._load_templates()
            self._clear_template_form()
            self.tpl_status.config(text=f"  '{name}' 삭제됨", fg=C["green"])
        else:
            self.tpl_status.config(text="  삭제 실패", fg=C["red"])

    def _clear_template_form(self):
        """템플릿 입력 폼을 초기화합니다."""
        self.tpl_name_var.set("")
        self.tpl_cat_var.set("default")
        self.tpl_cta_var.set("")
        self.tpl_intro_text.delete("1.0", "end")
        self.tpl_outro_text.delete("1.0", "end")
        # 트리뷰 선택 해제
        for item in self.tpl_tree.selection():
            self.tpl_tree.selection_remove(item)
