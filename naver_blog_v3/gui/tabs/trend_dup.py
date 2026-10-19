# gui/tabs/trend_dup.py — 트렌드 & 중복 검사 탭
import tkinter as tk
from tkinter import ttk
import logging

log = logging.getLogger(__name__)

from gui.theme import C, SP, FB, FS, FM, L, PrimaryBtn, run_async
from gui.widgets import Header, SectionCard, EmptyState, Banner, ScrollArea, DataTable
from gui.async_utils import UiLifecycle
from gui import icons
from gui import theme as _theme


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


class TrendDupTab(UiLifecycle, tk.Frame):
    def __init__(self, parent, app):
        tk.Frame.__init__(self, parent, bg=C["bg"])
        self.app = app
        self._init_lifecycle()
        self._build()

    def _ui_after(self, delay_ms, callback):
        """워커 스레드에서 UI 콜백을 메인 루프에 안전하게 예약한다(UiLifecycle 위임)."""
        self.ui_after(callback, delay=delay_ms)

    def _build(self):
        header = Header(self, "트렌드·중복", "뜨는 키워드와 중복 글을 확인해요.",
                        icon=icons.glyph("trend"))
        header.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["sm"]))

        # 세로 스크롤이 없으면 900×620 처럼 좁은 창에서 아래쪽 섹션(중복 검사)이
        # 그냥 창 밖으로 잘려 나간다(디자이너 실측) — ScrollArea 로 감싼다.
        scroll = ScrollArea(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.inner

        # Today-topic collection is an explicit, profile-scoped action. Keep it
        # above the existing trend and duplicate sections without replacing them.
        from gui.tabs.news_ideas import NewsIdeasPanel
        self.news_ideas = NewsIdeasPanel(body, self.app)
        self.news_ideas.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["lg"]))

        # ── 검색량 TOP 키워드 ────────────────────────────
        trend_card = SectionCard(body, "검색량 TOP 키워드", "최근 3개월 기준이에요.",
                                 icon=icons.glyph("trend"))
        trend_card.pack(fill="x", padx=SP["xl"], pady=(SP["lg"], SP["lg"]))

        self._api_banner = Banner(
            trend_card.body,
            "네이버 검색광고 API 키가 있으면 실제 검색량으로 갱신할 수 있어요.",
            kind="info", action_text="설정 열기",
            action_cmd=lambda: getattr(self.app, "show_tab", lambda k: None)("settings"))
        self._api_banner.pack(fill="x", pady=(0, SP["sm"]))

        self._trend_wrap = tk.Frame(trend_card.body, bg=C["card"])
        self.trend_frame = tk.Frame(self._trend_wrap, bg=C["card"])
        self.trend_frame.pack(fill="x")

        self._trend_empty = EmptyState(
            trend_card.body, icons.glyph("trend"), "아직 트렌드 데이터가 없어요",
            "'지금 트렌드 갱신'을 눌러 검색량을 가져와 보세요.")

        btn_row = tk.Frame(trend_card.body, bg=C["card"])
        btn_row.pack(fill="x", pady=(SP["sm"], 0))
        self._trend_btn = PrimaryBtn(btn_row, "지금 트렌드 갱신", self._update_trend)
        self._trend_btn.pack(side="left")

        # ── 중복 포스팅 검사 ──────────────────────────────
        dup_card = SectionCard(body, "중복 포스팅 검사", "기존 글과 얼마나 겹치는지 확인해요.",
                               icon=icons.glyph("content"))
        dup_card.pack(fill="both", expand=True, padx=SP["xl"], pady=(0, SP["lg"]))

        top_row = tk.Frame(dup_card.body, bg=C["card"])
        top_row.pack(fill="x", pady=(0, SP["sm"]))
        self._scan_btn = PrimaryBtn(top_row, "엑셀 전체 스캔", self._scan_dup)
        self._scan_btn.pack(side="right")

        self._dup_wrap = tk.Frame(dup_card.body, bg=C["card"])
        cols = ("중복 의심 제목", "유사 기존 글", "유사도")
        self.dup_tree = DataTable(self._dup_wrap, columns=cols, show="headings",
                                   height=8)
        for col, w in zip(cols, [280, 280, 70]):
            self.dup_tree.heading(col, text=col)
            self.dup_tree.column(col, width=_px(w), anchor="w")
        vsb = ttk.Scrollbar(self._dup_wrap, orient="vertical", command=self.dup_tree.yview)
        self.dup_tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.dup_tree.pack(fill="both", expand=True)
        self._wire_dup_tag_colors()

        self._dup_empty = EmptyState(
            dup_card.body, icons.glyph("content"), "아직 스캔하지 않았어요",
            "'엑셀 전체 스캔'을 눌러 중복 의심 글을 확인해 보세요.",
            action_text="엑셀 전체 스캔", action_cmd=self._scan_dup)

        self.dup_status = L(self, "", FS, C["muted"])
        self.dup_status.pack(anchor="w", padx=SP["xl"], pady=(0, SP["md"]))

        self._load_trend_display()
        self._sync_dup_empty()

    def _wire_dup_tag_colors(self) -> None:
        """`dup` 태그 전경색을 지금 칠하고, 팔레트 전환마다 다시 칠하도록
        `DataTable._on_retheme` 훅에 걸어 둔다 — `tag_configure()` 가
        저장하는 색은 호출 시점 리터럴 hex(``gui/data_table.py``, 공용
        모듈이라 여기서 손대지 않는다)라 한 번만 칠하면 라이트 전환 후에도
        다크 시절 값이 굳는다(channel.py `_wire_table_tag_colors` 와 동일
        패턴 — 자세한 이유는 그쪽 docstring 참고)."""
        def _paint() -> None:
            self.dup_tree.tag_configure("dup", foreground=C["red"])

        base_on_retheme = self.dup_tree._on_retheme

        def _hook() -> None:
            _paint()
            base_on_retheme()

        self.dup_tree._on_retheme = _hook
        _paint()

    # ── 빈 상태 토글 ──────────────────────────────────
    def _sync_trend_empty(self, has_rows: bool) -> None:
        if has_rows:
            self._trend_empty.hide()
            self._trend_wrap.pack(fill="x")
        else:
            self._trend_wrap.pack_forget()
            self._trend_empty.show()

    def _sync_dup_empty(self) -> None:
        if len(self.dup_tree.get_children()) == 0:
            self._dup_wrap.pack_forget()
            self._dup_empty.show()
        else:
            self._dup_empty.hide()
            self._dup_wrap.pack(fill="both", expand=True)

    def _load_trend_display(self):
        for w in self.trend_frame.winfo_children():
            w.destroy()
        try:
            from core.trend_updater import get_top_keywords
            top = get_top_keywords(10)
            if not top:
                self._sync_trend_empty(False)
                return
            for i, item in enumerate(top, 1):
                row = tk.Frame(self.trend_frame, bg=C["card"])
                row.pack(fill="x", pady=2)
                score = item.get("score", 0)
                tk.Label(row, text=f"{i:2d}.", font=FM, fg=C["muted"],
                         bg=C["card"], width=3).pack(side="left")
                # 순위·점수(고정폭)를 먼저 오른쪽에 붙이고, 키워드 라벨이 나머지 폭을
                # fill="x" 로 가져가게 한다 — 고정 width=22(글자수)로 자르면 실제 픽셀
                # 폭과 안 맞아 "…갈아타기," 처럼 중간에 잘려 보였다(디자이너 실측).
                if score <= 0:
                    # 실제로 0점인지, 아직 측정을 안 한 것인지 구분되지 않으면 사용자가
                    # "이 키워드는 인기가 없다"로 오인한다 — 갱신 전 상태를 정직하게 표기.
                    tk.Label(row, text="측정 전", font=FS, fg=C["muted"],
                             bg=C["card"], width=8, anchor="e").pack(side="right", padx=(8, 0))
                else:
                    color = C["red"] if score >= 70 else C["yellow"] if score >= 40 else C["muted"]
                    tk.Label(row, text=f"{score:.0f}", font=FM, fg=color,
                             bg=C["card"], width=4, anchor="e").pack(side="right")
                    bar_w = _px(150)
                    bar_frame = tk.Frame(row, bg=C["border"], height=_px(12), width=bar_w)
                    bar_frame.pack(side="right", padx=8)
                    bar_frame.pack_propagate(False)
                    fill_w = int(score / 100 * bar_w)
                    tk.Frame(bar_frame, bg=color, height=_px(12), width=fill_w).place(x=0, y=0)
                tk.Label(row, text=item.get("keyword", "")[:34], font=FB,
                         fg=C["text"], bg=C["card"], anchor="w").pack(
                    side="left", fill="x", expand=True, padx=(4, 8))
            self._sync_trend_empty(True)
        except Exception as e:
            log.debug("트렌드 표시 로드 오류", exc_info=True)
            L(self.trend_frame, f"로드 오류: {e}", FS, C["red"]).pack(anchor="w")
            self._sync_trend_empty(True)

    def _update_trend(self):
        def _task():
            try:
                from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_PUBLISH_POOL_SHEET
                from core.trend_updater import update_excel_trends
                self.app.set_status("📈 트렌드 갱신 중...", C["yellow"])
                scores = update_excel_trends(
                    _active_excel_path(KEYWORD_EXCEL_PATH), KEYWORD_PUBLISH_POOL_SHEET)
                if scores:
                    self._ui_after(100, self._load_trend_display)
                    self.app.set_status("트렌드 갱신 완료", C["green"])
                else:
                    self.app.set_status("트렌드 갱신 실패 (API 키 확인)", C["yellow"])
            except Exception as e:
                log.debug("트렌드 갱신 오류", exc_info=True)
                self.app.set_status(f"{e}", C["red"])
        run_async(self._trend_btn, _task, "갱신 중...")

    def _scan_dup(self):
        def _task():
            try:
                from config.settings import KEYWORD_EXCEL_PATH, KEYWORD_PUBLISH_POOL_SHEET
                from core.duplicate_checker import scan_excel_duplicates
                self.app.set_status("중복 스캔 중...", C["yellow"])
                results = scan_excel_duplicates(
                    _active_excel_path(KEYWORD_EXCEL_PATH), KEYWORD_PUBLISH_POOL_SHEET)
                self.dup_tree.delete(*self.dup_tree.get_children())
                for r in results:
                    self.dup_tree.insert("", "end",
                        values=(r["title"][:45], r["matched"][:45],
                                f'{r["score"]}%'), tags=("dup",))
                cnt = len(results)
                msg = f"  중복 의심: {cnt}건 발견" if cnt else "  중복 없음 "

                def _update():
                    self.dup_status.config(text=msg)
                    self._sync_dup_empty()
                self._ui_after(100, _update)
                self.app.set_status(f"스캔 완료 ({cnt}건)", C["green"] if cnt == 0 else C["orange"])
            except Exception as e:
                log.debug("중복 스캔 오류", exc_info=True)
                self.app.set_status(f"스캔 오류: {e}", C["red"])
        run_async(self._scan_btn, _task, "스캔 중...")
