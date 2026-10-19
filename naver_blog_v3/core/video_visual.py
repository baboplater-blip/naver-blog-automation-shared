# core/video_visual.py
# examplebrand 쇼츠 영상용 비주얼 이미지 생성 (PIL, 로컬·무비용·무외부API).
#
# 블로그 글엔 이미지가 없고 디스크에도 자산이 없으므로, 카테고리 테마 기반의
# 세로(1080×1920) 인포그래픽 카드 이미지를 직접 렌더한다. 이 이미지가 영상
# ContentScene 의 배경(<Img>)으로 들어간다 — "이미지가 들어가게" 의 핵심 소스.
#
# 설계 원칙:
#   - 1080×1920 세로. 상단 노치 여백 + 하단 자막존(360px) 을 비워 자막 가독성 확보.
#   - 카테고리 IMAGE_THEMES 색을 영상용으로 심화(딥 그라디언트 + 글로우).
#   - 핵심 숫자(stat) 를 히어로로 크게, 라벨/보조문구를 위/아래로.
#   - 한글은 Malgun Gothic — image_generator 와 동일 폰트 스택(검증된 경로).
#   - 어떤 단계가 실패해도 예외를 밖으로 던지지 않고 None/부분결과 반환(렌더 무중단).
#
# auto_scheduler·일 발행과 완전 무관 — build_multichannel_pack 경로에서만 호출.
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.logger import get_logger

log = get_logger(__name__)

try:
    from config.settings import IMAGE_THEMES, WINDOWS_FONTS
except (ImportError, AttributeError):  # pragma: no cover - 방어적
    IMAGE_THEMES = {
        "청년": {"bg": "#0d1b2a", "accent": "#00b4d8", "sub": "#90e0ef"},
        "주거": {"bg": "#1a0a2e", "accent": "#f72585", "sub": "#ff9ece"},
        "복지": {"bg": "#0a1628", "accent": "#06d6a0", "sub": "#80ffdb"},
        "바우처": {"bg": "#1a1100", "accent": "#ffd60a", "sub": "#ffe566"},
        "취업": {"bg": "#001524", "accent": "#ff6b35", "sub": "#ffb38a"},
        "의료": {"bg": "#1a0010", "accent": "#e040fb", "sub": "#f5a7ff"},
        "교육": {"bg": "#0d1f0d", "accent": "#69f0ae", "sub": "#b9ffd8"},
        "default": {"bg": "#111827", "accent": "#4f9cf9", "sub": "#93c5fd"},
    }
    WINDOWS_FONTS = {
        "regular": "C:/Windows/Fonts/malgun.ttf",
        "bold": "C:/Windows/Fonts/malgunbd.ttf",
        "fallback": "C:/Windows/Fonts/gulim.ttc",
    }

from PIL import Image, ImageDraw, ImageFont, ImageFilter

# ─────────────────────────────────────────────────────
# 캔버스 상수 (9:16)
# ─────────────────────────────────────────────────────
W = 1080
H = 1920
MARGIN = 96
CONTENT_W = W - MARGIN * 2          # 888
TOP_SAFE = 150                      # 상단 노치/시간 영역
CAPTION_ZONE = 420                  # 하단 자막 오버레이 영역(비워 둠)

EMOJI_FONT = "C:/Windows/Fonts/seguiemj.ttf"

# ── 프리미엄 타이포 폰트 스택 ──
# Malgun Gothic 은 "관공서 문서" 인상 + 굵기 2단(Reg/Bold)뿐이라 미니멀 포스터에 부적합.
# Noto Sans KR 가변폰트는 굵기 7단(Thin·Light·DemiLight·Regular·Medium·Bold·Black)을
# 한 파일로 지원 → 얇은 보조 + 초굵은 강조의 '굵기 대비'가 프리미엄 인상의 핵심.
SANS_VF = "C:/Windows/Fonts/NotoSansKR-VF.ttf"     # 기본(깔끔·현대)
SERIF_VF = "C:/Windows/Fonts/NotoSerifKR-VF.ttf"   # 고급·클래식(명조) 전환용
# 폰트 톤: 명조(세리프)=클래식·고급(레퍼런스 채택). Sans 로 되돌리려면 False.
USE_SERIF = True
# Sans·Serif 두 가변폰트의 굵기명 합집합(Serif=ExtraLight·SemiBold, Sans=Thin·DemiLight).
# 활성 폰트에 없는 굵기는 set_variation_by_name 이 조용히 무시하고 기본 인스턴스 유지.
_VF_WEIGHTS = ("Thin", "ExtraLight", "Light", "DemiLight", "Regular",
               "Medium", "SemiBold", "Bold", "Black")

# 썸네일 전용 프리미엄 accent — IMAGE_THEMES의 네온색은 카드/영상용(밝은 배경 대비).
# 순수 블랙 커버 위에선 채도가 조금 낮고 균형 잡힌 톤이 더 '고급·에디토리얼'하게 읽힌다.
_THUMB_ACCENT = {
    "청년": "#38BDF8",    # sky — 네온 시안(#00b4d8)보다 맑고 차분
    "주거": "#FB7185",    # rose — 핫마젠타(#f72585)보다 세련된 로즈
    "복지": "#34D399",    # emerald
    "바우처": "#FBBF24",   # amber gold — 따뜻한 프리미엄 골드
    "취업": "#FB923C",    # orange
    "의료": "#E879F9",    # orchid — 소프트 푸시아
    "교육": "#A3E635",    # lime
    "금융": "#38BDF8",    # sky
    "default": "#60A5FA",  # blue
}

# 썸네일 카테고리 배지 한글 라벨(분야 즉시 인지)
_CATEGORY_LABEL = {
    "청년": "청년·금융", "주거": "주거지원", "복지": "복지·수당",
    "바우처": "바우처", "취업": "취업·일자리", "의료": "의료·건강",
    "교육": "교육·장학", "금융": "금융·지원금", "default": "정부지원",
}


# ── 브랜드(프로필별) — default 는 내장 examplebrand ──
def _brand_name() -> str:
    try:
        from config import brand as _b
        return _b.name()
    except Exception:  # noqa: BLE001
        return "examplebrand"


def _brand_display() -> str:
    try:
        from config import brand as _b
        return _b.display()
    except Exception:  # noqa: BLE001
        return "examplebrand"


def _spaced(s: str) -> str:
    """'examplebrand' → 'S I L V E R H U G' (자간 브랜딩용)."""
    return " ".join(list(str(s)))


# ─────────────────────────────────────────────────────
# 색/폰트 헬퍼
# ─────────────────────────────────────────────────────
def _hex(color: str) -> tuple[int, int, int]:
    c = (color or "#000000").lstrip("#")
    if len(c) != 6:
        return (17, 24, 39)
    try:
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (17, 24, 39)


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    """a→b 를 t(0~1) 로 선형 보간."""
    return tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))  # type: ignore[return-value]


def _darken(rgb: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return _mix(rgb, (0, 0, 0), t)


# ── 대비/판독성 (썸네일 색 자동 결정) ──
INK = (11, 15, 20)          # 밝은 배경 위 텍스트
FOG = (245, 247, 250)       # 어두운 배경 위 텍스트


def _luma(rgb: tuple[int, int, int]) -> float:
    """상대 휘도(0~1, sRGB 선형화)."""
    def _lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (_lin(x) for x in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la, lb = _luma(a), _luma(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _readable_on(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    """배경 위에서 대비가 더 큰 전경(INK/FOG) 반환."""
    return FOG if _contrast(FOG, bg) >= _contrast(INK, bg) else INK


def _is_bright(rgb: tuple[int, int, int]) -> bool:
    """밝은 accent(골드·그린·라임 등) — 어두운 배경 위 히어로로 부적합."""
    return _luma(rgb) >= 0.35


def _font(size: int, bold: bool = True,
          weight: Optional[str] = None) -> ImageFont.FreeTypeFont:
    """Noto Sans KR 가변폰트 우선(굵기 축). weight 지정 시 그 굵기, 아니면 bold→Bold/Regular.

    Noto 로드 실패 시에만 Malgun 으로 폴백해 렌더 무중단을 보장한다.
    """
    w = weight or ("Bold" if bold else "Regular")
    if w not in _VF_WEIGHTS:
        w = "Bold" if bold else "Regular"
    vf = SERIF_VF if USE_SERIF else SANS_VF
    if os.path.exists(vf):
        try:
            f = ImageFont.truetype(vf, size)
            try:
                f.set_variation_by_name(w)
            except (OSError, ValueError):  # 가변축 미지원 폰트면 기본 인스턴스 유지
                pass
            return f
        except (OSError, ValueError) as e:
            log.warning(f"Noto 가변폰트 로드 실패({size}px/{w}) — Malgun 폴백: {e}")
    # 폴백: Malgun Gothic
    path = WINDOWS_FONTS["bold"] if w in ("Bold", "Black", "Medium") \
        else WINDOWS_FONTS["regular"]
    if not os.path.exists(path):
        path = WINDOWS_FONTS.get("fallback", path)
    try:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    except (OSError, ValueError) as e:
        log.warning(f"폰트 로드 실패({size}px), 기본 폰트: {e}")
    return ImageFont.load_default()


def _emoji_font(size: int) -> Optional[ImageFont.FreeTypeFont]:
    """Segoe UI Emoji(컬러). 없으면 None — 아이콘은 선택적 장식."""
    if not os.path.exists(EMOJI_FONT):
        return None
    try:
        return ImageFont.truetype(EMOJI_FONT, size)
    except (OSError, ValueError):
        return None


def _text_w(draw: ImageDraw.ImageDraw, text: str,
            font: ImageFont.FreeTypeFont) -> int:
    if not text:
        return 0
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


def _fit_font(draw: ImageDraw.ImageDraw, text: str, max_w: int,
              base_size: int, *, bold: bool = True,
              weight: Optional[str] = None,
              min_size: int = 40) -> ImageFont.FreeTypeFont:
    """text 가 max_w 안에 들어올 때까지 폰트 크기를 줄여 반환."""
    size = base_size
    while size > min_size:
        f = _font(size, bold=bold, weight=weight)
        if _text_w(draw, text, f) <= max_w:
            return f
        size -= 4
    return _font(min_size, bold=bold, weight=weight)


def _wrap(draw: ImageDraw.ImageDraw, text: str,
          font: ImageFont.FreeTypeFont, max_w: int,
          max_lines: int = 2) -> list[str]:
    """한글/영문 혼합 픽셀 기준 줄바꿈. 공백 우선, 없으면 글자 단위.

    max_lines 를 넘겨 내용이 잘릴 때에만 마지막 줄에 … 를 붙인다.
    내용이 전부 들어가면 원문을 손대지 않는다.
    """
    if not text:
        return []
    text = text.replace("\n", " ").strip()

    # 전체 줄 목록을 우선 생성(개수 제한 없이). 잘림 여부는 뒤에서 판정.
    all_lines: list[str] = []
    cur = ""
    for tok in text.split(" "):
        trial = (cur + " " + tok).strip() if cur else tok
        if _text_w(draw, trial, font) <= max_w:
            cur = trial
            continue
        if cur:
            all_lines.append(cur)
            cur = ""
        # 토큰 자체가 폭 초과 → 글자 단위로 쪼갬
        if _text_w(draw, tok, font) > max_w:
            piece = ""
            for ch in tok:
                if _text_w(draw, piece + ch, font) <= max_w:
                    piece += ch
                else:
                    if piece:
                        all_lines.append(piece)
                    piece = ch
            cur = piece
        else:
            cur = tok
    if cur:
        all_lines.append(cur)

    if len(all_lines) <= max_lines:
        return all_lines            # 전부 수용 — 손대지 않음

    # 잘림 발생 — 마지막 허용 줄에 … 부착
    kept = all_lines[:max_lines]
    last = kept[-1]
    while last and _text_w(draw, last + "…", font) > max_w and len(last) > 1:
        last = last[:-1]
    kept[-1] = last + "…"
    return kept


# ─────────────────────────────────────────────────────
# 배경 렌더 (딥 그라디언트 + 글로우 + 기하 악센트)
# ─────────────────────────────────────────────────────
def _vertical_gradient(top: tuple[int, int, int],
                       bottom: tuple[int, int, int]) -> Image.Image:
    """세로 그라디언트 기본 배경."""
    base = Image.new("RGB", (W, H), top)
    grad = Image.new("L", (1, H))
    for y in range(H):
        grad.putpixel((0, y), int(255 * (y / H)))
    grad = grad.resize((W, H))
    bottom_img = Image.new("RGB", (W, H), bottom)
    return Image.composite(bottom_img, base, grad)


def _add_glow(img: Image.Image, cx: int, cy: int, radius: int,
              rgb: tuple[int, int, int], alpha: int) -> None:
    """부드러운 원형 글로우를 additive 로 얹는다."""
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([cx - radius, cy - radius, cx + radius, cy + radius],
               fill=(rgb[0], rgb[1], rgb[2], alpha))
    glow = glow.filter(ImageFilter.GaussianBlur(radius // 2))
    img.paste(Image.alpha_composite(img.convert("RGBA"), glow).convert("RGB"),
              (0, 0))


def _draw_backdrop(category: str) -> tuple[Image.Image, dict]:
    """카테고리 테마 딥 배경 + 글로우 + 하단 자막존 암전. (img, palette) 반환."""
    theme = IMAGE_THEMES.get(category) or IMAGE_THEMES.get("default")
    bg = _hex(theme["bg"])
    # 카드 accent 도 썸네일·자막과 동일한 프리미엄 팔레트로 통일(네온색 대신).
    accent = _hex(_THUMB_ACCENT.get(category) or theme["accent"])
    sub = _hex(theme["sub"])

    top = _mix(bg, accent, 0.06)          # 상단 살짝 악센트 물듦
    bottom = _darken(bg, 0.55)            # 하단은 더 어둡게(자막 대비)
    img = _vertical_gradient(top, bottom).convert("RGB")

    # 우상단 대형 글로우 + 좌중단 보조 글로우
    _add_glow(img, int(W * 0.86), int(H * 0.16), 560, accent, 60)
    _add_glow(img, int(W * 0.10), int(H * 0.46), 420, sub, 34)

    # 하단 자막존 암전 그라디언트(가독성)
    scrim = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    for i in range(CAPTION_ZONE):
        y = H - CAPTION_ZONE + i
        a = int(150 * (i / CAPTION_ZONE))
        sd.line([(0, y), (W, y)], fill=(0, 0, 0, a))
    img = Image.alpha_composite(img.convert("RGBA"), scrim).convert("RGB")

    return img, {"bg": bg, "accent": accent, "sub": sub}


# ─────────────────────────────────────────────────────
# 아이콘(선택적) — 컬러 이모지, 실패 시 생략
# ─────────────────────────────────────────────────────
def _paste_emoji(img: Image.Image, emoji: str, xy: tuple[int, int],
                 size: int) -> bool:
    """중심 xy 에 컬러 이모지. 성공 True."""
    if not emoji:
        return False
    ef = _emoji_font(size)
    if ef is None:
        return False
    try:
        layer = Image.new("RGBA", (size * 2, size * 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        d.text((size, size), emoji, font=ef, anchor="mm",
               embedded_color=True)
        img.paste(layer, (xy[0] - size, xy[1] - size), layer)
        return True
    except (OSError, ValueError, TypeError):
        return False


# ─────────────────────────────────────────────────────
# 카드 1장 렌더
# ─────────────────────────────────────────────────────
def render_card(*, index: int, total: int, label: str, stat: str,
                caption: str, icon: str, category: str,
                out_path: Path) -> Optional[Path]:
    """단일 콘텐츠 카드 인포그래픽 이미지.

    Args:
        index: 0-based 카드 번호.
        total: 전체 카드 수(보통 3).
        label: 카드 제목(짧게). 예 "지원 대상".
        stat: 히어로 숫자/핵심(가장 크게). 예 "월 9만원".
        caption: 보조 한 줄. 예 "국민행복카드 충전".
        icon: 이모지 1개(선택).
        category: IMAGE_THEMES 키.
        out_path: 저장 경로(png).
    """
    try:
        img, pal = _draw_backdrop(category)
        draw = ImageDraw.Draw(img)
        accent, sub = pal["accent"], pal["sub"]
        bg = pal["bg"]

        # 거대 STEP 워터마크(배경 장식) — 우하단, 아주 옅게
        wm_font = _font(720, bold=True)
        wm = str(index + 1)
        wm_layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        wd = ImageDraw.Draw(wm_layer)
        wd.text((W - 60, H - CAPTION_ZONE - 120), wm, font=wm_font,
                anchor="rs", fill=(accent[0], accent[1], accent[2], 26))
        img = Image.alpha_composite(img.convert("RGBA"), wm_layer).convert("RGB")
        draw = ImageDraw.Draw(img)

        # ── 상단 바: 브랜드 + STEP 인디케이터 ── 훅/CTA 와 동일한 대문자 자간 브랜딩
        brand_f = _font(32, weight="Medium")
        draw.text((MARGIN, TOP_SAFE), _spaced(_brand_display()), font=brand_f,
                  fill=(188, 191, 198))
        # STEP 도트 인디케이터(우측)
        dot_r = 12
        gap = 40
        x = W - MARGIN
        for i in range(total - 1, -1, -1):
            fill = accent if i == index else _mix(bg, sub, 0.35)
            cx = x
            draw.ellipse([cx - dot_r, TOP_SAFE + 4, cx + dot_r, TOP_SAFE + 4 + dot_r * 2],
                         fill=fill)
            x -= gap

        # ── 좌측 악센트 바 ──
        bar_top = TOP_SAFE + 150
        draw.rounded_rectangle([MARGIN, bar_top, MARGIN + 12, bar_top + 120],
                               radius=6, fill=accent)

        # ── 카테고리/STEP 칩 ──
        chip_f = _font(38, bold=True)
        chip_txt = f"  STEP {index + 1}  "
        cb = draw.textbbox((0, 0), chip_txt, font=chip_f)
        cw, ch = cb[2] - cb[0], cb[3] - cb[1]
        chip_x = MARGIN + 44
        chip_y = bar_top - 6
        draw.rounded_rectangle([chip_x, chip_y, chip_x + cw + 20, chip_y + ch + 26],
                               radius=16, fill=accent)
        draw.text((chip_x + 10, chip_y + 8), chip_txt.strip(), font=chip_f,
                  fill=_darken(bg, 0.1))

        # 아이콘(칩 우측, 선택적)
        if icon:
            _paste_emoji(img, icon, (chip_x + cw + 130, chip_y + ch // 2 + 12), 84)
            draw = ImageDraw.Draw(img)

        # ── 라벨(카드 제목) ──
        y = bar_top + 190
        label_f = _fit_font(draw, label or "", CONTENT_W, 76, bold=True,
                            min_size=52)
        for line in _wrap(draw, label or "", label_f, CONTENT_W, max_lines=2):
            draw.text((MARGIN, y), line, font=label_f, fill=(255, 255, 255))
            y += int(label_f.size * 1.22)

        # ── 히어로 stat ──
        y += 60
        stat = (stat or "").strip()
        if stat:
            stat_f = _fit_font(draw, stat, CONTENT_W, 200, bold=True,
                               min_size=90)
            # 여러 줄이면 히어로 크기 낮춤
            stat_lines = _wrap(draw, stat, stat_f, CONTENT_W, max_lines=2)
            if len(stat_lines) > 1:
                stat_f = _fit_font(draw, stat_lines[0], CONTENT_W, 150,
                                   bold=True, min_size=80)
            for line in stat_lines:
                # 숫자 강조 그림자
                draw.text((MARGIN + 3, y + 3), line, font=stat_f,
                          fill=_darken(bg, 0.4))
                draw.text((MARGIN, y), line, font=stat_f, fill=accent)
                y += int(stat_f.size * 1.06)

        # ── 보조 caption ──
        caption = (caption or "").strip()
        if caption:
            y += 30
            cap_f = _fit_font(draw, caption, CONTENT_W, 56, bold=False,
                              min_size=40)
            for line in _wrap(draw, caption, cap_f, CONTENT_W, max_lines=2):
                draw.text((MARGIN, y), line, font=cap_f, fill=(188, 191, 198))
                y += int(cap_f.size * 1.25)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path), "PNG")
        return out_path
    except (OSError, ValueError) as e:
        log.warning(f"카드 이미지 렌더 실패(index={index}) — 텍스트 카드 폴백: {e}")
        return None


# ─────────────────────────────────────────────────────
# 썸네일(표지) — 피드에서 스크롤 멈추게 하는 커버
# ─────────────────────────────────────────────────────
import re as _re
# 히어로에 어울리는 "판돈" 숫자 — 금액·기간·비율·연령 등. 절차 카운트(단계/가지/회/건)는 제외.
_NUM_PHRASE = _re.compile(
    r"(?:월\s?|연\s?|최대\s?|최소\s?)?\d[\d,]*\s?"
    r"(?:만\s?원|억\s?원|천\s?원|원|만원|개월|일|주|년|시간|%|퍼센트|배|명|세|평|㎡)")
# 마감/기한 — 강한 후킹 숫자
_DEADLINE = _re.compile(r"(?:D-\s?\d+|마감\s?임박|\d+일\s?(?:남|전)|\d+월\s?\d+일까지)")
# 절차 카운트 — 히어로로 약함(있어도 통계 숫자로 승격 금지)
_WEAK_COUNT = _re.compile(r"^\s*\d+\s?(?:단계|가지|회|건|번)\s*$")


def _is_weak_count(s: str) -> bool:
    return bool(_WEAK_COUNT.match(s or ""))


_YEAR_LIKE = _re.compile(r"^(19|20)\d\d\s?년?$")


def _is_year(g: str) -> bool:
    """'2026', '2026년' 등 연도성 표현 — 히어로 숫자로 부적합."""
    return bool(_YEAR_LIKE.match((g or "").replace(" ", "")))


def _num_phrase(s: str) -> str:
    """문자열에서 판돈 숫자 구 하나(마감>금액/기간, 연도·절차카운트 제외). 없으면 ''."""
    if not s:
        return ""
    dm = _DEADLINE.search(s)
    if dm:
        return _re.sub(r"\s+", " ", dm.group()).strip()
    for m in _NUM_PHRASE.finditer(s):
        g = _re.sub(r"\s+", " ", m.group()).strip()
        if _is_year(g):
            continue
        return g
    return ""


def _pick_hero_stat(cards: list) -> tuple[str, str, str]:
    """카드들 중 썸네일 히어로로 쓸 (stat, caption, icon) 선택.

    우선순위: 금액(원 포함) > 숫자 포함 > 첫 비어있지 않은 stat.
    """
    def _stat(c):
        return (c.get("stat") or "").strip() if isinstance(c, dict) else ""

    money = [c for c in cards if isinstance(c, dict) and "원" in _stat(c)]
    digit = [c for c in cards if isinstance(c, dict)
             and any(ch.isdigit() for ch in _stat(c))]
    nonempty = [c for c in cards if isinstance(c, dict) and _stat(c)]
    chosen = (money or digit or nonempty or [None])[0]
    if not chosen:
        return "", "", ""
    return (_stat(chosen), (chosen.get("caption") or "").strip(),
            (chosen.get("icon") or "").strip())


def _pick_numeric_hero(cards: list, extra: tuple = ()) -> tuple[str, str, str]:
    """숫자 히어로를 stat→caption→text→extra(훅/제목) 순으로 탐색.

    Returns (숫자구, 그 카드의 caption, 그 카드의 icon). 없으면 ('','','').
    """
    def _g(c, k):
        return (c.get(k) or "").strip() if isinstance(c, dict) else ""

    # 1) stat 에 금액/숫자
    for c in cards:
        s = _g(c, "stat")
        if "원" in s and any(ch.isdigit() for ch in s):
            return s, _g(c, "caption"), _g(c, "icon")
    for c in cards:
        s = _g(c, "stat")
        if (any(ch.isdigit() for ch in s)
                and not _is_year(s) and not _is_weak_count(s)):
            return s, _g(c, "caption"), _g(c, "icon")
    # 2) caption / text 에서 숫자 구 추출
    for key in ("caption", "text"):
        for c in cards:
            p = _num_phrase(_g(c, key))
            if p:
                return p, _g(c, "caption"), _g(c, "icon")
    # 3) 훅/제목에서 숫자 구
    for s in extra:
        p = _num_phrase(s or "")
        if p:
            icon = next((_g(c, "icon") for c in cards if _g(c, "icon")), "")
            return p, "", icon
    return "", "", ""


def thumb_accent_hex(category: str) -> str:
    """썸네일·영상 자막 공용 프리미엄 accent 헥사('#RRGGBB').

    자막 하이라이트가 썸네일 강조어와 같은 색을 쓰도록 shorts_renderer 가 이 값을
    inputProps.accent 로 넘긴다. 없는 카테고리는 테마 accent 로 폴백.
    """
    theme = IMAGE_THEMES.get(category) or IMAGE_THEMES.get("default")
    return _THUMB_ACCENT.get(category) or theme.get("accent") or "#FBBF24"


def _thumb_backdrop(W_: int, H_: int, category: str) -> tuple[Image.Image, dict]:
    """썸네일 전용 배경 — 순수에 가까운 블랙(미니멀 타이포).

    그라디언트·글로우 없이 거의 평평한 블랙. 카테고리 accent 는 강조 단어에만 쓴다.
    순수 블랙 위에선 모든 accent(골드·시안·핑크 등)가 고대비라 색 보정이 불필요.
    """
    theme = IMAGE_THEMES.get(category) or IMAGE_THEMES.get("default")
    # 썸네일은 프리미엄 accent 팔레트 우선, 없는 카테고리만 테마 네온색으로 폴백.
    accent = _hex(_THUMB_ACCENT.get(category) or theme["accent"])
    sub = _hex(theme["sub"])
    bg = _hex(theme["bg"])

    # 뎁스만 주는 아주 미세한 세로 그라디언트(밴딩 방지), 사실상 블랙.
    top, bottom = (15, 15, 16), (6, 6, 7)
    base = Image.new("RGB", (W_, H_), top)
    grad = Image.new("L", (1, H_))
    for y in range(H_):
        grad.putpixel((0, y), int(255 * (y / H_)))
    grad = grad.resize((W_, H_))
    img = Image.composite(Image.new("RGB", (W_, H_), bottom), base, grad)

    return img, {"bg": bg, "accent": accent, "sub": sub,
                 "bright": _is_bright(accent)}


def _draw_lines(draw, lines, font, x, y, fill, *, line_gap, stroke_w, stroke_fill):
    """여러 줄 텍스트를 외곽선과 함께 그리고 다음 y 반환."""
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill,
                  stroke_width=stroke_w, stroke_fill=stroke_fill)
        y += int(font.size * line_gap)
    return y


# 문장형에서 초대형으로 강조할 핵심어 후보(순서=우선순위)
_EMPH_TRIGGERS = (
    "반드시", "꼭", "무료", "공짜", "놓치면", "놓치지", "손해", "주의", "필독",
    "지금", "전액", "최대", "총정리", "이것만", "단돈", "오직", "마감", "필수",
    "자격", "조건", "신청", "혜택", "인상", "확대",
)


def _emphasis_word(text: str) -> str:
    """문장에서 초대형 강조할 한 어절 선택 — 트리거어 우선, 없으면 가장 긴 어절."""
    words = [w for w in (text or "").split() if w]
    if not words:
        return (text or "").strip()
    for t in _EMPH_TRIGGERS:
        for w in words:
            if t in w:
                return w
    return max(words, key=len)


def _split_around(text: str, token: str) -> tuple[str, str, str]:
    """text 를 token 앞/토큰/뒤 로 분해. token 없으면 (text, token, '')."""
    text = (text or "").strip()
    if token and token in text:
        i = text.index(token)
        return text[:i].strip(), token, text[i + len(token):].strip()
    return text, token or "", ""


def _draw_center(draw: ImageDraw.ImageDraw, y: int, text: str,
                 font: ImageFont.FreeTypeFont, fill, cx: int) -> None:
    """가로 중앙 정렬 한 줄(y=상단)."""
    if not text:
        return
    b = draw.textbbox((0, 0), text, font=font)
    draw.text((cx - (b[2] - b[0]) // 2 - b[0], y - b[1]), text, font=font, fill=fill)


def _draw_center_tracked(draw: ImageDraw.ImageDraw, y: int, text: str,
                         font: ImageFont.FreeTypeFont, fill, cx: int,
                         tracking: int) -> None:
    """자간(tracking)을 준 중앙 정렬 한 줄 — 브랜드/카테고리용."""
    if not text:
        return
    widths = [draw.textlength(ch, font=font) for ch in text]
    total = sum(widths) + tracking * (len(text) - 1)
    b = draw.textbbox((0, 0), text, font=font)
    x = cx - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y - b[1]), ch, font=font, fill=fill)
        x += w + tracking


def _render_thumbnail(*, headline: str, hero: str, sub: str, numeric: bool,
                      category: str, category_label: str, cta_text: str,
                      out_path: Path, wide: bool = False,
                      brand: str = "") -> Optional[Path]:
    """폰트만으로 — 순수 블랙, 중앙 정렬, 한 어절 초대형 강조(색+밑줄). 미니멀·고급.

    강조 토큰: 숫자형=숫자, 문장형=핵심어. 나머지는 흰색 위/아래로 감싼다.
    박스·pill·글로우·그라디언트 없이 타이포로만.
    """
    try:
        Wt, Ht = (1280, 720) if wide else (1080, 1920)
        cx = Wt // 2
        cw = int(Wt * 0.88)
        img, pal = _thumb_backdrop(Wt, Ht, category)
        accent = pal["accent"]
        draw = ImageDraw.Draw(img)
        WHITE = (244, 246, 249)
        DIMMED = (188, 191, 198)
        GRAY = (150, 153, 160)

        hero = (hero or "").strip()
        if numeric:
            emph = hero
            pre, _, post = _split_around((headline or hero).strip(), emph)
            post = post.strip(" ?.!·,")
        else:
            emph = _emphasis_word(hero)
            pre, emph, post = _split_around(hero, emph)

        # 굵기 대비가 프리미엄 인상의 핵심: 강조어=Black(초굵) / 셋업=Light(얇게) / 결말=Bold.
        # 셋업(pre)·캡션(sub)을 Light 로 낮춰 강조어 Black 과의 대비를 극대화(에디토리얼).
        # min_size 하한을 낮춰 '최대 240만원'처럼 긴 강조구도 폭(cw) 안에서 축소되게 한다.
        emph_f = _fit_font(draw, emph, cw, int(Ht * (0.17 if not wide else 0.26)),
                           weight="Black", min_size=int(Ht * (0.062 if not wide else 0.10)))
        pre_f = _font(int(Ht * (0.050 if not wide else 0.068)), weight="Light")
        post_f = _font(int(Ht * (0.072 if not wide else 0.096)), weight="Bold")
        sub_f = _font(int(Ht * (0.033 if not wide else 0.044)), weight="Light")

        pre_lines = _wrap(draw, pre, pre_f, cw, 2) if pre else []
        post_lines = _wrap(draw, post, post_f, cw, 2) if post else []
        sub_lines = _wrap(draw, sub, sub_f, cw, 2) if sub else []

        ul_h = int(Ht * 0.009)     # 얇은 rule — '하이라이터'보다 정제된 인상
        gap_s = int(Ht * 0.014)
        block_h = sum(int(pre_f.size * 1.20) for _ in pre_lines)
        block_h += int(emph_f.size) + gap_s + ul_h + int(Ht * 0.018)
        block_h += sum(int(post_f.size * 1.14) for _ in post_lines)
        if sub_lines:
            block_h += int(Ht * 0.024) + sum(int(sub_f.size * 1.28) for _ in sub_lines)

        y = int(Ht * (0.47 if not wide else 0.52)) - block_h // 2
        y = max(y, int(Ht * (0.21 if not wide else 0.24)))

        # ── 상단: 브랜드(넓은 자간) + 카테고리(자간) ── 자간을 넓혀 에디토리얼·고급 인상
        brand_f = _font(int(Ht * 0.021), weight="Medium")
        _brand_txt = brand.upper() if brand else _brand_display()
        _draw_center_tracked(draw, int(Ht * 0.055), f"{_brand_txt}  ·  2026",
                             brand_f, DIMMED, cx, tracking=int(Ht * 0.009))
        if category_label:
            cat_f = _font(int(Ht * 0.026), weight="Medium")
            _draw_center_tracked(draw, int(Ht * 0.098), category_label, cat_f,
                                 accent, cx, tracking=int(Ht * 0.006))

        # ── pre (흰, 얇게, 살짝 자간) ── 셋업은 공기감 있게
        for ln in pre_lines:
            _draw_center_tracked(draw, y, ln, pre_f, WHITE, cx,
                                 tracking=int(Ht * 0.002))
            y += int(pre_f.size * 1.20)

        # ── 강조어(accent, 초대형) + 밑줄(얇고 정제된 rule) ──
        _draw_center(draw, y, emph, emph_f, accent, cx)
        eb = draw.textbbox((0, 0), emph, font=emph_f)
        y += int(emph_f.size) + gap_s
        ubw = int(min(eb[2] - eb[0], cw) * 0.96)
        draw.rounded_rectangle([cx - ubw // 2, y, cx + ubw // 2, y + ul_h],
                               radius=ul_h // 2, fill=accent)
        y += ul_h + int(Ht * 0.018)

        # ── post (흰, 대) ──
        for ln in post_lines:
            _draw_center(draw, y, ln, post_f, WHITE, cx)
            y += int(post_f.size * 1.14)

        # ── sub (회색, 얇게, 미세 자간) ──
        if sub_lines:
            y += int(Ht * 0.024)
            for ln in sub_lines:
                _draw_center_tracked(draw, y, ln, sub_f, GRAY, cx,
                                     tracking=int(Ht * 0.003))
                y += int(sub_f.size * 1.28)

        # ── CTA (하단 중앙, 흰 텍스트 + accent ▼) ──
        cta_f = _font(int(Ht * (0.034 if not wide else 0.048)), weight="Medium")
        cta = (cta_text or "자격·신청 확인").strip()
        cyy = int(Ht * (0.86 if not wide else 0.85))
        gap_a = int(Ht * 0.013)
        tb = draw.textbbox((0, 0), cta, font=cta_f)
        ab = draw.textbbox((0, 0), "▼", font=cta_f)
        total = (tb[2] - tb[0]) + gap_a + (ab[2] - ab[0])
        x0 = cx - total // 2
        draw.text((x0 - tb[0], cyy - tb[1]), cta, font=cta_f, fill=WHITE)
        draw.text((x0 + (tb[2] - tb[0]) + gap_a - ab[0], cyy - ab[1]), "▼",
                  font=cta_f, fill=accent)

        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.convert("RGB").save(str(out_path), "JPEG", quality=92)
        return out_path
    except (OSError, ValueError) as e:
        log.warning(f"썸네일 렌더 실패(wide={wide}) — 폴백: {e}")
        return None


def build_thumbnail(script: dict, category: str, out_dir: Path,
                    *, slug: str = "") -> dict:
    """shorts_script → 썸네일 커버 2종(세로 피드용 + 16:9 유튜브용).

    Returns {"vertical": path|None, "wide": path|None}.
    """
    out_dir = Path(out_dir)
    cards = (script or {}).get("h2_cards") or []
    hook = ((script or {}).get("hook") or (script or {}).get("title") or "").strip()
    # 글 제목(숫자 풍부)은 히어로 숫자 추출의 마지막 근거 — 파이프라인이 _post_title 로 주입
    title = ((script or {}).get("_post_title") or (script or {}).get("title") or "").strip()

    num_hero, num_cap, num_icon = _pick_numeric_hero(cards, extra=(hook, title))

    def _supporting(exclude: str) -> str:
        """카드 caption > stat 중 hook/히어로와 겹치지 않는 첫 구절."""
        for key in ("caption", "stat"):
            for c in cards:
                if not isinstance(c, dict):
                    continue
                v = (c.get(key) or "").strip()
                if v and v != hook and v != exclude:
                    return v
        return "신청 자격·방법 총정리"

    if num_hero:
        # 혜택형 — 숫자를 히어로로, 훅은 헤드라인
        headline = hook
        hero = num_hero
        numeric = True
        icon = num_icon or next((c.get("icon") for c in cards
                                 if isinstance(c, dict) and c.get("icon")), "")
        sub = num_cap or _supporting(num_hero)
    else:
        # 절차/설명형 — 훅 문장을 히어로로 크게
        headline = ""
        hero = hook
        numeric = False
        icon = next((c.get("icon") for c in cards
                     if isinstance(c, dict) and c.get("icon")), "")
        sub = _supporting(hook)

    # 카테고리 배지 라벨 — 매핑되면 그대로, 미매핑은 브랜드 기본 라벨
    # (default='정부지원'이라 기존 동일). 브랜드 라벨이 비었으면 카테고리명 자체를 배지로.
    category_label = _CATEGORY_LABEL.get(category)
    if category_label is None:
        try:
            from config import brand as _brand
            category_label = _brand.video_category_label() or category
        except Exception:  # noqa: BLE001 — 브랜드 로드 실패 시 내장 default 라벨 폴백
            category_label = _CATEGORY_LABEL["default"]
    # CTA — 마감형이면 긴급, 아니면 신청 안내(하단 화살표는 렌더에서 부착)
    deadline = bool(_DEADLINE.search(hero)) if numeric else False
    cta_text = "마감 전 신청" if deadline else ("받는 방법 보기" if numeric
                                            else "자격·신청 확인")

    vertical = _render_thumbnail(
        headline=headline, hero=hero, sub=sub, numeric=numeric,
        category=category, category_label=category_label, cta_text=cta_text,
        out_path=out_dir / "thumbnail.jpg", wide=False)
    wide = _render_thumbnail(
        headline=headline, hero=hero, sub=sub, numeric=numeric,
        category=category, category_label=category_label, cta_text=cta_text,
        out_path=out_dir / "thumbnail_wide.jpg", wide=True)

    log.info(f"썸네일 생성: vertical={'O' if vertical else 'X'} "
             f"wide={'O' if wide else 'X'} (slug={slug or '미지정'})")
    return {"vertical": str(vertical) if vertical else None,
            "wide": str(wide) if wide else None}


def render_backdrop_only(category: str, out_path: Path) -> Optional[Path]:
    """텍스트 없는 순수 배경(훅/CTA 용). 실패 시 None."""
    try:
        img, _ = _draw_backdrop(category)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(str(out_path), "PNG")
        return out_path
    except (OSError, ValueError) as e:
        log.warning(f"배경 이미지 렌더 실패 — 폴백: {e}")
        return None


# ─────────────────────────────────────────────────────
# 스크립트 → 카드 이미지 세트
# ─────────────────────────────────────────────────────
def build_card_images(script: dict, category: str, out_dir: Path,
                      *, slug: str = "") -> dict:
    """shorts_script 의 h2_cards 3개를 인포그래픽 이미지로 렌더.

    각 카드 dict 는 다음 키를 활용(없으면 안전 폴백):
        text     : 라벨(제목)         — 필수
        stat     : 히어로 숫자/핵심    — 없으면 text 재사용 안 하고 생략
        caption  : 보조 한 줄          — 선택
        icon     : 이모지 1개          — 선택

    Returns:
        {
          "cards": [str|None, ...],   # 각 카드 이미지 절대경로(실패=None)
          "backdrop": str|None,       # 훅/CTA 공용 배경 절대경로
          "count": int,               # 성공 카드 수
        }
    """
    out_dir = Path(out_dir)
    assets = out_dir / "assets"
    cards = (script or {}).get("h2_cards") or []
    if not isinstance(cards, list):
        cards = []
    total = min(len(cards), 3) or 3

    results: list[Optional[str]] = []
    ok = 0
    for i, card in enumerate(cards[:3]):
        if isinstance(card, dict):
            label = str(card.get("text") or "")
            stat = str(card.get("stat") or "")
            caption = str(card.get("caption") or "")
            icon = str(card.get("icon") or "")
        else:
            label = str(card or "")
            stat = caption = icon = ""
        p = render_card(index=i, total=total, label=label, stat=stat,
                        caption=caption, icon=icon, category=category,
                        out_path=assets / f"card_{i + 1}.png")
        if p is not None:
            results.append(str(p))
            ok += 1
        else:
            results.append(None)

    backdrop = render_backdrop_only(category, assets / "backdrop.png")

    log.info(f"영상 카드 이미지 생성: {ok}/{len(cards[:3])}장 성공 "
             f"(category={category}, slug={slug or '미지정'})")
    return {
        "cards": results,
        "backdrop": str(backdrop) if backdrop else None,
        "count": ok,
    }


# ─────────────────────────────────────────────────────
# 진행자(브랜드 모델) 플레이트 — 훅/CTA 인트로·아웃트로 앵커
# ─────────────────────────────────────────────────────
# 사용자가 넣어둔 예시브랜드 브랜드 모델 이미지(multichannel/assets/예시브랜드 모델)를
# 훅(인트로)·CTA(아웃트로)에 "진행자"로 세운다. 배경이 밝은 회색 스튜디오라
# near-black 테마와 안 맞으므로 **테마색 다크 틴트 + 세로 스크림**으로 통일한다
# (매팅 불필요·완전 로컬·시네마틱). 얼굴은 상단 1/3, 중앙 제목은 정장(어두움)+스크림
# 위에 얹혀 흰 명조가 또렷이 읽힌다. 모델 이미지가 없으면 조용히 None(기존 배경 폴백).
# 내장(default) 진행자 모델 폴더 — 브랜드 팩이 없을 때의 폴백.
PRESENTER_DIR = Path(__file__).resolve().parents[2] / "multichannel" / "assets" / "예시브랜드 모델"

# 관찰된 포즈(정렬 순서): [0]=팔짱(자신감) [1]=손 벌림(환영) … [6]=검지 위로(주목).
# 훅=환영, CTA=주목 이 자연스럽다. 파일 교체에 안전하도록 인덱스는 clamp.
_PRESENTER_POSE_INDEX = {"hook": 1, "cta": 6}


def _presenter_root() -> Path:
    """활성 프로필 브랜드 팩의 진행자 폴더(있으면), 없으면 내장 예시브랜드."""
    try:
        from config import brand as _b
        d = _b.presenter_dir()
        if d is not None:
            return d
    except Exception:  # noqa: BLE001
        pass
    return PRESENTER_DIR


def _presenter_sources() -> list[Path]:
    """브랜드 모델 이미지 목록(정렬). 폴더 없거나 비면 빈 리스트."""
    root = _presenter_root()
    if not root or not root.exists():
        return []
    return sorted(p for p in root.glob("*.png") if p.is_file())


def _pick_presenter_src(role: str) -> Optional[Path]:
    """role(hook/cta)에 맞는 포즈 이미지 1장. 부족하면 첫 이미지로 폴백."""
    files = _presenter_sources()
    if not files:
        return None
    idx = _PRESENTER_POSE_INDEX.get(role, 0)
    return files[idx] if idx < len(files) else files[0]


def _lerp(points: list[tuple[int, int]], y: int) -> int:
    """(y, val) 제어점들 사이 선형 보간. points 는 y 오름차순."""
    if y <= points[0][0]:
        return points[0][1]
    if y >= points[-1][0]:
        return points[-1][1]
    for i in range(1, len(points)):
        y0, v0 = points[i - 1]
        y1, v1 = points[i]
        if y <= y1:
            t = (y - y0) / (y1 - y0) if y1 != y0 else 0.0
            return int(round(v0 + (v1 - v0) * t))
    return points[-1][1]


def _presenter_scrim(category: str) -> Image.Image:
    """세로 스크림(RGBA). 얼굴존(상단)은 밝게, 제목·자막존(중하단)은 어둡게.

    스크림 색을 near-black 에 카테고리 accent 를 살짝 섞어(themed shadows) 피부는
    자연스럽게 두고 어두운 영역만 테마 색조를 띠게 한다. 1×H 알파 컬럼→W 리사이즈.
    """
    accent = _hex(_THUMB_ACCENT.get(category) or _THUMB_ACCENT["default"])
    tint = _mix((6, 7, 10), accent, 0.16)  # 어두운 영역에 테마 색조
    # (y, alpha) — 얼굴(190~660)은 낮게, 제목(880~1200)·자막(1430~) 갈수록 진하게.
    ramp = [(0, 150), (190, 84), (660, 74), (900, 150),
            (1200, 200), (1430, 224), (H, 238)]
    col = Image.new("L", (1, H))
    px = col.load()
    for y in range(H):
        px[0, y] = _lerp(ramp, y)
    alpha = col.resize((W, H))
    scrim = Image.new("RGBA", (W, H), (*tint, 0))
    scrim.putalpha(alpha)
    return scrim


def _compose_presenter(base: Image.Image, category: str) -> Image.Image:
    """모델 원본 → 1080×1920 테마 틴트 플레이트(RGB)."""
    accent = _hex(_THUMB_ACCENT.get(category) or _THUMB_ACCENT["default"])

    # 1) cover-fit 1080×1920 (수평 중앙, 수직 상단정렬 → 얼굴을 상단 1/3 유지).
    sw, sh = base.size
    scale = max(W / sw, H / sh)
    nw, nh = int(round(sw * scale)), int(round(sh * scale))
    img = base.resize((nw, nh), Image.LANCZOS)
    x0 = (nw - W) // 2
    img = img.crop((x0, 0, x0 + W, H))

    # 2) 탈채도(42%) + near-black 다크닝(40%) → 회색 스튜디오 배경이 차콜로 가라앉고
    #    전체가 무드있게 통일(밋밋한 회색빛 제거).
    gray = img.convert("L").convert("RGB")
    img = Image.blend(img, gray, 0.42)
    img = Image.blend(img, Image.new("RGB", (W, H), (8, 9, 12)), 0.40)

    # 3) 테마 accent 미세 워시(5%) — 카테고리 색조로 전체 통일.
    img = Image.blend(img, Image.new("RGB", (W, H), _darken(accent, 0.72)), 0.05)

    # 4) 세로 스크림 합성(테마 색조·텍스트 가독존).
    img = Image.alpha_composite(img.convert("RGBA"),
                                _presenter_scrim(category)).convert("RGB")
    return img


def build_presenter_plate(category: str, out_path: Path, *, role: str = "hook",
                          src: Optional[Path] = None) -> Optional[Path]:
    """단일 진행자 플레이트 생성·저장. 모델 이미지 없거나 실패 시 None(폴백)."""
    src = src or _pick_presenter_src(role)
    if not src or not Path(src).exists():
        return None
    try:
        base = Image.open(src).convert("RGB")
        plate = _compose_presenter(base, category or "default")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        plate.save(str(out_path), "PNG")
        return out_path
    except (OSError, ValueError) as e:
        log.warning(f"진행자 플레이트 생성 실패 — 폴백: {e}")
        return None


def build_presenter_plates(category: str, out_dir: Path) -> dict:
    """훅·CTA 진행자 플레이트 2종. 모델 폴더 없으면 {'hook':None,'cta':None}."""
    assets = Path(out_dir) / "assets"
    hook = build_presenter_plate(category, assets / "presenter_hook.png", role="hook")
    cta = build_presenter_plate(category, assets / "presenter_cta.png", role="cta")
    if hook or cta:
        log.info(f"진행자 플레이트 생성: hook={'O' if hook else 'X'} "
                 f"cta={'O' if cta else 'X'} (category={category})")
    return {"hook": str(hook) if hook else None,
            "cta": str(cta) if cta else None}


__all__ = ["build_card_images", "build_thumbnail", "render_card",
           "render_backdrop_only", "thumb_accent_hex",
           "build_presenter_plate", "build_presenter_plates", "W", "H"]
