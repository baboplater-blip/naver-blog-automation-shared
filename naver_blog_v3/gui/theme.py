# gui/theme.py — 테마 상수 + 위젯 헬퍼
"""색·폰트·간격 토큰과 공용 위젯 팩토리. 기존 이름(C 12키·FT/FB/FS/FM/FH·
Btn/GhostBtn/Ent/...)은 절대 바꾸지 않는다 — 앱 전역이 이 이름을 그대로
참조한다(2026-09-02 GUI 개편 설계서 §0.2). 이번 개편에서는 값 보정과
새 토큰·헬퍼 추가만 한다.

customtkinter 렌더링 계층(auto-dev 8호, 2026-09-03): `HAS_CTK` 가 True 면
버튼·Card 팩토리가 CTk 위젯을 만들어 반환한다. tk 옵션 dict 계약
(`btn["bg"]`, `btn.cget("state")`, `.config(...)`)을 쓰는 기존 탭 코드가
그대로 동작해야 하므로, CTk 위젯은 얇은 어댑터(`_CtkButtonAdapter`/
`_CtkFrameAdapter`)로 감싼다 — customtkinter 6.0 은 `Misc.__getitem__` 이
클래스 정의 시점에 `cget` 원본 메서드를 그대로 가리키는 tkinter 특성 때문에
CTk 가 오버라이드한 `cget` 을 우회해 `btn["state"]` 가 예외를 던진다(실측
확인) — 그래서 어댑터가 `__getitem__`/`__setitem__` 을 직접 정의한다.
"""
import json
import logging
import os
import re
import sys
import tkinter as tk
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gui import icons

log = logging.getLogger(__name__)


# ── 고DPI 대응 기반(9호 Phase 1, 2026-09-03) ─────────────────────────
# Windows 프로세스 DPI 인식은 **첫 창(HWND)이 뜨기 전에만** 의미가 있다
# (그 뒤엔 무시되거나 실패) — `gui/theme.py` 는 `app.py` 가 `tk.Tk()` 를
# 만들기 전에 임포트되는 게 보통이라(다른 탭 모듈이 먼저 이 모듈을
# 임포트), 모듈 최상단 이 시점에 호출한다. customtkinter 설치 여부와
# 무관하게(순정 tk 폰트도 DPI 인식 시 OS 가 실제 배율을 알려줘 흐려지지
# 않는다) 항상 시도한다.
def _enable_process_dpi_awareness() -> bool:
    """이 프로세스를 DPI 인식 상태로 만든다 — 성공하면 OS 가 창을 낮은
    해상도로 그려 비트맵으로 확대(흐림)하는 대신, 우리가 실제 픽셀
    단위로 그린 걸 그대로 보여준다("크립"함 여부만 담당 — 실제 배율
    계산은 아래 `UI_SCALE`/`px()` 가 별개로 맡는다).

    순서: `SetProcessDpiAwarenessContext(PER_MONITOR_AWARE_V2)`(Windows 10
    1703+, 모니터를 옮기면 실시간 대응) → 실패하면
    `SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE=2)`(Windows 8.1+
    — customtkinter 자신도 내부적으로 같은 값을 쓴다, `ScalingTracker.
    activate_high_dpi_awareness()` 실측 확인) → 그것도 안 되면(비Windows·
    구형 Windows) 조용히 포기. 실패해도 앱은 그냥 이전처럼(OS 비트맵
    확대) 계속 동작한다 — 크래시 없음."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes
    except ImportError:
        return False
    try:
        # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 은 정수가 아니라
        # 특수 포인터 상수(-4) — ctypes 로는 c_void_p 로 넘긴다.
        context = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(context):
            return True
    except (AttributeError, OSError) as exc:
        log.debug("[theme] SetProcessDpiAwarenessContext 실패: %s", exc, exc_info=True)
    try:
        PROCESS_PER_MONITOR_DPI_AWARE = 2
        # SetProcessDpiAwareness 는 HRESULT 를 돌려준다 — S_OK(0)이 성공.
        return ctypes.windll.shcore.SetProcessDpiAwareness(
            PROCESS_PER_MONITOR_DPI_AWARE) == 0
    except (AttributeError, OSError) as exc:
        log.debug("[theme] SetProcessDpiAwareness 실패: %s", exc, exc_info=True)
    return False


_DPI_AWARE = _enable_process_dpi_awareness()


def _detect_system_dpi_scale() -> float:
    """`GetDpiForSystem()/96` — DPI 인식을 켠 *뒤에* 불러야 진짜 시스템
    DPI 를 준다(인식 안 된 프로세스는 항상 96 만 본다, 이른바 "DPI
    가상화"). 실패하면(비Windows·ctypes 오류 등) 1.0."""
    if sys.platform != "win32":
        return 1.0
    try:
        import ctypes
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi and dpi > 0:
            return dpi / 96.0
    except (AttributeError, OSError) as exc:
        log.debug("[theme] GetDpiForSystem 실패: %s", exc, exc_info=True)
    return 1.0


def _detect_ui_scale() -> float:
    """`UI_SCALE` 결정 우선순위: 환경변수 `NBA_UI_SCALE`(테스트·캡처용
    강제값, 예 "1.5") > 시스템 DPI(위 `_detect_system_dpi_scale()`) > 1.0.
    """
    env_value = os.environ.get("NBA_UI_SCALE")
    if env_value:
        try:
            parsed = float(env_value)
            if parsed > 0:
                return parsed
        except ValueError:
            log.debug("[theme] NBA_UI_SCALE 파싱 실패(무시): %r", env_value)
    return _detect_system_dpi_scale()


UI_SCALE: float = _detect_ui_scale()   # 임포트 시점 스냅샷(FONT_FAMILY 와 같은 패턴)


def px(n: float) -> int:
    """`UI_SCALE` 배율을 적용한 정수 픽셀값(반올림). 위젯 키트 전역의
    하드코딩 픽셀(높이·아이콘 크기·Canvas 좌표·wraplength 등)은 전부
    이 함수를 거친다 — 원값을 그대로 쓰면 고DPI 모니터에서 다른 요소는
    커지는데 그것만 원래 크기로 남아 어긋난다."""
    return round(n * UI_SCALE)


def set_ui_scale(factor: float) -> None:
    """`UI_SCALE` 을 바꾸고 `SP`/`R`(+ `gui.icons` 의 자체 스케일)을 그
    자리에서 재계산한다 — `apply_palette()` 의 `C.update()` 와 같은 관례로
    dict 객체를 재할당하지 않는다(다른 모듈이 `from gui.theme import SP`
    로 같은 객체를 참조 중이라 재할당하면 그쪽은 옛 dict 를 계속 본다).

    기동 시 1회(또는 테스트에서 스케일을 강제할 때) 쓰는 함수다 — 리스너
    등록·구독 같은 건 없다(설계 지침 그대로: "SP/R 재계산 + 리스너 없음").
    이미 만들어진 위젯의 크기는 되돌리지 않는다(다음 화면 재구성 때
    새 SP/R 값으로 자연히 반영됨 — retheme() 과 같은 전제)."""
    global UI_SCALE
    UI_SCALE = float(factor) if factor and factor > 0 else 1.0
    SP.update({k: px(v) for k, v in SP_BASE.items()})
    R.update({k: px(v) for k, v in R_BASE.items()})
    try:
        icons.set_ui_scale(UI_SCALE)
    except Exception as exc:  # noqa: BLE001 — icons 쪽 실패가 theme 쪽까지 막지 않는다
        log.debug("[theme] icons.set_ui_scale 동기화 실패: %s", exc, exc_info=True)


# ── customtkinter 통합 ────────────────────────────────
try:
    import customtkinter as ctk
    HAS_CTK = True
except Exception as _ctk_import_exc:  # noqa: BLE001 — 미설치 등 어떤 이유로든 tk 로 폴백
    ctk = None
    HAS_CTK = False
    log.debug("[theme] customtkinter 임포트 실패 — tk 폴백: %s", _ctk_import_exc)

# ── 이모지 정리 (디자인 시스템 v3 §1 원칙4 — 이모지 전면 금지) ──────────
# `gui/app.py` 에서 이리로 옮겼다(캡처 심사 3차 — 스캐너는 정적 리터럴만
# 보고 `bus.status()/bus.log()` 로 넘어가는 런타임 f-string 은 보지 못하는
# 사각지대가 있어, `App.set_status` 뿐 아니라 `RunBus.log/status/end`
# fan-out 직전·`PostingTab.log()` 등 여러 렌더 경계가 같은 함수를 공유해야
# 했다. `theme.py` 는 `app.py`/`run_bus.py`/`gui/tabs/*.py` 어디서든
# 순환임포트 없이 참조 가능한 가장 낮은 층이라 여기로 공개한다.
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿\U0001F900-\U0001F9FF️]")


def strip_emoji(text: str) -> str:
    """이모지·이모지 변형 선택자(VS16) 를 제거한 문자열. 렌더 경계(활동
    바 상태 pill·포스팅 로그 박스·토스트·팝오버 등)에서 워커 스레드가
    보낸 런타임 문구를 화면에 꽂기 직전 항상 이 함수를 거친다."""
    return _EMOJI_RE.sub("", text or "").strip()


if HAS_CTK:
    # DPI 스케일링 통일 결정(9호 Phase 1, 2026-09-03 — 8호의 "일단 끈다"
    # 판단을 뒤집는다, 근거를 여기 남긴다).
    #
    # customtkinter 는 CTk 위젯마다 두 겹의 자동 배율을 갖는다:
    #   ① `window_dpi_scaling_dict[window]` — 그 위젯이 속한 창이 떠 있는
    #     모니터의 실제 DPI(`GetDpiForMonitor`) 를 실시간으로 추적해 곱함
    #     (`ScalingTracker.get_window_dpi_scaling()` 실측 확인)
    #   ② `widget_scaling`/`window_scaling` — `ctk.set_widget_scaling()` 로
    #     사용자가 얹는 추가 배율(기본 1.0)
    # 두 겹 다 CTk 위젯(height/corner_radius/padding/font)에만 적용되고,
    # 이 앱의 절반 이상을 차지하는 순정 tk 위젯(tk.Frame 패딩·Canvas 좌표
    # 등)에는 전혀 안 닿는다 — CTk 쪽만 자동으로 커지고 tk 쪽은 그대로라
    # 레이아웃이 어긋나는 게 8호가 DPI 인식을 껐던 이유였다.
    #
    # 9호는 이 자리를 반대로 푼다 — CTk 의 자동 배율(①②) 을 **전부
    # 무력화**하고, 아래 정의된 우리 자신의 `UI_SCALE`/`px()` 하나로 tk·CTk
    # 양쪽 모두를 균일하게 스케일한다(CTk 위젯 생성 시 height=px(36) 처럼
    # 이미 스케일된 값을 넘긴다 — `_ctk_font`/`Ent`/`_ctk_flat_button` 등
    # 참조). 그러면서도 OS DPI 인식 자체는 켠다(위 `_enable_process_dpi_
    # awareness()`, Tk 루트 생성 전에 이미 호출됨) — 이건 "크기 배율"이
    # 아니라 "OS 가 비트맵으로 확대해 흐리게 그리지 않게" 하는 별개의
    # 스위치라, ①②를 꺼도 화면은 여전히 선명하다(폰트 pt 튜플은 Tk 자체가
    # DPI 인식 시 알아서 확대해준다 — 아래 FB/FS 등 참조, 이것도 CTk
    # 배율과 무관).
    #
    # `deactivate_automatic_dpi_awareness()` 는 ①을 무력화(그 함수 안에서
    # `get_window_dpi_scaling()` 이 항상 1 을 돌려주게 만듦)하고,
    # `set_widget_scaling(1.0)`/`set_window_scaling(1.0)` 은 ②를 명시적
    # 중립값으로 고정한다(customtkinter 기본값도 1.0 이지만, 나중에
    # 누군가 실수로 바꾸는 걸 막기 위해 명시). 셋 다 안 하면(또는 하나만
    # 하면) `px()` 로 이미 배율을 곱한 값 위에 CTk 가 또 곱해 이중 확대
    # 된다 — 이게 이번 라운드가 "한쪽으로 통일" 해야 했던 이유.
    try:
        ctk.deactivate_automatic_dpi_awareness()
        ctk.set_widget_scaling(1.0)
        ctk.set_window_scaling(1.0)
    except Exception as _dpi_exc:  # noqa: BLE001
        log.debug("[theme] CTk 자체 DPI 배율 무력화 실패(무시): %s", _dpi_exc)

    _CTK_THEME_PATH = Path(__file__).resolve().parent / "ctk_theme.json"
    try:
        ctk.set_default_color_theme(str(_CTK_THEME_PATH))
    except Exception as _theme_exc:  # noqa: BLE001 — 실패해도 ctk 내장 기본 테마로 계속
        log.debug("[theme] ctk_theme.json 로드 실패: %s", _theme_exc, exc_info=True)

# ── 테마 팔레트 ───────────────────────────────────────
# v4(2026-09-21, 21st.dev 참고 개편) — **키 이름은 전부 불변**, 값만 조정한다.
# 방향: ①색조가 섞인 남색 계열 표면을 **중성 근검정**으로 바꿔 파랑 강조색
# 하나만 눈에 띄게 한다(예전 bg/#0b0c10·card/#16171d 은 파랑기가 돌아 accent
# 와 경쟁했다) ②표면 5층(bg<surface<surface_2<card<card_hover<surface_3)의
# 간격을 고르게 벌려 카드 경계가 테두리 없이도 읽히게 한다 ③accent 를 한
# 단계 진하게(#2f6fe0→#2563eb) 해 흰 글자 대비를 4.70→5.17 로 올린다.
# 불변식 2개(테스트가 고정): dark 는 card_hover == btn_secondary,
# light 는 card == on_accent == surface_3.
C = {
    "bg":      "#0a0a0c", "surface": "#0f0f12", "card": "#151519",
    "border":  "#26262d", "accent":  "#2563eb", "green": "#3ddc97",
    "red":     "#ff6b6b", "yellow":  "#f5c542", "purple":"#c084fc",
    # muted 는 2026-09-22 가독성 점검에서 #80818e → #90919e 로 올렸다. 예전
    # 값은 card 위에서 4.72:1(AA 하한 4.5 바로 위)이라 작은 글씨의 안내 문장이
    # 흐려 보였다 — 새 값은 card 5.84 / bg 6.34 이고 text_2(7.90)보다는 여전히
    # 옅어 위계는 그대로다.
    "orange":  "#fb923c", "text":    "#ececf1", "muted": "#90919e",
}
# 신규 토큰(추가만, 기존 키 제거·이름변경 없음). state_bg 4종은 기존 관례대로
# card 90% + 상태색 10% 블렌드를 새 card(#151519)·새 상태색 기준으로 재계산.
C.update({
    "surface_2":     "#121215",  # 입력창 기본 채움·테이블 헤더 — 2층
    "text_2":        "#a9aab6",  # 2차 본문(섹션 설명·카드 부제)
    "accent_soft":   "#16213a",  # hover/선택 전 미리보기 배경
    "success_bg":    "#192926",  # 성공/완료 알약·배너 배경(card 90% + green 10%)
    "warn_bg":       "#2b271d",  # 주의/대기 알약·배너 배경(card 90% + yellow 10%)
    "danger_bg":     "#2c1e21",  # 실패/삭제 확인 알약·배너 배경(card 90% + red 10%)
    "info_bg":       "#171d2e",  # 안내/진행중 알약·배너 배경(card 90% + accent 10%)
    "card_hover":    "#1c1c22",  # 클릭 가능한 카드·행의 hover 배경 — 4층
    "border_strong": "#35353f",  # 입력창·드롭다운 등 상호작용 요소 경계선
    "focus":         "#7aa7ff",  # 키보드 포커스 링 전용색
    "btn_secondary": "#1c1c22",  # SecondaryBtn 기본 배경 — card_hover 와 통일
})
# 주 버튼(Primary/Btn/Danger) 글자색 — accent 톤이 인디고블루로 바뀌며 흰
# 텍스트로 전환(다크·라이트 두 모드가 "흰 글자 온 블루"로 통일).
C["on_accent"] = "#ffffff"
C["accent_hover"] = "#3b82f6"  # 클릭 가능 요소 hover 채움색(호버는 WCAG 등급 대상 아님)
C["surface_3"] = "#1e1e24"     # 팝오버·툴팁·드롭다운 전용 최고 엘리베이션(5층)

# ── 라이트 팔레트 + 팔레트 전환 (GUI 개편 auto-dev 5호, 2026-09-02) ─────
# PALETTES["dark"] 는 위에서 조립한 C 의 스냅샷(26키) 그 자체 — 재입력 오타
# 리스크 없이 단일 진실원본을 보장한다. PALETTES["light"] 는 WCAG AA 기준으로
# 새로 골랐다: text/bg ≥ 7:1, text_2·muted/card ≥ 4.5:1, on_accent/accent ≥ 4.5:1
# (실측 대비비는 tests/test_gui_theme.py 참조).
PALETTES = {
    "dark": dict(C),
    "light": {
        "bg":            "#f7f8fa",
        "surface":       "#f0f1f5",
        "card":          "#ffffff",
        "border":        "#e3e5ec",
        "accent":        "#2563eb",
        "green":         "#15803d",
        "red":           "#c62828",
        "yellow":        "#8a6d1a",
        "purple":        "#6b21a8",
        "orange":        "#b6560f",
        "text":          "#14161d",
        "muted":         "#5d6473",
        "surface_2":     "#eff0f4",
        "text_2":        "#4a5060",
        "accent_soft":   "#dce8fd",
        "success_bg":    "#e8f6ee",
        "warn_bg":       "#fdf4e0",
        "danger_bg":     "#fdeaea",
        "info_bg":       "#e8effd",
        "card_hover":    "#f3f4f8",
        "border_strong": "#c3c7d4",
        "focus":         "#1d4ed8",
        "btn_secondary": "#eff1f6",
        "on_accent":     "#ffffff",
        "accent_hover":  "#3b74ee",  # gui-design-system-v3.md §2-2 — accent 보다 살짝 밝게
        "surface_3":     "#ffffff",  # card 와 동일(라이트는 밝기로 층을 못 만듦) — 반드시
                                     # border_strong 테두리와 함께 써서 구분한다(§2-2/§5-9)
    },
}

_current_palette = "dark"


def apply_palette(name: str) -> str:
    """팔레트를 in-place 로 전환한다.

    다른 모듈이 ``from gui.theme import C`` 로 같은 dict 객체를 참조하므로
    ``C`` 를 재할당하지 않고 ``C.update(...)`` 로만 값을 바꾼다. 미지 이름은
    "dark" 로 폴백한다. HAS_CTK 면 ``ctk.set_appearance_mode(...)`` 도 호출해
    (light,dark) 페어로 만들어진 CTk 위젯들을 함께 전환한다. 반환값은 실제
    적용된 팔레트 이름.
    """
    global _current_palette
    resolved = name if name in PALETTES else "dark"
    C.update(PALETTES[resolved])
    _current_palette = resolved
    if HAS_CTK:
        try:
            ctk.set_appearance_mode(resolved)
        except Exception as exc:  # noqa: BLE001 — ctk 모드 전환 실패해도 C 갱신은 유지
            log.debug("[theme] ctk.set_appearance_mode 실패: %s", exc, exc_info=True)
    return resolved


def current_palette() -> str:
    """현재 적용된 팔레트 이름("dark"|"light")."""
    return _current_palette


def _relative_luminance(hex_color: str) -> float:
    """WCAG 2.1 상대휘도(0~1)."""
    c = hex_color.lstrip("#")
    r, g, b = (int(c[i:i + 2], 16) / 255 for i in (0, 2, 4))

    def _lin(ch: float) -> float:
        return ch / 12.92 if ch <= 0.03928 else ((ch + 0.055) / 1.055) ** 2.4

    r, g, b = _lin(r), _lin(g), _lin(b)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(hex_a: str, hex_b: str) -> float:
    """WCAG 2.1 대비비(1~21). 인자 순서는 무관(대칭)."""
    la = _relative_luminance(hex_a) + 0.05
    lb = _relative_luminance(hex_b) + 0.05
    return max(la, lb) / min(la, lb)


# ── 라이브 팔레트 전환 (retheme — GUI 개편 auto-dev 6호, 2026-09-02) ────
# 옵션 이름으로 "배경류"/"전경류" 를 나눈다(그대로 "background"/"foreground"
# 를 포함하는지). 두 팔레트 모두에서 값이 우연히 겹치는 토큰(dark: bg==
# on_accent, light: card==on_accent, surface==btn_secondary)이 있어, 역매핑이
# 여러 토큰에 동시에 맞을 때 이 우선순위로 하나를 고른다.
_BG_OPTIONS = ("bg", "background", "activebackground", "highlightbackground",
               "insertbackground", "selectbackground", "troughcolor")
_FG_OPTIONS = ("fg", "foreground", "activeforeground", "highlightcolor",
               "selectforeground", "disabledforeground")
_COLOR_OPTIONS = _BG_OPTIONS + _FG_OPTIONS

_BG_PRIORITY = ["bg", "surface", "card", "surface_2", "surface_3", "card_hover",
                "btn_secondary", "border", "border_strong", "accent_soft",
                "success_bg", "warn_bg", "danger_bg", "info_bg", "accent",
                "accent_hover", "green", "red", "yellow", "purple", "orange",
                "focus", "text", "text_2", "muted", "on_accent"]
_FG_PRIORITY = ["text", "text_2", "muted", "on_accent", "accent", "accent_hover",
                "green", "red", "yellow", "purple", "orange", "focus",
                "border_strong", "border", "bg", "surface", "card", "surface_2",
                "surface_3", "card_hover", "btn_secondary", "accent_soft",
                "success_bg", "warn_bg", "danger_bg", "info_bg"]


def _reverse_lookup(value: str, old_palette: Dict[str, str],
                     priority: List[str]) -> Optional[str]:
    """old_palette 에서 값이 value 인 토큰 중 우선순위가 가장 높은 것 하나.

    매치가 없으면 None(이 위젯 옵션은 팔레트 색이 아니었다는 뜻 — 건드리지
    않는다). 매치가 하나뿐이면 그대로. 여럿이면(값 충돌) priority 순서로.
    """
    matches = [tok for tok, v in old_palette.items() if v == value]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    for tok in priority:
        if tok in matches:
            return tok
    return matches[0]   # 우선순위 표에 없는 낯선 충돌 — 결정론적으로 첫 매치


# 역매핑 인덱스 = (bg용 {값: 토큰}, fg용 {값: 토큰}).
_ReverseIndex = Tuple[Dict[str, str], Dict[str, str]]


def _build_reverse_index(palette: Dict[str, str]) -> _ReverseIndex:
    """`_reverse_lookup()` 의 결과를 팔레트 한 벌당 한 번만 미리 계산한다.

    전환 한 번 동안 old 팔레트는 불변인데, 예전엔 위젯 옵션마다
    `_reverse_lookup()` 이 팔레트 전체(30여 토큰)를 새로 훑었다 — 위젯
    3,000개 × 색 옵션 13개면 수만 번의 O(팔레트) 스캔이 된다. 선택 규칙은
    `_reverse_lookup()` 과 **글자 그대로 같다**(매치 1개면 그대로, 값이
    겹치면 우선순위 표, 표에 없는 낯선 충돌이면 팔레트 순서상 첫 매치).
    """
    by_value: Dict[str, List[str]] = {}
    for token, value in palette.items():
        by_value.setdefault(value, []).append(token)

    def _pick(matches: List[str], priority: List[str]) -> str:
        if len(matches) == 1:
            return matches[0]
        for token in priority:
            if token in matches:
                return token
        return matches[0]

    return ({v: _pick(m, _BG_PRIORITY) for v, m in by_value.items()},
            {v: _pick(m, _FG_PRIORITY) for v, m in by_value.items()})


# 위젯 클래스별 "실제로 가진 색 옵션" 캐시 — 아래 `_color_options_for()` 참조.
_COLOR_OPTION_CACHE: Dict[type, Tuple[str, ...]] = {}
# tk 옵션 별칭(둘 다 같은 옵션) — 정식 이름이 있으면 별칭은 건너뛴다.
_OPTION_ALIASES = {"bg": "background", "fg": "foreground"}


def _color_options_for(widget) -> Tuple[str, ...]:
    """이 위젯이 실제로 가진 색 옵션만 (위젯 클래스당 1회 계산 후 캐시).

    예전엔 13개 옵션을 전부 `cget()` 해 보고 없는 것은 TclError 로 걸렀는데,
    Tcl 예외 한 번이 성공 조회보다 4~5배 비싸다(2026-09-22 실측: `tk.Frame`
    13옵션 전수 19.8µs vs 보유 옵션만 2.1µs, `tk.Label` 13.3µs vs 4.7µs).
    옵션 구성은 위젯 **클래스**가 결정하므로 클래스마다 한 번만
    `keys()`(Tcl 1회)로 확인해 캐시한다. 읽지 못하면(파괴된 위젯·특수
    위젯) 예전처럼 전체 목록을 돌려준다 — 결과는 같고 느릴 뿐이다.
    """
    cls = widget.__class__
    cached = _COLOR_OPTION_CACHE.get(cls)
    if cached is not None:
        return cached
    try:
        available = set(widget.keys())
    except Exception:   # noqa: BLE001 — 옛 경로로 폴백(캐시도 남기지 않는다)
        return _COLOR_OPTIONS
    options = tuple(opt for opt in _COLOR_OPTIONS
                    if opt in available
                    and _OPTION_ALIASES.get(opt) not in available)
    _COLOR_OPTION_CACHE[cls] = options
    return options


def remap_color(value: str, kind: str = "fg") -> str:
    """단일 hex 색 값을 "이전 팔레트의 어떤 토큰이었는지" 역매핑해 **현재**
    팔레트(`C`) 기준 값으로 바꾼다(9호 Phase 5, DataTable 태그 색 회귀
    수리 — `tag_configure(foreground=C["orange"])` 처럼 생성 시점 hex 를
    그대로 저장해두는 위젯이 `_on_retheme()` 안에서 쓴다).

    내부적으로 `retheme()` 이 tk 위젯에 쓰는 것과 같은 역매핑
    (`_reverse_lookup` + 우선순위 표)을 재사용한다 — 동작을 두 벌로
    유지하지 않는다. `kind="fg"`(기본, 글자색류)면 `_FG_PRIORITY`,
    `kind="bg"`(배경색류)면 `_BG_PRIORITY` 로 충돌(같은 값을 가진 토큰이
    여럿인) 시 우선순위를 정한다.

    호출 시점의 `dark`/`light` 팔레트 양쪽을 다 뒤진다 — 저장해 둔 색이
    어느 팔레트에서 만들어졌는지 굳이 기억해둘 필요가 없다(대개 한쪽
    에서만 매치된다). 어느 토큰과도 안 맞으면(팔레트 토큰이 아닌 사용자
    임의색) 원값을 그대로 돌려준다 — 훼손하지 않는다.
    """
    if not isinstance(value, str) or not value:
        return value
    priority = _BG_PRIORITY if kind == "bg" else _FG_PRIORITY
    for old_pal in (PALETTES["dark"], PALETTES["light"]):
        token = _reverse_lookup(value, old_pal, priority)
        if token is not None and token in C:
            return C[token]
    return value


def _rethemed_value(option: str, current: Any, old_palette: Dict[str, str],
                     new_palette: Dict[str, str],
                     index: Optional[_ReverseIndex] = None) -> Optional[str]:
    """옵션의 현재 값을 새 팔레트 값으로 바꿀 값. 대상이 아니면 None.

    `index` 가 오면 미리 만든 역매핑(`_build_reverse_index`)을 쓰고, 없으면
    예전처럼 그때그때 `_reverse_lookup()` 한다 — 결과는 동일하다.
    """
    if not isinstance(current, str) or not current:
        return None
    if index is not None:
        token = (index[0] if option in _BG_OPTIONS else index[1]).get(current)
    else:
        priority = _BG_PRIORITY if option in _BG_OPTIONS else _FG_PRIORITY
        token = _reverse_lookup(current, old_palette, priority)
    if token is None or token not in new_palette:
        return None
    new_value = new_palette[token]
    return new_value if new_value != current else None


def _retheme_one_widget(widget, old_palette: Dict[str, str],
                        new_palette: Dict[str, str],
                        index: Optional[_ReverseIndex] = None) -> None:
    """위젯 하나의 색 옵션을 재도색.

    이 위젯이 실제로 가진 색 옵션만 조회하고(`_color_options_for`), 바뀌는
    옵션은 모아 **한 번의 `configure()`** 로 적용한다(Tcl 왕복 축소). 모아
    적용이 실패하면 옵션 하나씩 다시 시도해 예전과 같은 '하나가 실패해도
    나머지는 적용' 동작을 유지한다. 읽는 값은 모두 변경 **전** 상태라,
    옵션을 순서대로 하나씩 적용하던 예전 결과와 같다(별칭 bg/background 는
    `_color_options_for` 가 이미 하나로 줄인다)."""
    changes: Dict[str, str] = {}
    for option in _color_options_for(widget):
        try:
            current = widget.cget(option)
        except tk.TclError:
            continue
        new_value = _rethemed_value(option, current, old_palette, new_palette,
                                    index)
        if new_value is None:
            continue
        changes[option] = new_value
    if changes:
        try:
            widget.configure(**changes)
        except tk.TclError:
            for option, new_value in changes.items():
                try:
                    widget.configure(**{option: new_value})
                except tk.TclError:
                    pass
    if isinstance(widget, tk.Canvas):
        _retheme_canvas_items(widget, old_palette, new_palette, index)
    if isinstance(widget, tk.Text):
        _retheme_text_tags(widget, old_palette, new_palette, index)


def _retheme_canvas_items(canvas: "tk.Canvas", old_palette: Dict[str, str],
                          new_palette: Dict[str, str],
                          index: Optional[_ReverseIndex] = None) -> None:
    """차트 등 Canvas 위에 그려진 아이템의 fill/outline 도 같은 방식으로."""
    try:
        item_ids = canvas.find_all()
    except tk.TclError:
        return
    for item_id in item_ids:
        for option, priority in (("fill", _BG_PRIORITY), ("outline", _FG_PRIORITY)):
            try:
                current = canvas.itemcget(item_id, option)
            except tk.TclError:
                continue
            if not current:
                continue
            if index is not None:
                token = (index[0] if priority is _BG_PRIORITY
                         else index[1]).get(current)
            else:
                token = _reverse_lookup(current, old_palette, priority)
            if token is None or token not in new_palette:
                continue
            new_value = new_palette[token]
            if new_value == current:
                continue
            try:
                canvas.itemconfigure(item_id, **{option: new_value})
            except tk.TclError:
                pass


def _retheme_text_tags(text_widget: "tk.Text", old_palette: Dict[str, str],
                       new_palette: Dict[str, str],
                       index: Optional[_ReverseIndex] = None) -> None:
    """Text/ScrolledText 의 tag_configure 색(foreground/background)도 재도색."""
    try:
        tag_names = text_widget.tag_names()
    except tk.TclError:
        return
    for tag in tag_names:
        for option, priority in (("foreground", _FG_PRIORITY),
                                 ("background", _BG_PRIORITY)):
            try:
                current = text_widget.tag_cget(tag, option)
            except tk.TclError:
                continue
            if not current:
                continue
            if index is not None:
                token = (index[0] if priority is _BG_PRIORITY
                         else index[1]).get(current)
            else:
                token = _reverse_lookup(current, old_palette, priority)
            if token is None or token not in new_palette:
                continue
            new_value = new_palette[token]
            if new_value == current:
                continue
            try:
                text_widget.tag_configure(tag, **{option: new_value})
            except tk.TclError:
                pass


# CTk 위젯 안전망(8호 Phase 2 디자이너 감사 🔴2)에서 훑는 색 옵션 —
# fg/bg 계열은 _BG_PRIORITY, 글자색 계열은 _FG_PRIORITY 로 역매핑한다.
_CTK_BG_LIKE_OPTIONS = ("fg_color", "bg_color", "border_color", "hover_color",
                        "button_color", "progress_color", "selected_color",
                        "selected_hover_color", "unselected_color",
                        "unselected_hover_color")
_CTK_FG_LIKE_OPTIONS = ("text_color", "text_color_disabled",
                        "placeholder_text_color")
_CTK_COLOR_OPTIONS = _CTK_BG_LIKE_OPTIONS + _CTK_FG_LIKE_OPTIONS


def _retheme_ctk_widget(widget, old_palette: Dict[str, str],
                        index: Optional[_ReverseIndex] = None) -> None:
    """CTk 위젯 1개의 색 옵션 안전망 — 정상적으로 (light,dark) 페어로 만든
    위젯은 손대지 않는다(`cget()` 이 튜플을 그대로 돌려주면 이미
    `ctk.set_appearance_mode()`(retheme() 가 apply_palette() 안에서 호출)
    만으로 스스로 다시 그려짐 — customtkinter 6.0 실측 확인).

    `cget()` 이 **단일 hex 문자열**을 돌려주는 위젯(제작 실수 등으로
    페어가 아니라 단일 색으로 만들어진 레거시/누락 케이스)만 골라
    old_palette 역매핑으로 토큰을 찾고, 그 토큰의 정식 (light,dark) 페어로
    `configure()` 한다 — 이번 전환은 물론 이후로도 스스로 재도색되는
    "진짜 페어" 위젯이 된다. 지원 안 하는 옵션·"transparent" 같은 특수값은
    조용히 건너뛴다(`ValueError`/`TclError` 둘 다 — CTkBaseClass.cget() 은
    모르는 옵션에 TclError 가 아니라 ValueError 를 던지는 경우가 있다,
    8호 Phase 2 실측 확인)."""
    for option in _CTK_COLOR_OPTIONS:
        try:
            current = widget.cget(option)
        except (tk.TclError, ValueError, AttributeError):
            continue
        if not isinstance(current, str) or not current or current == "transparent":
            continue   # 이미 페어(튜플)거나 이 위젯엔 없는 옵션·특수값.
        priority = _FG_PRIORITY if option in _CTK_FG_LIKE_OPTIONS else _BG_PRIORITY
        if index is not None:
            token = (index[1] if priority is _FG_PRIORITY
                     else index[0]).get(current)
        else:
            token = _reverse_lookup(current, old_palette, priority)
        if token is None:
            continue
        try:
            widget.configure(**{option: _pair(token)})
        except (tk.TclError, ValueError):
            pass


# CTk 위젯이 내부적으로 들고 있는 "구현 전용" 자식 위젯의 속성 이름 —
# _retheme_widget_tree 가 CTk 컨테이너 안까지 순회할 때 이 자식들만은
# 건너뛴다(코더 E 실측, 8호 Phase 2: CTkButton 내부 `_canvas`/`_text_label`
# 를 역매핑하면 hover/포커스 하이라이트가 깨진다). 위젯 종류마다 갖는
# 속성이 달라 `getattr(w, name, None)` 으로 존재하는 것만 모은다.
# ⚠️ `CTkFrame` 은 이미 자체 `winfo_children()` 오버라이드로 `_canvas` 를
# 뺀 목록을 돌려준다(customtkinter 6.0 실측 확인 — "not a child but part
# of the CTkFrame itself" 라는 원본 docstring 그대로) — 여기 목록은
# CTkButton/CTkEntry/CTkSwitch/CTkComboBox 등 그 오버라이드가 없는
# 위젯들을 위한 것이고, CTkFrame 에겐 그냥 no-op 이라 겹쳐도 무해하다.
_CTK_INTERNAL_CHILD_ATTRS = (
    "_canvas", "_bg_canvas", "_text_label", "_image_label", "_entry",
    "_textbox", "_scrollbar", "_dropdown_menu", "_parent_canvas",
    "_parent_frame", "_progressbar",
)


def _ctk_internal_children(widget) -> set:
    """widget 이 내부 구현으로 참조하는 자식 객체 집합(identity 비교용)."""
    internal = set()
    for name in _CTK_INTERNAL_CHILD_ATTRS:
        obj = getattr(widget, name, None)
        if obj is not None:
            internal.add(obj)
    return internal


def _retheme_widget_tree(widget, old_palette: Dict[str, str],
                         new_palette: Dict[str, str],
                         index: Optional[_ReverseIndex] = None) -> None:
    """widget(보통 root)과 그 아래 전체(Toplevel 포함 — Toplevel 도 만든
    부모의 winfo_children() 에 잡힌다) 를 재귀 순회하며 재도색한다.

    CTk 위젯(auto-dev 8호)은 `_retheme_ctk_widget()` 안전망을 먼저 태운
    뒤, **자체 내부 구현 위젯만 건너뛰고 나머지 자식은 계속 순회**한다
    (코더 E 실측 회귀, 8호 Phase 2 — 예전엔 CTk 위젯을 만나면 그 아래를
    아예 안 봐서, Card 같은 CTkFrame 컨테이너 *안*에 넣은 순정
    `tk.Label(bg=C["card"])` 등이 라이트 전환 후에도 다크로 굳었다. 홈
    신호등·대시보드 KPI·성장·채널 탭처럼 CTkFrame 안에 tk 위젯을 넣는
    화면 전부가 영향을 받았다). `_ctk_internal_children()` 로 그 위젯의
    캔버스·내부 라벨 등만 골라 스킵하고, 사용자가 넣은 진짜 자식(tk
    위젯이든 중첩 CTk 위젯이든)은 정상적으로 재귀한다.

    파괴된 위젯이 순회 도중 섞여 있어도 개별 widget 단위에서 TclError 를
    삼켜 흐름이 끊기지 않는다.
    """
    if index is None:   # 트리 전체에서 한 번만 만든다(재귀 중엔 그대로 전달).
        index = _build_reverse_index(old_palette)
    skip: set = ()
    if HAS_CTK and isinstance(widget, ctk.CTkBaseClass):
        try:
            _retheme_ctk_widget(widget, old_palette, index)
        except Exception as exc:  # noqa: BLE001 — 안전망 실패가 전환을 막으면 안 됨
            log.debug("[theme] CTk 위젯 안전망 재도색 실패: %s", exc, exc_info=True)
        try:
            skip = _ctk_internal_children(widget)
        except Exception as exc:  # noqa: BLE001 — 내부 자식 탐지 실패해도 순회는 계속
            log.debug("[theme] CTk 내부 자식 탐지 실패: %s", exc, exc_info=True)
    else:
        try:
            _retheme_one_widget(widget, old_palette, new_palette, index)
        except tk.TclError:
            pass
    # 위젯 자체 재도색 훅(9호 Phase 2, DataTable 등) — 있으면 호출하고 그
    # 서브트리는 더 내려가지 않는다. Canvas 아이템(가상화된 표 행 등)은
    # 일반 리버스 매핑으로 태그별 임의색까지 정확히 못 잡는 경우가 많아,
    # 위젯이 "내가 내 서브트리를 통째로 다시 그린다"고 선언하는 편이
    # CTk 내부 위젯 스킵과 같은 이유로 더 안전하다.
    on_retheme = getattr(widget, "_on_retheme", None)
    if callable(on_retheme):
        try:
            on_retheme()
        except Exception as exc:  # noqa: BLE001 — 훅 실패가 나머지 트리 전환을 막으면 안 됨
            log.debug("[theme] _on_retheme 훅 실패: %s", exc, exc_info=True)
        return
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return
    for child in children:
        if child in skip:
            continue
        _retheme_widget_tree(child, old_palette, new_palette, index)


def _retheme_ttk_styles() -> None:
    """ttk 스타일 재적용 — Treeview/ProgressRow 는 자체 팔레트 가드로 재호출만
    하면 되고, Combobox/Scrollbar 는 여기서 최소 3종을 직접 설정한다."""
    configure_treeview_style()
    try:
        from gui import widgets as _widgets   # 지연 임포트 — 순환 임포트 회피
        _widgets._ensure_progress_style()
    except Exception as exc:  # noqa: BLE001 — widgets 미로드 등도 팔레트 적용은 유지
        log.debug("[theme] ProgressRow 스타일 재적용 실패: %s", exc, exc_info=True)
    try:
        from tkinter import ttk
        sty = ttk.Style()
        try:
            sty.theme_use("clam")
        except tk.TclError:
            pass
        sty.configure("TCombobox", fieldbackground=C["surface"], foreground=C["text"],
                      background=C["card"], insertcolor=C["text"])
        sty.configure("Vertical.TScrollbar", troughcolor=C["surface"],
                      background=C["card"], arrowcolor=C["text_2"])
        sty.configure("Horizontal.TScrollbar", troughcolor=C["surface"],
                      background=C["card"], arrowcolor=C["text_2"])
        # 이미지 탭 진행률 바(탭 생성 시 1회 configure) — 리뷰 7호 Medium: 재적용 누락
        sty.configure("Image.Horizontal.TProgressbar", troughcolor=C["border"],
                      background=C["green"], darkcolor=C["green"],
                      lightcolor=C["green"], bordercolor=C["border"])
    except tk.TclError as exc:
        log.debug("[theme] Combobox/Scrollbar 스타일 재적용 실패: %s", exc)


def retheme(root, name: str) -> str:
    """재시작 없이 라이브 팔레트를 전환한다.

    ① 현재 팔레트 스냅샷(old) ② apply_palette(name) 으로 C 를 in-place 갱신
    (+ HAS_CTK 면 ctk.set_appearance_mode) ③ root(와 그 아래 모든 Toplevel)의
    tk 위젯 트리를 재귀 순회해, 각 위젯의 색 옵션 현재값이 old 팔레트의 어떤
    토큰과 같으면 새 값으로 교체(CTk 위젯은 건너뜀 — set_appearance_mode 가
    처리) ④ ttk 스타일(Treeview·ProgressRow·Combobox·Scrollbar)도 재적용.

    순회·스타일 재적용 중 무엇이 실패해도(TclError 포함 어떤 예외든) 삼키고
    계속한다 — 팔레트(C)는 이미 적용된 뒤이므로, 이 함수는 항상 실제
    적용된 팔레트 이름을 반환한다(미지 이름은 apply_palette 가 "dark" 로
    폴백). 다음 화면 재구성(탭 재생성 등)은 어차피 새 C 값으로 그려지므로,
    지금 화면에 떠 있는 위젯 재도색이 부분적으로 실패해도 다음 진입 시
    자연 치유된다.
    """
    old_palette = dict(C)
    resolved = apply_palette(name)
    try:
        _retheme_widget_tree(root, old_palette, C)
    except Exception as exc:  # noqa: BLE001 — 순회 실패해도 팔레트 적용은 유지
        log.debug("[theme] retheme 위젯 트리 순회 실패: %s", exc, exc_info=True)
    try:
        _retheme_ttk_styles()
    except Exception as exc:  # noqa: BLE001
        log.debug("[theme] retheme ttk 스타일 재적용 실패: %s", exc, exc_info=True)
    return resolved


# 간격 토큰(px) — 카드 패딩·사이드바 들여쓰기 등 전 위젯 공통 기준.
# xxxl(48, gui-design-system-v3.md §4)은 섹션↔섹션 사이 대여백 전용 신규 토큰.
# _BASE 는 100% DPI(96) 기준 설계값 원본 — 절대 직접 쓰지 않는다(값이
# 굳어 고DPI 에서 확대가 안 됨). 코더들이 이미 `SP["md"]` 식으로 광범위하게
# 참조하므로, 이 이름(SP/R) 은 px() 적용된 값을 담아 자동으로 커지게 하고
# 원본은 재계산용으로만 `_BASE` 에 보관한다(9호 Phase 1 — `set_ui_scale()`
# 참조, 위 정의부).
SP_BASE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24, "xxl": 32, "xxxl": 48}
SP = {k: px(v) for k, v in SP_BASE.items()}

# 반경 토큰(px, CTk corner_radius 네이티브 지원) — gui-design-system-v3.md §4.
R_BASE = {"sm": 6, "md": 10, "lg": 14, "pill": 999}
R = {k: px(v) for k, v in R_BASE.items()}

# ── 타이포 스케일 ─────────────────────────────────────
# 본문 폰트 우선순위: Pretendard(디자인 시스템 v3 지정) → Segoe UI Variable
# → 맑은 고딕(항상 존재하는 한국어 Windows 안전망). 탐지엔 살아있는 Tk 루트가
# 필요하다(gui/icons.py 와 같은 제약) — theme.py 는 app.py 가 root 를 만들기
# *전에* 임포트되는 게 보통이라(다른 탭 모듈들이 먼저 `from gui.theme import
# FT` 를 실행), 이 시점의 감지는 대개 폴백값을 쓴다. 그래서 FT/FB/FS/FH/FN
# 은 "임포트 시점 스냅샷"(기존 이름·튜플 계약 유지용)이고, 루트가 생긴 뒤
# 항상 정확한 값이 필요한 신규 코드는 아래 `font()` 헬퍼를 쓸 것(위젯 생성은
# 루트가 있어야만 가능하므로 이 헬퍼는 호출 시점엔 항상 정확하다).
_BODY_FONT_CANDIDATES = ("Pretendard", "Segoe UI Variable", "맑은 고딕")
FONT_FAMILY_FALLBACK = "맑은 고딕"

_body_font_cache: Optional[str] = None
_body_font_cache_valid = False


def _detect_body_font_family() -> str:
    global _body_font_cache, _body_font_cache_valid
    if _body_font_cache_valid:
        return _body_font_cache
    root = tk._default_root  # noqa: SLF001 — tkinter 표준 관례
    if root is None:
        # 루트 생기기 전(모듈 임포트 시점) — 1순위 Windows GDI 폰트 열거,
        # 실패하면 레지스트리, 그마저 실패하면 폴백. GDI 를 1순위로 두는
        # 이유(지휘자 2026-09-03 실측): 이 개발 PC 의 Pretendard 는
        # AddFontResource 류로 **세션에만 로드**돼 있어(정적 설치가 아님)
        # 레지스트리(HKCU/HKLM Fonts)·폰트 폴더 어디에도 안 잡힌다 —
        # `tkfont.families()` 에는 보이는데(루트가 있으면) 레지스트리로는
        # 영원히 못 찾는 사각지대. GDI `EnumFontFamiliesExW` 는 "지금 이
        # 세션에 실제로 로드된" 폰트를 보므로 루트 없이도 그 사각지대를
        # 메운다(`_detect_font_via_gdi` 참조). 캐시하지 않는다 — 루트가
        # 생긴 뒤 다시 호출되면 tkfont 경로로 정확히 재감지한다.
        return (_detect_font_via_gdi() or _detect_font_via_registry()
                or FONT_FAMILY_FALLBACK)
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families(root))
    except tk.TclError as exc:
        log.debug("[theme] 폰트 목록 조회 실패: %s", exc, exc_info=True)
        _body_font_cache = FONT_FAMILY_FALLBACK
        _body_font_cache_valid = True
        return _body_font_cache
    _body_font_cache = next((c for c in _BODY_FONT_CANDIDATES if c in families),
                            FONT_FAMILY_FALLBACK)
    _body_font_cache_valid = True
    return _body_font_cache


def _normalize_registry_font_name(name: str) -> str:
    """레지스트리 폰트 값 이름을 비교 가능한 형태로 정규화한다.

    실측 값 이름 예: "Segoe UI Variable (TrueType)", "Pretendard Variable
    (TrueType)", "Pretendard-Regular (TrueType)". 소문자화 + 끝의 형식
    괄호구문(" (TrueType)" 등) 제거 + 흔한 구분자(하이픈)를 공백으로
    통일한다 — 8호 Phase 2: 원래 `startswith(cand)` 는 "Pretendard Variable
    (TrueType)"처럼 그대로도 매치되지만, "Pretendard-Regular" 류 하이픈
    결합이나 대소문자가 다른 변형은 놓칠 수 있어 보강했다.
    """
    n = name.strip().lower()
    n = re.sub(r"\s*\([^)]*\)\s*$", "", n)   # 끝의 "(TrueType)" 류 괄호 제거
    n = n.replace("-", " ")
    return re.sub(r"\s+", " ", n).strip()


def _detect_font_via_registry() -> Optional[str]:
    """Tk 루트 없이 Windows 폰트 레지스트리(HKCU/HKLM Fonts)에서 후보 서체 설치 여부를 본다.

    Windows 가 아니거나 조회 실패 시 None(호출자가 폴백). 값 이름 예:
    "Pretendard Variable (TrueType)" / "Pretendard-Regular (TrueType)".
    `_BODY_FONT_CANDIDATES` 순서(Pretendard 우선)대로 찾되, 각 후보를
    "정확히 그 이름" 또는 "그 이름으로 시작하는 변형(웨이트·Variable 접미사
    포함)"까지 정규화 비교로 잡는다 — 이 PC 처럼 Pretendard 가 레지스트리에
    없이 세션 전용(private) 리소스로만 등록된 경우는 어차피 레지스트리로
    못 보므로(알려진 한계, 살아있는 Tk 루트가 생기면 `tkinter.font.families()`
    쪽이 정확히 잡는다) None 을 반환해 폴백에 맡긴다.
    """
    try:
        import winreg  # type: ignore
    except ImportError:
        return None
    names: list = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        try:
            i = 0
            while True:
                try:
                    name, _val, _typ = winreg.EnumValue(key, i)
                except OSError:
                    break
                names.append(name)
                i += 1
        finally:
            winreg.CloseKey(key)
    normalized = [_normalize_registry_font_name(n) for n in names]
    for cand in _BODY_FONT_CANDIDATES:
        cand_norm = _normalize_registry_font_name(cand)
        for n in normalized:
            if n == cand_norm or n.startswith(cand_norm + " "):
                return cand
    return None


_LF_FACESIZE = 32
_LF_DEFAULT_CHARSET = 1

_gdi_families_cache: Optional[frozenset] = None
_gdi_families_cache_valid = False


def _enum_installed_font_families_via_gdi() -> Optional[frozenset]:
    """Windows GDI `EnumFontFamiliesExW` 로 "지금 이 세션에 로드돼 있는"
    모든 폰트 패밀리 이름 집합을 얻는다. Tk 루트가 없어도 동작한다(살아
    있는 디바이스 컨텍스트만 있으면 됨 — `user32.GetDC(0)`).

    레지스트리(`_detect_font_via_registry`)와 달리 AddFontResource 류로
    **세션에만** 로드된 폰트(정적 설치가 아닌 것 — 2026-09-03 지휘자
    실측: 이 개발 PC 의 Pretendard 가 이 경우, `tkfont.families()` 에는
    보이는데 레지스트리·폰트 폴더 어디에도 없음)까지 본다.

    Windows 가 아니거나 ctypes 호출 어디서든 실패하면 None(호출자가
    다음 폴백 단계로 넘어간다). 결과는 프로세스 생존 동안 모듈 캐시 —
    설치된 폰트 목록이 런타임에 바뀌는 경우는 이 앱 시나리오에 없다.
    """
    global _gdi_families_cache, _gdi_families_cache_valid
    if _gdi_families_cache_valid:
        return _gdi_families_cache
    try:
        import ctypes
        from ctypes import wintypes
        if not hasattr(ctypes, "windll"):
            raise AttributeError("windll 없음 — Windows 가 아님")

        class _LOGFONTW(ctypes.Structure):
            _fields_ = [
                ("lfHeight", wintypes.LONG), ("lfWidth", wintypes.LONG),
                ("lfEscapement", wintypes.LONG), ("lfOrientation", wintypes.LONG),
                ("lfWeight", wintypes.LONG), ("lfItalic", wintypes.BYTE),
                ("lfUnderline", wintypes.BYTE), ("lfStrikeOut", wintypes.BYTE),
                ("lfCharSet", wintypes.BYTE), ("lfOutPrecision", wintypes.BYTE),
                ("lfClipPrecision", wintypes.BYTE), ("lfQuality", wintypes.BYTE),
                ("lfPitchAndFamily", wintypes.BYTE),
                ("lfFaceName", wintypes.WCHAR * _LF_FACESIZE),
            ]

        # 콜백의 두 번째 인자(TEXTMETRICW*)는 안 읽으므로 정확한 구조체
        # 정의 없이 c_void_p 로 받는다(포인터 값만 통과, 역참조 안 함).
        _FONTENUMPROC = ctypes.WINFUNCTYPE(
            ctypes.c_int, ctypes.POINTER(_LOGFONTW), ctypes.c_void_p,
            wintypes.DWORD, wintypes.LPARAM)

        found: set = set()

        def _collect(lpelfe, _lpntme, _font_type, _lparam):
            try:
                found.add(lpelfe.contents.lfFaceName)
            except (ValueError, AttributeError):
                pass
            return 1   # 0 을 돌려주면 열거가 중단됨 — 항상 계속 진행.

        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        hdc = user32.GetDC(0)
        if not hdc:
            raise OSError("GetDC(0) 실패")
        try:
            logfont = _LOGFONTW()
            logfont.lfCharSet = _LF_DEFAULT_CHARSET
            callback = _FONTENUMPROC(_collect)
            gdi32.EnumFontFamiliesExW(hdc, ctypes.byref(logfont), callback, 0, 0)
        finally:
            user32.ReleaseDC(0, hdc)
    except (ImportError, OSError, AttributeError, ValueError) as exc:
        log.debug("[theme] GDI 폰트 열거 실패: %s", exc, exc_info=True)
        _gdi_families_cache = None
        _gdi_families_cache_valid = True
        return None
    _gdi_families_cache = frozenset(found)
    _gdi_families_cache_valid = True
    return _gdi_families_cache


def _detect_font_via_gdi() -> Optional[str]:
    """GDI 로 열거한 패밀리 집합에서 `_BODY_FONT_CANDIDATES` 순서(Pretendard
    우선)로 첫 매치를 돌려준다. 열거 자체가 실패하면(비-Windows 등) None."""
    families = _enum_installed_font_families_via_gdi()
    if families is None:
        return None
    return next((c for c in _BODY_FONT_CANDIDATES if c in families), None)


FONT_FAMILY = _detect_body_font_family()   # 임포트 시점 스냅샷(루트 없으면 폴백)


# ── 굵은 글꼴 — 합성 bold 대신 정적 SemiBold 패밀리(있으면) ─────────
# Tk 는 굵기를 normal/bold 2단계로만 인식해(합성 bold — 렌더러가 획을
# 인위적으로 부풀림) 디자인 시스템 v3 §3 이 지정한 "Pretendard SemiBold"
# 정적 패키지의 실제 렌더 품질에 못 미친다. 그 정적 패밀리가 이 환경에
# 있으면(코드값 확인 없이 "이름이 존재하는가"만 검사) weight=normal 로
# 그 이름 자체를 써서 상용 서체 렌더 품질을 확보한다(8호 Phase 2 지휘자
# 실측 지시). 매핑에 없는 본문 폰트(맑은 고딕 등)는 기존처럼 합성 bold.
_BOLD_FAMILY_MAP: Dict[str, str] = {"Pretendard": "Pretendard SemiBold"}

_bold_font_cache: Optional[str] = None
_bold_font_cache_valid = False


def _detect_bold_font_family() -> Optional[str]:
    """`FONT_FAMILY` 에 대응하는 정적 SemiBold 패밀리 이름(있으면) 또는
    None(호출자는 기존처럼 `(FONT_FAMILY, size, "bold")` 합성 bold로
    폴백). Tk 루트가 있으면 `tkfont.families()`, 없으면 GDI 열거로 존재
    여부를 확인한다 — `_detect_body_font_family()` 와 같은 우선순위."""
    global _bold_font_cache, _bold_font_cache_valid
    if _bold_font_cache_valid:
        return _bold_font_cache
    candidate = _BOLD_FAMILY_MAP.get(FONT_FAMILY)
    if candidate is None:
        _bold_font_cache = None
        _bold_font_cache_valid = True
        return None
    root = tk._default_root  # noqa: SLF001
    families: Optional[frozenset] = None
    if root is not None:
        try:
            import tkinter.font as tkfont
            families = frozenset(tkfont.families(root))
        except tk.TclError as exc:
            log.debug("[theme] SemiBold 확인용 폰트 목록 조회 실패: %s", exc,
                     exc_info=True)
            families = None
    if families is None:
        families = _enum_installed_font_families_via_gdi()
    if families is None:
        return None   # 캐시하지 않음 — 나중에 루트/GDI 가 생기면 재시도
    _bold_font_cache = candidate if candidate in families else None
    _bold_font_cache_valid = True
    return _bold_font_cache


FONT_FAMILY_BOLD = _detect_bold_font_family()   # 임포트 시점 스냅샷


def _bold_tuple(size: int) -> tuple:
    """FT/FH/FN/F_DISPLAY 공용 — SemiBold 정적 패밀리가 있으면 그걸
    weight=normal 로, 없으면 기존처럼 합성 bold 튜플로."""
    if FONT_FAMILY_BOLD:
        return (FONT_FAMILY_BOLD, size)
    return (FONT_FAMILY, size, "bold")


def font_weight(font_tuple: tuple) -> str:
    """FT/FH/FN/F_DISPLAY 처럼 환경에 따라 2원소(SemiBold 정적 패밀리를
    weight=normal 로 씀) 또는 3원소((family,size,"bold") 합성 bold)일 수
    있는 튜플에서 굵기 문자열만 안전하게 뽑는다 — `len(...)` 로 직접
    분기하지 말고 이 헬퍼를 쓸 것(8호 Phase 2 리뷰어 지적, 호출부가
    늘어날수록 분기 중복을 막는다)."""
    return font_tuple[2] if len(font_tuple) > 2 else "normal"


def font(size: int, weight: str = "normal") -> tuple:
    """실시간 재감지 폰트 튜플 — (family, size) 또는 (family, size, weight).

    새 코드(widgets.py 신규 컴포넌트, 향후 탭 재도장)는 고정된 FT/FB/...
    대신 이 함수를 쓰면 위젯을 만드는 시점(=루트가 반드시 존재하는 시점)의
    정확한 폰트 패밀리를 얻는다. `weight="bold"` 는 `_detect_bold_font_family()`
    로 SemiBold 정적 패밀리를 우선 시도한다(없으면 합성 bold 폴백).
    """
    family = _detect_body_font_family()
    if weight == "normal":
        return (family, size)
    if weight == "bold":
        bold_family = _detect_bold_font_family()
        if bold_family:
            return (bold_family, size)
        return (family, size, "bold")
    return (family, size, weight)


# 크기는 gui-design-system-v3.md §3 타이포 스케일(Pretendard 기준). 굵은
# 토큰(FT/FH/FN/F_DISPLAY)은 `_bold_tuple()` 로 SemiBold 정적 패밀리가
# 있으면 그 이름(weight=normal)을, 없으면 합성 bold 를 쓴다(8호 Phase 2
# — 위 `FONT_FAMILY_BOLD` 정의부 참조, 문서 §3 "굵기 주의" 해결).
# ⚠️ 그래서 이 넷은 실행 환경에 따라 **2원소 또는 3원소** 튜플이다 —
# `font_tuple[2]` 를 직접 인덱싱하지 말 것(SemiBold 있는 환경에선
# IndexError). 굵기가 필요하면 위 `font_weight(font_tuple)` 헬퍼를 쓴다.
# v4(2026-09-21): 화면 제목(FH)을 18 → 21 로 올려 "제목 > 섹션 > 본문 >
# 캡션"의 단차를 뚜렷하게 만든다(개편 전 캡처에서 제목 18 / 섹션 14 / 본문
# 13 이 거의 같은 덩어리로 읽혀 화면마다 "지금 어디인지"가 안 보였다).
# FT/FB/FS/FM 은 밀도가 높은 기존 탭(설정·통계 등)의 레이아웃을 흔들지
# 않도록 그대로 둔다.
FT = _bold_tuple(14)                     # Section
FB = (FONT_FAMILY, 13)                   # Body
FS = (FONT_FAMILY, 11)                   # Caption
FM = ("Consolas", 12)                    # Mono — 로그·코드 전용, 고정폭 유지
FH = _bold_tuple(21)                     # Title(화면 제목)
FN = _bold_tuple(28)                     # KPI 숫자 — 대시보드·관제탑 큰 숫자 전용

# 설계서 §1.1/§3 표기와 동일한 이름의 별칭 — 신규 코드는 이 이름을 써도 되고
# 기존 FT/FB/FS/FM/FH 를 계속 써도 된다(완전 동일 튜플).
F_TITLE = FH
F_SECTION = FT
F_BODY = FB
F_SMALL = FS
F_CAPTION = FS                            # gui-design-system-v3.md §3 표기명
F_MONO = FM
F_NUM = FN
F_DISPLAY = _bold_tuple(24)               # 온보딩·빈 상태 헤드라인 전용(극소수 화면)


# ── 색 유틸 ───────────────────────────────────────────
def _lighten(hex_color: str, amt: int = 28) -> str:
    """hex 색을 amt 만큼 밝게(hover 배경 계산용, 버튼 팩토리 공용)."""
    c = hex_color.lstrip("#")
    r, g, b = int(c[:2], 16), int(c[2:4], 16), int(c[4:], 16)
    return "#{:02x}{:02x}{:02x}".format(min(255, r + amt), min(255, g + amt), min(255, b + amt))


def _darken(hex_color: str, amt: int = 28) -> str:
    """hex 색을 amt 만큼 어둡게(라이트 팔레트 hover 등 — _lighten 의 반대)."""
    c = hex_color.lstrip("#")
    r, g, b = int(c[:2], 16), int(c[2:4], 16), int(c[4:], 16)
    return "#{:02x}{:02x}{:02x}".format(max(0, r - amt), max(0, g - amt), max(0, b - amt))


def _pair(token: str) -> tuple:
    """토큰 이름 → (light값, dark값) 튜플. CTk 위젯 생성 전용(양쪽 팔레트
    고정값을 넘겨두면 ctk.set_appearance_mode() 전환만으로 스스로 재도색됨)."""
    return (PALETTES["light"].get(token, C.get(token, "")),
            PALETTES["dark"].get(token, C.get(token, "")))


def _resolve_pair(value):
    """CTk 의 (light,dark) 페어 또는 단일 문자열 → 현재 팔레트 기준 값 1개.

    CTk 는 색을 페어로 저장하는 경우가 많아(`cget('fg_color')` 가 리스트를
    돌려줄 수 있다 — customtkinter 6.0 실측 확인), ["bg"] 류 호환 계약에서
    문자열 하나로 뭉개 돌려줄 때 쓴다.
    """
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        return value[1] if current_palette() == "dark" else value[0]
    return value


def _safe_bg_of(widget) -> str:
    """부모 위젯의 배경색을 안전하게 읽는다 — CTk 위젯은 "bg" 를 몰라
    `ValueError` 를 던진다(tk 는 TclError). 둘 다 삼키고 C["card"] 폴백."""
    try:
        return widget.cget("bg")
    except (tk.TclError, ValueError):
        return C["card"]


def _single_to_pair(value: str) -> tuple:
    """단일 hex 값(예: 호출부가 넘긴 color=C["green"])을 CTk 페어로 승격.

    현재 활성 팔레트에서 어느 토큰과 같은 값인지 찾아 그 토큰의 (light,dark)
    페어를 쓴다 — 그래야 나중에 팔레트가 바뀌어도 같이 바뀐다. 어떤 토큰과도
    안 맞는 임의의 색이면(팔레트 밖 색) 고정 페어(value, value) 로 감수한다
    (라이트/다크 전환에 안 따라가는 것 — 흔치 않은 커스텀 color= 호출부만
    영향받는 알려진 한계).
    """
    token = _reverse_lookup(value, C, _BG_PRIORITY)
    if token is not None and token in PALETTES["light"] and token in PALETTES["dark"]:
        return _pair(token)
    return (value, value)


# CTkButton/CTkFrame 이 실제로 받는 옵션만 화이트리스트로 통과시킨다 — 기존
# 탭 코드는 여전히 tk.Button 시절 습관대로 `.config(pady=12, font=..., bd=0,
# relief="flat", cursor="hand2", ...)` 처럼 tk 전용 옵션을 섞어 부른다
# (실측: `gui/tabs/home.py` 의 `run_btn.config(font=..., pady=12)` 가 바로
# 이 경우 — CTkButton 은 pady 를 몰라 ValueError 를 던진다). 탭 코드를 고칠
# 수 없는 라운드라, 모르는 옵션은 조용히 버리는 쪽을 택했다 — 크래시보다
# "그 옵션만 무시됨"이 훨씬 안전하다(state/text 등 핵심 옵션은 전부 살아있다).
_CTK_BUTTON_SAFE_KEYS = frozenset({
    "width", "height", "corner_radius", "border_width", "border_spacing",
    "bg_color", "fg_color", "hover_color", "border_color", "text_color",
    "text_color_disabled", "text", "font", "textvariable", "image", "state",
    "hover", "command", "compound", "anchor",
})
_CTK_FRAME_SAFE_KEYS = frozenset({
    "width", "height", "corner_radius", "border_width", "bg_color", "fg_color",
    "border_color", "background_corner_colors",
})
_CTK_ENTRY_SAFE_KEYS = frozenset({
    "width", "height", "corner_radius", "border_width", "bg_color", "fg_color",
    "border_color", "text_color", "placeholder_text_color", "textvariable",
    "placeholder_text", "font", "state", "show", "justify", "validate",
    "validatecommand",
})


# ── tk 옵션 dict 계약 어댑터(CTk 전용) ─────────────────
class _CtkButtonAdapter:
    """CTkButton(아이콘 있으면 CTkFrame[CTkLabel+CTkButton]) 위의 얇은 어댑터.

    tk 옵션 dict 접근 계약(`btn["state"]`, `btn["text"]`, `.cget(...)`,
    `.config(...)`)을 CTk 위젯에서도 그대로 쓸 수 있게 한다(모듈 docstring
    참조 — `Misc.__getitem__` 이 cget 오버라이드를 우회하는 문제의 우회책).
    명시적으로 정의하지 않은 나머지 속성·메서드는 실제 상호작용 위젯
    (`self._button` — `.invoke()`/`.bind()`/`.winfo_*()` 등)에 위임한다.
    """

    def __init__(self, container, button, icon_label=None):
        self._container = container   # 배치(pack/grid/place) 기준 = 겉모습
        self._button = button          # state/text/command 을 실제로 갖는 위젯
        self._icon_label = icon_label
        self._bound_sequences: Dict[str, bool] = {}

    # ── tk 옵션 dict 계약 ──────────────────────────────
    def cget(self, key):
        if key in ("bg", "background"):
            return _resolve_pair(self._button.cget("fg_color"))
        if key in ("fg", "foreground"):
            return _resolve_pair(self._button.cget("text_color"))
        return self._button.cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def configure(self, **kwargs):
        remapped = {}
        for k, v in kwargs.items():
            if k in ("bg", "background"):
                remapped["fg_color"] = _single_to_pair(v) if isinstance(v, str) else v
            elif k in ("fg", "foreground"):
                remapped["text_color"] = _single_to_pair(v) if isinstance(v, str) else v
            elif k in _CTK_BUTTON_SAFE_KEYS:
                remapped[k] = v
            # else: CTkButton 이 모르는 tk 전용 옵션(pady/padx/bd/relief/cursor
            # 등) — 조용히 버린다(위 상수 정의부 주석 참조).
        if remapped:
            self._button.configure(**remapped)

    config = configure

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    # ── 배치는 컨테이너(아이콘 있으면 감싸는 프레임) 기준 ───
    def pack(self, *a, **kw):
        return self._container.pack(*a, **kw)

    def grid(self, *a, **kw):
        return self._container.grid(*a, **kw)

    def place(self, *a, **kw):
        return self._container.place(*a, **kw)

    def pack_forget(self):
        return self._container.pack_forget()

    def grid_forget(self):
        return self._container.grid_forget()

    def destroy(self):
        return self._container.destroy()

    def winfo_manager(self):
        return self._container.winfo_manager()

    def winfo_children(self):
        return self._container.winfo_children()

    def bind(self, sequence=None, func=None, add=None):
        """CTkButton.bind() 는 내부 캔버스/라벨로 리다이렉트만 하고 반환값이
        없어(`return` 문 자체가 없음 — customtkinter 6.0 실측 확인) 조회
        형태(`btn.bind(seq)`, func 생략)가 항상 None 을 돌려준다. 실제
        콜백 등록·발화는 정상 동작하므로(이벤트는 진짜로 온다), 등록
        시점의 시퀀스만 여기서 따로 기억해뒀다가 조회에 답한다."""
        if func is None and sequence is not None:
            return self._bound_sequences.get(sequence)
        kwargs = {} if add is None else {"add": add}
        result = self._button.bind(sequence, func, **kwargs)
        if sequence is not None:
            self._bound_sequences[sequence] = True
        return result

    # ── 나머지는 실제 버튼에 위임(.invoke()/.after() 등) ───
    def __getattr__(self, name):
        return getattr(self._button, name)


class _CtkFrameAdapter:
    """CTkFrame 위의 얇은 어댑터 — `card["bg"]` 읽기/`.config(bg=...)` 호환.

    다른 위젯을 이 어댑터를 master 로 삼아 만들어도(`tk.Label(card, ...)`)
    실제 Tcl 위젯 트리에는 내부의 진짜 CTkFrame 아래로 올바르게 매달린다
    (`.tk`/`.destroy()`/`.pack()`/`.place()`/`.grid()`/`.cget()`/
    `.configure()` 등 명시하지 않은 나머지는 전부 겉 프레임(`self._frame`)
    에 위임 — 2026-09-03 실측 확인).

    `content`(9호 Phase 1 신규) — `Card(parent, padx=, pady=)` 처럼 내부
    여백을 요청하면 `_ctk_card()` 가 겉 프레임 안에 투명 패딩용 프레임을
    하나 더 만들어 여기 넘긴다. **다른 위젯을 이 어댑터의 master 로 쓸 때
    실제로 매달리는 곳(`_w`)과 Python 쪽 자식 장부(`children`)는 그
    패딩 프레임을 가리켜야** padx/pady 가 진짜 내부 여백으로 보인다 —
    안 그러면(겉 프레임에 직접 매달리면) 패딩 프레임 자체만 한 칸 들여쓰기
    될 뿐 그 *안*에 넣은 라벨·버튼은 여전히 겉 프레임 가장자리에 붙는다.
    `content` 를 안 주면(기존 대다수 호출) `self._content is self._frame`
    이라 동작이 완전히 그대로다."""

    def __init__(self, frame, content=None):
        self._frame = frame
        self._content = content if content is not None else frame
        self._bound_sequences: Dict[str, bool] = {}

    @property
    def _w(self):
        """다른 위젯 생성자가 master=이_어댑터 로 받았을 때 실제로 매달릴
        Tcl 경로 — 항상 `self._content` 기준(패딩 없으면 겉 프레임과 동일)."""
        return self._content._w

    @property
    def children(self):
        """tkinter 위젯 생성자가 `master.children[name] = self` 로 직접
        쓰는 Python 쪽 자식 장부 — `_w` 와 같은 대상(`self._content`)을
        가리켜야 Tcl 실제 부모와 어긋나지 않는다."""
        return self._content.children

    def winfo_children(self):
        """겉 프레임이 아니라 실제 콘텐츠가 매달린 곳의 자식을 보여준다
        (패딩 프레임을 쓰면 겉 프레임의 유일한 Tcl 자식은 그 패딩
        프레임 자체라, 위임에 맡기면 사용자가 넣은 라벨·버튼이 하나도
        안 보인다)."""
        return self._content.winfo_children()

    def cget(self, key):
        if key in ("bg", "background"):
            return _resolve_pair(self._frame.cget("fg_color"))
        return self._frame.cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def configure(self, **kwargs):
        remapped = {}
        for k, v in kwargs.items():
            if k in ("bg", "background"):
                remapped["fg_color"] = _single_to_pair(v) if isinstance(v, str) else v
            elif k in _CTK_FRAME_SAFE_KEYS:
                remapped[k] = v
            # else: CTkFrame 이 모르는 tk 전용 옵션은 조용히 버린다(위 상수 참조).
        if remapped:
            self._frame.configure(**remapped)

    config = configure

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def bind(self, sequence=None, func=None, add=None):
        """CTkFrame.bind() 도 CTkButton 과 같은 이유로 조회 시 항상 None —
        _CtkButtonAdapter.bind() 와 동일한 우회(등록 시퀀스를 직접 기억)."""
        if func is None and sequence is not None:
            return self._bound_sequences.get(sequence)
        kwargs = {} if add is None else {"add": add}
        result = self._frame.bind(sequence, func, **kwargs)
        if sequence is not None:
            self._bound_sequences[sequence] = True
        return result

    def event_generate(self, sequence, **kwargs):
        """CTkFrame.bind() 가 실제로는 내부 `_canvas` 에 등록하므로(위 bind()
        docstring 참조), 합성 이벤트도 같은 대상에 쏴야 그 콜백이 실행된다
        — 겉 프레임에 쏘면 아무 일도 안 일어난다(2026-09-03 실측 확인)."""
        target = getattr(self._frame, "_canvas", None) or self._frame
        return target.event_generate(sequence, **kwargs)

    def __getattr__(self, name):
        return getattr(self._frame, name)


class _CtkEntryAdapter:
    """CTkEntry 위의 얇은 어댑터 — tk.Entry 옵션 dict 계약(`["state"]`,
    `.cget`/`.config`) + `.get()/.insert()/.delete()/.focus_set()` 는
    CTkEntry 가 이미 네이티브로 갖고 있어(실측 확인) `__getattr__` 위임만
    으로 충분하다. `show=` 는 생성자·`.configure(show=...)` 양쪽 다 CTkEntry
    가 내부 tk.Entry 로 그대로 통과시킨다(customtkinter6-notes.md §2 CTkEntry
    — 화이트리스트 kwargs 로 `show`/`validate` 등을 실제 tk.Entry 에 전달).
    """

    def __init__(self, entry):
        self._entry = entry
        self._bound_sequences: Dict[str, bool] = {}

    def cget(self, key):
        if key in ("bg", "background"):
            return _resolve_pair(self._entry.cget("fg_color"))
        if key in ("fg", "foreground"):
            return _resolve_pair(self._entry.cget("text_color"))
        return self._entry.cget(key)

    def __getitem__(self, key):
        return self.cget(key)

    def configure(self, **kwargs):
        remapped = {}
        for k, v in kwargs.items():
            if k in ("bg", "background"):
                remapped["fg_color"] = _single_to_pair(v) if isinstance(v, str) else v
            elif k in ("fg", "foreground"):
                remapped["text_color"] = _single_to_pair(v) if isinstance(v, str) else v
            elif k in _CTK_ENTRY_SAFE_KEYS:
                remapped[k] = v
            # else: CTkEntry 가 모르는 tk 전용 옵션(highlightthickness 등)은
            # 조용히 버린다 — 위 상수 정의부 주석 참조.
        if remapped:
            self._entry.configure(**remapped)

    config = configure

    def __setitem__(self, key, value):
        self.configure(**{key: value})

    def bind(self, sequence=None, func=None, add=None):
        """CTkEntry.bind() 도 CTkButton/CTkFrame 과 같은 이유로 조회 시
        항상 None — 동일한 우회(등록 시퀀스를 직접 기억)."""
        if func is None and sequence is not None:
            return self._bound_sequences.get(sequence)
        kwargs = {} if add is None else {"add": add}
        result = self._entry.bind(sequence, func, **kwargs)
        if sequence is not None:
            self._bound_sequences[sequence] = True
        return result

    def __getattr__(self, name):
        return getattr(self._entry, name)


# ── 공통 위젯 ─────────────────────────────────────────
def F(parent, **kw):
    return tk.Frame(parent, bg=kw.pop("bg", C["bg"]), **kw)


def Card(parent, **kw):
    """카드 컨테이너 — HAS_CTK 면 CTkFrame(radius 12·테두리 1px) + 어댑터,
    아니면 기존 순정 tk.Frame. 반환값을 다른 위젯의 master 로 써도 안전하다."""
    if HAS_CTK:
        return _ctk_card(parent, **kw)
    return tk.Frame(parent, bg=C["card"], relief="flat", **kw)


def _ctk_card(parent, **kw):
    # tk.Frame 시절 관례로 넘어오던 relief 는 CTkFrame 옵션이 아니라(전달
    # 시 ValueError) 여기서 걸러낸다. padx/pady 는(코더 E 실측, 9호 Phase 1)
    # 예전엔 여기서 같이 버려져 `Card(parent, padx=16, pady=12)` 가
    # HAS_CTK 경로에서 내부 여백을 통째로 잃었다(tk 폴백은 tk.Frame 이
    # padx/pady 를 네이티브로 지원해 정상이었음) — 아래에서 따로 꺼내
    # 내부 패딩 프레임으로 재현한다.
    kw.pop("relief", None)
    pad_x = kw.pop("padx", None)
    pad_y = kw.pop("pady", None)
    # ⚠️ 회귀 방지 메모(8호 Phase 2 리뷰어 지적) — `parent` 가 순정 tk
    # 위젯(tk.Tk/Toplevel/Frame/LabelFrame 등, CTk 아님)이면 아래 CTkFrame
    # 생성자가 **parent.configure/parent.config 를 몽키패치**한다
    # (customtkinter ctk_base_class.py `CTkBaseClass.__init__` 실측 확인
    # — bg= 변경을 감지해 이 CTkFrame 의 bg_color 에 전파하려는 의도적
    # 설계). 같은 parent 아래 CTk 위젯을 여러 개 만들면 매번 "이전 patch
    # 를 감싸는 새 patch" 가 쌓여 체인이 되고(기능은 정상 — 맨 안쪽까지
    # 순서대로 호출됨), 테스트에서 그 parent 로 mock/스텁 객체를 쓰며
    # `.configure` 를 직접 monkeypatch 하고 있었다면 이 체인이 그 위에
    # 덧씌워진다 — 이후 `parent.configure` 참조는 테스트가 심어둔 원본이
    # 아니라 CTk 가 감싼 래퍼다. 테스트가 fixture 생성 *이후*에
    # `parent.configure` 를 다시 monkeypatch 하거나, patch 시점을 CTk
    # 위젯 생성 이전으로 맞추면 이 함정을 피한다.
    frame = ctk.CTkFrame(parent, corner_radius=px(10), fg_color=_pair("card"),
                         border_width=px(1), border_color=_pair("border"), **kw)
    if pad_x is None and pad_y is None:
        return _CtkFrameAdapter(frame)
    # 투명 내부 프레임을 pack() 하면서 padx/pady 를 줘 "카드 안쪽 여백"을
    # 재현한다 — 값은 여기서 또 px() 하지 않는다: 실제 호출부(dashboard/
    # growth/home 탭)는 이미 `SP[...]` 토큰이나 `px()` 로 스케일해서
    # 넘기는 기존 관례(tk 폴백이 `tk.Frame(**kw)` 로 그대로 받던 것과
    # 동일)라, 여기서 또 곱하면 이중 확대된다.
    content = ctk.CTkFrame(frame, corner_radius=0, border_width=0,
                           fg_color="transparent")
    content.pack(fill="both", expand=True,
                padx=(pad_x if pad_x is not None else 0),
                pady=(pad_y if pad_y is not None else 0))
    return _CtkFrameAdapter(frame, content=content)


def L(parent, text, font=None, color=None, **kw):
    return tk.Label(parent, text=text, font=font or FB,
                    fg=color or C["text"], bg=parent["bg"], **kw)

def Sep(parent):
    return tk.Frame(parent, height=px(1), bg=C["border"])


# ── CTk 폰트 헬퍼(8호 Phase 2 캡처 실측 수정) ──────────────────────────
def _ctk_font(size: int, bold: bool = False, family: Optional[str] = None):
    """CTk 위젯 전용 `ctk.CTkFont` 생성 — 반드시 이 함수(또는 `ctk.CTkFont`
    를 직접)를 거쳐야 한다, **순정 튜플 `(family, size)` 를 CTk 위젯의
    font= 에 직접 넘기지 않는다.**

    이유(실측 캡처에서 CTk 버튼 글자가 지정한 13 보다 훨씬 작게(~10px)
    렌더된 원인): 순정 tk 는 font 튜플의 양수 size 를 "포인트"로 해석하고
    (음수라야 "픽셀"), CTk 위젯은 그 위에 `_apply_font_scaling()` 이
    `widget_scaling` 을 한 번 더 곱한다(`ctk_base_class.py`/
    `scaling_base_class.py` 확인) — `deactivate_automatic_dpi_awareness()`
    로 자동 DPI 인식을 껐어도(theme.py 상단) 이 스케일링 곱셈 자체는 남아
    있어 포인트 해석과 겹치면 실제 렌더 크기가 지정값보다 작아진다.
    `ctk.CTkFont` 는 내부에서 항상 음수(=픽셀) size 를 쓰므로
    (`CTkFont.__init__` 의 `size=-abs(self._size)`) 이 이중 축소를
    원천 차단한다.

    `family=` 를 명시하지 않으면 `bold=True` 일 때 `FONT_FAMILY_BOLD`
    (SemiBold 정적 패밀리, 있으면)를 쓰고 없으면 합성 bold. `family=` 를
    명시하면(예: 아이콘 글꼴) 그 패밀리를 그대로 쓰고 `bold` 는 weight
    only 로 반영한다(아이콘 글리프 패밀리를 body 폰트로 바꿔치기하면 안
    되므로 — `IconButton` 참조).

    `size` 는 설계 기준(100% DPI) 값 — 여기서 `px()` 로 `UI_SCALE` 을
    적용한다(9호 Phase 1). 호출부는 항상 100% 기준 숫자(13/14 등)를
    넘기고, 이미 스케일된 값을 또 넘기면 이중 확대된다."""
    if family is None:
        use_bold_family = bold and FONT_FAMILY_BOLD
        family = FONT_FAMILY_BOLD if use_bold_family else FONT_FAMILY
        weight = "normal" if use_bold_family else ("bold" if bold else "normal")
    else:
        weight = "bold" if bold else "normal"
    return ctk.CTkFont(family=family, size=px(size), weight=weight)


# ── 버튼 팩토리 — tk/CTk 공용 골격 ─────────────────────
def _ctk_flat_button(parent, text, cmd, *, fg_pair, text_pair, hover_pair=None,
                     border_pair=None, w=None, icon=None, transparent=False,
                     font_size=13, height=36, bold=False):
    """CTkButton 생성 공용 골격 — 아이콘이 있어도 버튼 1개(컨테이너 없음).

    이전 라운드는 아이콘+버튼을 tk.Frame 으로 감싸 나란히 배치했으나,
    8호 Phase 2 코더 C·E 실측: 기존 코드베이스가 널리 쓰는
    ``parent.winfo_children()[0].invoke()`` 패턴(버튼 팩토리 반환값이
    부모의 유일한 자식이라고 가정)이 컨테이너를 만나면 깨진다(컨테이너엔
    `.invoke()` 가 없음). 그래서 아이콘을 `icons.render_icon_image()` 로
    PIL 렌더 → `ctk.CTkImage` 로 만들어 CTkButton 자체의 `image=` 에 얹는다
    (`compound="left"` 로 이미지+텍스트 나란히) — 컨테이너 자체가 사라져
    "버튼 1개" 계약이 그대로 유지된다. 아이콘 렌더가 실패하면(PIL 없음·
    폰트 파일 없음 등) `image=None` 이 되어 텍스트만 있는 평범한 CTkButton
    으로 조용히 폴백한다(크래시 없음).

    `height`/`w`(너비 계산용) 는 항상 100% DPI 기준값으로 받아 여기서
    한 번에 `px()` 를 적용한다(9호 Phase 1) — 호출부(Btn/PrimaryBtn/...)
    는 신경 쓸 필요 없이 기존처럼 36/44 같은 기준값만 넘기면 된다.
    아이콘 이미지 크기(`icons.render_icon_image`)는 그 함수 내부에서
    자체적으로 스케일하므로 여기서 또 px() 하지 않는다(이중 확대 방지).
    """
    width_px = px(max(90, w * 9 + 24)) if w else px(120)
    icon_image = icons.render_icon_image(icon, size=16, color=text_pair) if icon else None
    kwargs = dict(text=text, command=cmd, corner_radius=px(8), height=px(height),
                  width=width_px, fg_color=("transparent" if transparent else fg_pair),
                  text_color=text_pair, hover=not transparent,
                  font=_ctk_font(font_size, bold=bold))
    if icon_image is not None:
        kwargs["image"] = icon_image
        kwargs["compound"] = "left"
    if hover_pair is not None and not transparent:
        kwargs["hover_color"] = hover_pair
    if border_pair is not None:
        kwargs["border_width"] = px(1)
        kwargs["border_color"] = border_pair

    btn = ctk.CTkButton(parent, **kwargs)
    return _CtkButtonAdapter(btn, btn, None)


def _flat_button(parent, text, cmd, bg, fg, active_bg=None, active_fg=None,
                  w=None, border_color=None):
    """색상 조합만 다른 플랫 버튼 4종의 공용 골격(hover 는 _lighten 기반) — tk 폴백."""
    active_bg = active_bg or _lighten(bg)
    active_fg = active_fg or fg
    kw = {}
    if w is not None:
        kw["width"] = w
    btn = tk.Button(parent, text=text, command=cmd, font=FB, fg=fg, bg=bg,
                     activebackground=active_bg, activeforeground=active_fg,
                     relief="flat", cursor="hand2", padx=SP["md"], pady=px(10),
                     bd=0, highlightthickness=(px(1) if border_color else 0),
                     highlightbackground=border_color or bg, **kw)
    btn.bind("<Enter>", lambda e: btn.config(bg=active_bg))
    btn.bind("<Leave>", lambda e: btn.config(bg=bg))
    return btn


def Btn(parent, text, cmd, w=14, color=None, icon=None):
    """기존 주 버튼 팩토리 — 시그니처 불변(icon= 만 추가), HAS_CTK 면 CTkButton.

    color 는 단일 hex(예: C["green"])로 넘어오던 기존 관례를 유지하되,
    CTk 경로에서는 `_single_to_pair` 로 팔레트 토큰이면 페어로 승격한다.
    """
    if HAS_CTK:
        fg_pair = _single_to_pair(color) if color else _pair("accent")
        return _ctk_flat_button(parent, text, cmd, fg_pair=fg_pair,
                                text_pair=_pair("on_accent"), w=w, icon=icon)
    bg = color or C["accent"]
    fg = C.get("on_accent", C["bg"])
    b = tk.Button(parent, text=text, command=cmd, font=FB,
                  fg=fg, bg=bg, activebackground=_lighten(bg),
                  activeforeground=fg, relief="flat", cursor="hand2",
                  width=w, padx=SP["md"], pady=px(10))
    b.bind("<Enter>", lambda e: b.config(bg=_lighten(bg)))
    b.bind("<Leave>", lambda e: b.config(bg=bg))
    return b


def PlainBtn(parent, text, cmd, w=14, color=None):
    """항상 순정 tk.Button — CTk 어댑터 계약이 안 맞는 자리를 위한 탈출구
    (설계서 지침: "계약이 까다로운 곳에서 쓸 수 있게"). Btn() 의 tk 폴백과
    완전히 동일한 구현.
    """
    bg = color or C["accent"]
    fg = C.get("on_accent", C["bg"])
    b = tk.Button(parent, text=text, command=cmd, font=FB,
                  fg=fg, bg=bg, activebackground=_lighten(bg),
                  activeforeground=fg, relief="flat", cursor="hand2",
                  width=w, padx=SP["md"], pady=px(10))
    b.bind("<Enter>", lambda e: b.config(bg=_lighten(bg)))
    b.bind("<Leave>", lambda e: b.config(bg=bg))
    return b


def GhostBtn(parent, text, cmd, w=12, icon=None):
    """기존 고스트 버튼 팩토리 — 시그니처 불변(icon= 만 추가)."""
    if HAS_CTK:
        return _ctk_flat_button(parent, text, cmd, fg_pair=_pair("card"),
                                text_pair=_pair("muted"), hover_pair=_pair("card_hover"),
                                border_pair=_pair("border"), w=w, icon=icon, font_size=13)
    return tk.Button(parent, text=text, command=cmd, font=FB,
                     fg=C["muted"], bg=C["card"],
                     activebackground=C["border"], activeforeground=C["text"],
                     relief="flat", cursor="hand2", width=w, padx=SP["md"], pady=px(9),
                     bd=px(1), highlightthickness=px(1), highlightbackground=C["border"])


def Ent(parent, var, show=None, w=28):
    """입력창 — HAS_CTK 면 CTkEntry(높이 36·radius 8) + 어댑터, 아니면 순정
    tk.Entry(8호 Phase 1 에서는 보류했으나, tests/test_gui_settings.py 의
    `highlightthickness` 단언은 FormField 인스턴스 자체(`naver_pw_entry[
    "widget"]`)에 대한 것이라 Ent 전환과 무관함을 재확인 — Phase 2 에서
    전환 진행).

    `["state"]`/`.get()`/`.insert()`/`.delete()`/`.focus_set()`/`show=`
    전부 어댑터가 보장한다(`_CtkEntryAdapter`).
    """
    if HAS_CTK:
        width_px = px(max(120, int(w) * 9))
        # fg_color 는 surface_2(surface 보다 카드 배경과 더 잘 구분되는
        # 톤) — 8호 Phase 2 디자이너 감사 🔴1: surface 는 card/bg 와 너무
        # 가까워(특히 라이트 팔레트에서 card=흰색 위) 입력창 경계가
        # 안 보였다. 전부 (light,dark) 페어.
        entry = ctk.CTkEntry(parent, textvariable=var, show=show,
                             width=width_px, height=px(36), corner_radius=px(8),
                             fg_color=_pair("surface_2"),
                             border_color=_pair("border_strong"),
                             text_color=_pair("text"),
                             placeholder_text_color=_pair("muted"),
                             font=_ctk_font(13))
        return _CtkEntryAdapter(entry)
    return tk.Entry(parent, textvariable=var, show=show, font=FB,
                    fg=C["text"], bg=C["surface_2"], insertbackground=C["accent"],
                    relief="flat", width=w, bd=0, highlightthickness=px(1),
                    highlightbackground=C["border_strong"], highlightcolor=C["focus"])


def PrimaryBtn(parent, text, cmd, w=None, icon=None, size: str = "md"):
    """accent 배경 주 버튼 — 화면당 1개 원칙(설계서 §4-1). icon= 은 선택.

    `size="lg"`(8호 Phase 2 캡처 실측 추가) — 히어로 버튼(홈 탭 큰 실행
    버튼 등) 전용: 높이 44·폰트 14·SemiBold(`FONT_FAMILY_BOLD` 있으면
    그 패밀리, 없으면 합성 bold). 기본값 "md" 는 기존과 완전히 동일
    (높이 36·폰트 13·normal)."""
    is_lg = size == "lg"
    if HAS_CTK:
        return _ctk_flat_button(parent, text, cmd, fg_pair=_pair("accent"),
                                text_pair=_pair("on_accent"),
                                hover_pair=_pair("accent_hover"), w=w, icon=icon,
                                height=(44 if is_lg else 36),
                                font_size=(14 if is_lg else 13), bold=is_lg)
    btn = _flat_button(parent, text, cmd, bg=C["accent"],
                       fg=C.get("on_accent", C["bg"]), w=w)
    if is_lg:
        btn.config(font=(FONT_FAMILY, 14, "bold"), pady=px(14))
    return btn


def SecondaryBtn(parent, text, cmd, w=None, icon=None):
    """btn_secondary 배경 보조 버튼 — 화면당 여러 개 허용."""
    if HAS_CTK:
        return _ctk_flat_button(parent, text, cmd, fg_pair=_pair("btn_secondary"),
                                text_pair=_pair("text"), hover_pair=_pair("card_hover"),
                                border_pair=_pair("border_strong"), w=w, icon=icon)
    return _flat_button(parent, text, cmd, bg=C["btn_secondary"], fg=C["text"],
                         active_bg=C["card_hover"], w=w, border_color=C["border_strong"])


def DangerBtn(parent, text, cmd, w=None, icon=None):
    """red 배경 — 삭제·전체 리프레시·중지 등 파괴적 동작 전용."""
    if HAS_CTK:
        return _ctk_flat_button(parent, text, cmd, fg_pair=_pair("red"),
                                text_pair=_pair("on_accent"), w=w, icon=icon)
    return _flat_button(parent, text, cmd, bg=C["red"],
                         fg=C.get("on_accent", C["bg"]), w=w)


def SubtleBtn(parent, text, cmd, icon=None):
    """텍스트 링크풍 버튼 — 밑줄 없음, 기본 muted → hover 시 text 로 밝아짐."""
    if HAS_CTK:
        return _ctk_flat_button(parent, text, cmd, fg_pair=_pair("muted"),
                                text_pair=_pair("muted"), icon=icon,
                                transparent=True, font_size=12)
    try:
        bg = parent.cget("bg")
    except tk.TclError:
        bg = C["bg"]
    btn = tk.Button(parent, text=text, command=cmd, font=FS, fg=C["muted"],
                     bg=bg, activebackground=bg, activeforeground=C["text"],
                     relief="flat", cursor="hand2", bd=0, highlightthickness=0,
                     padx=SP["xs"], pady=px(2))
    btn.bind("<Enter>", lambda e: btn.config(fg=C["text"]))
    btn.bind("<Leave>", lambda e: btn.config(fg=C["muted"]))
    return btn


_treeview_styled_for = None   # 마지막으로 스타일을 적용한 팔레트 이름(None=미적용)

def configure_treeview_style():
    """Treeview 공통 스타일 v3 (팔레트당 1회 적용) — 행높이 32·헤더 볼드·
    선택행 accent_soft·테두리 0.

    ⚠️ "헤더 uppercase"(디자인 시스템 v3 요청)는 ttk 스타일만으로 못 한다 —
    Tk 는 CSS text-transform 같은 게 없어서, 각 탭이 `tree.heading(col,
    text=...)` 를 호출할 때 텍스트 자체를 대문자로 넘겨야 실제로 적용된다
    (이 함수는 색·폰트·행높이만 담당). 헤더 폰트를 살짝 굵게+자간 느낌을
    주는 걸로 1차 근사한다.
    """
    global _treeview_styled_for
    if _treeview_styled_for == current_palette():
        return
    from tkinter import ttk
    sty = ttk.Style()
    sty.theme_use("clam")
    heading_font = (FS[0], FS[1], "bold")
    for name in ("App.Treeview", "Dark.Treeview", "KW.Treeview",
                 "CT.Treeview", "CH.Treeview"):
        sty.configure(name,
                       background=C["surface"], foreground=C["text"],
                       fieldbackground=C["surface"], borderwidth=0, rowheight=px(32))
        sty.configure(f"{name}.Heading",
                       background=C["card"], foreground=C["muted"],
                       borderwidth=0, font=heading_font)
        sty.map(name, background=[("selected", C["accent_soft"])],
                foreground=[("selected", C["text"])])
    _treeview_styled_for = current_palette()

def run_async(btn, task_fn, label=None):
    """버튼 비활성화 후 백그라운드 작업 실행, 완료 시 복원"""
    orig_text  = btn.cget("text")
    orig_state = btn.cget("state")
    btn.config(state="disabled", text=label or "처리 중...")
    def _wrapper():
        try:
            task_fn()
        finally:
            btn.after(0, lambda: btn.config(state="normal", text=orig_text))
    threading.Thread(target=_wrapper, daemon=True).start()

def bind_mousewheel(canvas):
    """canvas에 마우스 진입 시만 스크롤 바인딩 (탭 간 충돌 방지)"""
    def _on_wheel(e):
        canvas.yview_scroll(int(-1*(e.delta/120)), "units")
    canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", _on_wheel))
    canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
# ── 모션(9호 Phase 5, 2026-09-03) ──────────────────────
# 환경변수 NBA_NO_MOTION=1 이면 기본값 자체가 꺼진 채로 임포트된다(테스트·
# 저사양·접근성 스위치 — CI 는 대개 이 값을 안 주므로 기본은 True).
def _detect_motion_enabled() -> bool:
    """`MOTION_ENABLED` 기본값 계산 — `_detect_ui_scale()` 과 같은 패턴
    (함수로 분리해 테스트가 `monkeypatch.setenv` 로 재계산을 직접 검증할
    수 있게 한다). `NBA_NO_MOTION=1` 이면만 끈다 — 다른 값(빈 문자열
    포함)은 켜짐으로 취급한다."""
    return os.environ.get("NBA_NO_MOTION") != "1"


MOTION_ENABLED: bool = _detect_motion_enabled()

# widget -> 진행 중인 after id. widget 객체 자체를 키로 써서(제 identity
# 로) 참조가 살아있는 동안만 유효하고, <Destroy> 핸들러가 스스로 지운다
# (weakref 없이도 안전 — 아래 _on_animated_widget_destroy 참조).
_ANIMATIONS: Dict[Any, str] = {}
# <Destroy> 핸들러를 이미 걸어둔 위젯 집합 — animate() 가 같은 위젯에
# 여러 번 불려도 <Destroy> 바인딩이 중첩되지 않게 막는다.
_MOTION_DESTROY_BOUND: set = set()


def _cancel_animation(widget) -> None:
    """widget 에 예약된 after 콜백(있으면) 취소 — 재호출·destroy 양쪽에서 쓴다."""
    after_id = _ANIMATIONS.pop(widget, None)
    if after_id is not None:
        try:
            widget.after_cancel(after_id)
        except (tk.TclError, ValueError):
            pass


def _on_animated_widget_destroy(widget) -> None:
    _cancel_animation(widget)
    _MOTION_DESTROY_BOUND.discard(widget)


def animate(widget, steps: int, interval_ms: int,
            fn: "Callable[[int, int], None]",
            on_done: "Optional[Callable[[], None]]" = None) -> None:
    """`widget` 위에서 after 기반 계단식 애니메이션을 돌린다.

    `fn(step_index, total_steps)` 가 매 스텝(0-based `step_index`, 항상
    `steps` 그대로인 `total_steps`)마다 불린다 — 호출부가 이 두 값으로
    ease 곡선을 계산해 위치/alpha 를 정한다. 마지막 스텝이 끝나면
    `on_done()` 을 부른다(있으면).

    ● `MOTION_ENABLED` 가 False(또는 `NBA_NO_MOTION=1`)면 after 체인을
      아예 안 만들고 `fn(steps-1, steps)` 를 1회 즉시 호출 + `on_done()`
      으로 "최종 상태로 즉시 점프"한다 — 이 경로에서는 아래의 재호출·
      destroy 처리를 포함해 기존(모션 도입 전) 동작과 바이트 단위로
      동일하다(테스트 기본 전제).
    ● 같은 widget 에 다시 호출되면 진행 중이던 이전 체인을 취소하고
      새로 시작한다("재호출 시 재시작").
    ● widget 이 destroy 되면 예약된 after 콜백을 `<Destroy>` 바인딩이
      즉시 취소한다("invalid command name" 0건 — UiLifecycle 규약과
      같은 방향). 혹시 그 사이 콜백이 이미 발화 중이었다면 `fn` 안에서
      난 TclError 도 여기서 삼켜 체인을 조용히 멈춘다(이중 방어).
    """
    _cancel_animation(widget)
    if widget not in _MOTION_DESTROY_BOUND:
        _MOTION_DESTROY_BOUND.add(widget)
        try:
            widget.bind("<Destroy>",
                       lambda _e, w=widget: _on_animated_widget_destroy(w),
                       add="+")
        except tk.TclError:
            pass

    if not MOTION_ENABLED:
        try:
            fn(steps - 1, steps)
        except tk.TclError:
            return
        if on_done is not None:
            try:
                on_done()
            except tk.TclError:
                pass
        return

    state = {"step": 0}

    def _tick():
        i = state["step"]
        try:
            fn(i, steps)
        except tk.TclError:
            _ANIMATIONS.pop(widget, None)
            return
        state["step"] += 1
        if state["step"] >= steps:
            _ANIMATIONS.pop(widget, None)
            if on_done is not None:
                try:
                    on_done()
                except tk.TclError:
                    pass
            return
        try:
            after_id = widget.after(interval_ms, _tick)
        except tk.TclError:
            _ANIMATIONS.pop(widget, None)
            return
        _ANIMATIONS[widget] = after_id

    _tick()


def ease_out_quad(t: float) -> float:
    """2차 ease-out(감속) — `t` 는 0~1 진행률, 결과도 0~1. Toast 슬라이드·
    Toast/Popover/Tooltip 페이드가 공용으로 쓴다."""
    t = min(max(t, 0.0), 1.0)
    return 1.0 - (1.0 - t) ** 2
