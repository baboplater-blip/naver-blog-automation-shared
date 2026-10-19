# core/popup_handler.py — 팝업/배너/오버레이 자동 닫기
from __future__ import annotations

import time

try:
    from selenium.webdriver.common.by import By
    from selenium.common.exceptions import (
        NoSuchElementException, ElementNotInteractableException,
        StaleElementReferenceException, WebDriverException,
    )
except ImportError:
    # selenium 미설치 환경에서도 모듈 로드 가능하도록 폴백
    By = None
    NoSuchElementException = Exception
    ElementNotInteractableException = Exception
    StaleElementReferenceException = Exception
    WebDriverException = Exception

from config.logger import get_logger
log = get_logger(__name__)

_SELENIUM_CLICK_ERRORS = (
    NoSuchElementException, ElementNotInteractableException,
    StaleElementReferenceException,
)

_POPUP_SELECTORS = [
    # 네이버 공통 팝업 닫기 버튼
    ".btn_close",  ".pop_close",  "#closeBtn",
    "button.close", "[class*='popup'] .close",
    "[class*='modal'] .close", "[class*='layer'] .close",
    # 네이버 쿠키/알림 배너
    ".cookie_banner .close", "#bannerClose",
    "[class*='banner'] button[class*='close']",
    # 보안 알림/프로모션 레이어
    ".layer_popup .btn_close", ".ly_popup .btn_close",
    "a.btn_close", "button[aria-label='닫기']",
    # 브라우저 알림 거부
    ".notification-deny", "#deny-notification",
]


def dismiss_popups(driver, max_attempts: int = 3):
    """페이지에 떠 있는 팝업/배너/오버레이를 모두 닫는다."""
    closed = 0
    for sel in _POPUP_SELECTORS:
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, sel)
            for btn in btns:
                if btn.is_displayed():
                    try:
                        btn.click()
                        closed += 1
                        time.sleep(0.3)
                    except _SELENIUM_CLICK_ERRORS:
                        log.debug(f"팝업 닫기 스킵: {sel}")
        except _SELENIUM_CLICK_ERRORS:
            log.debug(f"팝업 셀렉터 조회 실패: {sel}")
    # JS로 흔한 오버레이 강제 제거
    driver.execute_script("""
        document.querySelectorAll(
            '[class*="popup"], [class*="modal"], [class*="layer_wrap"], [class*="dimmed"]'
        ).forEach(el => {
            if (el.offsetParent !== null) el.style.display = 'none';
        });
    """)
    if closed:
        log.info(f"  🚫 팝업 {closed}개 닫음")
    return closed


def dismiss_draft_recovery_popup(driver):
    """
    '이전에 작성하던 글이 있습니다' 드래프트 복구 팝업을 닫는다.
    "불러오기"를 클릭하여 드래프트를 로드 (영구 처리).
    "취소"는 팝업만 일시적으로 닫고 드래프트가 남아있어 반복 출현하므로 사용하지 않음.
    """
    # 0) "작성 중인 글이 있습니다" 팝업 (유형B) — "취소" 클릭으로 새 글 시작
    try:
        has_draft_popup = driver.execute_script("""
            var body = document.body.innerText || '';
            return body.indexOf('작성 중인 글') !== -1
                || body.indexOf('작성중이던') !== -1
                || body.indexOf('이어서 작성') !== -1;
        """)
        if has_draft_popup:
            btns = driver.find_elements(
                By.XPATH,
                "//button[contains(.,'취소')] | //a[contains(.,'취소')]")
            for btn in btns:
                if btn.is_displayed():
                    try:
                        btn.click()
                    except _SELENIUM_CLICK_ERRORS:
                        driver.execute_script("arguments[0].click();", btn)
                    time.sleep(2)
                    log.info("  🚫 드래프트 복구 팝업 처리 ('취소' — 새 글 작성)")
                    return True
    except WebDriverException:
        pass

    # 1) "이전에 작성하던 글" 팝업 (유형A) — "불러오기" 등
    dismiss_texts = ["불러오기", "새로 작성", "새로작성", "삭제", "아니오"]
    for text in dismiss_texts:
        try:
            btns = driver.find_elements(
                By.XPATH,
                f"//button[contains(.,'{text}')] | "
                f"//a[contains(.,'{text}')] | "
                f"//span[contains(.,'{text}')]/ancestor::button")
            for btn in btns:
                if btn.is_displayed():
                    try:
                        btn.click()
                    except _SELENIUM_CLICK_ERRORS:
                        driver.execute_script("arguments[0].click();", btn)
                    time.sleep(2)
                    log.info(f"  🚫 드래프트 복구 팝업 처리 ('{text}')")
                    return True
        except _SELENIUM_CLICK_ERRORS:
            continue

    # 2) JS로 localStorage/sessionStorage에서 드래프트 데이터 삭제
    try:
        cleared = driver.execute_script("""
            var cleared = 0;
            // localStorage에서 draft/backup 관련 키 삭제
            for (var i = localStorage.length - 1; i >= 0; i--) {
                var key = localStorage.key(i);
                if (key && (key.indexOf('draft') !== -1 || key.indexOf('backup') !== -1 ||
                    key.indexOf('autosave') !== -1 || key.indexOf('temp') !== -1)) {
                    localStorage.removeItem(key);
                    cleared++;
                }
            }
            // sessionStorage도 동일
            for (var j = sessionStorage.length - 1; j >= 0; j--) {
                var skey = sessionStorage.key(j);
                if (skey && (skey.indexOf('draft') !== -1 || skey.indexOf('backup') !== -1 ||
                    skey.indexOf('autosave') !== -1 || skey.indexOf('temp') !== -1)) {
                    sessionStorage.removeItem(skey);
                    cleared++;
                }
            }
            return cleared;
        """)
        if cleared and cleared > 0:
            log.debug(f"  드래프트 스토리지 {cleared}건 삭제")
    except WebDriverException:
        log.debug("드래프트 스토리지 삭제 실패")

    # 3) JS로 모달/팝업 강제 제거 (최후의 수단)
    removed = driver.execute_script("""
        var removed = 0;
        var sels = [
            '[class*="popup"]', '[class*="modal"]', '[class*="layer"]',
            '[class*="dialog"]', '[class*="dimmed"]'
        ];
        sels.forEach(function(s) {
            document.querySelectorAll(s).forEach(function(el) {
                if (el.offsetParent !== null && el.offsetHeight > 50) {
                    el.style.display = 'none';
                    removed++;
                }
            });
        });
        return removed;
    """)
    if removed and removed > 0:
        time.sleep(0.5)
        log.info(f"  🚫 JS로 팝업/레이어 {removed}개 강제 제거")
        return True

    return False


def dismiss_editor_popups(driver):
    """네이버 블로그 에디터의 도움말/가이드/튜토리얼 팝업을 닫는다."""
    # 먼저 드래프트 복구 팝업 처리
    dismiss_draft_recovery_popup(driver)

    editor_popup_sels = [
        # 도움말/가이드 팝업
        ".se-help-panel-close-button", ".se-guide-close",
        ".se-popup-close", ".se-tooltip-close",
        "button.se-help-close", ".se-layer-close",
        # 공통 닫기 버튼 (에디터 내)
        ".se-popup-button-close", ".se-close-button",
        "[class*='guide'] button[class*='close']",
        "[class*='help'] button[class*='close']",
        "[class*='tooltip'] button[class*='close']",
        # 확인/닫기 텍스트 버튼
        ".se-popup-button-confirm",
        # 튜토리얼 스킵
        "[class*='tutorial'] button", "[class*='onboarding'] button",
    ]
    closed = 0
    for sel in editor_popup_sels:
        try:
            for btn in driver.find_elements(By.CSS_SELECTOR, sel):
                if btn.is_displayed():
                    try:
                        btn.click()
                        closed += 1
                        time.sleep(0.3)
                    except _SELENIUM_CLICK_ERRORS:
                        log.debug(f"에디터 팝업 닫기 스킵: {sel}")
        except _SELENIUM_CLICK_ERRORS:
            log.debug(f"에디터 팝업 셀렉터 조회 실패: {sel}")
    # JS로 에디터 내 오버레이 강제 제거
    try:
        driver.execute_script("""
            document.querySelectorAll(
                '[class*="se-help"], [class*="se-guide"], [class*="se-tooltip"],
                 [class*="se-popup-dimmed"], [class*="tutorial"], [class*="onboarding"]'
            ).forEach(el => { el.style.display = 'none'; });
        """)
    except WebDriverException:
        log.debug("에디터 JS 오버레이 제거 실패")
    if closed:
        log.info(f"  🚫 에디터 팝업 {closed}개 닫음")
