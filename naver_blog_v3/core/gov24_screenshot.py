# core/gov24_screenshot.py
# 정부24에서 지원사업 페이지를 자동으로 스크린샷 캡처합니다
from __future__ import annotations

import time, os, re, sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
log = get_logger(__name__)
try:
    from config.settings import GOVERNMENT24_URL_MAP, DEFAULT_GOV24_URL, PAGE_TIMEOUT
except (ImportError, AttributeError):
    GOVERNMENT24_URL_MAP = {}
    DEFAULT_GOV24_URL    = "https://www.gov.kr/portal/main/index"
    PAGE_TIMEOUT         = 15

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.common.exceptions import NoSuchElementException, TimeoutException, WebDriverException

try:
    from config import profile as _profile
    SCREENSHOT_DIR = str(_profile.screenshots_dir())
except Exception:  # noqa: BLE001 — 프로필 라우터 실패 시 레거시 하드코딩 경로로 폴백
    SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screenshots")
os.makedirs(SCREENSHOT_DIR, exist_ok=True)


def _make_screenshot_driver() -> webdriver.Chrome:
    """스크린샷 전용 드라이버 (헤드리스, 넓은 해상도)"""
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--hide-scrollbars")
    opts.add_argument("--disable-infobars")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])

    try:
        from webdriver_manager.chrome import ChromeDriverManager
        svc = Service(ChromeDriverManager().install())
    except (ImportError, FileNotFoundError, OSError):
        svc = Service()

    return webdriver.Chrome(service=svc, options=opts)


def find_gov24_url(title: str) -> str:
    """제목 키워드로 정부24 URL 자동 매핑"""
    for keyword, url in GOVERNMENT24_URL_MAP.items():
        if keyword in title:
            return url
    return DEFAULT_GOV24_URL


def make_screenshot_driver() -> webdriver.Chrome:
    """스크린샷 전용 드라이버 생성 (외부에서 공유 가능)"""
    return _make_screenshot_driver()


def capture_gov24_page(title: str, post_id: str,
                       driver: webdriver.Chrome = None) -> list[str]:
    """
    정부24 페이지에서 스크린샷 최대 3장 캡처.
    driver를 전달하면 재사용하고, 없으면 새로 생성 후 종료합니다.
    반환: 저장된 스크린샷 파일 경로 리스트
    """
    url = find_gov24_url(title)
    saved = []
    own_driver = driver is None

    log.info(f"정부24 스크린샷 캡처 중: {url}")
    if own_driver:
        driver = _make_screenshot_driver()

    try:
        driver.get(url)
        time.sleep(3)

        # ── 팝업 닫기 ──
        for sel in [".layerPopup .btnClose", ".pop_close", "#closeBtn", ".modal-close"]:
            try:
                driver.find_element(By.CSS_SELECTOR, sel).click()
                time.sleep(0.5)
            except NoSuchElementException:
                pass

        # ── 스크롤 없이 전체 페이지 높이로 창 늘리기 ──
        total_h = driver.execute_script("return document.body.scrollHeight")
        total_h = min(total_h, 4000)  # 최대 4000px
        driver.set_window_size(1280, total_h)
        time.sleep(1)

        # ── 샷 1: 상단 요약 영역 (신청자격·지원금액 표 포함) ──
        try:
            summary_el = driver.find_element(
                By.CSS_SELECTOR,
                ".svcInfo_detail, .service-summary, .content-area, #contents, main"
            )
            driver.execute_script("arguments[0].scrollIntoView(true);", summary_el)
            time.sleep(0.8)
        except NoSuchElementException:
            pass

        shot1 = os.path.join(SCREENSHOT_DIR, f"gov24_{post_id}_1.png")
        driver.save_screenshot(shot1)
        if os.path.exists(shot1) and os.path.getsize(shot1) > 0:
            saved.append(shot1)
            log.info(f"샷1 저장: {shot1}")
        else:
            log.warning(f"스크린샷 저장 실패 또는 빈 파일: {shot1}")
            if os.path.exists(shot1):
                os.remove(shot1)

        # ── 샷 2: 신청방법 섹션 ──
        try:
            els = driver.find_elements(By.XPATH,
                "//*[contains(text(),'신청방법') or contains(text(),'신청 방법') or contains(text(),'신청절차')]"
            )
            if els:
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", els[0])
                time.sleep(0.8)
                shot2 = os.path.join(SCREENSHOT_DIR, f"gov24_{post_id}_2.png")
                driver.save_screenshot(shot2)
                if os.path.exists(shot2) and os.path.getsize(shot2) > 0:
                    saved.append(shot2)
                    log.info(f"샷2 저장: {shot2}")
                else:
                    log.warning(f"스크린샷 저장 실패 또는 빈 파일: {shot2}")
                    if os.path.exists(shot2):
                        os.remove(shot2)
        except (NoSuchElementException, TimeoutException):
            pass

        # ── 샷 3: 문의처·관련 서비스 ──
        try:
            driver.execute_script("window.scrollTo(0, document.body.scrollHeight * 0.75);")
            time.sleep(0.8)
            shot3 = os.path.join(SCREENSHOT_DIR, f"gov24_{post_id}_3.png")
            driver.save_screenshot(shot3)
            if os.path.exists(shot3) and os.path.getsize(shot3) > 0:
                saved.append(shot3)
                log.info(f"샷3 저장: {shot3}")
            else:
                log.warning(f"스크린샷 저장 실패 또는 빈 파일: {shot3}")
                if os.path.exists(shot3):
                    os.remove(shot3)
        except (NoSuchElementException, TimeoutException):
            pass

    except (TimeoutException, WebDriverException) as e:
        log.warning(f"정부24 스크린샷 실패: {e}")
    finally:
        if own_driver:
            driver.quit()

    return saved


def crop_screenshot_to_section(img_path: str, output_path: str,
                                 top_ratio: float = 0.0,
                                 bottom_ratio: float = 1.0) -> str:
    """스크린샷의 특정 비율 구간만 크롭"""
    try:
        from PIL import Image
        img = Image.open(img_path)
        w, h = img.size
        top    = int(h * top_ratio)
        bottom = int(h * bottom_ratio)
        cropped = img.crop((0, top, w, bottom))
        cropped.save(output_path)
        return output_path
    except (ImportError, FileNotFoundError, OSError) as e:
        log.warning(f"크롭 실패: {e}")
        return img_path
