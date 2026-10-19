# core/thumbnail_compose.py
# 🖼 썸네일 한글 **로컬 합성** — 배경은 생성 엔진, 글자는 우리가 정확히 그린다 (2026-09-18)
# ─────────────────────────────────────────────────────────────────────────────
# 이 모듈이 존재하는 이유
#   썸네일 문구를 이미지 생성 모델에게 "정확히 새겨 달라"고 맡기면 한글이 깨지거나 철자가
#   틀리고, 그걸 잡으려고 편당 비전(OCR) 검증을 한 번 더 돌려야 했다. 배경(글자 없는
#   이미지)만 모델에게 받고 **한글은 Pillow 로 직접 합성**하면:
#     · **모델이 글자를 잘못 새기는 오류**가 사라진다 — 받은 문구를 그대로 렌더한다.
#       ⚠️ 문구 자체가 틀렸으면(브리프·제목의 오타) 그대로 틀리게 나오고, 고른 글꼴이
#       해당 글자의 글리프를 갖고 있지 않으면 □ 로 나올 수 있다(아래 `_renders_hangul`
#       은 '한글' 샘플로만 검사한다). 합성은 문구의 사실·맞춤법을 검사하지 않는다.
#     · 비전 검증 호출이 사라진다(검증할 '모델이 쓴 글자'가 없다)
#     · 계정별 색·메시지를 우리가 통제한다
#
# 이 모듈의 계약
#   · **원본 배경을 절대 덮어쓰지 않는다** — 항상 새 파일로 저장한다.
#   · 합성 결과 PNG 에 마커를 남겨 **같은 파일에 두 번 합성하지 않는다**.
#   · 한글 폰트를 못 찾거나 합성이 실패하면 **성공으로 속이지 않는다** — ok=False 와
#     사람이 조치할 수 있는 사유를 돌려준다(호출자가 그 슬롯을 미완으로 처리한다).
#   · OS 에 설치된 폰트만 **허용 목록**으로 찾는다. 폰트를 내려받지 않는다.
#
# 관련 모듈
#   `core/text_overlay.py` 는 FLUX 일러스트용 teal 고정 팔레트 카드(배지·칩·Q&A)를 그리는
#   기존 경로다. 이 모듈은 계정 색을 받는 **단일 메시지 커버** 전용이라 따로 두었다.
from __future__ import annotations

import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from config.logger import get_logger
    log = get_logger(__name__)
except Exception:  # noqa: BLE001
    import logging
    log = logging.getLogger(__name__)


# 합성 완료 마커 — PNG 텍스트 청크에 남긴다(파일명 규칙에 의존하지 않는다).
MARKER_KEY: str = "nba_thumb_composed"
MARKER_VALUE: str = "1"
MARKER_MESSAGE_KEY: str = "nba_thumb_message"


def square_canvas_size(width: Any, height: Any) -> Tuple[int, int]:
    """Build a square legacy thumbnail canvas from stored width/height preferences."""
    side = max(int(width), int(height), 1)
    return side, side

# 환경 변수로 폰트를 직접 지정할 수 있다(설치 경로가 특이한 환경의 탈출구).
FONT_ENV: str = "NBA_THUMB_FONT"

# OS 한글 폰트 허용 목록 — 굵은 글꼴 우선(작게 보일 때 가독성이 결정된다).
# ⚠️ 여기 없는 폰트를 코드가 내려받지 않는다(요건: 폰트 임의 다운로드 금지).
_FONT_CANDIDATES: Tuple[str, ...] = (
    # Windows
    "C:/Windows/Fonts/NotoSansKR-VF.ttf",
    "C:/Windows/Fonts/malgunbd.ttf",
    "C:/Windows/Fonts/malgun.ttf",
    "C:/Windows/Fonts/NanumGothicBold.ttf",
    "C:/Windows/Fonts/NanumGothic.ttf",
    "C:/Windows/Fonts/NanumBarunGothicBold.ttf",
    "C:/Windows/Fonts/gulim.ttc",
    "C:/Windows/Fonts/batang.ttc",
    # Linux
    "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJKkr-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    # macOS
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/Library/Fonts/AppleGothic.ttf",
)

# 레이아웃 비율(캔버스 폭 기준) — 1:1 정사각형에서 목록 썸네일 크기로 줄여도 읽히게.
_MARGIN_RATIO = 0.085           # 좌우 여백
_BOTTOM_PAD_RATIO = 0.085       # 아래 여백
_SCRIM_RATIO = 0.56             # 하단 가독성 밴드 높이
_ACCENT_W_RATIO = 0.11          # 강조 바 길이
_ACCENT_H_RATIO = 0.018         # 강조 바 두께
_NOTE_SIZE_RATIO = 0.042        # 보조 문구 크기
_START_SIZE_RATIO = 0.12        # 메시지 시작 글자 크기
_MIN_SIZE_RATIO = 0.055         # 더 줄이지 않는 하한(이보다 작으면 못 읽는다)
_LINE_HEIGHT = 1.22
_MAX_LINES = 3
# 글자 블록이 차지할 수 있는 최대 높이(캔버스 높이 대비). 가독성 밴드가 이 블록을 덮으므로
# 이 값이 곧 "배경이 얼마나 보이는가"를 정한다 — 글자를 키우는 것보다 여백이 우선이다.
_TEXT_BLOCK_MAX_RATIO = 0.28
_ACCENT_TERM_RE = re.compile(
    r"\d[\d,]*(?:\s?(?:만원|천원|원|억원|%|퍼센트|개월|주|일|시간|명|개|회))?"
    r"|지원|환급|혜택|수급|보조|장려|급여|감면|할인|신청"
)

DEFAULT_CANVAS: int = 1024

_WS_RE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────────────────────────────
# 색 · 폰트
# ─────────────────────────────────────────────────────────────────────────────
def hex_to_rgb(value: Any, default: Tuple[int, int, int] = (18, 33, 63)) -> Tuple[int, int, int]:
    """`#rrggbb`/`#rgb` → (r,g,b). 잘못된 값이면 기본값(합성이 색 때문에 깨지지 않게)."""
    text = str(value or "").strip().lstrip("#")
    try:
        if len(text) == 3:
            return tuple(int(c * 2, 16) for c in text)  # type: ignore[return-value]
        if len(text) == 6:
            return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))
    except ValueError:
        pass
    return default


def _renders_hangul(font: Any) -> bool:
    """이 폰트가 한글 글리프를 실제로 갖고 있는지 확인한다.

    경로가 존재한다는 사실만으로는 부족하다 — 라틴 전용 폰트를 잡으면 한글이 두부(□)로
    나오고, 그건 "합성 성공"으로 보고돼서는 안 되는 실패다.
    """
    try:
        mask = font.getmask("한글")
        return bool(mask) and mask.getbbox() is not None
    except Exception:  # noqa: BLE001 — 판정 불가는 미지원으로(보수적)
        return False


def find_font(size: int) -> Tuple[Optional[Any], str]:
    """한글을 그릴 수 있는 폰트를 허용 목록에서 찾는다.

    Returns:
        (font, source). font 가 None 이면 이 환경에 한글 폰트가 없다는 뜻이고, 호출자는
        **실패로 보고해야 한다**(글자 없는 배경을 완성 썸네일로 넘기지 않는다).
    """
    try:
        from PIL import ImageFont
    except Exception as exc:  # noqa: BLE001 — Pillow 부재(이 저장소에선 필수 의존성)
        log.warning(f"[thumbnail_compose] Pillow 로드 실패({type(exc).__name__})")
        return None, ""

    size = max(8, int(size))
    candidates: List[str] = []
    override = os.environ.get(FONT_ENV, "").strip()
    if override:
        candidates.append(override)
    candidates.extend(_FONT_CANDIDATES)

    for raw in candidates:
        try:
            if not raw or not Path(raw).is_file():
                continue
            font = ImageFont.truetype(raw, size)
        except (OSError, ValueError):
            continue
        # 가변 폰트(Noto VF)는 기본 축이 Thin 이라 굵기를 명시한다 — 실패해도 진행.
        try:
            font.set_variation_by_axes([800])
        except (OSError, ValueError, AttributeError):
            pass
        if _renders_hangul(font):
            return font, raw
    log.warning("[thumbnail_compose] 한글 폰트를 찾지 못했다 — 썸네일 합성 불가")
    return None, ""


# ─────────────────────────────────────────────────────────────────────────────
# 마커(중복 합성 방지)
# ─────────────────────────────────────────────────────────────────────────────
def is_composed(png_path: Any) -> bool:
    """이 파일이 이미 우리 합성을 거친 썸네일인지(PNG 텍스트 마커로 판정)."""
    try:
        from PIL import Image
        with Image.open(str(png_path)) as img:
            return str(img.info.get(MARKER_KEY) or "") == MARKER_VALUE
    except Exception:  # noqa: BLE001 — 못 읽으면 '아직 아니다'로 본다(합성 시도 허용)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# 텍스트 배치(줄바꿈 · 자동 크기)
# ─────────────────────────────────────────────────────────────────────────────
def _wrap(draw: Any, text: str, font: Any, max_w: int, max_lines: int) -> Optional[List[str]]:
    """어절 우선 줄바꿈. `max_lines` 안에 다 담기지 못하면 None(호출자가 글자를 줄인다).

    한 어절이 한 줄보다 길면 그 어절만 글자 단위로 쪼갠다(한글은 어절이 길 수 있다).
    """
    words = [w for w in str(text or "").split(" ") if w]
    if not words:
        return None
    lines: List[str] = []
    current = ""

    def _flush() -> None:
        nonlocal current
        if current:
            lines.append(current)
            current = ""

    for word in words:
        trial = f"{current} {word}".strip()
        # ⚠️ 첫 어절도 반드시 폭을 검사한다 — "current 가 비었으면 통과" 로 두면 한 줄보다
        #    긴 어절(공백 없는 긴 한글 제목)이 검사 없이 통째로 들어가 밖으로 삐져나간다.
        if draw.textlength(trial, font=font) <= max_w:
            current = trial
            continue
        _flush()
        if draw.textlength(word, font=font) <= max_w:
            current = word
            continue
        # 어절 자체가 한 줄보다 길다 — 그 어절만 글자 단위로 나눈다.
        for ch in word:
            if draw.textlength(current + ch, font=font) <= max_w:
                current += ch
                continue
            _flush()
            current = ch
            if len(lines) > max_lines:
                return None
    _flush()
    if not lines or len(lines) > max_lines:
        return None
    return lines


def _fit_message(draw: Any, message: str, *, canvas: int,
                 max_w: int) -> Tuple[Optional[Any], List[str], int, str]:
    """메시지가 `max_lines` 안에 들어가는 **가장 큰** 글자 크기를 찾는다.

    하한까지 줄여도 안 들어가면 메시지를 자르지 않고 실패한다. 확정 문구가 온전히
    들어가지 않을 때 성공처럼 보이는 결과를 만들지 않는다.
    """
    start = max(10, int(canvas * _START_SIZE_RATIO))
    floor = max(9, int(canvas * _MIN_SIZE_RATIO))
    block_budget = int(canvas * _TEXT_BLOCK_MAX_RATIO)
    source = ""
    font = None
    for size in range(start, floor - 1, -2):
        font, source = find_font(size)
        if font is None:
            return None, [], 0, ""
        lines = _wrap(draw, message, font, max_w, _MAX_LINES)
        # 줄 수만 맞아도 블록이 예산을 넘으면 더 줄인다(배경을 다 덮지 않게).
        if lines and len(lines) * int(size * _LINE_HEIGHT) <= block_budget:
            return font, lines, size, source

    # 하한에서 말줄임 — 어절 경계로 담고 넘치는 마지막 줄만 '…' 처리.
    font, source = find_font(floor)
    if font is None:
        return None, [], 0, ""
    lines: List[str] = []
    current = ""
    for ch in str(message):
        if draw.textlength(current + ch, font=font) <= max_w:
            current += ch
            continue
        lines.append(current)
        current = ch
        if len(lines) == _MAX_LINES:
            break
    if len(lines) < _MAX_LINES and current:
        lines.append(current)
    # Never cut a supplied amount, unit, or question into an ellipsis. Leave the
    # thumbnail pending when the confirmed message cannot fit at a readable size.
    return None, [], 0, source


# ─────────────────────────────────────────────────────────────────────────────
# 공개 진입점
# ─────────────────────────────────────────────────────────────────────────────
def compose(base_png: Any, out_png: Any, *, message: str,
            style: Optional[Dict[str, Any]] = None,
            note: str = "", cta: str = "", category: str = "",
            date_label: str = "", canvas: Optional[int] = DEFAULT_CANVAS) -> Dict[str, Any]:
    """글자 없는 배경(`base_png`) 위에 한글 메시지를 합성해 `out_png` 로 저장한다.

    Args:
        message: 썸네일에 얹을 짧은 한글 메시지(편집 브리프가 정한 값).
        style: `core.editorial_profile.thumbnail_style()` 형식 — bg/accent/text 색.
        note: 메시지 아래 작게 넣을 보조 문구(하위 호환용).
        cta: 브리프에서 가져온 짧은 행동 문구. 비어 있으면 중립 CTA를 쓴다.
        category: 글 카테고리에서 가져온 일반 주제 배지(공식 인증 문구는 허용하지 않음).
        date_label: 검증된 제작월 표시(형식: YYYY.MM).
        canvas: 정사각형 한 변 픽셀. None 이면 원본 크기를 그대로 쓴다.

    Returns:
        {"ok": bool, "path": str|None, "reason": str, "font": str, "font_size": int,
         "lines": list[str], "skipped": str}

        · ok=True, skipped="already_composed" → 이미 합성된 파일이라 **다시 하지 않았다**.
        · ok=False → 사람이 조치할 사유가 `reason` 에 있다(`font_missing` 등).
          이 경우 `path` 는 None 이며, 호출자는 **완성으로 세면 안 된다.**
    """
    result: Dict[str, Any] = {"ok": False, "path": None, "reason": "", "font": "",
                              "font_size": 0, "lines": [], "skipped": ""}
    base = Path(str(base_png or ""))
    out = Path(str(out_png or ""))

    text = _WS_RE.sub(" ", str(message or "")).strip()
    if not text:
        result["reason"] = "message_empty"
        return result
    if not base.is_file():
        result["reason"] = "base_missing"
        return result

    # 이미 우리 손을 거친 썸네일이면 다시 합성하지 않는다(요건: 반복 합성 금지).
    if is_composed(base):
        result.update(ok=True, path=str(base), skipped="already_composed")
        return result

    try:
        from PIL import Image, ImageDraw, ImageFilter
        from PIL import PngImagePlugin
    except Exception as exc:  # noqa: BLE001
        result["reason"] = f"pillow_unavailable:{type(exc).__name__}"
        return result

    try:
        with Image.open(str(base)) as raw:
            img = raw.convert("RGBA")
    except Exception as exc:  # noqa: BLE001 — 깨진/미지원 파일
        log.warning(f"[thumbnail_compose] 배경을 열 수 없다({type(exc).__name__}): {base.name}")
        result["reason"] = f"base_unreadable:{type(exc).__name__}"
        return result

    try:
        side = int(canvas) if canvas else max(img.width, img.height)
        side = max(256, side)
        if img.width != side or img.height != side:
            img = _cover_square(img, side, Image)

        st = style or {}
        bg = hex_to_rgb(st.get("bg"), (18, 33, 63))
        accent = hex_to_rgb(st.get("accent"), (231, 182, 75))
        fg = hex_to_rgb(st.get("text"), (247, 247, 242))

        margin = int(side * _MARGIN_RATIO)
        max_w = side - 2 * margin
        draw = ImageDraw.Draw(img)

        font, lines, size, font_src = _fit_message(draw, text, canvas=side, max_w=max_w)
        if font is None:
            result["reason"] = "font_missing" if not font_src else "message_too_long"
            return result

        line_h = int(size * _LINE_HEIGHT)
        note_text = _WS_RE.sub(" ", str(cta or note or "자세히 보기")).strip()
        note_font = None
        note_h = 0
        if note_text:
            note_font, _src = find_font(max(9, int(side * _NOTE_SIZE_RATIO)))
            if note_font is not None:
                note_h = int(side * _NOTE_SIZE_RATIO * 1.9)

        bottom = side - int(side * _BOTTOM_PAD_RATIO)
        text_bottom = bottom - note_h
        text_top = text_bottom - line_h * len(lines)

        # 강조 바 — 메시지 위에 짧게(계정 강조색이 여기서만 쓰인다).
        bar_h = max(3, int(side * _ACCENT_H_RATIO))
        bar_y = text_top - int(side * 0.045)

        # ⚠️ 가독성 밴드는 **글자 블록을 확보한 뒤** 그린다. 고정 높이로 먼저 깔면 줄 수가
        #    많은 메시지의 첫 줄이 밴드 위(반투명 구간)로 올라가 밝은 배경에서 읽히지 않는다.
        _draw_scrim(img, bg, side, solid_from_y=bar_y - int(side * 0.025),
                    Image=Image, ImageDraw=ImageDraw)

        ImageDraw.Draw(img).rounded_rectangle(
            [margin, bar_y, margin + int(side * _ACCENT_W_RATIO), bar_y + bar_h],
            radius=bar_h // 2, fill=accent + (255,))

        y = text_top
        for line in lines:
            _draw_accent_line(img, (margin, y), line, font, fg, accent,
                              Image, ImageDraw, ImageFilter)
            y += line_h

        if note_text and note_font is not None:
            ImageDraw.Draw(img).text((margin, text_bottom + int(side * 0.012)),
                                     note_text, font=note_font, fill=fg + (200,))

        _draw_category_badge(img, category, accent, side, Image, ImageDraw, ImageFilter)
        safe_date = str(date_label or "").strip()
        if re.fullmatch(r"20\d{2}\.\d{2}", safe_date):
            date_font, _date_source = find_font(max(12, int(side * 0.04)))
            if date_font is not None:
                date_draw = ImageDraw.Draw(img)
                date_width = date_draw.textlength(safe_date, font=date_font)
                _shadow_text(img, (side - margin - int(date_width), margin // 2), safe_date,
                             date_font, fg, Image, ImageDraw, ImageFilter)

        out.parent.mkdir(parents=True, exist_ok=True)
        meta = PngImagePlugin.PngInfo()
        meta.add_text(MARKER_KEY, MARKER_VALUE)
        meta.add_text(MARKER_MESSAGE_KEY, text[:80])
        img.convert("RGB").save(str(out), "PNG", pnginfo=meta)
    except Exception as exc:  # noqa: BLE001 — 합성 실패는 정직하게 실패로 보고
        log.warning(f"[thumbnail_compose] 합성 실패({type(exc).__name__}): {exc}")
        result["reason"] = f"compose_error:{type(exc).__name__}"
        return result

    result.update(ok=True, path=str(out), font=font_src, font_size=size, lines=list(lines))
    log.info("[thumbnail_compose] 썸네일 합성 완료 - %d자 %d줄 %dpx (%s)",
             len(text), len(lines), size, Path(font_src).name if font_src else "?")
    return result


def _draw_accent_line(img: Any, xy: Tuple[int, int], text: str, font: Any,
                      foreground: Tuple[int, int, int], accent: Tuple[int, int, int],
                      Image: Any, ImageDraw: Any, ImageFilter: Any) -> None:
    """Keep the full headline intact while coloring source-backed amounts/benefit terms."""
    _shadow_text(img, xy, text, font, foreground, Image, ImageDraw, ImageFilter)
    drawer = ImageDraw.Draw(img)
    for match in _ACCENT_TERM_RE.finditer(text):
        before = text[:match.start()]
        x = xy[0] + int(drawer.textlength(before, font=font))
        drawer.text((x, xy[1]), match.group(), font=font, fill=accent + (255,))


def _draw_category_badge(img: Any, category: str, accent: Tuple[int, int, int],
                         side: int, Image: Any, ImageDraw: Any,
                         ImageFilter: Any) -> None:
    """Draw a small generic topic badge using only the supplied article category."""
    label = _WS_RE.sub(" ", str(category or "")).strip()[:8]
    if not label:
        return
    size = max(14, int(side * 0.032))
    max_diameter = int(side * 0.22)
    font, _source = find_font(size)
    if font is None:
        return
    drawer = ImageDraw.Draw(img)
    while drawer.textlength(label, font=font) > max_diameter - 24 and size > 12:
        size -= 2
        font, _source = find_font(size)
        if font is None:
            return
    if drawer.textlength(label, font=font) > max_diameter - 24:
        return
    diameter = max(int(side * 0.12), int(drawer.textlength(label, font=font)) + 24)
    diameter = min(diameter, max_diameter)
    x, y = int(side * 0.06), int(side * 0.055)
    drawer.ellipse((x, y, x + diameter, y + diameter), fill=(24, 32, 40, 235),
                   outline=accent + (255,), width=max(1, side // 300))
    bbox = drawer.textbbox((0, 0), label, font=font)
    text_width, text_height = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if text_width > diameter - 12:
        return
    drawer.text((x + (diameter - text_width) // 2 - bbox[0],
                 y + (diameter - text_height) // 2 - bbox[1]),
                label, font=font, fill=(255, 255, 255, 255))


def _cover_square(img: Any, side: int, Image: Any) -> Any:
    """비율을 유지하며 정사각형으로 채운다(중앙 크롭 — 배경이 늘어나 일그러지지 않게)."""
    w, h = img.width, img.height
    scale = side / min(w, h)
    new = (max(side, int(round(w * scale))), max(side, int(round(h * scale))))
    resized = img.resize(new, Image.LANCZOS)
    left = (resized.width - side) // 2
    top = (resized.height - side) // 2
    return resized.crop((left, top, left + side, top + side))


def _draw_scrim(img: Any, bg: Tuple[int, int, int], side: int, *,
                solid_from_y: int, Image: Any, ImageDraw: Any) -> None:
    """사진을 유지하는 검정 반투명 그라데이션으로 하단 문구의 대비를 확보한다.

    `solid_from_y`는 기존 호출부 호환을 위해 남긴 경계 이름이다. 이 위치 아래도
    검정이 완전 불투명해지지 않아 배경 사진이 끝까지 이어진다. 계정 배경색은
    의도적으로 사용하지 않는다.
    """
    solid_from_y = max(0, min(int(solid_from_y), side))
    ramp = max(1, int(side * 0.24))
    top = max(0, solid_from_y - ramp)
    height = side - top
    if height <= 0:
        return
    layer = Image.new("RGBA", (side, height), (0, 0, 0, 0))
    drawer = ImageDraw.Draw(layer)
    solid_row = solid_from_y - top
    peak_alpha = 190
    for row in range(height):
        if row >= solid_row:
            alpha = peak_alpha
        else:
            f = row / max(1, solid_row)
            alpha = int(round(peak_alpha * (f ** 1.4)))
        drawer.line([(0, row), (side, row)], fill=(0, 0, 0, alpha))
    img.alpha_composite(layer, (0, top))


def _shadow_text(img: Any, xy: Tuple[int, int], text: str, font: Any,
                 fill: Tuple[int, int, int], Image: Any, ImageDraw: Any,
                 ImageFilter: Any) -> None:
    """흐린 검은 그림자 + 크리스프 본문 — 밴드 위에서도 글자 경계가 또렷하게."""
    try:
        layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((xy[0], xy[1] + max(1, img.width // 400)), text,
                                   font=font, fill=(0, 0, 0, 110))
        img.alpha_composite(layer.filter(ImageFilter.GaussianBlur(max(2, img.width // 180))))
    except Exception:  # noqa: BLE001 — 그림자는 장식이라 실패해도 본문은 그린다
        pass
    ImageDraw.Draw(img).text(xy, text, font=font, fill=fill + (255,))


def availability() -> Dict[str, Any]:
    """이 환경에서 한글 합성이 가능한지(진단·화면 표기용). 부작용 0."""
    font, source = find_font(48)
    return {
        "ok": font is not None,
        "font": source,
        "reason": "" if font is not None else "font_missing",
        "env_override": os.environ.get(FONT_ENV, "").strip(),
    }


def normalize_thumbnail_square(image_path: Any) -> Dict[str, Any]:
    """Return a square PNG while preserving the source and every visible frame edge.

    Already-square PNGs are returned byte-for-byte. Other valid images are contained
    over a blurred, cover-scaled background and written to a new sibling file. PNG
    text metadata is retained so composed-thumbnail markers remain effective.
    """
    source = Path(str(image_path or ""))
    result: Dict[str, Any] = {
        "ok": False, "path": None, "reason": "", "derived": False,
        "source_size": None, "output_size": None,
    }
    if not source.is_file():
        result["reason"] = "thumbnail_missing"
        return result
    try:
        from PIL import Image, ImageFilter, PngImagePlugin
    except ImportError as exc:
        result["reason"] = f"pillow_unavailable:{type(exc).__name__}"
        return result

    try:
        with Image.open(source) as raw:
            raw.load()
            source_size = raw.size
            result["source_size"] = list(source_size)
            metadata = dict(raw.info)
            image = raw.convert("RGBA")
        width, height = image.size
        if width <= 0 or height <= 0:
            result["reason"] = "thumbnail_invalid_dimensions"
            return result
        side = max(width, height)
        if width == height:
            result.update(ok=True, path=source, output_size=[side, side])
            return result

        background = _cover_square(image, side, Image).filter(
            ImageFilter.GaussianBlur(max(8, side // 24)))
        contained = image.copy()
        contained.thumbnail((side, side), Image.Resampling.LANCZOS)
        background.alpha_composite(
            contained, ((side - contained.width) // 2, (side - contained.height) // 2))

        output = source.with_name(f"{source.stem}_square_{uuid.uuid4().hex[:10]}.png")
        png_info = PngImagePlugin.PngInfo()
        for key, value in metadata.items():
            if isinstance(key, str) and isinstance(value, str):
                png_info.add_text(key, value)
        background.convert("RGB").save(output, "PNG", pnginfo=png_info)
        with Image.open(output) as check:
            check.load()
            if check.width != check.height:
                output.unlink(missing_ok=True)
                result["reason"] = "thumbnail_square_validation_failed"
                return result
        result.update(ok=True, path=output, derived=True, output_size=[side, side])
        return result
    except (OSError, ValueError, TypeError) as exc:
        log.warning("[thumbnail_compose] 썸네일 정사각형 변환 실패(%s)", type(exc).__name__)
        result["reason"] = f"thumbnail_normalization_failed:{type(exc).__name__}"
        return result


__all__ = ["MARKER_KEY", "MARKER_MESSAGE_KEY", "FONT_ENV", "DEFAULT_CANVAS",
           "hex_to_rgb", "find_font", "is_composed", "compose", "availability",
           "normalize_thumbnail_square", "square_canvas_size"]
