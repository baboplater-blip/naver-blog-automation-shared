# gui/app.py — 메인 App 클래스 (GUI 개편 2026-09-02)
"""앱 셸 — 사이드바(설계서 §2.2, 자주 쓰는 3 + "더 보기" 접기 11)와
전역 하단 활동 바(§2.3)를 소유한다. 실행 상태는 :class:`RunBus` 가 단일
게시판 역할을 하고(§1.4), 워커 스레드 위생은 :class:`AppLifecycle` 이
지킨다(§1.3). 자세한 배경은 ``docs/auto-dev/gui-overhaul-design.md`` 참조.
"""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from tkinter import messagebox
from typing import Optional
from urllib.parse import urlencode
import tkinter as tk

log = logging.getLogger(__name__)

from gui import theme as _theme_module
from gui.theme import (C, FB, FS, FT, FH, SP, R, HAS_CTK, Btn, GhostBtn, L,
                        SecondaryBtn, font as _theme_font, strip_emoji)
from gui.theme import _pair as _theme_pair       # noqa: PLC2701 — widgets.py 와 동일 관례
from gui.theme import _resolve_pair as _theme_resolve_pair  # noqa: PLC2701
from gui.config_io import load_cfg
from gui.widgets import IconButton, Popover, ProgressRow, ScrollArea, StatusPill, Toast
from gui.async_utils import AppLifecycle
from gui.run_bus import RunBus, estimate_remaining_seconds
from gui import glossary
from gui import icons
from gui.humanize import format_minutes, humanize_detail

if HAS_CTK:
    import customtkinter as ctk

# 디자인 시스템 v3(§1 원칙4) — 이모지 전면 금지. `strip_emoji`(옛 이름
# `_strip_emoji`) 는 캡처 심사 3차 이후 `gui/theme.py` 공개 함수로
# 이전됐다(app.py·run_bus.py·gui/tabs/*.py 가 순환임포트 없이 공유하기
# 위함) — 이 파일에서 참조하는 `_strip_emoji` 는 하위호환 별칭이다.
_strip_emoji = strip_emoji

# 고DPI(9호 Phase 1) — `theme.px(n)` 은 `UI_SCALE`(NBA_UI_SCALE 환경변수 >
# 시스템 DPI > 1.0) 배율을 적용한 정수 픽셀을 돌려준다. 코더 A 가 같은
# 라운드에 만드는 중이라 `getattr` 폴백(없으면 항등함수)으로 참조한다.
# ⚠️ `theme.set_ui_scale()`(SP/R 자체 재계산)은 이 시점 기준 `SP_BASE`/
# `R_BASE` 미정의로 호출 시 NameError 가 난다(theme.py 미완성 구간, 코더
# A 몫) — 그래서 여기서는 호출하지 않고 `px()` 단독으로만 하드코딩
# 픽셀값을 스케일한다. `SP`/`R` 딕셔너리 자체 값은 이미 이 파일 전역에서
# 토큰으로 쓰이므로, 코더 A 가 `set_ui_scale()` 을 완성해 부르는 순간
# 자동으로 같이 커진다(이중 스케일 없음 — `px()` 를 SP/R 토큰 자체에는
# 절대 씌우지 않는다).
_px = getattr(_theme_module, "px", lambda n: n)
# 모션(9호 Phase 5) — theme.animate() 재사용. 위 _px 와 같은 방어적
# getattr 관례(코더 A 라운드가 먼저 끝났으니 지금은 항상 실제 값이지만,
# 이 파일 상단부의 기존 스타일을 그대로 따른다).
_animate = getattr(_theme_module, "animate", None)
_ease_out_quad = getattr(_theme_module, "ease_out_quad", lambda t: t)


def _motion_enabled() -> bool:
    """매 호출마다 theme.MOTION_ENABLED 를 새로 읽는다 — 모듈 상수를
    임포트 시점에 복사해두면 테스트가 `theme.MOTION_ENABLED = False` 로
    끄고 켜는 걸 못 본다(gui/theme.py 의 `from gui.theme import C` 는
    호출 시점에 읽으라는 기존 규범과 같은 이유)."""
    return bool(getattr(_theme_module, "MOTION_ENABLED", True))


from gui.tabs.home import HomeTab
from gui.tabs.archive import ArchiveTab
from gui.tabs.dashboard import DashboardTab
from gui.tabs.growth import GrowthTab
from gui.tabs.posting import PostingTab
from gui.tabs.trend_dup import TrendDupTab
from gui.tabs.keyword import KeywordTab
from gui.tabs.content import ContentTab
from gui.tabs.image_panel import ImagePanelTab
from gui.tabs.photo_post import PhotoPostTab
from gui.tabs.product_post import ProductPostTab
from gui.tabs.channel import ChannelTab
from gui.tabs.settings import SettingsTab
from gui.tabs.log import LogTab
from gui.tabs.profiles import ProfilesTab
from gui.tabs.autonomy import AutonomyTab
from gui import profile_actions as _pa

# ── 사이드바 개인화(auto-dev 6호 §2) ─────────────────────
# key -> (라벨, 기본 그룹). home 은 항상 고정 기본이라 여기 없다. posting·
# settings 는 기본값으로는 "자주 쓰는"에 있지만, 개인화로 밀려나면 각자의
# 기본 그룹(발행·진행→글쓰기, 설정→관리)으로 들어간다 — 그래서 나머지
# 12개 탭과 마찬가지로 그룹이 정의돼 있다(2026-09-18 무인 운영 탭 추가).
# (라벨, 기본 그룹, 아이콘 키) — 디자인 시스템 v3(§1 원칙4) 이후 라벨에
# 이모지를 더 이상 넣지 않는다. 아이콘은 사이드바 행이 별도 CTkLabel(아이콘
# 폰트)로 그린다(icons.py 의 키 — 두 폰트를 한 텍스트에 못 섞으므로 분리,
# customtkinter6-notes.md §4-4).
_TAB_REGISTRY = {
    "archive":   ("저장 글", "글쓰기", "folder"),
    "posting":   ("발행·진행", "글쓰기", "posting"),
    "photo":     ("사진으로 글쓰기", "글쓰기", "photo"),
    "product":   ("상품으로 글쓰기", "글쓰기", "product"),
    "image":     ("이미지 만들기", "글쓰기", "image"),
    "content":   ("다시 쓰기·시리즈", "글쓰기", "content"),
    "keyword":   ("키워드 찾기", "글쓰기", "keyword"),
    "dashboard": ("통계", "성장·분석", "stats"),
    "growth":    ("성장", "성장·분석", "growth"),
    "trend":     ("트렌드·중복", "성장·분석", "trend"),
    "channel":   ("알림·카페", "성장·분석", "channel"),
    "settings":  ("설정", "관리", "settings"),
    "profiles":  ("블로그 프로필", "관리", "profile"),
    "logs":      ("실행 기록", "관리", "log"),
    "autonomy":  ("무인 운영", "관리", "run"),
}
_GROUP_ORDER = ("글쓰기", "성장·분석", "관리")
_NON_HOME_KEYS = tuple(_TAB_REGISTRY.keys())   # compute_basic_tabs 후보
# v4(2026-09-21) — 주 동선 5개는 **항상 이 순서로 사이드바 맨 위에 고정**된다.
# "찾기 쉽게 고정" 이 요구사항이라, 개인화가 이 자리를 재배치하지 못한다.
# 예전 `_DEFAULT_BASIC_TABS`(home/posting/settings) 은 이 목록으로 대체됐다.
_PINNED_TABS = ["home", "archive", "posting", "profiles", "settings"]
_DEFAULT_BASIC_TABS = list(_PINNED_TABS)       # 하위호환 별칭(같은 객체 아님)
_MAX_PERSONALIZED_EXTRAS = 2   # 고정 5개 아래에 덧붙는 "자주 쓰는" 최대 개수
_MIN_USAGE_TOTAL_FOR_PERSONALIZE = 20   # 이보다 적게 눌렀으면 "데이터 부족"
_HOME_ICON_KEY = "home"   # home 은 _TAB_REGISTRY 밖(항상 고정 기본)이라 따로 둔다.


def compute_basic_tabs(usage: dict, personalize: bool) -> list:
    """사이드바 상단(고정 + 자주 쓰는)에 놓을 탭 키 목록을 계산하는 순수 함수.

    v4(2026-09-21) 로 역할이 바뀌었다 — 예전에는 ``home`` 외 2자리를 사용량
    상위 2개로 **교체**해서, 사용자가 자주 쓰는 탭이 바뀔 때마다 "발행·진행"
    이나 "설정" 이 사이드바에서 사라졌다. 주 동선이 자리를 지켜야 한다는
    요구에 따라, 이제 :data:`_PINNED_TABS` 5개는 **항상 이 순서 그대로**
    맨 앞에 있고, 개인화는 그 **아래에 최대 2개를 덧붙이기만** 한다.

    덧붙이지 않는 경우(전부 안전 폴백 — 고정 5개만 반환):

    - ``personalize`` 가 False(``ui_prefs`` 의 ``sidebar_personalize`` 꺼짐).
    - 고정 탭을 뺀 유효 클릭 총합이
      :data:`_MIN_USAGE_TOTAL_FOR_PERSONALIZE` 미만(데이터 부족).
    - 2위와 3위가 동률이라 "상위 2개"가 모호할 때(임의 순서 승격 금지).

    ``usage`` 의 미지의 키(옛 버전이 남긴 폐기된 탭 이름)는 후보 순회가
    ``_NON_HOME_KEYS`` 기준이라 자연히 무시된다.

    Args:
        usage: ``ui_prefs.get("tab_usage", {})`` 그대로. ``None`` 도 허용.
        personalize: ``ui_prefs.get("sidebar_personalize", True)``.

    Returns:
        ``_PINNED_TABS`` 로 시작하는 리스트(길이 5~7). 0번째는 항상 ``"home"``.
    """
    pinned = list(_PINNED_TABS)
    if not personalize:
        return pinned
    usage = usage or {}
    # 고정 탭은 후보에서 제외한다 — 이미 위에 있는 걸 또 올릴 이유가 없다.
    candidates = [k for k in _NON_HOME_KEYS if k not in pinned]
    counts = {}
    for key in candidates:
        try:
            counts[key] = int(usage.get(key, 0) or 0)
        except (TypeError, ValueError):
            counts[key] = 0
    if sum(counts.values()) < _MIN_USAGE_TOTAL_FOR_PERSONALIZE:
        return pinned
    ranked = sorted(candidates, key=lambda k: counts[k], reverse=True)
    n = _MAX_PERSONALIZED_EXTRAS
    # n위·n+1위가 동률이면 "상위 n개"가 모호하다 — 승격 자체를 하지 않는다.
    if len(ranked) > n and counts[ranked[n - 1]] == counts[ranked[n]]:
        return pinned
    return pinned + ranked[:n]


class _SidebarRow:
    """사이드바 항목 행(디자인 시스템 v3 §7-1) — 아이콘 CTkLabel + 텍스트
    CTkLabel 을 담은 CTkFrame. 둘을 한 CTkButton 안에 못 넣는 이유는 버튼이
    라벨 하나에 폰트 하나만 적용하기 때문(아이콘 폰트와 본문 폰트를 섞을 수
    없음 — customtkinter6-notes.md §4-4). HAS_CTK 가 False 면 기존 v2 의
    순정 tk.Button 한 줄로 폴백한다.

    tk 옵션 dict 계약(``["text"]``/``["fg"]``/``["bg"]``/``.config()``/
    ``.invoke()``/``.winfo_manager()``/``.pack()``)을 흉내내 ``show_tab``
    의 하이라이트 로직과 기존 테스트(``tab_btns[key]["text"]``·
    ``tab_btns[key].invoke()``)가 그대로 동작하게 한다.
    """

    def __init__(self, parent, key: str, icon_key: str, text: str, on_click):
        self.key = key
        self._on_click = on_click
        self._active = False
        self._is_ctk = HAS_CTK

        if self._is_ctk:
            # fg_color 는 문자열 "transparent" 가 아니라 반드시 실제
            # (light, dark) 페어로 준다(디자이너 심사 🔴7 — 라이트 모드
            # 사이드바 다크 고정 회귀). "transparent" 는 CTk 가 부모의 현재
            # 배경색을 **위젯 생성 시점에 한 번** 읽어 고정하는데, 이 행의
            # 부모(`body`)는 순정 tk.Frame(리씨임 대상, CTk 의 appearance
            # -mode 추적 밖)이라 나중에 `set_appearance_mode()` 로 라이트로
            # 바꿔도 이미 그려진 "transparent" 배경은 그 시점의(다크) 색에
            # 얼어붙는다. `self.row` 자체를 진짜 페어로 두면 CTk 가
            # appearance-mode 전환마다 자동 재도색한다 — 이후 그 안에 있는
            # `inner`(순수 레이아웃 래퍼)는 "transparent" 로 둬도 안전하다
            # (부모가 이제 살아있는 CTk 색 소스이기 때문).
            self.row = ctk.CTkFrame(parent, corner_radius=R["sm"], height=_px(36),
                                    fg_color=_theme_pair("surface"))
            self.row.pack_propagate(False)
            # 좌측 3px 스트라이프 — 자리는 항상 차지하되 색만 토글해(place
            # 로 항상 존재) 활성/비활성 전환 시 레이아웃이 흔들리지 않게
            # 한다. 비활성 색도 위와 같은 이유로 실제 페어(surface)를 쓴다.
            self.stripe = ctk.CTkFrame(self.row, width=_px(3), corner_radius=0,
                                       fg_color=_theme_pair("surface"))
            self.stripe.place(x=0, y=0, relheight=1)
            inner = ctk.CTkFrame(self.row, fg_color="transparent")
            inner.pack(side="left", fill="both", expand=True,
                      padx=(SP["md"], SP["sm"]))
            glyph = icons.glyph(icon_key) if icon_key else ""
            self.icon_lbl = ctk.CTkLabel(
                inner, text=glyph, width=_px(20),
                font=icons.icon_font(_px(18)) or _theme_font(_px(14)),
                text_color=_theme_pair("muted"), fg_color="transparent")
            self.icon_lbl.pack(side="left", padx=(0, SP["xs"]))
            self.text_lbl = ctk.CTkLabel(
                inner, text=text, font=_theme_font(_px(13)), anchor="w",
                text_color=_theme_pair("muted"), fg_color="transparent")
            self.text_lbl.pack(side="left", fill="x", expand=True)
            for w in (self.row, inner, self.icon_lbl, self.text_lbl):
                w.bind("<Button-1>", lambda _e: self._on_click(), add="+")
                w.bind("<Enter>", lambda _e: self._on_hover(True), add="+")
                w.bind("<Leave>", lambda _e: self._on_hover(False), add="+")
        else:
            self.row = tk.Button(
                parent, text=text, font=FB, fg=C["muted"], bg=C["surface"],
                activebackground=C["card"], activeforeground=C["text"],
                relief="flat", cursor="hand2", anchor="w", padx=SP["md"],
                pady=SP["xs"], bd=0, command=lambda: self._on_click())
            self.icon_lbl = None
            self.text_lbl = None
            self.stripe = None
        self.row.pack(fill="x")

    def _on_hover(self, entering: bool) -> None:
        if self._active:
            return
        try:
            self.row.configure(
                fg_color=(_theme_pair("card_hover") if entering
                         else _theme_pair("surface")))
        except Exception:  # noqa: BLE001 — hover 애니메이션 실패는 무해
            pass

    def set_active(self, active: bool) -> None:
        """선택 상태 갱신 — accent_soft 배경 + accent 글자 + 좌측 스트라이프.

        비활성 색도 "transparent" 대신 ``_theme_pair("surface")`` 를 쓴다
        (디자이너 심사 🔴7 — 생성자와 같은 이유, "transparent" 는 라이트/
        다크 전환에 반응하지 않는 얼어붙은 색으로 남을 수 있다)."""
        self._active = active
        if self._is_ctk:
            try:
                self.row.configure(
                    fg_color=(_theme_pair("accent_soft") if active
                             else _theme_pair("surface")))
                self.stripe.configure(
                    fg_color=(_theme_pair("accent") if active
                             else _theme_pair("surface")))
                color = _theme_pair("accent") if active else _theme_pair("muted")
                self.icon_lbl.configure(text_color=color)
                self.text_lbl.configure(text_color=color)
            except Exception:  # noqa: BLE001 — 재도색 실패는 무해(다음 전환 때 복구)
                pass
        else:
            if active:
                self.row.config(fg=C["accent"], bg=C["card"])
            else:
                self.row.config(fg=C["muted"], bg=C["surface"])

    # ── tk 옵션 dict 계약 ──────────────────────────────
    def cget(self, key):
        if key == "text":
            return (self.text_lbl.cget("text") if self._is_ctk
                    else self.row.cget("text"))
        if key in ("fg", "foreground"):
            if self._is_ctk:
                return _theme_resolve_pair(self.text_lbl.cget("text_color"))
            return self.row.cget("fg")
        if key in ("bg", "background"):
            if self._is_ctk:
                return _theme_resolve_pair(self.row.cget("fg_color"))
            return self.row.cget("bg")
        return self.row.cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def config(self, **kwargs):
        """``show_tab`` 의 옛(v2) 하이라이트 호출과의 하위호환 — accent 로
        바뀌면 활성, 그 외(muted 등)면 비활성으로 해석해 ``set_active`` 로
        위임한다. 새 코드는 ``set_active(bool)`` 을 직접 부르는 걸 권장."""
        fg = kwargs.get("fg") or kwargs.get("foreground")
        if fg is not None:
            self.set_active(fg == C["accent"])
            return
        if self._is_ctk:
            return
        self.row.config(**kwargs)

    configure = config

    def __setitem__(self, key, value):
        self.config(**{key: value})

    def invoke(self):
        self._on_click()

    def __getattr__(self, name):
        # 어댑터가 정의하지 않은 tk 위젯 메서드(winfo_ismapped·winfo_rootx 등)는
        # 실제 행 프레임에 위임한다(시각 스모크 도달성 검사 등 tk 계약 호환).
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.__dict__["row"], name)

    def winfo_manager(self):
        return self.row.winfo_manager()

    def winfo_exists(self):
        return self.row.winfo_exists()

    def pack(self, *a, **kw):
        return self.row.pack(*a, **kw)

    def pack_forget(self):
        return self.row.pack_forget()

    def bind(self, *a, **kw):
        return self.row.bind(*a, **kw)


class App(AppLifecycle):
    def __init__(self, start_tour: bool = True):
        """``start_tour=False`` 는 첫 실행 코치마크 투어(§9호 Phase 3)의
        자동 시작 자체를 완전히 끈다 — 기존 ``App()`` 무인자 호출은 전부
        그대로 동작하고(기본값 True), 자동 시작은 그 자체로도 세 겹으로
        보호돼 있다: ①1.5초 지연 예약(테스트가 그 전에 파괴하면 발동 안
        함) ②``ui_prefs.tour_done`` ③``NBA_NO_TOUR=1`` 환경변수. 대상
        위젯이 없으면(FakeApp 등) 각 단계가 조용히 스킵된다(``gui.tour``
        의 fail-open)."""
        self.root = tk.Tk()

        # 저장된 팔레트 적용 — 코더 A 가 만드는 gui/ui_prefs.py·
        # theme.apply_palette 가 아직 없을 수 있어(병렬 개발) ImportError 를
        # 조용히 삼킨다(auto-dev 5호 §3). 나머지 위젯 생성보다 먼저 실행해야
        # 이후 만들어지는 모든 위젯이 새 팔레트를 반영한다.
        try:  # noqa: BLE001 — 선택적 의존성, 실패해도 기본 다크 테마로 계속
            from gui import ui_prefs, theme as _theme
            _theme.apply_palette(ui_prefs.get("theme", "dark"))
        except Exception:
            pass

        self.root.title("블로그 자동화")
        self.root.geometry(f"{_px(1080)}x{_px(720)}")
        self.root.minsize(_px(900), _px(620))
        self.root.configure(bg=C["bg"])
        self._init_lifecycle(self.root)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.cfg        = load_cfg()
        self._running   = False
        # run_today 준비 단계(브라우저·로그인·후보 분석)가 실행 슬롯을 들고 있는가.
        # True 면 _start_run 이 재점유 없이 그 슬롯을 이어받는다.
        self._prep_owns_slot = False
        self._stop      = threading.Event()
        self.auth_event = threading.Event()
        self._current_run_label = None

        # 첫 실행 코치마크 투어(9호 Phase 3) — 생성자 인자로만 완전히 끌 수
        # 있다(테스트 헬퍼가 필요하면 ``App(start_tour=False)``). 자동 시작
        # 여부의 나머지 판단(``ui_prefs.tour_done``·``NBA_NO_TOUR``)은
        # `_maybe_start_tour` 가 한다.
        self._start_tour_enabled = bool(start_tour)
        self._tour = None
        # 발행 큐 정렬 캐시 재워밍 타이머(9호 Phase 3) — 등록 여부를
        # 테스트가 직접 확인할 수 있게 플래그로도 남긴다.
        self._queue_sort_rewarm_registered = False

        # 실행 상태 단일 게시판 + 토스트(설계서 §1.4·§1.5). 워커는 이 둘만
        # 안전하게 호출한다(스레드 안전 — RunBus/Toast 내부에서 재위임).
        self.bus   = RunBus(self)
        self.toast = Toast(self.root)

        self._build_ui()
        self.bus.subscribe(self)

        self._env_check()
        self._maybe_autostart_comfy()
        self._maybe_autostart_shorts()
        self._schedule_queue_sort_rewarm()

    # ── 셸 조립 ───────────────────────────────────────
    def _build_ui(self):
        # 활동 바를 먼저 pack 해야 창 전체 폭 하단에 항상 고정된다(사이드바·
        # 콘텐츠를 먼저 pack 하면 그쪽이 남은 공간을 다 차지해 활동 바가
        # 들어갈 자리가 없어진다 — visual-spec §6).
        self._build_activity_bar()
        self._build_sidebar_and_pages()

    def _build_activity_bar(self):
        """전역 하단 활동 바(디자인 시스템 v3 §7-1, 40px) — 상태 pill·진행률·
        중지 IconButton·배지."""
        bar = tk.Frame(self.root, bg=C["surface"], height=_px(40),
                        highlightthickness=_px(1), highlightbackground=C["border_strong"])
        bar.pack(side="bottom", fill="x")
        bar.pack_propagate(False)
        self._activity_bar = bar

        left = tk.Frame(bar, bg=C["surface"])
        left.pack(side="left", fill="both", expand=True, padx=SP["md"])

        # status_lbl 속성명 계약 유지(0.2) — StatusPill 은 tk.Label 서브클래스라
        # winfo_exists()·event_generate("<Button-1>") 등 기존 계약이 그대로
        # 성립한다(위젯 §7-1 kind 연동: 대기 neutral/실행 info/성공 ok/실패 error).
        self.status_lbl = StatusPill(
            left, text=self._neutral_status_text("대기 중"), kind="neutral")
        self.status_lbl.configure(cursor="hand2")
        self.status_lbl.pack(side="left")

        # 실행 중일 때만 표시(on_run_state 가 pack/pack_forget 토글). 중지는
        # ProgressRow 내장 버튼이 아니라 별도 IconButton(§7-1 "중지 IconButton
        # 별도") — 실행 중일 때만 보인다.
        self._activity_progress = ProgressRow(left, bg=C["surface"])
        self._activity_stop_btn = IconButton(left, "stop", self.stop_run,
                                              tooltip="중지")

        # 실행 요약 팝오버(업그레이드1) — 상태 pill·진행행 어느 쪽을 눌러도
        # 같은 팝오버가 뜬다. Popover 생성자가 anchor(status_lbl) 클릭을
        # 자체 바인딩하므로, 진행행 쪽은 여기서 추가로 toggle() 을 배선한다.
        # ProgressRow 는 라벨/퍼센트 자식 위젯이 배경을 거의 다 덮어(Tk 는
        # 클릭을 부모로 자동 전파하지 않는다) 프레임 자체 bind 만으론 거의
        # 안 눌린다 — 눈에 보이는 자식(라벨·퍼센트)에도 직접 배선한다.
        self._run_popover = Popover(self.status_lbl, self._build_run_popover_content)
        for _w in (self._activity_progress, getattr(self._activity_progress, "_label", None),
                   getattr(self._activity_progress, "_pct", None)):
            if _w is not None:
                _w.bind("<Button-1>", lambda _e: self._run_popover.toggle(), add="+")

        right = tk.Frame(bar, bg=C["surface"])
        right.pack(side="right", padx=SP["md"])

        self._publish_mode_lbl = tk.Label(
            right, text=self._publish_mode_badge_text(), font=FS,
            fg=C["text_2"], bg=C["surface"])
        self._publish_mode_lbl.pack(side="left", padx=(0, SP["md"]))

        try:
            badge_text = _strip_emoji(_pa.profile_badge_text())
        except Exception:  # noqa: BLE001
            badge_text = "default"
        # profile_badge 속성명 계약 유지(0.2) — 사이드바에서 활동 바 우측으로 이동.
        self.profile_badge = tk.Label(
            right, text=badge_text, font=FS,
            bg=C["surface"], fg=C["accent"], cursor="hand2")
        self.profile_badge.pack(side="left")
        self.profile_badge.bind("<Button-1>", lambda _e: self.show_tab("profiles"))

        # 지금 돌고 있는 코드가 언제 것인지 한 칸으로 — 앱을 다시 켰을 때
        # 변경이 실제로 반영됐는지 사용자가 눈으로 확인할 수 있게 한다.
        self._code_stamp_lbl = tk.Label(
            right, text=self._code_stamp_text(), font=FS,
            fg=C["muted"], bg=C["surface"])
        self._code_stamp_lbl.pack(side="left", padx=(SP["md"], 0))

    @staticmethod
    def _code_stamp_text() -> str:
        """실제로 로드된 코드 파일 중 가장 최근 수정 시각('업데이트 MM-DD HH:MM').

        2026-09-22 문구 점검 — 예전 '코드 MM-DD HH:MM' 은 사용자에게 뜻이
        전달되지 않는 내부 표현이었다(값·계산 방식은 그대로).

        빌드 번호가 없는 소스 실행 배포라 파일 시각이 유일하게 정직한 근거다.
        실패하면 빈 문자열(화면에 아무것도 덧붙이지 않는다)."""
        try:
            base = Path(__file__).resolve().parent.parent
            newest = 0.0
            for sub in ("gui", "core", "scheduler", "config"):
                d = base / sub
                if not d.is_dir():
                    continue
                for f in d.rglob("*.py"):
                    try:
                        newest = max(newest, f.stat().st_mtime)
                    except OSError:
                        continue
            if not newest:
                return ""
            return "업데이트 " + datetime.fromtimestamp(newest).strftime("%m-%d %H:%M")
        except Exception:  # noqa: BLE001 — 표시용 부가 정보가 기동을 막으면 안 됨
            return ""

    def _publish_mode_badge_text(self) -> str:
        mode = (self.cfg.get("publish_mode") or "spawn")
        key = "publish_attach" if mode == "attach" else "publish_spawn"
        return glossary.label(key)

    def set_theme(self, name: str) -> str:
        """라이브 테마 전환(auto-dev 6호 §1) — 설정 탭의 테마 라디오가
        ``getattr(app, "set_theme", None)`` 으로 호출한다.

        항상 ``ui_prefs`` 에 저장해 다음 실행에도 유지되게 한 뒤, 가능하면
        그 자리에서 즉시 재적용한다:

        - ``theme.retheme(root, name)`` 가 있으면(코더 A 작업 중 — 이
          세션 작성 시점엔 없을 수 있다) 그걸로 창 전체를 다시 칠하고
          "밝은/어두운 테마로 바꿨어요" 토스트.
        - 없으면 ``theme.apply_palette(name)`` 로 팔레트 상수(``C`` dict)만
          바꾼다 — 이미 그려진 위젯은 그대로지만(재시작해야 완전히 반영),
          "다시 켜면 적용" 토스트로 정직하게 안내한다.

        두 경로 모두 ``show_tab(현재 탭)`` 을 재호출해 사이드바 선택
        하이라이트·활동 바처럼 ``C`` 를 그때그때 읽어 그리는 부분만은
        즉시 새 색으로 갱신한다(``user=False`` 라 사용 집계는 안 됨).

        Returns:
            실제 적용(또는 저장)된 팔레트 이름.
        """
        resolved = name
        try:
            from gui import ui_prefs
            ui_prefs.set("theme", name)
        except Exception:  # noqa: BLE001 — 저장 실패해도 이번 세션 적용은 시도한다
            log.debug("테마 저장 실패", exc_info=True)

        try:
            from gui import theme as _theme
        except Exception:  # noqa: BLE001 — theme 모듈 자체는 항상 있어야 하지만 방어
            _theme = None

        applied_live = False
        retheme = getattr(_theme, "retheme", None) if _theme is not None else None
        if callable(retheme):
            try:
                resolved = retheme(self.root, name) or name
                applied_live = True
            except Exception:  # noqa: BLE001 — retheme 실패 시 팔레트 상수만이라도
                log.error("retheme 실패", exc_info=True)
        if not applied_live and _theme is not None:
            apply_palette = getattr(_theme, "apply_palette", None)
            if callable(apply_palette):
                try:
                    resolved = apply_palette(name) or name
                except Exception:  # noqa: BLE001
                    log.error("apply_palette 실패", exc_info=True)

        current_key = next((k for k, p in self.pages.items()
                            if p.winfo_manager()), None)
        if current_key is not None:
            self.show_tab(current_key)

        toast = getattr(self, "toast", None)
        if toast is not None:
            if applied_live:
                label = {"light": "밝은", "dark": "어두운"}.get(resolved, resolved)
                toast.show(f"{label} 테마로 바꿨어요", kind="ok")
            else:
                toast.show("테마를 저장했어요 · 다시 켜면 적용돼요", kind="info")
        return resolved

    def _build_run_popover_content(self, parent: tk.Widget) -> None:
        """실행 요약 팝오버 내용(업그레이드1) — `bus.snapshot()` 스냅샷을 그린다.

        진행 중: "지금 N편째 / 총 M · 완료 a · 실패 b · 건너뜀 c · 예상 남은
        시간 · 최근 실패 3건". 유휴: 마지막 실행 요약(없으면 "아직 실행
        전이에요"). `Popover.build_fn` 계약대로 매번 새로 그린다.
        """
        stats = self.bus.snapshot()
        pad = {"padx": SP["md"], "pady": SP["sm"]}
        if stats is None:
            tk.Label(parent, text="아직 실행 전이에요", font=FB,
                     fg=C["text_2"], bg=C["card"]).pack(anchor="w", **pad)
            SecondaryBtn(parent, "발행·진행 탭 열기",
                        lambda: self.show_tab("posting")).pack(
                anchor="w", padx=SP["md"], pady=(0, SP["md"]))
            return

        body = tk.Frame(parent, bg=C["card"])
        body.pack(fill="both", expand=True, padx=SP["md"], pady=SP["md"])

        running = stats.get("finished_at") is None
        cur, total = stats.get("cur", 0), stats.get("total", 0)
        ok = stats.get("ok", 0)
        fail = stats.get("fail", 0)
        skip = stats.get("skip", 0)

        title_text = (f"지금 {cur}편째 / 총 {total}편" if running
                      else f"{strip_emoji(stats.get('label', ''))} · {cur}/{total}편 처리")
        tk.Label(body, text=title_text, font=FT, fg=C["text"], bg=C["card"],
                 anchor="w").pack(fill="x")
        tk.Label(body, text=f"완료 {ok} · 실패 {fail} · 건너뜀 {skip}",
                 font=FB, fg=C["text_2"], bg=C["card"], anchor="w").pack(
            fill="x", pady=(SP["xs"], 0))

        if running:
            eta = estimate_remaining_seconds(stats)
            if eta is not None:
                eta_text = format_minutes(max(1, round(eta / 60)))
                tk.Label(body, text=f"예상 남은 시간: {eta_text}", font=FS,
                         fg=C["text_2"], bg=C["card"], anchor="w").pack(
                    fill="x", pady=(SP["xs"], 0))

        failures = stats.get("failures") or []
        if failures:
            tk.Label(body, text="최근 실패", font=FS, fg=C["muted"],
                     bg=C["card"], anchor="w").pack(fill="x", pady=(SP["sm"], 0))
            for title, reason in failures[-3:]:
                human = strip_emoji(humanize_detail(reason)) if reason else "실패"
                tk.Label(body, text=f"· {strip_emoji(title)[:24]} — {human}", font=FS,
                         fg=C["text_2"], bg=C["card"], anchor="w",
                         wraplength=_px(260), justify="left").pack(fill="x")

        SecondaryBtn(body, "발행·진행 탭 열기",
                    lambda: self.show_tab("posting")).pack(anchor="w",
                                                            pady=(SP["sm"], 0))

    def _build_sidebar_and_pages(self):
        """사이드바(디자인 시스템 v3 §7-1, 240px) + 콘텐츠 영역(최대 폭
        1120 중앙 정렬) + 16개 탭 페이지.

        상단은 :data:`_PINNED_TABS` 5개(오늘·저장 글·발행·진행·블로그
        프로필·설정)가 **항상 같은 순서로 고정**된다(v4, 2026-09-21).

        "더 보기"(``_adv_frame``) 안 3그룹(글쓰기/성장·분석/관리)은
        **아코디언**이다 — 한 번에 한 그룹의 항목 목록만 펼쳐지고(그룹 헤더는
        3개 항상 보임), 헤더 클릭이나 ``show_tab(key)`` 로 그 키가 속한
        그룹으로 전환하면 이전 그룹은 접힌다(2026-09-02 실측 재현 3건의
        근본 처방 — 3그룹 동시 펼침은 최소 창에서 잘린다). 항목 행은
        :class:`_SidebarRow`(아이콘+텍스트, 높이 36·radius 8·hover/활성
        상태) — 남는 여유는 :class:`ScrollArea` 가 안전장치로 흡수한다.
        """
        sb = tk.Frame(self.root, bg=C["surface"], width=_px(240))
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)
        # 사이드바 우측 헤어라인(디자인 시스템 v3 §7-1) — CTkFrame 은 4변
        # 통짜 border 만 지원해 한쪽 변만 그릴 수 없다. 1px 세로선으로 대체.
        tk.Frame(self.root, width=_px(1), bg=C["border"]).pack(side="left", fill="y")

        # 버전 라벨 — 사이드바 최하단에 고정(캡처 심사 🟡1: 예전엔 스크롤
        # 영역 안 목록 바로 아래 허공에 떠 있었다). `scroll`(스크롤 영역)
        # 보다 먼저 side="bottom" 으로 pack 해야 나머지 공간을 스크롤 영역이
        # 차지하기 전에 이 자리를 먼저 예약한다.
        tk.Label(sb, text="v3.0 · 블로그 자동화", font=FS, fg=C["muted"],
                 bg=C["surface"], anchor="w").pack(
            side="bottom", fill="x", padx=SP["md"], pady=SP["md"])

        self.content = tk.Frame(self.root, bg=C["bg"])
        self.content.pack(side="right", fill="both", expand=True)

        # 콘텐츠 최대 폭 1120 중앙 정렬(디자인 시스템 v3 §7-1) — 각 탭
        # 페이지는 이 안쪽 프레임을 부모로 삼는다(app.content 자체는 배경
        # 채우기 전용). place 로 폭만 재계산해 좌우 여백을 만든다 — 창이
        # 1120+여백보다 좁으면 창 폭에 맞춘다(SP.xl 최소 여백 유지).
        self._content_max_width = _px(1120)
        self._content_inner = tk.Frame(self.content, bg=C["bg"])
        self._content_inner.place(relx=0.5, rely=0, anchor="n",
                                  width=self._content_max_width, relheight=1)
        self.content.bind("<Configure>", self._on_content_resize)

        scroll = ScrollArea(sb, bg=C["surface"])
        scroll.pack(fill="both", expand=True)
        self._sidebar_scroll = scroll
        body = scroll.inner

        logo = tk.Frame(body, bg=C["surface"], height=_px(56))
        logo.pack(fill="x")
        logo.pack_propagate(False)
        tk.Label(logo, text=icons.glyph(_HOME_ICON_KEY),
                 font=icons.icon_font(_px(22)) or (FB[0], _px(20)), bg=C["surface"],
                 fg=C["accent"]).pack(side="left", padx=(SP["md"], SP["sm"]))
        tk.Label(logo, text="블로그 자동화", font=(FB[0], _px(13), "bold"),
                 bg=C["surface"], fg=C["text"]).pack(side="left")
        tk.Frame(body, height=_px(1), bg=C["border"]).pack(fill="x", padx=SP["md"],
                                                        pady=(0, SP["lg"]))

        self.pages    = {}
        self.tab_btns = {}

        # 개인화(auto-dev 6호 §2) — 기동 시 1회만 계산한다("다음 실행에
        # 반영" 원칙: 세션 중 사이드바가 움직이면 혼란스럽다). ui_prefs 가
        # 아직 없거나(코더 A 작업 중) 값이 이상해도 기본값으로 폴백.
        try:
            from gui import ui_prefs as _ui_prefs
            personalize = bool(_ui_prefs.get("sidebar_personalize", True))
            usage = _ui_prefs.get("tab_usage", {}) or {}
        except Exception:  # noqa: BLE001 — 개인화 실패는 기본 사이드바로 폴백
            personalize = True
            usage = {}
        basic_keys = compute_basic_tabs(usage, personalize)

        basic_tabs = [("home", "오늘")]
        for key in basic_keys[1:]:
            label = _TAB_REGISTRY.get(key, (key, None, None))[0]
            basic_tabs.append((key, label))
        # 고정 5개와 그 아래 "자주 쓰는" 승격분의 경계(사이드바 소제목 위치).
        pinned_count = len(_PINNED_TABS)

        # 그룹 3개(글쓰기/성장·분석/관리) — 순서가 _tab_order 의 단일 기준이며
        # Ctrl+1~9 는 이 순서를 그대로 따른다. 상단(고정+승격)에 이미 있는
        # 키는 그룹에서 빠진다(중복 행 금지 — tab_btns 는 키당 1행).
        group_defs = []
        for group_name in _GROUP_ORDER:
            items = [(key, info[0]) for key, info in _TAB_REGISTRY.items()
                     if info[1] == group_name and key not in basic_keys]
            group_defs.append((group_name, items))
        adv_tabs = [item for _, items in group_defs for item in items]
        tabs = basic_tabs + adv_tabs
        self._tab_order = [k for k, _ in tabs]
        self._advanced_keys = {k for k, _ in adv_tabs}
        self._adv_open = False

        # ── 페이지 인스턴스 (pages dict 키 16개 — 0.2 계약, 2026-09-21 저장 글 탭 추가) ──
        # _content_inner 가 부모(최대 폭 1120 중앙 정렬 — 위 참조).
        self.pages["home"]      = HomeTab(self._content_inner, self)
        self.pages["archive"]   = ArchiveTab(self._content_inner, self)
        self.pages["dashboard"] = DashboardTab(self._content_inner, self)
        self.pages["growth"]    = GrowthTab(self._content_inner, self)
        self.pages["posting"]   = PostingTab(self._content_inner, self)
        self.pages["trend"]     = TrendDupTab(self._content_inner, self)
        self.pages["keyword"]   = KeywordTab(self._content_inner, self)
        self.pages["content"]   = ContentTab(self._content_inner, self)
        self.pages["image"]     = ImagePanelTab(self._content_inner, self)
        self.pages["photo"]     = PhotoPostTab(self._content_inner, self)
        self.pages["product"]   = ProductPostTab(self._content_inner, self)
        self.pages["channel"]   = ChannelTab(self._content_inner, self)
        self.pages["profiles"]  = ProfilesTab(self._content_inner, self)
        self.pages["settings"]  = SettingsTab(self._content_inner, self)
        self.pages["logs"]      = LogTab(self._content_inner, self)
        self.pages["autonomy"]  = AutonomyTab(self._content_inner, self)

        # 단축키 힌트를 라벨에 표기(발견성). 힌트 문자열은 실제 바인딩과 같은
        # 소스에서 파생해 라벨↔바인딩이 어긋나지 않게 한다.
        shortcut_hints = self._tab_shortcut_hints(tabs)

        def _icon_key_for(key: str) -> str:
            if key == "home":
                return _HOME_ICON_KEY
            return _TAB_REGISTRY.get(key, (None, None, None))[2] or ""

        def _make_tab_btn(parent, key, text):
            hint = shortcut_hints.get(key)
            label = f"{text}   {hint}" if hint else text
            row = _SidebarRow(parent, key, _icon_key_for(key), label,
                              on_click=lambda k=key: self.show_tab(k, user=True))
            self.tab_btns[key] = row

        def _static_header(parent, name):
            """클릭 불가능한 구분용 소제목(그룹 캡션).

            캡처 심사 🟡2 — em-dash 로 감싼 선 장식("── 자주 쓰는 ──")을
            제거하고 Caption 톤만으로 표시한다(v3 그룹 헤더 규격: 장식 없이
            텍스트만, 대문자화는 한글에 효과가 없어 생략 — 영문 라벨이
            들어와도 무해하도록 `.upper()`는 적용).
            """
            hdr = tk.Label(parent, text=name.upper(), font=FS, fg=C["muted"],
                           bg=C["surface"], anchor="w", padx=SP["md"], pady=_px(2))
            hdr.pack(fill="x", pady=(SP["sm"], _px(2)))
            return hdr

        # 상단 블록 — 고정 5개는 "주요 작업", 개인화 승격분은 그 아래
        # "자주 쓰는" 소제목 뒤에 따로 붙인다(무엇이 고정이고 무엇이 내
        # 사용 이력으로 올라온 건지 구분되게).
        _static_header(body, "주요 작업")
        for key, text in basic_tabs[:pinned_count]:
            _make_tab_btn(body, key, text)
        if len(basic_tabs) > pinned_count:
            _static_header(body, "자주 쓰는")
            for key, text in basic_tabs[pinned_count:]:
                _make_tab_btn(body, key, text)

        # "더 보기" 접기 — 기본은 닫힘. 고급 탭을 show_tab 으로 열면 자동 펼침.
        self._adv_toggle_btn = tk.Button(
            body, text="더 보기  ▾", font=FB, fg=C["muted"], bg=C["surface"],
            activebackground=C["card"], activeforeground=C["text"],
            relief="flat", cursor="hand2", anchor="w", padx=SP["md"], pady=SP["xs"], bd=0,
            command=self._toggle_advanced)
        self._adv_toggle_btn.pack(fill="x")

        # "더 보기" 안 3그룹은 아코디언 — 헤더는 클릭 가능한 Button 이고,
        # 한 번에 한 그룹의 항목 프레임만 pack 된다(나머지는 pack_forget).
        self._adv_frame = tk.Frame(body, bg=C["surface"])
        self._adv_group_headers = {}
        self._adv_group_frames = {}
        self._adv_group_of_tab = {}
        self._adv_active_group = None
        for name, items in group_defs:
            if not items:  # 빈 그룹 헤더 방지(리뷰 7호 Low)
                continue
            hdr = tk.Button(
                self._adv_frame, text=self._group_header_text(name, False),
                font=FS, fg=C["muted"], bg=C["surface"],
                activebackground=C["card"], activeforeground=C["text"],
                relief="flat", cursor="hand2", anchor="w",
                padx=SP["md"], pady=_px(2), bd=0,
                command=lambda n=name: self._set_active_group(n))
            hdr.pack(fill="x", pady=(SP["xs"], 0))
            self._adv_group_headers[name] = hdr

            grp_frame = tk.Frame(self._adv_frame, bg=C["surface"])
            self._adv_group_frames[name] = grp_frame
            for key, text in items:
                _make_tab_btn(grp_frame, key, text)
                self._adv_group_of_tab[key] = name

        # 기본 펼침 그룹 — "현재 탭 그룹"이 없으므로(초기 탭은 home) 첫
        # 그룹("글쓰기")을 기본값으로 삼는다.
        self._set_active_group(group_defs[0][0])

        # (v4) 예전엔 여기 "v3.0" 라벨이 하나 더 있었다 — 사이드바 최하단의
        # "v3.0 · 블로그 자동화" 와 중복돼 목록 중간에 정체 모를 글자가 떠
        # 있는 것처럼 보였다(개편 전 캡처 실증). 하단 라벨 하나로 통일.

        self.dashboard = self.pages["dashboard"]
        self.show_tab("home")

        self._bind_tab_shortcuts()

    def _on_content_resize(self, event) -> None:
        """콘텐츠 영역 폭이 바뀔 때마다 ``_content_inner`` 폭을 재계산한다
        (디자인 시스템 v3 §7-1 — 최대 폭 1120 중앙 정렬, 창이 좁으면 창 폭에
        맞춤). ``event.width`` 는 ``self.content`` 프레임의 실제 폭이다."""
        inner = getattr(self, "_content_inner", None)
        if inner is None:
            return
        max_w = getattr(self, "_content_max_width", 1120)
        width = min(max_w, max(event.width, 1))
        try:
            inner.place_configure(width=width)
        except tk.TclError:
            pass

    @staticmethod
    def _group_header_text(name: str, active: bool) -> str:
        return f"{'▾' if active else '▸'} {name}"

    # ── 사이드바 아코디언 모션(9호 Phase 5) ───────────────────────────────
    def _animate_frame_expand(self, frame: tk.Frame, **pack_kwargs) -> None:
        """프레임을 높이 0→reqheight 로 펼친다(사이드바 "더 보기" 전체
        프레임과 그 안 아코디언 그룹 프레임 양쪽이 공유하는 헬퍼 — 8단계
        ~160ms ease-out, 실제 모션은 `theme.animate()` 에 위임(재호출 시
        이전 체인 취소·destroy 시 예약 취소 전부 그쪽 계약).

        `pack_kwargs` 는 `frame.pack(...)` 에 그대로 넘긴다(fill/after 등
        — height 는 이 메서드가 관리하므로 호출부가 넘기지 않는다).

        `MOTION_ENABLED=False`(또는 `NBA_NO_MOTION=1`)면 기존(모션 도입 전)과
        바이트 동일하게 그냥 `pack()` 한다 — propagate 조작·after 예약
        자체가 없다(§0.2 계약 테스트가 `pack()` 직후 `_adv_open`/
        `winfo_manager()` 를 동기적으로 기대하므로)."""
        if not _motion_enabled() or _animate is None:
            frame.pack(**pack_kwargs)
            return
        frame.pack(**pack_kwargs)
        try:
            frame.update_idletasks()
            target_h = frame.winfo_reqheight()
        except tk.TclError:
            return
        if target_h <= 1:
            return
        try:
            frame.pack_propagate(False)
            frame.configure(height=1)
        except tk.TclError:
            return

        def _step(i: int, steps: int) -> None:
            t = (i + 1) / steps
            h = max(1, round(target_h * _ease_out_quad(t)))
            frame.configure(height=h)

        def _on_done() -> None:
            try:
                frame.pack_propagate(True)
            except tk.TclError:
                pass

        _animate(frame, steps=8, interval_ms=20, fn=_step, on_done=_on_done)

    def _animate_frame_collapse(self, frame: tk.Frame) -> None:
        """펼쳐진 프레임을 높이 reqheight→0 으로 접은 뒤 `pack_forget()`
        한다(등장의 역방향). `MOTION_ENABLED=False` 면 기존과 바이트
        동일하게 즉시 `pack_forget()`."""
        if not _motion_enabled() or _animate is None:
            frame.pack_forget()
            return
        try:
            if frame.winfo_manager() != "pack":
                return
            frame.update_idletasks()
            start_h = frame.winfo_height()
        except tk.TclError:
            frame.pack_forget()
            return
        if start_h <= 1:
            frame.pack_forget()
            return
        try:
            frame.pack_propagate(False)
            frame.configure(height=start_h)
        except tk.TclError:
            frame.pack_forget()
            return

        def _step(i: int, steps: int) -> None:
            t = (i + 1) / steps
            h = max(1, round(start_h * (1 - _ease_out_quad(t))))
            frame.configure(height=h)

        def _on_done() -> None:
            try:
                frame.pack_forget()
                frame.pack_propagate(True)
            except tk.TclError:
                pass

        _animate(frame, steps=8, interval_ms=20, fn=_step, on_done=_on_done)

    def _set_active_group(self, name: str) -> None:
        """"더 보기" 안 아코디언 그룹 전환 — 클릭된(또는 show_tab 이 유도한)
        그룹의 항목 프레임만 펼치고 나머지는 접는다. 그룹 헤더 3개는 항상
        보이며(펼침/접힘 화살표만 바뀜), 실제 탭 버튼 목록만 토글된다.

        새로 활성화되는 그룹은 높이 애니메이션으로 펼친다(9호 Phase 5).
        비활성화되는 그룹은 **즉시(동기) pack_forget()** — `_adv_frame`
        전체 열기/닫기(`_toggle_advanced`)와 달리, 그룹 전환은
        `test_gui_navigation.py`(코더 G)의 §0.2 계약 테스트가 "다른
        그룹은 이미 접혀 있다"를 `_set_active_group()` 호출 직후 이벤트
        루프 펌프 없이 동기적으로 확인한다(실측 회귀 확인 — 접기까지
        애니메이션으로 늦추면 두 그룹이 한순간 동시에 packed 상태로
        보여 그 단언이 깨진다). 애니메이션 자체는 손실 없음 — 펼침
        효과만으로도 그룹 전환이 부드럽게 보인다."""
        frames = getattr(self, "_adv_group_frames", None)
        headers = getattr(self, "_adv_group_headers", None)
        if not frames or not headers or name not in frames:
            return
        self._adv_active_group = name
        for gname, frame in frames.items():
            is_active = gname == name
            if is_active:
                self._animate_frame_expand(frame, fill="x", after=headers[gname])
            else:
                frame.pack_forget()
            try:
                headers[gname].config(
                    text=self._group_header_text(gname, is_active),
                    fg=C["accent"] if is_active else C["muted"])
            except tk.TclError:
                pass

    # 숫자 단축키는 Ctrl+1~9 로 앞 9탭만 커버 가능(Ctrl-10 은 유효 keysym 아님).
    # 최초 설정에 필요한 settings(3번째)는 이미 숫자로 커버되지만, 관용적으로
    # 기억하기 쉬운 Ctrl+, 도 병행 배정한다. profiles→Ctrl+P 도 마찬가지.
    _NAMED_TAB_SHORTCUTS = {
        "settings": "<Control-comma>",
        "profiles": "<Control-p>",
    }

    # Tk 바인딩 시퀀스 → 사람이 읽는 힌트(라벨용). 바인딩과 표기를 한 소스에서.
    _SHORTCUT_HINT_TEXT = {
        "<Control-comma>": "Ctrl+,",
        "<Control-p>": "Ctrl+P",
    }

    def _tab_shortcut_hints(self, tabs) -> dict:
        """탭 키 → 사이드바 라벨에 붙일 단축키 힌트 문자열 맵.

        :meth:`_bind_tab_shortcuts` 와 동일한 규칙에서 파생한다(라벨↔바인딩
        불일치 방지): 앞 9탭은 ``Ctrl+1``~``Ctrl+9``, 이후 named 탭은
        :data:`_NAMED_TAB_SHORTCUTS` 시퀀스를 사람이 읽는 형태로.

        Args:
            tabs: ``(key, label)`` 순서 리스트 — 숫자 단축키 순번의 기준.

        Returns:
            단축키가 있는 탭 키만 담은 ``{key: "Ctrl+..."}`` dict.
        """
        hints: dict = {}
        for index, (key, _label) in enumerate(tabs[:9]):
            hints[key] = f"Ctrl+{index + 1}"
        for key, sequence in self._NAMED_TAB_SHORTCUTS.items():
            hint = self._SHORTCUT_HINT_TEXT.get(sequence)
            if hint:
                hints[key] = hint
        return hints

    def _bind_tab_shortcuts(self) -> None:
        """탭 전환 키보드 단축키를 바인딩한다.

        Ctrl+1~9 는 사이드바 앞 9탭에 순서대로(Ctrl-10 은 Tk 에서 유효 keysym 이
        아니므로 숫자는 9개까지만). 뒤쪽 탭은 마우스 전용이 되지 않도록
        :data:`_NAMED_TAB_SHORTCUTS` 의 문자 단축키로 접근한다. 존재하지 않는
        탭 키가 매핑에 있어도 조용히 건너뛴다(탭 구성 변경에 견고).
        """
        tab_keys = getattr(self, "_tab_order", list(self.pages.keys()))
        for index, key in enumerate(tab_keys[:9]):
            self.root.bind(f"<Control-Key-{index + 1}>",
                           lambda e, k=key: self.show_tab(k, user=True))
        for key, sequence in self._NAMED_TAB_SHORTCUTS.items():
            if key in self.pages:
                self.root.bind(sequence,
                               lambda e, k=key: self.show_tab(k, user=True))

    def _toggle_advanced(self):
        """사이드바 '더 보기' 그룹 펼치기/접기(9호 Phase 5 — 높이 애니메이션은
        `_animate_frame_expand`/`_animate_frame_collapse` 위임)."""
        self._adv_open = not self._adv_open
        if self._adv_open:
            self._animate_frame_expand(self._adv_frame, fill="x", after=self._adv_toggle_btn)
            self._adv_toggle_btn.config(text="더 보기  ▴")
        else:
            self._animate_frame_collapse(self._adv_frame)
            self._adv_toggle_btn.config(text="더 보기  ▾")

    def show_tab(self, key: str, user: bool = False):
        """탭을 전환한다.

        Args:
            key: 전환할 탭 키.
            user: 사이드바 버튼 클릭·키보드 단축키로 호출됐으면 True(사이드바
                개인화 §2 의 사용 집계 대상). 발행 후 자동 전환처럼 코드가
                프로그램적으로 부르는 호출은 기본값 False 로 두어 집계하지
                않는다(프로필 배지·팝오버의 "탭 열기" 버튼도 마찬가지).
        """
        if user:
            self._record_tab_usage(key)
        # 접혀 있는 고급 탭으로 이동하면 자동 펼침 — 하이라이트가 보이게.
        # 아코디언 그룹 전환도 함께: 그 키가 속한 그룹으로 전환해(다른 그룹은
        # 접힘) 어느 화면에서 딥링크로 들어와도 해당 항목이 보이게 한다.
        if key in getattr(self, "_advanced_keys", ()):
            if not self._adv_open:
                self._toggle_advanced()
            group = getattr(self, "_adv_group_of_tab", {}).get(key)
            if group is not None:
                self._set_active_group(group)
        for p in self.pages.values():
            p.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        for k, row in self.tab_btns.items():
            row.set_active(k == key)

    @staticmethod
    def _record_tab_usage(key: str) -> None:
        """사이드바 버튼/단축키로 연 탭만 ``ui_prefs`` 의 ``tab_usage`` 에
        누적한다(auto-dev 6호 §2). 개인화는 **다음 실행**에만 반영되므로
        여기서는 저장만 하고 사이드바를 다시 그리지 않는다 — 세션 중 버튼
        위치가 움직이면 사용자가 혼란스럽다."""
        try:
            from gui import ui_prefs
            usage = dict(ui_prefs.get("tab_usage", {}) or {})
            usage[key] = int(usage.get(key, 0) or 0) + 1
            ui_prefs.set("tab_usage", usage)
        except Exception:  # noqa: BLE001 — 통계 저장 실패가 탭 전환을 막으면 안 됨
            log.debug("탭 사용 기록 실패", exc_info=True)

    # 활동 바 색상 인자(레거시 호출부 다수가 C["yellow"] 등 원색을 그대로
    # 넘긴다) → StatusPill kind 매핑. 어떤 색도 해당 안 되면 neutral.
    def _status_kind_for_color(self, color) -> str:
        if not color:
            return "neutral"
        mapping = {
            C.get("green"):  "ok",
            C.get("red"):    "error",
            C.get("yellow"): "warn",
            C.get("orange"): "warn",
            C.get("accent"): "info",
        }
        return mapping.get(color, "neutral")

    @staticmethod
    def _neutral_status_text(text: str) -> str:
        """kind=neutral 상태에도 아이콘 글리프가 보이게 한다(캡처 심사 🟡3).

        ``widgets.StatusPill.set()`` 은 ``_KIND_ICON_NAME`` 매핑상 neutral 을
        일부러 아이콘 없음(``None``)으로 정의한다(코더 A `widgets.py`, 내
        파일 경계 밖). 대기 상태에서 pill 이 텍스트만 덩그러니 있는 문제를
        내 파일(app.py) 안에서만 고치기 위해, neutral 텍스트에 직접 시계
        글리프를 접두한다 — ``StatusPill.set()`` 은 이미 글리프가 붙은
        텍스트를 다시 건드리지 않으므로(neutral 은 자동 접두를 안 함) 이중
        접두 위험이 없다. 다른 kind(ok/warn/error/info)는 `.set()` 자체가
        이미 접두하므로 손대지 않는다.
        """
        if not icons.has_icon_font():
            return text
        glyph = icons.glyph("clock")
        if not glyph or glyph == "•":
            return text
        return f"{glyph} {text}"

    def set_status(self, text: str, color: str = None):
        """활동 바 상태 pill 을 갱신한다.

        워커 스레드(발행/로그인/ComfyUI)가 직접 호출해도 안전하도록,
        메인 스레드가 아니면 ``root.after(0, ...)`` 로 메인 루프에 재위임한다
        (Tkinter 는 thread-safe 하지 않음 → "main thread is not in main loop"
        크래시 방지). 창이 이미 파괴된 뒤 콜백/에러로그 경로에서 호출돼도
        무해하도록 ``tk.TclError`` 는 삼킨다.

        디자인 시스템 v3 §8(이모지 전면 제거) — 호출부가 수십 곳(발행·
        보관함·로그인 등 워커 스레드)에 흩어져 있어 각 리터럴을 일일이
        고치는 대신, 이 메서드를 렌더 경계의 단일 관문으로 삼아
        ``_strip_emoji`` 로 걸러낸다(호출부 문자열은 원본 유지 — 로그·
        `_status_log` 테스트 캡처는 그대로 "이미 실행 중" 등 부분 문자열
        매칭이라 영향 없음).
        """
        if threading.current_thread() is not threading.main_thread():
            try:
                self.root.after(0, self.set_status, text, color)
            except (tk.TclError, RuntimeError):
                # 창 파괴 후엔 after 예약도 실패할 수 있음 — 무해하게 무시
                pass
            return
        try:
            kind = self._status_kind_for_color(color)
            display = _strip_emoji(text)
            if kind == "neutral":
                display = self._neutral_status_text(display)
            self.status_lbl.set(display, kind)
        except tk.TclError:
            pass

    # ── RunBus 구독(활동 바 렌더) ───────────────────────
    def on_run_state(self, running: bool, label: str) -> None:
        """실행 중일 때만 진행행 + 중지 IconButton 을 활동 바에 표시한다."""
        self._current_run_label = label if running else None
        row = getattr(self, "_activity_progress", None)
        stop_btn = getattr(self, "_activity_stop_btn", None)
        if row is None:
            return
        try:
            if running:
                row.indeterminate(label or "")
                row.pack(side="left", fill="x", expand=True,
                         padx=(SP["sm"], SP["sm"]))
                if stop_btn is not None:
                    stop_btn.pack(side="left", padx=(0, SP["sm"]))
            else:
                row.pack_forget()
                row.reset()
                if stop_btn is not None:
                    stop_btn.pack_forget()
        except tk.TclError:
            pass

    def on_progress(self, cur: int, total: int, text: str = "") -> None:
        row = getattr(self, "_activity_progress", None)
        if row is None:
            return
        try:
            row.set(cur, total, text)
        except tk.TclError:
            pass

    # ── 재진입 안내(모달 → 토스트+깜빡임) ─────────────────
    def _warn_already_running(self) -> None:
        """실행 중 재진입 시도 안내 — 모달 대신 토스트+활동바 깜빡임.

        상태바 문구("이미 실행 중")는 기존 계약 그대로 유지한다(테스트가
        문자열로 검사) — 이 메서드가 유일한 발신 지점이다.
        """
        self.set_status("⏳ 이미 실행 중 — 완료 후 재시도", C["yellow"])
        label = strip_emoji(getattr(self, "_current_run_label", None) or "다른 작업")
        toast = getattr(self, "toast", None)
        if toast is not None:
            try:
                toast.show(f"지금 '{label}'이 진행 중이에요. 끝나면 다시 눌러 주세요",
                           kind="warn")
            except Exception:  # noqa: BLE001 — 토스트 실패가 재진입 안내를 막으면 안 됨
                pass
        self._blink_activity_bar()

    def _blink_activity_bar(self) -> None:
        bar = getattr(self, "_activity_bar", None)
        if bar is None:
            return
        try:
            original_bg = bar.cget("bg")
            bar.config(bg=C["yellow"])
            self.ui_after(self._restore_activity_bar_bg, bar, original_bg, delay=300)
        except tk.TclError:
            pass

    def _restore_activity_bar_bg(self, bar, bg) -> None:
        try:
            bar.config(bg=bg)
        except tk.TclError:
            pass

    # ── 실행 ──────────────────────────────────────────
    def _acquire_run_slot(self, warn: bool = True) -> bool:
        """실행 슬롯을 원자적으로 점유한다(메인 스레드 전용).

        세 실행 진입점(run_test·_start_run·_run_fill_drafts)이 공유하는
        단일 워커 슬롯의 재진입 가드. Tkinter 는 단일 메인 스레드에서만
        이벤트 콜백을 처리하므로, 이 검사+세팅 사이에 메인 루프를 재진입시키는
        호출(messagebox·wait_window 등)이 없으면 검사와 점유가 원자적이다
        → 버튼 더블클릭으로 두 콜백이 큐잉돼도 두 번째는 반드시 막힌다.

        슬롯을 실제로 잡은 뒤에만 True 를 돌려주고, 그 경우 호출부는 워커를
        띄우거나(성공) 즉시 ``self._running=False`` 로 반납해야(실패) 한다.

        Args:
            warn: 이미 실행 중일 때 사용자에게 안내할지. False 면 조용히
                False 만 돌려준다(호출부가 자체 안내를 할 때).

        Returns:
            슬롯을 새로 점유했으면 True, 이미 실행 중이면 False.
        """
        if self._running:
            if warn:
                self._warn_already_running()
            return False
        self._running = True
        return True

    def request_run_slot(self, label: str, warn: bool = True) -> bool:
        """`_acquire_run_slot` 의 새 이름 래퍼(설계서 §1.5) — 다른 코더가
        새 실행 진입점을 만들 때 이 이름으로 호출한다. 현재 동작은
        `_acquire_run_slot` 과 동일(경고는 토스트+깜빡임으로 통일돼 있음).

        Args:
            label: 시작하려는 작업 이름(현재는 향후 확장을 위한 자리 표시자
                — 실패 시 안내 문구는 `_current_run_label`, 즉 *이미* 실행
                중인 작업의 이름을 쓴다).
            warn: `_acquire_run_slot` 과 동일.
        """
        return self._acquire_run_slot(warn=warn)

    def _release_run_slot(self) -> None:
        """준비 단계가 잡고 있던 실행 슬롯을 반납한다(메인 스레드 전용)."""
        self._prep_owns_slot = False
        self._running = False

    def run_today(self):
        # 슬롯을 **클릭 즉시** 원자적으로 잡는다. 예전엔 preflight(수 초~수십 초)
        # 가 끝나고 확인 팝업이 뜬 뒤에야 `_running` 이 켜져, 그 사이 버튼이 계속
        # 살아 있어 더블클릭이 통과했고 화면에도 아무 변화가 없어 "눌렀는데 아무
        # 일도 안 일어난다"로 보였다.
        if not self._acquire_run_slot():
            return
        if not self.cfg.get("naver_id"):
            self._release_run_slot()
            messagebox.showerror("설정 필요", "네이버 계정을 설정 탭에서 입력해 주세요.")
            self.show_tab("settings")
            return

        self._prep_owns_slot = True
        self._stop.clear()          # 준비 단계도 [중지] 로 취소할 수 있게
        # ── 준비 → pre-flight 순서 ──────────────────────────────
        # 브라우저 준비(로그인 포함)를 **후보 분석보다 먼저** 한다. 예전엔 느린
        # 후보 분석이 앞에 있어, 로그인이 필요한 상태에서도 사용자는 수십 초
        # 동안 아무 창도 못 보고 기다려야 했다.
        # test_run_reentry_guard.py 가 이 경로를 fake-app 으로 실행하지 않으므로
        # self.spawn(AppLifecycle) 을 안전하게 쓸 수 있다(스레드 핸들 추적).
        self.set_status("⏳ 발행 준비 중...", C["accent"])
        self.spawn(self._preflight_then_run, name="preflight")

    # ── 발행 브라우저 준비(attach 모드) ─────────────────────────
    def _publish_login_url(self) -> str:
        return "https://nid.naver.com/nidlogin.login?" + urlencode({
            "url": "https://www.naver.com/main",
        })

    def _probe_publish_login(self, port: int) -> str:
        """발행 세션 크롬의 네이버 로그인 상태 판정('match'|'mismatch'|'unknown').

        읽기 전용 — 인증된 네이버 홈페이지의 로그인 사용자 ID를 대상 계정과
        대조한다. 현재 탭을 유지하며, 필요한 임시 확인 탭은 core helper 가 정리한다.
        실제 계정 값은 반환하지 않는다.
        """
        driver = None
        try:
            from core import naver_uploader as _nu
            from config import settings as s
            driver = _nu.attach_to_session(port, quiet=True)
            if driver is None:
                return "unknown"
            logged_in = _nu.read_authenticated_naver_user_id(
                driver, allow_homepage_probe=True)
            _ok, _reason, verdict = _nu.verify_publish_identity(
                getattr(s, "NAVER_ID", "") or "", logged_in)
            return verdict
        except Exception as e:  # noqa: BLE001 — attach/판독 실패는 unknown 으로 수렴
            log.debug(f"발행 세션 로그인 확인 실패: {type(e).__name__}")
            return "unknown"
        finally:
            if driver is not None:
                try:
                    from scheduler.auto_scheduler import _release_driver
                    _release_driver(driver, "attach", quiet=True)
                except Exception as e:  # noqa: BLE001 — attach 해제 실패는 세션 유지
                    log.debug("발행 세션 attach 해제 실패: %s", type(e).__name__)

    def _open_login_page_in_session(self, port: int) -> bool:
        """발행 세션 크롬에 **새 탭**으로 네이버 로그인 페이지를 띄운다.

        사용자가 보고 있던 탭을 빼앗지 않는다. 성공 True / 실패 False.
        """
        driver = None
        try:
            from core import naver_uploader as _nu
            driver = _nu.attach_to_session(port, quiet=True)
            if driver is None:
                return False
            try:
                driver.switch_to.new_window("tab")
            except Exception:  # noqa: BLE001 — 사용자 탭을 로그인 페이지로 바꾸지 않는다
                log.warning("발행 세션 로그인용 새 탭을 열지 못했습니다")
                return False
            driver.get(self._publish_login_url())
            return True
        except Exception as e:  # noqa: BLE001
            log.warning("발행 세션 로그인 페이지 열기 실패: %s",
                        type(e).__name__)
            return False
        finally:
            if driver is not None:
                try:
                    from scheduler.auto_scheduler import _release_driver
                    _release_driver(driver, "attach", quiet=True)
                except Exception as e:  # noqa: BLE001 — attach 해제 실패는 세션 유지
                    log.debug("발행 세션 attach 해제 실패: %s", type(e).__name__)

    def _wait_for_session_login(self, port: int, timeout: float = 300.0) -> bool:
        """로그인 창을 띄우고 사용자의 '인증완료'(또는 자동 감지)를 기다린다.

        기다리는 동안 무한 재시도하지 않는다 — `timeout` 초 뒤에는 실패로
        끝내고, `[중지]`(``self._stop``) 를 누르면 즉시 취소된다.

        Returns:
            로그인이 확인됐으면 True, 취소·시간초과면 False.
        """
        self.auth_event.clear()
        self._last_publish_login_verdict = "unknown"
        opened = self._open_login_page_in_session(port)
        self.bus.auth("waiting")
        if opened:
            self.bus.log("🔐 발행용 크롬에 네이버 로그인 창을 열었어요 — "
                         "그 창에서 로그인한 뒤 '인증완료'를 누르세요", "warn")
            self.bus.status("🔐 로그인 대기 중 — 발행용 크롬 창을 확인하세요", C["orange"])
        else:
            self.bus.log("🔐 발행용 크롬 창에서 네이버에 로그인한 뒤 "
                         "'인증완료'를 누르세요(로그인 페이지 자동 열기는 실패)", "warn")
            self.bus.status("🔐 로그인 대기 중 — 발행용 크롬 창을 확인하세요", C["orange"])

        deadline = time.monotonic() + max(10.0, float(timeout))
        next_probe = time.monotonic()
        warned_unknown = False
        try:
            while time.monotonic() < deadline:
                if self._stop.is_set():
                    return False
                wait_for = min(3.0, max(0.0, next_probe - time.monotonic()),
                               max(0.0, deadline - time.monotonic()))
                clicked = self.auth_event.wait(timeout=wait_for)
                if self._stop.is_set():
                    return False
                if clicked:
                    self.auth_event.clear()
                if clicked or time.monotonic() >= next_probe:
                    verdict = self._probe_publish_login(port)
                    self._last_publish_login_verdict = verdict
                    if self._stop.is_set():
                        return False
                    if verdict == "match":
                        return True
                    if verdict == "mismatch":
                        return False
                    if clicked:
                        if not warned_unknown:
                            self.bus.log(
                                "🔐 아직 로그인을 확인하지 못했어요 — 로그인 상태를 다시 확인합니다",
                                "warn")
                            warned_unknown = True
                        self.bus.auth("waiting")
                    next_probe = time.monotonic() + 30.0
            return False
        finally:
            self.auth_event.clear()
            self.bus.auth("done")

    def _prepare_publish_browser(self):
        """발행용 크롬을 준비한다(워커 스레드). ``(ok, 실패사유)`` 반환.

        spawn 모드는 파이프라인이 직접 창을 띄우고 로그인하므로 그대로 통과한다
        (기존 동작 불변). attach 모드만 여기서 세션 크롬 기동 → 로그인 확인 →
        (필요하면) 로그인 대기까지 끝내고 같은 실행을 이어간다.
        """
        mode = str(self.cfg.get("publish_mode", "spawn")).lower()
        if mode != "attach":
            return True, ""
        from core import chrome_session
        from config import settings as s
        port = int(getattr(s, "CHROME_ATTACH_PORT", 9222))

        self.bus.status("🖥 발행용 크롬 준비 중...", C["accent"])
        state = chrome_session.ensure_session(port)
        if state == "not_found":
            return False, ("크롬(chrome.exe)을 찾지 못했어요. 크롬을 설치한 뒤 "
                           "다시 실행하세요.")
        if state == "error":
            return False, (f"발행용 크롬을 열지 못했어요. 다른 프로그램이 포트 "
                           f"{port} 를 쓰고 있는지 확인하세요.")
        if state == "started":
            self.bus.log("🖥 발행용 크롬을 새로 띄웠어요 — 준비될 때까지 기다립니다", "info")
            if not chrome_session.wait_until_running(port, timeout=20.0):
                return False, (f"발행용 크롬이 준비되지 않았어요(포트 {port} 응답 없음). "
                               f"다른 프로그램이 그 포트를 쓰고 있을 수 있어요.")
        if self._stop.is_set():
            return False, "사용자가 취소했어요."

        self.bus.status("🔐 네이버 로그인 확인 중...", C["accent"])
        verdict = self._probe_publish_login(port)
        if verdict == "mismatch":
            return False, ("발행용 크롬이 설정과 **다른 네이버 계정**으로 로그인돼 "
                           "있어요. 그 창에서 로그아웃한 뒤 설정된 계정으로 다시 "
                           "로그인하세요.")
        if verdict != "match":
            if not self._wait_for_session_login(port):
                if self._stop.is_set():
                    return False, "사용자가 취소했어요."
                if getattr(self, "_last_publish_login_verdict", "unknown") == "mismatch":
                    return False, ("발행용 크롬이 설정과 다른 네이버 계정으로 "
                                   "로그인돼 있어요. 설정된 계정으로 다시 로그인하세요.")
                return False, ("로그인을 확인하지 못했어요(시간 초과). 발행용 크롬 "
                               "창에서 네이버에 로그인한 뒤 다시 실행하세요.")
            verdict = getattr(self, "_last_publish_login_verdict", "unknown")
            if verdict != "match":
                return False, ("로그인 확인 신호 뒤 계정 판독이 불확실해졌어요. "
                               "발행용 크롬 창에서 로그인 상태를 확인한 뒤 다시 시도하세요.")
        self.bus.log("🖥 발행용 크롬 준비 완료 — 로그인 확인됨", "ok")
        return True, ""

    def _abort_prep(self, reason: str) -> None:
        """준비 단계 실패 — 슬롯을 반드시 풀고 **진짜 이유**를 보여준다.

        '0/0 완료' 로 위장하지 않는다(메인 스레드 전용)."""
        self._release_run_slot()
        self.bus.status(f"❌ 시작하지 못했어요", C["red"])
        self.bus.log(f"❌ 발행을 시작하지 못했어요 — {reason}", "err")
        toast = getattr(self, "toast", None)
        if toast is not None:
            try:
                toast.show(reason, kind="error")
                return
            except Exception:  # noqa: BLE001
                pass
        messagebox.showerror("발행을 시작하지 못했어요", reason)

    def run_test(self):
        # 재진입(더블클릭 중복 실행) 원자 가드: _running 세팅을 워커 진입까지
        # 미루면 검사~세팅 사이 TOCTOU 창에 두 번째 클릭이 통과해 워커가 2개
        # 뜬다(같은 pt 위젯·_stop 공유 → 로그 뒤섞임). Tk 는 단일 메인 스레드라
        # 여기서 검사+세팅을 한 번에 하면 원자적이다. start 실패 시 되돌린다.
        #
        # ⚠ 여기·_start_run·run_fill_drafts 는 self.spawn 대신 raw
        # threading.Thread 를 그대로 쓴다 — tests/test_run_reentry_guard.py 가
        # 모듈 전역 `threading`(gui.app.threading) 을 통째로 가짜 네임스페이스로
        # monkeypatch 해 워커가 실제로 뜨지 않게 막는데, self.spawn 은
        # gui/async_utils.py 가 독자적으로 import 한 threading 을 쓰므로 그
        # monkeypatch 를 우회해버려(진짜 스레드가 뜸) 테스트가 깨진다. 대신
        # 스레드 핸들이 있으면 `_threads` 에도 보관해(있으면) 추적 일관성을
        # 최대한 맞춘다 — object.__new__(App) 로 만든 fake app 은 `_threads`
        # 가 없으므로 getattr 로 건너뛴다.
        if not self._acquire_run_slot():
            return
        try:
            self.show_tab("posting")
            self._stop.clear()
            th = threading.Thread(target=self._run, args=(True,), daemon=True)
            threads = getattr(self, "_threads", None)
            if threads is not None:
                threads.append(th)
            th.start()
        except Exception:
            # 워커 spawn 실패 — 슬롯을 즉시 반납해 다음 실행을 막지 않는다
            self._running = False
            log.error("run_test 워커 시작 실패", exc_info=True)
            raise

    def _preflight_then_run(self):
        """발행 준비(브라우저·로그인) → pre-flight 분석 → 발행 시작/팝업.

        백그라운드 스레드에서 실행. 모든 위젯 변경은 self.ui_after() 로 메인
        스레드에 위임한다. 어떤 경로로 끝나든 실행 슬롯은 반드시 풀리거나
        (_abort_prep) 워커로 넘어간다(_start_run) — 조용히 잠긴 채 남지 않는다.
        """
        try:
            self._preflight_then_run_inner()
        except BaseException as e:  # noqa: BLE001 — 슬롯을 든 채 죽지 않게
            log.error("발행 준비 스레드 예외", exc_info=True)
            self.ui_after(self._abort_prep,
                          f"준비 중 예상치 못한 오류가 났어요: {type(e).__name__}: {e}")

    def _preflight_then_run_inner(self):
        # ① 발행용 브라우저부터 — 실패하면 진짜 이유를 내고 슬롯을 푼다.
        try:
            ok, reason = self._prepare_publish_browser()
        except Exception as e:  # noqa: BLE001 — 준비 단계 예외도 사유로 수렴
            log.error("발행 브라우저 준비 실패", exc_info=True)
            ok, reason = False, f"발행용 브라우저를 준비하지 못했어요: {e}"
        if not ok:
            self.ui_after(self._abort_prep, reason)
            return
        if self._stop.is_set():
            self.ui_after(self._abort_prep, "사용자가 취소했어요.")
            return

        # ② 후보 분석(느림) — 브라우저가 준비된 뒤에 한다.
        self.ui_after(self.set_status, "🔍 보관함 점검 중...", C["accent"])
        try:
            from scheduler.auto_scheduler import preflight_check
            summary = preflight_check()
        except Exception:
            log.error("preflight_check 실패 — 일반 발행으로 진행", exc_info=True)
            # ⚠️ ui_after 의 키워드는 delay 전용 — fn 에 줄 kwargs 는 람다로 바인딩
            # (2026-09-03 라이브 실측: kwargs 직접 전달이 TypeError 로 preflight
            # 스레드를 죽여 '오늘 실행'이 시작되지 않았음).
            self.ui_after(lambda: self._start_run(allowed_categories=None))
            return  # _start_run 이 실발행 확인 팝업을 띄운다 (편수 미상 → None)

        if self._stop.is_set():
            self.ui_after(self._abort_prep, "사용자가 취소했어요.")
            return
        self.ui_after(self.set_status, "● 대기 중", C["muted"])

        if summary.get("fully_covered") or summary.get("total_candidates", 0) == 0:
            # 매칭 100% — 실발행 확인만 받고 진행 (되돌릴 수 없는 예약발행이므로)
            n = summary.get("total_candidates", 0)
            # fn kwargs 는 람다 바인딩(위 주석 참조 — ui_after 키워드는 delay 전용).
            self.ui_after(lambda n=n: self._start_run(
                allowed_categories=None, expected_count=n))
            return

        # 매칭 미달 — 모달 팝업으로 사용자 선택 받기
        self.ui_after(self._show_preflight_dialog, summary)

    def _show_preflight_dialog(self, summary: dict):
        """pre-flight 결과 모달 다이얼로그. 메인 스레드에서만 호출.

        세 가지 선택:
          1. 부족 카테고리 보관함 채우기 (1개 카테고리만 — 카테고리 선택 후 편수 입력)
          2. 매칭되는 카테고리만 실행 (allowed_categories=후보 중 매칭된 카테고리)
          3. 취소
        """
        from gui.tabs.dashboard import _PreflightDialog
        dlg = _PreflightDialog(self.root, summary)
        self.root.wait_window(dlg.top)

        choice = dlg.result.get("action")
        if choice == "fill":
            target_cat = dlg.result.get("category")
            n = dlg.result.get("count")
            # 보관함 채우기는 별도 진입점이므로 준비 단계가 잡은 슬롯을 먼저 반납한다
            # (안 그러면 run_fill_drafts 가 "이미 실행 중"으로 막힌다).
            self._release_run_slot()
            self.set_status("● 대기 중", C["muted"])
            if target_cat and n and n > 0:
                # run_fill_drafts 는 자체적으로 확인 dialog 띄움
                self.run_fill_drafts(category=target_cat, n=n)
            return
        if choice == "matched":
            covered_cats = {
                cat for cat, info in summary.get("by_category", {}).items()
                if info.get("covered") and info.get("needed", 0) > 0
            }
            if not covered_cats:
                self._release_run_slot()
                messagebox.showinfo(
                    "발행 시작 불가",
                    "매칭되는 카테고리가 없습니다. 보관함을 먼저 채워주세요.",
                )
                return
            n = sum(
                info.get("needed", 0)
                for cat, info in summary.get("by_category", {}).items()
                if cat in covered_cats
            )
            self._start_run(allowed_categories=covered_cats, expected_count=n)
            return
        # cancel — 준비 단계가 잡고 있던 슬롯을 반납한다(안 그러면 버튼이 영영 잠긴다)
        self._release_run_slot()
        self.set_status("● 대기 중", C["muted"])

    def _start_run(self, allowed_categories=None, expected_count=None):
        """실제 발행 워커 스레드를 띄운다 (run_today 의 두 번째 단계).

        이 메서드는 실제 예약발행(dry_run=False·save_only=False)으로 들어가는
        유일한 관문이다. 되돌릴 수 없는 작업이므로 워커 시작 직전 1회
        확인 팝업을 띄운다('보관함 채우기'가 이미 이중확인을 받는 것과 대칭).
        테스트(run_test→_run(True)) · 보관함 채우기(_run_fill_drafts,
        save_only) 경로는 이 메서드를 거치지 않으므로 확인 대상이 아니다.

        메인 스레드에서만 호출된다(모든 호출부가 root.after 경유 또는 메인
        스레드 이벤트 핸들러) → messagebox 가 메인 루프에서 안전하게 뜬다.

        Args:
            allowed_categories: 발행 대상 카테고리 필터(None 이면 전체).
            expected_count: 예약발행 예정 편수(안내 문구용). 미상이면 None.
        """
        # run_today 준비 단계가 이미 슬롯을 잡아둔 경우(정상 경로)엔 그 슬롯을
        # 이어받는다. 그 외(다른 진입점이 실행 중)만 "이미 실행 중"으로 막는다.
        owns = bool(getattr(self, "_prep_owns_slot", False))
        if self._running and not owns:
            self._warn_already_running()
            return

        # ── 실발행 확인 (되돌릴 수 없음) ──────────────────────────
        # 확인 팝업은 메인 루프를 재진입시키므로(대기 중 다른 클릭 콜백이 큐잉될
        # 수 있음), 슬롯 점유는 반드시 '예' 직후에 한다: 확인 → _running=True →
        # 워커 start. '아니오'/취소 시 _running 을 건드리지 않는다.
        count_phrase = (
            f"{expected_count}편을" if expected_count and expected_count > 0
            else "후보 글을"
        )
        if not messagebox.askyesno(
                "오늘 실행 — 실제 예약발행",
                f"{count_phrase} 네이버에 예약발행 등록합니다.\n"
                "현재 이미지 생성 설정·사용량 한도 안에서 출처 기록이 없는 보관 이미지를 이번 실행에 새로 생성해 복구합니다.\n"
                "출처가 확인된 이미지는 재사용하고 원본 파일은 보존합니다.\n"
                "이미지 생성에 추가 시간과 생성 사용량이 들 수 있습니다.\n"
                f"되돌릴 수 없습니다. 계속할까요?",
                icon="warning"):
            # 준비 단계가 잡아둔 슬롯이면 여기서 반납한다(아니면 버튼이 잠긴 채 남는다).
            if owns:
                self._release_run_slot()
            self.set_status("● 대기 중", C["muted"])
            return

        # 팝업 대기 동안 다른 진입점이 슬롯을 선점했을 수 있으니 원자 점유로
        # 재확인한다(더블클릭으로 이 팝업이 둘 떠도 실제 점유는 하나만 성공).
        # 이미 준비 단계가 잡은 슬롯을 들고 있으면 그대로 쓴다.
        if owns:
            self._prep_owns_slot = False
        elif not self._acquire_run_slot():
            return
        try:
            self.show_tab("posting")
            self._stop.clear()
            # raw threading.Thread 유지 이유는 run_test 주석 참조
            # (test_run_reentry_guard.py 의 monkeypatch 우회 방지).
            th = threading.Thread(
                target=self._run,
                kwargs={"allowed_categories": allowed_categories,
                        "repair_legacy_images": True},
                daemon=True)
            threads = getattr(self, "_threads", None)
            if threads is not None:
                threads.append(th)
            th.start()
        except Exception:
            self._running = False
            log.error("_start_run 워커 시작 실패", exc_info=True)
            raise

    def stop_run(self):
        self._stop.set()
        self.auth_event.set()  # 인증 대기 중이면 해제하여 데드락 방지
        self.set_status("⏹ 중지 요청됨...", C["yellow"])

    # ── 보관함 채우기 (save_only) ─────────────────────
    def run_fill_drafts(self, category: str = None, n: int = None):
        """보관함을 N편 채웁니다. 발행은 일어나지 않습니다 (save_only=True).

        _run 과 동일한 thread 슬롯을 공유하므로 self._running 가드 필수.

        Args:
            category: 특정 examplebrand 카테고리만 채울 때 지정. None 이면 전체.
                대시보드 "부족 카테고리 채우기" 다이얼로그가 전달 (2026-05-16).
            n: 편수. None 이면 simpledialog 로 묻는다.
        """
        if self._running:
            self._warn_already_running()
            return

        # simpledialog·askyesno 는 메인 루프를 재진입시키므로(대기 중 다른 클릭
        # 콜백이 큐잉될 수 있음), 슬롯 점유는 확인 '예' 직후에 원자적으로 한다.
        from tkinter import simpledialog
        if n is None:
            n = simpledialog.askinteger(
                "보관함 채우기", "보관할 편수 (1~50):",
                parent=self.root, minvalue=1, maxvalue=50, initialvalue=10)
            if n is None:
                return

        cat_label = f"[{category}] " if category else ""
        if not messagebox.askyesno(
                "보관함 채우기",
                f"보관함 {cat_label}{n}편 채우기 시작?\n"
                f"약 {n}×90초 소요. 발행은 일어나지 않습니다(보관만 함)."):
            return

        # 팝업 대기 동안 다른 진입점이 슬롯을 선점했을 수 있으니 원자 점유로
        # 재확인한다(더블클릭으로 이 팝업이 둘 떠도 실제 점유는 하나만 성공).
        if not self._acquire_run_slot():
            return
        try:
            self.show_tab("posting")
            self._stop.clear()
            # raw threading.Thread 유지 이유는 run_test 주석 참조
            # (test_run_reentry_guard.py 의 monkeypatch 우회 방지).
            th = threading.Thread(
                target=self._run_fill_drafts, args=(n, category),
                daemon=True)
            threads = getattr(self, "_threads", None)
            if threads is not None:
                threads.append(th)
            th.start()
        except Exception:
            self._running = False
            log.error("run_fill_drafts 워커 시작 실패", exc_info=True)
            raise

    def _run_fill_drafts(self, n: int, category: str = None):
        """보관함 채우기 실행 워커 (thread).

        슬롯 점유(``self._running=True``)는 진입점 run_fill_drafts 가 확인 팝업
        '예' 직후 메인 스레드에서 이미 원자적으로 했다. 여기서 다시 True 로
        두는 건 중복이지만 무해하다(리셋은 finally 에서만) — 백그라운드 스레드가
        직접 호출되는 테스트/재사용 경로에서도 상태가 일관되게 서도록 유지한다.

        progress_cb/auth_cb 는 ``self.bus.*`` 만 호출한다(포스팅 탭 직접 호출
        제거 — 설계서 §1.4). 단 글별 진행 체크리스트(``update_post_status``/
        ``init_post_list``)는 RunBus 의 5개 발신 동사(status/progress/log/
        auth/run_state)에 대응하는 verb 가 없어 포스팅 탭 참조를 유지한다
        (``self.ui_after`` 로 스레드 안전하게).
        """
        self._running = True
        self.auth_event.clear()
        pt = self.pages["posting"]

        cat_tag = f"[{category}] " if category else ""
        label = f"보관함 채우기 {cat_tag}({n}편)".strip()
        self.bus.begin(label)
        self.bus.status(f"💾 보관함 채우기 {cat_tag}({n}편)", C["accent"])
        self.bus.log(f"{'─'*52}", "muted")
        self.bus.log(
            f"💾 보관함 채우기 시작 {cat_tag}({n}편, 발행 없이 보관만) — "
            f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "info")
        self.ui_after(pt.init_post_list, [])

        _post_list_status = {
            "start":     "running",
            "ok":        "ok",
            "fail":      "fail",
            "duplicate": "skip",
        }

        def progress_cb(title, status, cur, total):
            labels = {
                "start":     (f"처리 중: {title[:28]}...", "info"),
                "ok":        (f"💾 보관: {title[:28]}", "ok"),
                "fail":      (f"❌ 실패: {title[:28]}", "err"),
                "duplicate": (f"⏭ 중복 Skip: {title[:28]}", "dup"),
            }
            msg, tag = labels.get(status, (title, ""))
            self.bus.log(f"  {msg}", tag)
            self.bus.progress(cur, total, msg[:24])
            self.bus.post_event(title, status)
            list_status = _post_list_status.get(status)
            if list_status:
                self.ui_after(pt.update_post_status, title, list_status)

        def auth_cb(state):
            if state == "waiting":
                self.bus.auth("waiting")
                self.bus.log(
                    "⚠️ 보안 인증 필요 — 브라우저에서 인증 후 '인증완료' 버튼을 클릭하세요", "warn")
                self.bus.status("⚠️ 보안 인증 대기 중", C["orange"])
            elif state == "done":
                self.bus.auth("done")
                self.bus.status(f"💾 보관함 채우기 ({n}편)", C["accent"])

        end_kind, end_summary = "ok", ""
        try:
            from scheduler.auto_scheduler import run_pipeline
            result = run_pipeline(
                target_date=None,
                dry_run=False,
                progress_cb=progress_cb,
                stop_flag=self._stop,
                auth_event=self.auth_event,
                auth_cb=auth_cb,
                save_only=True,
                max_count=n,
                fill_category=category,
            )
            saved = result.get("saved_drafts", 0)
            tot = result.get("total", 0)
            quality_skipped = result.get("skipped_quality", 0)

            if result.get("error"):
                reason = ("다른 실행이 진행 중이어서 시작하지 못했습니다."
                          if result["error"] == "already_running"
                          else f"실행하지 못했습니다: {result['error']}")
                self.bus.log(f"⚠️ {reason}", "warn")
                self.bus.status(reason, C["yellow"])
                end_kind, end_summary = "warn", reason
            elif self._stop.is_set():
                self.bus.log(f"\n⏹ 중지됨: {saved}/{tot}편 보관 후 중단", "warn")
                self.bus.status(f"⏹ 중지됨 (보관 {saved}편)", C["yellow"])
                end_kind, end_summary = "warn", f"중지됨 · 보관 {saved}편"
            else:
                self.bus.log(f"\n💾 보관 완료: {saved}편", "ok")
                if quality_skipped > 0:
                    self.bus.log(f"  ⚠️ 품질 게이트 스킵: {quality_skipped}건", "warn")
                    self.bus.status(
                        f"💾 보관 완료 ({saved}편), 품질 스킵 {quality_skipped}건",
                        C["yellow"])
                    end_kind = "warn"
                    end_summary = f"보관 {saved}편 완료 · 품질 스킵 {quality_skipped}건"
                else:
                    self.bus.status(f"💾 보관 완료 ({saved}편)", C["green"])
                    end_kind, end_summary = "ok", f"보관함 {saved}편 완료"
        except Exception as e:
            log.error("보관함 채우기 실행 오류", exc_info=True)
            self.bus.log(f"❌ 오류: {e}", "err")
            self.bus.status(f"❌ {str(e)[:30]}", C["red"])
            end_kind, end_summary = "error", f"오류로 중단됨 · {str(e)[:40]}"
        finally:
            self._running = False
            self.ui_after(pt.hide_auth_waiting)
            self.auth_event.clear()
            self.bus.end(end_summary, end_kind)
            self.ui_after(self.dashboard.refresh, delay=200)
            self.ui_after(self.pages["logs"].refresh, delay=200)
            self.ui_after(self.pages["home"].refresh, delay=200)

    def _run(self, dry_run: bool = False, allowed_categories=None,
             repair_legacy_images: bool = False):
        """오늘 발행/테스트 실행 워커(thread). ``_run_fill_drafts`` 와 동일한
        RunBus 배선 원칙을 따른다(위 docstring 참조)."""
        # 슬롯 점유(_running=True)는 진입점(run_test·_start_run)이 워커 start
        # 직전 메인 스레드에서 이미 원자적으로 했다. 여기 재세팅은 중복이지만
        # 무해하다(리셋은 finally 에서만) — 워커가 직접 호출되는 테스트/재사용
        # 경로에서도 상태가 일관되게 서도록 유지한다.
        self._running = True
        self.auth_event.clear()  # 인증 이벤트 초기화
        pt  = self.pages["posting"]
        mode = "[테스트]" if dry_run else "[실행]"
        label = "테스트 실행" if dry_run else "오늘 발행"
        self.bus.begin(label)

        self.bus.status("● 실행 중", C["green"])
        self.bus.log(f"{'─'*52}", "muted")
        self.bus.log(f"{mode} 시작 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}", "info")
        if allowed_categories:
            self.bus.log(
                f"  🎯 카테고리 필터: {', '.join(sorted(allowed_categories))}",
                "info")
        # 글별 진행 상태 체크리스트 초기화 (제목은 progress_cb에서 동적 추가)
        self.ui_after(pt.init_post_list, [])

        # progress_cb status → post_list status 매핑
        _post_list_status = {
            "start":     "running",
            "ok":        "ok",
            "fail":      "fail",
            "duplicate": "skip",
        }

        def progress_cb(title, status, cur, total):
            labels = {
                "start":     (f"처리 중: {title[:28]}...", "info"),
                "ok":        (f"✅ 완료: {title[:28]}", "ok"),
                "fail":      (f"❌ 실패: {title[:28]}", "err"),
                "duplicate": (f"⏭ 중복 Skip: {title[:28]}", "dup"),
            }
            msg, tag = labels.get(status, (title, ""))
            self.bus.log(f"  {msg}", tag)
            self.bus.progress(cur, total, msg[:24])
            self.bus.post_event(title, status)
            list_status = _post_list_status.get(status)
            if list_status:
                self.ui_after(pt.update_post_status, title, list_status)

        def auth_cb(state):
            """보안 인증 상태 콜백."""
            if state == "waiting":
                self.bus.auth("waiting")
                self.bus.log(
                    "⚠️ 보안 인증 필요 — 브라우저에서 인증 후 '인증완료' 버튼을 클릭하세요", "warn")
                self.bus.status("⚠️ 보안 인증 대기 중", C["orange"])
            elif state == "done":
                self.bus.auth("done")
                self.bus.status("● 실행 중", C["green"])

        end_kind, end_summary = "ok", ""
        try:
            from scheduler.auto_scheduler import run_pipeline
            result = run_pipeline(
                dry_run=dry_run,
                progress_cb=progress_cb,
                stop_flag=self._stop,
                auth_event=self.auth_event,
                auth_cb=auth_cb,
                allowed_categories=allowed_categories,
                repair_legacy_images=bool(repair_legacy_images and not dry_run),
            )
            # 홈 탭 요약 카드가 읽는다 — 실행 종류와 함께 세션에 보관
            self.last_run_result = {**result, "dry_run": dry_run}
            ok = result.get("success", 0)
            tot = result.get("total", 0)
            drafts_used = result.get("drafts_used", 0)
            saved = result.get("saved_drafts", 0)
            quality_skipped = result.get("skipped_quality", 0)
            has_outcome_field = "publication_outcomes" in result and not dry_run
            raw_outcomes = result.get("publication_outcomes")
            outcome_counts = None
            total_count = None
            if has_outcome_field and isinstance(raw_outcomes, dict):
                outcome_keys = ("registered", "failed", "unconfirmed",
                                "duplicate", "other_skipped")
                if all(key in raw_outcomes for key in outcome_keys):
                    raw_total = result.get("total")
                    if type(raw_total) is int and raw_total >= 0:
                        total_count = raw_total
                    outcome_counts = {
                        key: raw_outcomes[key]
                        for key in outcome_keys
                    }
                    if any(type(value) is not int or value < 0
                           for value in outcome_counts.values()):
                        outcome_counts = None
                    elif total_count is None or sum(outcome_counts.values()) > total_count:
                        outcome_counts = None
            outcome_data_invalid = has_outcome_field and outcome_counts is None

            if result.get("error"):
                reason = ("다른 실행이 진행 중이어서 시작하지 못했습니다."
                          if result["error"] == "already_running"
                          else f"실행하지 못했습니다: {result['error']}")
                self.bus.log(f"⚠️ {reason}", "warn")
                self.bus.status(reason, C["yellow"])
                end_kind, end_summary = "warn", reason
            elif outcome_data_invalid:
                reason = "발행 결과 집계를 읽지 못했어요. 발행 내역을 확인하세요."
                self.bus.log(f"⚠️ {reason}", "warn")
                self.bus.status(reason, C["yellow"])
                end_kind, end_summary = "warn", reason
            elif outcome_counts is not None:
                registered = outcome_counts["registered"]
                failed = outcome_counts["failed"]
                unconfirmed = outcome_counts["unconfirmed"]
                duplicate = outcome_counts["duplicate"]
                other_skipped = outcome_counts["other_skipped"]
                unclassified = max(
                    total_count - sum(outcome_counts.values()), 0)
                outcome_summary = (
                    f"예약 등록 {registered}/{total_count} · 실패 {failed} · "
                    f"확인 필요 {unconfirmed} · 중복 스킵 {duplicate} · "
                    f"기타 스킵/미시도 {other_skipped}"
                )
                if unclassified:
                    outcome_summary += f" · 집계되지 않은 항목 {unclassified}"
                self.bus.log(f"\n발행 결과: {outcome_summary}", "info")
                if drafts_used > 0:
                    self.bus.log(f"  📤 보관본 활용: {drafts_used}건 (글 생성 스킵)", "info")
                if saved > 0:
                    self.bus.log(f"  💾 보관함 적재: {saved}건(발행 없이 보관만)", "info")
                if quality_skipped > 0:
                    self.bus.log(f"  ⚠️ 품질 게이트 스킵: {quality_skipped}건", "warn")

                if failed:
                    reason = f"발행 실패 {failed}편 · {outcome_summary}"
                    if unconfirmed:
                        reason += " · 실제 등록 여부 확인 전 재발행하지 마세요"
                    self.bus.log(f"❌ {reason}", "err")
                    self.bus.status(f"❌ {reason}", C["red"])
                    end_kind, end_summary = "error", reason
                elif unconfirmed:
                    reason = (f"네이버 확인 필요 {unconfirmed}편 · {outcome_summary} · "
                              "실제 등록 여부 확인 전 재발행하지 마세요")
                    self.bus.log(f"⚠️ {reason}", "warn")
                    self.bus.status(f"⚠️ {reason}", C["yellow"])
                    end_kind, end_summary = "warn", reason
                elif self._stop.is_set():
                    reason = f"중지됨 · {outcome_summary}"
                    self.bus.log(f"⏹ {reason}", "warn")
                    self.bus.status(f"⏹ {reason}", C["yellow"])
                    end_kind, end_summary = "warn", reason
                elif duplicate or other_skipped or unclassified:
                    reason = f"일부 항목 건너뜀 · {outcome_summary}"
                    self.bus.log(f"⚠️ {reason}", "warn")
                    self.bus.status(f"⚠️ {reason}", C["yellow"])
                    end_kind, end_summary = "warn", reason
                else:
                    reason = outcome_summary
                    self.bus.log(f"✅ {reason}", "ok")
                    self.bus.status(f"✅ {reason}", C["green"])
                    end_kind, end_summary = "ok", reason
            elif self._stop.is_set():
                self.bus.log(f"\n⏹ 중지됨: {ok}/{tot}개 처리 후 중단", "warn")
                self.bus.status(f"⏹ 중지됨 ({ok}/{tot})", C["yellow"])
                end_kind, end_summary = "warn", f"중지됨 ({ok}/{tot})"
            else:
                self.bus.log(f"\n완료: {ok}/{tot}개 {'예약발행 등록' if not dry_run else '테스트'}", "ok")
                if drafts_used > 0:
                    self.bus.log(f"  📤 보관본 활용: {drafts_used}건 (글 생성 스킵)", "info")
                if saved > 0:
                    self.bus.log(f"  💾 보관함 적재: {saved}건(발행 없이 보관만)", "info")
                if quality_skipped > 0:
                    self.bus.log(f"  ⚠️ 품질 게이트 스킵: {quality_skipped}건", "warn")
                if quality_skipped > 0:
                    self.bus.status(
                        f"⚠️ 완료 ({ok}/{tot}), 품질 스킵 {quality_skipped}건",
                        C["yellow"])
                    end_kind = "warn"
                    end_summary = f"{ok}/{tot}편 완료 · 품질 스킵 {quality_skipped}건"
                else:
                    self.bus.status(f"✅ 완료 ({ok}/{tot})", C["green"])
                    end_kind, end_summary = "ok", f"{ok}/{tot}편 완료"
        except Exception as e:
            log.error("파이프라인 실행 오류", exc_info=True)
            self.bus.log(f"❌ 오류: {e}", "err")
            self.bus.status(f"❌ {str(e)[:30]}", C["red"])
            end_kind, end_summary = "error", f"오류로 중단됨 · {str(e)[:40]}"
        finally:
            self._running = False
            self.ui_after(pt.hide_auth_waiting)  # 인증 UI 정리
            self.auth_event.clear()  # 다음 실행을 위해 초기화
            self.bus.end(end_summary, end_kind)
            self.ui_after(self.dashboard.refresh, delay=200)
            self.ui_after(self.pages["logs"].refresh, delay=200)
            self.ui_after(self.pages["home"].refresh, delay=200)

    # ── 결합 해소 — App 공용 서비스(설계서 §1.5) ────────────
    def start_login_refresh(self, on_done=None):
        """네이버 세션 쿠키 갱신 — growth 탭의 옛 ``_login_worker`` 를 App 으로
        이관(홈 탭 "로그인 다시" 와 성장 탭 로그인 버튼이 공유하는 단일 진입점).

        Args:
            on_done: 완료 후 메인 스레드에서 호출할 콜백(성공/실패 무관, 인자
                없음). growth 탭은 여기에 ``self.refresh`` 를 넘긴다.
        """
        if not self.request_run_slot("네이버 로그인", warn=True):
            return

        def _work():
            self.bus.begin("네이버 로그인")
            self.bus.status("🔐 네이버 로그인 중...", C["accent"])
            self.bus.log("🔐 네이버 로그인(쿠키 갱신) 시작", "info")

            def auth_cb(state):
                if state == "waiting":
                    self.bus.auth("waiting")
                    self.bus.log(
                        "⚠️ 보안 인증 필요 — 브라우저에서 인증 후 "
                        "'인증완료' 버튼을 클릭하세요", "warn")
                    self.bus.status("⚠️ 보안 인증 대기 중", C["orange"])
                elif state == "done":
                    self.bus.auth("done")
                    self.bus.status("🔐 네이버 로그인 중...", C["accent"])

            end_kind, end_summary = "ok", ""
            try:
                from core.naver_uploader import refresh_session_cookies
                res = refresh_session_cookies(auth_event=self.auth_event, auth_cb=auth_cb)
                if res.get("ok"):
                    self.bus.log(
                        f"✅ 로그인 성공 — 세션 쿠키 {res.get('count', 0)}개 갱신", "ok")
                    try:
                        from core.stats_fetcher import collect_stats
                        v = collect_stats() or {}
                        self.bus.log(
                            f"  방문자 조회: 오늘 {v.get('today', 0)} / 누적 {v.get('total', 0)}",
                            "info")
                    except Exception:  # noqa: BLE001 — 방문자 조회 실패는 로그인 결과에 영향 없음
                        log.debug("로그인 후 방문자 조회 실패", exc_info=True)
                    self.bus.status("✅ 로그인 완료", C["green"])
                    end_kind, end_summary = "ok", "네이버 로그인 완료"
                else:
                    self.bus.log(f"❌ 로그인 실패: {res.get('error', '')}", "err")
                    self.bus.status("❌ 로그인 실패", C["red"])
                    end_kind, end_summary = "error", "네이버 로그인 실패"
            except Exception as e:  # noqa: BLE001 — 워커 실패가 앱을 죽이면 안 됨
                log.error("네이버 로그인(쿠키 갱신) 오류", exc_info=True)
                self.bus.log(f"❌ 오류: {e}", "err")
                self.bus.status(f"❌ {str(e)[:30]}", C["red"])
                end_kind, end_summary = "error", f"로그인 오류 · {str(e)[:40]}"
            finally:
                self._running = False
                self.auth_event.clear()
                self.bus.end(end_summary, end_kind)
                if callable(on_done):
                    self.ui_after(on_done)

        self.spawn(_work, name="login-refresh")

    def _pick_slug_dialog(self, candidates: list) -> str:
        """간단한 Toplevel + Listbox 글 선택 다이얼로그.

        Returns:
            선택된 슬러그 문자열, 취소 시 빈 문자열.
        """
        dialog = tk.Toplevel(self.root)
        dialog.title("멀티채널 팩 — 글 선택")
        dialog.geometry(f"{_px(680)}x{_px(420)}")
        dialog.configure(bg=C["card"])
        dialog.transient(self.root)
        dialog.grab_set()

        L(dialog, "영상으로 만들 글을 선택하세요 (최근 20편)",
          FB, C["text"]).pack(anchor="w", padx=SP["lg"], pady=(_px(14), _px(6)))

        list_frame = tk.Frame(dialog, bg=C["card"])
        list_frame.pack(fill="both", expand=True, padx=SP["lg"], pady=SP["xs"])
        scrollbar = tk.Scrollbar(list_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        listbox = tk.Listbox(
            list_frame, font=FB, height=14, yscrollcommand=scrollbar.set,
            bg=C["card"], fg=C["text"], selectbackground=C["accent_soft"],
            selectforeground=C["text"], activestyle="none")
        for s in candidates:
            listbox.insert("end", s)
        listbox.pack(side="left", fill="both", expand=True)
        listbox.selection_set(0)
        scrollbar.config(command=listbox.yview)

        result = {"slug": ""}

        def _on_ok():
            sel = listbox.curselection()
            if sel:
                result["slug"] = candidates[sel[0]]
            dialog.destroy()

        def _on_cancel():
            dialog.destroy()

        btn_frame = tk.Frame(dialog, bg=C["card"])
        btn_frame.pack(fill="x", padx=SP["lg"], pady=_px(14))
        Btn(btn_frame, "선택", _on_ok, 14).pack(side="right", padx=(SP["sm"], 0))
        GhostBtn(btn_frame, "취소", _on_cancel, 14).pack(side="right")
        listbox.bind("<Double-Button-1>", lambda _e: _on_ok())
        dialog.bind("<Return>", lambda _e: _on_ok())
        dialog.bind("<Escape>", lambda _e: _on_cancel())
        dialog.wait_window()
        return result["slug"]

    def run_multichannel_build(self) -> None:
        """멀티채널 팩(쇼츠 mp4+캡션) 생성 진입점(설계서 §1.5).

        순수 로직(후보 수집·헬퍼 spawn·완료 감지)은 Tk 의존 0 인
        :mod:`gui.multichannel_service` 로 이관했다(auto-dev 5호 §1) — 이
        메서드는 화면(슬러그 선택 다이얼로그·확인 팝업·활동 바 갱신)만 맡고
        서비스 콜백을 ``self.ui_after`` 로 메인 스레드에 재위임한다.
        settings.py 는 코더 D 가 같은 서비스로 위임하도록 슬림화한다(이
        세션은 파일 경계상 그쪽을 건드리지 않는다).
        """
        from gui.multichannel_service import (harness_root,
                                               helper_script_path,
                                               list_candidate_slugs,
                                               start_build)

        root = harness_root()
        if not helper_script_path(root).exists():
            messagebox.showerror(
                "멀티채널 팩 생성", "영상 생성 도구를 찾지 못했어요. 프로그램을 다시 설치해 보세요.")
            return

        candidates = list_candidate_slugs(root)
        if not candidates:
            toast = getattr(self, "toast", None)
            msg = "영상으로 만들 수 있는 글이 아직 없어요."
            if toast is not None:
                toast.show(msg, kind="info")
            else:
                messagebox.showinfo("멀티채널 팩 생성", msg)
            return

        chosen = self._pick_slug_dialog(candidates)
        if not chosen:
            return

        out_dir = root / "runs" / chosen / "multichannel"
        if out_dir.exists() and any(out_dir.iterdir()):
            if not messagebox.askyesno(
                    "멀티채널 팩 생성",
                    "이미 만들어진 영상이 있어요.\n다시 만들어 덮어쓸까요?"):
                return

        self.bus.begin(f"영상 생성 · {chosen}")
        self.bus.status(f"🎬 멀티채널 팩 생성 중… ({chosen})", C["accent"])
        toast = getattr(self, "toast", None)
        if toast is not None:
            toast.show("별도 창에서 영상을 만들고 있어요. 시간이 걸릴 수 있어요.", kind="info")

        def _on_line(msg: str) -> None:
            self.ui_after(self.bus.status, f"🎬 {msg}", C["accent"])

        def _on_done(produced: bool, out_path) -> None:
            # start_build 는 워커 스레드에서 이 콜백을 부른다 — 메인 스레드로
            # 재위임(Tk 의존 0 인 서비스와 App 사이의 경계).
            def _finish() -> None:
                if produced:
                    self.bus.status(f"🎬 영상 생성 완료 ({chosen})", C["green"])
                    self.bus.end(f"영상 생성 완료 · {chosen}", kind="ok")
                else:
                    self.bus.status(f"🎬 영상 생성 창이 닫혔어요 ({chosen})", C["yellow"])
                    self.bus.end(f"영상 생성 미완료 · {chosen}", kind="warn")
            self.ui_after(_finish)

        # should_stop=self.is_destroyed(AppLifecycle) — 창이 이미 닫혔으면
        # 서비스가 알아서 폴링을 멈추고 on_done 을 부르지 않는다(리뷰 🟡1).
        proc = start_build(chosen, root, on_line=_on_line, on_done=_on_done,
                           should_stop=self.is_destroyed)
        if proc is None:
            messagebox.showerror("멀티채널 팩 생성", "영상 생성 도구를 시작하지 못했어요.")
            self.bus.end("영상 생성 시작 실패", kind="error")

    # ── 환경 체크 ─────────────────────────────────────
    def _env_check(self):
        # 최초/미완료 온보딩이면 '시작하기' 체크리스트 마법사로 안내한다(검은 CMD·
        # 설정 미아 제거 — 관문 ②④). 마법사가 뜨는 경우(핵심 미충족)엔 아래 단순
        # 경고도, 코치마크 투어도 띄우지 않는다(팝업 중복 방지 — 마법사가 먼저).
        if self._maybe_show_onboarding():
            return
        import shutil
        warns = []
        if not shutil.which("claude"):
            warns.append("Claude 연결이 필요해요 · 설정 탭에서 연결해 주세요")
        if not self.cfg.get("naver_id"):
            warns.append("네이버 계정이 아직 없어요 · 설정 탭에서 입력해 주세요")
        if warns:
            text = " · ".join(warns)
            # AppLifecycle.ui_after — delay>0 예약을 _pending_after 에 남겨
            # 창이 닫히기 전에 발화하지 않으면 destroy() 가 취소한다(2026-09-02
            # 검증조 재현 🟠4, 여기 raw self.root.after 도 같은 누수였다).
            self.ui_after(self._show_setup_needed, text, delay=600)
        self._maybe_start_tour()

    def _maybe_start_tour(self) -> None:
        """첫 실행 코치마크 투어(9호 Phase 3) 자동 시작을 예약한다.

        세 겹으로 보호한다 — ①생성자 ``start_tour=False`` ②``NBA_NO_TOUR=1``
        환경변수(테스트·헤드리스가 직접 끄고 싶을 때. conftest 는 이 값을
        건드리지 않는다 — 기본 경로는 아래 ③지연 자체가 보호막이다) ③
        ``gui.ui_prefs`` 의 ``tour_done``. 1.5초 뒤에나 실제로 뜨므로(그마저
        `_start_tour_now` 에서 "지금 홈 탭인가"까지 한 번 더 확인한다) 짧게
        생성했다 파괴하는 테스트는 이 타이머가 발화하기 전에 끝난다."""
        if not self._start_tour_enabled:
            return
        import os
        if os.environ.get("NBA_NO_TOUR") == "1":
            return
        try:
            from gui import ui_prefs
            if ui_prefs.get("tour_done", False):
                return
        except Exception:  # noqa: BLE001 — 취향 저장소 실패는 그냥 투어를 건너뛴다
            return
        self.ui_after(self._start_tour_now, delay=1500)

    def _start_tour_now(self) -> None:
        if self.is_destroyed():
            return
        # 자동 시작 한정 안전장치 — 타이머가 발화한 시점에 사용자가 이미
        # 다른 탭에 가 있으면(테스트가 탭을 순회 중이었거나, 실사용자가
        # 벌써 다른 작업을 시작했거나) 강제로 홈으로 되돌리며 끼어들지
        # 않는다. "처음 안내 다시 보기" 버튼(수동 경로)은 이 확인 없이
        # `start_tour()` 를 바로 부른다 — 사용자가 명시적으로 눌렀으므로.
        current_key = next(
            (k for k, p in getattr(self, "pages", {}).items()
             if getattr(p, "winfo_manager", lambda: "")()), None)
        if current_key not in (None, "home"):
            return
        self.start_tour()

    def start_tour(self) -> None:
        """투어를 지금 바로 시작한다 — 자동 시작 타이머와 설정 탭의
        "처음 안내 다시 보기" 버튼이 둘 다 여기로 모인다."""
        try:
            from gui.tour import Tour
        except Exception:  # noqa: BLE001 — 투어 모듈이 없어도 앱은 계속 돈다
            log.error("투어 모듈을 불러오지 못했어요", exc_info=True)
            return
        existing = getattr(self, "_tour", None)
        if existing is not None:
            try:
                existing.cancel()
            except Exception:  # noqa: BLE001
                pass
        try:
            self._tour = Tour(self)
            self._tour.start()
        except Exception:  # noqa: BLE001 — 투어 시작 실패가 앱을 막으면 안 된다
            log.error("투어 시작 실패", exc_info=True)

    def _show_setup_needed(self, text: str) -> None:
        """초기 설정 필요 안내 — 홈 탭 배너 우선, 없으면 토스트(8초)."""
        home = self.pages.get("home") if hasattr(self, "pages") else None
        banner_fn = getattr(home, "show_setup_banner", None)
        if callable(banner_fn):
            banner_fn(text)
            return
        toast = getattr(self, "toast", None)
        if toast is not None:
            try:
                toast.show(text, kind="warn", ms=8000)
                return
            except Exception:  # noqa: BLE001
                pass
        messagebox.showwarning("초기 설정 필요", text)

    def _maybe_show_onboarding(self) -> bool:
        """핵심 선행조건(Claude 연결·네이버 계정)이 비어 있으면 시작하기 마법사를 예약한다.

        온보딩 진단(core.onboarding_status.check_onboarding)의 missing 에 핵심 항목이
        있을 때만 뜬다 → 이미 운영 중인 사용자(로그인+계정 완비)에겐 뜨지 않는다.

        Returns:
            마법사를 예약했으면 True(호출부는 중복 경고 팝업을 스킵).
        """
        try:
            from core.onboarding_status import check_onboarding
            status = check_onboarding()
        except Exception:  # noqa: BLE001 — 진단 실패 시 기존 경고 경로로 폴백
            return False
        missing = status.get("missing", []) if status else []
        core_missing = [k for k in missing if k in ("claude", "account")]
        if not core_missing:
            return False

        def _show():
            try:
                from gui.onboarding_wizard import OnboardingWizard
                OnboardingWizard(self.root, self)
            except Exception:  # noqa: BLE001 — 마법사 실패가 앱 기동을 막지 않게
                log.error("온보딩 마법사 표시 실패", exc_info=True)

        # AppLifecycle.ui_after — 취소 가능한 지연 예약(위 _env_check 주석 참조).
        self.ui_after(_show, delay=700)
        return True

    def _maybe_autostart_comfy(self):
        """GUI 시작 시 로컬 ComfyUI(Krea 2)가 안 떠 있으면 백그라운드로 자동 기동.
        비차단·실패무해(하네스가 클라우드 폴백). 상태바에만 짧게 반영한다."""
        def _work():
            try:
                try:
                    from core.comfy_launcher import ensure_running
                except ImportError:
                    from naver_blog_v3.core.comfy_launcher import ensure_running
                status = ensure_running()
            except Exception:  # noqa: BLE001 — 자동 기동 실패는 무해
                return
            msg = {
                "started":  ("🟢 로컬 ComfyUI 자동 기동 중…", C.get("accent")),
                "already":  ("🟢 로컬 ComfyUI 사용 가능", C.get("accent")),
                "error":    ("⚠ ComfyUI 자동 기동 실패(클라우드로 생성)", C.get("muted")),
            }.get(status)
            if msg:
                self.ui_after(self.set_status, msg[0], msg[1])
        # fake-app monkeypatch 대상이 아니므로 self.spawn 을 안전하게 쓴다.
        self.spawn(_work, name="comfy-autostart")

    def _maybe_autostart_shorts(self):
        """GUI 시작 시 쇼츠 서버(shorts_studio :8788)가 안 떠 있으면 자동 기동.
        비차단·실패무해(렌더 단계가 기존 로컬 렌더러로 폴백). 사용자는 아무것도
        안 해도 '발행하면 쇼츠가 나온다'가 되도록 하는 단순화 장치."""
        def _work():
            try:
                try:
                    from core.shorts_studio_launcher import ensure_running
                except ImportError:
                    from naver_blog_v3.core.shorts_studio_launcher import ensure_running
                ensure_running()
            except Exception:  # noqa: BLE001 — 자동 기동 실패는 무해
                pass
        self.spawn(_work, name="shorts-autostart")

    # ── 발행 큐 정렬 캐시 재워밍(9호 Phase 3) ───────────────
    # 실측(auto_scheduler.py:768 `get_pending_posts` → `_sort_by_trend_priority`,
    # 2809 `preflight_check`): 홈 탭이 기동 직후 여는 "발행 준비" preflight가
    # 이미 `core.queue_sort_cache` 를 채운다 — 최초 워밍은 손댈 게 없다. 이
    # 타이머는 앱을 오래 켜 둔 채로 그 캐시의 TTL(6시간)이 조용히 지나가는
    # 경우만 보충한다: 정기적으로 캐시 파일 나이를 확인해 만료가 가까우면
    # 백그라운드 스레드에서 `preflight_check()` 를 다시 불러 미리 재계산해
    # 둔다(그 함수 안에서 캐시 미스를 만나면 자동으로 재저장한다 — 여기서
    # core.queue_sort_cache 를 직접 쓰지 않는다, 읽기만 한다).
    _QUEUE_SORT_REWARM_CHECK_MS = 10 * 60 * 1000   # 10분마다 점검
    _QUEUE_SORT_REWARM_BUFFER_S = 10 * 60          # 만료 10분 전(또는 이미 만료)이면 재워밍

    @staticmethod
    def _queue_sort_cache_enabled() -> bool:
        try:
            import config.settings as _settings
            return bool(getattr(_settings, "QUEUE_SORT_CACHE_ENABLED", True))
        except Exception:  # noqa: BLE001 — 설정 로드 실패는 활성으로 취급(캐시 모듈과 동일 관례)
            return True

    def _schedule_queue_sort_rewarm(self) -> None:
        """설정이 꺼져 있으면(``QUEUE_SORT_CACHE_ENABLED=False``) 타이머 자체를
        등록하지 않는다 — 캐시를 안 쓰는데 재워밍할 이유가 없다."""
        if not self._queue_sort_cache_enabled():
            return
        self._queue_sort_rewarm_registered = True
        self.ui_after(self._check_queue_sort_rewarm,
                      delay=self._QUEUE_SORT_REWARM_CHECK_MS)

    def _check_queue_sort_rewarm(self) -> None:
        if self.is_destroyed():
            return
        # 다음 점검을 먼저 다시 예약한다 — 아래에서 예외가 나도(설정이 실행
        # 중간에 이상해지는 등) 재워밍 루프 자체는 계속 돈다.
        self.ui_after(self._check_queue_sort_rewarm,
                      delay=self._QUEUE_SORT_REWARM_CHECK_MS)
        if not self._queue_sort_cache_enabled():
            return
        self.spawn(self._rewarm_queue_sort_cache_worker, name="queue-sort-rewarm")

    def _rewarm_queue_sort_cache_worker(self) -> None:
        """백그라운드 스레드 — Tk 호출 0(스레드 위생 규약)."""
        try:
            age = self._queue_sort_cache_age_seconds()
            if age is None:
                return  # 캐시 파일이 아직 없음 — 큰 큐(50건+)를 만난 적이 없거나 비활성.
            try:
                from core import queue_sort_cache as _qsc
                ttl = getattr(_qsc, "TTL_SECONDS", 6 * 60 * 60)
            except Exception:  # noqa: BLE001
                ttl = 6 * 60 * 60
            if age < ttl - self._QUEUE_SORT_REWARM_BUFFER_S:
                return  # 아직 여유 있음
            from scheduler.auto_scheduler import preflight_check
            preflight_check()
        except Exception:  # noqa: BLE001 — 재워밍 실패는 무해(다음 실사용 때 재계산됨)
            log.debug("[queue-sort-rewarm] 재워밍 실패(무해)", exc_info=True)

    @staticmethod
    def _queue_sort_cache_age_seconds() -> Optional[float]:
        """캐시 파일의 나이(초). ``core.queue_sort_cache`` 에 "지금 몇 초 전에
        계산됐나" 를 직접 묻는 공개 API 가 없다(그 모듈의 조회 API 는 설계상
        현재 큐(``pending``) 리스트를 넘겨야만 히트 여부를 답할 수 있다) —
        임무 지침대로 캐시 파일 mtime 으로 근사한다."""
        try:
            from config import profile as _profile
            from core import queue_sort_cache as _qsc
            filename = getattr(_qsc, "_CACHE_FILENAME", "queue_sort_cache.json")
            cache_path = _profile.logs_dir() / filename
        except Exception:  # noqa: BLE001
            return None
        try:
            if not cache_path.exists():
                return None
            # max(0.0, ...) — 파일시스템 타임스탬프 해상도 차이로 mtime 이
            # time.time() 보다 아주 살짝(마이크로초 단위) 앞서는 경우가
            # 실측 확인됐다(막 쓴 파일). 음수 나이는 의미가 없다.
            return max(0.0, time.time() - cache_path.stat().st_mtime)
        except OSError:
            return None

    def _on_close(self) -> None:
        """창 닫기 — AppLifecycle 플래그를 먼저 세워 이후 워커의 ui_after 를
        차단하고, 각 탭 페이지도 destroy 해 예약된 지연 after 콜백(설정 탭
        상태 새로고침 400/500ms·포스팅 탭 sash 조정·홈 탭 세션 프로브 700ms
        등)이 창 파괴 후 발화해 stderr 에 "invalid command name" 을 남기지
        않게 한다(2026-09-02 검증조 재현 🟠4 — App 자체는 AppLifecycle 이라
        지연 예약을 자기 destroy() 로 취소하지만, 각 탭 페이지의 예약은 그
        페이지 자신의 destroy() 가 불려야 취소된다).
        """
        tour = getattr(self, "_tour", None)
        if tour is not None:
            try:
                tour.cancel()  # tour_done 은 안 건드리고 화면(테두리·카드)만 정리
            except Exception:  # noqa: BLE001
                pass
        self.destroy()
        for p in getattr(self, "pages", {}).values():
            try:
                p.destroy()
            except tk.TclError:
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self):
        self.root.mainloop()
