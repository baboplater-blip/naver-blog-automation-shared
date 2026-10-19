# gui/tabs/_publish_section.py — 공용 "네이버에 발행" 섹션 (신규, GUI 개편 2026-09-02)
"""photo_post·product_post 가 공유하는 3단계 흐름의 마지막 단계(발행) UI.

두 탭이 각자 그리던 대상 메뉴 콤보·힌트 라벨·발행 버튼·상태 표시를 한
`SectionCard` 로 묶어 중복을 없앤다. 소유 탭은 설계서 §0.2 가 요구하는
기존 속성명을 그대로 노출해야 한다 — 예:
    self.publish = PublishSection(body, app, self._on_publish)
    self.cat_var = self.publish.cat_var
    self.cat_combo = self.publish.cat_combo
    self.publish_btn = self.publish.publish_btn
    self.cat_hint_lbl = self.publish.cat_hint_lbl

에러 문구는 `gui/humanize.py` 의 `publish_error_message(code)` 로 통일한다
(과거 photo_post·product_post 가 각자 들고 있던 `_PUBLISH_ERROR_MSG` 사전을
여기서 흡수). 그 사전을 직접 참조하던 코드가 남아 있을 가능성에 대비해
`_PUBLISH_ERROR_MSG` 이름의 하위호환 별칭도 함께 내보낸다(같은 dict 객체).
"""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk
from typing import Callable, Optional, Sequence

from gui import icons
from gui import theme as _theme_mod
from gui.theme import C, FS, HAS_CTK, PrimaryBtn, R, SP, _ctk_font
from gui.theme import _pair as _theme_pair  # noqa: PLC2701 — theme<->tabs 상호 참조는 기존 관례(gui/widgets.py 와 동일)
from gui.widgets import SectionCard, StatusPill
from gui.humanize import PUBLISH_ERROR_MSG as _PUBLISH_ERROR_MSG  # noqa: F401 — 하위호환 별칭
from gui.humanize import publish_error_message  # noqa: F401 — 소유 탭이 재-import 하기도 함

# 9호 Phase 1(고DPI) — theme.px() 가 아직 없을 수 있는 병렬 개발 상황을
# 대비한 방어적 폴백(코더 A 작업 중). 있으면 UI_SCALE 배율 적용, 없으면 항등.
px = getattr(_theme_mod, "px", lambda n: n)

if HAS_CTK:
    import customtkinter as ctk


# CTkComboBox.configure() 가 실제로 받는 옵션만 화이트리스트로 통과시킨다 —
# gui/theme.py 의 _CTK_BUTTON_SAFE_KEYS/_CTK_FRAME_SAFE_KEYS 와 동일한 이유
# (리뷰 🟡): 탭 코드가 `combo.config(width=..., relief=...)` 처럼 tk 전용
# 옵션을 섞어 부를 가능성에 대비 — CTkComboBox 는 모르는 키에 ValueError 를
# 던지므로, 크래시 대신 "그 옵션만 조용히 버림" 쪽을 택한다.
_CTK_COMBOBOX_SAFE_KEYS = frozenset({
    "values", "state", "font", "fg_color", "border_color", "text_color",
    "command", "variable", "width", "height",
})


class _CtkComboBoxAdapter:
    """CTkComboBox 위의 얇은 어댑터 — `combo["values"] = [...]`/`combo["values"]`/
    `combo["state"] = ...` 대괄호 계약을 유지한다.

    customtkinter 6.0 의 CTkComboBox.cget()/configure() 자체는 "values"·"state"
    를 정확히 처리하지만(실측 확인), `tk.Misc` 가 제공하는 대괄호 문법
    (`widget["key"]`/`widget["key"] = value`)은 Tcl 수준 cget/configure 커맨드를
    직접 호출해 파이썬 레벨 오버라이드를 우회한다 — 실측: `combo["values"]` 는
    TclError(`unknown option "-values"`), `combo["state"] = "disabled"` 는
    예외 없이 조용히 무시된다(진짜 상태는 안 바뀜 — 침묵 실패라 더 위험하다).
    `gui/theme.py` 의 `_CtkButtonAdapter`/`_CtkFrameAdapter` 와 동일한 원인·
    동일한 우회(진짜 위젯의 `.cget()`/`.configure()` 메서드로 재위임)다.
    """

    def __init__(self, combobox):
        self._combo = combobox

    def cget(self, key):
        return self._combo.cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def configure(self, **kwargs):
        safe = {k: v for k, v in kwargs.items() if k in _CTK_COMBOBOX_SAFE_KEYS}
        if safe:
            self._combo.configure(**safe)

    config = configure

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def __getattr__(self, name):
        return getattr(self._combo, name)

# _set_status(text, color) 가 넘기는 theme 헥스색 → StatusPill kind 매핑.
# 기존 photo_post/product_post 의 _set_status 호출부가 전부 C[...] 헥스색을
# 그대로 넘기므로(문자열 하드코딩 X), 색 상수 자체를 키로 쓰는 게 가장 안전하다.
def _color_to_kind() -> dict:
    """호출 시점의 C 값으로 만든다(라이트 테마 전환 대응 — import 시점 고정 금지)."""
    return {
        C["green"]:  "ok",
        C["red"]:    "error",
        C["orange"]: "warn",
        C["yellow"]: "warn",
        C["accent"]: "info",
        C["muted"]:  "neutral",
    }


def status_kind_for_color(color: Optional[str]) -> str:
    """theme 헥스색 → StatusPill kind. 모르는 색은 "neutral"(예외 없음)."""
    return _color_to_kind().get(color, "neutral")


def notify(app, fallback_title: str, text: str, kind: str = "info") -> None:
    """`app.toast` 가 있으면 토스트로, 없으면 정보성 messagebox 로 폴백한다.

    파괴적 확인(askyesno)·예외 에러(showerror)는 이 함수를 쓰지 않는다 —
    호출부가 직접 `messagebox.askyesno`/`showerror` 를 써야 한다(설계서
    §1.2 — 정보성 알림만 토스트 대상).
    """
    toast = getattr(app, "toast", None)
    if toast is not None:
        try:
            toast.show(text, kind=kind)
            return
        except Exception:  # noqa: BLE001 — 토스트 실패가 알림 자체를 막으면 안 됨
            pass
    if kind == "warn":
        messagebox.showwarning(fallback_title, text)
    else:
        messagebox.showinfo(fallback_title, text)


class PublishSection(SectionCard):
    """"N. 네이버에 발행" 섹션 — 대상 메뉴 콤보 + 힌트 + 발행 버튼 + 상태 알약."""

    def __init__(self, parent, app, on_publish: Callable[[], None],
                 title: str = "3. 네이버에 발행", **kw):
        subtitle = kw.pop(
            "subtitle",
            "엉뚱한 메뉴로 올라가지 않도록 대상 메뉴를 직접 골라야 해요.")
        super().__init__(parent, title, subtitle=subtitle, icon=icons.glyph("publish"), **kw)
        self.app = app
        self._on_publish = on_publish

        row = tk.Frame(self.body, bg=C["card"])
        row.pack(fill="x", pady=(0, px(6)))
        tk.Label(row, text="대상 메뉴:", font=FS, fg=C["text_2"],
                 bg=C["card"]).pack(side="left", padx=(0, SP["sm"]))
        self.cat_var = tk.StringVar()
        self.cat_combo = self._build_category_combo(row)
        self.cat_combo.pack(side="left", padx=(0, SP["md"]))
        self.publish_btn = PrimaryBtn(row, "네이버에 발행하기", self._handle_publish,
                                      icon=icons.glyph("publish"))
        self.publish_btn.pack(side="left")
        self.publish_btn.config(state="disabled")

        self.cat_hint_lbl = tk.Label(self.body, text="", font=FS, fg=C["text_2"],
                                     bg=C["card"], anchor="w", justify="left",
                                     wraplength=px(520))
        self.cat_hint_lbl.pack(anchor="w", pady=(px(4), 0))

        self.status_pill = StatusPill(self.body, "발행 대기", kind="neutral")
        self.status_pill.pack(anchor="w", pady=(SP["sm"], 0))

    def _build_category_combo(self, parent):
        """대상 메뉴 콤보 — HAS_CTK 면 CTkComboBox(디자이너 심사 🔴5: 스킨 안 된
        흰색 네이티브 ttk.Combobox 지적) + `_CtkComboBoxAdapter`, 아니면 기존
        순정 ttk.Combobox. 어느 쪽이든 `["values"]`/`.get()`/`.set()`/`cat_var`
        연동 계약은 동일하다(설계서 §5-7: 높이 36·corner_radius 6·fg_color
        surface_2 페어·border_color border_strong 페어·폰트 13)."""
        if HAS_CTK:
            combo = ctk.CTkComboBox(
                parent, variable=self.cat_var, values=[], width=px(220),
                height=px(36), corner_radius=R["sm"], state="disabled",
                fg_color=_theme_pair("surface_2"),
                border_color=_theme_pair("border_strong"),
                button_color=_theme_pair("border_strong"),
                button_hover_color=_theme_pair("accent_hover"),
                dropdown_fg_color=_theme_pair("surface_3"),
                dropdown_hover_color=_theme_pair("accent_soft"),
                dropdown_text_color=_theme_pair("text"),
                text_color=_theme_pair("text"), font=_ctk_font(13))
            return _CtkComboBoxAdapter(combo)
        return ttk.Combobox(parent, textvariable=self.cat_var,
                            state="disabled", width=28, font=FS)

    def _handle_publish(self) -> None:
        self._on_publish()

    # ── 소유 탭이 호출하는 갱신 API(설계서 §0.5) ──────────────────────
    def set_enabled(self, enabled: bool) -> None:
        """발행 버튼만 토글(콤보 상태는 `set_combo_state` 가 별도 관리)."""
        try:
            self.publish_btn.config(state="normal" if enabled else "disabled")
        except tk.TclError:
            pass

    def set_hint(self, text: str, kind: str = "neutral") -> None:
        colors = {"ok": C["green"], "warn": C["orange"], "error": C["red"],
                  "neutral": C["text_2"]}
        try:
            self.cat_hint_lbl.config(text=text, fg=colors.get(kind, C["text_2"]))
        except tk.TclError:
            pass

    def load_categories(self, labels: Sequence[str]) -> None:
        """저장된 블로그 메뉴 목록을 콤보에 채운다(빈 목록이면 직접 입력 허용).

        photo_post/product_post 가 각자 갖고 있던 `_apply_menus` 로직을
        그대로 이관 — 저장된 메뉴가 있으면 목록 선택만(오발행 방지), 없으면
        첫 발행이라 직접 입력을 허용한다.
        """
        labels = list(labels or [])
        try:
            self.cat_combo["values"] = labels
        except tk.TclError:
            return
        if labels:
            self.set_hint("위 목록에서 이 글을 올릴 메뉴를 고르세요.", "neutral")
        else:
            self.set_hint(
                "저장된 블로그 메뉴가 없어요 — 메뉴 이름을 정확히 입력해 주세요"
                "(첫 발행 후 자동으로 기억해요). 이름이 다르면 기본 메뉴로 올라갈 "
                "수 있어요.", "warn")

    def set_state(self, can_publish: bool, busy: bool) -> None:
        """발행 버튼·콤보 상태를 (can_publish, busy) 로 일괄 갱신.

        두 탭의 `_refresh_actions` 가 각자 들고 있던 분기(저장된 메뉴가 있으면
        readonly, 없으면 직접 입력)를 여기로 모았다.
        """
        self.set_enabled(can_publish and not busy)
        try:
            if not can_publish or busy:
                self.cat_combo.config(state="disabled")
            elif self.cat_combo["values"]:
                self.cat_combo.config(state="readonly")
            else:
                self.cat_combo.config(state="normal")
        except tk.TclError:
            pass

    def set_status(self, text: str, kind: str = "neutral") -> None:
        try:
            self.status_pill.set(text, kind)
        except tk.TclError:
            pass

    def selected_category(self) -> str:
        return self.cat_var.get().strip()

    def known_menus(self) -> list:
        try:
            return [str(v) for v in (self.cat_combo["values"] or ())]
        except tk.TclError:
            return []
