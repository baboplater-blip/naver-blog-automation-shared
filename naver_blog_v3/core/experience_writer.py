# core/experience_writer.py
# 📸 사진 인제스트(P0-② Phase 2) — 얇은 오케스트레이터.
# 사진 분석(photo_analyst) N개를 순서대로 프롬프트에 넣어 "사진마다 문단"을 쓰고,
# 서사 순서로 배치한 뒤 기존 data dict 계약({title, content, content_html, tags,
# category, sources})을 그대로 반환한다 → 하류(QA·중복·publish_brief·uploader)
# 전부 무수정.
#
# 재사용:
# - content_generator: _call_claude(텍스트 생성)·_brand_prompt_bits·
#   _load_prompt_strategy·markdown_to_naver_html.
# - quality_validator.validate: 재생성 없이 PASS/FAIL 판정만 걸어(사진↔문단 매핑
#   보존) _qa_failed 로 발행 차단(하드 게이트 "QA 걸림").
#
# 배치 결정(설계 판단):
# - 미리보기(runs/{slug}/draft.html·preview.html)는 실사진을 문단 사이에 끼운
#   <figure> 로 렌더(사진 수 가변 10~14, 고정 6/8 슬롯 아님).
# - 네이버 본문(content_html)은 로컬 이미지 src 없는 텍스트 → 깨진/중복 이미지 0.
# - 반환 data 에 image_paths(실사진 절대경로)와 _force_image_insert 를 실어 둔다.
#
# ⚠️ 발행 배선 상태(Phase 2 = 초안+미리보기까지):
# - 실사진→네이버 발행은 **아직 배선되지 않았다.** upload_post 를 호출하는 쪽
#   (auto_scheduler·post_refresher)은 experience_writer 를 알지 못한다. naver_uploader
#   의 게이트(_should_insert_images)는 _force_image_insert 신호를 받을 준비만 된 seam 이고,
#   그 신호를 실은 data 를 upload_post 로 넘기는 호출자는 없다.
# - 실배선은 별도 후속(P0-① attach 경로 라이브 검증과 얽혀 있음). 자동 발행 금지선 유지 —
#   이 모듈은 미리보기·runs 산출까지만 하고 발행 트리거는 사용자 몫.
from __future__ import annotations

import json
import re
import shutil
from datetime import date, datetime
from html import escape as html_escape
from pathlib import Path
from typing import List, Optional, Sequence, Union

from config.logger import get_logger
from core import photo_analyst
from core.photo_analyst import PhotoAnalysis

log = get_logger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent  # naver_blog_v3/

# 발행 안전 하한 — 이 개수 미만 사진이면 서사·글자수가 얕아 QA 통과가 어렵다(안내용).
_RECOMMENDED_MIN_PHOTOS = 4

# 사진 인제스트 실경험 글의 콘텐츠 타입 카테고리(주제 아님). 두 가지를 동시에 해결:
#  - fact_gate 비YMYL 버킷 → detect_category 오탐("주거박람회 후기"→주거 등)으로 인한
#    무근거_YMYL 하드 차단 방지(사진 글은 사진이 근거).
#  - settings.QV_CATEGORY_OVERRIDES["사진후기"] 로 넉넉한 max_chars → 사진 10~14장이
#    트림과 안 싸움. 사용자가 category 를 명시하면 그 값을 존중(YMYL 게이팅 opt-in).
_PHOTO_CATEGORY = "사진후기"


# ──────────────────────────────────────────────────────────────────────────────
# 슬러그 · 키워드
# ──────────────────────────────────────────────────────────────────────────────

_SLUG_SAFE_RE = re.compile(r"[^\w가-힣\-]", re.UNICODE)


def _slugify(title: str, target_date: Optional[str] = None) -> str:
    """'{date}_{kebab}' 슬러그. harness_emitter 규약과 정합(가능하면 그쪽 재사용)."""
    try:
        from core.harness_emitter import make_slug
        return make_slug(title, target_date)
    except Exception:  # noqa: BLE001 — profile 미로드 등, 로컬 폴백
        d = target_date or date.today().strftime("%Y-%m-%d")
        base = re.sub(r"\s+", "-", (title or "").strip())
        base = _SLUG_SAFE_RE.sub("", base).strip("-")[:60]
        return f"{d}_{base or 'photo-post'}"


def _primary_keyword(title: str, category: str = "") -> "tuple[Optional[str], List[str]]":
    """quality_validator.validate 와 동일한 규칙으로 제목의 핵심 키워드를 뽑는다.

    validate 의 keyword_density/keyword_early 게이트와 어긋나지 않게 같은 로직을
    미러링한다(핵심어를 본문에 3회·첫 300자에 넣는 보증에 사용).
    """
    clean = re.sub(r"^\d{4}-\d{2}-\d{2} — ", "", title or "")
    clean = re.sub(r"^\d{4}\s*", "", clean)
    words = re.findall(r"[가-힣]{2,}", clean)
    try:
        from core.quality_validator import _STOP_WORDS, _strip_josa
    except (ImportError, AttributeError):
        _STOP_WORDS = frozenset()

        def _strip_josa(word: str) -> str:
            return word

    seen = set()
    keywords: List[str] = []
    for word in words:
        stripped = _strip_josa(word)
        if stripped and stripped not in seen and stripped not in _STOP_WORDS and len(stripped) >= 2:
            seen.add(stripped)
            keywords.append(stripped)
    keywords = keywords[:3]
    if not keywords:
        return None, []
    primary = keywords[0]
    try:
        from core.content_generator import CATEGORY_KEYWORDS as _CK
        cat_kws = _CK.get(category or "", [])
        for kw in keywords:
            if any(kw in ck or ck in kw for ck in cat_kws):
                primary = kw
                break
    except Exception:  # noqa: BLE001
        pass
    return primary, keywords


# ──────────────────────────────────────────────────────────────────────────────
# 프롬프트 조립(기존 블록 재사용)
# ──────────────────────────────────────────────────────────────────────────────


def _photo_facts_block(analyses: Sequence[PhotoAnalysis]) -> str:
    """사진 분석을 번호 매긴 '사실' 블록으로. 각 사진의 보이는 것만 나열."""
    lines: List[str] = []
    for a in analyses:
        parts = [f"[사진 {a.order}]"]
        if a.scene:
            parts.append(f"장면: {a.scene}")
        if a.signs:
            parts.append("간판/상호: " + ", ".join(a.signs[:6]))
        if a.menu:
            parts.append("메뉴/가격: " + ", ".join(a.menu[:8]))
        if a.text_detected:
            parts.append("읽히는 글자: " + ", ".join(a.text_detected[:8]))
        if a.caption:
            parts.append(f"캡션: {a.caption}")
        if not a.vision_ok:
            parts.append("(비전 분석 실패 — 이 사진은 일반적인 표현으로만 언급, 없는 내용 추측 금지)")
        lines.append(" · ".join(parts))
    return "\n".join(lines)


def _build_prompt(analyses: Sequence[PhotoAnalysis], title_hint: Optional[str],
                  category: str) -> str:
    """content_generator 의 브랜드/전략 블록을 재사용한 실경험 집필 프롬프트."""
    from core.content_generator import _brand_prompt_bits, _load_prompt_strategy

    today = date.today().strftime("%Y년 %m월 %d일")
    clean_hint = re.sub(r"^\d{4}-\d{2}-\d{2} — ", "", title_hint or "").strip()
    bb = _brand_prompt_bits(today, clean_hint or "사진 후기")
    strategy = _load_prompt_strategy(category)

    n = len(analyses)
    strategy_lines = []
    if strategy.get("persona"):
        strategy_lines.append(f"- 독자 페르소나: {strategy['persona']}")
    if strategy.get("tone"):
        strategy_lines.append(f"- 톤앤매너: {strategy['tone']}")
    if strategy.get("forbidden_words"):
        strategy_lines.append(
            "- 금지 표현(절대 사용 금지): " + ", ".join(strategy["forbidden_words"]))
    strategy_block = "\n".join(strategy_lines)

    title_line = (f'제목 힌트: "{clean_hint}" (자연스럽게 다듬어도 됨)'
                  if clean_hint else "제목: 사진 내용에 맞게 직접 지어 주세요")

    return f"""당신은 네이버 블로그 "{bb['name']}"의 {bb['role']}입니다.
아래는 사용자가 직접 찍은 사진 {n}장을 시간순으로 분석한 결과입니다.
이 사진들을 소재로, **실제로 다녀온 사람의 1인칭 경험 후기**를 작성하세요.

{title_line}

[사진 사실 — 시간순 {n}장 (이 안에 있는 것만 근거로 삼을 것)]
{_photo_facts_block(analyses)}

[⚠️ 무근거 배제 — 최우선 원칙]
- 위 '사진 사실'에 적힌 것만 근거로 쓰세요. 사진에 없는 상호·메뉴·가격·장소·날씨·인원을
  절대 지어내지 마세요(개떡찰떡 핵심 원칙 = 신뢰).
- 비전 분석이 실패한 사진은 "사진 속 한 장면" 정도로만 담백하게 언급하고 세부를 추측하지 마세요.
- 사실이 빈약하면 문장을 지어내지 말고 감상·분위기 위주로 짧게 쓰세요.

{strategy_block}

[구성 — 반드시 이 골격]
- intro: 도입부 2~3문장. 어디를/무엇을 다녀온 기록인지 한눈에. 담백한 훅 1문장 포함.
- photo_paragraphs: **정확히 {n}개**. 사진 순서(1→{n})와 1:1 대응.
  각 문단은 그 사진 하나의 경험을 3~5문장으로. "직접 가보니", "먹어보니" 같은 1차 경험 표현 사용.
- outro: 마무리 2~3문장. 다시 갈지/추천 여부 등 솔직한 총평.
- faq: 실제 방문 예정자가 궁금할 Q&A 정확히 3개. 답변은 결론부터 2문장 이내.
- summary: 한 줄 요약(50자 이내).
- tags: 해시태그 10개(사진·장소·주제 기반, # 없이 단어만).

[문체]
- "~입니다/~합니다" 체. 한 문단 3~5문장, 벽 텍스트 금지.
- AI 티 나는 표현 자제("사실상", "~역할을 한다", "미래는 밝다"). 사람처럼 리듬 변화.
- 제목의 핵심 키워드는 도입부 첫 문장과 본문에 3회 이상 자연스럽게 등장시킬 것.

반드시 아래 JSON 형식으로만 응답(마크다운 코드블록 없이):
{{
  "title": "제목 (연도 불필요, 실제 경험이 드러나게)",
  "intro": "도입부",
  "section_heading": "사진 서사 섹션 제목(## 로 쓸 짧은 문장, 이모지 없이)",
  "photo_paragraphs": ["사진1 문단", "사진2 문단", "... 총 {n}개"],
  "outro": "마무리",
  "faq": [{{"q":"질문1","a":"답변1"}}, {{"q":"질문2","a":"답변2"}}, {{"q":"질문3","a":"답변3"}}],
  "summary": "한 줄 요약",
  "tags": ["태그1","...","태그10"],
  "sources": []
}}"""


# ──────────────────────────────────────────────────────────────────────────────
# 응답 → 마크다운 조립(N 사진 ↔ N 문단 보증)
# ──────────────────────────────────────────────────────────────────────────────


def _pad_or_trim_paragraphs(paragraphs: List[str],
                            analyses: Sequence[PhotoAnalysis]) -> List[str]:
    """photo_paragraphs 를 정확히 사진 수에 맞춘다(모델이 N개를 안 주면 보정)."""
    n = len(analyses)
    cleaned = [str(p).strip() for p in paragraphs if str(p).strip()]
    if len(cleaned) == n:
        return cleaned
    if len(cleaned) > n:
        log.warning(f"[experience] 문단 {len(cleaned)}개 > 사진 {n}장 — 초과분 병합/절삭")
        return cleaned[:n]
    # 부족분은 해당 사진의 캡션 기반 최소 문단으로 채운다(무근거 서술 없이).
    log.warning(f"[experience] 문단 {len(cleaned)}개 < 사진 {n}장 — 캡션 기반 폴백으로 보충")
    for idx in range(len(cleaned), n):
        cap = analyses[idx].caption or f"사진 {analyses[idx].order}"
        cleaned.append(f"{cap}. 사진 속 순간을 그대로 담아 두었습니다.")
    return cleaned


def _summary_table(analyses: Sequence[PhotoAnalysis]) -> str:
    """방문 요약 마크다운 표 — require_table 게이트를 결정론적으로 충족 + 실제로 유용."""
    rows = ["| 순서 | 장면 | 메모 |", "|---|---|---|"]
    for a in analyses:
        scene = (a.scene or a.caption or "사진").replace("|", "/")[:28]
        memo_parts = []
        if a.signs:
            memo_parts.append(a.signs[0])
        if a.menu:
            memo_parts.append(a.menu[0])
        memo = (", ".join(memo_parts) or (a.caption or "")).replace("|", "/")[:28]
        rows.append(f"| {a.order} | {scene} | {memo} |")
    return "\n".join(rows)


def _faq_block(faq: object) -> str:
    """FAQ 3개 이상을 보증. 모델이 부족하게 주면 담백한 기본 Q&A 로 채운다."""
    items: List["tuple[str, str]"] = []
    if isinstance(faq, list):
        for entry in faq:
            if isinstance(entry, dict):
                q = str(entry.get("q") or "").strip()
                a = str(entry.get("a") or "").strip()
                if q and a:
                    items.append((q, a))
    defaults = [
        ("사진은 언제 찍은 건가요?", "직접 방문했을 때 순서대로 찍은 사진입니다."),
        ("다시 방문할 의향이 있나요?", "기회가 되면 다시 찾고 싶은 곳이었습니다."),
        ("어떤 분께 추천하나요?", "비슷한 곳을 찾고 계신 분께 참고가 될 것 같습니다."),
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


def _pad_tags(tags: object, keywords: List[str],
              analyses: Sequence[PhotoAnalysis]) -> List[str]:
    """태그 10개를 보증(중복 제거). 모델이 부족하면 키워드·간판에서 보충."""
    result: List[str] = []
    seen = set()

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
    for a in analyses:
        for sign in a.signs:
            _add(sign)
        if len(result) >= 10:
            break
    generic = ["일상", "기록", "후기", "사진", "나들이", "추천", "방문후기", "리뷰", "여행", "맛집"]
    for g in generic:
        if len(result) >= 10:
            break
        _add(g)
    return result[:10]


def _assemble_markdown(parsed: dict, analyses: Sequence[PhotoAnalysis],
                       title: str, category: str) -> "tuple[str, List[str]]":
    """모델 응답 + 사진 분석을 QA 통과형 마크다운으로 조립.

    Returns:
        (content_markdown, tags). 각 사진 문단 뒤에 `<!-- PHOTO:n -->` 마커를 심어
        미리보기 단계에서 실사진 <figure> 로 치환한다.
    """
    intro = str(parsed.get("intro") or "").strip()
    section_heading = str(parsed.get("section_heading") or "").strip() or "하나씩 둘러본 기록"
    outro = str(parsed.get("outro") or "").strip() or "짧게나마 다녀온 기록을 남겨 둡니다."
    paragraphs = _pad_or_trim_paragraphs(
        parsed.get("photo_paragraphs") or [], analyses)

    primary_kw, keywords = _primary_keyword(title, category)
    tags = _pad_tags(parsed.get("tags"), keywords, analyses)

    blocks: List[str] = []
    if intro:
        blocks.append(intro)
    blocks.append(f"## {section_heading}")
    for a, para in zip(analyses, paragraphs):
        blocks.append(para)
        blocks.append(f"<!-- PHOTO:{a.order} -->")
    blocks.append("## 방문 요약")
    blocks.append(_summary_table(analyses))
    blocks.append("## 자주 묻는 질문")
    blocks.append(_faq_block(parsed.get("faq")))
    blocks.append("## 마치며")
    blocks.append(outro)

    content = "\n\n".join(b for b in blocks if b)

    # 키워드 밀도 보증(validate 의 keyword_density/keyword_early 게이트 정합).
    content = _ensure_keyword_presence(content, primary_kw)

    # 해시태그(본문 맨 끝, 한 줄).
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
        content = f"{primary_kw} 다녀온 기록을 사진과 함께 남깁니다.\n\n" + content
        count += 1
    if count < 3:
        need = 3 - count
        topups = [
            f"{primary_kw} 방문을 계획 중이라면 이 기록이 참고가 되길 바랍니다.",
            f"{primary_kw}의 분위기를 사진에 담아 두었습니다.",
        ]
        additions = topups[:need]
        if additions:
            content = content.rstrip() + "\n\n" + "\n\n".join(additions)
    return content


# ──────────────────────────────────────────────────────────────────────────────
# HTML 렌더(네이버 본문 · 미리보기)
# ──────────────────────────────────────────────────────────────────────────────

_FIGURE_RE = re.compile(r"<figure\b[^>]*>.*?</figure>\s*", re.IGNORECASE | re.DOTALL)
_PHOTO_MARKER_RE = re.compile(r"<!--\s*PHOTO:(\d+)\s*-->\s*", re.IGNORECASE)


def _strip_auto_figures(html: str) -> str:
    """markdown_to_naver_html 이 자동 삽입한 빈 figure 슬롯을 제거(사진 글은 실사진만)."""
    return _FIGURE_RE.sub("", html)


def _photo_rel_name(analysis: PhotoAnalysis) -> str:
    """미리보기용 상대 경로(runs/{slug}/ 기준). write_run 의 복사 대상과 일치.

    HEIC/HEIF 는 브라우저가 못 여니 .jpg 로 매핑(photo_analyst.preview_ext 단일 규칙).
    """
    ext = photo_analyst.preview_ext(Path(analysis.path).suffix)
    return f"photos/{analysis.order:02d}{ext}"


def _naver_body_html(html_no_fig: str) -> str:
    """네이버 발행 본문 — PHOTO 마커 제거한 순수 텍스트 HTML(로컬 이미지 src 0)."""
    return _PHOTO_MARKER_RE.sub("", html_no_fig)


def _preview_html(html_no_fig: str, analyses: Sequence[PhotoAnalysis]) -> str:
    """미리보기 HTML — PHOTO 마커를 실사진 <figure> 로 치환(문단 사이 배치)."""
    by_order = {a.order: a for a in analyses}

    def _replace(match: "re.Match") -> str:
        order = int(match.group(1))
        a = by_order.get(order)
        if a is None:
            return ""
        src = _photo_rel_name(a)
        caption = html_escape(a.caption or "", quote=False)
        return (
            f'<figure data-role="photo" data-n="{order}" '
            f'style="margin:18px 0;text-align:center;">'
            f'<img src="{html_escape(src, quote=True)}" alt="{caption}" '
            f'style="max-width:100%;border-radius:8px;">'
            f'<figcaption style="color:#888;font-size:13px;margin-top:6px;">'
            f'{caption}</figcaption></figure>\n'
        )

    return _PHOTO_MARKER_RE.sub(_replace, html_no_fig)


# ──────────────────────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────────────────────


def generate_experience_post(photos: Union[str, Path, Sequence[Union[str, Path]]],
                             title_hint: Optional[str] = None,
                             category: Optional[str] = None,
                             stop_flag=None,
                             analyses: Optional[Sequence[PhotoAnalysis]] = None) -> dict:
    """사진 폴더/경로목록 → 실경험 글 data dict.

    Args:
        photos: 사진 폴더 경로 또는 이미지 경로 시퀀스.
        title_hint: 제목 힌트(없으면 사진에서 생성).
        category: 카테고리 강제(없으면 비YMYL 콘텐츠 타입 _PHOTO_CATEGORY 기본).
        stop_flag: threading.Event 유사 객체.
        analyses: 이미 분석된 PhotoAnalysis 목록(주입 시 재분석 생략 — 테스트/재사용).

    Returns:
        기존 data dict 계약 + 사진 전용 확장 키:
        {title, content, content_html, summary, tags, category, sources,
         image_paths, preview_html, photo_analyses, slug, web_search_done,
         _force_image_insert, (실패 시 _failed/_qa_failed ...)}.
    """
    if analyses is None:
        analyses = photo_analyst.analyze_folder(photos, stop_flag=stop_flag)

    if not analyses:
        return {
            "title": title_hint or "사진 후기",
            "content": "", "content_html": "", "tags": [],
            "category": category or _PHOTO_CATEGORY, "sources": [],
            "image_paths": [], "preview_html": "", "photo_analyses": [],
            "web_search_done": False,
            "_failed": True, "_failed_reason": "no_photos",
        }

    if stop_flag is not None and getattr(stop_flag, "is_set", lambda: False)():
        return {
            "title": title_hint or "사진 후기",
            "content": "[중지됨]", "content_html": "", "tags": [],
            "category": category or _PHOTO_CATEGORY, "sources": [],
            "image_paths": [str(a.path) for a in analyses],
            "preview_html": "", "photo_analyses": [a.to_public() for a in analyses],
            "web_search_done": False, "_stopped": True,
        }

    from core.content_generator import _call_claude, markdown_to_naver_html

    # 사진 글은 콘텐츠 타입 카테고리(_PHOTO_CATEGORY, 비YMYL)를 기본으로 쓴다. 제목 기반
    # detect_category 는 "주거박람회 후기"→주거 같은 오탐으로 fact_gate YMYL 하드 차단을
    # 유발할 수 있어 쓰지 않는다. 사용자가 category 를 명시하면 그 값을 존중(opt-in).
    cat = category or _PHOTO_CATEGORY
    if len(analyses) < _RECOMMENDED_MIN_PHOTOS:
        log.info(f"[experience] 사진 {len(analyses)}장 — 권장 {_RECOMMENDED_MIN_PHOTOS}장 미만"
                 "(글자수·서사가 얕아 QA 미달 가능)")

    prompt = _build_prompt(analyses, title_hint, cat)
    # [설계도 주도 변주] AI 티 제거 코어(훅·place 말투·블랙리스트·제목 아키타입)를 덧붙인다.
    #   장르는 place 명시 — 실제 촬영=진짜 경험이라 allow_experience 유지(YMYL-force 로
    #   info 강등되지 않게). OFF/실패면 "" → 프롬프트 바이트 동일. 사진→문단 매핑·프라이버시는
    #   본문(_assemble)·복사 단계라 이 주입과 무관.
    _clean_hint = re.sub(r"^\d{4}-\d{2}-\d{2} — ", "", title_hint or "").strip()
    from core.content_generator import blueprint_core_for as _bp_core
    _core = _bp_core(_clean_hint or "사진 후기", cat, genre="place")
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
    parsed = photo_analyst._extract_json_object(raw) or {}

    if not parsed or not parsed.get("photo_paragraphs"):
        log.warning("[experience] 집필 응답 파싱 실패 — 캡션 기반 폴백으로 최소 조립")
        parsed = _fallback_parsed(analyses, title_hint)

    title = str(parsed.get("title") or title_hint or "").strip()
    if not title:
        title = (analyses[0].scene or analyses[0].caption or "사진 후기").strip()[:40]

    content, tags = _assemble_markdown(parsed, analyses, title, cat)

    sources = parsed.get("sources") if isinstance(parsed.get("sources"), list) else []
    image_paths = [str(a.path) for a in analyses]

    # 출처 기록(2026-09-23) — **사용자가 직접 고른 사진**이다. 이 경로(사진 폴더 선택
    # → 분석 → 집필)는 사람이 명시적으로 고른 파일만 지나간다. post_data 의 플래그
    # 같은 외부 메타데이터가 아니라 **이 선택 행위**가 근거이며, 기록은 파일 내용에
    # 묶인다(나중에 같은 경로에 다른 파일이 놓이면 기록은 무효가 된다).
    try:
        from core import image_asset_guard as _guard
        _guard.mark_many(image_paths, _guard.KIND_USER,
                         producer="photo_ingest", slug=_slugify(title))
    except Exception as exc:  # noqa: BLE001 — 기록 실패가 집필을 막지 않는다
        log.debug(f"[experience] 사진 출처 기록 실패({type(exc).__name__})")

    data = {
        "title": title,
        "content": content,
        "summary": str(parsed.get("summary") or "")[:80],
        "tags": tags,
        "category": cat,
        "sources": [str(s) for s in sources if s],
        # 사진 전용 확장(하류가 읽는 표준 키는 위쪽; 아래는 발행/미리보기 배선용).
        "image_paths": image_paths,
        "photo_analyses": [a.to_public() for a in analyses],
        "slug": _slugify(title),
        # 실경험 글은 사진 자체가 근거 — 웹검색 아님. 카테고리도 비YMYL 이라 fact_gate 통과.
        "web_search_done": False,
        # 사진 글 경로에서만 이미지 삽입을 켠다(전역 SKIP_IMAGE_INSERT 불변).
        "_force_image_insert": True,
        "_source": "photo_ingest",
    }

    # ⚠️ 순서 — QA 게이트를 **먼저** 돌린다. validate 가 초과분을 data["content"] 에서
    # 제자리 트림할 수 있으므로, content_html/preview_html 은 트림이 반영된 **최종**
    # content 로 렌더해야 한다(트림 전 내용으로 렌더하면 본문↔미리보기 데싱크).
    _apply_qa_gate(data, title)

    final_content = data["content"]
    html_full = markdown_to_naver_html(final_content, title=title, category=cat)
    html_no_fig = _strip_auto_figures(html_full)
    data["content_html"] = _naver_body_html(html_no_fig)
    data["preview_html"] = _preview_html(html_no_fig, analyses)

    log.info(f"[experience] 완료: '{title[:30]}' {len(final_content)}자 · 사진 {len(analyses)}장 "
             f"· QA {'FAIL' if data.get('_qa_failed') else 'PASS'}")
    return data


def _fallback_parsed(analyses: Sequence[PhotoAnalysis],
                     title_hint: Optional[str]) -> dict:
    """집필 응답이 없을 때 캡션만으로 최소 구조를 만든다(발행은 QA 가 판단)."""
    return {
        "title": title_hint or (analyses[0].caption or "사진 후기"),
        "intro": "직접 다녀온 기록을 사진과 함께 짧게 남깁니다.",
        "section_heading": "하나씩 둘러본 기록",
        "photo_paragraphs": [
            (a.caption or f"사진 {a.order}") + ". 그 순간을 사진에 담아 두었습니다."
            for a in analyses
        ],
        "outro": "짧게나마 다녀온 기록을 남겨 둡니다.",
        "faq": [],
        "summary": "",
        "tags": [],
        "sources": [],
    }


def _apply_qa_gate(data: dict, title: str) -> None:
    """quality_validator.validate 로 PASS/FAIL 만 판정(재생성 없음 — 사진↔문단 보존).

    실패면 _qa_failed/_failed 를 세워 하류 발행 게이트가 차단한다("QA 걸림").
    미리보기·초안은 그대로 산출되어 사용자가 확인·수정할 수 있다.
    """
    try:
        from core.quality_validator import validate
        passed, failures = validate(data)
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        log.debug(f"[experience] QA 게이트 스킵(로드 실패): {exc}")
        return
    data["_qa_passed"] = bool(passed)
    data["_qa_failures"] = list(failures)
    if not passed:
        reason = "; ".join(failures)[:200] or "qa_failed"
        data["_qa_failed"] = True
        data["_qa_failed_reason"] = reason
        data["_failed"] = True
        data.setdefault("_failed_reason", "qa_failed")
        log.warning(f"[experience] ⛔ QA 미통과 — 발행 차단: {reason}")


def write_run(data: dict, runs_root: Optional[Union[str, Path]] = None) -> Path:
    """data dict 를 runs/{slug}/ 규약으로 저장하고 실사진을 photos/ 로 복사.

    산출:
        draft.md         — 프론트매터 + 마크다운 본문
        draft.html       — 미리보기(실사진 figure) — 기존 draft.html 규약(가시)
        preview.html     — draft.html 과 동일(GUI 열기용)
        naver_body.html  — 네이버 발행 본문(텍스트, 로컬 이미지 src 0)
        photos/NN.ext    — 미리보기용 사진 복사본

    Returns:
        생성된 run 디렉토리 Path.
    """
    slug = str(data.get("slug") or _slugify(data.get("title") or "photo-post"))
    if runs_root is None:
        try:
            from config import profile as _profile
            runs_root = _profile.runs_dir()
        except Exception:  # noqa: BLE001
            runs_root = _BASE_DIR.parent / "runs"
    run_dir = Path(runs_root) / slug
    (run_dir / "photos").mkdir(parents=True, exist_ok=True)

    # 사진 복사(미리보기 상대경로 photos/NN.ext 와 정합). ⚠️ shutil.copy2 는 바이트
    # 그대로라 GPS 등 EXIF 가 잔존한다 → _copy_photo_privacy 로 메타데이터를 제거하며
    # 재저장(HEIC/HEIF 는 .jpg 로 변환). 확장자는 photo_analyst.preview_ext 규약과 일치.
    for public in data.get("photo_analyses") or []:
        order = public.get("order")
        src = public.get("path")
        if not order or not src:
            continue
        src_path = Path(src)
        if not src_path.is_file():
            continue
        ext = photo_analyst.preview_ext(src_path.suffix)
        dest = run_dir / "photos" / f"{int(order):02d}{ext}"
        _copy_photo_privacy(src_path, dest)

    front = _frontmatter(data)
    _write_text(run_dir / "draft.md", front + "\n\n" + (data.get("content") or ""))
    _write_text(run_dir / "draft.html", data.get("preview_html") or "")
    _write_text(run_dir / "preview.html", data.get("preview_html") or "")
    _write_text(run_dir / "naver_body.html", data.get("content_html") or "")
    log.info(f"[experience] runs 저장: {run_dir}")
    return run_dir


def _copy_photo_privacy(src: Path, dest: Path) -> bool:
    """사진을 dest 로 복사하되 **메타데이터(EXIF/GPS)를 제거**하며 재저장한다.

    Pillow 로 열어 exif 없이 다시 저장하면 GPS 를 포함한 모든 EXIF 가 사라진다
    (HEIC/HEIF 는 pillow-heif 가 등록돼 있으면 열려서 .jpg 로 변환·저장). 실패해도
    파이프라인을 죽이지 않는다(fail-open). ⚠️ 실패 시 원본을 그대로 copy2 하지 않는다
    — 그러면 GPS 가 유출되므로, 대신 그 사진의 미리보기 복사를 건너뛴다(경고 로그).

    Returns:
        복사(재저장) 성공 여부.
    """
    try:
        from PIL import Image
    except ImportError:
        log.warning(f"[experience] Pillow 없음 — EXIF strip 불가로 사진 복사 스킵: {dest.name}")
        return False
    try:
        with Image.open(src) as image:
            save_kwargs: dict = {}
            if dest.suffix.lower() in (".jpg", ".jpeg"):
                if image.mode not in ("RGB", "L"):
                    image = image.convert("RGB")
                save_kwargs = {"quality": 90}
            # exif= 를 넘기지 않으므로 GPS 포함 모든 메타데이터가 제거된다.
            image.save(dest, **save_kwargs)
        return True
    except (OSError, ValueError, KeyError) as exc:
        log.warning(f"[experience] 사진 메타데이터 제거 재저장 실패 — 미리보기 복사 스킵"
                    f"(원본 GPS 유출 방지) {dest.name}: {type(exc).__name__}")
        return False


def _frontmatter(data: dict) -> str:
    payload = {
        "slug": data.get("slug", ""),
        "title": data.get("title", ""),
        "category": data.get("category", "default"),
        "tags": data.get("tags", []),
        "source": "photo_ingest",
        "photo_count": len(data.get("image_paths") or []),
        "qa_passed": data.get("_qa_passed"),
        "created_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S") + "+09:00",
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return "---\n" + body + "\n---"


def _write_text(path: Path, text: str) -> None:
    """tmp → rename 원자적 텍스트 쓰기."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as fp:
        fp.write(text)
    tmp.replace(path)
