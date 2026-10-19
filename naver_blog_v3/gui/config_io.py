# gui/config_io.py — 설정 로드/저장 + settings.py 안전 in-place 패치
"""GUI 설정 저장 동선 (2026-05-16 봉합).

[배경] 이전 구현은 GUI 저장 시 settings.py 를 통째로 재생성하는 (A) 방식이었다.
수동으로 settings.py 에 추가한 상수들 (CLAUDE_MODEL_BY_CATEGORY, QV_CATEGORY_OVERRIDES,
CLAUDE_CLI_TIMEOUT=300 등) 이 저장 한 번에 모두 날아갔다.
이는 5/8 사고 메모리에 "적용 완료" 라고 기록된 패치들이 5/15 점검 시 사라져 있던
원인이다.

[현재] (B) in-place 갱신 방식. GUI 가 관리하는 키 화이트리스트 (_GUI_MANAGED_KEYS) 만
정규식으로 settings.py 내에서 값을 교체하고, 그 외 모든 상수·dict·주석은 그대로
보존된다. 저장 직전 timestamp 백업을 자동 생성하고, 저장 직후 핵심 키 import 검증으로
손상 여부를 확인한다.
"""
from __future__ import annotations

import difflib
import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional, Tuple

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent.parent  # naver_blog_v3/

SETTINGS_PY_PATH = BASE_DIR / "config" / "settings.py"


def _cfg_path() -> Path:
    """활성 프로필 자격증명·설정 파일(계정별). default 는 config/user_settings.json 동일."""
    try:
        from config import profile as _p
        return _p.credentials_path()
    except Exception:  # noqa: BLE001
        return BASE_DIR / "config" / "user_settings.json"

# 기본값 (최초 설치 시 사용) — 한 곳에서만 정의
_DEFAULT_SLOTS = ["07:00", "08:30", "10:00", "12:00", "13:30", "17:00", "19:30", "21:00"]
_DEFAULT_HOURS = ["06:00", "11:30"]


# ── 코드 기본값 상향 시 캐시 정합 (2026-08-02 감사 C1) ────────────────
# user_settings.json 은 **저장 시점의 스냅샷**이다. settings.py 의 코드 기본값을 올려도
# 프로필 캐시에 옛 기본값이 남아 있으면 load_cfg 가 그걸 읽고, 그다음 아무 저장(다른 탭
# 토글 포함)이나 일어나는 순간 _build_managed_values 가 옛 값을 settings.py 에 다시 써서
# 상향이 조용히 무효화된다(실측: settings.py 3200 ↔ load_cfg 2500).
#
# 해소 원칙 — "기본값을 따르던 값"만 승격하고 "사용자가 정한 값"은 절대 건드리지 않는다:
#   1) 캐시에 _code_default_baseline[키] 가 있으면(= 저장 당시의 코드 기본값 기록),
#      캐시 값이 그 baseline 과 같을 때만 새 기본값으로 승격한다.
#      사용자가 GUI 에서 다른 값으로 바꿔 저장했다면 캐시 값 != baseline 이라 보존된다.
#   2) baseline 이 없는 옛 캐시는 부트스트랩으로 _LEGACY 목록(과거에 우리가 배포했던
#      기본값들)과만 대조한다. 그 목록에 없는 값 = 사용자가 손댄 값 → 보존.
#   3) 키 자체가 없으면 사용자가 정한 적이 없는 것 → 코드 기본값을 주입.
_CODE_DEFAULT_BASELINE_KEY = "_code_default_baseline"

# cfg 키: (settings.py 상수명, 과거에 배포됐던 기본값들)
# ⚠️ bool 값 키는 넣지 말 것 — `True in (1,)` 이 참이라 정수 목록과 오매칭된다.
_CODE_DEFAULT_KEYS: dict = {
    "qv_max_chars": ("QV_MAX_CHARS", (2500, 3000)),
}


def _live_setting(name: str, default=None):
    """현재 settings.py 값. 로드 실패 시 default(= 판단 보류용 None)."""
    try:
        from config import settings as _s
        return getattr(_s, name, default)
    except Exception:  # noqa: BLE001 — settings 미로드면 default
        return default


def _current_code_defaults() -> dict:
    """지금 코드가 들고 있는 기본값 스냅샷(저장 시 baseline 으로 기록)."""
    out: dict = {}
    for cfg_key, (attr, _legacy) in _CODE_DEFAULT_KEYS.items():
        value = _live_setting(attr, None)
        if value is not None:
            out[cfg_key] = value
    return out


def _reconcile_code_defaults(cfg: dict) -> dict:
    """캐시에 남은 '옛 코드 기본값'만 현재 코드 기본값으로 승격한다(제자리 수정).

    사용자가 GUI 에서 의도적으로 넣은 값은 어떤 경우에도 덮어쓰지 않는다.
    디스크에는 쓰지 않는다 — 여기서 재저장하면 복호화 실패(다른 PC·키 불일치) 시
    빈 문자열이 자격증명을 덮어쓸 수 있다. 다음 정상 저장 때 자연히 반영된다.

    Args:
        cfg: 복호화된 설정 dict.

    Returns:
        같은 dict 객체(정합 반영).
    """
    baseline = cfg.get(_CODE_DEFAULT_BASELINE_KEY)
    if not isinstance(baseline, dict):
        baseline = {}

    for cfg_key, (attr, legacy_defaults) in _CODE_DEFAULT_KEYS.items():
        current = _live_setting(attr, None)
        if current is None:          # settings 로드 실패 — 판단 보류(캐시 유지)
            continue
        cached = cfg.get(cfg_key)
        if cached is None:           # 사용자가 정한 적 없음 → 코드 기본값
            cfg[cfg_key] = current
            continue
        if cached == current:
            continue
        recorded = baseline.get(cfg_key)
        is_stale_default = (cached == recorded if recorded is not None
                            else cached in legacy_defaults)
        if is_stale_default:
            log.info("[config_io] 캐시의 옛 기본값 정합: %s %s → %s",
                     cfg_key, cached, current)
            cfg[cfg_key] = current
    return cfg

# ── GUI 가 관리하는 키 화이트리스트 ─────────────────────────────────
# 이 키들만 settings.py 안에서 in-place 로 교체된다.
# 화이트리스트에 없는 상수 (CLAUDE_MODEL_BY_CATEGORY, QV_CATEGORY_OVERRIDES,
# CLAUDE_CLI_TIMEOUT, HARNESS_EMIT_ENABLED 등) 는 손대지 않는다.
# 추가/삭제는 신중하게 — 이 화이트리스트가 "GUI 가 안전하게 수정 가능한 영역" 의
# 단일 정의처다.
_GUI_MANAGED_KEYS: Tuple[str, ...] = (
    "NAVER_NO_PASSWORD_LOGIN",
    "DAILY_POST_LIMIT",
    "POSTS_PER_DAY",
    "POST_INTERVAL_SECONDS",
    "SCHEDULE_HOURS",
    "USE_RESERVATION",
    "RESERVATION_TIME_SLOTS",
    "KEYWORD_EXCEL_PATH",
    "KEYWORD_SHEET_NAME",
    "KEYWORD_LONGTAIL_SHEET",
    "KEYWORD_PUBLISH_POOL_SHEET",
    "USE_NAVER_API",
    "NAVER_API_PROVIDER",
    "USE_TREND_UPDATE",
    "ENABLE_CAFE_DISPATCH",
    "USE_KAKAO_NOTIFY",
    "SAVE_PREVIEW_HTML",
    "SKIP_IMAGE_INSERT",
    # 2026-05-16 — Codex CLI 이미지 자동 생성 통합 토글들.
    # GUI 에서 노출되지 않더라도 _GUI_MANAGED_KEYS 에 등록해두면 in-place 저장
    # 흐름에서 settings.py 손상 없이 갱신 가능하다.
    "IMAGE_INJECT_ENABLED",
    "IMAGE_PER_POST_DEFAULT",
    "IMAGE_PER_POST_DIGITAL",
    "IMAGE_DISPATCH_TIER2_ON_FAIL",
    "HEADLESS",
    "MAX_RETRY_COUNT",
    "QV_MIN_CHARS",
    "QV_MAX_CHARS",
    "QV_REQUIRE_TABLE",
    "QV_REQUIRE_FAQ",
    "QV_MIN_H2_COUNT",
    "QV_MIN_TAGS",
    "QV_MAX_RETRIES",
    "HARNESS_EMIT_ENABLED",
    # 2026-07-05 — 발행 후 쇼츠 영상 자동 생성 토글. GUI 설정 탭 체크박스로 노출.
    # ON 이면 auto_scheduler [HOOK F] 가 실발행 성공 후 detached 백그라운드로
    # 멀티채널 팩(mp4+캡션)을 생성한다(발행 흐름 비차단). 업로드는 여전히 안 함.
    "AUTO_VIDEO_AFTER_PUBLISH",
    # 한 번의 트리거로 렌더할 최대 편수(영상 없는 최근 발행 글). 렌더가 무거워
    # (편당 ~2분) 백로그가 쌓였을 때 배치 크기를 조절한다. GUI 숫자 필드로 노출.
    "AUTO_VIDEO_LIMIT",
    # 2026-07-15 — 네이버 검색광고 API 실측 검색량 사용 여부. GUI 설정 탭에서
    # 자격증명 3개(api_key·secret·customer)를 모두 입력하면 자동으로 True 가 된다
    # (비면 False → available()=False 로 휴면). 값 자체는 user_settings.json 에
    # 암호화 저장되고, 이 플래그만 settings.py 에 기록된다.
    "SEARCHAD_API_ENABLED",
    # 2026-07-20 — 발행 경로 이원화(P0-① Phase 1). PUBLISH_MODE 는 GUI 토글(spawn↔attach)
    # 로 바뀌고, 나머지 셋은 GUI 미노출이지만 in-place 저장 흐름에서 항상 방출돼
    # settings.py 손상 없이 보존된다(스켈레톤 생성 시에도 포함).
    "PUBLISH_MODE",
    "CHROME_ATTACH_PORT",
    "CHROME_ATTACH_AUTOSTART",
    "CHROME_SESSION_PROFILE_DIR",
    # 2026-07-20 — 사진 인제스트(P0-② Phase 2). GUI 미노출이지만 in-place 저장
    # 흐름에서 항상 방출돼 settings.py 손상 없이 보존된다.
    # (PHOTO_INBOX_DIR 는 _BASE_DIR 동적 표현식이라 managed 에서 제외 —
    #  _CRITICAL_PRESERVED_KEYS 로 존재만 보장하고 표현식은 그대로 둔다.)
    "PHOTO_INGEST_ENABLED",
    "PHOTO_VISION_MODEL",
    "PHOTO_VISION_TIMEOUT",
    # 2026-07-23 — 본문 분석형 인포카드(카드뉴스). GUI 미노출이지만 in-place 저장
    # 흐름에서 항상 방출돼 settings.py 손상 없이 보존된다(기본 OFF 옵트인).
    "IMAGE_INFOCARD_ENABLED",
    "IMAGE_INFOCARD_BUDGET",
    "IMAGE_INFOCARD_SIZE",
    "IMAGE_MODEL_SHEET_PATH",
    # 2026-08-01 — 순위 추적 구체 롱테일 쿼리 병행. GUI 미노출이지만 _build_managed_values
    # 가 항상 방출해 in-place 저장 흐름에서 기본값으로 리셋되지 않고 보존된다.
    "RANK_TRACK_SPECIFIC",
    "RANK_SPECIFIC_MIN_VOLUME",
    "RANK_SPECIFIC_MAX_VOLUME",
    # 2026-08-02 — 경쟁 블로그 분석(competitor_analyzer). GUI 미노출이지만
    # _build_managed_values 가 항상 방출해 in-place 저장에서 기본값 리셋되지 않는다.
    "COMPETITOR_ANALYSIS_ENABLED",
    "COMPETITOR_MIN_RELEVANCE",
    "COMPETITOR_MAX_BLOGS",
    "COMPETITOR_POSTS_PER_BLOG",
    # 2026-08-25 — 설계도 주도 변주(blueprint). GUI 미노출이지만 _build_managed_values
    # 가 항상 방출해 in-place 저장에서 기본값(False)으로 리셋되지 않고 보존된다(옵트인).
    "BLUEPRINT_ENABLED",
    # 2026-08-29 — AI 티 스코어러 Phase A(관측 전용). GUI 미노출이지만 항상 방출해
    # in-place 저장에서 기본값(False)으로 리셋되지 않고 보존된다(옵트인).
    "AI_TELL_SCORE_ENABLED",
    # 2026-08-29 — 안티-AI-티 규칙 프롬프트 이식 Phase C. GUI 미노출이지만 항상 방출해
    # in-place 저장에서 기본값(False)으로 리셋되지 않고 보존된다(옵트인).
    "ANTI_AI_TELL_PROMPT",
    # 2026-08-29 — 리스타일 배치 AI 티 우선순위 정렬 Phase D. GUI 미노출이지만 항상
    # 방출해 in-place 저장에서 기본값(False)으로 리셋되지 않고 보존된다(옵트인).
    "RESTYLE_ORDER_BY_AI_TELL",
    # 2026-08-29 — 젠스파크 이미지 자동 채움(Phase 2a). GUI 미노출이지만 _build_managed_values
    # 가 항상 방출해 in-place 저장에서 기본값으로 리셋되지 않고 보존된다(GENSPARK_ENABLED
    # 은 기본 OFF 옵트인).
    "GENSPARK_ENABLED",
    "GENSPARK_THUMB_RATIO",
    "GENSPARK_CARD_RATIO",
    "GENSPARK_INFOCARD_BUDGET",
    "GENSPARK_MAX_POSTS_PER_RUN",
    "GENSPARK_AUTO_SWITCH",
    "GENSPARK_VERIFY_MAX_REGEN",
    "GENSPARK_VISION_MODEL",
    "GENSPARK_QUALITY",
    # 2026-09-09 — 이미지 채움(엔진 중립) + Codex 이미지 엔진. GUI 미노출이지만
    # _build_managed_values 가 항상 방출해 in-place 저장에서 기본값으로 리셋되지 않고
    # 보존된다(IMAGE_FILL_ENABLED 기본 ON — 젠스파크에서 codex 로 기본 엔진 이전).
    "IMAGE_FILL_ENABLED",
    "IMAGE_FILL_ENGINE",
    "IMAGE_FILL_CARDS",
    "IMAGE_FILL_MAX_POSTS_PER_RUN",
    "IMAGE_FILL_THUMB_RATIO",
    "IMAGE_FILL_CARD_RATIO",
    # 2026-09-10 — 제한 병렬(설정 탭 노출). 저장 흐름에서 사라지면 사용자가 고른 동시
    # 실행 수가 조용히 기본값으로 돌아가므로 두 목록 모두에 등록해 보호한다.
    "IMAGE_FILL_PARALLEL",
    "CODEX_IMAGE_MODEL",
    "CODEX_IMAGE_TIMEOUT",
    "CODEX_IMAGE_MAX_CALLS_PER_RUN",
    "CODEX_IMAGE_CONFIG_OVERRIDES",
)

# ── 저장 후 sanity check 대상 핵심 키 ─────────────────────────────────
# 저장 직후 이 키들이 settings.py 에서 사라졌다면 경고. 사용자 설정이 아니라
# 수동 패치된 상수들이므로 보존되어야 한다.
# 2026-05-25 확장 — dict 리터럴(IMAGE_THEMES·GOVERNMENT24_URL_MAP·WINDOWS_FONTS),
# 수동 튠된 타임아웃(RETRY_WAIT_SECONDS·SUBPROCESS_TIMEOUT·API_REQUEST_TIMEOUT),
# 이미지 자동 생성 토글(IMAGE_INJECT_ENABLED), 정부24 폴백 URL(DEFAULT_GOV24_URL)
# 까지 추가. 이들이 사라지면 이미지·정부지원·재시도 흐름이 모두 영향받음.
_CRITICAL_PRESERVED_KEYS: Tuple[str, ...] = (
    "CLAUDE_MODEL_BY_CATEGORY",
    "QV_CATEGORY_OVERRIDES",
    "CLAUDE_CLI_TIMEOUT",
    "HARNESS_EMIT_ENABLED",
    "POST_HISTORY_MAX",
    "DUPLICATE_THRESHOLD",
    "IMAGE_THEMES",
    "GOVERNMENT24_URL_MAP",
    "WINDOWS_FONTS",
    "IMAGE_INJECT_ENABLED",
    "IMAGE_INJECT_BY_CATEGORY",
    "DIA_AEO_AUTOFIX_BY_CATEGORY",
    "RETRY_WAIT_SECONDS",
    "SUBPROCESS_TIMEOUT",
    "API_REQUEST_TIMEOUT",
    "DEFAULT_GOV24_URL",
    "NAVER_API_PROVIDER",
    # 보조금24 인증키(2026-07-19) — load_shared_secret 라인이 재생성에서 살아남게
    "GOV24_API_KEY",
    # 발행 후 배치 캡션 전용 모드(2026-07-19) — 영상은 shorts_studio auto 가 담당
    "AUTO_VIDEO_CAPTION_ONLY",
    # 발행 경로 이원화(2026-07-20) — 이 키들이 사라지면 attach/spawn 분기가 기본값으로
    # 되돌아간다. _build_managed_values 가 항상 방출하므로 저장 흐름에서 살아남는다.
    "PUBLISH_MODE",
    "CHROME_ATTACH_PORT",
    "CHROME_ATTACH_AUTOSTART",
    "CHROME_SESSION_PROFILE_DIR",
    # 사진 인제스트(2026-07-20) — 이 키들이 사라지면 사진 글쓰기가 기본값으로 돌아간다.
    # PHOTO_INBOX_DIR 는 여기 넣지 않는다: managed/emit 대상이 아니라 in-place 패처가
    # 절대 쓰지 않으므로(=비관리 상수는 그대로 보존됨), 이 키를 critical 로 요구하면
    # 그 줄이 없는 settings.py(구버전·테스트 스텁)에서 sanity check 가 롤백을 유발한다.
    # 소비자는 getattr(s, "PHOTO_INBOX_DIR", 기본) 로 읽어 없어도 안전하다.
    "PHOTO_INGEST_ENABLED",
    "PHOTO_VISION_MODEL",
    "PHOTO_VISION_TIMEOUT",
    # 🛒 쇼핑커넥트 제휴 리뷰(2026-07-20) — GOV24_API_KEY·AUTO_VIDEO_CAPTION_ONLY 와 동일
    # 방식: 방출(emit) 대상이 아니라 in-place 패처가 이 줄들을 절대 건드리지 않아 보존된다.
    # SHOPPING_AFFILIATE_LINK 는 load_shared_secret 라인(GOV24_API_KEY 동급). 소비자는 전부
    # getattr(s, KEY, 기본) 로 읽어 줄이 없어도 안전. QV_CATEGORY_OVERRIDES["상품리뷰"] 는
    # 그 dict 리터럴(이미 critical) 안에 있어 별도 등록 불필요.
    "SHOPPING_CONNECT_ENABLED",
    "SHOPPING_AFFILIATE_LINK",
    "SHOPPING_DISCLOSURE_TEXT",
    "PRODUCT_IMAGE_MAX",
    # 🎨 본문 분석형 인포카드(2026-07-23) — _build_managed_values 가 항상 방출하므로
    # in-place 저장·최소 골격 생성 흐름에서 살아남는다(기본 OFF 옵트인 보존).
    "IMAGE_INFOCARD_ENABLED",
    "IMAGE_INFOCARD_BUDGET",
    "IMAGE_INFOCARD_SIZE",
    "IMAGE_MODEL_SHEET_PATH",
    # 순위 추적 구체 롱테일 쿼리(2026-08-01) — _build_managed_values 가 항상 방출하므로
    # 저장 흐름에서 살아남는다. 소비자는 getattr(s, KEY, 기본) 로 읽어 줄이 없어도 안전.
    "RANK_TRACK_SPECIFIC",
    "RANK_SPECIFIC_MIN_VOLUME",
    "RANK_SPECIFIC_MAX_VOLUME",
    # 경쟁 블로그 분석(2026-08-02) — _build_managed_values 가 항상 방출하므로 저장 흐름에서
    # 살아남는다. 소비자는 getattr(s, KEY, 기본) 로 읽어 줄이 없어도 안전(기본 OFF 옵트인).
    "COMPETITOR_ANALYSIS_ENABLED",
    "COMPETITOR_MIN_RELEVANCE",
    "COMPETITOR_MAX_BLOGS",
    "COMPETITOR_POSTS_PER_BLOG",
    # 설계도 주도 변주(2026-08-25) — _build_managed_values 가 항상 방출하므로 저장 흐름에서
    # 살아남는다. 소비자는 getattr(s, KEY, False) 로 읽어 줄이 없어도 안전(기본 OFF 옵트인).
    "BLUEPRINT_ENABLED",
    # AI 티 스코어러 Phase A(2026-08-29) — 관측 전용 옵트인 플래그. GUI 저장이 기본값(False)
    # 으로 리셋하지 않게 critical 로 보호. 소비자는 getattr(s, KEY, False) 로 읽어도 안전.
    "AI_TELL_SCORE_ENABLED",
    # 안티-AI-티 규칙 프롬프트 이식 Phase C(2026-08-29) — 프롬프트 주입 옵트인 플래그.
    # GUI 저장이 기본값(False)으로 리셋하지 않게 critical 로 보호(누락 시 롤백 알람).
    "ANTI_AI_TELL_PROMPT",
    # 젠스파크 이미지 자동 채움(Phase 2a, 2026-08-29) — _build_managed_values 가 항상
    # 방출하므로 저장 흐름에서 살아남는다(GENSPARK_ENABLED 기본 OFF 옵트인 보존).
    "GENSPARK_ENABLED",
    "GENSPARK_THUMB_RATIO",
    "GENSPARK_CARD_RATIO",
    "GENSPARK_INFOCARD_BUDGET",
    "GENSPARK_MAX_POSTS_PER_RUN",
    "GENSPARK_AUTO_SWITCH",
    "GENSPARK_VERIFY_MAX_REGEN",
    "GENSPARK_VISION_MODEL",
    "GENSPARK_QUALITY",
    # 리스타일 배치 AI 티 우선순위 정렬 Phase D(2026-08-29) — 정렬 옵트인 플래그.
    # GUI 저장이 기본값(False)으로 리셋하지 않게 critical 로 보호(누락 시 롤백 알람).
    "RESTYLE_ORDER_BY_AI_TELL",
    # 🆓 무료 데이터 소스 API 키(2026-08-16) — GOV24_API_KEY 동급. load_shared_secret
    # 라인이라 재생성에서 사라지면 키가 유실되므로 critical 로 보호(누락 시 롤백 알람).
    # ⚠️ opt-in 플래그(*_ENABLED·*_TTL·*_PAGES·*_BUDGET)는 여기 넣지 않는다 — 소비자가
    # getattr(s, KEY, 기본 OFF) 로 읽어 줄이 없어도 무해하고, in-place 패처가 물리 줄을
    # 이미 보존한다(관리 키가 아니라 손대지 않음). critical 은 '누락=사고'인 키만.
    "YOUTUBE_API_KEY",
    "ECOS_API_KEY",
    "KOSIS_API_KEY",
    # 뉴스 글감 소스 인증키와 NAVER API 제공자 선택은 GUI 저장 시 보존한다.
    "TOURAPI_SERVICE_KEY",
    "SEOUL_API_KEY",
    "NAVER_API_HUB_KEY_ID",
    "NAVER_API_HUB_KEY",
    # 2026-09-09 — 이미지 채움(엔진 중립) + Codex 이미지 엔진. _build_managed_values 가
    # 항상 방출하므로 저장 흐름에서 살아남는다(IMAGE_FILL_ENABLED 기본 ON 보존 — 누락 시
    # 이미지 채움 마스터 스위치가 조용히 사라지는 사고를 롤백 알람으로 막는다).
    "IMAGE_FILL_ENABLED",
    "IMAGE_FILL_ENGINE",
    "IMAGE_FILL_CARDS",
    "IMAGE_FILL_MAX_POSTS_PER_RUN",
    "IMAGE_FILL_THUMB_RATIO",
    "IMAGE_FILL_CARD_RATIO",
    # 2026-09-10 — 제한 병렬(설정 탭 노출). 저장 흐름에서 사라지면 사용자가 고른 동시
    # 실행 수가 조용히 기본값으로 돌아가므로 두 목록 모두에 등록해 보호한다.
    "IMAGE_FILL_PARALLEL",
    "CODEX_IMAGE_MODEL",
    "CODEX_IMAGE_TIMEOUT",
    "CODEX_IMAGE_MAX_CALLS_PER_RUN",
    "CODEX_IMAGE_CONFIG_OVERRIDES",
)


# ──────────────────────────────────────────────────────────────────────
# Public API
# ──────────────────────────────────────────────────────────────────────


def load_cfg() -> dict:
    """user_settings.json 을 로드. 없으면 settings.py 에서 기본값 추출."""
    if _cfg_path().exists():
        try:
            from config.crypto import decrypt_dict, encrypt_dict, has_plaintext_secrets
            raw = json.loads(_cfg_path().read_text(encoding="utf-8"))
            decrypted = decrypt_dict(raw)
            # 평문 민감 필드가 있으면 자동 마이그레이션 (암호화 재저장)
            if has_plaintext_secrets(raw):
                encrypted = encrypt_dict(decrypted)
                from config import atomic_write_json
                atomic_write_json(_cfg_path(), encrypted)
            # 코드 기본값이 올라갔는데 캐시에 옛 기본값이 남아 있으면 여기서 정합한다
            # (사용자가 직접 바꾼 값은 보존 — _reconcile_code_defaults docstring 참고).
            result = _reconcile_code_defaults(decrypted)
            result.setdefault("naver_api_provider", "legacy")
            result.setdefault("tourapi_service_key", "")
            result.setdefault("seoul_api_key", "")
            return result
        except (json.JSONDecodeError, FileNotFoundError, ImportError):
            pass
    try:
        import config.settings as s
        return {
            "naver_id":             s.NAVER_ID,
            "naver_pw":             s.NAVER_PW,
            "no_password_login":    getattr(s, "NAVER_NO_PASSWORD_LOGIN", False),
            "daily_limit":          s.DAILY_POST_LIMIT,
            "posts_per_day":        getattr(s, "POSTS_PER_DAY", 3),
            "interval_sec":         s.POST_INTERVAL_SECONDS,
            "use_reservation":      s.USE_RESERVATION,
            "excel_path":           s.KEYWORD_EXCEL_PATH,
            "sheet_name":           s.KEYWORD_SHEET_NAME,
            "longtail_sheet":       getattr(s, "KEYWORD_LONGTAIL_SHEET", None),
            "publish_pool_sheet":   getattr(s, "KEYWORD_PUBLISH_POOL_SHEET", "발행후보_풀"),
            "use_kakao":            s.USE_KAKAO_NOTIFY,
            "kakao_token":          s.KAKAO_ACCESS_TOKEN,
            "save_preview":         s.SAVE_PREVIEW_HTML,
            "headless":             s.HEADLESS,
            "skip_image_insert":    getattr(s, "SKIP_IMAGE_INSERT", True),
            "use_naver_api":        s.USE_NAVER_API,
            "api_client_id":        s.NAVER_API_CLIENT_ID,
            "api_client_secret":    s.NAVER_API_CLIENT_SECRET,
            "naver_api_provider":   getattr(s, "NAVER_API_PROVIDER", "legacy"),
            "api_hub_key_id":       getattr(s, "NAVER_API_HUB_KEY_ID", ""),
            "api_hub_key":          getattr(s, "NAVER_API_HUB_KEY", ""),
            "tourapi_service_key":  getattr(s, "TOURAPI_SERVICE_KEY", ""),
            "seoul_api_key":        getattr(s, "SEOUL_API_KEY", ""),
            "use_trend_update":     s.USE_TREND_UPDATE,
            "enable_cafe_dispatch": getattr(s, "ENABLE_CAFE_DISPATCH", False),
            "schedule_hours":       s.SCHEDULE_HOURS,
            "max_retry":            s.MAX_RETRY_COUNT,
            "reservation_slots":    s.RESERVATION_TIME_SLOTS,
            "qv_min_chars":         getattr(s, "QV_MIN_CHARS", 1800),
            "qv_max_chars":         getattr(s, "QV_MAX_CHARS", 3200),
            "qv_require_table":     getattr(s, "QV_REQUIRE_TABLE", True),
            "qv_require_faq":       getattr(s, "QV_REQUIRE_FAQ", True),
            "qv_min_h2":            getattr(s, "QV_MIN_H2_COUNT", 3),
            "qv_min_tags":          getattr(s, "QV_MIN_TAGS", 10),
            "qv_max_retries":       getattr(s, "QV_MAX_RETRIES", 2),
            "harness_emit_enabled": getattr(s, "HARNESS_EMIT_ENABLED", True),
            "auto_video_after_publish": getattr(s, "AUTO_VIDEO_AFTER_PUBLISH", False),
            "auto_video_limit":         getattr(s, "AUTO_VIDEO_LIMIT", 5),
            "searchad_api_key":         getattr(s, "SEARCHAD_API_KEY", ""),
            "searchad_secret_key":      getattr(s, "SEARCHAD_SECRET_KEY", ""),
            "searchad_customer_id":     getattr(s, "SEARCHAD_CUSTOMER_ID", ""),
            "gov24_api_key":            getattr(s, "GOV24_API_KEY", ""),
            # 🆓 무료 데이터 소스 API 키(2026-08-16) — gov24 와 동급 공개 데이터 키(평문 무해).
            "youtube_api_key":          getattr(s, "YOUTUBE_API_KEY", ""),
            "ecos_api_key":             getattr(s, "ECOS_API_KEY", ""),
            "kosis_api_key":            getattr(s, "KOSIS_API_KEY", ""),
            "publish_mode":             getattr(s, "PUBLISH_MODE", "spawn"),
            "chrome_attach_port":       getattr(s, "CHROME_ATTACH_PORT", 9222),
            "chrome_attach_autostart":  getattr(s, "CHROME_ATTACH_AUTOSTART", True),
            "chrome_session_profile_dir": getattr(s, "CHROME_SESSION_PROFILE_DIR", None),
        }
    except (ImportError, AttributeError):
        return {}


def save_cfg(d: dict, notify: Optional[Callable[[dict], None]] = None) -> None:
    """GUI 설정 저장. user_settings.json 암호화 저장 + settings.py 안전 in-place 패치.

    settings.py 가 손상되면 백업이 자동 생성되어 있으니 사용자가 수동 복구 가능.
    저장 직후 핵심 키 sanity check 실패 시 경고 로그 발생 (예외는 raise 하지 않음).

    Args:
        d: GUI 가 넘긴 설정 dict.
        notify: 저장 직후 호출되는 콜백. dict 인자로 {"backup": Path, "diff_lines":
            list[str], "missing_keys": list[str], "changed_keys": list[str]} 를 받는다.
            GUI 에서 토스트·상태바 등에 표시할 수 있다. None 이면 호출 안 함.
    """
    from config.crypto import encrypt_dict
    from config import atomic_write_json

    # 저장 시점의 코드 기본값을 함께 기록 — 다음 상향 때 "기본값을 따르던 값"과
    # "사용자가 정한 값"을 구별하는 기준이 된다(_reconcile_code_defaults).
    payload = dict(d)
    baseline = payload.get(_CODE_DEFAULT_BASELINE_KEY)
    merged = dict(baseline) if isinstance(baseline, dict) else {}
    merged.update(_current_code_defaults())
    payload[_CODE_DEFAULT_BASELINE_KEY] = merged

    encrypted = encrypt_dict(payload)
    atomic_write_json(_cfg_path(), encrypted)

    _patch_settings_py_safely(d, notify=notify)


# ──────────────────────────────────────────────────────────────────────
# settings.py 안전 패치 동선
# ──────────────────────────────────────────────────────────────────────


def _patch_settings_py_safely(
    d: dict,
    notify: Optional[Callable[[dict], None]] = None,
) -> None:
    """settings.py 를 안전하게 갱신: 백업 → in-place 패치 → sanity check → diff 알림."""
    settings_path = SETTINGS_PY_PATH

    if not settings_path.exists():
        # 신규 설치 — 최소 골격을 만들어 둔다 (관리 키만 포함).
        log.warning("[config_io] settings.py 없음 — 최소 골격 생성")
        _write_minimal_settings_py(settings_path, d)
        if notify is not None:
            _safe_notify(notify, {
                "backup": None,
                "diff_lines": [],
                "missing_keys": [],
                "changed_keys": [],
                "is_initial_skeleton": True,
            })
        return

    # 1) 타임스탬프 백업
    backup_path = _backup_settings_py(settings_path)
    log.info("[config_io] settings.py 백업 생성: %s", backup_path.name)
    try:
        original = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error("[config_io] settings.py 읽기 실패: %s", exc, exc_info=True)
        raise

    # 2) in-place 패치
    try:
        patched = _patch_managed_keys(original, d)
        settings_path.write_text(patched, encoding="utf-8")
    except OSError as exc:
        log.error("[config_io] settings.py 패치 실패: %s", exc, exc_info=True)
        raise

    # 3) sanity check — 핵심 키가 살아있는지 텍스트 매칭
    missing = _sanity_check_critical_keys(settings_path)
    rolled_back = False
    if missing:
        # 자동 롤백: 핵심 키가 소실됐다면 in-place 패치가 손상을 낸 것이므로
        # 방금 만든 백업으로 즉시 원상복구한다. 수동 복구는 롤백 실패 시의 최후수단.
        log.error(
            "[설정 저장 실패] 핵심 키 누락: %s — 백업 %s 로 자동 롤백 시도",
            missing, backup_path.name,
        )
        rolled_back = _restore_from_backup(settings_path, backup_path, original)
        if rolled_back:
            log.info(
                "[config_io] 자동 롤백 완료 — settings.py 를 저장 전 상태로 원상복구함")
            # 롤백했으므로 이번 저장은 없던 일. 원본 대비 변경 없음으로 알린다.
            if notify is not None:
                _safe_notify(notify, {
                    "backup": backup_path,
                    "diff_lines": [],
                    "missing_keys": missing,
                    "changed_keys": [],
                    "is_initial_skeleton": False,
                    "rolled_back": True,
                })
            return
        # 롤백조차 실패 — 백업 파일은 그대로 남아있으니 수동 복구 안내로 폴백.
        log.error(
            "[config_io] 자동 롤백 실패 — 백업 %s 에서 수동 복구 필요", backup_path.name)

    # 4) diff 계산 — 변경 라인을 로그로 남기고 콜백에도 전달
    diff_lines, changed_keys = _compute_settings_diff(original, patched)
    if diff_lines:
        log.info("[config_io] 변경 키 %d개: %s", len(changed_keys), ", ".join(changed_keys))
        for line in diff_lines:
            log.debug("  %s", line)
    else:
        log.info("[config_io] 변경 사항 없음 (no-op save)")

    if notify is not None:
        _safe_notify(notify, {
            "backup": backup_path,
            "diff_lines": diff_lines,
            "missing_keys": missing,
            "changed_keys": changed_keys,
            "is_initial_skeleton": False,
            "rolled_back": rolled_back,
        })


def _compute_settings_diff(original: str, patched: str) -> Tuple[List[str], List[str]]:
    """저장 전후 settings.py 텍스트를 비교해 변경 라인과 변경된 키 목록을 반환.

    Args:
        original: 저장 직전 settings.py 텍스트.
        patched: 저장 직후 settings.py 텍스트.

    Returns:
        (diff_lines, changed_keys)
        diff_lines: unified diff 의 `-`/`+` 라인 (헤더 제외).
        changed_keys: 변경된 라인에서 추출한 `KEY = ...` 의 KEY 목록 (중복 제거).
    """
    if original == patched:
        return [], []

    diff_iter = difflib.unified_diff(
        original.splitlines(),
        patched.splitlines(),
        fromfile="settings.py (before)",
        tofile="settings.py (after)",
        lineterm="",
        n=0,
    )
    diff_lines: List[str] = []
    key_pattern = re.compile(r"^[+-]\s*([A-Z_][A-Z0-9_]*)\s*=")
    changed: List[str] = []
    seen: set = set()
    for line in diff_iter:
        if line.startswith("+++") or line.startswith("---") or line.startswith("@@"):
            continue
        diff_lines.append(line)
        match = key_pattern.match(line)
        if match:
            key = match.group(1)
            if key not in seen:
                seen.add(key)
                changed.append(key)
    return diff_lines, changed


def _safe_notify(notify: Callable[[dict], None], payload: dict) -> None:
    """notify 콜백 호출 실패가 저장 흐름을 중단시키지 않도록 격리."""
    try:
        notify(payload)
    except Exception as exc:  # noqa: BLE001 — 외부 콜백의 어떤 예외도 흡수
        log.warning("[config_io] notify 콜백 예외 (무시): %s", exc)


def _backup_settings_py(settings_path: Path) -> Path:
    """`settings.py.bak.YYYYMMDD_HHMMSS` 형식으로 백업 생성."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = settings_path.with_suffix(f".py.bak.{timestamp}")
    shutil.copy2(settings_path, backup_path)
    return backup_path


def _restore_from_backup(
    settings_path: Path,
    backup_path: Path,
    original_text: str,
) -> bool:
    """손상된 settings.py 를 백업으로 원상복구한다.

    1순위는 방금 만든 백업 파일 복사(메타데이터 포함 복원), 백업이 손상/부재하면
    메모리에 남아있는 저장 전 원본 텍스트로 재기록(2순위)한다. 두 경로 모두
    복구 직후 핵심 키가 되살아났는지 재검증한다.

    Args:
        settings_path: 복구 대상(손상된 settings.py).
        backup_path: 저장 직전 생성된 타임스탬프 백업.
        original_text: 저장 직전 읽어둔 settings.py 원본 텍스트(메모리 폴백용).

    Returns:
        복구 후 핵심 키가 모두 살아있으면 True, 아니면 False.
    """
    # 1순위 — 백업 파일에서 복사.
    if backup_path.exists():
        try:
            shutil.copy2(backup_path, settings_path)
            if not _sanity_check_critical_keys(settings_path):
                return True
            log.error(
                "[config_io] 백업 복사 후에도 핵심 키 누락 — 메모리 원본으로 재시도")
        except OSError as exc:
            log.error(
                "[config_io] 백업 복사 실패: %s — 메모리 원본으로 재시도",
                exc, exc_info=True)

    # 2순위 — 메모리에 남은 저장 전 원본 텍스트로 재기록.
    try:
        settings_path.write_text(original_text, encoding="utf-8")
    except OSError as exc:
        log.error("[config_io] 메모리 원본 재기록 실패: %s", exc, exc_info=True)
        return False
    return not _sanity_check_critical_keys(settings_path)


def backup_dir() -> Path:
    """settings.py 백업이 쌓이는 디렉토리(= config/). 백업 폴더 열기 헬퍼용."""
    return SETTINGS_PY_PATH.parent


def _patch_managed_keys(source: str, d: dict) -> str:
    """settings.py 소스에서 GUI 관리 키만 in-place 로 교체.

    화이트리스트(_GUI_MANAGED_KEYS) 의 키만 정규식 매치 후 새 값으로 교체한다.
    매치되지 않은 키는 파일 끝에 append. 그 외 모든 줄(상수·dict·주석)은
    원형 그대로 보존된다.

    Args:
        source: 원본 settings.py 텍스트.
        d: GUI 가 넘긴 새 설정 dict.

    Returns:
        패치된 settings.py 텍스트.
    """
    new_values = _build_managed_values(d)

    patched = source
    appended: List[str] = []

    for key, value_literal in new_values.items():
        replacement, count = _replace_assignment(patched, key, value_literal)
        if count > 0:
            patched = replacement
        else:
            # 키가 settings.py 에 아직 없음 — 끝에 append
            appended.append(f"{key:<22} = {value_literal}")

    # 새 API 자격증명은 평문 리터럴을 settings.py 에 쓰지 않는다. 구버전 설정 파일에도
    # 안전한 암호화 저장소 참조를 추가해 critical-key 검사와 신규 기능이 함께 동작하게 한다.
    secret_assignments = {
        "NAVER_API_HUB_KEY_ID": 'load_shared_secret("api_hub_key_id")',
        "NAVER_API_HUB_KEY": 'load_shared_secret("api_hub_key")',
        "TOURAPI_SERVICE_KEY": 'load_shared_secret("tourapi_service_key")',
        "SEOUL_API_KEY": 'load_shared_secret("seoul_api_key")',
    }
    appended_secrets = []
    for key, expression in secret_assignments.items():
        if not re.search(rf"^{re.escape(key)}\s*=", patched, re.MULTILINE):
            appended_secrets.append(f"{key:<22} = {expression}")
    if appended_secrets:
        if not re.search(r"^\s*from\s+config\.crypto\s+import\s+[^\n]*\bload_shared_secret\b",
                         patched, re.MULTILINE):
            appended.append("from config.crypto import load_shared_secret")
        appended.extend(appended_secrets)

    if appended:
        if not patched.endswith("\n"):
            patched += "\n"
        patched += "\n# ── GUI 자동 추가 (in-place 패치) ──\n"
        patched += "\n".join(appended) + "\n"

    return patched


def _replace_assignment(source: str, key: str, value_literal: str) -> Tuple[str, int]:
    """`KEY = ...` 한 줄을 새 값으로 교체. 줄 끝의 트레일링 주석은 보존.

    Returns:
        (새 텍스트, 교체 횟수)
    """
    # 줄 시작에서 `KEY` 와 공백·`=` 만 매치하고 값 부분 + 트레일링 주석을 통째로 교체.
    # 주석(`# ...`) 은 보존하려고 별도 캡처.
    pattern = re.compile(
        rf"^(?P<lead>{re.escape(key)}\s*=\s*)(?P<value>.+?)(?P<comment>\s*#.*)?$",
        re.MULTILINE,
    )

    def _sub(match: re.Match) -> str:
        comment = match.group("comment") or ""
        return f"{match.group('lead')}{value_literal}{comment}"

    new_source, count = pattern.subn(_sub, source, count=1)
    return new_source, count


def _build_managed_values(d: dict) -> dict:
    """GUI 설정 dict 에서 settings.py 에 기록할 파이썬 리터럴들을 생성."""
    def _int(val, default: int) -> int:
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    def _float(val, default: float) -> float:
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    def _bool(val, default: bool) -> bool:
        if isinstance(val, bool):
            return val
        if isinstance(val, str):
            return val.lower() in ("true", "1", "yes")
        return default

    def _live(name: str, default):
        """현재 settings.py 값(무관 저장이 신규 키를 리셋하지 않도록 폴백 기본값)."""
        return _live_setting(name, default)

    # ── 발행 경로 이원화(P0-①) ─────────────────────────────────────────
    # d 가 이 키들을 안 실어오면(다른 탭 저장 등) 현재 settings 값을 그대로 보존해
    # PUBLISH_MODE 가 조용히 spawn 으로 리셋되는 사고를 막는다.
    _pm_raw = d.get("publish_mode")
    if _pm_raw is None:
        _pm_raw = _live("PUBLISH_MODE", "spawn")
    _publish_mode = str(_pm_raw).lower().strip()
    if _publish_mode not in ("spawn", "attach"):
        _publish_mode = "spawn"

    _attach_port = _int(d.get("chrome_attach_port"),
                        _int(_live("CHROME_ATTACH_PORT", 9222), 9222))
    _attach_autostart = _bool(d.get("chrome_attach_autostart"),
                              bool(_live("CHROME_ATTACH_AUTOSTART", True)))
    _session_dir = d.get("chrome_session_profile_dir")
    if _session_dir is None:
        _session_dir = _live("CHROME_SESSION_PROFILE_DIR", None)
    if _session_dir in (None, "", "None"):
        _session_dir_literal = "None"
    else:
        _session_dir_literal = json.dumps(str(_session_dir), ensure_ascii=False)

    longtail = d.get("longtail_sheet")
    longtail_literal = json.dumps(longtail, ensure_ascii=False) if longtail else "None"

    excel_path = d.get("excel_path", "네이버 키워드별 검색량.xlsx")

    # 검색광고 API 는 자격증명 3개가 모두 채워졌을 때만 켠다(별도 토글 없이 자동).
    # 하나라도 비면 False → searchad_volume.available() 이 False 로 휴면.
    _searchad_ready = all(
        str(d.get(k, "") or "").strip()
        for k in ("searchad_api_key", "searchad_secret_key", "searchad_customer_id"))

    return {
        "NAVER_NO_PASSWORD_LOGIN":   str(_bool(d.get("no_password_login"), False)),
        "DAILY_POST_LIMIT":          str(_int(d.get("daily_limit"), 8)),
        "POSTS_PER_DAY":             str(_int(d.get("posts_per_day"), 3)),
        "POST_INTERVAL_SECONDS":     str(_int(d.get("interval_sec"), 180)),
        "SCHEDULE_HOURS":            json.dumps(d.get("schedule_hours", _DEFAULT_HOURS), ensure_ascii=False),
        "USE_RESERVATION":           str(_bool(d.get("use_reservation"), True)),
        "RESERVATION_TIME_SLOTS":    json.dumps(d.get("reservation_slots", _DEFAULT_SLOTS), ensure_ascii=False),
        "KEYWORD_EXCEL_PATH":        json.dumps(excel_path, ensure_ascii=False),
        "KEYWORD_SHEET_NAME":        json.dumps(d.get("sheet_name", "메인키워드"), ensure_ascii=False),
        "KEYWORD_LONGTAIL_SHEET":    longtail_literal,
        "KEYWORD_PUBLISH_POOL_SHEET": json.dumps(d.get("publish_pool_sheet", "발행후보_풀"), ensure_ascii=False),
        "USE_NAVER_API":             str(_bool(d.get("use_naver_api"), False)),
        "NAVER_API_PROVIDER":        json.dumps(
            str(d.get("naver_api_provider") or _live("NAVER_API_PROVIDER", "legacy"))
            if str(d.get("naver_api_provider") or _live("NAVER_API_PROVIDER", "legacy"))
            in ("legacy", "api_hub") else "legacy", ensure_ascii=False),
        "USE_TREND_UPDATE":          str(_bool(d.get("use_trend_update"), True)),
        "ENABLE_CAFE_DISPATCH":      str(_bool(d.get("enable_cafe_dispatch"), False)),
        "USE_KAKAO_NOTIFY":          str(_bool(d.get("use_kakao"), False)),
        "SAVE_PREVIEW_HTML":         str(_bool(d.get("save_preview"), True)),
        "SKIP_IMAGE_INSERT":         str(_bool(d.get("skip_image_insert"), True)),
        "HEADLESS":                  str(_bool(d.get("headless"), False)),
        "MAX_RETRY_COUNT":           str(_int(d.get("max_retry"), 2)),
        "QV_MIN_CHARS":              str(_int(d.get("qv_min_chars"), 1800)),
        "QV_MAX_CHARS":              str(_int(d.get("qv_max_chars"), 3200)),
        "QV_REQUIRE_TABLE":          str(_bool(d.get("qv_require_table"), True)),
        "QV_REQUIRE_FAQ":            str(_bool(d.get("qv_require_faq"), True)),
        "QV_MIN_H2_COUNT":           str(_int(d.get("qv_min_h2"), 3)),
        "QV_MIN_TAGS":               str(_int(d.get("qv_min_tags"), 10)),
        "QV_MAX_RETRIES":            str(_int(d.get("qv_max_retries"), 2)),
        "HARNESS_EMIT_ENABLED":      str(_bool(d.get("harness_emit_enabled"), True)),
        "AUTO_VIDEO_AFTER_PUBLISH":  str(_bool(d.get("auto_video_after_publish"), False)),
        "AUTO_VIDEO_LIMIT":          str(_int(d.get("auto_video_limit"), 5)),
        "SEARCHAD_API_ENABLED":      str(bool(_searchad_ready)),
        "PUBLISH_MODE":              json.dumps(_publish_mode, ensure_ascii=False),
        "CHROME_ATTACH_PORT":        str(_attach_port),
        "CHROME_ATTACH_AUTOSTART":   str(_attach_autostart),
        "CHROME_SESSION_PROFILE_DIR": _session_dir_literal,
        # 사진 인제스트(P0-② Phase 2) — GUI 미노출이라 현재 settings 값을 그대로 방출해
        # in-place 저장에서 조용히 기본값으로 리셋되는 사고를 막는다.
        # (PHOTO_INBOX_DIR 는 동적 표현식이라 방출 제외 — 위 화이트리스트 주석 참고.)
        "PHOTO_INGEST_ENABLED":      str(_bool(d.get("photo_ingest_enabled"),
                                              bool(_live("PHOTO_INGEST_ENABLED", True)))),
        "PHOTO_VISION_MODEL":        json.dumps(
            str(d.get("photo_vision_model") or _live("PHOTO_VISION_MODEL", "claude")),
            ensure_ascii=False),
        "PHOTO_VISION_TIMEOUT":      str(_int(d.get("photo_vision_timeout"),
                                             _int(_live("PHOTO_VISION_TIMEOUT", 180), 180))),
        # 본문 분석형 인포카드(2026-07-23) — GUI 미노출이라 현재 settings 값을 그대로
        # 방출해 무관 저장이 기본값으로 리셋하는 사고를 막는다(기본 OFF 옵트인).
        "IMAGE_INFOCARD_ENABLED":    str(_bool(d.get("image_infocard_enabled"),
                                              bool(_live("IMAGE_INFOCARD_ENABLED", False)))),
        "IMAGE_INFOCARD_BUDGET":     str(_int(d.get("image_infocard_budget"),
                                             _int(_live("IMAGE_INFOCARD_BUDGET", 5), 5))),
        "IMAGE_INFOCARD_SIZE":       json.dumps(
            str(d.get("image_infocard_size") or _live("IMAGE_INFOCARD_SIZE", "1024x1280")),
            ensure_ascii=False),
        "IMAGE_MODEL_SHEET_PATH":    json.dumps(
            str(d.get("image_model_sheet_path") or _live("IMAGE_MODEL_SHEET_PATH", "")),
            ensure_ascii=False),
        # 순위 추적 구체 롱테일 쿼리(2026-08-01) — GUI 미노출이라 현재 settings 값을
        # 그대로 방출해 무관 저장이 기본값으로 리셋하는 사고를 막는다(기본 ON).
        "RANK_TRACK_SPECIFIC":       str(_bool(d.get("rank_track_specific"),
                                              bool(_live("RANK_TRACK_SPECIFIC", True)))),
        "RANK_SPECIFIC_MIN_VOLUME":  str(_int(d.get("rank_specific_min_volume"),
                                             _int(_live("RANK_SPECIFIC_MIN_VOLUME", 500), 500))),
        "RANK_SPECIFIC_MAX_VOLUME":  str(_int(d.get("rank_specific_max_volume"),
                                             _int(_live("RANK_SPECIFIC_MAX_VOLUME", 3000), 3000))),
        # 경쟁 블로그 분석(2026-08-02) — GUI 미노출이라 현재 settings 값을 그대로 방출해
        # 무관 저장이 기본값으로 리셋하는 사고를 막는다(기본 OFF 옵트인).
        "COMPETITOR_ANALYSIS_ENABLED": str(_bool(d.get("competitor_analysis_enabled"),
                                                bool(_live("COMPETITOR_ANALYSIS_ENABLED", False)))),
        "COMPETITOR_MIN_RELEVANCE":  repr(_float(d.get("competitor_min_relevance"),
                                                _float(_live("COMPETITOR_MIN_RELEVANCE", 0.40), 0.40))),
        "COMPETITOR_MAX_BLOGS":      str(_int(d.get("competitor_max_blogs"),
                                             _int(_live("COMPETITOR_MAX_BLOGS", 12), 12))),
        "COMPETITOR_POSTS_PER_BLOG": str(_int(d.get("competitor_posts_per_blog"),
                                             _int(_live("COMPETITOR_POSTS_PER_BLOG", 20), 20))),
        # 설계도 주도 변주(2026-08-25) — GUI 미노출이라 현재 settings 값을 그대로 방출해
        # 무관 저장이 기본값(False)으로 리셋하는 사고를 막는다(기본 OFF 옵트인).
        "BLUEPRINT_ENABLED":         str(_bool(d.get("blueprint_enabled"),
                                              bool(_live("BLUEPRINT_ENABLED", False)))),
        # AI 티 스코어러 Phase A(2026-08-29) — GUI 미노출이라 현재 settings 값을 그대로 방출해
        # 무관 저장이 기본값(False)으로 리셋하는 사고를 막는다(관측 전용 옵트인).
        "AI_TELL_SCORE_ENABLED":     str(_bool(d.get("ai_tell_score_enabled"),
                                              bool(_live("AI_TELL_SCORE_ENABLED", False)))),
        # 안티-AI-티 규칙 프롬프트 이식 Phase C(2026-08-29) — GUI 미노출이라 현재 settings 값을
        # 그대로 방출해 무관 저장이 기본값(False)으로 리셋하는 사고를 막는다(옵트인).
        "ANTI_AI_TELL_PROMPT":       str(_bool(d.get("anti_ai_tell_prompt"),
                                              bool(_live("ANTI_AI_TELL_PROMPT", False)))),
        # 리스타일 배치 AI 티 우선순위 정렬 Phase D(2026-08-29) — GUI 미노출이라 현재
        # settings 값을 그대로 방출해 무관 저장이 기본값(False)으로 리셋하는 사고를 막는다.
        "RESTYLE_ORDER_BY_AI_TELL":  str(_bool(d.get("restyle_order_by_ai_tell"),
                                              bool(_live("RESTYLE_ORDER_BY_AI_TELL", False)))),
        # 젠스파크 이미지 자동 채움(Phase 2a, 2026-08-29) — GUI 미노출이라 현재 settings
        # 값을 그대로 방출해 무관 저장이 기본값으로 리셋하는 사고를 막는다(기본 OFF 옵트인).
        "GENSPARK_ENABLED":          str(_bool(d.get("genspark_enabled"),
                                              bool(_live("GENSPARK_ENABLED", False)))),
        "GENSPARK_THUMB_RATIO":      json.dumps(
            str(d.get("genspark_thumb_ratio") or _live("GENSPARK_THUMB_RATIO", "1:1")),
            ensure_ascii=False),
        "GENSPARK_CARD_RATIO":       json.dumps(
            str(d.get("genspark_card_ratio") or _live("GENSPARK_CARD_RATIO", "2:3")),
            ensure_ascii=False),
        "GENSPARK_INFOCARD_BUDGET":  str(_int(d.get("genspark_infocard_budget"),
                                             _int(_live("GENSPARK_INFOCARD_BUDGET", 3), 3))),
        "GENSPARK_MAX_POSTS_PER_RUN": str(_int(d.get("genspark_max_posts_per_run"),
                                              _int(_live("GENSPARK_MAX_POSTS_PER_RUN", 25), 25))),
        "GENSPARK_AUTO_SWITCH":      str(_bool(d.get("genspark_auto_switch"),
                                              bool(_live("GENSPARK_AUTO_SWITCH", True)))),
        "GENSPARK_VERIFY_MAX_REGEN": str(_int(d.get("genspark_verify_max_regen"),
                                             _int(_live("GENSPARK_VERIFY_MAX_REGEN", 2), 2))),
        "GENSPARK_VISION_MODEL":     json.dumps(
            str(d.get("genspark_vision_model") or _live("GENSPARK_VISION_MODEL", "claude")),
            ensure_ascii=False),
        "GENSPARK_QUALITY":          json.dumps(
            str(d.get("genspark_quality") or _live("GENSPARK_QUALITY", "자동")),
            ensure_ascii=False),
        # 이미지 채움(엔진 중립) + Codex 이미지 엔진(2026-09-09) — GUI 미노출이라 현재
        # settings 값을 그대로 방출해 무관 저장이 기본값으로 리셋하는 사고를 막는다
        # (IMAGE_FILL_ENABLED 기본 ON — 젠스파크에서 codex 로 기본 엔진 이전).
        "IMAGE_FILL_ENABLED":        str(_bool(d.get("image_fill_enabled"),
                                              bool(_live("IMAGE_FILL_ENABLED", True)))),
        "IMAGE_FILL_ENGINE":         json.dumps(
            str(d.get("image_fill_engine") or _live("IMAGE_FILL_ENGINE", "codex")),
            ensure_ascii=False),
        "IMAGE_FILL_CARDS":          str(_int(d.get("image_fill_cards"),
                                             _int(_live("IMAGE_FILL_CARDS", 3), 3))),
        "IMAGE_FILL_MAX_POSTS_PER_RUN": str(_int(d.get("image_fill_max_posts_per_run"),
                                              _int(_live("IMAGE_FILL_MAX_POSTS_PER_RUN", 25), 25))),
        "IMAGE_FILL_THUMB_RATIO":    json.dumps(
            str(d.get("image_fill_thumb_ratio") or _live("IMAGE_FILL_THUMB_RATIO", "1:1")),
            ensure_ascii=False),
        "IMAGE_FILL_CARD_RATIO":     json.dumps(
            str(d.get("image_fill_card_ratio") or _live("IMAGE_FILL_CARD_RATIO", "2:3")),
            ensure_ascii=False),
        # 제한 병렬(2026-09-10) — GUI 노출 필드. 코드 기본값 2, 상한은 소비자
        # (draft_image_fill._parallel_workers)가 4로 clamp 한다.
        "IMAGE_FILL_PARALLEL":       str(_int(d.get("image_fill_parallel"),
                                             _int(_live("IMAGE_FILL_PARALLEL", 2), 2))),
        "CODEX_IMAGE_MODEL":         json.dumps(
            str(d.get("codex_image_model") or _live("CODEX_IMAGE_MODEL", "gpt-5.5")),
            ensure_ascii=False),
        "CODEX_IMAGE_TIMEOUT":       str(_int(d.get("codex_image_timeout"),
                                             _int(_live("CODEX_IMAGE_TIMEOUT", 600), 600))),
        "CODEX_IMAGE_MAX_CALLS_PER_RUN": str(_int(d.get("codex_image_max_calls_per_run"),
                                              _int(_live("CODEX_IMAGE_MAX_CALLS_PER_RUN", 40), 40))),
        "CODEX_IMAGE_CONFIG_OVERRIDES": json.dumps(
            str(d.get("codex_image_config_overrides")
                or _live("CODEX_IMAGE_CONFIG_OVERRIDES", "")),
            ensure_ascii=False),
    }


def _sanity_check_critical_keys(settings_path: Path) -> List[str]:
    """settings.py 텍스트에서 핵심 키가 살아있는지 확인.

    import 대신 텍스트 매칭으로 검증 — sys.modules 캐시·환경 의존성을 피하기 위함.
    Returns:
        누락된 키 이름 리스트. 빈 리스트면 모두 정상.
    """
    try:
        source = settings_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("[config_io] sanity check 읽기 실패: %s", exc)
        return list(_CRITICAL_PRESERVED_KEYS)

    missing: List[str] = []
    for key in _CRITICAL_PRESERVED_KEYS:
        # `KEY =` 패턴이 줄 시작에서 1회 이상 나타나야 함
        pattern = re.compile(rf"^{re.escape(key)}\s*=", re.MULTILINE)
        if not pattern.search(source):
            missing.append(key)
    return missing


def _write_minimal_settings_py(settings_path: Path, d: dict) -> None:
    """settings.py 부재 시 최소 골격 생성. 관리 키만 기록 — 수동 추가 상수는 없다."""
    new_values = _build_managed_values(d)
    lines = [
        "# config/settings.py — GUI 자동 생성 (최소 골격)",
        "from config.crypto import load_secret, load_shared_secret  # noqa: F401",
        "",
        'NAVER_ID = load_secret("naver_id", "your_naver_id")',
        'NAVER_PW = load_secret("naver_pw")',
        'NAVER_API_CLIENT_ID = load_secret("api_client_id", "your_client_id")',
        'NAVER_API_CLIENT_SECRET = load_secret("api_client_secret")',
        'NAVER_API_PROVIDER = "legacy"',
        'NAVER_API_HUB_KEY_ID = load_shared_secret("api_hub_key_id")',
        'NAVER_API_HUB_KEY = load_shared_secret("api_hub_key")',
        'TOURAPI_SERVICE_KEY = load_shared_secret("tourapi_service_key")',
        'SEOUL_API_KEY = load_shared_secret("seoul_api_key")',
        'KAKAO_ACCESS_TOKEN = load_secret("kakao_token")',
    ]
    for key, value_literal in new_values.items():
        lines.append(f"{key} = {value_literal}")
    settings_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# 하위 호환: 기존 코드가 `_write_py` 를 호출하던 경우를 위한 얇은 별칭.
def _write_py(d: dict) -> None:  # pragma: no cover — 외부 호출 없을 가능성
    """[Deprecated] _patch_settings_py_safely 로 대체. 시그니처만 유지."""
    _patch_settings_py_safely(d)
