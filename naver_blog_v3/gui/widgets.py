# gui/widgets.py — 공용 위젯 라이브러리 (신규, GUI 개편 2026-09-02)
"""탭 전역에서 재사용하는 순정 tkinter 위젯 모음. `theme.py` 의 `C` 토큰·
`SP` 간격 토큰만 쓰고 매직넘버를 두지 않는다. 외부 UI 라이브러리 금지
(순수 tk/ttk).

messagebox 사용 규칙(설계서 §1.2): 파괴적·되돌릴 수 없는 동작 확인
(askyesno)과 예외 에러(showerror)만 messagebox 를 쓴다. 정보성 알림은
`Toast.show(...)`, 파괴적 확인은 `InlineConfirm` 을 우선 검토한다.
"""
from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from gui import icons
from gui.theme import (C, SP, R, FB, FS, FT, F_CAPTION, FONT_FAMILY, HAS_CTK,
                        DangerBtn, GhostBtn, PrimaryBtn, SecondaryBtn, SubtleBtn,
                        Ent, bind_mousewheel, current_palette, px,
                        animate, ease_out_quad)
from gui.theme import _pair as _theme_pair   # noqa: PLC2701 — 같은 패키지 내부 재사용(theme<->widgets 상호 참조는 기존 관례)
from gui.theme import _ctk_font   # noqa: PLC2701 — CTk 위젯 font= 는 항상 이 헬퍼(픽셀 고정, theme.py docstring 참조)
# ttk.Treeview 대체 위젯(9호 Phase 2) — widgets.py 에서 재수출해 탭 코드가
# `from gui.widgets import DataTable` 한 줄로 다른 위젯들과 같이 가져올 수
# 있게 한다(본체는 gui/data_table.py, 이 파일 경계 밖).
from gui.data_table import DataTable

if HAS_CTK:
    import customtkinter as ctk

# ── 상태 의미 색 — Pill/Banner/Toast/InlineConfirm 공용 ─────────────────
# 토큰 *이름*을 단일 진실 원천으로 두고, 단일 hex 버전(`_build_kind_colors`
# — 순정 tk 위젯: StatusPill/InlineConfirm 줄무늬 Frame 등)과 (light,dark)
# 페어 버전(`_kind_color_pairs` — CTk 위젯: Toast/Banner 의 CTkFrame
# fg_color) 을 여기서 같이 파생시킨다 — 8호 Phase 2 디자이너 감사 🔴2:
# Toast/Banner 가 `colors["bg"]`(단일 hex) 를 CTkFrame 의 fg_color 에 그대로
# 넘겨 `ctk.set_appearance_mode()` 로 안 바뀌던(라이트 전환 시 다크 고정)
# 회귀의 근본 수정.
_KIND_TOKEN_NAMES: Dict[str, Dict[str, str]] = {
    "ok":      {"bg": "success_bg", "fg": "green",  "stripe": "green"},
    "success": {"bg": "success_bg", "fg": "green",  "stripe": "green"},
    "warn":    {"bg": "warn_bg",    "fg": "yellow", "stripe": "yellow"},
    "error":   {"bg": "danger_bg",  "fg": "red",    "stripe": "red"},
    "info":    {"bg": "info_bg",    "fg": "accent", "stripe": "accent"},
    "neutral": {"bg": "card",       "fg": "muted",  "stripe": "border"},
}


def _build_kind_colors() -> Dict[str, Dict[str, str]]:
    """호출 시점의 C 값으로 만든다 — import 시점에 굳히면 theme.apply_palette()
    (라이트 테마) 이후에도 알약·토스트·배너가 다크 색을 쓴다(2026-09-02 5호 발견).
    순정 tk 위젯(단일 hex 로 충분한 곳) 전용 — CTk 위젯은 `_kind_color_pairs()`."""
    return {kind: {k: C[v] for k, v in tokens.items()}
            for kind, tokens in _KIND_TOKEN_NAMES.items()}


class _LazyKindColors(dict):
    """하위호환 별칭 — 읽을 때마다 현재 팔레트로 재계산."""
    def __getitem__(self, key):
        return _build_kind_colors()[key]

    def get(self, key, default=None):
        return _build_kind_colors().get(key, default)


_KIND_COLORS: Dict[str, Dict[str, str]] = _LazyKindColors()


def _kind_colors(kind: str) -> Dict[str, str]:
    table = _build_kind_colors()
    return table.get(kind, table["neutral"])


def _kind_color_pairs(kind: str) -> Dict[str, tuple]:
    """`_kind_colors()` 의 (light,dark) 페어 버전 — CTk 위젯의 fg_color 등에
    쓴다. 페어로 넘기면 `ctk.set_appearance_mode()` 호출만으로 위젯이
    스스로 다시 그려져(내부 구현 확인, customtkinter 6.0), `retheme()` 가
    이 위젯의 존재를 몰라도(트리 순회에서 CTk 는 건너뜀) 라이트/다크 전환이
    항상 맞게 반영된다."""
    tokens = _KIND_TOKEN_NAMES.get(kind, _KIND_TOKEN_NAMES["neutral"])
    return {k: _theme_pair(v) for k, v in tokens.items()}


def _bg_of(widget) -> str:
    """부모 위젯의 배경색을 안전하게 읽는다(ttk 위젯 등 bg 없는 경우 폴백)."""
    try:
        return widget.cget("bg")
    except tk.TclError:
        return C["bg"]


# ── 섹션 카드 ─────────────────────────────────────────
def _resolve_icon_glyph(icon: Optional[str]) -> Optional[str]:
    """icon 인자가 icons.ICONS 키(신규 코드)면 글리프 문자로, 아니면(레거시
    이모지 문자열 등) 받은 값을 그대로 돌려준다(하위호환 — 기존 탭들은
    icon="🎯" 식으로 이모지를 직접 넘긴다, 8호 Phase 2 기준 남아있는 emoji
    는 이 함수가 새로 만들지 않는다는 것만 보장한다)."""
    if icon and icon in icons.ICONS:
        return icons.glyph(icon)
    return icon


class SectionCard(tk.Frame):
    """제목 + 한 줄 설명 + 우측 액션 영역 + body. 모든 탭 섹션의 단일 컨테이너.

    항상 순정 tk.Frame(CTkFrame 로 안 바꿈) — 8호 Phase 2 실측: `self._surface`
    를 CTkFrame 으로 두면 `winfo_children()` 재귀 순회 유틸이 그 프레임을
    만났을 때 `.cget("text")` 를 부르는 흔한 패턴(`tests/test_gui_settings.py`
    의 진단 도구 라벨 훑기 등)이 깨진다 — CTkFrame.cget() 은 모르는 옵션에
    `TclError` 가 아니라 `ValueError` 를 던져(customtkinter 6.0 실측 확인),
    `except tk.TclError` 로 짠 기존 순회 코드가 못 잡는다. SectionCard 는
    설정 탭처럼 카드가 수십 개 쌓이는 화면에 워낙 광범위하게 쓰여 이 위험을
    감수하지 않기로 했다(Card()/Ent() 는 이런 범용 순회 유틸의 대상이 되는
    자리가 아니라서 영향이 없었음 — 실측으로 구분). 아이콘 글리프 지원은
    유지한다(CTk 여부와 무관하게 항상 가능).
    """

    def __init__(self, parent, title: str, subtitle: Optional[str] = None,
                 icon: Optional[str] = None, help_text: Optional[str] = None, **kw):
        pad_x = kw.pop("padx", SP["lg"])
        pad_y = kw.pop("pady", SP["md"])
        # v4(2026-09-21): 카드에 1px 헤어라인 테두리 — 배경만으로 구분하던
        # 예전 카드는 bg/card 명도 차가 작아 "어디서 카드가 끝나는지" 가
        # 안 보였다. Tk 는 둥근 모서리를 못 그리므로 테두리+여백으로 카드
        # 경계를 만든다(CTkFrame 기반 `theme.Card` 와 시각적으로 통일).
        kw.setdefault("highlightthickness", px(1))
        kw.setdefault("highlightbackground", C["border"])
        kw.setdefault("highlightcolor", C["border"])
        super().__init__(parent, bg=C["card"], relief="flat", bd=0, **kw)

        content = self
        self._content = content
        header = tk.Frame(content, bg=C["card"])
        header.pack(fill="x", padx=pad_x, pady=(pad_y, 0))
        icon_glyph = _resolve_icon_glyph(icon)
        if icon_glyph:
            icon_font = (icons.icon_font(18) if (icon in icons.ICONS and icons.has_icon_font())
                        else FT)
            tk.Label(header, text=icon_glyph, font=icon_font, fg=C["text"],
                     bg=C["card"]).pack(side="left", padx=(0, SP["xs"]))
        tk.Label(header, text=title, font=FT, fg=C["text"],
                 bg=C["card"]).pack(side="left")
        if help_text:
            HelpIcon(header, help_text).pack(side="left", padx=(SP["xs"], 0))
        self.actions = tk.Frame(header, bg=C["card"])
        self.actions.pack(side="right")

        self._pad_x = pad_x
        self._subtitle_lbl: Optional[tk.Label] = None

        self.body = tk.Frame(content, bg=C["card"])

        if subtitle:
            self.set_subtitle(subtitle)
        self.body.pack(fill="both", expand=True, padx=pad_x, pady=(SP["sm"], pad_y))

    def set_subtitle(self, text: str) -> None:
        """부제(설명 한 줄)를 설정/교체한다. 최초 호출 시 구분선까지 함께 만든다."""
        if self._subtitle_lbl is None:
            self._subtitle_lbl = tk.Label(self._content, text=text, font=FS,
                                          fg=C["text_2"], bg=C["card"], anchor="w",
                                          justify="left")
            self._subtitle_lbl.pack(fill="x", padx=self._pad_x, pady=(SP["xs"], 0),
                                    before=self.body if self.body.winfo_manager() else None)
            sep = tk.Frame(self._content, height=px(1), bg=C["border"])
            sep.pack(fill="x", padx=self._pad_x, pady=(SP["sm"], 0),
                     before=self.body if self.body.winfo_manager() else None)
        else:
            self._subtitle_lbl.config(text=text)


# ── 탭 상단 헤더 ──────────────────────────────────────
class Header(tk.Frame):
    """탭 최상단: 큰 제목 + 부제 + 우측 액션들 + 하단 헤어라인 1px.

    v4(2026-09-21) 수리 — 예전엔 ``pack_propagate(False)`` 로 높이를 56px 에
    **못 박아** 두 가지 실물 결함이 있었다(개편 전 캡처로 실증):

    ① 제목(FH)+부제(FS)의 자연 높이가 56px 를 넘으면 부제가 제목 **위에
       겹쳐 그려졌다**(발행·진행 탭 "오늘 실행 상황을 확인하고 제어해요").
    ② ``title_wrap`` 을 먼저 pack 하고 ``actions`` 를 나중에 pack 해서,
       Tk 가 남는 폭을 제목에 먼저 주고 액션 버튼을 화면 밖으로 밀어냈다
       (900×620 에서 "테스트"·"중지" 가 통째로 잘렸고, 1080×720 에서도
       "중지" 가 반 잘렸다).

    처방: 높이는 **최소값**(`HEIGHT`)만 보장하고 내용에 따라 자라게 하며,
    ``actions`` 를 **먼저** pack 해 액션 폭을 선점하고 제목이 남는 폭을
    받게 한다(제목이 길면 제목이 줄어들지, 버튼이 사라지지 않는다).

    `HEIGHT` 은 100% DPI 기준값이며 이제 "최소 높이" 를 뜻한다.
    """

    HEIGHT = 56

    def __init__(self, parent, title: str, subtitle: Optional[str] = None,
                 icon: Optional[str] = None, **kw):
        from gui.theme import FH
        bg = kw.pop("bg", C["bg"])
        super().__init__(parent, bg=bg, **kw)
        top = tk.Frame(self, bg=bg)
        top.pack(side="top", fill="both", expand=True, pady=(SP["xs"], SP["sm"]))
        # 높이는 "최소" 로만 쓴다 — pack_propagate 를 끄지 않으므로 내용이
        # 커지면 프레임도 같이 커진다(겹침 0). 최소 높이는 폭 1px 짜리
        # 보이지 않는 스페이서로 확보한다(제목만 있는 헤더도 납작해지지 않게).
        tk.Frame(top, width=px(1), height=px(self.HEIGHT) - SP["xs"] - SP["sm"],
                 bg=bg).pack(side="left")

        # ① 액션 먼저 — 폭 선점(잘림 방지). 액션이 비어 있으면 0폭이라 무해.
        self.actions = tk.Frame(top, bg=bg)
        self.actions.pack(side="right", padx=(SP["md"], 0))

        icon_glyph = _resolve_icon_glyph(icon)
        if icon_glyph:
            icon_font = (icons.icon_font(22) if (icon in icons.ICONS and icons.has_icon_font())
                        else FH)
            tk.Label(top, text=icon_glyph, font=icon_font, fg=C["accent"],
                     bg=bg).pack(side="left", padx=(0, SP["sm"]))
        title_wrap = tk.Frame(top, bg=bg)
        title_wrap.pack(side="left", fill="both", expand=True)
        tk.Label(title_wrap, text=title, font=FH, fg=C["text"], bg=bg,
                 anchor="w").pack(anchor="w", fill="x")
        if subtitle:
            tk.Label(title_wrap, text=subtitle, font=FS, fg=C["muted"], bg=bg,
                     anchor="w", justify="left").pack(anchor="w", fill="x",
                                                       pady=(px(2), 0))

        tk.Frame(self, height=px(1), bg=C["border"]).pack(side="bottom", fill="x")


# ── 스크롤 영역 ───────────────────────────────────────
class ScrollArea(tk.Frame):
    """canvas+inner+scrollbar. 마우스휠은 진입 시만(bind_mousewheel 재사용)."""

    def __init__(self, parent, **kw):
        bg = kw.pop("bg", C["bg"])
        super().__init__(parent, bg=bg, **kw)
        self._canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        vsb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.inner = tk.Frame(self._canvas, bg=bg)
        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=vsb.set)
        self._canvas.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)
        bind_mousewheel(self._canvas)

    def _on_inner_configure(self, _event) -> None:
        try:
            self._canvas.configure(scrollregion=self._canvas.bbox("all"))
        except tk.TclError:
            pass

    def _on_canvas_configure(self, event) -> None:
        try:
            self._canvas.itemconfig(self._win, width=event.width)
        except tk.TclError:
            pass


# ── 빈 상태 ───────────────────────────────────────────
# 기존 탭들이 EmptyState(icon=) 에 직접 넘기던 이모지 → 신규 아이콘 이름
# 매핑(8호 Phase 2 실측 — gui/tabs/*.py 전수 grep 결과 이 10종이 전부).
# 완벽히 들어맞는 개념이 없는 것(예: "📝" 템플릿)은 기본값으로 떨어진다 —
# 정확한 의미보다 "이모지 대신 아이콘 폰트로 통일"이 이번 라운드의 목적.
_EMPTY_STATE_ICON_MAP = {
    "📭": "folder", "🎯": "search", "📈": "growth", "🧪": "test",
    "🚨": "warning", "🔥": "trend", "🔁": "refresh", "♻️": "refresh",
    "📚": "log", "📖": "log",
}
_EMPTY_STATE_DEFAULT_ICON = "folder"


def _resolve_empty_state_glyph(icon: str) -> str:
    """icon 이 icons.ICONS 키(신규 코드)면 그 글리프, 레거시 이모지면
    매핑표(위) 로 아이콘을 골라 글리프로. 아이콘 폰트가 이 환경에 없으면
    (레거시 이모지 한정) 정보 손실 없이 원래 이모지 문자열을 그대로
    돌려준다 — widgets.py 소스 자체엔 이모지 리터럴이 없다(매핑표는 dict
    키일 뿐 화면에 새로 노출하는 문자열이 아니다)."""
    if icon in icons.ICONS:
        return icons.glyph(icon)
    if not icons.has_icon_font():
        return icon
    return icons.glyph(_EMPTY_STATE_ICON_MAP.get(icon, _EMPTY_STATE_DEFAULT_ICON))


class EmptyState(tk.Frame):
    """빈 목록/차트 자리에 놓는 안내. show()/hide() 로 토글."""

    def __init__(self, parent, icon: str, title: str, hint: str = "",
                 action_text: Optional[str] = None,
                 action_cmd: Optional[Callable] = None, **kw):
        bg = kw.pop("bg", C["card"])
        super().__init__(parent, bg=bg, **kw)
        self._visible = False
        glyph = _resolve_empty_state_glyph(icon)
        # glyph 가 실제 PUA 아이콘 문자인지(=icon_font 로 그려야 함)로 폰트를
        # 고른다 — icon 원본이 icons.ICONS 키였든 매핑표를 거친 레거시
        # 이모지였든, 최종 glyph 값 자체로 판단해야 정확하다.
        is_pua_glyph = bool(glyph) and 0xE000 <= ord(glyph[0]) <= 0xF8FF
        icon_font = (icons.icon_font(32) if (is_pua_glyph and icons.has_icon_font())
                    else ("맑은 고딕", 32))
        tk.Label(self, text=glyph, font=icon_font, fg=C["muted"],
                 bg=bg).pack(pady=(SP["xxl"], SP["sm"]))
        tk.Label(self, text=title, font=FB, fg=C["text_2"], bg=bg).pack()
        if hint:
            tk.Label(self, text=hint, font=FS, fg=C["muted"], bg=bg,
                     wraplength=px(360), justify="center").pack(pady=(SP["xs"], 0))
        if action_text and action_cmd is not None:
            SecondaryBtn(self, action_text, action_cmd).pack(pady=(SP["md"], SP["xxl"]))
        else:
            tk.Frame(self, height=SP["xxl"], bg=bg).pack()

    def show(self) -> None:
        if not self._visible:
            self.pack(fill="both", expand=True)
            self._visible = True

    def hide(self) -> None:
        if self._visible:
            self.pack_forget()
            self._visible = False


def attach_empty_state(tree, empty: EmptyState,
                        is_empty_fn: Optional[Callable[[], bool]] = None) -> Callable[[], None]:
    """tree 항목이 0개면 empty.show(), 아니면 empty.hide().

    Treeview 는 삽입/삭제 시 이벤트를 쏘지 않으므로, 항목을 채운 직후 및
    갱신할 때마다 반환된 checker 를 다시 호출하는 방식으로 쓴다.

    `DataTable`(9호 Phase 2, ttk.Treeview 대체)도 `get_children()` 을
    같은 시그니처(인자 없이 호출 시 튜플 반환)로 구현하므로 코드 변경
    없이 그대로 호환된다 — `tree` 자리에 Treeview 든 DataTable 이든
    똑같이 넘기면 된다(`tests/test_gui_data_table.py` 에서 확인).
    """
    def _check() -> None:
        try:
            is_empty = is_empty_fn() if is_empty_fn else (len(tree.get_children()) == 0)
        except tk.TclError:
            return
        if is_empty:
            empty.show()
        else:
            empty.hide()

    _check()
    return _check


# ── 상태 알약 ─────────────────────────────────────────
# 레거시 호출자(예: gui/tabs/home.py)가 이미 텍스트 맨 앞에 이모지 신호등을
# 직접 박아 넣는 관례가 있다(`lt["state"].set("🟡 확인하지 못했어요", "warn")`
# 등, 8호 Phase 2 실측 확인) — 그 탭들을 고칠 수 없는 라운드라, 이미 이런
# 접두가 있는 텍스트는 새 아이콘 글리프를 덧붙이지 않는다(이중 접두 방지 +
# tests/test_home_ux.py 의 `.startswith("🟢")` 계약 보존). 여기 나열된 문자
# 자체는 화면에 새로 노출하는 게 아니라 "이미 있는지 감지"만 하는 용도다.
_LEGACY_STATUS_PREFIXES = ("\U0001f7e2", "\U0001f7e1", "\U0001f534", "⚪")
_KIND_ICON_NAME = {"ok": "success", "success": "success", "warn": "warning",
                   "error": "close", "info": "info", "neutral": None}


def _starts_with_icon_glyph(text: str) -> bool:
    """텍스트 첫 글자가 이미 아이콘 폰트 글리프(PUA 코드포인트)인지.

    범위는 `EmptyState` 의 판정과 동일(0xE000~0xF8FF) — 이모지 서로게이트
    페어(0x1F000 대 이상, PUA 보다 수치가 더 큼)와 혼동하지 않도록 상·하한
    둘 다 확인한다(memory `korean_regex...` 류 실수 재발 방지)."""
    if not text:
        return False
    return 0xE000 <= ord(text[0]) <= 0xF8FF


def _pill_display_text(text: str, kind: str) -> str:
    """StatusPill 표시 문구 — 레거시 이모지 접두, 또는 호출부가 이미
    아이콘 폰트 글리프를 직접 붙여 넘긴 경우(둘 다) 새 글리프를 또
    붙이지 않는다.

    8호 Phase 2 캡처 실측 회귀 — `gui/tabs/home.py._iconed()` 처럼
    호출부가 자체적으로 `icons.glyph(...)` 를 접두한 뒤 `.set()` 에
    넘기는 기존 관례가 있었는데, 그 결과 문자열은 `_LEGACY_STATUS_
    PREFIXES`(옛 이모지)에 안 걸려 여기서 또 접두돼 "⚠ ⚠ 확인하지
    못했어요"처럼 이중 표시됐다. `_starts_with_icon_glyph()` 로 PUA
    글리프 접두도 함께 감지해 dedupe 한다."""
    if not text:
        return text
    if any(text.startswith(p) for p in _LEGACY_STATUS_PREFIXES):
        return text
    if _starts_with_icon_glyph(text):
        return text
    icon_name = _KIND_ICON_NAME.get(kind)
    if not icon_name or not icons.has_icon_font():
        return text
    glyph = icons.glyph(icon_name)
    if not glyph or glyph == "•":
        return text
    return f"{glyph} {text}"


class StatusPill(tk.Label):
    """kind: "ok"|"warn"|"error"|"info"|"neutral" → 배경 *_bg + 글자색.

    tk.Label 계약 유지(홈 탭의 `_lights[..]["state"]["text"]` 직접 읽기
    등을 고려해 CTkLabel 로 바꾸지 않는다 — 8호 Phase 1 결정 재확인). 알약
    형태는 좌우 패딩 10px + 낮은 세로 패딩으로 근사한다(Tk 는 진짜 라운드를
    못 그린다).

    아이콘 글리프 자동 접두는 **`set()` 호출에만** 적용한다(임무 지시 원문
    그대로) — 생성자에서도 접두하면 `gui/tabs/profiles.py` 처럼 정적 라벨을
    그대로 기대하는 기존 호출(`StatusPill(row, "현재", kind="ok")`,
    `tests/test_gui_settings.py` 가 정확히 이 텍스트를 단언)이 깨진다
    (8호 Phase 2 실측 확인 — 회귀).
    """

    def __init__(self, parent, text: str = "", kind: str = "neutral", **kw):
        colors = _kind_colors(kind)
        super().__init__(parent, text=text, font=FS,
                         fg=colors["fg"], bg=colors["bg"], padx=px(10), pady=px(3),
                         relief="flat", **kw)

    def set(self, text: str, kind: str) -> None:
        colors = _kind_colors(kind)
        self.config(text=_pill_display_text(text, kind), fg=colors["fg"], bg=colors["bg"])


# ── 진행 행 ───────────────────────────────────────────
_PROGRESS_STYLE = "GuiWidgets.Horizontal.TProgressbar"
_progress_style_for = None   # 마지막으로 스타일을 적용한 팔레트 이름(None=미적용)


def _ensure_progress_style() -> None:
    """ProgressRow 공통 스타일(팔레트당 1회 적용) — theme.configure_treeview_style
    과 동일한 가드 패턴(리뷰 🔵4): 불리언이 아니라 "적용된 팔레트 이름"으로
    가드해, `apply_palette()` 로 테마를 전환한 뒤 다시 호출하면 새 팔레트
    색으로 재적용된다."""
    global _progress_style_for
    if _progress_style_for == current_palette():
        return
    sty = ttk.Style()
    try:
        sty.theme_use("clam")
    except tk.TclError:
        pass
    sty.configure(_PROGRESS_STYLE, troughcolor=C["surface"], background=C["accent"],
                  bordercolor=C["surface"], lightcolor=C["accent"], darkcolor=C["accent"])
    _progress_style_for = current_palette()


class ProgressRow(tk.Frame):
    """라벨 + 진행 바(CTkProgressBar 가능하면 그걸, 아니면 ttk.Progressbar) +
    퍼센트 + (선택) 중지 버튼. 공개 메서드(set/reset/indeterminate)는 내부
    구현과 무관하게 동일하다 — 어느 쪽이든 호출부는 신경 쓸 필요 없다."""

    def __init__(self, parent, on_stop: Optional[Callable] = None, **kw):
        bg = kw.pop("bg", C["card"])
        super().__init__(parent, bg=bg, **kw)
        self._use_ctk = HAS_CTK
        self._label = tk.Label(self, text="", font=F_CAPTION, fg=C["text"], bg=bg,
                               anchor="w")
        self._label.pack(side="left", fill="x", expand=True)
        if self._use_ctk:
            self._bar = ctk.CTkProgressBar(self, orientation="horizontal",
                                           mode="determinate", width=px(160),
                                           height=px(6), corner_radius=R["pill"],
                                           progress_color=_theme_pair("accent"),
                                           fg_color=_theme_pair("surface"))
            self._bar.set(0)
        else:
            _ensure_progress_style()
            self._bar = ttk.Progressbar(self, orient="horizontal", mode="determinate",
                                        style=_PROGRESS_STYLE, length=px(160))
        self._bar.pack(side="left", padx=SP["sm"])
        self._pct = tk.Label(self, text="", font=FS, fg=C["text_2"], bg=bg,
                             width=5, anchor="e")
        self._pct.pack(side="left")
        if on_stop is not None:
            DangerBtn(self, "중지", on_stop).pack(side="left", padx=(SP["sm"], 0))

    def _stop_indeterminate(self) -> None:
        try:
            self._bar.stop()
        except tk.TclError:
            pass

    def set(self, cur: int, total: int, text: str = "") -> None:
        """determinate 진행률 표시. total<=0 이면 0%."""
        self._stop_indeterminate()
        total = max(int(total or 0), 0)
        cur = max(int(cur or 0), 0)
        pct = 0 if total <= 0 else min(100, round(cur * 100 / total))
        if self._use_ctk:
            self._bar.configure(mode="determinate")
            self._bar.set(min(1.0, pct / 100))
        else:
            self._bar.config(mode="determinate")
            self._bar["maximum"] = max(total, 1)
            self._bar["value"] = min(cur, max(total, 1))
        self._pct.config(text=f"{pct}%")
        self._label.config(text=text or "")

    def reset(self) -> None:
        self._stop_indeterminate()
        if self._use_ctk:
            self._bar.configure(mode="determinate")
            self._bar.set(0)
        else:
            self._bar.config(mode="determinate")
            self._bar["value"] = 0
        self._pct.config(text="")
        self._label.config(text="")

    def indeterminate(self, text: str) -> None:
        """진행률을 모를 때(수 미상 작업) — 왕복 애니메이션."""
        self._label.config(text=text or "")
        self._pct.config(text="")
        try:
            self._bar.configure(mode="indeterminate")
            self._bar.start() if self._use_ctk else self._bar.start(12)
        except tk.TclError:
            pass


# ── 툴팁 ──────────────────────────────────────────────
class Tooltip:
    """위젯 hover 700ms 후 작은 창을 띄운다. HelpIcon·버튼 등에 재사용."""

    def __init__(self, widget, text: str, delay_ms: int = 700):
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip: Optional[tk.Toplevel] = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        try:
            self._after_id = self.widget.after(self.delay_ms, self._show)
        except tk.TclError:
            pass

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except (tk.TclError, ValueError):
                pass
            self._after_id = None

    def _show(self) -> None:
        """라운드 카드(설계 v3 §5-5) + 의사 그림자 — 별도 Toplevel 을 더
        띄우는 대신(멀티 Toplevel 은 Windows 에서 z-order 가 어긋난 전례가
        있어 — `Popover` 클래스 docstring 참조) **같은 Toplevel 안에서**
        카드를 오른쪽·아래로 3px 여백을 두고 `pack()` 해, 창 배경(그림자
        색)이 그 여백만큼 비치게 근사한다 — `place(width=,height=)` 는
        CTkFrame 이 지원하지 않아(생성자 전용, 2026-09-03 실측) pack 의
        여백 방식을 쓴다."""
        if self._tip is not None:
            return
        try:
            x = self.widget.winfo_rootx() + px(12)
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + px(6)
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            try:
                tip.attributes("-topmost", True)
            except tk.TclError:
                pass
            tip.configure(bg=C["bg"])
            if HAS_CTK:
                card = ctk.CTkFrame(tip, corner_radius=R["md"],
                                    fg_color=_theme_pair("card"), border_width=px(1),
                                    border_color=_theme_pair("border_strong"))
            else:
                card = tk.Frame(tip, bg=C["card"], bd=px(1), relief="flat",
                                highlightthickness=px(1),
                                highlightbackground=C["border_strong"])
            tk.Label(card, text=self.text, font=FS, fg=C["text"], bg=C["card"],
                     padx=SP["sm"], pady=SP["xs"], wraplength=px(240),
                     justify="left").pack()
            card.pack(padx=(0, px(3)), pady=(0, px(3)))
            tip.wm_geometry(f"+{x}+{y}")
            self._tip = tip
            self._fade_in(tip)
        except tk.TclError:
            self._tip = None

    @staticmethod
    def _fade_in(win: tk.Toplevel) -> None:
        """등장 페이드 인(9호 Phase 5 — alpha 0→1, 8단계 ~120ms, ease-out).
        Toplevel 전용 속성(`-alpha`)이라 Tooltip 은 항상 이 경로다 —
        `_show()` 가 매번 새 Toplevel 을 만들므로(재사용 없음) `animate()`
        의 "재호출 시 재시작" 은 이 위젯에선 실질적으로 안 쓰인다."""
        try:
            win.attributes("-alpha", 0.0)
        except tk.TclError:
            return

        def _step(i: int, steps: int) -> None:
            t = (i + 1) / steps
            win.attributes("-alpha", ease_out_quad(t))

        animate(win, steps=8, interval_ms=15, fn=_step)

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip is not None:
            try:
                self._tip.destroy()
            except tk.TclError:
                pass
            self._tip = None


def HelpIcon(parent, text: str) -> tk.Label:
    """"ⓘ" 라벨 + Tooltip. 전문용어 설명에 쓴다."""
    lbl = tk.Label(parent, text="ⓘ", font=FS, fg=C["muted"], bg=_bg_of(parent),
                   cursor="hand2")
    Tooltip(lbl, text)
    return lbl


# ── 폼 필드 ───────────────────────────────────────────
_FORM_LABEL_WIDTH_PX = 160
_FORM_INPUT_WIDTH_PX = 480
# 행 높이(8호 Phase 2 디자이너 3차 감사 실측 원인) — CTkEntry/CTkComboBox
# 자체 높이(36)와 맞춘다. label_wrap/input_wrap 이 이 값 없이 width= 만
# 받고 pack_propagate(False) 로 잠기면, Tk Frame 의 미지정 height 기본값
# (사실상 1px)이 그대로 굳어버려 안에 넣은 CTkEntry 가 winfo_reqheight()
# 로는 36 을 보고하면서도 실제 렌더 높이는 1px 로 짜부라졌다(winfo_height()
# 실측 확인 — "위젯이 배경과 같은 색이라 안 보인다" 로 보고된 버그의 진짜
# 원인, fg_color/투명도 문제가 전혀 아니었다). width/height 둘 다 명시해야
# propagate(False) 가 "의도한 크기" 로 고정된다.
_FORM_ROW_HEIGHT_PX = 36


class FormField(tk.Frame):
    """라벨(좌, 고정폭 160px) + 입력(우, 고정폭 480px) + 힌트(아래 작은 글씨).

    kind: "entry"|"password"|"check"|"combo"|"spin"|"path"

    8호 Phase 2(설계 v3 §5-6)부터 HAS_CTK 면 kind 별로 CTk 위젯을 쓴다 —
    check→`Switch`(CTkSwitch), combo→CTkComboBox, spin→CTkEntry+±
    IconButton, path→CTkEntry+폴더 IconButton, entry/password→`Ent`
    (CTkEntry). CTk 가 없으면 전부 이전과 동일한 순정 tk 위젯으로 폴백—
    어느 kind 든 `.widget`/`.var` 계약은 그대로다.
    """

    def __init__(self, parent, label: str, var, kind: str = "entry",
                 hint: Optional[str] = None, values: Optional[Sequence] = None,
                 show_toggle: bool = False, browse_cmd: Optional[Callable] = None,
                 width: int = 32, **kw):
        bg = kw.pop("bg", _bg_of(parent))
        super().__init__(parent, bg=bg, **kw)
        self.var = var
        self.kind = kind

        row = tk.Frame(self, bg=bg)
        row.pack(fill="x")
        label_wrap = tk.Frame(row, bg=bg, width=px(_FORM_LABEL_WIDTH_PX),
                              height=px(_FORM_ROW_HEIGHT_PX))
        label_wrap.pack(side="left")
        label_wrap.pack_propagate(False)
        tk.Label(label_wrap, text=label, font=FB, fg=C["text_2"], bg=bg,
                 anchor="e", justify="right").pack(side="right")
        input_wrap = tk.Frame(row, bg=bg, width=px(_FORM_INPUT_WIDTH_PX),
                              height=px(_FORM_ROW_HEIGHT_PX))
        input_wrap.pack(side="left", padx=(SP["sm"], 0))
        input_wrap.pack_propagate(False)
        self.widget = self._build_widget(input_wrap, kind, var, values, width,
                                         show_toggle, browse_cmd, bg)

        self._hint_lbl: Optional[tk.Label] = None
        if hint:
            self._hint_lbl = tk.Label(self, text=hint, font=FS, fg=C["text_2"],
                                      bg=bg, anchor="w", justify="left")
            self._hint_lbl.pack(fill="x",
                                padx=(px(_FORM_LABEL_WIDTH_PX) + SP["sm"], 0),
                                pady=(SP["xs"], 0))

    def _build_widget(self, parent, kind, var, values, width, show_toggle,
                       browse_cmd, bg):
        if kind == "check":
            sw = Switch(parent, var=var)
            sw.pack(side="left", anchor="w")
            return sw
        if kind == "combo":
            return self._build_combo(parent, var, values, width)
        if kind == "spin":
            return self._build_spin(parent, var, values, width)
        if kind == "path":
            return self._build_path(parent, var, width, browse_cmd)
        # entry / password
        return self._build_entry(parent, var, kind, width, show_toggle)

    def _build_combo(self, parent, var, values, width):
        options = list(values or [])
        if HAS_CTK:
            # fg_color=surface_2(Ent 와 동일 근거 — 8호 Phase 2 디자이너
            # 감사 🔴1, surface 는 card/bg 와 대비가 약함).
            w = ctk.CTkComboBox(parent, variable=var, values=options,
                                width=px(_FORM_INPUT_WIDTH_PX), height=px(36),
                                corner_radius=px(8), fg_color=_theme_pair("surface_2"),
                                border_color=_theme_pair("border_strong"),
                                button_color=_theme_pair("border_strong"),
                                text_color=_theme_pair("text"), font=_ctk_font(13))
            w.pack(side="left", fill="x", expand=True)
            return w
        w = ttk.Combobox(parent, textvariable=var, values=options,
                         width=width, state="readonly")
        w.pack(side="left", fill="x", expand=True)
        return w

    def _build_spin(self, parent, var, values, width):
        lo, hi = (list(values) + [0, 100])[:2] if values else (0, 100)
        if HAS_CTK:
            entry = Ent(parent, var, w=width)
            entry.pack(side="left", fill="x", expand=True)

            def _step(delta: int):
                try:
                    current = int(var.get())
                except (tk.TclError, ValueError):
                    current = lo
                var.set(max(lo, min(hi, current + delta)))

            IconButton(parent, "remove", lambda: _step(-1),
                      tooltip="줄이기").pack(side="left", padx=(SP["xs"], 0))
            IconButton(parent, "add", lambda: _step(1),
                      tooltip="늘리기").pack(side="left")
            return entry
        w = tk.Spinbox(parent, from_=lo, to=hi, textvariable=var, width=width,
                       font=FB, fg=C["text"], bg=C["surface"], relief="flat",
                       bd=0, highlightthickness=px(1),
                       highlightbackground=C["border_strong"],
                       highlightcolor=C["focus"])
        w.pack(side="left")
        return w

    def _build_path(self, parent, var, width, browse_cmd):
        entry = Ent(parent, var, w=width)
        entry.pack(side="left", fill="x", expand=True)
        if browse_cmd is not None:
            IconButton(parent, "folder", browse_cmd,
                      tooltip="찾아보기").pack(side="left", padx=(SP["xs"], 0))
        return entry

    def _build_entry(self, parent, var, kind, width, show_toggle):
        show_char = "*" if kind == "password" else None
        entry = Ent(parent, var, show=show_char, w=width)
        entry.pack(side="left", fill="x", expand=True)
        if kind == "password" and show_toggle:
            state = {"visible": False}

            def _toggle():
                state["visible"] = not state["visible"]
                entry.config(show="" if state["visible"] else "*")
                toggle_btn.config(text=icons.glyph("view"))

            toggle_btn = SubtleBtn(parent, icons.glyph("view"), _toggle)
            toggle_btn.pack(side="left", padx=(SP["xs"], 0))
        return entry


# ── 인라인 확인 배너 ──────────────────────────────────
class InlineConfirm(tk.Frame):
    """askyesno 대체(비파괴적 확인용): 카드 안에 한 줄 배너로 확인받는다."""

    def __init__(self, parent, **kw):
        bg = kw.pop("bg", C["warn_bg"])
        super().__init__(parent, bg=bg, **kw)
        self._visible = False

    def ask(self, message: str, confirm_text: str, on_confirm: Callable,
            danger: bool = False) -> None:
        for w in self.winfo_children():
            w.destroy()
        colors = _kind_colors("error" if danger else "warn")
        bg = colors["bg"]
        self.config(bg=bg)
        tk.Frame(self, width=px(4), bg=colors["stripe"]).pack(side="left", fill="y")
        body = tk.Frame(self, bg=bg)
        body.pack(side="left", fill="both", expand=True, padx=SP["sm"], pady=SP["sm"])
        tk.Label(body, text=message, font=FB, fg=C["text"], bg=bg, anchor="w",
                 justify="left", wraplength=px(420)).pack(anchor="w")
        btn_row = tk.Frame(body, bg=bg)
        btn_row.pack(anchor="e", pady=(SP["xs"], 0))

        def _confirm():
            self.hide()
            on_confirm()

        GhostBtn(btn_row, "취소", self.hide).pack(side="left")
        confirm_factory = DangerBtn if danger else PrimaryBtn
        confirm_factory(btn_row, confirm_text, _confirm).pack(side="left",
                                                              padx=(SP["xs"], 0))
        if not self._visible:
            self.pack(fill="x", pady=(0, SP["sm"]))
            self._visible = True

    def hide(self) -> None:
        if self._visible:
            self.pack_forget()
            self._visible = False
        for w in self.winfo_children():
            w.destroy()


# ── 토스트 ────────────────────────────────────────────
class Toast:
    """우하단 스택 토스트. kind별 배경, ms 후 자동 소멸, 클릭 시 즉시 닫힘.

    워커 스레드에서 호출돼도 안전 — 메인 스레드가 아니면 `root.after(0, ...)`
    로 재위임한다. 파괴된 root 에서는 조용히 무시한다.
    """

    def __init__(self, root):
        self.root = root
        self._stack: List[tk.Toplevel] = []

    def show(self, text: str, kind: str = "info", ms: int = 4000) -> None:
        try:
            on_main = threading.current_thread() is threading.main_thread()
        except RuntimeError:
            on_main = True
        if not on_main:
            try:
                self.root.after(0, self._show_main, text, kind, ms)
            except (tk.TclError, RuntimeError):
                pass
            return
        self._show_main(text, kind, ms)

    def _show_main(self, text: str, kind: str, ms: int) -> None:
        try:
            if not self.root.winfo_exists():
                return
        except tk.TclError:
            return
        colors = _kind_colors(kind)
        try:
            win = tk.Toplevel(self.root)
            win.wm_overrideredirect(True)
            try:
                win.attributes("-topmost", True)
            except tk.TclError:
                pass
            win.configure(bg=colors["bg"])
            if HAS_CTK:
                # 카드 느낌(설계 v3 §5-5 라운드 10-12) — Toplevel 자체는
                # 각져 있지만(순정 Tk 는 창 모서리를 못 둥글림) 안쪽 카드가
                # 라운드라 겉보기엔 거의 같은 효과.
                # fg_color 는 단일 hex 가 아니라 (light,dark) 페어로(8호
                # Phase 2 디자이너 감사 🔴2) — 단일 hex 를 CTkFrame 에
                # 주면 ctk.set_appearance_mode() 로 안 바뀌어 라이트
                # 전환 중 토스트가 떠 있으면 다크 색 그대로 고정된다.
                frame = ctk.CTkFrame(win, corner_radius=R["lg"],
                                     fg_color=_kind_color_pairs(kind)["bg"],
                                     border_width=0)
                frame.pack(fill="both", expand=True, padx=px(1), pady=px(1))
            else:
                frame = tk.Frame(win, bg=colors["bg"])
                frame.pack(fill="both", expand=True)
            tk.Frame(frame, width=px(4), bg=colors["stripe"]).pack(side="left", fill="y")
            icon_glyph = icons.glyph(_KIND_ICON_NAME.get(kind, "info") or "info")
            if icon_glyph and icon_glyph != "•":
                tk.Label(frame, text=icon_glyph, font=icons.icon_font(16) or FB,
                         fg=colors["fg"], bg=colors["bg"]).pack(
                             side="left", padx=(SP["sm"], 0))
            lbl = tk.Label(frame, text=text, font=FB, fg=colors["fg"], bg=colors["bg"],
                           padx=SP["sm"], pady=SP["sm"], wraplength=px(280), justify="left")
            lbl.pack(side="left", fill="both", expand=True)
            close_btn = IconButton(frame, "close", lambda: self._dismiss(win))
            close_btn.pack(side="right", padx=(0, SP["xs"]))
            for w in (win, frame, lbl):
                w.bind("<Button-1>", lambda _e, win=win: self._dismiss(win))
        except tk.TclError:
            return
        self._stack.append(win)
        self._reflow()
        self._animate_entrance(win)
        try:
            win.after(ms, lambda: self._dismiss(win))
        except tk.TclError:
            self._dismiss(win)

    @staticmethod
    def _animate_entrance(win: tk.Toplevel) -> None:
        """등장 슬라이드(9호 Phase 5 — 아래→제자리, after 8단계 ~160ms,
        ease-out). `_reflow()` 가 이미 이 win 의 최종 위치를 잡아둔
        *뒤에* 호출된다 — 그 최종 y 를 기준으로 살짝 아래(px(24))에서
        시작해 위로 올라온다. 스택의 다른 토스트는 건드리지 않는다
        (그쪽은 이미 `_reflow()` 로 자기 최종 위치에 있음)."""
        try:
            win.update_idletasks()
            w = win.winfo_width()
            h = win.winfo_height()
            final_x = win.winfo_x()
            final_y = win.winfo_y()
        except tk.TclError:
            return
        start_y = final_y + px(24)
        try:
            win.wm_geometry(f"{w}x{h}+{final_x}+{start_y}")
        except tk.TclError:
            return

        def _step(i: int, steps: int) -> None:
            t = (i + 1) / steps
            y = round(start_y + (final_y - start_y) * ease_out_quad(t))
            win.wm_geometry(f"{w}x{h}+{final_x}+{y}")

        animate(win, steps=8, interval_ms=20, fn=_step)

    def _dismiss(self, win: tk.Toplevel) -> None:
        if win not in self._stack:
            return
        self._stack.remove(win)

        def _finish() -> None:
            try:
                win.destroy()
            except tk.TclError:
                pass
            self._reflow()

        self._animate_exit(win, _finish)

    @staticmethod
    def _animate_exit(win: tk.Toplevel, on_done) -> None:
        """퇴장 페이드(9호 Phase 5 — Toplevel 이라 `-alpha` 1→0, 8단계).
        Toast 는 항상 Toplevel 이라 이 경로만 쓴다(설계 지시의 "임베디드
        Frame 이면 슬라이드 아웃" 대체 경로는 이 위젯엔 해당 없음 — 다른
        임베디드 토스트류 위젯이 생기면 그쪽에서 별도 구현할 것)."""
        try:
            win.attributes("-alpha", 1.0)
        except tk.TclError:
            on_done()
            return

        def _step(i: int, steps: int) -> None:
            t = (i + 1) / steps
            win.attributes("-alpha", max(0.0, 1.0 - ease_out_quad(t)))

        animate(win, steps=8, interval_ms=15, fn=_step, on_done=on_done)

    def _reflow(self) -> None:
        try:
            self.root.update_idletasks()
            rx = self.root.winfo_rootx() + self.root.winfo_width()
            ry = self.root.winfo_rooty() + self.root.winfo_height()
        except tk.TclError:
            return
        y_cursor = ry - SP["xl"]
        for win in reversed(self._stack):
            try:
                win.update_idletasks()
                w = max(240, min(360, win.winfo_reqwidth()))
                h = win.winfo_reqheight() or 44
                x = rx - SP["xl"] - w
                y_cursor -= h
                win.wm_geometry(f"{w}x{h}+{x}+{y_cursor}")
                y_cursor -= SP["sm"]
            except tk.TclError:
                continue


# ── 기타 헬퍼 ─────────────────────────────────────────
def kv_row(parent, label: str, value_widget=None,
           value_text: str = "") -> Tuple[tk.Label, Any]:
    """지표 한 줄(라벨 좌 / 값 우). value_widget 이 있으면 그걸, 없으면 라벨을 만든다."""
    bg = _bg_of(parent)
    row = tk.Frame(parent, bg=bg)
    row.pack(fill="x", pady=(2, 2))
    lbl = tk.Label(row, text=label, font=FS, fg=C["text_2"], bg=bg, anchor="w")
    lbl.pack(side="left")
    if value_widget is not None:
        value_widget.pack(side="right")
        val = value_widget
    else:
        val = tk.Label(row, text=value_text, font=FB, fg=C["text"], bg=bg, anchor="e")
        val.pack(side="right")
    return lbl, val


def Banner(parent, text: str, kind: str = "info",
           action_text: Optional[str] = None,
           action_cmd: Optional[Callable] = None,
           closable: bool = True) -> tk.Frame:
    """상단 인라인 배너. 반환된 프레임은 호출자가 직접 pack 한다.

    HAS_CTK 면 라운드 카드(설계 v3 §5-5) — `ctk.CTkFrame` 은 `tk.Frame` 의
    서브클래스라(customtkinter 6.0 확인) `isinstance(banner, tk.Frame)`
    계약은 그대로 유지된다. 아이콘 글리프가 앞에 붙고, `closable=True`
    (기본값)면 우측에 닫기 아이콘 버튼이 생겨 클릭하면 배너 자신을
    지운다 — 호출자가 들고 있던 참조는 그대로 남지만(기존 코드베이스
    관례상 이후 접근은 `tk.TclError` 로 방어), 화면에서는 즉시 사라진다.
    """
    colors = _kind_colors(kind)
    if HAS_CTK:
        # fg_color 는 (light,dark) 페어로(8호 Phase 2 디자이너 감사 🔴2) —
        # Banner 는 Toast 와 달리 화면에 계속 떠 있는 위젯이라(설정 미완
        # 배너 등) 단일 hex 로 만들면 라이트 전환 후에도 다크 배경 그대로
        # 고정되는 문제가 훨씬 눈에 띈다.
        frame = ctk.CTkFrame(parent, corner_radius=R["lg"],
                             fg_color=_kind_color_pairs(kind)["bg"],
                             border_width=0)
    else:
        frame = tk.Frame(parent, bg=colors["bg"])
    tk.Frame(frame, width=px(4), bg=colors["stripe"]).pack(side="left", fill="y")
    icon_glyph = icons.glyph(_KIND_ICON_NAME.get(kind, "info") or "info")
    if icon_glyph and icon_glyph != "•":
        tk.Label(frame, text=icon_glyph, font=icons.icon_font(16) or FB,
                 fg=colors["fg"], bg=colors["bg"]).pack(
                     side="left", padx=(SP["sm"], 0))
    body = tk.Frame(frame, bg=colors["bg"])
    body.pack(side="left", fill="both", expand=True, padx=SP["sm"], pady=SP["sm"])
    tk.Label(body, text=text, font=FB, fg=colors["fg"], bg=colors["bg"], anchor="w",
             justify="left", wraplength=px(480)).pack(side="left")
    if closable:
        IconButton(frame, "close", frame.destroy).pack(side="right",
                                                        padx=(0, SP["xs"]))
    if action_text and action_cmd is not None:
        SecondaryBtn(frame, action_text, action_cmd).pack(side="right",
                                                          padx=SP["sm"], pady=SP["sm"])
    return frame


# ── 팝오버 ────────────────────────────────────────────
# 앱 전체에서 열려 있는 Popover 는 최대 1개(리뷰 🟡3) — 새 Popover 가
# show() 되면 이전에 열려 있던 걸 먼저 닫는다. 세션(프로세스) 전역이 아니라
# "동시에 화면에 뜬 팝오버는 하나"라는 UX 규칙이라 모듈 전역 변수로 충분하다.
_OPEN_POPOVER: Optional["Popover"] = None


class Popover:
    """anchor 위젯 근처에 뜨는 **Toplevel 없는** 오버레이(업그레이드1).

    `place()` 로 anchor 의 최상위 창(root) 위에 절대 배치한다 — 별도
    Toplevel 을 쓰지 않는 이유는 Windows 에서 Toplevel 의 z-order·포커스
    전환이 자잘하게 어긋나는 이력이 있어서다(기존 코드베이스의 모달들도
    같은 이유로 `askyesno` 류를 선호). anchor 클릭으로 토글되고, 바깥
    클릭이나 Esc 로 닫힌다. 동시에 열리는 Popover 는 앱 전체에서 하나뿐
    (`_OPEN_POPOVER`) — 새로 열리는 쪽이 기존 걸 자동으로 닫는다.

    바깥 클릭 감지는 `root.bind_all`(전역 bindtag) 대신 `root.bind`(root
    자신의 인스턴스 바인딩)를 쓴다 — Tk 위젯의 기본 bindtags 에는 자신이
    속한 toplevel 의 경로명이 포함되므로, root 에 건 바인딩만으로도 그
    창 안 어떤 자식을 클릭하든 이벤트를 받는다(실측 확인). 이 방식은
    `root.bind(seq, func, add="+")` 가 돌려주는 funcid 를 `root.unbind(seq,
    funcid)` 로 **정확히 그 핸들러만** 제거할 수 있어(리뷰 🟡3), `unbind_all`
    처럼 같은 시퀀스의 다른 전역 바인딩까지 몽땅 지우는 사고를 피한다.

    `build_fn(parent_frame)` 은 **열릴 때마다** 호출돼 내용을 새로 그린다
    (스냅샷 — 열려 있는 동안 값이 바뀌어도 자동 갱신되진 않는다. 갱신하려면
    `hide()` 후 `show()` 로 다시 연다).

    ⚠️ 모션 없음(9호 Phase 5 설계 지시대로 의도적 — Toplevel 이 아니라
    `place()` 기반이라 `-alpha` 를 못 쓴다. 기능은 동일, 페이드만 생략).
    """

    def __init__(self, anchor_widget: tk.Widget,
                 build_fn: Callable[[tk.Widget], None]):
        self.anchor = anchor_widget
        self.build_fn = build_fn
        self.root = anchor_widget.winfo_toplevel()
        self._frame: Optional[tk.Frame] = None
        self._shadow: Optional[tk.Frame] = None
        self._visible = False
        self._click_bind_id: Optional[str] = None
        self._escape_bind_id: Optional[str] = None
        self.anchor.bind("<Button-1>", lambda _e: self.toggle(), add="+")

    def toggle(self) -> None:
        self.hide() if self._visible else self.show()

    def show(self) -> None:
        global _OPEN_POPOVER
        if _OPEN_POPOVER is not None and _OPEN_POPOVER is not self:
            _OPEN_POPOVER.hide()  # 동시에는 하나만 — 먼저 열려 있던 걸 닫는다.
        if self._visible:
            self.hide()  # 다시 열 때마다 내용을 새로 그리기 위해 먼저 닫는다.
        if HAS_CTK:
            frame = ctk.CTkFrame(self.root, corner_radius=R["lg"],
                                 fg_color=_theme_pair("card"), border_width=px(1),
                                 border_color=_theme_pair("border_strong"))
        else:
            frame = tk.Frame(self.root, bg=C["card"], bd=0,
                             highlightthickness=px(1), highlightbackground=C["border_strong"])
        try:
            self.build_fn(frame)
        except Exception:  # noqa: BLE001 — 내용 생성 실패가 앱을 막으면 안 됨
            tk.Label(frame, text="내용을 표시하지 못했어요", font=FS,
                     fg=C["muted"], bg=C["card"]).pack(padx=SP["md"], pady=SP["md"])
        try:
            frame.update_idletasks()
            w = max(frame.winfo_reqwidth(), px(220))
            h = frame.winfo_reqheight()
            ax = self.anchor.winfo_rootx() - self.root.winfo_rootx()
            ay = self.anchor.winfo_rooty() - self.root.winfo_rooty()
            anchor_h = self.anchor.winfo_height()
            root_w = self.root.winfo_width()
        except tk.TclError:
            frame.destroy()
            return
        y = ay - h - SP["xs"]
        if y < 0:                       # 위쪽 공간이 부족하면 아래로.
            y = ay + anchor_h + SP["xs"]
        x = min(max(0, ax), max(0, root_w - w))
        # 의사 그림자(설계 v3 §5-5) — 카드보다 살짝 오른쪽·아래로 어긋난
        # 어두운 색 프레임을 먼저 놓고, 그 위에 카드를 놓는다. 진짜 반투명
        # 그림자는 순정 Tk 로 못 그리므로(알파 없음) "테두리 밖으로 살짝
        # 비치는 어두운 판"으로 근사한다 — 실패해도 카드 자체는 뜬다.
        shadow = None
        try:
            shadow = tk.Frame(self.root, bg=C["bg"])
            shadow.place(x=x + px(3), y=y + px(3), width=w, height=h)
        except tk.TclError:
            shadow = None
        try:
            if HAS_CTK:
                # CTkFrame.place() 는 width/height 를 안 받는다(생성자
                # 전용, 2026-09-03 실측) — configure() 로 먼저 크기를
                # 확정한 뒤 위치만 place() 한다.
                frame.configure(width=w, height=h)
                frame.place(x=x, y=y)
            else:
                frame.place(x=x, y=y, width=w, height=h)
        except tk.TclError:
            frame.destroy()
            if shadow is not None:
                shadow.destroy()
            return
        self._frame = frame
        self._shadow = shadow
        self._visible = True
        _OPEN_POPOVER = self
        # root 자신에 건 인스턴스 바인딩 — funcid 를 보관해 hide() 에서
        # 정확히 이 핸들러만 제거한다(클래스 docstring 참조).
        self._click_bind_id = self.root.bind("<Button-1>", self._on_outside_click, add="+")
        self._escape_bind_id = self.root.bind("<Escape>", lambda _e: self.hide(), add="+")

    def _on_outside_click(self, event) -> None:
        if self._frame is None:
            return
        widget = getattr(event, "widget", None)
        while widget is not None:
            if widget in (self._frame, self.anchor, self._shadow):
                return  # 팝오버 안(또는 anchor·그림자) 클릭 — 무시.
            widget = getattr(widget, "master", None)
        self.hide()

    def hide(self) -> None:
        global _OPEN_POPOVER
        if not self._visible:
            return
        self._visible = False
        if self._frame is not None:
            try:
                self._frame.destroy()
            except tk.TclError:
                pass
            self._frame = None
        if self._shadow is not None:
            try:
                self._shadow.destroy()
            except tk.TclError:
                pass
            self._shadow = None
        if self._click_bind_id is not None:
            try:
                self.root.unbind("<Button-1>", self._click_bind_id)
            except tk.TclError:
                pass
            self._click_bind_id = None
        if self._escape_bind_id is not None:
            try:
                self.root.unbind("<Escape>", self._escape_bind_id)
            except tk.TclError:
                pass
            self._escape_bind_id = None
        if _OPEN_POPOVER is self:
            _OPEN_POPOVER = None

    def is_visible(self) -> bool:
        return self._visible


# ── 세그먼트 버튼(신규, auto-dev 8호) ──────────────────
class Segmented(tk.Frame):
    """세그먼트 버튼 — CTkSegmentedButton(있으면) 또는 버튼 그룹 근사.

    설정·콘텐츠 탭의 "다시 쓰기/시리즈/템플릿" 류 모드 전환에 쓴다.
    `var`(tk.StringVar) 를 안 주면 내부에서 하나 만든다. `get()`/`set(value)`
    로 현재 선택을 읽고/바꾼다 — 내부 구현(ctk 유무)과 무관하게 동일하다.
    """

    def __init__(self, parent, values: Sequence[str], var=None,
                 command: Optional[Callable[[str], None]] = None, **kw):
        bg = kw.pop("bg", _bg_of(parent))
        super().__init__(parent, bg=bg, **kw)
        self._values = list(values)
        self.var = var if var is not None else tk.StringVar(
            value=self._values[0] if self._values else "")
        self._buttons: Dict[str, tk.Button] = {}
        self._command = command

        if HAS_CTK:
            def _on_change(value):
                self.var.set(value)
                if self._command:
                    self._command(value)

            self._widget = ctk.CTkSegmentedButton(
                self, values=self._values, command=_on_change,
                selected_color=_theme_pair("accent"),
                selected_hover_color=_theme_pair("accent_hover"),
                unselected_color=_theme_pair("btn_secondary"),
                unselected_hover_color=_theme_pair("card_hover"),
                text_color=_theme_pair("text"), font=_ctk_font(13))
            if self.var.get():
                self._widget.set(self.var.get())
            self._widget.pack(fill="x")
        else:
            row = tk.Frame(self, bg=bg)
            row.pack(fill="x")
            for value in self._values:
                b = tk.Button(row, text=value, font=FS, relief="flat",
                              cursor="hand2", padx=SP["sm"], pady=px(4), bd=0,
                              command=lambda v=value: self._select_tk(v))
                b.pack(side="left", padx=(0, SP["xs"]))
                self._buttons[value] = b
            self._refresh_tk_buttons()

    def _select_tk(self, value: str) -> None:
        self.var.set(value)
        self._refresh_tk_buttons()
        if self._command:
            self._command(value)

    def _refresh_tk_buttons(self) -> None:
        active = self.var.get()
        for value, btn in self._buttons.items():
            if value == active:
                btn.config(bg=C["accent"], fg=C.get("on_accent", C["bg"]))
            else:
                btn.config(bg=C["btn_secondary"], fg=C["text"])

    def get(self) -> str:
        return self.var.get()

    def set(self, value: str) -> None:
        self.var.set(value)
        if HAS_CTK:
            self._widget.set(value)
        else:
            self._refresh_tk_buttons()


# ── 스위치(신규, auto-dev 8호) ─────────────────────────
class Switch(tk.Frame):
    """토글 스위치 — CTkSwitch(있으면) 또는 Checkbutton 근사.

    `var`(tk.BooleanVar) 를 안 주면 내부에서 만든다. `get()`/`set(bool)`.
    """

    def __init__(self, parent, var=None, text: str = "",
                 command: Optional[Callable[[], None]] = None, **kw):
        bg = kw.pop("bg", _bg_of(parent))
        super().__init__(parent, bg=bg, **kw)
        self.var = var if var is not None else tk.BooleanVar(value=False)

        if HAS_CTK:
            self._widget = ctk.CTkSwitch(
                self, text=text, variable=self.var, onvalue=True, offvalue=False,
                command=command, progress_color=_theme_pair("accent"),
                button_color=_theme_pair("card"), fg_color=_theme_pair("border"),
                text_color=_theme_pair("text"), font=_ctk_font(13))
        else:
            self._widget = tk.Checkbutton(
                self, text=text, variable=self.var, bg=bg, activebackground=bg,
                selectcolor=C["surface"], fg=C["text"], font=FB, bd=0,
                highlightthickness=0, command=command)
        self._widget.pack(anchor="w")

    def get(self) -> bool:
        return bool(self.var.get())

    def set(self, value: bool) -> None:
        self.var.set(bool(value))


def IconButton(parent, icon: str, cmd: Callable, tooltip: Optional[str] = None,
              **kw):
    """32px 정사각 아이콘 전용 버튼(텍스트 없음) — 툴바·헤더 액션류에 쓴다.

    아이콘 폰트가 없으면 "•" 텍스트 폴백(icons.glyph 계약 그대로).
    """
    glyph = icons.glyph(icon)
    if HAS_CTK:
        # 아이콘 글리프 자체는 body 폰트(FONT_FAMILY)로 못 그린다(PUA
        # 코드포인트가 그 폰트엔 없음) — family 는 아이콘 폰트(있으면)를
        # 그대로 쓰되, 픽셀 고정은 다른 CTk 위젯과 동일하게 `_ctk_font`
        # 로(8호 Phase 2 캡처 실측 — Subtle/IconButton 은 12).
        icon_family = icons.ICON_FONT_FAMILY or FONT_FAMILY
        btn = ctk.CTkButton(parent, text=glyph, command=cmd, width=px(32),
                            height=px(32), corner_radius=px(8),
                            fg_color="transparent",
                            hover_color=_theme_pair("card_hover"),
                            text_color=_theme_pair("muted"),
                            font=_ctk_font(12, family=icon_family), **kw)
    else:
        btn = tk.Button(parent, text=glyph, command=cmd,
                        font=icons.icon_font(16) or FB, fg=C["muted"], bg=C["card"],
                        activebackground=C["card_hover"], relief="flat",
                        cursor="hand2", width=2, height=1, bd=0,
                        highlightthickness=0, **kw)
    if tooltip:
        Tooltip(btn, tooltip)
    return btn


# ── 단계 표시기(신규, auto-dev 8호, 8호 Phase 2 캡처 실측 원형 전환) ──
_STEP_DOT_SIZE = 22


class _StepDot:
    """`StepIndicator` 배지 1개 — Canvas 원(oval)+텍스트 아이템 위의 얇은
    래퍼. 기존 `tk.Label` 기반 `_dots` 가 갖던 `.cget("text")` 계약(회귀
    가드 테스트가 이걸로 완료 여부를 읽는다)을 그대로 유지하면서 실제
    렌더는 사각 배지 → 원형으로 바꾼다(Tk 는 위젯 자체를 원형으로 못
    그리므로 Canvas 아이템으로 근사 — 진짜 원)."""

    def __init__(self, canvas: tk.Canvas, oval_id: int, text_id: int):
        self.canvas = canvas
        self.oval_id = oval_id
        self.text_id = text_id

    def cget(self, key: str):
        if key == "text":
            return self.canvas.itemcget(self.text_id, "text")
        return self.canvas.cget(key)

    def config(self, *, text: Optional[str] = None, fill: Optional[str] = None,
              outline: Optional[str] = None, text_fill: Optional[str] = None) -> None:
        if fill is not None or outline is not None:
            kw: Dict[str, str] = {}
            if fill is not None:
                kw["fill"] = fill
            if outline is not None:
                kw["outline"] = outline
            self.canvas.itemconfigure(self.oval_id, **kw)
        if text is not None:
            self.canvas.itemconfigure(self.text_id, text=text)
        if text_fill is not None:
            self.canvas.itemconfigure(self.text_id, fill=text_fill)


class StepIndicator(tk.Frame):
    """1-2-3 단계 표시 — 사진/상품 탭의 입력→미리보기→발행 흐름에 쓴다.

    지난 단계는 완료(초록 채움+체크 글리프), 현재 단계는 accent 채움+흰
    숫자, 이후 단계는 테두리만(채움 없음)+muted 숫자. 배지는 22px 원형
    (`tk.Canvas` 오벌 — Tk 위젯 자체엔 원형이 없어 Canvas 아이템으로
    그린다), 연결선은 2px.
    """

    def __init__(self, parent, steps: Sequence[str], **kw):
        bg = kw.pop("bg", _bg_of(parent))
        super().__init__(parent, bg=bg, **kw)
        self._steps = list(steps)
        self._active = 0
        self._bg = bg
        self._dots: List[_StepDot] = []
        self._labels: List[tk.Label] = []
        # _STEP_DOT_SIZE 는 100% DPI 기준값(22) — 실제 Canvas 지오메트리는
        # px() 적용값(9호 Phase 1)을 쓴다.
        dot_size = px(_STEP_DOT_SIZE)
        row = tk.Frame(self, bg=bg)
        row.pack(fill="x")
        for i, text in enumerate(self._steps):
            if i > 0:
                tk.Frame(row, width=px(24), height=px(2), bg=C["border"]).pack(
                    side="left", pady=(dot_size // 2, 0))
            cell = tk.Frame(row, bg=bg)
            cell.pack(side="left")
            canvas = tk.Canvas(cell, width=dot_size, height=dot_size,
                               bg=bg, highlightthickness=0, bd=0)
            canvas.pack()
            oval_id = canvas.create_oval(px(1), px(1), dot_size - px(1),
                                         dot_size - px(1),
                                         fill=bg, outline=C["border"], width=px(1))
            text_id = canvas.create_text(dot_size / 2, dot_size / 2,
                                         text=str(i + 1), font=FS, fill=C["muted"])
            lbl = tk.Label(cell, text=text, font=FS, fg=C["muted"], bg=bg)
            lbl.pack()
            self._dots.append(_StepDot(canvas, oval_id, text_id))
            self._labels.append(lbl)
        self.set_active(0)

    def set_active(self, index: int) -> None:
        """index(0-base) 단계를 "현재"로 표시하고 이전/이후를 재도색한다."""
        self._active = max(0, min(index, len(self._steps) - 1)) if self._steps else 0
        on_accent = C.get("on_accent", C["bg"])
        for i, (dot, lbl) in enumerate(zip(self._dots, self._labels)):
            if i < self._active:
                dot.config(text=icons.glyph("success"), fill=C["green"],
                          outline=C["green"], text_fill=on_accent)
                lbl.config(fg=C["text_2"])
            elif i == self._active:
                dot.config(text=str(i + 1), fill=C["accent"],
                          outline=C["accent"], text_fill=on_accent)
                lbl.config(fg=C["text"])
            else:
                dot.config(text=str(i + 1), fill=self._bg,
                          outline=C["border"], text_fill=C["muted"])
                lbl.config(fg=C["muted"])

    def active(self) -> int:
        return self._active
