# core/text_overlay.py — 배경 일러스트 위 '가독성 밴드' 오버레이 시스템 (768 기준)
"""FLUX/Krea 는 한글을 못 그린다. 그래서 모델은 '글자 없는 일러스트'만 만들고, 이 모듈이
그 위에 한글(뱃지·제목·칩·Q&A)을 PIL 로 합성한다.

핵심 원칙(가장 중요):
  배경 위에 텍스트를 절대 그냥 얹지 않는다. 텍스트 영역마다 불투명(solid) teal 그라디언트
  '밴드'를 깔고, 밴드의 solid 구간이 글자 전체를 완전히 덮은 뒤 그 아래에서만 투명으로
  페이드시킨다 → 어떤 배경 위에서도 글자가 확실히 읽힌다.

폰트: Noto Sans KR 가변폰트(Weight 축) — 400/500/700/800/900. 실패 시 Malgun 폴백.
색: 고정 teal 브랜드 팔레트(카테고리 무관).
좌표/폰트: 768 기준 px. 캔버스 폭 W 에 비례(s = W/768)해 1024·2048 어디서든 동일 비율.

역할별 레시피:
  thumb·body : 제목 카드 — 상단 밴드(뱃지+제목) + 하단 밴드(칩+크레딧)
  info       : info 카드 — 상단 밴드(뱃지+제목) + 어두운 콘텐츠 패널(Q&A / 체크리스트 행)

어떤 예외가 나도 원본(base)을 반환한다 — 이미지 파이프라인을 절대 깨지 않는다.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from PIL import Image, ImageDraw, ImageFont, ImageFilter

from config.logger import get_logger
from core.image_generator import _font  # Noto 로드 실패 시 Malgun 폴백용

log = get_logger(__name__)

# ── 컬러 토큰 (teal 가독성 팔레트) ──────────────────────────────────────────────
_TEAL_DEEP = (12, 63, 66)       # #0c3f42 — 밴드 상단/그림자
_TEAL = (19, 79, 82)            # #134f52 — 밴드 본체
_TEAL_BRAND = (15, 91, 94)      # #0f5b5e — 뱃지 텍스트
_TEAL_TINT = (121, 208, 204)    # #79d0cc — 서브텍스트·체크 배경
_AMBER = (245, 158, 11)         # #f59e0b — Q 아이콘 배경
_AMBER_TEXT = (58, 36, 0)       # #3a2400 — Q 아이콘 글자
_CHECK = (255, 206, 122)        # #ffce7a — 칩 체크 표시
_PANEL_DARK = (17, 25, 34, 235)  # rgba(17,25,34,.92) — info 콘텐츠 패널
_WHITE = (255, 255, 255)
_WHITE_75 = (255, 255, 255, 191)  # 크레딧
_DEFAULT_BADGES = ("자격", "금액", "신청", "대상")

# ── 폰트 (Noto Sans KR 가변 — Weight 축은 항상 명시, 기본이 Thin(100)이라) ──────
_SANS_VF = "C:/Windows/Fonts/NotoSansKR-VF.ttf"


def _sans(size: int, weight: int = 400) -> ImageFont.FreeTypeFont:
    """Noto Sans KR 가변폰트를 지정 굵기(wght 400~900)로. 실패 시 Malgun 폴백."""
    size = max(1, int(size))
    if os.path.exists(_SANS_VF):
        try:
            f = ImageFont.truetype(_SANS_VF, size)
            try:
                f.set_variation_by_axes([weight])
            except (OSError, ValueError, AttributeError):
                pass
            return f
        except (OSError, ValueError) as exc:
            log.warning(f"[text_overlay] Noto Sans 로드 실패({size}/{weight}) — Malgun 폴백: {exc}")
    return _font(size, bold=(weight >= 600))


# ── 텍스트/도형 유틸 ────────────────────────────────────────────────────────────
def _clean_title(title: str) -> str:
    """발행풀 접두('YYYY-MM-DD — ')를 떼어낸 순수 제목."""
    return re.sub(r"^\d{4}-\d{2}-\d{2}\s*[—-]\s*", "", (title or "").strip())


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont,
          max_w: int, max_lines: int) -> list[str]:
    """한글은 픽셀 폭 기준 글자 단위로 감싼다. 마지막 줄이 넘치면 말줄임."""
    text = (text or "").strip()
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            if len(lines) >= max_lines:
                return lines[:max_lines]
            continue
        if draw.textlength(cur + ch, font=font) <= max_w:
            cur += ch
        else:
            lines.append(cur)
            cur = ch
            if len(lines) >= max_lines:
                last = lines[-1]
                while last and draw.textlength(last + "…", font=font) > max_w:
                    last = last[:-1]
                lines[-1] = last + "…"
                return lines
    if cur and len(lines) < max_lines:
        lines.append(cur)
    return lines


def _interp(stops: list, f: float):
    """정렬된 그라디언트 stop[(pos, (r,g,b), a)] 에서 위치 f 의 (rgb, alpha) 보간."""
    if f <= stops[0][0]:
        return stops[0][1], stops[0][2]
    if f >= stops[-1][0]:
        return stops[-1][1], stops[-1][2]
    for i in range(1, len(stops)):
        p0, c0, a0 = stops[i - 1]
        p1, c1, a1 = stops[i]
        if f <= p1:
            t = (f - p0) / (p1 - p0) if p1 > p0 else 0.0
            rgb = tuple(int(round(c0[j] + (c1[j] - c0[j]) * t)) for j in range(3))
            a = int(round(a0 + (a1 - a0) * t))
            return rgb, a
    return stops[-1][1], stops[-1][2]


def _band(base: Image.Image, y_top: int, height: int, stops: list) -> None:
    """세로 그라디언트 '가독성 밴드'를 그려 합성. stops 는 밴드 위→아래 기준."""
    if height <= 0:
        return
    W = base.width
    layer = Image.new("RGBA", (W, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    denom = max(1, height - 1)
    for row in range(height):
        rgb, a = _interp(stops, row / denom)
        d.line([(0, row), (W, row)], fill=(rgb[0], rgb[1], rgb[2], a))
    base.alpha_composite(layer, (0, y_top))


def _rrect(base: Image.Image, box, radius: int, *, fill=None,
           outline=None, width: int = 1) -> None:
    """반투명 지원 둥근 사각형(패널·칩·뱃지) — RGBA 레이어에 그려 합성."""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        list(box), radius=radius, fill=fill, outline=outline, width=max(1, width))
    base.alpha_composite(layer)


def _draw_check(draw: ImageDraw.ImageDraw, x: int, y: int, size: int,
                color, width: int) -> None:
    """체크마크(✓)를 선으로 직접 그린다(폰트 글리프 결손 회피)."""
    p1 = (x, y + size * 0.55)
    p2 = (x + size * 0.38, y + size * 0.92)
    p3 = (x + size * 0.95, y + size * 0.12)
    draw.line([p1, p2, p3], fill=color, width=max(2, width), joint="curve")


def _shadow_text(base: Image.Image, xy, text: str, font: ImageFont.FreeTypeFont,
                 fill=_WHITE, *, dy: int = 2, blur: int = 6, alpha: int = 76) -> None:
    """흰 텍스트 + text-shadow(0 2px 12px rgba(0,0,0,.3)) 근사 — 흐린 검은 그림자 후 크리스프."""
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0], xy[1] + dy), text, font=font, fill=(0, 0, 0, alpha))
    base.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))
    ImageDraw.Draw(base).text(xy, text, font=font, fill=fill)


# ── 브랜드 헬퍼 ──────────────────────────────────────────────────────────────────
def _brand_bits() -> Any:
    try:
        from config import brand as _brand
        return _brand
    except Exception:  # noqa: BLE001
        return None


def _badge_label(category: str, brand: Any) -> str:
    tag = ""
    if brand is not None:
        try:
            tag = brand.thumbnail_tag() or ""
        except Exception:  # noqa: BLE001
            tag = ""
    return f"{category} {tag}".strip() if tag else category


def _brand_badges(brand: Any):
    if brand is not None:
        try:
            b = brand.thumbnail_badges()
            if b:
                return tuple(b)
        except Exception:  # noqa: BLE001
            pass
    return _DEFAULT_BADGES


def _brand_logo(brand: Any) -> str:
    if brand is not None:
        try:
            return brand.thumbnail_logo() or ""
        except Exception:  # noqa: BLE001
            return ""
    return "정부지원 완벽가이드 블로그"


def _pill_badge(base: Image.Image, x: int, y: int, text: str, s: float) -> None:
    """흰 배경 + teal-brand 텍스트, weight 700, 23px, padding 9/22, radius 999."""
    draw = ImageDraw.Draw(base)
    f = _sans(int(23 * s), 700)
    pad_x, pad_y = int(22 * s), int(9 * s)
    tb = draw.textbbox((0, 0), text, font=f)
    tw, th = tb[2] - tb[0], tb[3] - tb[1]
    w, h = tw + 2 * pad_x, th + 2 * pad_y
    _rrect(base, [x, y, x + w, y + h], radius=h // 2, fill=(255, 255, 255, 255))
    ImageDraw.Draw(base).text((x + pad_x - tb[0], y + pad_y - tb[1]), text,
                              font=f, fill=_TEAL_BRAND)


# ── 카드: 제목(썸네일/본문) ──────────────────────────────────────────────────────
def _draw_title_card(img: Image.Image, s: float, *, title: str, subtitle: str,
                     category: str, brand: Any) -> None:
    W, H = img.width, img.height

    # 상단 밴드 300px: #0c3f42 0% → #134f52 74% → 투명 100%
    _band(img, 0, int(300 * s),
          [(0.0, _TEAL_DEEP, 255), (0.74, _TEAL, 255), (1.0, _TEAL, 0)])
    # 하단 밴드 178px: (0deg) #0c3f42 0%(하) → #134f52 62% → 투명 100%(상)
    bh = int(178 * s)
    _band(img, H - bh, bh,
          [(0.0, _TEAL, 0), (0.38, _TEAL, 255), (1.0, _TEAL_DEEP, 255)])

    draw = ImageDraw.Draw(img)
    mx = int(46 * s)

    # 뱃지 (좌상단 46,42)
    _pill_badge(img, mx, int(42 * s), _badge_label(category, brand), s)

    # 제목: 흰색 weight 900, 48px, lh 1.2, 2줄 — solid 구간(0~222px) 안에 하단선
    tf = _sans(int(48 * s), 900)
    lines = _wrap(draw, _clean_title(title), tf, W - 2 * mx, 2)
    y = int(104 * s)
    lh = int(48 * 1.2 * s)
    for ln in lines:
        _shadow_text(img, (mx, y), ln, tf)
        y += lh

    # 하단: 칩 + 크레딧
    _draw_chips(img, s, brand)
    logo = _brand_logo(brand)
    if logo:
        ImageDraw.Draw(img).text((mx, H - int(40 * s)), logo,
                                 font=_sans(int(18 * s), 500), fill=_WHITE_75)


def _draw_chips(img: Image.Image, s: float, brand: Any) -> None:
    """반투명 흰 칩(회화/캠프/유학/꿀팁) + ✓(#ffce7a). 하단 밴드 solid 구간에 배치."""
    W, H = img.width, img.height
    draw = ImageDraw.Draw(img)
    f = _sans(int(21 * s), 600)
    th = f.getbbox("가")[3]
    pad_x, gap_in, gap = int(16 * s), int(9 * s), int(12 * s)
    chk = int(20 * s)
    h = int(44 * s)
    x = int(46 * s)
    y = H - int(112 * s)
    for b in _brand_badges(brand)[:4]:
        tw = int(draw.textlength(b, font=f))
        w = pad_x + chk + gap_in + tw + pad_x
        _rrect(img, [x, y, x + w, y + h], radius=h // 2,
               fill=(255, 255, 255, 41), outline=(255, 255, 255, 128),
               width=max(1, int(1 * s)))
        _draw_check(ImageDraw.Draw(img), x + pad_x, y + (h - chk) // 2, chk,
                    _CHECK, int(3 * s))
        ImageDraw.Draw(img).text((x + pad_x + chk + gap_in, y + (h - th) // 2 - int(2 * s)),
                                 b, font=f, fill=_WHITE)
        x += w + gap


# ── 카드: info(Q&A / 체크리스트) ─────────────────────────────────────────────────
def _draw_info_card(img: Image.Image, s: float, *, title: str, subtitle: str,
                    category: str, items: Optional[list], brand: Any) -> None:
    W, H = img.width, img.height

    # 상단 밴드 206px: #0c3f42 0% → #134f52 82% → 투명 100%
    _band(img, 0, int(206 * s),
          [(0.0, _TEAL_DEEP, 255), (0.82, _TEAL, 255), (1.0, _TEAL, 0)])

    draw = ImageDraw.Draw(img)
    mx = int(46 * s)
    _pill_badge(img, mx, int(42 * s), _badge_label(category, brand), s)

    # 제목: 흰색 weight 800, 33px, 2줄
    tf = _sans(int(33 * s), 800)
    lines = _wrap(draw, _clean_title(title), tf, W - 2 * mx, 2)
    y = int(96 * s)
    lh = int(33 * 1.2 * s)
    for ln in lines:
        _shadow_text(img, (mx, y), ln, tf)
        y += lh

    rows = _normalize_items(items)
    if not rows:
        return

    # 콘텐츠 패널: inset 좌우36 위212 아래30, rgba(17,25,34,.92), radius26, 1px 테두리
    px0, py0 = int(36 * s), int(212 * s)
    px1, py1 = W - int(36 * s), H - int(30 * s)
    # 드롭섀도(0 16px 40px rgba(0,0,0,.35)) 근사
    sh = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(sh).rounded_rectangle([px0, py0 + int(16 * s), px1, py1 + int(16 * s)],
                                         radius=int(26 * s), fill=(0, 0, 0, 90))
    img.alpha_composite(sh.filter(ImageFilter.GaussianBlur(int(20 * s))))
    _rrect(img, [px0, py0, px1, py1], radius=int(26 * s), fill=_PANEL_DARK,
           outline=(255, 255, 255, 26), width=max(1, int(1 * s)))

    # 내부 콘텐츠 영역(padding 44/46), 항목 flex-column space-around
    ix0, iy0 = px0 + int(46 * s), py0 + int(44 * s)
    ix1, iy1 = px1 - int(46 * s), py1 - int(44 * s)
    n = min(len(rows), 4)
    slot = (iy1 - iy0) / n
    cir = int(44 * s)
    qf = _sans(int(31 * s), 700)
    sf = _sans(int(22 * s), 500)
    qglyph = _sans(int(24 * s), 800)
    for i in range(n):
        (sym, head), body = rows[i]
        cy = int(iy0 + slot * i + slot / 2)
        icx, icy = ix0, cy - cir // 2
        is_q = (sym == "Q")
        ImageDraw.Draw(img).ellipse([icx, icy, icx + cir, icy + cir],
                                    fill=_AMBER if is_q else _TEAL_TINT)
        if is_q:
            tb = draw.textbbox((0, 0), "Q", font=qglyph)
            ImageDraw.Draw(img).text(
                (icx + (cir - (tb[2] - tb[0])) // 2 - tb[0],
                 icy + (cir - (tb[3] - tb[1])) // 2 - tb[1]),
                "Q", font=qglyph, fill=_AMBER_TEXT)
        else:
            _draw_check(ImageDraw.Draw(img), icx + int(11 * s), icy + int(11 * s),
                        cir - int(22 * s), _TEAL_BRAND, int(4 * s))

        tx = icx + cir + int(20 * s)
        maxw = ix1 - tx
        block_h = int(31 * s) + int(8 * s) + int(22 * s)
        qy = cy - block_h // 2
        qline = _wrap(draw, head, qf, maxw, 1)
        if qline:
            ImageDraw.Draw(img).text((tx, qy), qline[0], font=qf, fill=_WHITE)
        for sl in _wrap(draw, body, sf, maxw, 1):
            ImageDraw.Draw(img).text((tx, qy + int(31 * s) + int(8 * s)), sl,
                                     font=sf, fill=_TEAL_TINT)
            break


def _normalize_items(items: Optional[Iterable[dict]]) -> list:
    """Q&A/스텝 아이템 → [((헤드기호, 헤드텍스트), 본문)]. q/a→'Q', step/desc→번호."""
    out: list = []
    if not items:
        return out
    sn = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        if "q" in it and "a" in it:
            out.append((("Q", str(it["q"]).strip()), str(it["a"]).strip()))
        elif "step" in it and "desc" in it:
            sn += 1
            out.append(((str(sn), str(it["step"]).strip()), str(it["desc"]).strip()))
    return out


def _load_canvas(base_png: Path, size: Optional[int]) -> Image.Image:
    img = Image.open(base_png).convert("RGBA")
    if size and (img.width != size or img.height != size):
        img = img.resize((size, size), Image.LANCZOS)
    return img


# ── 공개 진입점 ──────────────────────────────────────────────────────────────────
def overlay_card(base_png: Path, out_png: Path, *, role: str, title: str,
                 subtitle: str = "", category: str = "default",
                 items: Optional[list] = None,
                 canvas: Optional[int] = None) -> Path:
    """글자 없는 일러스트(base_png) 위에 한글 가독성 밴드 오버레이를 합성해 out_png 저장.

    실패해도 base_png 를 out_png 로 복사/유지하고 경로를 반환한다(파이프라인 불변).
    좌표/폰트는 768 기준이며 캔버스 폭에 비례(s=W/768)해 스케일된다.
    """
    base_png, out_png = Path(base_png), Path(out_png)
    try:
        img = _load_canvas(base_png, canvas)
        s = img.width / 768.0
        brand = _brand_bits()
        if role in ("thumb", "body"):
            _draw_title_card(img, s, title=title, subtitle=subtitle,
                             category=category, brand=brand)
        elif role == "info":
            _draw_info_card(img, s, title=title, subtitle=subtitle,
                            category=category, items=items, brand=brand)
        else:
            log.warning(f"[text_overlay] 미지원 role={role} — 원본 유지")
            img.convert("RGB").save(out_png, "PNG")
            return out_png
        img.convert("RGB").save(out_png, "PNG")
        return out_png
    except Exception as exc:  # noqa: BLE001 — 오버레이 실패는 원본으로 진행
        log.warning(f"[text_overlay] 오버레이 실패({exc}) — 원본 이미지 사용")
        try:
            if base_png != out_png:
                Image.open(base_png).convert("RGB").save(out_png, "PNG")
            return out_png
        except Exception:  # noqa: BLE001
            return base_png
