# gui/tour.py — 첫 실행 코치마크 투어 (신규, auto-dev 9호)
"""처음 앱을 켠 사용자에게 핵심 위치 4곳을 순서대로 짚어 주는 코치마크 안내.

전체 화면을 덮는 반투명 오버레이 대신(Tk 알파 오버레이는 비용이 크고
CTk 위젯과 충돌 이력이 있다 — ``gui.widgets.Popover`` 의 설계 노트와 같은
이유), 대상 위젯 옆에 작은 카드를 띄우고 대상 위젯 둘레를 accent 색 얇은
테두리로 감싸는 방식을 쓴다. 카드는 ``gui.widgets.StepIndicator`` 를 그대로
재사용한다(단계 점 표시).

``gui.widgets.Popover`` 를 그대로 재사용하지 않고 이 파일 안에 최소 구현을
둔 이유 — ``Popover`` 는 생성자에서 anchor 위젯의 ``<Button-1>`` 을 무조건
가로채 ``toggle()`` 을 건다. 투어 대상은 실제 기능이 있는 버튼("오늘 실행"
등)이라 그 바인딩이 투어가 끝난 뒤에도 anchor 에 영구히 남으면, 사용자가
나중에 그 버튼을 정상적으로 누를 때마다 낡은 팝오버 인스턴스가 같이
토글되는 부작용이 생긴다. 여기서는 순수하게 ``next()``/``skip()`` 버튼
클릭으로만 진행하고, 단계마다 그려 넣은 위젯(테두리 4조각 + 카드)을 다음
단계로 넘어가기 전에 전부 파괴해 아무 바인딩도 남기지 않는다.

대상 위젯은 각 단계의 ``get_target(tour)`` 콜백이 실제 속성 경로로 조회한다
(``app.pages["home"].run_btn`` 등). 속성이 없거나 위젯이 파괴됐으면 그 콜백은
``None`` 을 돌려주고, ``Tour`` 는 그 단계를 건너뛴다(fail-open) — 탭 구성이
바뀌거나 FakeApp 처럼 속성이 아예 없는 테스트 환경에서도 예외 없이 끝까지
넘어간다.
"""
from __future__ import annotations

import logging
import tkinter as tk
from typing import Callable, List, Optional, Sequence

from gui import theme as _theme
from gui.theme import C, FB, FT, SP
from gui.theme import PrimaryBtn, SubtleBtn
from gui.widgets import StepIndicator

log = logging.getLogger(__name__)

# auto-dev 9호 Phase 1(고DPI) — 다른 파일들과 같은 관례: theme.px() 가 아직
# 없는 이전 라운드에서도 항등함수로 안전하게 폴백한다.
px = getattr(_theme, "px", lambda n: n)

_RING_THICKNESS = 2
_RING_PAD = 4
_CARD_WRAP = 260


class TourStep:
    """투어 한 단계 — 제목·설명 + 대상 위젯을 찾는 콜백."""

    __slots__ = ("title", "body", "get_target", "on_show")

    def __init__(self, title: str, body: str,
                 get_target: Callable[["Tour"], Optional[tk.Widget]],
                 on_show: Optional[Callable[["Tour"], None]] = None):
        self.title = title
        self.body = body
        self.get_target = get_target
        self.on_show = on_show


def _home_tab(tour: "Tour"):
    pages = getattr(tour.app, "pages", None)
    if not isinstance(pages, dict):
        return None
    return pages.get("home")


def _run_button_target(tour: "Tour") -> Optional[tk.Widget]:
    return getattr(_home_tab(tour), "run_btn", None)


def _queue_light_target(tour: "Tour") -> Optional[tk.Widget]:
    """홈 탭 "발행 준비" 신호등 카드 — ``_lights["queue"]["state"]`` 의
    상태 배지가 아니라 그 배지를 담은 카드(``.master``)를 강조한다(상태
    글자 하나만 감싸면 너무 작다)."""
    lights = getattr(_home_tab(tour), "_lights", None)
    if not isinstance(lights, dict):
        return None
    entry = lights.get("queue")
    if not isinstance(entry, dict):
        return None
    widget = entry.get("state")
    if widget is None:
        return None
    return getattr(widget, "master", widget)


def _sidebar_more_target(tour: "Tour") -> Optional[tk.Widget]:
    return getattr(tour.app, "_adv_toggle_btn", None)


def _settings_ops_segment_target(tour: "Tour") -> Optional[tk.Widget]:
    pages = getattr(tour.app, "pages", None)
    settings = pages.get("settings") if isinstance(pages, dict) else None
    segment_btns = getattr(settings, "_segment_btns", None)
    if not isinstance(segment_btns, dict):
        return None
    return segment_btns.get("자동화·진단")


def _show_home(tour: "Tour") -> None:
    tour._show_tab_safely("home")


def _show_settings(tour: "Tour") -> None:
    tour._show_tab_safely("settings")


def default_steps() -> List[TourStep]:
    """기본 투어 4단계(설계서 지침 순서 그대로): 오늘 실행 → 신호등 →
    더 보기 → 설정."""
    return [
        TourStep(
            "오늘 실행 버튼",
            "이 버튼 하나로 오늘 발행할 글을 준비해요. 눌러 보기 전에 "
            "아래 상태부터 같이 살펴볼게요.",
            _run_button_target, on_show=_show_home,
        ),
        TourStep(
            "발행 준비 상태",
            "지금 발행할 준비가 됐는지 색으로 보여줘요. 빨간불이면 무엇을 "
            "해야 하는지도 바로 안내해요.",
            _queue_light_target, on_show=_show_home,
        ),
        TourStep(
            "더 보기",
            "사진으로 글쓰기, 성장 리포트 같은 기능은 여기 접혀 있어요. "
            "눌러서 펼쳐 보세요.",
            _sidebar_more_target,
        ),
        TourStep(
            "설정",
            "네이버 계정, 발행 방식, 화면 테마 같은 걸 여기서 바꿀 수 "
            "있어요.",
            _settings_ops_segment_target, on_show=_show_settings,
        ),
    ]


class Tour:
    """앱 위 코치마크 투어. ``start()`` 로 첫 단계부터 시작하고, 카드의
    "다음"/"건너뛰기" 버튼이 각각 ``next_step()``/``skip()`` 을 부른다.

    스레드를 전혀 쓰지 않는다 — 전부 메인 스레드에서 버튼 클릭에 반응해
    동기적으로 진행되므로, ``AppLifecycle.ui_after`` 예약 취소 규약이 적용될
    타이머 자체가 없다(투어를 예약하는 쪽인 ``App._maybe_start_tour`` 의
    ``ui_after(delay=...)`` 만 그 규약을 따르면 된다).
    """

    def __init__(self, app, steps: Optional[Sequence[TourStep]] = None):
        self.app = app
        self.root = app.root
        self._steps = list(steps) if steps is not None else default_steps()
        self._index = -1
        self._active = False
        self._card: Optional[tk.Frame] = None
        self._ring: List[tk.Widget] = []

    # ── 공개 API ─────────────────────────────────────────
    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._index = -1
        self._advance()

    def next_step(self) -> None:
        self._advance()

    def skip(self) -> None:
        self.finish()

    def is_active(self) -> bool:
        return self._active

    def finish(self) -> None:
        """투어를 끝낸다 — 화면 정리 + ``ui_prefs.tour_done`` 저장 + 홈으로
        복귀. ``skip()``/마지막 단계 이후 자동 종료 둘 다 여기로 모인다."""
        was_active = self._active
        self._clear_visuals()
        self._active = False
        if was_active:
            try:
                from gui import ui_prefs
                ui_prefs.set("tour_done", True)
            except Exception:  # noqa: BLE001 — 취향 저장 실패가 투어 종료를 막으면 안 된다
                log.debug("[tour] tour_done 저장 실패(무해)", exc_info=True)
        self._show_tab_safely("home")

    def cancel(self) -> None:
        """진행 상태(``tour_done``)는 건드리지 않고 화면만 정리한다 — 창을
        닫을 때 등, "끝냈다"고 기록하면 안 되는 중단에 쓴다."""
        self._clear_visuals()
        self._active = False

    # ── 내부 ─────────────────────────────────────────────
    def _show_tab_safely(self, key: str) -> None:
        show_tab = getattr(self.app, "show_tab", None)
        if callable(show_tab):
            try:
                show_tab(key)
            except Exception:  # noqa: BLE001 — FakeApp 등 탭이 없을 수 있다
                log.debug("[tour] show_tab(%r) 실패(무해)", key, exc_info=True)

    @staticmethod
    def _widget_alive(widget) -> bool:
        try:
            return bool(widget.winfo_exists())
        except (tk.TclError, AttributeError):
            return False

    def _measure(self, widget):
        """대상 위젯의 (루트 기준 x, y, 폭, 높이).

        폭·높이는 ``winfo_width()``/``winfo_height()`` 대신 ``winfo_reqwidth()``/
        ``winfo_reqheight()`` 를 쓴다 — 전자는 창이 실제로 화면에 매핑된
        뒤에야 값이 채워져(withdraw() 된 헤드리스 테스트 창에서는 위젯을
        아무리 pack 해도 영원히 1×1 로 남는다, 실측 확인) 정상적인 라이브
        창에서도 첫 프레임 타이밍에 따라 불안정할 수 있다. 후자는 위젯이
        요청하는 "자연스러운" 크기를 pack 결과만으로 즉시 계산해 매핑
        여부와 무관하다 — ``widgets.Popover.show()`` 도 카드 자체 크기를
        잴 때 같은 방식을 쓴다.
        """
        try:
            self.root.update_idletasks()
            x = widget.winfo_rootx() - self.root.winfo_rootx()
            y = widget.winfo_rooty() - self.root.winfo_rooty()
            w = widget.winfo_reqwidth()
            h = widget.winfo_reqheight()
        except (tk.TclError, AttributeError):
            return None
        if w <= 1 or h <= 1:
            return None
        return x, y, w, h

    def _advance(self) -> None:
        while True:
            self._index += 1
            if self._index >= len(self._steps):
                self.finish()
                return
            step = self._steps[self._index]
            if step.on_show is not None:
                try:
                    step.on_show(self)
                except Exception:  # noqa: BLE001 — 탭 전환 실패는 이 단계를 건너뛸 이유일 뿐
                    log.debug("[tour] on_show 실패(스킵)", exc_info=True)
            target = None
            try:
                target = step.get_target(self)
            except Exception:  # noqa: BLE001 — 대상 조회 실패도 스킵 사유
                log.debug("[tour] 대상 위젯 조회 실패(스킵)", exc_info=True)
            if target is None or not self._widget_alive(target):
                continue
            if self._show_step(step, target):
                return
            # 측정·렌더 실패도 다음 단계로 — 화면에 아무것도 안 남긴다.

    def _show_step(self, step: TourStep, target: tk.Widget) -> bool:
        self._clear_visuals()
        measured = self._measure(target)
        if measured is None:
            return False
        x, y, w, h = measured
        try:
            self._ring = self._draw_ring(x, y, w, h)
            self._card = self._build_card(x, y, w, h, step)
        except tk.TclError:
            self._clear_visuals()
            return False
        if self._card is None:
            self._clear_visuals()
            return False
        return True

    def _draw_ring(self, x: int, y: int, w: int, h: int) -> List[tk.Frame]:
        """대상 둘레에 얇은 테두리 4조각을 그린다(위/아래/좌/우) — 속을
        가득 채운 프레임을 쓰면 대상 위젯을 그대로 덮어 버리므로, 실제
        내용을 가리지 않게 네 변만 그린다."""
        thick = px(_RING_THICKNESS)
        pad = px(_RING_PAD)
        color = C["accent"]
        pieces = (
            (x - pad, y - pad, w + 2 * pad, thick),               # 위
            (x - pad, y + h + pad - thick, w + 2 * pad, thick),   # 아래
            (x - pad, y - pad, thick, h + 2 * pad),               # 왼쪽
            (x + w + pad - thick, y - pad, thick, h + 2 * pad),   # 오른쪽
        )
        frames: List[tk.Frame] = []
        for px_, py_, pw_, ph_ in pieces:
            frame = tk.Frame(self.root, bg=color, bd=0, highlightthickness=0)
            frame.place(x=px_, y=py_, width=max(1, pw_), height=max(1, ph_))
            frames.append(frame)
        return frames

    def _build_card(self, x: int, y: int, w: int, h: int,
                    step: TourStep) -> Optional[tk.Frame]:
        frame = tk.Frame(self.root, bg=C["card"], bd=0,
                         highlightthickness=px(1),
                         highlightbackground=C["border_strong"])
        pad = SP["md"]
        tk.Label(frame, text=step.title, font=FT, fg=C["text"], bg=C["card"],
                anchor="w", justify="left").pack(
            fill="x", padx=pad, pady=(pad, SP["xs"]))
        tk.Label(frame, text=step.body, font=FB, fg=C["text_2"], bg=C["card"],
                anchor="w", justify="left", wraplength=px(_CARD_WRAP)).pack(
            fill="x", padx=pad)

        indicator_row = tk.Frame(frame, bg=C["card"])
        indicator_row.pack(fill="x", padx=pad, pady=(SP["sm"], 0))
        indicator = StepIndicator(indicator_row, [""] * len(self._steps))
        indicator.set_active(self._index)
        indicator.pack(anchor="w")

        btn_row = tk.Frame(frame, bg=C["card"])
        btn_row.pack(fill="x", padx=pad, pady=(SP["sm"], pad))
        SubtleBtn(btn_row, "건너뛰기", self.skip).pack(side="left")
        is_last = self._index >= len(self._steps) - 1
        PrimaryBtn(btn_row, "완료" if is_last else "다음",
                  self.next_step).pack(side="right")

        try:
            frame.update_idletasks()
            card_w = max(frame.winfo_reqwidth(), px(220))
            card_h = frame.winfo_reqheight()
            root_w = self.root.winfo_width()
            root_h = self.root.winfo_height()
        except tk.TclError:
            frame.destroy()
            return None

        gap = SP["sm"]
        card_y = y + h + gap
        if card_y + card_h > root_h:
            card_y = y - card_h - gap
        if card_y < 0:
            card_y = max(0, min(y + h + gap, root_h - card_h))
        card_x = min(max(0, x), max(0, root_w - card_w))

        try:
            frame.place(x=card_x, y=card_y, width=card_w, height=card_h)
        except tk.TclError:
            frame.destroy()
            return None
        return frame

    def _clear_visuals(self) -> None:
        for w in self._ring:
            try:
                w.destroy()
            except tk.TclError:
                pass
        self._ring = []
        if self._card is not None:
            try:
                self._card.destroy()
            except tk.TclError:
                pass
            self._card = None
