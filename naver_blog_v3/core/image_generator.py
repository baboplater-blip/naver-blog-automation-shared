# core/image_generator.py
# Windows 최적화 이미지 생성 (썸네일 + 카드뉴스 5장 세트)
from __future__ import annotations

import os, re, sys, textwrap
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
log = get_logger(__name__)
try:
    from config.settings import (
        THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT, CARD_WIDTH, IMAGE_THEMES, WINDOWS_FONTS
    )
except (ImportError, AttributeError):
    THUMBNAIL_WIDTH  = 800
    THUMBNAIL_HEIGHT = 420
    CARD_WIDTH       = 800
    IMAGE_THEMES = {
        "청년":{"bg":"#0d1b2a","accent":"#00b4d8","sub":"#90e0ef"},
        "주거":{"bg":"#1a0a2e","accent":"#f72585","sub":"#ff9ece"},
        "복지":{"bg":"#0a1628","accent":"#06d6a0","sub":"#80ffdb"},
        "바우처":{"bg":"#1a1100","accent":"#ffd60a","sub":"#ffe566"},
        "취업":{"bg":"#001524","accent":"#ff6b35","sub":"#ffb38a"},
        "의료":{"bg":"#1a0010","accent":"#e040fb","sub":"#f5a7ff"},
        "교육":{"bg":"#0d1f0d","accent":"#69f0ae","sub":"#b9ffd8"},
        "default":{"bg":"#111827","accent":"#4f9cf9","sub":"#93c5fd"},
    }
    WINDOWS_FONTS = {"regular":"C:/Windows/Fonts/malgun.ttf","bold":"C:/Windows/Fonts/malgunbd.ttf","fallback":"C:/Windows/Fonts/gulim.ttc"}
from PIL import Image, ImageDraw, ImageFont, ImageFilter

try:
    from config import profile as _profile
    OUTPUT_DIR = str(_profile.output_dir())
except Exception:  # noqa: BLE001 — 프로필 라우터 로드 실패 시 레거시 경로 폴백
    OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "output")
os.makedirs(OUTPUT_DIR, exist_ok=True)


# ──────────────────────────────────────────────────
# 🔤 폰트 로더 (Windows 한글 최적화)
# ──────────────────────────────────────────────────
def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = WINDOWS_FONTS["bold"] if bold else WINDOWS_FONTS["regular"]
    if not os.path.exists(path):
        path = WINDOWS_FONTS["fallback"]
    try:
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    except (OSError, ValueError) as e:
        log.warning(f"폰트 로드 실패, 기본 폰트 사용: {e}")
    log.warning(f"폰트 파일 없음, 기본 폰트 사용: {path}")
    return ImageFont.load_default()


def _hex(color: str):
    """#rrggbb → (r,g,b)"""
    c = color.lstrip("#")
    return tuple(int(c[i:i+2], 16) for i in (0, 2, 4))


# ──────────────────────────────────────────────────
# 🎨 배경 렌더 (그라디언트 + 도형 장식)
# ──────────────────────────────────────────────────
def _draw_bg(draw: ImageDraw.ImageDraw, W: int, H: int,
             bg: str, accent: str):
    bg_rgb  = _hex(bg)
    acc_rgb = _hex(accent)

    # 배경
    draw.rectangle([0, 0, W, H], fill=bg_rgb)

    # 우상단 글로우 원
    for r in range(220, 0, -30):
        alpha = int(30 * (r / 220))
        overlay_c = tuple(min(255, b + alpha) for b in acc_rgb)
        draw.ellipse([W - r, -r, W + r, r], fill=overlay_c)

    # 좌하단 소형 원
    draw.ellipse([-60, H - 60, 60, H + 60],
                 fill=tuple(min(255, b + 15) for b in bg_rgb))

    # 하단 악센트 바
    draw.rectangle([0, H - 8, W, H], fill=acc_rgb)


# ──────────────────────────────────────────────────
# 🖼️ 1. 메인 썸네일
# ──────────────────────────────────────────────────
def create_thumbnail(title: str, category: str, post_id: str) -> str:
    try:
        # 프로필별 브랜드 팩(default=내장 examplebrand, 로드 실패 시 리터럴 폴백).
        try:
            from config import brand as _brand
        except Exception:  # noqa: BLE001
            _brand = None

        theme = IMAGE_THEMES.get(category, IMAGE_THEMES["default"])
        bg, accent, sub = theme["bg"], theme["accent"], theme["sub"]
        # Older per-account settings may still request 800x420. The thumbnail
        # producer is now square locally without rewriting those user preferences.
        from core.thumbnail_compose import square_canvas_size
        W, H = square_canvas_size(THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)

        img  = Image.new("RGB", (W, H))
        draw = ImageDraw.Draw(img)
        _draw_bg(draw, W, H, bg, accent)

        clean = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', title)

        # 카테고리 뱃지 — 브랜드 태그 접미(default='정부지원', named 빈값이면 접미 생략)
        badge_f = _font(17, bold=True)
        tag     = _brand.thumbnail_tag() if _brand is not None else "정부지원"
        badge   = f" {category} {tag} " if tag else f" {category} "
        bx, by  = 40, 36
        bb = draw.textbbox((bx, by), badge, font=badge_f)
        draw.rounded_rectangle([bb[0]-6, bb[1]-4, bb[2]+6, bb[3]+4],
                                radius=5, fill=_hex(accent))
        draw.text((bx, by), badge, font=badge_f, fill=_hex(bg))

        # 연도 뱃지 (우상단)
        yr_f = _font(15)
        draw.text((W - 90, 38), "2026", font=yr_f, fill=_hex(sub))

        # 제목 (자동 줄바꿈)
        title_f = _font(36, bold=True)
        sub_f   = _font(20)
        lines   = textwrap.wrap(clean, width=19)[:3]
        y       = 95
        for line in lines:
            draw.text((40, y), line, font=title_f, fill=(255, 255, 255))
            y += 50

        # 구분선
        draw.line([40, y + 10, W - 40, y + 10], fill=_hex(accent), width=2)

        # 서브 텍스트 — 브랜드 서브카피(default='📋 신청방법 · 대상 · 지원금액 완벽 정리')
        subtext = (_brand.thumbnail_subtext() if _brand is not None
                   else "📋 신청방법 · 대상 · 지원금액 완벽 정리")
        if subtext:
            draw.text((40, y + 22), subtext, font=sub_f, fill=_hex(sub))

        # 로고 텍스트 — 브랜드 로고(default='정부지원 완벽가이드 블로그')
        logo_f = _font(14)
        logo = (_brand.thumbnail_logo() if _brand is not None
                else "정부지원 완벽가이드 블로그")
        if logo:
            draw.text((40, H - 36), logo, font=logo_f, fill=_hex(sub))

        # 출처 표기 — 브랜드 출처(default='gov.kr 기반', 빈값이면 표기 생략)
        source_note = (_brand.thumbnail_source_note() if _brand is not None
                       else "gov.kr 기반")
        if source_note:
            draw.text((W - 130, H - 36), source_note, font=logo_f, fill=_hex(sub))

        out = os.path.join(OUTPUT_DIR, f"thumb_{post_id}.png")
        img.save(out, "PNG")
        log.info(f"썸네일: {out}")
        return out
    except (OSError, ValueError) as e:
        log.error(f"썸네일 생성 실패: {e}", exc_info=True)
        return ""


# ──────────────────────────────────────────────────
# 📰 2. 카드뉴스 시리즈 (5장)
# ──────────────────────────────────────────────────
CARD_TEMPLATES = [
    {"label": "핵심요약",  "icon": "📌", "num": "01"},
    {"label": "신청자격",  "icon": "✅", "num": "02"},
    {"label": "신청방법",  "icon": "📝", "num": "03"},
    {"label": "지원금액",  "icon": "💰", "num": "04"},
    {"label": "주의사항",  "icon": "⚠️", "num": "05"},
]

# 내용이 없을 때 예전에 카드에 찍던 문구. 이제는 **절대 렌더하지 않는다** —
# 자리표시 카드를 식별(재렌더 대조)하는 용도로만 남겨 둔다. 2026-09-23 사고 참조.
PLACEHOLDER_ITEMS = ["정보를 불러오는 중입니다...", "잠시 후 다시 확인해주세요."]


def _usable_items(sections: dict, key: str) -> list[str]:
    """이 카드에 실제로 찍을 내용이 있는가. 없으면 빈 리스트.

    자리표시 문구는 **내용으로 치지 않는다** — 예전에는 여기가 비면
    "정보를 불러오는 중입니다…"를 찍어 넣었고, 그 이미지가 파일로 실재하고
    PNG 로 정상 디코드되는 탓에 발행 직전 게이트를 그대로 통과해 실제 블로그에
    올라갔다(2026-09-23 사용자 신고).
    """
    raw = (sections or {}).get(key)
    if isinstance(raw, (str, bytes)):
        raw = [raw]
    items: list[str] = []
    for entry in (raw or []):
        text = str(entry or "").strip()
        if not text or text in PLACEHOLDER_ITEMS:
            continue
        items.append(text)
    return items


def create_card_set(title: str, sections: dict, category: str, post_id: str) -> list[str]:
    """
    카드뉴스 시리즈 생성 — **내용이 있는 카드만** 만든다.
    sections: {"핵심요약": ["내용1","내용2",...], "신청자격": [...], ...}
    반환: 저장된 이미지 경로 리스트. 내용이 하나도 없으면 빈 리스트(=생성 실패).

    ⚠️ 빈 리스트는 "카드 0장"이라는 **명시적 미완**이다. 호출자는 이것을 성공
    자산으로 취급하면 안 된다 — 발행 경계(`naver_uploader._prepare_publish_images`)가
    이미지 0장을 fail-closed 로 막는다.
    """
    try:
        return _create_card_set_impl(title, sections, category, post_id)
    except (OSError, ValueError) as e:
        log.error(f"카드 이미지 생성 실패: {e}", exc_info=True)
        return []


def placeholder_card_hashes(title: str, category: str) -> set:
    """이 제목·카테고리로 만들어졌을 **자리표시 카드**의 sha256 집합.

    과거(2026-09-23 수리 이전)에 생성돼 보관본에 남아 있는 자리표시 카드를
    발행 경계가 식별하기 위한 것이다. 같은 입력으로 옛 렌더를 메모리에 재현해
    해시만 내고 **파일은 쓰지 않는다**(부작용 0).
    """
    import hashlib
    import io

    hashes: set = set()
    try:
        for tmpl in CARD_TEMPLATES:
            img = _render_card(title, category, tmpl, list(PLACEHOLDER_ITEMS))
            buf = io.BytesIO()
            img.save(buf, "PNG")
            hashes.add(hashlib.sha256(buf.getvalue()).hexdigest())
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.debug(f"자리표시 해시 계산 실패({type(e).__name__}: {e}) — 판정 보류")
        return set()
    return hashes


def _create_card_set_impl(title: str, sections: dict, category: str, post_id: str) -> list[str]:
    """카드뉴스 생성 내부 구현."""
    paths = []
    for tmpl in CARD_TEMPLATES:
        key   = tmpl["label"]
        items = _usable_items(sections, key)
        if not items:
            # ★ 내용이 없으면 **카드를 만들지 않는다**(2026-09-23). 예전에는 여기서
            #   자리표시 문구를 찍어 넣어, 내용이 빈 카드가 정상 PNG 로 발행됐다.
            log.warning(f"카드 '{key}' 내용 없음 — 생성하지 않음(자리표시 카드 금지)")
            continue

        img = _render_card(title, category, tmpl, items)
        out = os.path.join(OUTPUT_DIR, f"card_{post_id}_{tmpl['num']}.png")
        img.save(out, "PNG")
        paths.append(out)
        log.info(f"카드 {tmpl['num']}: {out}")

    if not paths:
        log.error("카드뉴스 0장 — 카드에 넣을 내용(card_sections)이 없습니다.")
    return paths


def _render_card(title: str, category: str, tmpl: dict, items: list):
    """카드 1장을 메모리에 렌더한다(파일 저장 없음 — 부작용 0).

    `placeholder_card_hashes` 가 옛 자리표시 카드를 **바이트 단위로 재현**해
    식별할 수 있도록, 렌더 로직을 저장 경로에서 분리해 둔 순수 함수다.
    """
    theme = IMAGE_THEMES.get(category, IMAGE_THEMES["default"])
    bg, accent = theme["bg"], theme["accent"]
    clean = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', title)

    H = 180 + len(items) * 76 + 60
    H = max(H, 420)
    W = CARD_WIDTH

    img  = Image.new("RGB", (W, H))
    draw = ImageDraw.Draw(img)
    _draw_bg(draw, W, H, bg, accent)

    # ── 헤더 ──
    draw.rectangle([0, 0, W, 88], fill=_hex(accent))
    num_f   = _font(13, bold=True)
    label_f = _font(18, bold=True)
    draw.text((20, 14), f"CARD {tmpl['num']} / 05", font=num_f, fill=_hex(bg))
    draw.text((20, 36), f"{tmpl['icon']}  {tmpl['label']}", font=label_f, fill=_hex(bg))

    # ── 부제 (글 제목 축약) ──
    sub_title = clean if len(clean) <= 30 else clean[:28] + "…"
    sub_f2    = _font(15)
    draw.text((W - len(sub_title) * 9 - 20, 60), sub_title, font=sub_f2, fill=_hex(bg))

    # ── 각 항목 ──
    item_f = _font(20, bold=True)
    y = 108
    for i, item in enumerate(items[:6]):
        # 번호 원
        cx, cy = 36, y + 18
        draw.ellipse([cx-18, cy-18, cx+18, cy+18], fill=_hex(accent))
        draw.text((cx - 6, cy - 12), str(i + 1), font=_font(17, bold=True), fill=_hex(bg))

        # 항목 텍스트 (자동 줄바꿈)
        line = textwrap.shorten(str(item), width=34, placeholder="…")
        draw.text((66, y + 6), line, font=item_f, fill=(255, 255, 255))

        # 구분선
        draw.line([20, y + 52, W - 20, y + 52], fill=_hex(accent), width=1)
        y += 70

    # ── 하단 CTA ──
    draw.rectangle([0, H - 48, W, H - 8], fill=_hex(accent))
    cta_f = _font(15, bold=True)
    draw.text((W // 2 - 140, H - 36), "👉 자세한 내용은 블로그 본문에서 확인하세요",
              font=cta_f, fill=_hex(bg))

    return img


# ──────────────────────────────────────────────────
# 📦 포스트용 전체 이미지 세트 생성
# ──────────────────────────────────────────────────
def generate_all_images(title: str, post_data: dict,
                         gov24_shots: list, post_id: str) -> list[str]:
    """
    전체 이미지 생성:
      1. 썸네일 1장
      2. 카드뉴스 — **내용이 있는 카드만** (내용이 없으면 0장)
      3. 공공사이트 캡처 — ★발행 이미지가 아니다★

    반환: 업로드할 이미지 경로 순서 리스트 (썸네일 → 카드).

    ⚠️ 2026-09-23 변경 — ``gov24_shots``(공공기관 사이트 캡처)를 더 이상 업로드
    목록에 넣지 않는다. 이 캡처는 **출처 확인용 참고 자료**이지 글의 본문 이미지가
    아닌데, 예전에는 그대로 블로그에 올라갔다. 실제 사고: 건강검진 글에 국민연금
    **대표홈/개인민원/기금운용 선택 화면**이 본문 이미지로 발행됐다 — 제목에서
    URL 을 못 찾아 사이트 홈으로 폴백한 캡처였다. 캡처 파일은 지우지 않고
    screenshots/ 에 그대로 남으며, 출처가 기록돼 발행 경계가 식별할 수 있다.
    """
    category  = post_data.get("category", "default")
    sections  = post_data.get("card_sections", {})

    thumb = create_thumbnail(title, category, post_id)
    cards = create_card_set(title, sections, category, post_id)

    # 업로드 순서: 썸네일 → 카드
    all_images = ([thumb] if thumb else []) + cards

    try:
        from core import image_asset_guard as _guard
        _guard.mark_many(all_images, _guard.KIND_GENERATED,
                         producer="image_generator", slug=str(post_id or ""))
        # 참고 캡처는 '발행 금지' 출처로 기록해 둔다 — 다른 경로를 타고 업로드
        # 목록에 섞여 들어와도 발행 경계가 막을 수 있게(이중 방어).
        _guard.mark_many(gov24_shots or [], _guard.KIND_REFERENCE,
                         producer="public_site_screenshot", slug=str(post_id or ""))
    except Exception as e:  # noqa: BLE001 — 출처 기록 실패가 생성을 막지 않는다
        log.debug(f"이미지 출처 기록 실패({type(e).__name__}) — 발행 경계 판정은 계속")

    n_ref = len(gov24_shots or [])
    log.info(f"총 {len(all_images)}장 이미지 준비 완료"
             + (f" (참고용 캡처 {n_ref}장은 발행하지 않음)" if n_ref else ""))
    if not all_images:
        log.error("발행용 이미지 0장 — 카드 내용(card_sections)이 없고 썸네일도 실패했습니다.")
    return all_images
