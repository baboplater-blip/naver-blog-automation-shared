# gui/tabs/keyword.py — 키워드 확장 & 마감 감지 탭
import tkinter as tk
from tkinter import ttk, messagebox
import json
import logging

log = logging.getLogger(__name__)

from gui.theme import (C, SP, FB, FS, L, Ent, bind_mousewheel,
                       PrimaryBtn, SecondaryBtn, run_async)
from gui.widgets import Header, SectionCard, EmptyState, ProgressRow
from gui.data_table import DataTable
from gui.async_utils import UiLifecycle
from gui import icons
from gui import theme as _theme


def _px(n):
    """`theme.px(n)` 가 있으면 그걸(고DPI 스케일 반영), 없으면 그대로 —
    auto-dev 9호, content.py/growth.py 의 동일 헬퍼와 같은 방어적 폴백 패턴."""
    scale_fn = getattr(_theme, "px", None)
    return scale_fn(n) if scale_fn else n


def _active_excel_path(fallback: str) -> str:
    """활성 프로필의 키워드 엑셀 경로. brand.excel_path() 우선, 빈값이면 fallback.

    default(examplebrand) 프로필은 brand.excel_path() 가 KEYWORD_EXCEL_PATH 를 그대로
    돌려주므로 기존 경로와 동일(무변화). named 프로필은 자기 keywords/*.xlsx 를 쓴다.
    """
    try:
        from config import brand as _brand
        return _brand.excel_path() or fallback
    except Exception:  # noqa: BLE001 — 브랜드 헬퍼 부재 시 레거시 경로
        return fallback


class KeywordTab(UiLifecycle, tk.Frame):
    """롱테일 파생 + 마감 임박 감지 통합 탭"""
    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self.app = app
        self._expanded_results = {}   # {base_kw: [expanded,...]}
        self._deadline_results = []
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
        # ── 헤더 ────────────────────────────────────────
        header = Header(self, "키워드 확장", "새 키워드를 발굴하고 마감 임박 제도를 확인해요.",
                        icon=icons.glyph("keyword"))
        header.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["sm"]))

        # ── 스크롤 영역 ──────────────────────────────────
        canvas = tk.Canvas(self, bg=C["bg"], bd=0, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        sf = tk.Frame(canvas, bg=C["bg"])
        sf.bind("<Configure>",
                lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=sf, anchor="nw")
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        bind_mousewheel(canvas)

        # ════════════════════════════════════════════════
        # 섹션 A: 롱테일 키워드 파생
        # ════════════════════════════════════════════════
        sec_a = SectionCard(sf, "롱테일 키워드 자동 파생",
                            "메인 키워드 1개로 롱테일 10개를 자동 생성해요 (Claude + 네이버 자동완성).",
                            icon=icons.glyph("keyword"))
        sec_a.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], 0))

        # 입력행
        inp_row = tk.Frame(sec_a.body, bg=C["card"])
        inp_row.pack(fill="x", pady=(0, 10))
        L(inp_row, "메인 키워드:", FB, C["muted"]).pack(side="left")
        self.kw_var = tk.StringVar()
        self.kw_entry = Ent(inp_row, self.kw_var, w=22)
        self.kw_entry.pack(side="left", padx=(8, 8), ipady=4)
        self._expand_btn = PrimaryBtn(inp_row, "파생 생성", self._run_expand)
        self._expand_btn.pack(side="left")
        self._batch_btn = SecondaryBtn(inp_row, "일괄 파생", self._run_batch_expand)
        self._batch_btn.pack(side="left", padx=6)

        # 진행 표시 — 아무 작업도 안 한 초기 상태에서 빈 진행률 상자만 보이면
        # "뭔가 멈춘 것처럼" 보인다(디자이너 지적) → 실제 작업을 시작할 때만 보여준다.
        self.expand_prog = ProgressRow(sec_a.body)

        # 결과 표
        self._kw_tree_wrap = tk.Frame(sec_a.body, bg=C["card"])
        kw_cols = ("파생 키워드", "검색의도", "경쟁도", "출처", "엑셀추가")
        self.kw_tree = DataTable(self._kw_tree_wrap, columns=kw_cols, show="headings",
                                  height=8)
        col_widths = [310, 80, 60, 80, 70]
        for col, w in zip(kw_cols, col_widths):
            self.kw_tree.heading(col, text=col)
            self.kw_tree.column(col, width=_px(w), anchor="w")
        kw_vsb = ttk.Scrollbar(self._kw_tree_wrap, orient="vertical", command=self.kw_tree.yview)
        self.kw_tree.configure(yscrollcommand=kw_vsb.set)
        kw_vsb.pack(side="right", fill="y")
        self.kw_tree.pack(fill="both", expand=True)

        # 태그 색상
        self.kw_tree.tag_configure("low",  foreground=C["green"])
        self.kw_tree.tag_configure("mid",  foreground=C["yellow"])
        self.kw_tree.tag_configure("high", foreground=C["orange"])
        self.kw_tree.tag_configure("ac",   foreground=C["accent"])

        self._kw_empty = EmptyState(
            sec_a.body, icons.glyph("keyword"), "아직 파생 결과가 없어요",
            "메인 키워드를 입력하고 '파생 생성'을 눌러 보세요.")
        self._sync_kw_empty()

        # 하단 버튼
        a_bot = tk.Frame(sec_a.body, bg=C["card"])
        a_bot.pack(fill="x", pady=(10, 0))
        SecondaryBtn(a_bot, "선택 항목 추가", self._add_selected_to_excel).pack(side="left")
        SecondaryBtn(a_bot, "전체 엑셀 추가", self._add_all_to_excel).pack(side="left", padx=8)
        self.kw_status = L(a_bot, "", FS, C["muted"])
        self.kw_status.pack(side="right", padx=4)

        # ════════════════════════════════════════════════
        # 섹션 B: 마감 임박 감지
        # ════════════════════════════════════════════════
        sec_b = SectionCard(sf, "신청 마감 임박 감지",
                            "마감 14일 이내 글은 엑셀 최상단에 자동 배치돼요 · 3일 이내는 긴급으로 표시돼요.",
                            icon=icons.glyph("warning"))
        sec_b.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["xl"]))

        # 범례
        legend = tk.Frame(sec_b.body, bg=C["card"])
        legend.pack(fill="x", pady=(0, 10))
        for color, label in [(C["red"], "D-3 이내 긴급"),
                              (C["orange"], "D-14 이내 주의"),
                              (C["muted"], "정상")]:
            tk.Label(legend, text=label, font=FS, fg=color,
                     bg=C["card"]).pack(side="left", padx=(0, 16))

        # 결과 표
        self._dl_tree_wrap = tk.Frame(sec_b.body, bg=C["card"])
        dl_cols = ("공고명", "마감일", "D-DAY", "긴급도", "키워드")
        self.dl_tree = DataTable(self._dl_tree_wrap, columns=dl_cols, show="headings",
                                  height=7)
        dl_widths = [240, 90, 60, 60, 160]
        for col, w in zip(dl_cols, dl_widths):
            self.dl_tree.heading(col, text=col)
            self.dl_tree.column(col, width=_px(w), anchor="w")
        dl_vsb = ttk.Scrollbar(self._dl_tree_wrap, orient="vertical", command=self.dl_tree.yview)
        self.dl_tree.configure(yscrollcommand=dl_vsb.set)
        dl_vsb.pack(side="right", fill="y")
        self.dl_tree.pack(fill="both", expand=True)

        self.dl_tree.tag_configure("urgent",  foreground=C["red"])
        self.dl_tree.tag_configure("warning", foreground=C["orange"])
        self.dl_tree.tag_configure("normal",  foreground=C["muted"])

        self._dl_empty = EmptyState(
            sec_b.body, icons.glyph("warning"), "아직 마감 목록이 없어요",
            "'지금 마감 스캔'을 눌러 신청 마감이 임박한 제도를 확인해 보세요.",
            action_text="지금 마감 스캔", action_cmd=lambda: self._run_deadline_scan())
        self._sync_dl_empty()

        b_bot = tk.Frame(sec_b.body, bg=C["card"])
        b_bot.pack(fill="x", pady=(10, 0))
        self._deadline_btn = SecondaryBtn(b_bot, "지금 마감 스캔", self._run_deadline_scan)
        self._deadline_btn.pack(side="left")
        SecondaryBtn(b_bot, "임박건 엑셀 삽입", self._inject_deadlines).pack(side="left", padx=8)
        self.dl_status = L(b_bot, "", FS, C["muted"])
        self.dl_status.pack(side="right", padx=4)

        # 저장된 데이터 로드
        self._load_cached_deadlines()

    # ── 빈 상태 토글 ──────────────────────────────────
    def _sync_kw_empty(self) -> None:
        if len(self.kw_tree.get_children()) == 0:
            self._kw_tree_wrap.pack_forget()
            self._kw_empty.show()
        else:
            self._kw_empty.hide()
            self._kw_tree_wrap.pack(fill="both", expand=True)

    def _sync_dl_empty(self) -> None:
        if len(self.dl_tree.get_children()) == 0:
            self._dl_tree_wrap.pack_forget()
            self._dl_empty.show()
        else:
            self._dl_empty.hide()
            self._dl_tree_wrap.pack(fill="both", expand=True)

    # ── 진행 표시 show/hide ────────────────────────────
    def _show_expand_prog(self) -> None:
        self.expand_prog.pack(fill="x", pady=(0, 8))

    def _hide_expand_prog(self) -> None:
        self.expand_prog.pack_forget()

    # ── 롱테일 파생 ──────────────────────────────────────
    def _run_expand(self):
        kw = self.kw_var.get().strip()
        if not kw:
            self._notify("메인 키워드를 입력해 주세요", "warn")
            return
        self._show_expand_prog()
        def _task():
            try:
                from core.keyword_expander import expand_keyword
                def prog(msg, cur, tot):
                    self._ui_after(0, lambda: self.expand_prog.set(cur, tot, msg))
                self.app.set_status("키워드 파생 중...", C["yellow"])
                results = expand_keyword(kw, force=True, progress_cb=prog)
                self._expanded_results[kw] = results
                self._ui_after(100, lambda: self._refresh_kw_tree(kw, results))
                self._ui_after(100, lambda: self.expand_prog.set(
                    len(results), len(results), f"{len(results)}개 파생 완료"))
                self.app.set_status(f"'{kw}' 파생 완료 ({len(results)}개)", C["green"])
            except Exception as e:
                log.debug("키워드 파생 오류", exc_info=True)
                self._ui_after(0, self._hide_expand_prog)
                self._ui_after(0, lambda: self._notify(f"파생 중 오류가 났어요: {e}", "error"))
                self.app.set_status(f"파생 오류: {e}", C["red"])
        run_async(self._expand_btn, _task, "파생 중...")

    def _run_batch_expand(self):
        self._show_expand_prog()
        def _task():
            try:
                from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_SHEET_NAME
                from core.keyword_expander import batch_expand_from_excel
                excel_path = _active_excel_path(KEYWORD_EXCEL_PATH)
                def prog(msg, cur, tot):
                    self._ui_after(0, lambda: self.expand_prog.set(cur, tot, msg))
                self.app.set_status("일괄 파생 중...", C["yellow"])
                r = batch_expand_from_excel(
                    excel_path, KEYWORD_SHEET_NAME, progress_cb=prog)
                self._expanded_results.update(r.get("results", {}))
                # 트리 갱신 — 일괄 결과 전체를 한 번에 표시 (단일 결과로 덮어쓰기 방지)
                results_dict = r.get("results", {})
                self._ui_after(100, lambda d=results_dict: self._refresh_kw_tree_batch(d))
                self._ui_after(100, lambda: self.expand_prog.set(
                    r["total_expanded"], r["total_expanded"],
                    f"{r['processed']}개 키워드 → {r['total_expanded']}개 롱테일"))
                self.app.set_status(f"일괄 파생 완료 (총 {r['total_expanded']}개)", C["green"])
            except Exception as e:
                log.debug("일괄 파생 오류", exc_info=True)
                self._ui_after(0, self._hide_expand_prog)
                self._ui_after(0, lambda: self._notify(f"일괄 파생 중 오류가 났어요: {e}", "error"))
        run_async(self._batch_btn, _task, "일괄 파생 중...")

    _INTENT_LABELS = {"info": "정보형", "how": "신청방법",
                       "check": "자격확인", "compare": "비교형"}
    _DIFF_LABELS   = {"low": "낮음", "mid": "중간", "high": "높음"}

    def _insert_kw_row(self, item: dict, prefix: str = ""):
        """파생 키워드 한 행을 트리뷰에 삽입합니다."""
        kw     = item.get("keyword", "")
        intent = self._INTENT_LABELS.get(item.get("intent", ""), "정보형")
        diff   = item.get("difficulty", "mid")
        source = "자동완성" if item.get("source") == "autocomplete" else "Claude"
        added  = "" if item.get("added_to_excel") else "—"
        display_kw = f"{prefix}{kw}" if prefix else kw
        self.kw_tree.insert(
            "", "end",
            values=(display_kw, intent,
                    self._DIFF_LABELS.get(diff, diff), source, added),
            tags=(diff,))

    def _refresh_kw_tree(self, base_kw: str, results: list):
        self.kw_tree.delete(*self.kw_tree.get_children())
        for item in results:
            self._insert_kw_row(item)
        self.kw_status.config(text=f"  '{base_kw}' — {len(results)}개 파생")
        self._sync_kw_empty()

    def _refresh_kw_tree_batch(self, results_dict: dict):
        """일괄 파생 결과 전체를 한 번에 트리뷰에 표시합니다.

        같은 메인 키워드의 롱테일들이 묶여 보이도록 그룹 헤더 스타일로
        들여쓰기하여 출력합니다.
        """
        self.kw_tree.delete(*self.kw_tree.get_children())
        total = 0
        for base_kw, results in results_dict.items():
            if not results:
                continue
            # 그룹 헤더 행 (메인 키워드 표시)
            self.kw_tree.insert(
                "", "end",
                values=(f"▼ {base_kw}  ({len(results)}개)",
                        "", "", "", ""),
                tags=("ac",))
            for item in results:
                self._insert_kw_row(item, prefix="    ")
                total += 1
        self.kw_status.config(
            text=f"  일괄 파생 {len(results_dict)}개 키워드 · {total}개 롱테일")
        self._sync_kw_empty()

    def _count_all_expandable(self) -> int:
        """'전체 엑셀 추가'가 실제로 추가할 항목 수(확인 문구에 쓴다)."""
        return sum(len(results) for results in self._expanded_results.values())

    def _add_selected_to_excel(self):
        selected = self.kw_tree.selection()
        if not selected:
            self._notify("추가할 키워드를 표에서 선택해 주세요 (클릭 또는 Ctrl+클릭)", "warn")
            return
        self._do_add_to_excel(selected_only=True)

    def _add_all_to_excel(self):
        n = self._count_all_expandable()
        if n <= 0:
            self._notify("먼저 키워드를 파생해 주세요", "warn")
            return
        if not messagebox.askyesno(
                "전체 엑셀 추가",
                f"엑셀에 {n}개를 추가해요. 되돌리려면 엑셀에서 직접 지워야 해요.\n\n계속할까요?"):
            return
        self._do_add_to_excel(selected_only=False)

    def _do_add_to_excel(self, selected_only: bool):
        def _task():
            try:
                from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_SHEET_NAME
                try:
                    from config.settings import KEYWORD_LONGTAIL_SHEET
                except ImportError:
                    KEYWORD_LONGTAIL_SHEET = None
                from core.keyword_expander import add_to_excel

                # tree iid → 메인 키워드(parent) 역매핑 (일괄 모드 그룹 헤더에서 추정)
                # tree는 그룹 헤더 행 "▼ {base_kw}  (N개)" 다음에 들여쓴 자식 행들이 온다.
                iid_to_parent: dict = {}
                current_parent = ""
                for iid in self.kw_tree.get_children(""):
                    vals = self.kw_tree.item(iid, "values")
                    if not vals:
                        continue
                    cell = (vals[0] or "")
                    stripped = cell.strip()
                    if stripped.startswith("▼"):
                        # 그룹 헤더 — 'N개' suffix 제거
                        head = stripped.lstrip("▼").strip()
                        # "{base_kw}  (12개)" → base_kw
                        if "(" in head:
                            head = head.rsplit("(", 1)[0].strip()
                        current_parent = head
                    else:
                        iid_to_parent[iid] = current_parent

                if selected_only:
                    sel_iids = self.kw_tree.selection()
                    items = []
                    for iid in sel_iids:
                        vals = self.kw_tree.item(iid, "values")
                        if not vals:
                            continue
                        kw_str = (vals[0] or "").strip()
                        # 일괄 모드 그룹 헤더 행("▼ ...") 스킵
                        if not kw_str or kw_str.startswith("▼"):
                            continue
                        parent = iid_to_parent.get(iid, "")
                        items.append({"keyword": kw_str, "parent_keyword": parent})
                else:
                    items = []
                    for base_kw, results in self._expanded_results.items():
                        for r in results:
                            entry = dict(r)
                            entry.setdefault("parent_keyword", base_kw)
                            items.append(entry)

                if not items:
                    self._ui_after(0, lambda: self._notify("추가할 키워드가 없어요", "warn"))
                    return

                added = add_to_excel(
                    items, _active_excel_path(KEYWORD_EXCEL_PATH), KEYWORD_SHEET_NAME,
                    longtail_sheet=KEYWORD_LONGTAIL_SHEET,
                )
                msg = f"  {added}개 엑셀 추가 완료"
                self._ui_after(0, lambda: self.kw_status.config(text=msg, fg=C["green"]))
                self._ui_after(0, lambda: self._notify(f"엑셀에 {added}개를 추가했어요", "ok"))
                self.app.set_status(f"엑셀에 {added}개 키워드 추가", C["green"])
            except Exception as e:
                log.debug("엑셀 추가 오류", exc_info=True)
                self._ui_after(0, lambda: self.kw_status.config(
                    text=f"  {e}", fg=C["red"]))
                self._ui_after(0, lambda: self._notify(f"엑셀 추가 중 오류가 났어요: {e}", "error"))
        self.spawn(_task, name="keyword-add-to-excel")

    # ── 마감 임박 감지 ───────────────────────────────────
    def _run_deadline_scan(self):
        def _task():
            try:
                from core.deadline_tracker import fetch_deadlines
                def prog(msg, cur, tot):
                    self._ui_after(0, lambda: self.dl_status.config(
                        text=f"  {msg}", fg=C["yellow"]))
                self.app.set_status("마감 스캔 중...", C["yellow"])
                results = fetch_deadlines(progress_cb=prog)
                self._deadline_results = results
                self._ui_after(100, lambda: self._refresh_dl_tree(results))
                urgent  = sum(1 for x in results if x.get("urgency") == "urgent")
                warning = sum(1 for x in results if x.get("urgency") == "warning")
                msg = f"  총 {len(results)}건 | 긴급 {urgent}건  주의 {warning}건"
                self._ui_after(100, lambda: self.dl_status.config(text=msg, fg=C["text"]))
                self.app.set_status(f"마감 스캔 완료 (긴급 {urgent}건)", C["green"])
            except Exception as e:
                log.debug("마감 스캔 오류", exc_info=True)
                self._ui_after(0, lambda: self.dl_status.config(
                    text=f"  오류: {e}", fg=C["red"]))
                self.app.set_status(f"스캔 오류: {e}", C["red"])
        run_async(self._deadline_btn, _task, "스캔 중...")

    def _refresh_dl_tree(self, results: list):
        self.dl_tree.delete(*self.dl_tree.get_children())
        icon = {"urgent": "", "warning": "", "normal": ""}
        for item in results[:50]:
            urg = item.get("urgency", "normal")
            d   = item.get("days_left", 0)
            self.dl_tree.insert("", "end",
                values=(
                    item.get("title", "")[:38],
                    item.get("deadline", ""),
                    f"D-{d}" if d > 0 else "오늘 마감!",
                    icon.get(urg, ""),
                    item.get("keyword", "")[:22],
                ),
                tags=(urg,))
        self._sync_dl_empty()

    def _inject_deadlines(self):
        if not self._deadline_results:
            self._notify("먼저 '지금 마감 스캔'을 실행해 주세요", "warn")
            return
        def _task():
            try:
                from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_PUBLISH_POOL_SHEET
                from core.deadline_tracker import inject_urgent_to_excel
                inserted = inject_urgent_to_excel(
                    _active_excel_path(KEYWORD_EXCEL_PATH), KEYWORD_PUBLISH_POOL_SHEET,
                    self._deadline_results)
                msg = f"  {inserted}개 엑셀 최상단에 삽입 완료"
                self._ui_after(0, lambda: self.dl_status.config(text=msg, fg=C["green"]))
                self._ui_after(0, lambda: self._notify(f"마감 임박 {inserted}개를 엑셀에 배치했어요", "ok"))
                self.app.set_status(f"마감 임박 {inserted}개 엑셀 배치 완료", C["green"])
            except Exception as e:
                log.debug("마감 엑셀 배치 오류", exc_info=True)
                self._ui_after(0, lambda: self.dl_status.config(
                    text=f"  {e}", fg=C["red"]))
                self._ui_after(0, lambda: self._notify(f"엑셀 배치 중 오류가 났어요: {e}", "error"))
        self.spawn(_task, name="keyword-inject-deadlines")

    def _load_cached_deadlines(self):
        try:
            from core.deadline_tracker import get_deadline_summary
            summary = get_deadline_summary()
            all_items = summary.get("urgent", []) + summary.get("warning", [])
            if all_items:
                self._deadline_results = all_items
                self._refresh_dl_tree(all_items)
                u = len(summary.get("urgent", []))
                w = len(summary.get("warning", []))
                self.dl_status.config(
                    text=f"  캐시 로드됨 — 긴급 {u}건  주의 {w}건",
                    fg=C["muted"])
        except (ImportError, json.JSONDecodeError, FileNotFoundError, KeyError) as e:
            # 캐시 부재/손상은 치명적이지 않지만(스캔으로 재생성 가능) 무반응은 금지 —
            # 로그로 남기고 사용자에게 "스캔 필요"를 조용히 안내한다.
            log.debug("마감 캐시 로드 오류", exc_info=True)
            try:
                self.dl_status.config(
                    text="  캐시 없음 — '지금 마감 스캔'을 실행하세요", fg=C["muted"])
            except tk.TclError:
                pass    # 창이 이미 파괴된 경우 무해
