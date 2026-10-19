"""공공기관·시장 정보 사이트 자동 스크린샷 (정부24 + 홈택스 + 청약홈 + 국민연금공단).

`gov24_screenshot.py` 의 헤드리스 캡처 패턴을 4개 사이트로 확장한 통합 모듈.
사이트별 셀렉터·URL 매핑을 dict 로 관리하고 카테고리에 맞춰 디스패치한다.

사용 흐름:
    from core.public_site_screenshot import capture_for_category
    shots = capture_for_category(title, post_id, category="절세", driver=...)

설계 원칙:
- 기존 gov24_screenshot.py 은 보존 (auto_scheduler 의 capture_gov24_page 호출 호환)
- 헤드리스 드라이버 생성은 gov24_screenshot.make_screenshot_driver 재사용
- 셀렉터가 모두 실패해도 페이지 통째 캡처(robust fallback)
- 빈 파일 검사 + 에러 격리 (gov24 패턴과 동일)
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.logger import get_logger
log = get_logger(__name__)

from selenium.webdriver.common.by import By
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException, WebDriverException,
)

from core.gov24_screenshot import make_screenshot_driver, SCREENSHOT_DIR

_BASE_DIR = Path(__file__).resolve().parent.parent
_URL_JSON = _BASE_DIR / "templates" / "public_site_urls.json"


# ─────────────────────────────────────────────────────
# 사이트별 캡처 설정
# ─────────────────────────────────────────────────────

# fallback 의 일반적 팝업/배너 셀렉터 — 사이트 무관하게 공통 적용
_GENERIC_POPUP_SELECTORS = [
    ".layerPopup .btnClose", ".pop_close", "#closeBtn", ".modal-close",
    "button.close", ".btn-close", ".popup-close", ".popup .close",
    "[aria-label='닫기']", ".today-close", ".cookie-banner .close",
]

# 사이트별 추가 팝업·콘텐츠 영역 셀렉터
SITE_CONFIGS = {
    "gov24": {
        "label": "정부24",
        "popup_extra": [],
        "summary_selectors": [
            ".svcInfo_detail", ".service-summary", ".content-area",
            "#contents", "main",
        ],
        "section_keywords": ["신청방법", "신청 방법", "신청절차"],
    },
    "hometax": {
        "label": "홈택스",
        "popup_extra": [
            "#mf_wfm_layout_popupHeader_btnPopupClose",  # 홈택스 메인 팝업 (mf_ 접두)
            ".popup_layer .close", ".close_btn",
        ],
        "summary_selectors": [
            "#mf_wfm_layout_main", ".main-section", ".content-wrap",
            "#contents", "main",
        ],
        "section_keywords": ["신고방법", "신고 방법", "납부방법", "신청방법"],
    },
    "chungyak": {
        "label": "청약홈",
        "popup_extra": [
            ".pop_wrap .btn_close", ".popUp_close", ".btn_pop_close",
        ],
        "summary_selectors": [
            "#contentsArea", ".content_wrap", ".sub-cont",
            "#contents", "main",
        ],
        "section_keywords": ["신청방법", "청약자격", "청약절차", "당첨자발표"],
    },
    "nps": {
        "label": "국민연금공단",
        "popup_extra": [
            ".popup_wrap .btn_close", ".pop-close", "#popClose",
        ],
        "summary_selectors": [
            "#container", ".sub_contents", ".contents",
            "#contents", "main",
        ],
        "section_keywords": ["수급요건", "가입대상", "신청방법", "수령액", "수급자격"],
    },
}

# examplebrand 12 카테고리 → 캡처 사이트 매핑
# None 이면 캡처 건너뜀 (디지털·금융 등 공식 단일 사이트가 없는 카테고리)
CATEGORY_TO_SITE = {
    # 정부지원 7종
    "청년":   "gov24",
    "주거":   "gov24",
    "복지":   "gov24",
    "바우처": "gov24",
    "취업":   "gov24",
    "의료":   "gov24",
    "교육":   "gov24",
    # 비즈니스·경제 3종
    "부동산": "chungyak",
    "절세":   "hometax",
    "금융":   None,     # 단일 공식 사이트 없음 — 추후 시드
    # life-stage
    "노후":   "nps",
    # 시니어 디지털 — 화면 캡처는 별도(에뮬레이터·실제 기기)
    "디지털": None,
    # fallback
    "default": "gov24",
}


# ─────────────────────────────────────────────────────
# URL 매핑 로드
# ─────────────────────────────────────────────────────

_url_data_cache: dict | None = None


def _load_url_data() -> dict:
    """templates/public_site_urls.json 을 로드(캐시).

    실패 시 빈 dict 를 반환해 캡처는 default_url 로만 동작한다.
    """
    global _url_data_cache
    if _url_data_cache is not None:
        return _url_data_cache
    url_json = _URL_JSON
    try:
        from config import brand as _brand
        url_json = _brand.template_path("public_site_urls.json")
    except Exception:  # noqa: BLE001
        pass
    try:
        with url_json.open(encoding="utf-8") as fh:
            _url_data_cache = json.load(fh)
    except (OSError, ValueError) as e:
        log.warning(f"public_site_urls.json 로드 실패 ({e}) — default URL 만 사용")
        _url_data_cache = {}
    return _url_data_cache


def _find_url_for_site(title: str, site: str) -> str:
    """제목 키워드로 사이트별 URL 매핑.

    gov24 는 settings.GOVERNMENT24_URL_MAP (기존 자산) 우선 사용.
    나머지 사이트는 public_site_urls.json 에서 로드.
    """
    if site == "gov24":
        try:
            from core.gov24_screenshot import find_gov24_url
            return find_gov24_url(title)
        except Exception:
            pass
    data = _load_url_data().get(site, {})
    url_map = data.get("url_map", {})
    for keyword, url in url_map.items():
        if keyword in title:
            return url
    return data.get("default_url") or ""


# ─────────────────────────────────────────────────────
# 캡처 엔진 (사이트별 dispatch)
# ─────────────────────────────────────────────────────

def _close_popups(driver, extra_selectors: list[str]) -> None:
    selectors = list(_GENERIC_POPUP_SELECTORS) + list(extra_selectors or [])
    for sel in selectors:
        try:
            els = driver.find_elements(By.CSS_SELECTOR, sel)
            for el in els[:3]:
                try:
                    el.click()
                    time.sleep(0.3)
                except (WebDriverException, NoSuchElementException):
                    pass
        except WebDriverException:
            pass


def _expand_window_to_page(driver, max_height: int = 4000) -> None:
    try:
        total_h = driver.execute_script("return document.body.scrollHeight")
        total_h = min(int(total_h or 1200), max_height)
        driver.set_window_size(1280, total_h)
        time.sleep(1)
    except WebDriverException:
        pass


def _scroll_into_view_first_match(driver, selectors: list[str]) -> bool:
    for sel in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
            driver.execute_script("arguments[0].scrollIntoView(true);", el)
            time.sleep(0.6)
            return True
        except (NoSuchElementException, WebDriverException):
            continue
    return False


def _save_safe(driver, path: str) -> bool:
    try:
        driver.save_screenshot(path)
        if os.path.exists(path) and os.path.getsize(path) > 0:
            log.info(f"스크린샷 저장: {path}")
            return True
        log.warning(f"빈 파일 — 삭제: {path}")
        if os.path.exists(path):
            os.remove(path)
    except (WebDriverException, OSError) as e:
        log.warning(f"스크린샷 저장 실패 ({path}): {e}")
    return False


def capture_for_category(title: str, post_id: str, category: str,
                         driver=None) -> list[str]:
    """카테고리에 맞는 공공기관 사이트에서 최대 3장 캡처.

    Args:
        title: 글 제목 (URL 매핑 키워드 추출용)
        post_id: 파일명 접두 (예: "0508_01")
        category: detect_category 결과 ("청년","절세","노후" 등)
        driver: 외부에서 만든 드라이버를 재사용. None 이면 새로 만들고 종료.

    Returns:
        저장된 png 파일 경로 리스트 (실패 시 빈 리스트).
    """
    site = CATEGORY_TO_SITE.get(category)
    if not site:
        log.debug(f"카테고리 '{category}' 는 캡처 대상이 아님 — skip")
        return []

    cfg = SITE_CONFIGS.get(site)
    if not cfg:
        log.warning(f"SITE_CONFIGS 에 '{site}' 설정 없음 — skip")
        return []

    url = _find_url_for_site(title, site)
    if not url:
        log.warning(f"{cfg['label']} URL 매핑 실패 — skip")
        return []

    saved: list[str] = []
    own_driver = driver is None
    if own_driver:
        driver = make_screenshot_driver()

    log.info(f"{cfg['label']} 스크린샷 캡처: {url}")
    try:
        driver.get(url)
        time.sleep(3)

        _close_popups(driver, cfg.get("popup_extra", []))
        _expand_window_to_page(driver)

        # 샷 1 — 콘텐츠 요약 영역
        _scroll_into_view_first_match(driver, cfg.get("summary_selectors", []))
        shot1 = os.path.join(SCREENSHOT_DIR, f"{site}_{post_id}_1.png")
        if _save_safe(driver, shot1):
            saved.append(shot1)

        # 샷 2 — 사이트별 핵심 섹션 키워드 (XPath 텍스트 검색)
        keywords = cfg.get("section_keywords", [])
        if keywords:
            xpath = " | ".join(
                f"//*[contains(text(),'{kw}')]" for kw in keywords
            )
            try:
                els = driver.find_elements(By.XPATH, xpath)
                if els:
                    driver.execute_script(
                        "arguments[0].scrollIntoView({block: 'center'});",
                        els[0],
                    )
                    time.sleep(0.6)
                    shot2 = os.path.join(SCREENSHOT_DIR, f"{site}_{post_id}_2.png")
                    if _save_safe(driver, shot2):
                        saved.append(shot2)
            except (NoSuchElementException, WebDriverException):
                pass

        # 샷 3 — 페이지 하단 (관련 정보·문의처)
        try:
            driver.execute_script(
                "window.scrollTo(0, document.body.scrollHeight * 0.75);"
            )
            time.sleep(0.6)
            shot3 = os.path.join(SCREENSHOT_DIR, f"{site}_{post_id}_3.png")
            if _save_safe(driver, shot3):
                saved.append(shot3)
        except WebDriverException:
            pass

    except (TimeoutException, WebDriverException) as e:
        log.warning(f"{cfg['label']} 스크린샷 실패: {e}")
    finally:
        if own_driver:
            try:
                driver.quit()
            except WebDriverException:
                pass

    return saved


def site_for_category(category: str) -> str | None:
    """외부에서 카테고리 → 사이트 매핑을 조회할 때 사용 (디버깅·로그용)."""
    return CATEGORY_TO_SITE.get(category)
