# gui/data_table.py — CTk 룩 가상화 표(신규, 9호 Phase 2 2026-09-03)
"""`ttk.Treeview` 대체 기반 위젯 — 각진 ttk 표 대신 이 앱의 CTk 룩(카드
배경·accent 선택 스트라이프·hairline 구분선)에 맞춘 표를 그린다.

## 설계 요지
- **API 는 Treeview 실사용 서브셋을 그대로 구현**한다 — 탭 코드가
  `ttk.Treeview(` → `DataTable(` 로 **기계적 치환**만 하면 되도록. 지원
  메서드: `get_children`/`tag_configure`/`heading`/`delete`/`column`/
  `insert`/`selection*`/`focus`/`item`/`see`/`exists`/`yview*`/
  `configure`/`bind`/`identify_row`/`move`.
- **평평한 표만 지원**(`show="headings"` 전제) — 트리 들여쓰기·`#0`
  아이콘 컬럼은 없다. `insert(parent=iid, ...)` 로 부모를 줘도 값은
  보존되지만(`item()`으로 그대로 읽힘) 화면상 평평한 순서에 놓인다.
  이 앱의 실사용 표(로그·키워드·발행 큐 등)는 전부 `parent=""` 라
  실질적 영향이 없다.
- **가상화**: 화면에 보이는 행만 Canvas 아이템으로 그린다(로그 탭
  수천 행 대비). `insert()`/`delete()`/`item()`/`tag_configure()`/
  `heading()`/`column()` 처럼 대량 반복 호출될 수 있는 변경 메서드는
  `after_idle` 로 재그리기를 늦춰 묶는다(코드로 5,000행을 반복
  `insert()` 해도 그때마다 캔버스를 다시 그리면 0.5초 예산을 못
  맞춘다 — 실측 확인). 반면 스크롤·선택·정렬처럼 사용자 상호작용에
  즉각 반응해야 하는 경로는 동기적으로 다시 그린다.
- **색은 항상 호출 시점 `theme.C`** 를 읽는다(라이트 전환 대응) —
  `_on_retheme()` 훅(`theme.retheme()` 가 위젯 트리 순회 중 이 메서드가
  있으면 호출한다, `theme.py` 참조)에서 전체를 다시 그린다.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from gui import icons
from gui.theme import C, SP, FB, FS, FT, HAS_CTK, px, remap_color

log = __import__("logging").getLogger(__name__)

# 100% DPI 기준 설계값 — 실제 사용 시 전부 px() 를 거친다(9호 Phase 1 관례).
_ROW_HEIGHT_BASE = 32
_HEADER_HEIGHT_BASE = 32
_STRIPE_WIDTH_BASE = 3
_DEFAULT_COL_WIDTH_BASE = 100
_DEFAULT_MINWIDTH_BASE = 20
_CELL_PAD_BASE = 8


def _is_numeric(value: Any) -> bool:
    """빈 문자열·None 은 숫자가 아니다(전부 숫자 컬럼이어도 빈 값이 섞이면
    문자열 정렬로 폴백 — 숫자 하나라도 파싱 실패 시 전체를 문자열 취급하는
    `_default_sort_numeric` 판단에 쓰인다)."""
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    s = str(value).strip()
    if not s:
        return False
    try:
        float(s.replace(",", ""))
        return True
    except ValueError:
        return False


def _numeric_key(value: Any) -> float:
    try:
        return float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        return float("-inf")


class DataTable(tk.Frame):
    """`ttk.Treeview` 대체 — 가상화된 CTk 룩 표.

    생성자: ``DataTable(parent, columns=(...), show="headings", height=8,
    selectmode="browse"|"extended", style=<무시>, yscrollcommand=None)``

    미지원 Treeview 메서드(리뷰 M2 — 호출하면 AttributeError, 필요해지면
    후속 보강): ``xview``/``xview_moveto``(가로 스크롤), ``bbox``,
    ``detach``/``reattach``, ``parent``, ``next``/``prev``,
    ``selection_toggle``, `#0` 트리 컬럼(``get_children(parent=비어있지
    않음)`` 은 항상 빈 튜플 — 클래스 상단 설명대로 평평한 표만 지원).
    ``index(iid)``·``set(iid, column=None, value=None)`` 은 평평한 순서
    기준으로 구현되어 있다(트리 부모/자식 개념 없음).
    """

    def __init__(self, parent, columns: Sequence[str] = (),
                 show: str = "headings", height: int = 8,
                 selectmode: str = "browse", style: Optional[str] = None,
                 yscrollcommand: Optional[Callable] = None, **kw):
        bg = kw.pop("bg", C["card"])
        # style/show 는 API 호환을 위해 받되 무시한다(Treeview 는 ttk 스타일
        # 이름·"tree"/"headings" 표시 모드를 쓰지만, 이 위젯은 항상 헤딩
        # 전용 평평한 표만 그린다).
        kw.pop("style", None)
        self._show = show
        super().__init__(parent, bg=bg, **kw)

        self._columns: Tuple[str, ...] = tuple(columns)
        self._visible_rows_opt = max(1, int(height))
        self._selectmode = selectmode
        self._yscrollcommand = yscrollcommand

        self._col_opts: Dict[str, dict] = {}
        self._headings: Dict[str, dict] = {}
        for c in self._columns:
            self._init_column(c)

        self._tag_opts: Dict[str, dict] = {}
        self._font_cache: Dict[Any, tkfont.Font] = {}

        self._items: Dict[str, dict] = {}
        self._order: List[str] = []
        self._next_id = 0

        self._selection: set = set()
        self._focus_iid: Optional[str] = None
        self._hover_iid: Optional[str] = None
        self._anchor_iid: Optional[str] = None   # Shift 범위선택 기준점
        self._top_index = 0

        self._sort_col: Optional[str] = None
        self._sort_reverse = False

        self._redraw_pending = False
        self._external_handlers: Dict[str, List[Callable]] = {}

        self._build_ui(bg)
        self._bind_events()

    # ── 컬럼/헤더 상태 ──────────────────────────────────
    def _init_column(self, col: str) -> None:
        self._col_opts[col] = {
            "width": px(_DEFAULT_COL_WIDTH_BASE),
            "minwidth": px(_DEFAULT_MINWIDTH_BASE),
            "anchor": "w",
            "stretch": True,
        }
        self._headings[col] = {"text": col, "anchor": "w", "command": None}

    # ── 위젯 구성 ──────────────────────────────────────
    def _build_ui(self, bg) -> None:
        row_h = px(_ROW_HEIGHT_BASE)
        header_h = px(_HEADER_HEIGHT_BASE)

        self._header_canvas = tk.Canvas(self, bg=C["surface_2"], height=header_h,
                                        highlightthickness=0, bd=0)
        self._header_canvas.pack(side="top", fill="x")

        body = tk.Frame(self, bg=bg)
        body.pack(side="top", fill="both", expand=True)
        self._canvas = tk.Canvas(body, bg=C["card"], highlightthickness=0, bd=0,
                                 height=row_h * self._visible_rows_opt)
        self._canvas.pack(side="left", fill="both", expand=True)

        # 텍스트 폭 측정용(말줄임 계산) — Canvas create_text 에도 같은
        # 폰트 스펙(pt 튜플)을 그대로 쓴다. Tk 는 DPI 인식 시 pt 튜플을
        # 자동으로 확대하므로(9호 Phase 1 관례) 여기서 px() 하지 않는다
        # — CTk 위젯 font= 만 px() 대상(이중 확대 방지).
        self._font = tkfont.Font(font=FB)
        self._header_font = tkfont.Font(font=FT)

    def _bind_events(self) -> None:
        c = self._canvas
        c.bind("<Configure>", self._on_body_configure, add="+")
        c.bind("<Button-1>", self._on_click, add="+")
        c.bind("<Motion>", self._on_motion, add="+")
        c.bind("<Leave>", lambda e: self._set_hover(None), add="+")
        c.bind("<MouseWheel>", self._on_mousewheel, add="+")
        c.bind("<Up>", self._on_key_up, add="+")
        c.bind("<Down>", self._on_key_down, add="+")
        c.bind("<Home>", self._on_key_home, add="+")
        c.bind("<End>", self._on_key_end, add="+")
        self._header_canvas.bind("<Configure>", lambda e: self._redraw_header(), add="+")
        self._header_canvas.bind("<Button-1>", self._on_header_click, add="+")

    # ── tk.Frame.bind 오버라이드 — 실제 상호작용 표면은 body 캔버스 ──
    def bind(self, sequence=None, func=None, add=None):
        """Treeview 는 자기 자신이 곧 상호작용 표면이라 `.bind()` 를
        직접 받지만, DataTable 은 body Canvas(`self._canvas`)가 실제
        표면이다 — 겉 Frame 에 바인딩하면 클릭이 하나도 안 잡힌다.

        외부 호출은 시퀀스별 자체 디스패처(``self._external_handlers``)
        를 거친다(리뷰 M1) — Tk 표준처럼 ``add`` 미지정이면 그 시퀀스의
        이전 **외부** 핸들러를 교체하고, ``add="+"`` 명시 시만 누적한다.
        Tkinter 표준 ``Canvas.unbind()`` 는 시퀀스 전체 바인딩 스크립트를
        지우는 알려진 결함이 있어(내부 `_bind_events` 바인딩까지 같이
        날아감), 실제 `unbind` 호출 없이 자체 리스트로만 교체한다 — 내부
        바인딩은 이 경로를 전혀 거치지 않으므로 항상 보존된다."""
        if sequence is None:
            return self._canvas.bind()
        if func is None:
            return self._canvas.bind(sequence)
        if sequence not in self._external_handlers:
            self._canvas.bind(sequence, self._make_external_dispatcher(sequence), add="+")
            self._external_handlers[sequence] = []
        if add:
            self._external_handlers[sequence].append(func)
        else:
            self._external_handlers[sequence] = [func]
        return f"external:{sequence}:{len(self._external_handlers[sequence])}"

    def _make_external_dispatcher(self, sequence: str) -> Callable:
        def _dispatch(event, _seq=sequence) -> None:
            for handler in list(self._external_handlers.get(_seq, [])):
                handler(event)
        return _dispatch

    # ── Treeview 호환 메서드 ────────────────────────────
    def get_children(self, item: str = "") -> Tuple[str, ...]:
        if item == "":
            return tuple(self._order)
        # 트리 구조 미지원(클래스 docstring 참조) — 지정 부모의 자식은 없다.
        return ()

    def tag_configure(self, tag: str, **opts) -> None:
        cur = self._tag_opts.setdefault(tag, {})
        for k in ("foreground", "background", "font"):
            if k in opts:
                cur[k] = opts[k]
        self._schedule_redraw()

    def heading(self, col: str, text: Optional[str] = None,
                anchor: Optional[str] = None, command: Optional[Callable] = None,
                **_ignored) -> Optional[dict]:
        if col not in self._headings:
            self._init_column(col)
            if col not in self._columns:
                self._columns = self._columns + (col,)
        h = self._headings[col]
        if text is None and anchor is None and command is None:
            return dict(h)
        if text is not None:
            h["text"] = text
        if anchor is not None:
            h["anchor"] = anchor
        if command is not None:
            h["command"] = command
        self._redraw_header()
        return None

    def column(self, col: str, width: Optional[int] = None,
               minwidth: Optional[int] = None, anchor: Optional[str] = None,
               stretch: Optional[bool] = None, **_ignored) -> Optional[dict]:
        if col not in self._col_opts:
            self._init_column(col)
            if col not in self._columns:
                self._columns = self._columns + (col,)
        opt = self._col_opts[col]
        if width is None and minwidth is None and anchor is None and stretch is None:
            return dict(opt)
        if width is not None:
            opt["width"] = int(width)
        if minwidth is not None:
            opt["minwidth"] = int(minwidth)
        if anchor is not None:
            opt["anchor"] = anchor
        if stretch is not None:
            opt["stretch"] = bool(stretch)
        self._schedule_redraw()
        return None

    def insert(self, parent: str, index: Any, iid: Optional[str] = None,
               values: Sequence = (), tags: Any = (), text: str = "") -> str:
        if iid is None:
            while True:
                self._next_id += 1
                iid = f"row{self._next_id}"
                if iid not in self._items:
                    break
        if isinstance(tags, str):
            tags = (tags,)
        self._items[iid] = {
            "values": tuple(values), "tags": tuple(tags), "text": text,
            "parent": parent,
        }
        if parent == "" or parent not in self._items:
            if index == "end" or index is None:
                self._order.append(iid)
            else:
                pos = max(0, min(int(index), len(self._order)))
                self._order.insert(pos, iid)
        else:
            # 트리 미지원(클래스 docstring) — 값은 보존, 평평한 끝에 추가.
            self._order.append(iid)
        self._schedule_redraw()
        return iid

    def delete(self, *items: str) -> None:
        to_delete: set = set()
        for it in items:
            if isinstance(it, (list, tuple, set)):
                to_delete.update(it)
            else:
                to_delete.add(it)
        if not to_delete:
            return
        # 리스트 컴프리헨션 1회 순회 — 개별 list.remove() 반복(O(n²)) 대신
        # O(n) 로 끝낸다(5,000행 delete 성능 게이트).
        self._order = [iid for iid in self._order if iid not in to_delete]
        for iid in to_delete:
            self._items.pop(iid, None)
        self._selection -= to_delete
        if self._focus_iid in to_delete:
            self._focus_iid = None
        if self._hover_iid in to_delete:
            self._hover_iid = None
        self._clamp_top()
        self._schedule_redraw()

    def selection(self) -> Tuple[str, ...]:
        return tuple(iid for iid in self._order if iid in self._selection)

    def selection_set(self, items: Any) -> None:
        items = self._as_iid_tuple(items)
        new_sel = set(i for i in items if i in self._items)
        changed = new_sel != self._selection
        self._selection = new_sel
        if items:
            self._anchor_iid = items[-1]
        self._redraw_body()
        if changed:
            self._canvas.event_generate("<<TreeviewSelect>>")

    def selection_remove(self, items: Any) -> None:
        items = self._as_iid_tuple(items)
        before = set(self._selection)
        self._selection -= set(items)
        self._redraw_body()
        if self._selection != before:
            self._canvas.event_generate("<<TreeviewSelect>>")

    def selection_clear(self) -> None:
        if not self._selection:
            return
        self._selection.clear()
        self._redraw_body()
        self._canvas.event_generate("<<TreeviewSelect>>")

    @staticmethod
    def _as_iid_tuple(items: Any) -> Tuple[str, ...]:
        if isinstance(items, str):
            return (items,)
        return tuple(items)

    def focus(self, item: Optional[str] = None):
        if item is None:
            return self._focus_iid or ""
        if item in self._items:
            self._focus_iid = item
        return None

    def item(self, iid: str, option: Optional[str] = None, **kw):
        it = self._items.get(iid)
        if it is None:
            raise tk.TclError(f"DataTable: 존재하지 않는 iid {iid!r}")
        if kw:
            if "values" in kw:
                it["values"] = tuple(kw["values"])
            if "tags" in kw:
                tags = kw["tags"]
                it["tags"] = (tags,) if isinstance(tags, str) else tuple(tags)
            if "text" in kw:
                it["text"] = kw["text"]
            self._schedule_redraw()
            return None
        if option is not None:
            return it.get(option)
        return dict(it)

    def index(self, iid: str) -> int:
        """행의 표시 순서 인덱스(리뷰 M2 — Treeview 호환). 트리 미지원이라
        항상 평평한 `self._order` 기준 위치를 돌려준다."""
        if iid not in self._items:
            raise tk.TclError(f"DataTable: 존재하지 않는 iid {iid!r}")
        return self._order.index(iid)

    def set(self, iid: str, column: Optional[str] = None, value: Any = None):
        """컬럼 단위 값 조회/설정(리뷰 M2 — Treeview 호환).
        ``column`` 생략 시 {컬럼명: 값} dict 전체, ``value`` 생략 시 해당
        컬럼 값만 반환. 둘 다 주면 그 컬럼 값을 설정한다."""
        it = self._items.get(iid)
        if it is None:
            raise tk.TclError(f"DataTable: 존재하지 않는 iid {iid!r}")
        values = list(it["values"])
        while len(values) < len(self._columns):
            values.append("")
        if column is None:
            return {c: values[i] for i, c in enumerate(self._columns)}
        if column not in self._columns:
            raise tk.TclError(f"DataTable: 존재하지 않는 컬럼 {column!r}")
        idx = self._columns.index(column)
        if value is None:
            return values[idx]
        values[idx] = value
        it["values"] = tuple(values)
        self._schedule_redraw()
        return None

    def see(self, iid: str) -> None:
        if iid not in self._items or iid not in self._order:
            return
        idx = self._order.index(iid)
        visible = self._visible_rows()
        if idx < self._top_index:
            self._top_index = idx
        elif idx >= self._top_index + visible:
            self._top_index = idx - visible + 1
        self._clamp_top()
        self._redraw_body()

    def exists(self, iid: str) -> bool:
        return iid in self._items

    def identify_row(self, y: int) -> str:
        iid = self._row_at_y(y)
        return iid or ""

    def move(self, iid: str, parent: str, index: Any) -> None:
        if iid not in self._items:
            return
        if iid in self._order:
            self._order.remove(iid)
        if index == "end" or index is None or int(index) >= len(self._order):
            self._order.append(iid)
        else:
            self._order.insert(max(0, int(index)), iid)
        self._items[iid]["parent"] = parent
        self._schedule_redraw()

    # ── 스크롤(ttk.Scrollbar command= 프로토콜과 호환) ───
    def yview(self, *args):
        total = len(self._order)
        if not args:
            return self._scroll_fractions()
        action = args[0]
        if total == 0:
            return None
        if action == "moveto":
            frac = float(args[1])
            self._top_index = int(round(frac * total))
        elif action == "scroll":
            n = int(args[1])
            what = args[2] if len(args) > 2 else "units"
            step = n * (self._visible_rows() if what == "pages" else 1)
            self._top_index += step
        self._clamp_top()
        self._redraw_body()
        return None

    def yview_moveto(self, fraction: float) -> None:
        self.yview("moveto", fraction)

    def _scroll_fractions(self) -> Tuple[float, float]:
        total = len(self._order)
        if total == 0:
            return (0.0, 1.0)
        visible = self._visible_rows()
        first = self._top_index / total
        last = min(1.0, (self._top_index + visible) / total)
        return (first, last)

    def _update_scrollbar(self) -> None:
        if self._yscrollcommand is None:
            return
        first, last = self._scroll_fractions()
        try:
            self._yscrollcommand(first, last)
        except Exception as exc:  # noqa: BLE001 — 외부 스크롤바 콜백 실패가 표를 막으면 안 됨
            log.debug("[data_table] yscrollcommand 실패: %s", exc, exc_info=True)

    # ── configure/bind 는 tk.Frame 표준을 최대한 재사용 ──
    def configure(self, **kw):
        if "yscrollcommand" in kw:
            self._yscrollcommand = kw.pop("yscrollcommand")
        if "height" in kw:
            self._visible_rows_opt = max(1, int(kw.pop("height")))
            self._canvas.configure(height=px(_ROW_HEIGHT_BASE) * self._visible_rows_opt)
        if "selectmode" in kw:
            self._selectmode = kw.pop("selectmode")
        kw.pop("style", None)
        if "columns" in kw:
            for c in kw.pop("columns"):
                if c not in self._col_opts:
                    self._init_column(c)
            self._columns = tuple(dict.fromkeys(self._columns))
        remaining = {k: v for k, v in kw.items() if k in ("bg", "background")}
        if remaining:
            try:
                super().configure(**remaining)
            except tk.TclError:
                pass
        self._schedule_redraw()

    config = configure

    # ── 내부: 레이아웃/그리기 ────────────────────────────
    def _visible_rows(self) -> int:
        row_h = px(_ROW_HEIGHT_BASE)
        h = self._canvas.winfo_height() or (row_h * self._visible_rows_opt)
        return max(1, h // row_h)

    def _clamp_top(self) -> None:
        max_top = max(0, len(self._order) - self._visible_rows())
        self._top_index = min(max(0, self._top_index), max_top)

    def _column_layout(self) -> List[Tuple[str, int, int]]:
        """[(col, x시작, 폭)] — Treeview 근사. 캔버스 폭과 컬럼 요청폭
        합의 차이(늘어남 +/줄어듦 -)를 stretch 컬럼에 **요청폭 비례**로
        배분한다 — 늘어나면 비례 확장, 줄어들면 비례 축소하되 각 컬럼은
        자기 minwidth 밑으로는 안 내려간다(코더 실측 회귀, 최소창
        900×620 캡처: 고정폭 합이 위젯 폭을 넘어도 손을 안 대 마지막
        컬럼들이 잘렸다 — ttk.Treeview 는 stretch 컬럼을 줄여서라도 항상
        꽉 맞춘다). stretch 컬럼이 하나도 없으면 차이를 마지막 컬럼
        하나가 통째로 흡수한다(그마저도 minwidth 미만으로는 안 줄인다).
        """
        if not self._columns:
            return []
        widths = {c: self._col_opts[c]["width"] for c in self._columns}
        total_requested = sum(widths.values())
        canvas_w = max(self._canvas.winfo_width(), 1)
        diff = canvas_w - total_requested   # >0 늘어남, <0 줄어듦
        stretch_cols = [c for c in self._columns if self._col_opts[c].get("stretch", True)]

        if diff != 0:
            if stretch_cols:
                stretch_total = sum(widths[c] for c in stretch_cols) or 1
                for c in stretch_cols:
                    share = widths[c] / stretch_total
                    minw = self._col_opts[c].get("minwidth", 0)
                    widths[c] = max(minw, widths[c] + diff * share)
            else:
                last = self._columns[-1]
                minw = self._col_opts[last].get("minwidth", 0)
                widths[last] = max(minw, widths[last] + diff)

        layout = []
        x = 0
        for c in self._columns:
            w = widths[c]
            layout.append((c, x, w))
            x += w
        return layout

    def _truncate(self, font: tkfont.Font, text: str, max_width: float) -> str:
        if max_width <= 0:
            return ""
        if font.measure(text) <= max_width:
            return text
        ell = "…"
        ell_w = font.measure(ell)
        if ell_w >= max_width:
            return ""
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid]) + ell_w <= max_width:
                lo = mid
            else:
                hi = mid - 1
        return text[:lo] + ell

    def _on_body_configure(self, _event=None) -> None:
        self._clamp_top()
        self._redraw_body()
        self._redraw_header()

    def _redraw(self) -> None:
        self._redraw_pending = False
        self._redraw_header()
        self._redraw_body()

    def _schedule_redraw(self) -> None:
        """대량 반복 호출(insert 등)에 쓰는 지연 재그리기 — 같은 이벤트
        루프 틱 안에서 여러 번 불려도 실제 캔버스 재작업은 1회로 묶인다."""
        if self._redraw_pending:
            return
        self._redraw_pending = True
        try:
            self.after_idle(self._redraw)
        except tk.TclError:
            self._redraw_pending = False

    def _redraw_header(self) -> None:
        canvas = self._header_canvas
        try:
            canvas.configure(bg=C["surface_2"])
            canvas.delete("all")
        except tk.TclError:
            return
        header_h = px(_HEADER_HEIGHT_BASE)
        pad = SP["sm"]
        layout = self._column_layout()
        for col, x, w in layout:
            heading = self._headings[col]
            glyph = ""
            if col == self._sort_col:
                glyph = icons.glyph("collapse" if not self._sort_reverse else "expand")
            glyph_w = self._header_font.measure(glyph) if glyph else 0
            anchor = heading.get("anchor", "w")
            avail = w - 2 * pad - (glyph_w + pad if glyph else 0)
            text = self._truncate(self._header_font, heading["text"], avail)
            if anchor == "e":
                tx, anc = x + w - pad - (glyph_w + pad if glyph else 0), "e"
            elif anchor == "center":
                tx, anc = x + w / 2, "center"
            else:
                tx, anc = x + pad, "w"
            canvas.create_text(tx, header_h / 2, text=text, fill=C["muted"],
                               font=FT, anchor=anc)
            if glyph:
                canvas.create_text(x + w - pad, header_h / 2, text=glyph,
                                   fill=C["text"], font=self._icon_font(),
                                   anchor="e")
        if layout:
            total_w = layout[-1][1] + layout[-1][2]
            for col, x, w in layout[:-1]:
                canvas.create_line(x + w, 0, x + w, header_h, fill=C["border"])
            canvas.create_line(0, header_h - 1, max(total_w, canvas.winfo_width()),
                               header_h - 1, fill=C["border"])

    def _icon_font(self):
        fam_size = icons.icon_font(12)
        return fam_size or FS

    def _get_font(self, spec: Any) -> tkfont.Font:
        """행 폰트 캐시(리뷰 M4) — `tag_configure(font=)` 로 지정된 커스텀
        폰트를 재도색마다 `tkfont.Font()` 로 새로 만들지 않고 스펙 키로
        재사용한다(대량 태그 표에서 매 스크롤 redraw 마다 Font 객체가
        새로 생겨 GC 압박을 주던 것을 제거)."""
        if spec is FB:
            return self._font
        key = spec if isinstance(spec, (str, tuple)) else id(spec)
        cached = self._font_cache.get(key)
        if cached is None:
            cached = tkfont.Font(font=spec)
            self._font_cache[key] = cached
        return cached

    def _redraw_body(self) -> None:
        canvas = self._canvas
        try:
            canvas.configure(bg=C["card"])
            canvas.delete("all")
        except tk.TclError:
            return
        row_h = px(_ROW_HEIGHT_BASE)
        pad = SP["sm"]
        stripe_w = px(_STRIPE_WIDTH_BASE)
        layout = self._column_layout()
        canvas_w = max(canvas.winfo_width(), (layout[-1][1] + layout[-1][2]) if layout else 1)
        visible = self._visible_rows()
        end = min(self._top_index + visible, len(self._order))
        for slot, i in enumerate(range(self._top_index, end)):
            iid = self._order[i]
            item = self._items[iid]
            y0 = slot * row_h
            y1 = y0 + row_h
            selected = iid in self._selection
            hovered = iid == self._hover_iid and not selected
            row_bg = C["accent_soft"] if selected else (C["card_hover"] if hovered else C["card"])
            canvas.create_rectangle(0, y0, canvas_w, y1, fill=row_bg, outline="")
            if selected:
                canvas.create_rectangle(0, y0, stripe_w, y1, fill=C["accent"], outline="")
            tag_opt: Dict[str, Any] = {}
            for t in item["tags"]:
                tag_opt.update(self._tag_opts.get(t, {}))
            fg = tag_opt.get("foreground") or C["text"]
            row_font = tag_opt.get("font") or FB
            row_font_obj = self._get_font(row_font)
            values = item["values"]
            for col_idx, (col, x, w) in enumerate(layout):
                raw = values[col_idx] if col_idx < len(values) else ""
                avail = w - 2 * pad
                txt = self._truncate(row_font_obj, "" if raw is None else str(raw), avail)
                anchor = self._col_opts[col].get("anchor", "w")
                if anchor == "e":
                    tx, anc = x + w - pad, "e"
                elif anchor == "center":
                    tx, anc = x + w / 2, "center"
                else:
                    tx, anc = x + pad, "w"
                canvas.create_text(tx, (y0 + y1) / 2, text=txt, fill=fg,
                                   font=row_font, anchor=anc)
            for col, x, w in layout[:-1]:
                canvas.create_line(x + w, y0, x + w, y1, fill=C["border"])
        self._update_scrollbar()

    # ── 마우스/키보드 ───────────────────────────────────
    def _row_at_y(self, y: int) -> Optional[str]:
        row_h = px(_ROW_HEIGHT_BASE)
        slot = int(y // row_h)
        i = self._top_index + slot
        if 0 <= i < len(self._order):
            return self._order[i]
        return None

    def _on_click(self, event) -> None:
        self._canvas.focus_set()
        iid = self._row_at_y(event.y)
        if iid is None:
            return
        self.focus(iid)
        shift = bool(event.state & 0x0001)
        ctrl = bool(event.state & 0x0004)
        if self._selectmode == "extended" and shift and self._anchor_iid in self._order:
            a = self._order.index(self._anchor_iid)
            b = self._order.index(iid)
            lo, hi = (a, b) if a <= b else (b, a)
            self._selection = set(self._order[lo:hi + 1])
        elif self._selectmode == "extended" and ctrl:
            if iid in self._selection:
                self._selection.discard(iid)
            else:
                self._selection.add(iid)
            self._anchor_iid = iid
        else:
            self._selection = {iid}
            self._anchor_iid = iid
        self._redraw_body()
        self._canvas.event_generate("<<TreeviewSelect>>")

    def _on_motion(self, event) -> None:
        self._set_hover(self._row_at_y(event.y))

    def _set_hover(self, iid: Optional[str]) -> None:
        if iid != self._hover_iid:
            self._hover_iid = iid
            self._redraw_body()

    def _on_mousewheel(self, event) -> str:
        # 위젯 로컬 bind 만 쓴다(bind_all 금지 — customtkinter6-notes 함정,
        # 여러 표/스크롤 영역이 같은 화면에 있을 때 서로 간섭한다).
        delta = event.delta
        if delta:
            self._top_index -= (3 if delta > 0 else -3)
            self._clamp_top()
            self._redraw_body()
        return "break"

    def _on_key_up(self, event) -> str:
        self._move_focus(-1)
        return "break"

    def _on_key_down(self, event) -> str:
        self._move_focus(1)
        return "break"

    def _on_key_home(self, event) -> str:
        if self._order:
            self._select_and_focus(self._order[0])
        return "break"

    def _on_key_end(self, event) -> str:
        if self._order:
            self._select_and_focus(self._order[-1])
        return "break"

    def _move_focus(self, delta: int) -> None:
        if not self._order:
            return
        if self._focus_iid in self._order:
            idx = self._order.index(self._focus_iid)
        else:
            idx = -1 if delta > 0 else len(self._order)
        new_idx = min(max(idx + delta, 0), len(self._order) - 1)
        self._select_and_focus(self._order[new_idx])

    def _select_and_focus(self, iid: str) -> None:
        self.focus(iid)
        self._selection = {iid}
        self._anchor_iid = iid
        self.see(iid)
        self._redraw_body()
        self._canvas.event_generate("<<TreeviewSelect>>")

    def _on_header_click(self, event) -> None:
        layout = self._column_layout()
        for col, x, w in layout:
            if x <= event.x < x + w:
                self._on_heading_clicked(col)
                return

    def _on_heading_clicked(self, col: str) -> None:
        command = self._headings[col].get("command")
        if command is not None:
            try:
                command()
            except Exception as exc:  # noqa: BLE001 — 사용자 콜백 실패가 표를 막으면 안 됨
                log.debug("[data_table] heading command 실패(%s): %s", col, exc, exc_info=True)
            return
        self._default_sort(col)

    def _default_sort(self, col: str) -> None:
        idx = self._columns.index(col)
        reverse = (self._sort_col == col and not self._sort_reverse)
        pairs = [(self._items[iid]["values"][idx] if idx < len(self._items[iid]["values"]) else "", iid)
                 for iid in self._order]
        numeric = all(_is_numeric(v) for v, _ in pairs) if pairs else True
        if numeric:
            pairs.sort(key=lambda t: _numeric_key(t[0]), reverse=reverse)
        else:
            pairs.sort(key=lambda t: str(t[0]), reverse=reverse)
        self._order = [iid for _, iid in pairs]
        self._sort_col = col
        self._sort_reverse = reverse
        self._redraw()

    # ── 재도색 훅(theme.retheme() 이 호출, gui/theme.py 참조) ──
    def _on_retheme(self) -> None:
        """theme.retheme() 가 위젯 트리 순회 중 호출한다(gui/theme.py
        `_retheme_widget_tree` 참조). Canvas 아이템 자체는 항상 `theme.C`
        를 새로 읽어 전체를 다시 그리면 되지만(가상화라 비용도 낮음),
        `tag_configure(foreground=C["orange"])` 처럼 **생성 시점 hex 를
        그대로 저장해 둔 태그 색**은 그냥 다시 그리는 것만으론 안 바뀐다
        — `_redraw_body()` 가 저장된 값을 그대로 읽어 쓰기 때문(9호
        Phase 5 코더 E2 실측 회귀: 라이트/다크 전환 후 태그 색이 옛
        팔레트에 굳어 있었다). 그래서 다시 그리기 전에 저장된 태그 색을
        `theme.remap_color()` 로 먼저 갱신한다 — retheme() 이 일반 tk
        위젯에 쓰는 것과 같은 역매핑을 재사용(동작 두 벌 유지 안 함).

        `configure()`(대량 변경용 지연 재그리기)를 거치지 않고 직접
        `tk.Frame.configure()` + 동기 `_redraw()` 로 즉시 반영한다."""
        for opts in self._tag_opts.values():
            if "foreground" in opts:
                opts["foreground"] = remap_color(opts["foreground"], "fg")
            if "background" in opts:
                opts["background"] = remap_color(opts["background"], "bg")
        try:
            tk.Frame.configure(self, bg=C["card"])
        except tk.TclError:
            pass
        self._redraw_pending = False
        self._redraw()


__all__ = ["DataTable"]
