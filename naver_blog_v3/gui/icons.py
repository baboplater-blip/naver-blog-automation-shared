# gui/icons.py — 아이콘 폰트 매핑 (신규, GUI 개편 auto-dev 8호 2026-09-03)
"""Segoe Fluent Icons(Win11)·Segoe MDL2 Assets(Win10 폴백) 글리프 이름→문자 매핑.

이모지 대신 시스템 아이콘 폰트를 쓰면 상용 서비스급 룩앤필에 가까워진다.
두 폰트 모두 마이크로소프트 PUA(Private Use Area) 코드포인트를 공유하도록
설계돼 있어(Fluent Icons 는 MDL2 Assets 의 상위 호환) 같은 코드포인트로
Win10/11 양쪽을 커버한다. 어느 쪽도 없는 환경(비-Windows 등)에서는
`has_icon_font()` 가 False 를 반환하고, `glyph()` 는 텍스트 폴백 "•" 를
돌려준다 — 호출부는 아이콘 유무를 신경 쓰지 않고 항상 `glyph()`/`icon_font()`
만 쓰면 된다.

코드포인트는 `docs/auto-dev/gui-design-system-v3.md` §6(디자이너 공식
글리프 맵)과 대조해 맞췄다 — "확정"(Home·Settings·Camera 등)은 신뢰도가
높고, "확인 필요"로 표시된 항목(Shop·Picture·Tag·AreaChart·Copy·Ringer·
Bookmark·History 등)은 문서 권장값을 그대로 채택했지만 실제 렌더 스팟
체크(문자표 등)는 아직 안 했다 — 틀렸다면 문자만 교체하면 된다. "발행"
전용 글리프는 의도적으로 없다(Play 재사용, §6-4). "중지"는 문서가 아이콘
대신 색 도형(빨강 사각형)을 권장하지만, 그 방식은 `glyph(name)->str` 계약과
안 맞아(문자가 아니라 위젯이 필요) 이번 라운드는 보류 — `stop` 키는 여전히
후보 글리프(E71A)를 쓴다.

폰트 탐지는 **살아있는 Tk 루트가 있을 때만** 가능하다(tkinter.font.families()
는 Tcl 인터프리터가 필요) — 루트가 아직 없으면 `has_icon_font()`/`glyph()`
는 "아직 모름" 상태를 텍스트 폴백으로 안전하게 처리하고, 루트가 생긴 뒤
다시 호출하면 정확한 값을 캐싱한다(임시 루트를 만들었다 부수는 방식은
`tkinter._default_root` 오염 위험이 있어 쓰지 않는다 — 2026-09-03 실측 확인,
루트 없이 tk.StringVar() 를 만들면 즉시 RuntimeError).
"""
from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

# ── 고DPI 스케일(9호 Phase 1, 2026-09-03) ────────────────────────────
# icons.py 는 theme.py 가 임포트하는 쪽이라(순환 임포트 방지) theme.UI_SCALE
# 을 직접 참조할 수 없다 — 대신 theme.py 가 자기 UI_SCALE 을 계산한 뒤
# (모듈 임포트 시점 1회) `icons.set_ui_scale()` 을 호출해 값을 밀어준다
# (`theme.set_ui_scale()` 도 재호출 시 같이 동기화). 리스너 등록이 아니라
# 단발성 값 전달이라 순환 위험이 없다.
_UI_SCALE = 1.0


def set_ui_scale(factor: float) -> None:
    """`theme.set_ui_scale()` 이 호출한다 — icons 모듈 자체 배율만 갱신
    (SP/R 같은 재계산 대상 dict 는 icons.py 엔 없다)."""
    global _UI_SCALE
    _UI_SCALE = float(factor) if factor and factor > 0 else 1.0


def _px(n: float) -> int:
    """`theme.px()` 와 동일 공식 — icons.py 는 독립 모듈이라 자체 사본을
    둔다(순환 임포트 회피, 위 주석 참조)."""
    return round(n * _UI_SCALE)


try:  # CTkImage 생성용 — icons.py 는 theme.py 가 임포트하므로(순환 방지)
    # customtkinter 를 여기서 독립적으로(theme.py 를 거치지 않고) 감지한다.
    import customtkinter as _ctk
    _HAS_CTK = True
except Exception:  # pragma: no cover — customtkinter 미설치 환경 폴백
    _ctk = None
    _HAS_CTK = False

# 우선순위: Fluent Icons(Win11) → MDL2 Assets(Win10 폴백) → 없음.
_ICON_FONT_CANDIDATES: Tuple[str, ...] = ("Segoe Fluent Icons", "Segoe MDL2 Assets")

# 아이콘 폰트 이름 → 실제 TTF 파일 후보(PIL 렌더링용 — tkinter.font 는 이름만
# 알지 파일 경로를 안 준다). 2026-09-03 이 개발 PC 실측: 두 파일 모두 존재
# (`C:/Windows/Fonts/SegoeIcons.ttf`·`segmdl2.ttf`). 못 찾으면 CTkButton
# 아이콘 이미지 합성을 포기하고 텍스트만 쓰는 버튼으로 자연 폴백한다.
_ICON_FONT_FILES: Dict[str, Tuple[str, ...]] = {
    "Segoe Fluent Icons": ("C:/Windows/Fonts/SegoeIcons.ttf",),
    "Segoe MDL2 Assets": ("C:/Windows/Fonts/segmdl2.ttf", "C:/Windows/Fonts/SegMDL2.ttf"),
}

_TEXT_FALLBACK = "•"

_cached_family: Optional[str] = None
_cache_valid = False

_font_file_cache: Optional[str] = None
_font_file_cache_valid = False

# (name, size, light_hex, dark_hex) -> (light_PIL.Image, dark_PIL.Image).
# **CTkImage 완성품이 아니라 원본 PIL 이미지만** 캐싱한다 — CTkImage 는
# 내부적으로 ImageTk.PhotoImage 를 지연 생성해 캐싱하는데(customtkinter
# ctk_image.py `_scaled_*_photo_images`), 그 PhotoImage 는 "생성 당시의"
# Tcl 인터프리터(tk 루트)에 묶인다. CTkImage 객체 자체를 모듈 전역에 캐싱해
# 여러 테스트(루트를 매번 새로 만들고 부수는)에 걸쳐 재사용하면, 먼저 죽은
# 루트에서 만들어진 PhotoImage 를 나중 루트가 다시 쓰려다
# `_tkinter.TclError: image "pyimage9" doesn't exist` 로 죽는다(2026-09-03
# 실측 회귀 — 코더 C·E 실측 반영 작업 중 발견). PIL 이미지 자체는 순수
# 파이썬 데이터라 Tcl 인터프리터와 무관 → 이건 캐싱해도 안전. `CTkImage`
# 래퍼는 호출마다 새로 만들어(비용은 dict 생성 수준, PIL 재렌더는 없음)
# 항상 "현재" 루트에 새 PhotoImage 가 생기게 한다.
_image_cache: Dict[Tuple[str, int, str, str], Any] = {}


def _detect_icon_font_family() -> Optional[str]:
    """설치된 아이콘 폰트 이름(우선순위 첫 매치) 또는 None.

    Tk 루트가 아직 없으면 캐시하지 않고 None 을 돌려준다 — 루트가 생긴 뒤
    다시 호출되면 그때 정확히 감지해 캐싱한다.
    """
    global _cached_family, _cache_valid
    if _cache_valid:
        return _cached_family
    root = tk._default_root  # noqa: SLF001 — tkinter 표준 관례(다른 모듈도 참조)
    if root is None:
        return None
    try:
        import tkinter.font as tkfont
        families = set(tkfont.families(root))
    except tk.TclError as exc:
        log.debug("[icons] 폰트 목록 조회 실패: %s", exc, exc_info=True)
        _cached_family = None
        _cache_valid = True
        return None
    _cached_family = next((c for c in _ICON_FONT_CANDIDATES if c in families), None)
    _cache_valid = True
    return _cached_family


def has_icon_font() -> bool:
    """아이콘 폰트(Fluent Icons 또는 MDL2 Assets)가 이 환경에 있는지."""
    return _detect_icon_font_family() is not None


def icon_font(size: int = 16):
    """(family, px(size)) 튜플 — tk.Label/CTkLabel/CTkButton 의 font= 어디든
    쓸 수 있다. `size` 는 설계 기준(100% DPI) 값 — 실제 반환 크기는
    `_px()` 로 `_UI_SCALE` 을 적용한 값이다(9호 Phase 1).

    아이콘 폰트가 없으면 None — 호출부는 `has_icon_font()` 로 먼저 걸러
    쓰거나, None 이면 본문 폰트로 대체해야 한다.
    """
    family = _detect_icon_font_family()
    if family is None:
        return None
    return (family, _px(size))


def _icon_font_file_path() -> Optional[str]:
    """감지된 아이콘 폰트 이름에 대응하는 실제 TTF 파일 경로(있으면).

    `_detect_icon_font_family()` 와 같은 "루트 없으면 캐시 안 함" 원칙을
    따른다 — 가족 감지가 아직 안 됐으면(None) 이쪽도 미확정으로 둔다.
    """
    global _font_file_cache, _font_file_cache_valid
    if _font_file_cache_valid:
        return _font_file_cache
    family = _detect_icon_font_family()
    if family is None:
        return None
    for candidate in _ICON_FONT_FILES.get(family, ()):
        if Path(candidate).exists():
            _font_file_cache = candidate
            _font_file_cache_valid = True
            return candidate
    _font_file_cache = None
    _font_file_cache_valid = True
    return None


def _draw_glyph_image(glyph_char: str, font_path: str, size: int, color_hex: str):
    """PUA 글리프 1개를 (size x size) 투명 RGBA PIL 이미지 중앙에 그린다."""
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(font_path, max(1, int(size * 0.82)))
    bbox = draw.textbbox((0, 0), glyph_char, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (size - text_w) / 2 - bbox[0]
    y = (size - text_h) / 2 - bbox[1]
    draw.text((x, y), glyph_char, font=font, fill=color_hex)
    return img


def render_icon_image(name: str, size: int = 16, color="#e8ecf4"):
    """PUA 글리프를 PIL 로 렌더해 `ctk.CTkImage` 로 반환 — `CTkButton(image=)`
    자리에 바로 꽂으면 아이콘+텍스트가 버튼 하나에 합쳐진다(8호 Phase 2,
    코더 C·E 실측 반영: 아이콘 라벨+버튼을 감싸는 별도 컨테이너를 쓰면
    `parent.winfo_children()[0].invoke()` 같은 기존 "버튼 1개" 계약이
    깨진다 — 컨테이너 자체를 없애는 이 방식이 가장 안전).

    `color` 는 단일 hex 문자열이거나 (light_hex, dark_hex) 페어 — 페어면
    라이트/다크 두 장을 각각 그려 `CTkImage(light_image=, dark_image=)` 로
    넘긴다. 그러면 `ctk.set_appearance_mode()` 전환 시 CTk 가 알맞은 쪽을
    자동으로 그려(위젯 트리 재순회 없이) 아이콘 색도 라이트/다크에 맞게
    바뀐다.

    PIL 미설치·CTk 미설치·아이콘 폰트(파일) 없음·모르는 이름 중 하나라도
    해당하면 None — 호출부는 `image=None` 이면 자연히 텍스트만 있는
    CTkButton 이 된다(크래시 없음, 실패는 항상 조용한 폴백).

    `size` 는 설계 기준(100% DPI) 값 — 실제 렌더·캐시·CTkImage 크기는
    `_px()` 로 `_UI_SCALE` 을 적용한 값이다(9호 Phase 1)."""
    if not _HAS_CTK or _ctk is None:
        return None
    glyph_char = ICONS.get(name)
    if not glyph_char or not has_icon_font():
        return None
    font_path = _icon_font_file_path()
    if not font_path:
        return None
    size = _px(size)
    if isinstance(color, (tuple, list)) and len(color) == 2:
        light_hex, dark_hex = color
    else:
        light_hex = dark_hex = color
    cache_key = (name, size, str(light_hex), str(dark_hex))
    pil_pair = _image_cache.get(cache_key)
    if pil_pair is None:
        try:
            light_img = _draw_glyph_image(glyph_char, font_path, size, light_hex)
            dark_img = _draw_glyph_image(glyph_char, font_path, size, dark_hex)
        except Exception as exc:  # pragma: no cover — PIL/폰트 파일 손상 등 방어
            log.debug("[icons] 글리프 렌더 실패(%s): %s", name, exc, exc_info=True)
            return None
        pil_pair = (light_img, dark_img)
        _image_cache[cache_key] = pil_pair
    light_img, dark_img = pil_pair
    try:
        # CTkImage 래퍼는 매번 새로 만든다 — 이유는 위 _image_cache 정의부
        # docstring 참조(내부 PhotoImage 가 "현재" Tcl 루트에 새로 생겨야 함).
        return _ctk.CTkImage(light_image=light_img, dark_image=dark_img, size=(size, size))
    except Exception as exc:  # pragma: no cover
        log.debug("[icons] CTkImage 생성 실패(%s): %s", name, exc, exc_info=True)
        return None


def glyph(name: str) -> str:
    """아이콘 이름 → 글리프 문자. 폰트가 없거나 이름을 모르면 "•" 폴백.

    폰트가 없으면 코드포인트를 그대로 반환해봐야 tofu(빈 네모)만 보이므로,
    이름을 알아도(=ICONS 에 있어도) 무조건 텍스트 폴백을 돌려준다.
    """
    if not has_icon_font():
        return _TEXT_FALLBACK
    return ICONS.get(name, _TEXT_FALLBACK)


def __getattr__(name: str):
    """`icons.ICON_FONT_FAMILY` 를 매 접근마다 재감지되는 값으로 노출(PEP 562).

    모듈 임포트 시점에 고정하면 "아직 루트가 없어 None" 이 영원히 굳어버리는
    문제가 있어, 속성 접근 자체를 함수 호출로 위임한다.
    """
    if name == "ICON_FONT_FAMILY":
        return _detect_icon_font_family()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# key -> 코드포인트(문자열 이스케이프). 탭 14개 + 액션 20개 = 34개.
ICONS: Dict[str, str] = {
    # ── 탭(14) ──────────────────────────────────────────
    "home": "",       # Home
    "posting": "",    # Play — 발행·진행 탭(전용 글리프 대신 재사용, 설계 v3 §6-4)
    "settings": "",   # Settings(톱니)
    "photo": "",      # Camera
    "product": "",    # Shop
    "image": "",      # Picture
    "content": "",    # Refresh — 다시쓰기·시리즈(의미상 자연히 일치, 설계 v3 §6-3)
    "keyword": "",    # Tag
    "stats": "",      # Chart(막대) — 대시보드/통계
    "growth": "",     # Up(상승 화살표) — 성장 관제탑
    "trend": "",      # Copy(중복 비교) — 설계 v3 §6-3 "확인 권장"
    "channel": "",    # Ringer(종) — 알림·카페, 설계 v3 §6-3 "확인 권장"
    "profile": "",    # Bookmark — 기존 "🔖" 관용 계승, 설계 v3 §6-3
    "log": "",        # History — 설계 v3 §6-3 "확인 권장"

    # ── 액션(20) ────────────────────────────────────────
    "run": "",        # Play
    "stop": "",       # Stop
    "save": "",       # Save
    "refresh": "",    # Refresh
    "search": "",     # Search
    "folder": "",     # Folder
    "publish": "",    # Play(전용 "발행" 글리프 없음 — 설계 v3 §6-4 핵심 결정 1)
    "test": "",       # Test 류(근사)
    "add": "",        # Add(+)
    "delete": "",     # Delete(휴지통)
    "help": "",       # Help(물음표)
    "close": "",      # Cancel(X)
    "expand": "",     # ChevronDown
    "collapse": "",   # ChevronUp
    "theme": "",      # Contrast/Brightness
    "warning": "",    # Warning(느낌표 삼각형)
    "success": "",    # CheckMark
    "info": "",       # Info(원)
    "clock": "",      # Clock/Recent
    "user": "",       # Person(단일 사용자, profile 과 구분)
    "view": "",       # View(눈) — FormField 비밀번호 표시/숨김 토글 전용(8호 Phase 2)
    "remove": "",     # Remove(빼기, MDL2 E738) — FormField spin "-" 전용(8호 Phase 2)
}
