"""recipe_extractor — 생성된 본문에서 BodyRecipe / InfoRecipe 자동 추출.

auto_scheduler 가 ImageDispatcher.dispatch(...) 를 호출할 때 본문 구조를 그대로
이미지 슬롯에 반영하기 위한 모듈. content_generator 가 만든 markdown 또는
HTML 본문을 입력으로 받아 다음 패턴을 인식한다.

  - 5단계 흐름:  "1. ..." / "1단계 ..." / "- 1단계 ..." 형태 — five_step BodyRecipe
  - Before/After 비교: "비교" / "Before"·"After" / "현재 vs" 같은 H2 키워드 — before_after
  - 자가 진단/체크리스트: "체크리스트" / "자가진단" — self_diagnosis BodyRecipe
  - FAQ:        "FAQ" / "자주 묻는" H2 아래 Q/A 페어 — faq InfoRecipe
  - 비교 InfoRecipe: 표(<table>) 가 있는 섹션
  - 체크리스트 InfoRecipe: "준비 서류" / "체크리스트" 섹션

본문에서 신호가 부족하면 카테고리 기본 폴백 recipe 를 채워 dispatcher 호출
규약(2 BodyRecipe + 3 InfoRecipe / 디지털 +1+1) 을 항상 만족시킨다.

부수 효과 0 — codex / Selenium / 네이버 호출 일체 없음.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Optional

# codex_cli_bot 의 dataclass 를 그대로 재사용 (BodyRecipe / InfoRecipe).
# Day 1 이후 명세가 안정화돼 있어 동일 모듈을 참조하는 게 정합.
#
# 이 모듈은 auto_scheduler 가 `from core.recipe_extractor` 로 임포트한다. 그 경로에서는
# naver_blog_v3 의 부모 디렉토리가 sys.path 에 없어 아래 절대 임포트가
# ModuleNotFoundError('naver_blog_v3') 로 실패한다(2026-06-29 로그상 20/20건 이미지 스킵).
# codex_cli_bot / image_dispatcher 와 동일하게 프로젝트 루트를 선삽입해 해소한다. (2026-07-03 버그픽스)
_PKG_ROOT = Path(__file__).resolve().parent.parent          # naver_blog_v3/
_PROJECT_ROOT = _PKG_ROOT.parent                            # naver_blog_v3 의 부모
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from naver_blog_v3.core.codex_cli_bot import (  # noqa: E402
    BodyRecipe,
    InfoRecipe,
)

# ──────────────────────────────────────────────────────────────────────────────
# 패턴
# ──────────────────────────────────────────────────────────────────────────────

# H2 본문 추출 — content_generator 의 markdown_to_naver_html 가 만드는 형식
# <h2 style="...">텍스트</h2> 와 markdown ## 텍스트 둘 다 매칭.
_H2_HTML_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
_H2_MD_RE = re.compile(r"^##\s+(.+)$", re.MULTILINE)

# 단계 패턴 — HTML 변환 후 한 줄로 합쳐진 본문도 잡히도록 line anchor 가 아닌
# whitespace boundary 를 사용한다. desc 는 다음 단계 마커 또는 H2/끝까지.
_STEP_NUMBERED_RE = re.compile(
    r"(?:^|[\s\.\)·,;])(?:[-•*]\s*)?(\d+)[\.\)]\s*(.+?)"
    r"(?=(?:[\s\.\)·,;](?:[-•*]\s*)?\d+[\.\)]|\n\s*##|\Z))",
    re.DOTALL,
)
_STEP_KOREAN_RE = re.compile(
    r"(?:^|[\s\.\)·,;])(?:[-•*]\s*)?(\d+)\s*단계\s*[:：·]?\s*(.+?)"
    r"(?=(?:[\s\.\)·,;](?:[-•*]\s*)?\d+\s*단계|\n\s*##|\Z))",
    re.DOTALL,
)

# FAQ Q/A — 마크다운: "Q. ...?" / "A. ..." 또는 "Q1: ..." 형태
_FAQ_QA_RE = re.compile(
    r"(?:^|\n)\s*Q\s*\d*\s*[\.\:：]?\s*(?P<q>.+?)\s*\n\s*A\s*\d*\s*[\.\:：]?\s*(?P<a>.+?)(?=\n\s*Q|\n\s*##|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# 체크리스트 — "□ 항목" / "- 항목" 패턴
_CHECKLIST_BULLET_RE = re.compile(
    r"(?:^|\n)\s*[□☑✅\-•*]\s+(.+?)(?=\n|$)",
)

# 표(<table>) 시그널 — 비교 InfoRecipe 후보
_TABLE_RE = re.compile(r"<table\b", re.IGNORECASE)

# 섹션 키워드
_FIVE_STEP_HEADING_HINTS = ("단계", "절차", "방법", "흐름", "step")
_BEFORE_AFTER_HEADING_HINTS = ("vs", "비교", "차이", "before", "after", "전후")
_SELF_DIAG_HEADING_HINTS = ("자가진단", "자가 진단", "체크리스트", "체크 리스트", "체크사항")
_FAQ_HEADING_HINTS = ("FAQ", "자주 묻는", "자주묻는", "Q&A", "Q & A")
_COMPARE_HEADING_HINTS = ("비교", "차이", "vs", "옵션", "선택지")
_CHECKLIST_HEADING_HINTS = ("체크리스트", "준비 서류", "준비서류", "유의사항", "주의사항", "확인 사항")


# ──────────────────────────────────────────────────────────────────────────────
# 헬퍼
# ──────────────────────────────────────────────────────────────────────────────


def _strip_tags(text: str) -> str:
    """HTML 태그 제거 + 공백 정규화."""
    if not text:
        return ""
    cleaned = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", cleaned).strip()


def _split_sections(body: str) -> list[tuple[str, str]]:
    """본문을 (H2_제목, 섹션_본문) 튜플 목록으로 분할.

    HTML/markdown 어느 쪽이든 동작. 첫 H2 이전 도입부는 무시.
    """
    sections: list[tuple[str, str]] = []

    # 우선 HTML <h2> 시도
    matches = list(_H2_HTML_RE.finditer(body))
    if matches:
        for idx, match in enumerate(matches):
            heading = _strip_tags(match.group(1))
            start = match.end()
            end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
            sections.append((heading, body[start:end]))
        return sections

    # 마크다운 ## 시도
    matches_md = list(_H2_MD_RE.finditer(body))
    if matches_md:
        for idx, match in enumerate(matches_md):
            heading = match.group(1).strip()
            start = match.end()
            end = matches_md[idx + 1].start() if idx + 1 < len(matches_md) else len(body)
            sections.append((heading, body[start:end]))
    return sections


def _heading_matches(heading: str, hints: tuple[str, ...]) -> bool:
    if not heading:
        return False
    lowered = heading.lower()
    for hint in hints:
        if hint.lower() in lowered:
            return True
    return False


def _find_steps(section_text: str, *, limit: int = 5) -> list[dict[str, str]]:
    """섹션 안에서 5단계 흐름을 찾아 [{"step":..., "desc":...}, ...] 로 반환."""
    items: list[dict[str, str]] = []

    text = _strip_tags(section_text)
    # 한글 N단계 패턴 우선 — 더 명시적
    found_korean = list(_STEP_KOREAN_RE.finditer(text))
    if found_korean:
        for match in found_korean[:limit]:
            step_no, desc = match.group(1).strip(), match.group(2).strip()
            items.append({"step": f"{step_no}단계", "desc": desc[:80]})
        return items

    # 폴백: 숫자형 1./2./3.
    found_num = list(_STEP_NUMBERED_RE.finditer(text))
    for match in found_num[:limit]:
        step_no, desc = match.group(1).strip(), match.group(2).strip()
        items.append({"step": f"{step_no}단계", "desc": desc[:80]})
    return items


def _find_faq_pairs(section_text: str, *, limit: int = 5) -> list[dict[str, str]]:
    """섹션에서 Q/A 페어를 추출."""
    items: list[dict[str, str]] = []
    text = _strip_tags(section_text)
    for match in _FAQ_QA_RE.finditer(text):
        q = match.group("q").strip().rstrip("?").rstrip(".").strip()
        a = match.group("a").strip()
        if not q or not a:
            continue
        items.append({"q": q[:60] + ("?" if not q.endswith("?") else ""),
                      "a": a[:120]})
        if len(items) >= limit:
            break
    return items


def _find_checklist_items(section_text: str, *, limit: int = 5) -> list[dict[str, str]]:
    """섹션의 체크리스트 항목을 추출."""
    text = _strip_tags(section_text)
    items: list[dict[str, str]] = []
    for match in _CHECKLIST_BULLET_RE.finditer(text):
        line = match.group(1).strip()
        if not line or len(line) < 4:
            continue
        # "항목 — 설명" 패턴이면 분리, 아니면 q=항목, a=확인 필요.
        if "—" in line or "-" in line[2:]:
            sep_idx = max(line.find("—"), line.find("-", 2))
            if sep_idx > 0:
                q, a = line[:sep_idx].strip(), line[sep_idx + 1:].strip()
                items.append({"q": q[:60], "a": a[:120] or "준비"})
                if len(items) >= limit:
                    break
                continue
        items.append({"q": line[:60], "a": "확인 필요"})
        if len(items) >= limit:
            break
    return items


# ──────────────────────────────────────────────────────────────────────────────
# 폴백 recipe (본문에서 신호가 부족할 때 dispatcher 호출 규약 보존)
# ──────────────────────────────────────────────────────────────────────────────


def _fallback_body_recipe(category: str, title: str, idx: int) -> BodyRecipe:
    """본문에서 단계/비교를 못 찾았을 때 사용할 안전한 폴백."""
    if idx == 0:
        return BodyRecipe(
            kind="five_step",
            items=[
                {"step": "1단계", "desc": "자격 확인"},
                {"step": "2단계", "desc": "서류 준비"},
                {"step": "3단계", "desc": "신청서 작성"},
                {"step": "4단계", "desc": "제출"},
                {"step": "5단계", "desc": "결과 확인"},
            ],
            illustration_hint="a left-to-right sequence of simple everyday scenes with small friendly icons",
        )
    return BodyRecipe(
        kind="before_after",
        items=[],
        illustration_hint=(
            "a split composition — on the left a slightly worried person, "
            "on the right the same person relieved and confident"
        ),
    )


def _fallback_info_recipe(category: str, title: str, idx: int) -> InfoRecipe:
    """신호 부족 시 info 슬롯 폴백."""
    if idx == 0:
        return InfoRecipe(
            kind="faq",
            items=[
                {"q": "누가 신청할 수 있나요?", "a": "본문 자격 조건 참고"},
                {"q": "언제 신청하나요?", "a": "본문 시기 항목 참고"},
                {"q": "어디서 신청하나요?", "a": "본문 신청 방법 참고"},
            ],
        )
    if idx == 1:
        return InfoRecipe(
            kind="compare",
            items=[
                {"q": "지원 유형 차이", "a": "본문 비교 표 참고"},
                {"q": "중복 신청 가능 여부", "a": "본문 유의사항 참고"},
            ],
        )
    return InfoRecipe(
        kind="checklist",
        items=[
            {"q": "신분증 준비", "a": "본인 확인용"},
            {"q": "관련 서류 준비", "a": "본문 항목 참고"},
        ],
    )


# ──────────────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────────────


def extract_recipes_from_post(
    post_data: dict,
    category: str,
    *,
    body_slot_count: int = 2,
    info_slot_count: int = 3,
) -> tuple[list[BodyRecipe], list[InfoRecipe]]:
    """post_data 의 본문에서 BodyRecipe / InfoRecipe 자동 추출.

    Args:
        post_data: content_generator.generate_blog_post 결과. 본문은
            "content_html" (변환 후) 또는 "content" (markdown) 키에서 읽는다.
        category: examplebrand 12 카테고리 — 디지털일 때 body 3 + info 4 슬롯 호환.
        body_slot_count: dispatcher 가 기대하는 body 레시피 개수.
        info_slot_count: dispatcher 가 기대하는 info 레시피 개수.

    Returns:
        (body_recipes, info_recipes). 본문에서 못 찾은 슬롯은 카테고리
        기본 폴백으로 채워 길이를 항상 일치시킨다.
    """
    title = ""
    body = ""
    if isinstance(post_data, dict):
        title = str(post_data.get("title") or "").strip()
        body = str(
            post_data.get("content_html")
            or post_data.get("content")
            or ""
        )

    sections = _split_sections(body)

    body_recipes: list[BodyRecipe] = []
    info_recipes: list[InfoRecipe] = []
    used_section_indices: set[int] = set()

    # 1) BodyRecipe 후보 — 5단계 / Before-After / 자가진단
    for idx, (heading, section_body) in enumerate(sections):
        if len(body_recipes) >= body_slot_count:
            break

        # 5단계 흐름
        if (_heading_matches(heading, _FIVE_STEP_HEADING_HINTS)
                or _find_steps(section_body)):
            steps = _find_steps(section_body)
            if steps:
                body_recipes.append(BodyRecipe(
                    kind="five_step",
                    items=steps,
                    illustration_hint="a left-to-right sequence of simple everyday scenes with small friendly icons",
                ))
                used_section_indices.add(idx)
                continue

        # Before/After 비교 (제목 키워드 기반)
        if _heading_matches(heading, _BEFORE_AFTER_HEADING_HINTS):
            body_recipes.append(BodyRecipe(
                kind="before_after",
                items=[],
                illustration_hint=(
                    "a split composition — on the left a worried person, "
                    "on the right the same person relieved and confident"
                ),
            ))
            used_section_indices.add(idx)
            continue

        # 자가 진단 / 체크리스트
        if _heading_matches(heading, _SELF_DIAG_HEADING_HINTS):
            checklist = _find_checklist_items(section_body)
            body_recipes.append(BodyRecipe(
                kind="self_diagnosis",
                items=checklist,
                illustration_hint=(
                    "a person thoughtfully reviewing a few items, "
                    "gentle checkmark motifs, tidy desk"
                ),
            ))
            used_section_indices.add(idx)

    # 부족분 폴백
    while len(body_recipes) < body_slot_count:
        body_recipes.append(
            _fallback_body_recipe(category, title, len(body_recipes))
        )

    # 2) InfoRecipe 후보 — FAQ / 비교(표) / 체크리스트
    for idx, (heading, section_body) in enumerate(sections):
        if len(info_recipes) >= info_slot_count:
            break
        if idx in used_section_indices:
            continue

        # FAQ
        if _heading_matches(heading, _FAQ_HEADING_HINTS):
            faq_items = _find_faq_pairs(section_body)
            info_recipes.append(InfoRecipe(
                kind="faq",
                items=faq_items or _fallback_info_recipe(category, title, 0).items,
                illustration_hint="a calm friendly scene of a helpful person with a warm reassuring expression",
            ))
            used_section_indices.add(idx)
            continue

        # 비교 — 표가 있으면 우선
        if _heading_matches(heading, _COMPARE_HEADING_HINTS) or _TABLE_RE.search(section_body):
            # 표가 있으면 표를 카드 일러스트 힌트로만 사용 (items 는 빈 채로 둬도 됨)
            info_recipes.append(InfoRecipe(
                kind="compare",
                items=[],
                illustration_hint=(
                    "a calm balanced scene gently suggesting two options "
                    "side by side, soft and uncluttered"
                ),
            ))
            used_section_indices.add(idx)
            continue

        # 체크리스트
        if _heading_matches(heading, _CHECKLIST_HEADING_HINTS):
            checklist = _find_checklist_items(section_body)
            info_recipes.append(InfoRecipe(
                kind="checklist",
                items=checklist or _fallback_info_recipe(category, title, 2).items,
                illustration_hint=(
                    "a calm tidy desk scene with a few neat everyday objects"
                ),
            ))
            used_section_indices.add(idx)

    # 부족분 폴백
    while len(info_recipes) < info_slot_count:
        info_recipes.append(
            _fallback_info_recipe(category, title, len(info_recipes))
        )

    return body_recipes, info_recipes


__all__ = ["extract_recipes_from_post"]
