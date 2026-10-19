# core/product_writer.py — 🛒 상품 데이터 → 제휴 리뷰 글 (2026-07-20)
"""세 번째 콘텐츠 소스의 '집필기'. 제목 기반(generate_blog_post)·사진 기반
(experience_writer)에 이어 **상품 기반**. content_generator 의 프롬프트 블록
(_brand_prompt_bits·_load_prompt_strategy·AEO·체류)과 data dict 계약·QA/발행 게이트는
그대로 재사용한다. 순수 신규는 두 가지뿐:

  1) 제휴 링크 CTA 결정론적 삽입 — 사용자의 affiliate_url 을 본문에 클릭 가능 링크로
     넣어 커미션 귀속을 보장(원본 상품 링크 아님).
  2) 대가성 표시(공정위) 결정론적 삽입 — 모델이 빼도 코드가 강제로 항상 넣는다.
     이것이 경쟁사(간판 수익 기능) 대비 우리의 차별점이자 하드 게이트.

무근거 날조 금지(fact_gate 철학): **추출된 상품 데이터 안의 값만** 사실로 쓰고,
사양·성분·효능·성능 수치를 지어내지 않는다(프롬프트 가드 + 결정론 표는 데이터로만 구성).

반환 data dict 는 photo_publish.publish_photo_run 이 그대로 소비한다
(content_html·image_paths·_force_image_insert·_qa_failed) — 발행 배선 추가 최소.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import List, Optional, Sequence

from config.logger import get_logger
from core.product_extractor import is_valid_url

log = get_logger(__name__)

# 상품 리뷰의 콘텐츠 타입 카테고리(주제 아님). 두 가지를 해결:
#  - fact_gate 비YMYL 버킷 → detect_category 오탐으로 인한 무근거_YMYL 하드 차단 방지
#    (상품 리뷰는 재정·건강 직결 YMYL 이 아니고, 가격 수치는 추출 데이터가 근거).
#  - settings.QV_CATEGORY_OVERRIDES["상품리뷰"] 로 하한 완화 → 날조 압력 감소.
_PRODUCT_CATEGORY = "상품리뷰"

_SLUG_SAFE_RE = re.compile(r"[^\w가-힣\-]", re.UNICODE)

# markdown_to_naver_html 이 자동 삽입하는 빈 figure 슬롯(배치 이미지 dispatcher 용).
# 상품 이미지는 image_paths+_force_image_insert 업로드 경로로 들어가므로(단건 발행은
# dispatcher 를 안 탐), 이 빈 슬롯을 제거해 발행 본문에 빈 이미지 박스가 남지 않게 한다
# (experience_writer 와 동일 처리).
_FIGURE_RE = re.compile(r"<figure\b[^>]*>.*?</figure>\s*", re.IGNORECASE | re.DOTALL)

# 대가성 문구가 이미 본문에 있는지 판정할 때 쓰는 안정적 표지(문구를 바꿔도 걸리게).
_DISCLOSURE_MARKERS = ("쇼핑커넥트", "수수료", "제휴", "파트너스")
_DEFAULT_DISCLOSURE = ("이 글은 네이버 쇼핑커넥트 활동의 일환으로, 구매 시 일정액의 "
                       "수수료를 제공받을 수 있습니다.")


# ──────────────────────────────────────────────────────────────────────
# settings / 슬러그 / 키워드 (순수)
# ──────────────────────────────────────────────────────────────────────
def _disclosure_text() -> str:
    """공정위 대가성 표시 문구(settings 우선, 비면 기본). 항상 비어 있지 않게 보장."""
    try:
        from config import settings as _s
        text = str(getattr(_s, "SHOPPING_DISCLOSURE_TEXT", "") or "").strip()
        if text:
            return text
    except (ImportError, AttributeError):
        pass
    return _DEFAULT_DISCLOSURE


def _resolve_affiliate_url(candidate: str) -> str:
    """제휴 링크 확정 — 후보(URL 검증) → settings.SHOPPING_AFFILIATE_LINK(URL 검증).

    깨진 값·상품명·원본 상품 URL 을 제휴 링크로 쓰지 않는다(C-1: 커미션 무결성).
    둘 다 URL 이 아니면 빈 문자열을 돌려준다(호출부가 affiliate_missing 로 정직히 고지).
    """
    cand = str(candidate or "").strip()
    if is_valid_url(cand):
        return cand
    try:
        from config import settings as _s
        fallback = str(getattr(_s, "SHOPPING_AFFILIATE_LINK", "") or "").strip()
    except (ImportError, AttributeError):
        fallback = ""
    return fallback if is_valid_url(fallback) else ""


def _slugify(title: str, target_date: Optional[str] = None) -> str:
    """'{date}_{kebab}' 슬러그. harness_emitter 규약과 정합(가능하면 재사용)."""
    try:
        from core.harness_emitter import make_slug
        return make_slug(title, target_date)
    except Exception:  # noqa: BLE001 — 프로필 미로드 등, 로컬 폴백
        d = target_date or date.today().strftime("%Y-%m-%d")
        base = re.sub(r"\s+", "-", (title or "").strip())
        base = _SLUG_SAFE_RE.sub("", base).strip("-")[:60]
        return f"{d}_{base or 'product-post'}"


def _primary_keyword(title: str, category: str = "") -> "tuple[Optional[str], List[str]]":
    """quality_validator.validate 와 동일한 규칙으로 제목의 핵심 키워드를 뽑는다."""
    clean = re.sub(r"^\d{4}-\d{2}-\d{2} — ", "", title or "")
    clean = re.sub(r"^\d{4}\s*", "", clean)
    words = re.findall(r"[가-힣]{2,}", clean)
    try:
        from core.quality_validator import _STOP_WORDS, _strip_josa
    except (ImportError, AttributeError):
        _STOP_WORDS = frozenset()

        def _strip_josa(word: str) -> str:
            return word

    seen: set = set()
    keywords: List[str] = []
    for word in words:
        stripped = _strip_josa(word)
        if stripped and stripped not in seen and stripped not in _STOP_WORDS and len(stripped) >= 2:
            seen.add(stripped)
            keywords.append(stripped)
    keywords = keywords[:3]
    if not keywords:
        return None, []
    return keywords[0], keywords


# ──────────────────────────────────────────────────────────────────────
# 상품 사실 블록 / 결정론적 표 (데이터로만 — 무근거 날조 0)
# ──────────────────────────────────────────────────────────────────────
def _fmt_price(value: object) -> Optional[str]:
    try:
        return f"{int(value):,}원"
    except (ValueError, TypeError):
        return None


def _spec_rows(product: dict) -> "List[tuple[str, str]]":
    """상품 데이터에서 확인된 값만 (라벨, 값) 행으로. 없는 값은 넣지 않는다."""
    rows: "List[tuple[str, str]]" = []
    name = str(product.get("name") or "").strip()
    if name:
        rows.append(("상품명", name))
    price = _fmt_price(product.get("price"))
    if price:
        high = _fmt_price(product.get("price_high"))
        rows.append(("가격", f"{price}부터" if not high or high == price else f"{price} ~ {high}"))
    if str(product.get("brand") or "").strip():
        rows.append(("브랜드", str(product["brand"]).strip()))
    if str(product.get("mall") or "").strip():
        rows.append(("판매처", str(product["mall"]).strip()))
    if str(product.get("category_hint") or "").strip():
        rows.append(("분류", str(product["category_hint"]).strip()))
    rating = product.get("rating")
    review_count = product.get("review_count")
    if rating is not None:
        label = f"{rating}점"
        if review_count:
            label += f" (리뷰 {int(review_count):,}개)"
        rows.append(("평점", label))
    return rows


def _spec_table_markdown(product: dict) -> str:
    """제품 정보 마크다운 표 — require_table 게이트를 결정론적으로 충족 + 100% 데이터 근거."""
    rows = _spec_rows(product)
    if not rows:
        return ""
    lines = ["| 항목 | 내용 |", "|---|---|"]
    for label, value in rows:
        safe = str(value).replace("|", "/").replace("\n", " ")[:80]
        lines.append(f"| {label} | {safe} |")
    return "\n".join(lines)


def _product_facts_block(product: dict) -> str:
    """모델에게 넘길 '상품 사실' — 이 안에 있는 것만 근거로 삼게 한다."""
    parts: List[str] = []
    for label, value in _spec_rows(product):
        parts.append(f"- {label}: {value}")
    desc = str(product.get("description") or "").strip()
    if desc:
        parts.append(f"- 공식 설명(요약): {desc[:200]}")
    return "\n".join(parts) or "- (구조화된 상품 정보가 매우 적음 — 감상·구매 팁 위주로 짧게)"


# ──────────────────────────────────────────────────────────────────────
# 프롬프트 (content_generator 블록 재사용 + 날조 가드)
# ──────────────────────────────────────────────────────────────────────
def _build_prompt(product: dict, category: str, persona: Optional[str]) -> str:
    """상품 데이터로 제휴 리뷰를 쓰는 프롬프트. 브랜드/전략 블록 재사용 + 무근거 가드."""
    from core.content_generator import _brand_prompt_bits, _load_prompt_strategy

    today = date.today().strftime("%Y년 %m월 %d일")
    name = str(product.get("name") or "상품").strip()
    bb = _brand_prompt_bits(today, name)
    strategy = _load_prompt_strategy(category)

    strategy_lines: List[str] = []
    if persona:
        strategy_lines.append(f"- 독자 페르소나: {persona}")
    elif strategy.get("persona"):
        strategy_lines.append(f"- 독자 페르소나: {strategy['persona']}")
    if strategy.get("tone"):
        strategy_lines.append(f"- 톤앤매너: {strategy['tone']}")
    if strategy.get("forbidden_words"):
        strategy_lines.append(
            "- 금지 표현(절대 사용 금지): " + ", ".join(strategy["forbidden_words"]))
    strategy_block = "\n".join(strategy_lines)

    return f"""당신은 네이버 블로그 "{bb['name']}"의 {bb['role']}입니다.
아래 '상품 사실'만을 근거로, 실제 사용해 본 사람의 눈높이에서 **정직한 상품 리뷰**를 씁니다.

[상품 사실 — 이 안에 있는 것만 근거로 삼을 것]
{_product_facts_block(product)}

[⚠️ 무근거 배제 — 최우선 원칙(신뢰가 우리 차별점)]
- 위 '상품 사실'에 없는 **사양·성분·용량·성능 수치·인증·효능·비교우위**를 절대 지어내지 마세요.
- 데이터에 가격/평점이 없으면 임의의 숫자를 만들지 말고 "판매처에서 확인" 같은 표현으로 두세요.
- 사용 소감은 일반적·주관적으로("이런 제품군은 보통…", "구매 전 확인하면 좋은 점") 쓰고,
  구체적 제품 특성을 단정하지 마세요. 사실이 빈약하면 문장을 늘리지 말고 짧게 쓰세요.
- 과장·허위 광고 표현(최고, 유일, 100%, 완벽, 부작용 없음 등)을 쓰지 마세요.

{strategy_block}

[구성 — 반드시 이 골격]
- intro: 도입부 2~3문장. 어떤 상품을 왜 골라 보는지. 담백한 훅 1문장 포함.
- sections: **2~3개**. 각 섹션은 heading(짧은 문장, 이모지 없이)과 body(3~5문장).
  예: 어떤 사람에게 맞는지 / 고를 때 확인할 점 / 사용 상황 팁. (사양 날조 금지)
- outro: 마무리 2~3문장. 솔직한 총평과 누구에게 추천하는지.
- faq: 구매 예정자가 궁금할 Q&A **정확히 3개**. 답변은 결론부터 2문장 이내.
- summary: 한 줄 요약(50자 이내).
- tags: 해시태그 10개(상품·브랜드·용도 기반, # 없이 단어만).

[문체]
- "~입니다/~합니다" 체. 한 문단 3~5문장, 벽 텍스트 금지.
- 상품명("{name}")을 도입부 첫 문장과 본문에 3회 이상 자연스럽게 등장시키세요.
- AI 티 나는 표현 자제. 사람처럼 리듬 변화.

반드시 아래 JSON 형식으로만 응답(마크다운 코드블록 없이):
{{
  "title": "제목 (상품명이 드러나게, 연도 불필요)",
  "intro": "도입부",
  "sections": [{{"heading": "섹션 제목", "body": "섹션 본문"}}],
  "outro": "마무리",
  "faq": [{{"q":"질문1","a":"답변1"}}, {{"q":"질문2","a":"답변2"}}, {{"q":"질문3","a":"답변3"}}],
  "summary": "한 줄 요약",
  "tags": ["태그1","...","태그10"]
}}"""


# ──────────────────────────────────────────────────────────────────────
# 응답 → 마크다운 조립
# ──────────────────────────────────────────────────────────────────────
def _faq_block(faq: object) -> str:
    """FAQ 3개 이상 보증. 모델이 부족하면 담백한 기본 Q&A 로 채운다."""
    items: "List[tuple[str, str]]" = []
    if isinstance(faq, list):
        for entry in faq:
            if isinstance(entry, dict):
                q = str(entry.get("q") or "").strip()
                a = str(entry.get("a") or "").strip()
                if q and a:
                    items.append((q, a))
    defaults = [
        ("어디에서 구매하나요?", "본문 하단의 링크에서 판매처와 최신 가격을 확인하실 수 있습니다."),
        ("가격은 어떻게 되나요?", "시점에 따라 달라질 수 있어, 구매 전 판매처에서 최신 가격을 확인하세요."),
        ("어떤 분께 추천하나요?", "비슷한 상품을 찾고 계신 분께 참고가 될 만한 제품입니다."),
    ]
    for q, a in defaults:
        if len(items) >= 3:
            break
        items.append((q, a))
    lines: List[str] = []
    for idx, (q, a) in enumerate(items[:5], start=1):
        lines.append(f"**Q{idx}. {q}**")
        lines.append(a)
        lines.append("")
    return "\n".join(lines).rstrip()


def _pad_tags(tags: object, keywords: List[str], product: dict) -> List[str]:
    """태그 10개 보증(중복 제거). 모델이 부족하면 키워드·브랜드·분류에서 보충."""
    result: List[str] = []
    seen: set = set()

    def _add(raw: object) -> None:
        tag = re.sub(r"[^\w가-힣]", "", str(raw))
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)

    if isinstance(tags, list):
        for tag in tags:
            _add(tag)
    for kw in keywords:
        _add(kw)
    for extra in (product.get("brand"), product.get("mall")):
        _add(extra)
    generic = ["추천", "리뷰", "후기", "쇼핑", "가성비", "비교", "구매가이드", "인기", "상품추천", "선물"]
    for g in generic:
        if len(result) >= 10:
            break
        _add(g)
    return result[:10]


def _assemble_markdown(parsed: dict, product: dict, title: str, category: str,
                       disclosure: str,
                       affiliate_present: bool = True) -> "tuple[str, List[str]]":
    """모델 응답 + 상품 데이터를 QA 통과형 마크다운으로 조립.

    대가성 문구는 **맨 위**에 둔다(quality_validator._trim_content 는 뒤에서부터 잘라
    FAQ·마지막 섹션을 보호하므로, 상단 문구는 절삭에서 살아남는다).
    """
    intro = str(parsed.get("intro") or "").strip()
    outro = str(parsed.get("outro") or "").strip() or "필요하신 분께 참고가 되었으면 합니다."

    sections = parsed.get("sections")
    section_blocks: List[str] = []
    if isinstance(sections, list):
        for sec in sections[:4]:
            if not isinstance(sec, dict):
                continue
            heading = str(sec.get("heading") or "").strip()
            body = str(sec.get("body") or "").strip()
            if heading and body:
                section_blocks.append(f"## {heading}\n\n{body}")
    if not section_blocks:
        # 폴백: 최소 2개 섹션(무근거 없이 담백하게).
        section_blocks = [
            "## 어떤 상품인지\n\n" + (intro or f"{title}을(를) 소개합니다."),
            "## 구매 전 확인 포인트\n\n판매처에서 최신 가격과 상세 정보를 확인해 보시길 권합니다.",
        ]

    primary_kw, keywords = _primary_keyword(title, category)
    tags = _pad_tags(parsed.get("tags"), keywords, product)

    blocks: List[str] = []
    # ① 대가성 표시(상단, 결정론) — 모델 응답과 무관하게 항상.
    blocks.append(disclosure)
    if intro:
        blocks.append(intro)
    # ② 제품 정보 표(데이터 근거만) — require_table 충족.
    spec_table = _spec_table_markdown(product)
    if spec_table:
        blocks.append("## 제품 정보 한눈에 보기")
        blocks.append(spec_table)
    # ③ 본문 섹션
    blocks.extend(section_blocks)
    # ④ FAQ
    blocks.append("## 자주 묻는 질문")
    blocks.append(_faq_block(parsed.get("faq")))
    # ⑤ 마치며 + CTA 리드인. 제휴 링크가 있을 때만 '아래 링크' 안내를 넣는다(링크가 없는데
    # '아래 링크'라고 쓰면 거짓 안내 — affiliate_missing 이면 링크 없는 문장으로 바꾼다).
    blocks.append("## 마치며")
    if affiliate_present:
        blocks.append(outro + "\n\n아래 링크에서 최신 가격과 상세 정보를 확인하실 수 있습니다.")
    else:
        blocks.append(outro + "\n\n구매 전 판매처에서 최신 가격과 상세 정보를 확인해 보시길 권합니다.")

    content = "\n\n".join(b for b in blocks if b)
    content = _ensure_keyword_presence(content, primary_kw)
    content += "\n\n" + " ".join(f"#{t}" for t in tags)
    return content, tags


def _ensure_keyword_presence(content: str, primary_kw: Optional[str]) -> str:
    """핵심 키워드가 본문 3회·첫 300자에 있게 결정론적으로 보증(과하지 않게)."""
    if not primary_kw:
        return content
    despace = lambda s: re.sub(r"\s+", "", s)  # noqa: E731
    kw_ds = despace(primary_kw)
    count = despace(content).count(kw_ds)
    early = kw_ds in despace(content[:300])
    if not early:
        content = f"{primary_kw} 구매를 고민 중이라면 이 리뷰가 참고가 되길 바랍니다.\n\n" + content
        count += 1
    if count < 3:
        need = 3 - count
        topups = [
            f"{primary_kw}의 특징을 사용자 눈높이에서 짚어 봅니다.",
            f"{primary_kw}을(를) 고를 때 확인하면 좋은 점을 정리했습니다.",
        ][:need]
        if topups:
            content = content.rstrip() + "\n\n" + "\n\n".join(topups)
    return content


# ──────────────────────────────────────────────────────────────────────
# HTML 후처리 — 대가성/CTA 결정론적 주입 (URL 은 마크다운 정규식을 안 거치게)
# ──────────────────────────────────────────────────────────────────────
def _has_disclosure(text: str) -> bool:
    """대가성 문구(또는 그 표지)가 존재하는지."""
    return any(marker in (text or "") for marker in _DISCLOSURE_MARKERS)


def _disclosure_html(disclosure: str) -> str:
    """상단 대가성 고지 박스(회색, 눈에 띄되 방해되지 않게)."""
    from html import escape
    return (
        '<p style="background:#f5f5f5;border:1px solid #e0e0e0;border-radius:8px;'
        'padding:10px 14px;margin:0 0 16px;color:#666;font-size:13px;line-height:1.7;">'
        + escape(disclosure) + '</p>\n')


def _inject_disclosure_html(html: str, disclosure: str) -> str:
    """content_html 에 대가성 문구가 없으면 **맨 앞에 강제 주입**(하드 게이트).

    마크다운 상단 문구가 정상 렌더되면 이미 존재하므로 no-op. 어떤 경로(모델 누락·
    절삭·렌더 이상)로 사라졌더라도 이 함수가 결정론적으로 보장한다.
    """
    if _has_disclosure(html):
        return html
    return _disclosure_html(disclosure) + (html or "")


def _inject_affiliate_cta_html(html: str, affiliate_url: str, product_name: str) -> str:
    """제휴 링크를 클릭 가능한 CTA 박스로 본문 끝에 결정론적 삽입(커미션 귀속).

    URL 은 markdown_to_naver_html 의 표/강조 정규식을 거치지 않도록 렌더 후 주입한다.
    affiliate_url 이 **진짜 URL 이 아니면(빈 값·상품명 등) 삽입하지 않는다** — 깨진
    href(예: <a href="상품명">) 를 만들지 않는다(C-1: 커미션 무결성·조용한 실패 제거).
    """
    url = (affiliate_url or "").strip()
    if not is_valid_url(url):
        return html or ""
    from html import escape
    safe_url = escape(url, quote=True)
    label = escape((product_name or "상품").strip()[:40])
    cta = (
        '<div style="text-align:center;margin:24px 0;">'
        f'<a href="{safe_url}" target="_blank" rel="nofollow sponsored noopener" '
        'style="display:inline-block;background:#1a73e8;color:#fff;text-decoration:none;'
        'padding:14px 28px;border-radius:8px;font-size:16px;font-weight:700;">'
        f'👉 {label} 최신 가격·상세 정보 보러 가기</a></div>\n'
    )
    return (html or "") + cta


# ──────────────────────────────────────────────────────────────────────
# QA 게이트 (experience_writer 와 동일 계약 — validate 로 PASS/FAIL 만)
# ──────────────────────────────────────────────────────────────────────
def _apply_qa_gate(data: dict, title: str) -> None:
    """quality_validator.validate 로 PASS/FAIL 판정(재생성 없음). 실패면 발행 차단 신호."""
    try:
        from core.quality_validator import validate
        passed, failures = validate(data)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        log.debug("[product_writer] QA 게이트 스킵(로드 실패): %s", exc)
        return
    data["_qa_passed"] = bool(passed)
    data["_qa_failures"] = list(failures)
    if not passed:
        reason = "; ".join(failures)[:200] or "qa_failed"
        data["_qa_failed"] = True
        data["_qa_failed_reason"] = reason
        data["_failed"] = True
        data.setdefault("_failed_reason", "qa_failed")
        log.warning("[product_writer] ⛔ QA 미통과 — 발행 차단: %s", reason)


def _fallback_parsed(product: dict) -> dict:
    """집필 응답이 없을 때 데이터만으로 최소 구조(발행 여부는 QA 가 판단)."""
    name = str(product.get("name") or "상품").strip()
    return {
        "title": name,
        "intro": f"{name}을(를) 사용자 눈높이에서 정직하게 살펴봅니다.",
        "sections": [
            {"heading": "어떤 상품인지", "body": f"{name}의 기본 정보를 정리했습니다. "
             "구체적인 사양은 판매처에서 확인하시길 권합니다."},
            {"heading": "구매 전 확인 포인트", "body": "가격과 배송, 판매처 신뢰도를 "
             "함께 확인하면 실패 없는 선택에 도움이 됩니다."},
        ],
        "outro": "필요하신 분께 참고가 되었으면 합니다.",
        "faq": [],
        "summary": "",
        "tags": [],
    }


# ──────────────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────────────
def generate_product_post(product: dict,
                          affiliate_url: str,
                          category: Optional[str] = None,
                          persona: Optional[str] = None,
                          stop_flag=None) -> dict:
    """상품 데이터 → 제휴 리뷰 글 data dict(photo_publish.publish_photo_run 호환).

    Args:
        product: product_extractor.extract_product 반환 dict(name·price·images·
                 image_paths·mall·brand·category_hint 등).
        affiliate_url: CTA 후보 제휴 링크. URL 검증 통과분만 채택하고, 아니면
                       settings.SHOPPING_AFFILIATE_LINK 로 폴백. 둘 다 없으면 CTA 를
                       삽입하지 않고 affiliate_missing=True 로 정직히 표시(커미션 0 고지).
        category: 카테고리 강제(없으면 비YMYL _PRODUCT_CATEGORY 기본).
        persona: 독자 페르소나 강제(없으면 전략 기본).
        stop_flag: threading.Event 유사 객체.

    Returns:
        {title, content, content_html, summary, tags, category, sources,
         image_paths, _force_image_insert, affiliate_url, affiliate_missing,
         product, slug, web_search_done, (QA 실패 시 _qa_failed/_failed ...)}.
        affiliate_missing=True 이면 유효 제휴 링크가 없어 수수료가 발생하지 않는 글이다
        (호출부/GUI 가 사용자에게 고지해야 함).
    """
    if not isinstance(product, dict) or not str(product.get("name") or "").strip():
        return {
            "title": "", "content": "", "content_html": "", "tags": [],
            "category": category or _PRODUCT_CATEGORY, "sources": [],
            "image_paths": [], "web_search_done": False,
            "_failed": True, "_failed_reason": "no_product",
        }

    cat = category or _PRODUCT_CATEGORY
    disclosure = _disclosure_text()

    from core.content_generator import _call_claude, markdown_to_naver_html
    from core.photo_analyst import _extract_json_object

    prompt = _build_prompt(product, cat, persona)
    # [설계도 주도 변주] AI 티 제거 코어(훅·product 말투·블랙리스트·제목 아키타입)를 덧붙인다.
    #   장르 product 명시. OFF/실패면 "" → 프롬프트 바이트 동일. ⚠️ 대가성 표시·제휴 CTA·
    #   affiliate_missing 고지는 _assemble_markdown/HTML 후처리에서 렌더 후 강제 삽입되므로
    #   이 프롬프트 주입과 무관하게 그대로 보존된다(공정위 안전망 무손).
    _pname = str(product.get("name") or "상품").strip()
    from core.content_generator import blueprint_core_for as _bp_core
    _core = _bp_core(_pname, cat, genre="product")
    if _core:
        prompt += "\n\n" + _core
    # [안티-AI-티 규칙 Phase C] ANTI_AI_TELL_PROMPT OFF(기본)면 "" → 프롬프트 바이트 동일.
    try:
        from core.content_generator import _anti_ai_tell_block as _atb_fn
        _atb = _atb_fn()
        if _atb:
            prompt += "\n\n" + _atb
    except Exception:  # noqa: BLE001 — 블록 조회 실패는 기존 프롬프트 유지
        pass
    raw = _call_claude(prompt, use_web_search=False, stop_flag=stop_flag, category=cat)

    if stop_flag is not None and getattr(stop_flag, "is_set", lambda: False)():
        return {
            "title": str(product.get("name") or ""), "content": "[중지됨]",
            "content_html": "", "tags": [], "category": cat, "sources": [],
            "image_paths": list(product.get("image_paths") or []),
            "web_search_done": False, "_stopped": True,
        }

    parsed = _extract_json_object(raw) or {}
    if not parsed or not (parsed.get("sections") or parsed.get("intro")):
        log.warning("[product_writer] 집필 응답 파싱 실패 — 데이터 기반 폴백으로 최소 조립")
        parsed = _fallback_parsed(product)

    title = str(parsed.get("title") or product.get("name") or "").strip()
    if not title:
        title = str(product.get("name") or "상품 리뷰").strip()[:40]

    # 제휴 링크 확정(URL 검증 → settings 폴백). 깨진 값/상품명은 절대 링크로 안 씀(C-1).
    final_affiliate = _resolve_affiliate_url(affiliate_url)
    affiliate_missing = not final_affiliate
    if affiliate_missing:
        log.warning("[product_writer] ⚠ 유효한 제휴 링크 없음 — 수수료 미발생 글로 생성"
                    "(CTA 링크 미삽입, 정직 고지)")

    content, tags = _assemble_markdown(
        parsed, product, title, cat, disclosure,
        affiliate_present=not affiliate_missing)

    image_paths = [str(p) for p in (product.get("image_paths") or []) if p]

    # 출처 기록(2026-09-23) — 사용자가 **직접 넣은 상품 링크**에서 이번 실행이 내려받은
    # 대표 이미지다. 발행 경계가 출처 없는 이미지를 막으므로 이 지점에서 기록한다.
    # (외부 메타데이터의 플래그가 아니라 '사용자가 이 링크로 상품 글을 만들라고 지시한
    #  경로'가 근거다. 기록은 파일 내용에 묶인다.)
    try:
        from core import image_asset_guard as _guard
        _guard.mark_many(image_paths, _guard.KIND_USER,
                         producer="product_extractor", slug=_slugify(title))
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 집필을 막지 않는다
        log.debug(f"[product_writer] 상품 이미지 출처 기록 실패({type(exc).__name__})")

    data = {
        "title": title,
        "content": content,
        "summary": str(parsed.get("summary") or "")[:80],
        "tags": tags,
        "category": cat,
        "sources": [],
        "image_paths": image_paths,
        "slug": _slugify(title),
        # 상품 데이터가 근거 — 웹검색 아님. 비YMYL 라 fact_gate 통과.
        "web_search_done": False,
        # 상품 글 경로에서만 이미지 삽입을 켠다(전역 SKIP_IMAGE_INSERT 불변).
        "_force_image_insert": True,
        "_source": "product_ingest",
        # 확정된 제휴 링크(URL 검증 통과분만). 미확보면 빈 문자열 + affiliate_missing=True.
        "affiliate_url": final_affiliate,
        "affiliate_missing": affiliate_missing,
        "product": {k: product.get(k) for k in
                    ("name", "price", "mall", "brand", "product_url",
                     "product_id", "source")},
    }

    # ⚠️ 순서 — QA 게이트를 **먼저**(validate 가 초과분을 content 에서 제자리 트림).
    _apply_qa_gate(data, title)

    final_content = data["content"]
    html = markdown_to_naver_html(final_content, title=title, category=cat)
    # 자동 figure 슬롯 제거 — 상품 이미지는 image_paths 업로드 경로로 들어간다(빈 박스 방지).
    html = _FIGURE_RE.sub("", html)
    # 대가성 표시 하드 게이트 — 렌더 후에도 반드시 존재하게(모델이 빼도 코드가 강제).
    html = _inject_disclosure_html(html, disclosure)
    # 제휴 링크 CTA — 클릭 가능 링크로 결정론적 삽입.
    html = _inject_affiliate_cta_html(html, data["affiliate_url"], title)
    data["content_html"] = html

    log.info("[product_writer] 완료: '%s' %d자 · 이미지 %d장 · QA %s",
             title[:30], len(final_content), len(image_paths),
             "FAIL" if data.get("_qa_failed") else "PASS")
    return data


__all__ = ["generate_product_post"]
