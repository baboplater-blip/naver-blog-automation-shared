# core/caption_generator.py
# examplebrand 멀티채널 팩 — 글 1편 → 4채널 캡션 + Shorts 스크립트
# Claude Max 구독 사용 (API 비용 없음 — content_generator._call_claude 재사용)
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import List, Optional

# 루트(naver_blog_v3) 를 path 에 추가 — content_generator 와 동일 패턴
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import safe_load_json
from config.logger import get_logger
from core.content_generator import _call_claude, _parse_json_response, detect_category

log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent  # naver_blog_v3/
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

TEMPLATE_DIR = BASE_DIR / "templates"
PERSONAS_PATH = TEMPLATE_DIR / "multichannel_personas.json"
OUTPUT_DIR = _DATA_DIR / "output"
SCREENSHOTS_DIR = _DATA_DIR / "screenshots"


# ─────────────────────────────────────────────────────
# 페르소나 로드
# ─────────────────────────────────────────────────────
_DEFAULT_PERSONAS = {
    "youtube_shorts": {"tone": "정보 우선", "hashtag_strategy": "도메인 5~10",
                       "forbidden_words": []},
    "instagram_reels": {"tone": "감성·따뜻함", "hashtag_count_exact": 30,
                        "forbidden_words": []},
    "tiktok": {"tone": "후킹", "hashtag_count_min": 5, "hashtag_count_max": 10,
               "forbidden_words": []},
    "shorts_script": {"tone": "30초 안에 핵심만",
                      "tts_text_target_chars": 150,
                      "h2_card_count_exact": 3,
                      "forbidden_words": []},
    "common": {"brand_name": "examplebrand",
               "default_cta": "자세한 절차는\nexamplebrand 블로그에서",
               "category_default_hashtags": {"default": ["#시니어", "#예시브랜드"]}},
}


def _personas_path() -> Path:
    """활성 프로필 브랜드 팩의 페르소나(있으면), 없으면 공유 templates. 호출시점 해석."""
    try:
        from config import brand as _brand
        return _brand.template_path("multichannel_personas.json")
    except Exception:  # noqa: BLE001
        return PERSONAS_PATH


def _load_personas() -> dict:
    """multichannel_personas.json 로드(프로필 브랜드 팩 우선). 실패 시 안전한 기본값."""
    p = _personas_path()
    data = safe_load_json(p, default=None)
    if isinstance(data, dict) and data:
        return data
    log.warning(f"multichannel_personas.json 로드 실패 — 기본값 사용: {p}")
    return _DEFAULT_PERSONAS


# ─────────────────────────────────────────────────────
# 본문에서 H2 추출 + 핵심 3개 선택
# ─────────────────────────────────────────────────────
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_NUMBER_RE = re.compile(r"\d[\d,]*")
# 숫자+단위(금액·기간·비율·연령 등) 구 — 영상 stat 후보
_STAT_RE = re.compile(
    r"(?:월\s?)?\d[\d,]*\s?(?:만\s?원|억\s?원|천\s?원|원|만원|개월|일|주|년|시간|"
    r"%|퍼센트|배|명|가지|단계|회|건|세|kg|킬로|평|㎡)")
# 카테고리별 기본 아이콘 (fallback)
_CAT_ICON = {
    "청년": "🧑", "주거": "🏠", "복지": "🤝", "바우처": "💳",
    "취업": "💼", "의료": "🏥", "교육": "📚", "금융": "💰", "default": "📌",
}
_POS_ICON = ["✅", "💰", "📅"]   # 카드 위치별 기본 아이콘


def _loose_json_object(raw: str) -> Optional[dict]:
    """Claude 응답에서 최상위 JSON 객체를 관대하게 추출.

    haiku 가 종종 코드펜스·트레일링 콤마·단일따옴표를 섞어 내므로 단계적으로 시도:
      1) 코드펜스 제거 후 strict json.loads
      2) 최외곽 {...} 만 잘라 json.loads
      3) 트레일링 콤마 제거 후 json.loads
      4) ast.literal_eval (단일따옴표 파이썬 dict 대응)
    실패하면 None (호출측이 fallback).
    """
    import ast
    if not raw or not isinstance(raw, str):
        return None
    s = re.sub(r'^\s*```[a-z]*\n?', '', raw.strip(), flags=re.IGNORECASE)
    s = re.sub(r'\n?```\s*$', '', s).strip()

    def _try(txt: str) -> Optional[dict]:
        try:
            v = json.loads(txt)
            return v if isinstance(v, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    for candidate in (s, ):
        got = _try(candidate)
        if got is not None:
            return got

    # 최외곽 중괄호 블록
    start, end = s.find("{"), s.rfind("}")
    if start == -1 or end <= start:
        return None
    block = s[start:end + 1]
    got = _try(block)
    if got is not None:
        return got

    # 트레일링 콤마 제거
    no_trailing = re.sub(r",(\s*[}\]])", r"\1", block)
    got = _try(no_trailing)
    if got is not None:
        return got

    # 단일따옴표 파이썬 dict
    try:
        v = ast.literal_eval(block)
        return v if isinstance(v, dict) else None
    except (ValueError, SyntaxError):
        return None


def _extract_stat(text: str, max_len: int = 12) -> str:
    """문자열에서 숫자+단위 구를 하나 뽑아 stat 후보로 반환. 없으면 ''."""
    if not text:
        return ""
    m = _STAT_RE.search(text)
    if m:
        s = re.sub(r"\s+", " ", m.group()).strip()
        return s[:max_len]
    return ""


def _extract_h2(content_md: str) -> List[str]:
    """마크다운에서 ## 헤딩만 추출."""
    if not content_md:
        return []
    return [h.strip() for h in _H2_RE.findall(content_md) if h.strip()]


def _pick_top3_h2(h2_list: List[str]) -> List[str]:
    """H2 5~7개 중 핵심 3개 선택.

    우선순위:
    1. 숫자 포함 H2 (예: "1단계", "5만원", "3가지")
    2. 등장 순서 앞쪽
    """
    if len(h2_list) <= 3:
        # 부족하면 빈 카드 채우기 — 정확히 3개 보장
        padded = list(h2_list)
        while len(padded) < 3:
            padded.append("")
        return padded[:3]

    scored = []
    for idx, h in enumerate(h2_list):
        has_number = bool(_NUMBER_RE.search(h))
        # 숫자 있으면 가산점, 등장 순서는 감점(앞쪽 우선)
        score = (10 if has_number else 0) - idx * 0.3
        scored.append((score, idx, h))
    scored.sort(key=lambda t: (-t[0], t[1]))
    top3 = [h for _, _, h in scored[:3]]
    # 원래 순서대로 정렬 (영상 흐름이 본문 흐름을 따라가게)
    top3_in_order = [h for h in h2_list if h in top3][:3]
    return top3_in_order


# ─────────────────────────────────────────────────────
# 스크린샷 매칭
# ─────────────────────────────────────────────────────
def _find_screenshot_for(slug: str, h2_text: str,
                         category: str = "") -> Optional[str]:
    """slug 또는 카테고리에 매칭되는 스크린샷 file:// URL 반환.

    탐색 순서:
    1. naver_blog_v3/output/{slug}_*.png  (썸네일·카드뉴스 등 그 글 전용)
    2. naver_blog_v3/screenshots/gov24_*.png  (카테고리=디지털·정부지원 한정)
    """
    if not slug:
        return None

    # 1. output/{slug}_*.png — 우선 cardnews 또는 step 시리즈
    candidates: list[Path] = []
    for pattern in (f"{slug}_card_*.png", f"{slug}_step_*.png", f"{slug}_*.png"):
        candidates.extend(sorted(OUTPUT_DIR.glob(pattern)))

    if candidates:
        # 첫 매칭 — file:// URL 로 변환
        return candidates[0].resolve().as_uri()

    # 2. screenshots/ — 정부24·디지털 카테고리만
    if category in ("디지털", "복지", "주거", "바우처"):
        screenshots = sorted(SCREENSHOTS_DIR.glob("gov24_*.png"))
        if screenshots:
            return screenshots[0].resolve().as_uri()

    return None


# ─────────────────────────────────────────────────────
# 프롬프트 구성
# ─────────────────────────────────────────────────────
def _build_prompt(post_data: dict, personas: dict) -> str:
    """Claude CLI 에 줄 단일 프롬프트 (JSON 출력 강제)."""
    title = post_data.get("title", "").strip()
    content = post_data.get("content_html") or post_data.get("content") or ""
    category = post_data.get("category") or detect_category(title) or "default"
    tags = post_data.get("tags") or []

    # 본문은 마크다운 우선. HTML 이면 태그 제거해 텍스트만 추출.
    if "content_html" in post_data and not post_data.get("content"):
        content = re.sub(r"<[^>]+>", " ", content)
        content = re.sub(r"\s+", " ", content).strip()

    # 본문 길이 제한 — Claude 토큰 절약
    if len(content) > 3500:
        content = content[:3500] + "\n... (이하 생략)"

    h2_list = _extract_h2(post_data.get("content") or "")
    h2_hint = "\n".join(f"- {h}" for h in h2_list[:7]) if h2_list else "(H2 없음)"

    youtube_p = personas.get("youtube_shorts", {})
    insta_p = personas.get("instagram_reels", {})
    tiktok_p = personas.get("tiktok", {})
    script_p = personas.get("shorts_script", {})

    yt_max = youtube_p.get("title_max_chars", 60)
    yt_tags_min = youtube_p.get("tags_count_min", 5)
    yt_tags_max = youtube_p.get("tags_count_max", 12)
    insta_count = insta_p.get("hashtag_count_exact", 30)
    tt_min = tiktok_p.get("hashtag_count_min", 5)
    tt_max = tiktok_p.get("hashtag_count_max", 10)
    tts_target = script_p.get("tts_text_target_chars", 150)
    tts_max = script_p.get("tts_text_max_chars", 220)
    card_count = script_p.get("h2_card_count_exact", 3)
    card_max = script_p.get("card_text_max_chars", 28)

    # 모든 채널의 forbidden_words 합집합
    forbidden = set()
    for ch in (youtube_p, insta_p, tiktok_p, script_p):
        for w in ch.get("forbidden_words") or []:
            forbidden.add(w)
    forbidden_str = ", ".join(sorted(forbidden)) if forbidden else "(없음)"

    # 브랜드(멀티채널 personas 의 common) — default=examplebrand(문자열 동일).
    _common = personas.get("common") or {}
    brand_nm = _common.get("brand_name") or "examplebrand"
    brand_desc = _common.get("brand_desc") or "시니어 친화 정보"
    _def_tags = (_common.get("category_default_hashtags") or {}).get("default") or ["#시니어"]
    brand_tag = _def_tags[0] if _def_tags else "#시니어"
    # 톤 설명(4채널 공통 헤더용) — 브랜드 팩(named) 오버라이드, default 는 기존 리터럴 폴백.
    try:
        from config import brand as _brand
        brand_tone = _brand.brand_tone() or "시니어 친화·따뜻함"
    except Exception:  # noqa: BLE001 — 브랜드 로드 실패 시 기존 동작 유지
        brand_tone = "시니어 친화·따뜻함"

    return f"""당신은 {brand_nm} 블로그({brand_desc})의 멀티채널 마케터입니다.
아래 블로그 글 1편을 4가지 채널 캡션 + 30초 Shorts 스크립트로 변환하세요.

⚠️ 절대 규칙 (반드시 지킬 것):
- 되묻지 마세요. 확인 질문·양해·인사·설명을 일절 하지 마세요.
- 본문이 비어 있거나 부족해도, 제목·태그·카테고리만으로 합리적으로 추론해
  완성하세요. (제목의 숫자·제도명이 곧 핵심 — 예: "월 20만원"이 제목에 있으면 stat 으로.)
- 출력은 오직 JSON 객체 하나. 코드블록(```)·앞뒤 문장 금지. 첫 글자는 반드시 {{ .

[글 제목] {title}
[카테고리] {category}
[기존 태그] {', '.join(str(t) for t in tags[:10]) if tags else '(없음)'}

[본문 H2 헤딩]
{h2_hint}

[본문 발췌]
{content}

────────────────────────────────────
[채널별 사양]

1. YouTube Shorts:
   - title: {yt_max}자 이내, 검색 SEO 최적화. 첫 5초 훅 강조.
   - description: 600~900자. 본문 요약 + {brand_nm} 블로그 안내 + 해시태그.
   - tags: {yt_tags_min}~{yt_tags_max}개 (#쇼츠 {brand_tag} 포함)
   - tone: {youtube_p.get('tone', '정보 우선')}

2. Instagram Reels:
   - caption: 400~800자, 줄바꿈 포함. 첫 줄 임팩트.
   - hashtags: 정확히 {insta_count}개 (대형5+중형15+소형10)
   - first_frame_text: 30자 이내 (영상 첫 프레임 텍스트)
   - tone: {insta_p.get('tone', '감성·따뜻함')}

3. TikTok:
   - caption: 150~250자. 첫 1초가 결정.
   - hashtags: {tt_min}~{tt_max}개 (트렌드 포함)
   - tone: {tiktok_p.get('tone', '후킹')}

4. Shorts 스크립트 (30초 영상용):
   - hook: 0~3초 훅 텍스트. 궁금증·손해 회피를 자극하는 한 문장 (22자 이내).
     예: "이거 모르면 월 9만원 손해", "60일 지나면 못 받습니다".
   - hook_keyword: 화면 배지로 띄울 키워드 1개 (# 또는 단어, 12자 이내)
   - h2_cards: 정확히 {card_count}개. 각 카드는 세로 영상의 큰 카드 한 장이 된다.
     각 카드는 아래 4개 필드를 채운다 (영상 화면 요소):
       · text    : 카드 제목/질문. 짧게 (14자 이내). 예 "지원 금액", "신청 기한".
       · stat    : 그 카드에서 가장 중요한 "숫자 또는 핵심 한 마디" — 화면에 가장 크게 뜬다.
                   가능하면 숫자를 넣는다 (10자 이내). 예 "월 9만원", "60일 이내", "만 2세 미만".
                   숫자가 정말 없으면 핵심 키워드 2~3어절. 절대 text 와 똑같이 쓰지 말 것.
       · caption : stat 을 보완하는 짧은 한 줄 (16자 이내, 선택). 예 "국민행복카드 충전".
       · icon    : 카드 내용에 어울리는 이모지 1개. 예 💰 📅 ✅ 📝 ⚠️ 👶 🏠 💳 🏥 📞.
     3장은 "대상 → 금액 → 방법/기한" 처럼 시청 흐름이 자연스럽게 이어지게 배열.
   - cta: 25~30초 CTA 문구 (줄바꿈 \\n 가능, 30자 이내)
   - tts_text: 25초 분량 한국어 내레이션 ({tts_target}자 목표, {tts_max}자 절대 초과 금지).
     자연스러운 구어체. 시니어 친화. 마침표·쉼표로 구간 분리.
     hook(3초) + 본문(22초) + CTA(5초) 흐름을 한 흐름의 글로.
     본문 22초는 h2_cards 3장의 stat 을 말로 풀어 설명하는 순서로.

[{brand_nm} 톤 — 4채널 공통]
- {brand_tone}·차분함. 부모님께 보여드릴 수 있는 정보 톤.
- 짧은 문장. 어려운 행정용어는 쉽게 풀어서.

[금지 단어 — 모두 절대 사용 금지]
{forbidden_str}

[출력 형식 — JSON 만, 마크다운 코드블록 없이]
{{
  "youtube": {{
    "title": "...",
    "description": "...",
    "tags": ["태그1", "태그2", ...],
    "category_id": "27",
    "made_for_kids": false
  }},
  "instagram": {{
    "caption": "...",
    "hashtags": ["#태그1", "#태그2", ...],
    "first_frame_text": "..."
  }},
  "tiktok": {{
    "caption": "...",
    "hashtags": ["#태그1", "#태그2", ...]
  }},
  "shorts_script": {{
    "hook": "...",
    "hook_keyword": "#키워드",
    "h2_cards": [
      {{"text": "지원 대상", "stat": "만 2세 미만", "caption": "저소득 영아 가구", "icon": "👶"}},
      {{"text": "지원 금액", "stat": "월 9만원", "caption": "국민행복카드 충전", "icon": "💰"}},
      {{"text": "신청 기한", "stat": "60일 이내", "caption": "출생일 기준", "icon": "📅"}}
    ],
    "cta": "자세한 절차는\\n{brand_nm} 블로그에서",
    "tts_text": "..."
  }}
}}"""


# ─────────────────────────────────────────────────────
# Fallback (Claude 실패 시)
# ─────────────────────────────────────────────────────
def _build_fallback(post_data: dict, personas: dict) -> dict:
    """Claude CLI 실패 시 본문 첫 200자 + 카테고리 기본 해시태그로 단순 캡션.

    examplebrand 일 발행과는 무관. 단순히 멀티채널 팩 생성을 차단하지 않기 위함.
    """
    title = post_data.get("title", "")
    content = post_data.get("content") or ""
    category = post_data.get("category") or detect_category(title) or "default"
    tags = post_data.get("tags") or []

    # 본문 첫 200자
    text_preview = re.sub(r"[#*`]+", "", content)
    text_preview = re.sub(r"\s+", " ", text_preview).strip()[:200]

    common = personas.get("common") or {}
    cat_tags = (common.get("category_default_hashtags") or {}).get(
        category, common.get("category_default_hashtags", {}).get("default", ["#시니어"])
    )

    # 브랜드 핸들(라틴) — default 는 "examplebrand" 로 폴백(기존 문자열 동일).
    try:
        from config import brand as _brand
        brand_nm = _brand.name() or "examplebrand"
    except Exception:  # noqa: BLE001 — 브랜드 로드 실패 시 기존 동작 유지
        brand_nm = "examplebrand"

    base_hashtags = list(cat_tags) + [f"#{t}" for t in tags[:5] if t]
    insta_30 = (base_hashtags * 6)[:30]  # 30개 채우기

    h2_list = _extract_h2(content)
    cards_text = _pick_top3_h2(h2_list)
    cat_icon = _CAT_ICON.get(category, _CAT_ICON["default"])
    h2_cards = []
    for i, t in enumerate(cards_text):
        label = (t or "핵심 절차").strip()
        # H2 제목 자체에 숫자 구가 있으면 stat 으로 승격(틀린 숫자 차용 금지 —
        # 라벨에 없으면 비워 둔다). 라벨의 숫자는 stat 으로 옮기고 라벨은 정리.
        stat = _extract_stat(label)
        if stat and stat in label:
            label = re.sub(re.escape(stat), "", label).strip(" ?·-") or "핵심"
        h2_cards.append({
            "text": label[:16],
            "stat": stat,
            "caption": "",
            "icon": _POS_ICON[i] if i < len(_POS_ICON) else cat_icon,
        })

    cta = (common.get("default_cta") or f"자세한 절차는\n{brand_nm} 블로그에서")
    tts_text = (
        f"안녕하세요. {title} 입니다. "
        f"{text_preview} "
        f"자세한 절차는 {brand_nm} 블로그에서 확인하세요."
    )[:220]

    log.warning("caption_generator: Claude CLI fail - fallback captions used")

    return {
        "youtube": {
            "title": title[:60],
            "description": f"{title}\n\n{text_preview}\n\n자세한 정보는 {brand_nm} 블로그에서 확인하세요.",
            "tags": [t for t in tags[:10] if t] or ["시니어", "예시브랜드"],
            "category_id": "27",
            "made_for_kids": False,
        },
        "instagram": {
            "caption": f"{title}\n\n{text_preview}\n\n자세한 절차는 {brand_nm} 블로그에서.",
            "hashtags": insta_30,
            "first_frame_text": title[:30],
        },
        "tiktok": {
            "caption": f"{title} {text_preview[:120]}",
            "hashtags": base_hashtags[:10] or ["#시니어", "#예시브랜드"],
        },
        "shorts_script": {
            "hook": title[:25],
            "hook_keyword": (cat_tags[0] if cat_tags else "#시니어")[:12],
            "h2_cards": h2_cards,
            "cta": cta,
            "tts_text": tts_text,
        },
        "_fallback": True,
    }


# ─────────────────────────────────────────────────────
# 결과 검증·정규화
# ─────────────────────────────────────────────────────
# Claude haiku 가 자주 만드는 어색·오류 한국어 교정(안심 맥락에서 '걱정하세요'=반대 의미 등).
# 정확 패턴만 치환해 오탐 방지. 발화·자막·화면 텍스트 모두에 적용.
_PHRASE_FIXES = [
    (re.compile(r"걱정하세요(?!\?)"), "걱정 마세요"),   # '걱정하세요'(걱정해라)→'걱정 마세요'
    (re.compile(r"걱정하지\s*마세요"), "걱정 마세요"),   # 표기 통일
    (re.compile(r"염려하세요(?!\?)"), "염려 마세요"),
]


def _fix_korean_phrases(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    for pat, repl in _PHRASE_FIXES:
        text = pat.sub(repl, text)
    return text


def _normalize_result(result: dict, personas: dict, post_data: dict) -> dict:
    """Claude 응답을 채널별 사양에 맞게 자르고 보정.

    - h2_cards 가 3개가 아니면 패딩/트림
    - tts_text 길이 max 초과 시 트림
    - hashtags 개수 보정
    - 흔한 생성 오류 문구 교정(_fix_korean_phrases)
    """
    common = personas.get("common") or {}
    youtube_p = personas.get("youtube_shorts") or {}
    insta_p = personas.get("instagram_reels") or {}
    tiktok_p = personas.get("tiktok") or {}
    script_p = personas.get("shorts_script") or {}

    # YouTube
    yt = result.setdefault("youtube", {})
    yt_max = youtube_p.get("title_max_chars", 60)
    if isinstance(yt.get("title"), str) and len(yt["title"]) > yt_max:
        yt["title"] = yt["title"][: yt_max - 1] + "…"
    yt.setdefault("category_id", youtube_p.get("category_id", "27"))
    yt.setdefault("made_for_kids", False)
    if not isinstance(yt.get("tags"), list):
        yt["tags"] = []

    # Instagram — 정확히 30개
    insta = result.setdefault("instagram", {})
    insta_target = insta_p.get("hashtag_count_exact", 30)
    hashtags = insta.get("hashtags") or []
    if not isinstance(hashtags, list):
        hashtags = []
    # 중복 제거 (순서 유지)
    seen = set()
    deduped = []
    for h in hashtags:
        if isinstance(h, str) and h and h not in seen:
            seen.add(h)
            deduped.append(h)
    if len(deduped) < insta_target:
        # 카테고리 기본 해시태그 + generic 풀로 보충
        category = post_data.get("category") or "default"
        cat_defaults = (common.get("category_default_hashtags") or {}).get(
            category, []
        )
        # generic 풀 — 30개를 항상 채울 수 있도록 충분히 다양
        generic_pool = [
            "#시니어", "#예시브랜드", "#실버", "#부모님", "#정보",
            "#일상", "#팁", "#정부지원", "#복지정보", "#가족",
            "#노후준비", "#건강", "#디지털", "#스마트폰", "#카카오톡",
            "#실버라이프", "#5060", "#6070", "#효도", "#엄마",
            "#아빠", "#부모님선물", "#실용정보", "#꿀팁", "#알면좋은",
            "#한국", "#korea", "#senior", "#silver", "#daily",
            "#information", "#dailylife", "#parents", "#mom", "#dad",
        ]
        candidates_in_order = list(cat_defaults) + generic_pool
        for d in candidates_in_order:
            if not isinstance(d, str) or not d:
                continue
            if d not in seen:
                seen.add(d)
                deduped.append(d)
            if len(deduped) >= insta_target:
                break
    insta["hashtags"] = deduped[:insta_target]

    # TikTok — 5~10개
    tt = result.setdefault("tiktok", {})
    tt_min = tiktok_p.get("hashtag_count_min", 5)
    tt_max = tiktok_p.get("hashtag_count_max", 10)
    tt_hashtags = tt.get("hashtags") or []
    if not isinstance(tt_hashtags, list):
        tt_hashtags = []
    tt_hashtags = [h for h in tt_hashtags if isinstance(h, str) and h]
    if len(tt_hashtags) < tt_min:
        tt_hashtags.extend(["#시니어", "#예시브랜드", "#실버", "#정보", "#fyp"][: tt_min - len(tt_hashtags)])
    tt["hashtags"] = tt_hashtags[:tt_max]

    # Shorts script — h2_cards 정확히 3개
    script = result.setdefault("shorts_script", {})
    card_count = script_p.get("h2_card_count_exact", 3)
    card_max = script_p.get("card_text_max_chars", 28)
    cards = script.get("h2_cards") or []
    if not isinstance(cards, list):
        cards = []

    # 각 카드 정규화 — text/stat/caption/icon 4필드 (영상 인포그래픽 요소)
    label_max = script_p.get("card_label_max_chars", 16)
    stat_max = script_p.get("card_stat_max_chars", 12)
    cap_max = script_p.get("card_caption_max_chars", 20)

    def _first_emoji(s: str) -> str:
        """문자열에서 첫 이모지(있으면) 1개만."""
        for ch in (s or ""):
            if ord(ch) >= 0x1F000 or 0x2600 <= ord(ch) <= 0x27BF:
                return ch
        return ""

    norm_cards = []
    for c in cards:
        if isinstance(c, dict):
            text = (c.get("text") or "").strip()
            stat = (c.get("stat") or "").strip()
            caption = (c.get("caption") or "").strip()
            icon = _first_emoji(str(c.get("icon") or ""))
        elif isinstance(c, str):
            text, stat, caption, icon = c.strip(), "", "", ""
        else:
            continue
        if not text and not stat:
            continue
        # 길이 보정
        if len(text) > label_max:
            text = text[: label_max - 1] + "…"
        # stat 이 text 와 동일하면(모델 실수) stat 비움 — 화면 중복 방지
        if stat and stat == text:
            stat = ""
        if len(stat) > stat_max:
            stat = stat[:stat_max]
        if len(caption) > cap_max:
            caption = caption[: cap_max - 1] + "…"
        norm_cards.append({"text": text or stat, "stat": stat,
                           "caption": caption, "icon": icon})

    # 부족하면 본문 H2 에서 보충 (stat/caption/icon 은 비움)
    if len(norm_cards) < card_count:
        h2_list = _extract_h2(post_data.get("content") or "")
        backup = _pick_top3_h2(h2_list)
        for h in backup:
            if len(norm_cards) >= card_count:
                break
            t = (h or "핵심 절차").strip()[:label_max]
            if t and not any(c["text"] == t for c in norm_cards):
                norm_cards.append({"text": t, "stat": "", "caption": "",
                                   "icon": ""})

    while len(norm_cards) < card_count:
        norm_cards.append({"text": "핵심 절차", "stat": "", "caption": "",
                           "icon": ""})
    script["h2_cards"] = norm_cards[:card_count]

    # tts_text 길이 트림
    tts_max = script_p.get("tts_text_max_chars", 220)
    tts = script.get("tts_text") or ""
    if not isinstance(tts, str):
        tts = str(tts)
    if len(tts) > tts_max:
        # 마침표 단위로 자르기 시도
        truncated = tts[:tts_max]
        last_period = truncated.rfind(".")
        if last_period > tts_max * 0.7:
            truncated = truncated[: last_period + 1]
        script["tts_text"] = truncated
    elif not tts.strip():
        # tts 누락 — fallback
        script["tts_text"] = (
            f"안녕하세요. {post_data.get('title','')[:40]}. "
            f"자세한 절차는 examplebrand 블로그에서 확인하세요."
        )

    # hook / hook_keyword / cta 누락 보완
    if not script.get("hook"):
        script["hook"] = (post_data.get("title") or "")[:25]
    if not script.get("hook_keyword"):
        cat = post_data.get("category") or "default"
        defaults = (common.get("category_default_hashtags") or {}).get(cat, ["#시니어"])
        script["hook_keyword"] = (defaults[0] if defaults else "#시니어")[:12]
    if not script.get("cta"):
        script["cta"] = common.get("default_cta") or "자세한 절차는\nexamplebrand 블로그에서"

    # 흔한 생성 오류 문구 교정 — 발화(tts)·화면(hook/cta)·카드 텍스트 전반
    for key in ("tts_text", "hook", "cta"):
        if script.get(key):
            script[key] = _fix_korean_phrases(script[key])
    for c in script.get("h2_cards") or []:
        if isinstance(c, dict):
            for k in ("text", "stat", "caption"):
                if c.get(k):
                    c[k] = _fix_korean_phrases(c[k])

    return result


# ─────────────────────────────────────────────────────
# 메인 진입점
# ─────────────────────────────────────────────────────
def generate_multichannel_captions(post_data: dict,
                                   *,
                                   slug: Optional[str] = None,
                                   stop_flag=None) -> dict:
    """블로그 글 1편 → 4채널 캡션 + Shorts 스크립트.

    Args:
        post_data: {
            "title": str,
            "category": str (선택, 없으면 detect_category 호출),
            "content_html" 또는 "content": str,
            "tags": list[str] (선택),
            "slug": str (선택, screenshot 매칭용),
        }
        slug: 스크린샷 매칭용. post_data["slug"] 와 별도 지정 가능.
        stop_flag: threading.Event — 사용자 중지 요청.

    Returns:
        {
            "youtube": {...},
            "instagram": {...},
            "tiktok": {...},
            "shorts_script": {
                "hook": str, "hook_keyword": str,
                "h2_cards": [{"text": str, "screenshot": str|None}, ...3개],
                "cta": str,
                "tts_text": str,
            }
        }
    """
    if not isinstance(post_data, dict):
        raise TypeError("post_data must be a dict")
    title = (post_data.get("title") or "").strip()
    if not title:
        raise ValueError("post_data['title'] 이 비어 있습니다")

    # 카테고리 자동 감지
    if not post_data.get("category"):
        post_data["category"] = detect_category(title) or "default"

    target_slug = slug or post_data.get("slug") or ""

    personas = _load_personas()
    prompt = _build_prompt(post_data, personas)

    log.info(f"멀티채널 캡션 생성 시작: {title[:40]} (slug={target_slug or '미지정'})")
    raw = _call_claude(prompt, max_retries=2, use_web_search=False,
                       stop_flag=stop_flag,
                       category=post_data.get("category", ""))

    # 중지 요청 시 조기 종료 — fallback 도 정규화 후 반환
    if stop_flag and stop_flag.is_set():
        log.info("중지 요청 감지: generate_multichannel_captions 조기 종료")
        result = _build_fallback(post_data, personas)
        return _normalize_result(result, personas, post_data)

    result: dict = {}
    if raw:
        # _parse_json_response 는 title/content 키를 강제하므로 관대 파서 직접 사용
        parsed = _loose_json_object(raw)
        if parsed is not None:
            result = parsed
        else:
            # 되묻기·거부 등 JSON 이 아예 없는 응답 — 앞부분만 로그(자격정보 무관)
            log.warning(f"multichannel JSON 없음 - fallback (응답 앞 60자: "
                        f"{raw.strip()[:60]!r})")

    # 필수 키 검증 — 4개 모두 dict 여야 함
    required_top = ("youtube", "instagram", "tiktok", "shorts_script")
    if not result or not all(isinstance(result.get(k), dict) for k in required_top):
        log.warning("multichannel response shape incomplete - using fallback")
        result = _build_fallback(post_data, personas)

    # 정규화 (개수·길이 보정)
    result = _normalize_result(result, personas, post_data)

    # screenshot 자동 매칭 (h2_cards 의 각 카드에 screenshot 필드 추가)
    if target_slug:
        cat = post_data.get("category", "")
        for card in result["shorts_script"]["h2_cards"]:
            if "screenshot" not in card:
                shot = _find_screenshot_for(target_slug, card["text"], cat)
                card["screenshot"] = shot
    else:
        for card in result["shorts_script"]["h2_cards"]:
            card.setdefault("screenshot", None)

    log.info(
        f"멀티채널 캡션 생성 완료 — "
        f"YT 제목 {len(result['youtube'].get('title',''))}자, "
        f"IG #{len(result['instagram'].get('hashtags',[]))}, "
        f"TT #{len(result['tiktok'].get('hashtags',[]))}, "
        f"TTS {len(result['shorts_script'].get('tts_text',''))}자, "
        f"카드 {len(result['shorts_script'].get('h2_cards',[]))}개"
    )
    return result


__all__ = ["generate_multichannel_captions"]
