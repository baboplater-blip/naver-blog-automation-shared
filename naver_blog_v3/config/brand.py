# config/brand.py — 프로필별 브랜드 팩 (Phase 2, 2026-07-05)
"""활성 프로필의 브랜드 자산(이름·진행자 모델·키워드 엑셀)을 로드.

멀티 블로그는 "완전히 다른 브랜드/분야"라, 화면에 박히는 브랜드명·진행자 모델·
키워드 엑셀이 프로필마다 달라야 한다. 브랜드 팩은 `profiles/<name>/brand/` 에 둔다:
    brand/brand.json     {"name","display","channel","excel"}
    brand/presenter/*.png (진행자 모델 이미지)
    (키워드 엑셀은 profiles/<name>/keywords/*.xlsx)

**default 프로필은 내장 examplebrand 기본값** → 기존 동작 무변화(브랜드 팩 없어도 됨).
어떤 로드 실패도 기본값으로 폴백(렌더·발행 무중단).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from config import profile as _profile

# 내장(default) 기본값 — examplebrand 브랜드.
_APP_ROOT = Path(__file__).resolve().parent.parent            # naver_blog_v3/
_HARNESS_ROOT = _APP_ROOT.parent                              # naver_blog_automation_v3/
_BUILTIN_PRESENTER = _HARNESS_ROOT / "multichannel" / "assets" / "예시브랜드 모델"
_DEFAULTS = {"name": "examplebrand", "display": "examplebrand", "channel": "examplebrand"}

# ── named 프로필용 중립 기본값 ──────────────────────────────────────────────
# 새 블로그의 brand.json 이 불완전해도 examplebrand(정부지원) 문구가 새지 않게, named 프로필의
# 미설정 필드는 아래 '분야 무관 중립값'으로 폴백한다. (default 프로필은 항상 examplebrand 유지.)
_NEUTRAL_WRITER_ROLE = "해당 분야의 정보를 정직하게 전하는 블로그 운영자"
_NEUTRAL_SOURCES = "직접 경험과 신뢰할 수 있는 자료"
# [판단 흔적] ②에서 예시로 드는 '공개된 1차 자료'의 종류. 분야 무관 어휘만 쓴다
# (정부지원 전용 어휘 '신청 서식·공지'가 영어교육 프로필에 새지 않게).
_NEUTRAL_JUDGMENT_SOURCES = "공식 홈페이지·안내 자료·이용 약관·문의 답변"
_NEUTRAL_RELIABILITY = (
    "[⚠️ 정보 신뢰성 — 최우선 원칙]\n"
    "- 오늘 날짜: {today}. 2026년 현재 기준의 정확하고 최신인 정보만 작성할 것.\n"
    "- 확인되지 않은 내용은 단정하지 말고, 근거가 있으면 함께 밝힐 것.\n"
    "- 과장·허위·오해를 부르는 표현을 쓰지 말 것."
)
_NEUTRAL_PERSONA = (
    "Subject persona: Korean readers interested in this blog's topic. "
    "Warm, friendly, trustworthy tone. Show a friendly Korean character with objects "
    "relevant to the subject; keep it clean and on-topic."
)
_NEUTRAL_BADGES = ["정보", "가이드", "꿀팁", "정리"]


def _brand_json(name: Optional[str] = None) -> dict:
    """활성(또는 지정) 프로필의 brand/brand.json. 없거나 실패 시 {}."""
    bd = _profile.brand_dir(name)
    if bd is not None:
        p = bd / "brand.json"
        try:
            if p.exists():
                d = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(d, dict):
                    return d
        except (OSError, ValueError):
            pass
    return {}


def name(profile_name: Optional[str] = None) -> str:
    """브랜드 핸들(소문자, 채널/URL 용). default=examplebrand."""
    return str(_brand_json(profile_name).get("name") or _DEFAULTS["name"]).strip()


def display(profile_name: Optional[str] = None) -> str:
    """화면 표기명(대문자 브랜딩). 지정 없으면 name 의 대문자. default=examplebrand."""
    d = _brand_json(profile_name)
    return str(d.get("display") or name(profile_name).upper()).strip()


def channel(profile_name: Optional[str] = None) -> str:
    """CTA 채널명(예: '{channel} 채널 구독'). default=examplebrand."""
    d = _brand_json(profile_name)
    return str(d.get("channel") or name(profile_name)).strip()


def _meta_str(key: str, profile_name: Optional[str] = None,
              named_default: str = "") -> str:
    """brand.json 의 문자열 필드.

    - 값이 있으면 그 값.
    - 없을 때: **named 프로필**은 `named_default`(중립값), **default 프로필**은 ""
      (호출측이 기존 examplebrand 기본으로 폴백 → default 바이트 동일 유지).
    """
    v = _brand_json(profile_name).get(key)
    if isinstance(v, str) and v.strip():
        return v.strip()
    if not _profile.is_default(profile_name):
        return named_default
    return ""


def blog_name(profile_name: Optional[str] = None) -> str:
    """생성 프롬프트에서 부르는 블로그 이름(산문체). default=예시브랜드(호출측 폴백)."""
    return _meta_str("blog_name", profile_name)


def writer_role(profile_name: Optional[str] = None) -> str:
    """작가 역할(예: '영어교육 정보 전문 작가'). named 미설정=중립값, default=""(호출측 폴백)."""
    return _meta_str("writer_role", profile_name, _NEUTRAL_WRITER_ROLE)


def brand_tone(profile_name: Optional[str] = None) -> str:
    return _meta_str("brand_tone", profile_name)


def primary_sources(profile_name: Optional[str] = None) -> str:
    """본문에 명시할 1차 출처(예: '정부24·복지로'). named 미설정=중립값, default=""(호출측 폴백)."""
    return _meta_str("primary_sources", profile_name, _NEUTRAL_SOURCES)


def aeo_example(profile_name: Optional[str] = None) -> str:
    return _meta_str("aeo_example", profile_name)


def image_caption_example(profile_name: Optional[str] = None) -> str:
    return _meta_str("image_caption_example", profile_name)


def judgment_sources_example(profile_name: Optional[str] = None) -> str:
    """생성 프롬프트 [판단 흔적] ②에서 '무엇을 확인했는지' 예시로 드는 1차 자료 종류.

    default(예시브랜드)는 ""(호출측이 정부지원 문구로 폴백), named 프로필은 분야 무관
    중립값. 영어교육·상품리뷰처럼 '신청 서식'이 없는 분야에서 정부 어휘가 새지 않게
    brand.json "judgment_sources_example" 로 오버라이드할 수 있다.
    """
    return _meta_str("judgment_sources_example", profile_name,
                     _NEUTRAL_JUDGMENT_SOURCES)


def reliability_block(profile_name: Optional[str] = None) -> str:
    """생성 프롬프트의 '정보 신뢰성' 블록({today}/{clean} 치환 가능).
    named 미설정=중립 블록(정부 문구 없음), default=""(호출측 examplebrand 폴백)."""
    return _meta_str("reliability_block", profile_name, _NEUTRAL_RELIABILITY)


def image_persona_default(profile_name: Optional[str] = None) -> str:
    """이미지 생성 프롬프트의 default 페르소나 directive(영문).
    named 미설정=중립 페르소나, default=""(호출측 내장 정부 페르소나 폴백)."""
    return _meta_str("image_persona_default", profile_name, _NEUTRAL_PERSONA)


def web_search(profile_name: Optional[str] = None):
    """글 생성 시 웹 검색 정책. 반환:
      - None  → 호출측 기본(settings.WEB_SEARCH_CATEGORIES 세트 사용) = default 프로필 동작
      - True  → 이 프로필의 모든 카테고리에서 웹 검색 ON
      - False → 모든 카테고리에서 OFF
      - list  → 지정 카테고리에서만 ON

    default(예시브랜드)는 항상 None(정부지원 카테고리 세트 그대로). named 프로필은
    brand.json "web_search"(true/false/[리스트])로 제어(미설정이면 None → 사실상 비정부
    카테고리는 OFF). 영어교육처럼 최신 정보가 필요하면 true 권장.
    """
    if _profile.is_default(profile_name):
        return None
    v = _brand_json(profile_name).get("web_search")
    if isinstance(v, bool):
        return v
    if isinstance(v, (list, tuple)):
        return [str(c) for c in v]
    return None


def genre(profile_name: Optional[str] = None) -> str:
    """블루프린트 장르의 프로필 전역 강제값(선택). 반환:
      - ""    → 미지정(default 포함) → blueprint_engine 이 자동 감지 → 최종 info 로 귀결.
      - 그 외 → brand.json "genre" 값(예: "place")을 모든 글에 강제.

    default(예시브랜드)는 항상 "" — 자동 감지가 정부지원 제목을 info 로 귀결시킨다
    (기존 동작·바이트 동일 유지). named 프로필은 brand.json "genre" 로 분야 장르를
    고정할 수 있다(맛집 블로그 전체를 place 로 등).
    """
    return _meta_str("genre", profile_name)


def category_genre(profile_name: Optional[str] = None) -> dict:
    """카테고리→장르 매핑(선택). brand.json "category_genre" 오브젝트.

    default(브랜드팩 없음)는 항상 {} — 자동 감지에 맡긴다(바이트 동일).
    named 프로필은 {"맛집": "place", "정보": "info"} 처럼 카테고리별 장르를 지정할 수 있다.
    genre() 전역값보다 카테고리 매핑이 우선한다(blueprint_engine 에서 순서 결정).
    """
    d = _brand_json(profile_name).get("category_genre")
    if isinstance(d, dict):
        return {str(k): str(v) for k, v in d.items() if v}
    return {}


def deadline_enabled(profile_name: Optional[str] = None) -> bool:
    """정부24/정책포털 마감 추적·엑셀 주입을 이 프로필에서 쓸지.

    **default(예시브랜드)=True**(정부지원 블로그, 기존 동작). named 프로필은 brand.json 에
    "deadline_tracker": true 를 명시하지 않는 한 **False**(영어교육 등 비정부 분야에
    정부 공고가 발행 큐에 주입되는 것을 막음).
    """
    if _profile.is_default(profile_name):
        return True
    return bool(_brand_json(profile_name).get("deadline_tracker", False))


# ── 썸네일·영상 픽셀 브랜딩 — .get(key, examplebrand기본)로 '명시 빈값'과 '미설정' 구분.
#    default 프로필은 brand.json 이 없어 항상 아래 examplebrand 기본값(픽셀 바이트 동일).
#    named 프로필은 값을 주거나 ""(빈 문자열)로 '표기 없음'을 명시할 수 있다.
def _meta_default(key: str, silver_default: str, profile_name: Optional[str] = None,
                  named_default: Optional[str] = None) -> str:
    """썸네일·영상 픽셀 브랜딩 필드.

    - 키가 있으면(빈 문자열 명시 포함) 그 값 — named 프로필이 ""로 '표기 없음'을 명시 가능.
    - 키가 없을 때: **named 프로필**은 `named_default`(주면), **default 프로필**은 examplebrand.
      → 새 블로그가 brand.json 을 안 채워도 '정부지원' 픽셀 문구가 새지 않는다.
    """
    d = _brand_json(profile_name)
    if key in d:
        v = d[key]
        return str(v) if isinstance(v, str) else silver_default
    if named_default is not None and not _profile.is_default(profile_name):
        return named_default
    return silver_default


def thumbnail_logo(profile_name: Optional[str] = None) -> str:
    """썸네일 하단 로고 문구. default='정부지원 완벽가이드 블로그', named 미설정=표시명."""
    return _meta_default("thumbnail_logo", "정부지원 완벽가이드 블로그", profile_name,
                         named_default=display(profile_name))


def thumbnail_tag(profile_name: Optional[str] = None) -> str:
    """썸네일 카테고리 뱃지 접미. default='정부지원', named 미설정=''(접미 없음)."""
    return _meta_default("thumbnail_tag", "정부지원", profile_name, named_default="")


def thumbnail_source_note(profile_name: Optional[str] = None) -> str:
    """썸네일 출처 표기. default='gov.kr 기반', named 미설정=''(표기 없음)."""
    return _meta_default("thumbnail_source_note", "gov.kr 기반", profile_name, named_default="")


def thumbnail_subtext(profile_name: Optional[str] = None) -> str:
    """썸네일 서브 카피. default='📋 신청방법…', named 미설정=''(부제 없음)."""
    return _meta_default("thumbnail_subtext", "📋 신청방법 · 대상 · 지원금액 완벽 정리",
                         profile_name, named_default="")


def video_category_label(profile_name: Optional[str] = None) -> str:
    """영상 썸네일 미매핑 카테고리 배지 라벨. default='정부지원', named 미설정=''(카테고리명 사용)."""
    return _meta_default("video_category_label", "정부지원", profile_name, named_default="")


def thumbnail_badges(profile_name: Optional[str] = None) -> list:
    """썸네일 하단 마이크로 배지 라벨(2~4개). text_overlay 가 FLUX 일러스트 위에 그린다.
    default=['자격','금액','신청','대상'], named 미설정=['정보','가이드','꿀팁','정리']."""
    v = _brand_json(profile_name).get("thumbnail_badges")
    if isinstance(v, list) and v:
        return [str(x) for x in v][:4]
    if not _profile.is_default(profile_name):
        return list(_NEUTRAL_BADGES)
    return ["자격", "금액", "신청", "대상"]


def categories(profile_name: Optional[str] = None) -> Optional[list]:
    """활성 프로필의 콘텐츠 카테고리 목록(GUI 커버리지·이미지 카테고리용).

    우선순위: brand.json 의 "categories" 배열 → prompt_strategies.json 키(예약키 제외).
    **default(브랜드팩 없음)는 None** — 호출측이 기존 하드코딩 목록을 그대로 쓰게 한다
    (기존 동작 무변화). named 프로필은 자기 분야 카테고리(예: 영어교육)를 돌려준다.
    """
    if _profile.brand_dir(profile_name) is None:
        return None                                    # default → 기존 하드코딩 유지
    d = _brand_json(profile_name)
    cats = d.get("categories")
    if isinstance(cats, list) and cats:
        return [str(c) for c in cats]
    try:
        p = template_path("prompt_strategies.json", profile_name)
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            keys = [k for k in data if k not in ("forbidden_words_global", "default")]
            if keys:
                return keys
    except (OSError, ValueError):
        pass
    return None


def presenter_dir(profile_name: Optional[str] = None) -> Optional[Path]:
    """진행자 모델 폴더. 프로필 brand/presenter 에 png 있으면 그것, 없으면 내장 예시브랜드.

    None 이면 진행자 없음(영상은 진행자 없이 렌더).
    """
    bd = _profile.brand_dir(profile_name)
    if bd is not None:
        pdir = bd / "presenter"
        try:
            if pdir.is_dir() and any(pdir.glob("*.png")):
                return pdir
        except OSError:
            pass
    return _BUILTIN_PRESENTER if _BUILTIN_PRESENTER.exists() else None


def excel_path(profile_name: Optional[str] = None) -> str:
    """키워드 엑셀 경로. 프로필 keywords/*.xlsx 우선(brand.json.excel 지정 우선), 없으면 settings 기본."""
    d = _brand_json(profile_name)
    kd = _profile.keywords_dir(profile_name)
    if kd is not None:
        # brand.json 에 excel 파일명이 명시되면 그것
        named = d.get("excel")
        if named:
            cand = kd / str(named)
            if cand.exists():
                return str(cand)
        # 아니면 keywords 폴더의 첫 xlsx
        try:
            xls = sorted(kd.glob("*.xlsx"))
            if xls:
                return str(xls[0])
        except OSError:
            pass
    # 폴백: settings 기본(default 프로필의 기존 경로)
    try:
        from config import settings as _s
        return str(getattr(_s, "KEYWORD_EXCEL_PATH", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def template_path(filename: str, profile_name: Optional[str] = None) -> Path:
    """브랜드 팩 템플릿 오버라이드. `profiles/<name>/brand/templates/<filename>` 있으면
    그것(프로필별 페르소나·전략), 없으면 공유 `naver_blog_v3/templates/<filename>`.

    default 프로필은 항상 공유 템플릿(기존 동작 무변화).
    """
    bd = _profile.brand_dir(profile_name)
    if bd is not None:
        cand = bd / "templates" / filename
        try:
            if cand.exists():
                return cand
        except OSError:
            pass
    return _APP_ROOT / "templates" / filename


def summary(profile_name: Optional[str] = None) -> dict:
    """브랜드 요약(로그·GUI 표기용, 민감정보 없음)."""
    pdir = presenter_dir(profile_name)
    return {
        "name": name(profile_name),
        "display": display(profile_name),
        "channel": channel(profile_name),
        "presenter_dir": str(pdir) if pdir else None,
        "excel": excel_path(profile_name),
    }


__all__ = ["name", "display", "channel", "categories", "blog_name", "writer_role",
           "brand_tone", "primary_sources", "aeo_example", "image_caption_example",
           "judgment_sources_example", "genre", "category_genre",
           "reliability_block", "image_persona_default", "web_search", "deadline_enabled",
           "thumbnail_logo", "thumbnail_tag", "thumbnail_source_note", "thumbnail_subtext",
           "video_category_label", "presenter_dir", "excel_path", "template_path", "summary"]
