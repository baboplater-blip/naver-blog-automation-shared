# core/naver_uploader.py
# Windows + 네이버 블로그 자동 업로드 (Selenium) — 예약발행 지원

import time, os, sys, threading, pyperclip
import math
from pathlib import Path
from typing import Optional
from urllib import parse as _urlparse
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
log = get_logger(__name__)
from config.settings import (
    NAVER_ID, NAVER_PW, HEADLESS,
    PAGE_TIMEOUT, ELEMENT_WAIT, BLOG_CATEGORY_NO,
    USE_RESERVATION, MAX_RETRY_COUNT, RETRY_WAIT_SECONDS,
    SKIP_IMAGE_INSERT,
)
from config import atomic_write_json, safe_load_json
# 모듈 참조(속성 접근용) — 신규 설정 키는 항상 getattr(settings, "KEY", 기본값) 로 읽는다
# (계약 §0-5: config/settings.py 는 이 작업 범위 밖이라 키가 없어도 동작해야 한다).
from config import settings as _settings_module

# ──────────────────────────────────────────────────
# 쿠키 저장 경로
# ──────────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_COOKIE_MAX_AGE_SEC = 24 * 60 * 60  # 24시간


def _cookie_path() -> Path:
    """세션 쿠키 경로 — 활성 프로필별(계정 격리). default 는 레거시
    _runtime/session_cookies.json 와 바이트 동일. 호출 시점 해석(import-time 고정 회피)."""
    from config import profile as _p
    return _p.cookie_path()

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException, TimeoutException,
    ElementNotInteractableException, ElementClickInterceptedException,
    StaleElementReferenceException, WebDriverException,
)
from selenium.webdriver.common.action_chains import ActionChains

from core.popup_handler import dismiss_popups, dismiss_editor_popups, dismiss_draft_recovery_popup

try:
    from webdriver_manager.chrome import ChromeDriverManager
    _USE_WDM = True
except ImportError:
    _USE_WDM = False


# ──────────────────────────────────────────────────
# 🔒 발행 대상 신원 게이트 (오발행 방지 — 멀티 계정/프로필)
# ──────────────────────────────────────────────────
# 페이블5 보안 결론: "발행 대상 표시"만으론 부족 — 쿠키가 틀리면 표시조차 틀림.
# 발행 직전(글 쓰기 전) 브라우저에 실제 로그인된 blogId 를 읽어 NAVER_ID 와 대조한다.
# 확실한 불일치(다른 id 판독)는 모드 무관 항상 차단, 판독 불가는 strict 에서만 차단.
# 기본 warn 이라 정상(정확한 계정) 파이프라인은 절대 안 막힌다(URL 로 항상 match).
import re as _re
from config import profile as _profile

_ID_SANITIZE = _re.compile(r"[^a-z0-9_-]")


def _normalize_blog_id(s) -> str:
    return _ID_SANITIZE.sub("", str(s or "").strip().lower())


def verify_publish_identity(intended_id, logged_in_id, current_url: str = ""):
    """발행 대상 신원 검증(순수·fail-closed). 반환: (ok, reason, verdict).

    verdict: 'match'(진행 가능) / 'mismatch'(항상 차단) / 'unknown'(strict 만 차단).
    logged_in_id 를 못 읽어도(''), current_url 이 대상의 postwrite 면 match 로 본다.
    """
    a = _normalize_blog_id(intended_id)
    li = _normalize_blog_id(logged_in_id)
    if not a:
        return False, "발행 대상 ID 비어있음", "unknown"
    if li:
        if li == a:
            return True, f"일치({a})", "match"
        return False, f"불일치: 로그인={li} ≠ 대상={a}", "mismatch"
    if f"/{a}/postwrite" in (current_url or "").lower():
        return True, f"URL 확인({a})", "match"
    return False, "로그인 계정 판독 불가", "unknown"


_READ_LOGGED_IN_ID_JS = r"""
try {
  var cands = [];
  if (window.blogId) cands.push(window.blogId);
  if (window.g_szBlogID) cands.push(window.g_szBlogID);
  var sel = ['a.gnb_my','#gnb_my_namebox a','a[href*="//blog.naver.com/"][class*="my"]',
             '.MyView-module__link_menu___'];
  for (var i=0;i<sel.length;i++){
    var e=document.querySelector(sel[i]);
    if(e){var h=e.getAttribute('href')||''; var m=h.match(/blog\.naver\.com\/([A-Za-z0-9_-]+)/); if(m) cands.push(m[1]);}
  }
  return (cands.filter(Boolean)[0]||"").toString();
} catch(e){ return ""; }
"""


_READ_AUTHENTICATED_NAVER_USER_ID_JS = r"""
try {
  var host = (window.location && window.location.hostname || "").toLowerCase();
  var path = (window.location && window.location.pathname || "").toLowerCase();
  if ((host !== "www.naver.com" && host !== "naver.com") ||
      (path !== "/" && path !== "/main" && path !== "/main/")) return "";

  var data = window["EAGER-DATA"];
  var gv = data && data.GV;
  var loggedIn = gv && gv.login === true;
  if (gv && gv.login === true) {
    if (typeof gv.email === "string") {
      var emailMatch = gv.email.trim().match(/^([A-Za-z0-9._+-]+)@naver\.com$/i);
      if (emailMatch) return emailMatch[1];
    }
  }

  var emailNode = document.querySelector('[class*="MyView-module__desc_email"]');
  var logoutNode = document.querySelector('[class*="MyView-module__btn_logout"]');
  var emailText = emailNode ? (emailNode.textContent || "").trim() : "";
  var domMatch = emailText.match(/^([A-Za-z0-9._+-]+)@naver\.com$/i);
  if (logoutNode && domMatch) return domMatch[1];
  if (loggedIn && typeof gv.userId === "string" && gv.userId.trim()) {
    return gv.userId.trim();
  }
  return "";
} catch (e) { return ""; }
"""

_AUTH_HOMEPAGE_MAX_PAGE_LOAD_TIMEOUT = 10.0


def _is_trusted_naver_homepage(current_url: str) -> bool:
    """현재 탭이 네이버 메인 홈페이지 경로인지 검증한다."""
    try:
        parsed_url = _urlparse.urlsplit(str(current_url or ""))
    except (TypeError, ValueError):
        return False
    return (
        parsed_url.scheme.lower() == "https"
        and (parsed_url.hostname or "").lower() in {"www.naver.com", "naver.com"}
        and parsed_url.path.lower() in {"", "/", "/main", "/main/"}
    )


def _read_naver_homepage_user_id(driver) -> str:
    """검증된 네이버 메인에서 로그인 사용자 ID만 읽는다."""
    try:
        current_url = driver.current_url
    except WebDriverException:
        return ""
    if not _is_trusted_naver_homepage(current_url):
        return ""
    try:
        value = driver.execute_script(_READ_AUTHENTICATED_NAVER_USER_ID_JS)
    except WebDriverException:
        return ""
    return str(value or "").strip()


def read_authenticated_naver_user_id(
        driver, *, allow_homepage_probe: bool = False, timeout: float = 5.0,
        poll_interval: float = 0.25) -> str:
    """네이버 홈페이지의 인증된 로그인 ID를 읽고, 필요할 때만 임시 탭을 쓴다.

    현재 탭은 절대 이동하지 않는다. 임시 탭은 호출자가 명시적으로 허용했을 때만
    만들며, 생성한 핸들만 닫고 항상 원래 탭으로 돌아온다.
    """
    user_id = _read_naver_homepage_user_id(driver)
    if user_id or not allow_homepage_probe:
        return user_id

    try:
        original_handle = driver.current_window_handle
        existing_handles = set(driver.window_handles)
    except WebDriverException:
        return ""

    temporary_handle = None
    original_page_load_timeout = None
    page_load_timeout_changed = False
    deadline = time.monotonic() + max(0.0, float(timeout))
    try:
        driver.switch_to.new_window("tab")
        candidate = driver.current_window_handle
        if candidate != original_handle and candidate not in existing_handles:
            temporary_handle = candidate
        if temporary_handle is None:
            return ""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return ""
        try:
            current_timeout = driver.timeouts.page_load
            timeout_value = float(current_timeout)
            if not math.isfinite(timeout_value):
                return ""
            bounded_timeout = min(
                _AUTH_HOMEPAGE_MAX_PAGE_LOAD_TIMEOUT, remaining)
            if timeout_value > 0:
                bounded_timeout = min(timeout_value, bounded_timeout)
            original_page_load_timeout = current_timeout
            page_load_timeout_changed = True
            driver.set_page_load_timeout(bounded_timeout)
        except (WebDriverException, AttributeError, TypeError, ValueError):
            log.debug("확인 탭 페이지 로드 제한 설정을 건너뜀")
            return ""
        try:
            driver.get("https://www.naver.com/")
        except TimeoutException:
            # document 일부가 이미 로드됐을 수 있으므로 현재 DOM 계정 증거는 읽는다.
            log.debug("네이버 확인 페이지 로드 제한 시간 경과")
        while True:
            user_id = _read_naver_homepage_user_id(driver)
            if user_id:
                return user_id
            if time.monotonic() >= deadline:
                return ""
            time.sleep(max(0.01, float(poll_interval)))
    except (WebDriverException, TypeError, ValueError):
        return ""
    finally:
        if page_load_timeout_changed:
            try:
                driver.set_page_load_timeout(original_page_load_timeout)
            except (WebDriverException, AttributeError, TypeError, ValueError) as cleanup_error:
                log.debug("발행 세션 페이지 로드 제한 복원 실패: %s",
                          type(cleanup_error).__name__)
        if temporary_handle is not None:
            try:
                driver.switch_to.window(temporary_handle)
                driver.close()
            except WebDriverException as cleanup_error:
                log.debug("임시 확인 탭 닫기 실패: %s",
                          type(cleanup_error).__name__)
        try:
            driver.switch_to.window(original_handle)
        except WebDriverException as cleanup_error:
            log.debug("발행 세션 원래 탭 복귀 실패: %s",
                      type(cleanup_error).__name__)


def read_logged_in_blog_id(driver) -> str:
    """현재 로그인된 네이버 blogId 를 best-effort 로 읽는다. 실패 시 ''(판독 불가)."""
    try:
        v = driver.execute_script(_READ_LOGGED_IN_ID_JS)
        return str(v or "").strip()
    except Exception:  # noqa: BLE001 — 판독 실패는 빈 문자열(차단 결정은 상위에서)
        return ""


def _identity_gate_mode() -> str:
    """게이트 모드 off/warn/strict. named 프로필(멀티계정)은 완전 off 금지(최소 warn).

    attach 발행 경로(사람이 세션 크롬에 수동 로그인)는 오계정 발행 위험이 spawn 보다
    커서(쿠키 자동 로그인이 아니라 그 창의 로그인 계정을 그대로 씀) 신원 게이트를
    strict 로 자동 상향한다 — unknown(판독 불가)도 차단, mismatch 만이 아니라.
    spawn 은 기존 PUBLISH_IDENTITY_GATE 기본값('warn') 그대로(회귀 0).
    """
    publish_mode = "spawn"
    try:
        from config import settings as _s
        mode = str(getattr(_s, "PUBLISH_IDENTITY_GATE", "warn")).lower()
        publish_mode = str(getattr(_s, "PUBLISH_MODE", "spawn")).lower()
    except Exception:  # noqa: BLE001
        mode = "warn"
    if publish_mode == "attach":
        return "strict"
    if mode not in ("off", "warn", "strict"):
        mode = "warn"
    if mode == "off" and not _profile.is_default():
        mode = "warn"
    return mode


def _assert_publish_identity(driver) -> bool:
    """발행 직전 신원 게이트. True=진행, False=중단(fail-closed).

    mismatch(확실히 다른 계정)는 모드 무관 항상 중단. unknown 은 strict 만 중단.
    게이트 내부 오류가 파이프라인을 깨지 않게 방어(warn: 진행 / strict: 중단).
    """
    mode = _identity_gate_mode()
    if mode == "off":
        return True
    try:
        read = read_logged_in_blog_id(driver)
        ok, reason, verdict = verify_publish_identity(
            NAVER_ID, read, getattr(driver, "current_url", ""))
    except Exception as e:  # noqa: BLE001
        log.warning(f"  ⚠️ 신원 게이트 오류(무시 가능): {type(e).__name__}: {str(e)[:60]}")
        return mode != "strict"
    prof = _profile.active_profile()
    if verdict == "match":
        log.info(f"  🔒 발행 대상 확인: {reason} [프로필 {prof}]")
        return True
    if verdict == "mismatch":
        log.error(f"  ⛔ 오발행 차단 — {reason} [프로필 {prof}] — 발행 중단(fail-closed)")
        return False
    if mode == "strict":
        log.error(f"  ⛔ 발행 대상 신원 판독 불가 — strict 모드 중단 [프로필 {prof}]")
        return False
    log.warning(f"  ⚠️ 발행 대상 신원 판독 불가 — warn 모드라 진행 [프로필 {prof}] "
                f"(엄격 차단: settings.PUBLISH_IDENTITY_GATE='strict')")
    return True


# ──────────────────────────────────────────────────
# 🌐 드라이버 초기화
# ──────────────────────────────────────────────────
# 프로필별 브라우저 지문(2026-07-19 격리 감사 후속 — fingerprint_seed 실배선).
# default 프로필은 기존 고정값 그대로(동작 불변), named 프로필만 시드 기반으로
# UA 버전·창 크기를 결정론적으로 분산해 "전 프로필 동일 지문" 링킹 신호를 끊는다.
_UA_TMPL = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{v}.0.0.0 Safari/537.36")


def _fingerprint_profile() -> tuple:
    """(user_agent, window_size) — named 프로필은 fingerprint_seed 로 결정론 분산."""
    ua, win = _UA_TMPL.format(v=145), "1400,920"          # 기존 고정값(default 불변)
    try:
        if _profile.profile_root() is None:
            return ua, win
        import hashlib
        seed = _profile.fingerprint_seed()
        h = hashlib.sha256(str(seed).encode("utf-8")).digest()
        versions = (144, 145, 146)                         # 그럴듯한 최신 크롬 버전대만
        sizes = ("1366,768", "1440,900", "1536,864", "1600,900", "1920,1080")
        ua = _UA_TMPL.format(v=versions[h[0] % len(versions)])
        win = sizes[h[1] % len(sizes)]
        log.info("  🫆 프로필 지문 적용(UA 버전·창 크기 분산 — 시드 비노출)")
    except Exception:  # noqa: BLE001 — 지문 실패는 기본값으로
        pass
    return ua, win


def _apply_profile_isolation(opts, *, user_data_dir=None) -> None:
    """named 프로필이면 Chrome user-data-dir(세션 물리 격리)+전용 프록시(1계정=1IP)를 적용.

    default 프로필은 아무 것도 안 함(기존 동작 무변화). 다계정 링킹(같은 지문·IP) 방지.
    격리 설정 실패가 드라이버 기동을 막지 않게 방어.
    user_data_dir: 명시 시 그 폴더 사용(카페 등 동시 기동 드라이버가 발행 드라이버의
    user-data-dir 를 잡아 Chrome 이중 기동 거부가 나는 것 방지). 미지정=프로필 기본.
    """
    try:
        udd = user_data_dir if user_data_dir is not None else _profile.chrome_profile_dir()
        if udd is not None:
            udd.mkdir(parents=True, exist_ok=True)
            opts.add_argument(f"--user-data-dir={udd}")
            log.info(f"  🧩 프로필 Chrome 세션 격리: …/{udd.parent.name}/{udd.name}")
        proxy = _profile.proxy_url()
        if proxy:
            # --proxy-server 는 자격증명 미지원 → scheme 유지한 host:port 만 넘긴다.
            hostport = proxy.split("@")[-1]
            opts.add_argument(f"--proxy-server={hostport}")
            log.info("  🌐 프로필 전용 프록시 적용(주소 비노출)")
            if "@" in proxy:
                log.warning("  ⚠️ 인증 프록시(user:pass)는 --proxy-server 미지원 — "
                            "인증은 selenium-wire 필요(후속). 현재 host:port 만 적용")
    except Exception as e:  # noqa: BLE001
        log.warning(f"  프로필 격리 설정 실패(무시): {type(e).__name__}: {str(e)[:60]}")


def init_driver() -> webdriver.Chrome:
    opts = Options()
    if HEADLESS:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    # 영구 프로필(--user-data-dir) 사용 시 크롬이 비정상 종료되면 다음 기동에서
    # "세션을 복원하시겠습니까?" 버블이 떠 자동화 포커스를 가로챌 수 있다.
    # 두 플래그로 억제(경쟁사 blogauto-naver 차용, 무해·되돌리기 쉬움).
    opts.add_argument("--disable-session-crashed-bubble")
    opts.add_argument("--hide-crash-restore-bubble")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_experimental_option("prefs", {
        "credentials_enable_service": False,
        "profile.password_manager_enabled": False,
        "profile.password_manager_leak_detection": False,
    })
    _ua, _win = _fingerprint_profile()   # named 프로필=시드 분산, default=기존 고정값
    opts.add_argument(f"--window-size={_win}")
    opts.add_argument(f"user-agent={_ua}")
    # 프로필 격리(named 전용): Chrome 세션 물리 분리 + 전용 프록시. default 무변화.
    _apply_profile_isolation(opts)
    svc = Service(ChromeDriverManager().install()) if _USE_WDM else Service()
    driver = webdriver.Chrome(service=svc, options=opts)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    })
    return driver


# ──────────────────────────────────────────────────
# 🖥 CDP attach — 이미 로그인된 발행 세션 크롬에 얹기 (P0-① Phase 1)
# ──────────────────────────────────────────────────
# init_driver 는 매번 새 크롬을 spawn 하고 naver_login 을 태운다(무인 발행).
# attach 경로는 사용자가 직접 로그인해 둔 세션 크롬(원격 디버깅 포트)에 붙어
# naver_login 을 건너뛴다 — 로그인 개편·쿠키 24h·2FA·캡차가 비적용된다.
# 발행 DOM 로직(_do_upload/_paste_html/_insert_images/…)은 100% 그대로 재사용하며,
# 오계정 방어(_assert_publish_identity)도 _do_upload 내부에서 그대로 동작한다.
# ★ init_driver 는 무수정 — attach 는 형제 함수로만 추가(기본 spawn 경로 회귀 0).
def _build_attach_options(port: int) -> Options:
    """CDP attach 용 Chrome Options — 실행 중 크롬의 debuggerAddress 만 설정한다.

    HEADLESS·user-data-dir·프록시·지문은 붙이지 않는다(이미 뜬 창에 얹는 것이므로
    Chrome 기동 옵션은 세션 크롬 쪽이 이미 확정한 상태). debuggerAddress 만 있으면
    Selenium 이 새 크롬을 띄우지 않고 그 세션에 attach 한다.
    """
    opts = Options()
    opts.add_experimental_option("debuggerAddress", f"127.0.0.1:{int(port)}")
    return opts


def attach_to_session(port: int = 9222, *, quiet: bool = False) -> Optional["webdriver.Chrome"]:
    """이미 로그인된 발행 세션 크롬(원격 디버깅 포트)에 Selenium 으로 붙는다.

    naver_login 을 호출하지 않는다(세션이 이미 살아있음). 신원 게이트는 발행 직전
    `_do_upload` 내부의 `_assert_publish_identity` 가 그대로 수행한다.

    Args:
        quiet: True 면 **성공 안내 로그만** 억제한다(실패·경고는 그대로).
            GUI 홈 탭의 유휴 세션 프로브처럼 수 초 간격으로 반복 호출되는
            읽기 전용 경로에서 콘솔이 같은 줄로 도배되는 것을 막는 용도.
            기본 False — 발행 등 기존 호출의 로그는 한 줄도 바뀌지 않는다.

    Returns:
        webdriver.Chrome — attach 성공.
        None — attach 실패(세션 미기동·포트 불응답 등). 발행은 fail-closed 로 중단.
    """
    try:
        opts = _build_attach_options(port)
        svc = Service(ChromeDriverManager().install()) if _USE_WDM else Service()
        driver = webdriver.Chrome(service=svc, options=opts)
    except WebDriverException as e:
        log.error(f"  ❌ 발행 세션 attach 실패(127.0.0.1:{port}) — 세션 크롬이 열려 있고 "
                  f"네이버에 로그인돼 있는지 확인하세요: {type(e).__name__}: {str(e)[:80]}")
        return None
    except Exception as e:  # noqa: BLE001 — 드라이버 생성 계열 예외 전부 fail-closed
        log.error(f"  ❌ 발행 세션 attach 오류(127.0.0.1:{port}): "
                  f"{type(e).__name__}: {str(e)[:80]}", exc_info=True)
        return None

    # 기존 CDP 스니펫 재사용 — attach 후에도 webdriver 흔적 1회 제거(spawn 과 동일 방어).
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
        })
    except WebDriverException as e:
        log.debug(f"  attach 후 webdriver 스니펫 스킵(무해): {type(e).__name__}")
    if not quiet:
        log.info(f"  🖥 발행 세션 attach 성공(127.0.0.1:{port}) — 기존 로그인 세션 재사용")
    return driver


# ──────────────────────────────────────────────────
# 🍪 쿠키 저장 / 복원 헬퍼
# ──────────────────────────────────────────────────
def _save_cookies(driver: "webdriver.Chrome") -> None:
    """로그인 성공 후 Selenium 쿠키를 JSON 파일로 저장."""
    try:
        cookies = driver.get_cookies()
        atomic_write_json(_cookie_path(), cookies)
        log.info(f"  🍪 쿠키 저장 완료 ({len(cookies)}개) → {_cookie_path().name}")
    except Exception as e:
        log.warning(f"  쿠키 저장 실패: {e}")


def _load_cookies(driver: "webdriver.Chrome") -> bool:
    """저장된 쿠키를 복원하고 로그인 유효성을 확인한다.

    Returns:
        True — 쿠키 복원으로 로그인 세션이 유효함
        False — 복원 실패 또는 세션 만료
    """
    if not _cookie_path().exists():
        return False

    # 24시간 만료 체크
    try:
        age = time.time() - _cookie_path().stat().st_mtime
        if age > _COOKIE_MAX_AGE_SEC:
            log.info("  🍪 쿠키 파일 만료 (24시간 초과) — 삭제 후 새로 로그인")
            _cookie_path().unlink(missing_ok=True)
            return False
    except OSError as e:
        log.warning(f"  쿠키 파일 상태 확인 실패: {e}")
        return False

    cookies = safe_load_json(_cookie_path())
    if not cookies or not isinstance(cookies, list):
        log.warning("  🍪 쿠키 파일 로드 실패 또는 형식 오류")
        return False

    try:
        # 쿠키를 추가하기 위해 먼저 naver.com 도메인에 접속
        driver.get("https://www.naver.com")
        time.sleep(1)

        for cookie in cookies:
            # Selenium이 받아들이지 못하는 키 제거
            cookie.pop("sameSite", None)
            cookie.pop("storeId", None)
            try:
                driver.add_cookie(cookie)
            except Exception:
                pass  # 개별 쿠키 실패는 무시

        # 로그인 상태 확인: blog.naver.com 접속
        driver.get("https://blog.naver.com")
        time.sleep(2)

        # 로그인 상태 판별: 프로필/사용자 정보 요소 존재 여부
        logged_in = driver.execute_script("""
            // 방법 1: 블로그 홈의 프로필 영역
            if (document.querySelector('.blog_my_area')
                || document.querySelector('.area_my_info')
                || document.querySelector('#nickname')
                || document.querySelector('.nick')) return true;
            // 방법 2: URL이 로그인 리다이렉트가 아닌지
            if (window.location.href.indexOf('nidlogin') > -1) return false;
            // 방법 3: 로그인 버튼이 없으면 이미 로그인 상태
            var loginBtn = document.querySelector('.btn_login, .link_login, a[href*="nidlogin"]');
            return !loginBtn;
        """)

        if logged_in:
            log.info("  🍪 쿠키 로그인 성공 — ID/PW 로그인 건너뜀")
            return True
        else:
            log.warning("  🍪 쿠키 세션 만료 — ID/PW 로그인으로 전환")
            return False

    except Exception as e:
        log.warning(f"  🍪 쿠키 복원 실패: {e}")
        return False


def _delete_cookies() -> None:
    """쿠키 파일을 삭제한다."""
    try:
        if _cookie_path().exists():
            _cookie_path().unlink(missing_ok=True)
            log.info("  🍪 쿠키 파일 삭제 완료")
    except OSError as e:
        log.warning(f"  쿠키 파일 삭제 실패: {e}")


# ──────────────────────────────────────────────────
# 🔐 네이버 로그인
# ──────────────────────────────────────────────────
def _no_password_login_enabled() -> bool:
    """무비번(쿠키) 로그인 모드가 켜져 있는지. (관문 ③ 자격증명 투명성 · 2026-07-15)

    켜지면 비밀번호를 저장/입력하지 않고 사용자가 브라우저에서 직접 로그인한다.
    설정을 지연 로드해 settings.py 재생성에도 견고하게(없으면 False=현행 유지).
    """
    try:
        from config.settings import NAVER_NO_PASSWORD_LOGIN
        return bool(NAVER_NO_PASSWORD_LOGIN)
    except (ImportError, AttributeError):
        return False


def _manual_login(driver, auth_event=None, auth_cb=None) -> bool:
    """무비번 모드 로그인 — 사용자가 브라우저에서 직접 로그인(비밀번호 저장/입력 0).

    쿠키가 이미 유효하면 이 함수는 호출되지 않는다(naver_login 이 먼저 _load_cookies).
    여기 도달했다는 건 쿠키가 없거나 만료됐다는 뜻:
      · GUI(attended, auth_event 있음): 로그인 페이지를 열고 사용자가 직접 로그인 →
        '인증완료' 신호(auth_event) 대기 → 로그인 확인 후 쿠키 저장.
      · 무인(auth_event 없음): 사람이 로그인해줄 수 없으므로 fail-closed(False) —
        스케줄러가 사용자에게 '수동 로그인 필요'를 알린다(무비번 모드의 trade-off).

    Returns:
        로그인 성공 시 True(+쿠키 저장), 실패/타임아웃/무인이면 False.
    """
    driver.get("https://nid.naver.com/nidlogin.login?mode=form")

    if auth_event is None:
        # 무인 실행 — 무비번 모드에선 쿠키 만료 시 자동 로그인이 불가하다.
        log.error(
            "  ❌ 무비번 모드 + 쿠키 만료 + 무인 실행 → 로그인 불가. "
            "GUI에서 '네이버 로그인'으로 세션을 갱신하세요.")
        return False

    log.warning("  🔐 무비번 로그인 — 브라우저에서 직접 로그인 후 '인증완료'를 누르세요")
    if auth_cb:
        auth_cb("waiting")
    completed = auth_event.wait(timeout=300)
    if auth_cb:
        auth_cb("done")
    if not completed:
        log.error("  ❌ 무비번 로그인 대기 타임아웃 (5분 초과)")
        return False

    # 로그인 페이지를 벗어났는지로 성공 판정
    try:
        WebDriverWait(driver, 8).until(
            lambda d: "nidlogin.login" not in d.current_url)
    except TimeoutException:
        pass
    if "nidlogin.login" not in driver.current_url:
        log.info("  ✅ 무비번 로그인 성공 — 비밀번호 저장 없이 쿠키만 보관")
        _save_cookies(driver)
        return True
    log.error("  ❌ 무비번 로그인 실패 (로그인 페이지 잔류)")
    _delete_cookies()
    return False


# 네이버 로그인 제출 버튼 후보 — 2026-07 페이지 개편으로 'log.login' 이
# 'loginBtn_column'(세로형)/'loginBtn_row'(가로형) 으로 바뀌었다.
# 구 ID 도 후보에 남겨 재개편·구버전 페이지에 대비한다.
_LOGIN_BTN_IDS = ("loginBtn_column", "loginBtn_row", "log.login")


def _click_login_button(driver) -> bool:
    """로그인 제출 버튼을 후보 ID 순서로 찾아 클릭한다.

    레이아웃 변형(column/row)에 따라 한쪽만 화면에 보이므로
    clickable(=표시된) 버튼을 우선 클릭하고, 전부 실패하면 JS 로
    존재하는 버튼을 직접 클릭한다. 어떤 경우에도 예외를 밖으로 던지지
    않는다 — 버튼을 못 찾으면 False(로그인 실패 처리용).
    """
    for btn_id in _LOGIN_BTN_IDS:
        try:
            WebDriverWait(driver, 3).until(
                EC.element_to_be_clickable((By.ID, btn_id))).click()
            return True
        except (TimeoutException, WebDriverException):
            continue
    try:
        return bool(driver.execute_script(
            "var ids = arguments[0];"
            "for (var i = 0; i < ids.length; i++) {"
            "  var el = document.getElementById(ids[i]);"
            "  if (el && el.offsetParent !== null) { el.click(); return true; }"
            "}"
            "for (var i = 0; i < ids.length; i++) {"
            "  var el = document.getElementById(ids[i]);"
            "  if (el) { el.click(); return true; }"
            "}"
            "return false;", list(_LOGIN_BTN_IDS)))
    except WebDriverException:
        return False


def naver_login(driver: webdriver.Chrome,
                auth_event=None, auth_cb=None) -> bool:
    """네이버 로그인.

    Args:
        driver: Chrome WebDriver 인스턴스
        auth_event: threading.Event — GUI에서 인증 완료 신호를 받기 위한 이벤트.
                    None이면 기존 input() 방식으로 대기 (CLI 호환).
        auth_cb: callable — 보안 인증 감지 시 GUI에 알림을 보내는 콜백.
                 호출 시그니처: auth_cb("waiting") / auth_cb("done")
    """
    log.info("  🔐 네이버 로그인 시도...")

    # ── 쿠키 복원 시도 (ID/PW 로그인 전) ──
    if _load_cookies(driver):
        return True

    # ── 무비번 모드: 비밀번호 없이 사용자가 직접 로그인(관문 ③) ──
    if _no_password_login_enabled():
        return _manual_login(driver, auth_event, auth_cb)

    log.info("  🔑 ID/PW 로그인 진행...")

    driver.get("https://nid.naver.com/nidlogin.login?mode=form")
    # 로그인 폼 로드 대기 (sleep 대신 요소 대기)
    try:
        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.ID, "id")))
    except TimeoutException:
        log.warning("  로그인 폼 로드 지연")
    driver.execute_script("document.querySelector('#id').value = arguments[0]", NAVER_ID)
    driver.execute_script("document.querySelector('#pw').value = arguments[0]", NAVER_PW)
    time.sleep(0.3)
    if not _click_login_button(driver):
        log.error("  ❌ 로그인 버튼을 찾지 못함 — 네이버 로그인 페이지가 또 바뀌었을 수 있습니다")
        _delete_cookies()
        return False
    # URL 변경 대기 (로그인 처리 완료까지)
    try:
        WebDriverWait(driver, 8).until(lambda d: "nidlogin.login" not in d.current_url)
    except TimeoutException:
        time.sleep(2)

    cur = driver.current_url
    # 로그인 페이지를 벗어났고 naver.com 메인이면 → 인증 없이 바로 성공
    already_ok = ("nidlogin.login" not in cur
                  and ("www.naver.com" in cur or "mail.naver.com" in cur
                       or "blog.naver.com" in cur))

    if not already_ok:
        # 보안 인증 필요 (영수증 문자, 캡차, 2차 인증 등)
        if auth_event is not None:
            # GUI 모드: 콜백으로 인증 대기 UI 표시 → Event 대기
            log.warning("  ⚠️  보안 인증 필요 — 브라우저에서 인증 후 '인증완료' 버튼을 클릭하세요")
            if auth_cb:
                auth_cb("waiting")
            completed = auth_event.wait(timeout=300)
            if auth_cb:
                auth_cb("done")
            if not completed:
                log.error("  ❌ 보안 인증 타임아웃 (5분 초과)")
                return False
        else:
            # CLI 모드: 기존 input() 대기
            log.warning("  ⚠️  보안 인증 필요 — 브라우저에서 직접 처리 후 Enter 누르세요")
            input("  [Enter 계속] ")
        cur = driver.current_url

    # 로그인 성공 판정: 로그인 페이지에서 벗어났으면 성공
    if "nidlogin.login" not in cur:
        log.info("  ✅ 로그인 성공!")
        _save_cookies(driver)  # 쿠키 저장 (다음 실행 시 재사용)
        return True
    log.error("  ❌ 로그인 실패")
    _delete_cookies()  # 실패 시 잔여 쿠키 제거
    return False


def refresh_session_cookies(auth_event=None, auth_cb=None) -> dict:
    """발행 없이 네이버 로그인만 수행해 세션쿠키를 갱신(재저장)한다.

    GUI '네이버 로그인(쿠키 갱신)' 버튼용 — 브라우저 창을 열어 로그인(필요 시 보안인증은
    auth_cb/auth_event 로 사용자 확인)하고, 성공하면 naver_login 이 _save_cookies 로
    session_cookies.json 을 갱신한다. 절대 발행하지 않는다(측정용 세션 확보 전용).

    반환: {"ok": bool, "count": int, "error": str}
    """
    driver = None
    try:
        driver = init_driver()
        ok = naver_login(driver, auth_event=auth_event, auth_cb=auth_cb)
        count = 0
        try:
            if ok and _cookie_path().exists():
                saved = safe_load_json(_cookie_path())
                count = len(saved) if isinstance(saved, list) else 0
        except OSError:
            pass
        return {"ok": bool(ok), "count": count,
                "error": "" if ok else "login_failed"}
    except Exception as e:
        log.error(f"세션쿠키 갱신 실패: {e}", exc_info=True)
        return {"ok": False, "count": 0, "error": str(e)[:120]}
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass


# ──────────────────────────────────────────────────
# 🔧 SE 에디터 인스턴스 확보 (공용)
# ──────────────────────────────────────────────────
def _ensure_se_editor(driver):
    """window.__seEditor가 없으면 SE.launcher에서 획득하여 설정한다."""
    return driver.execute_script("""
        if (window.__seEditor && window.__seEditor._documentService) return 'CACHED';
        var se = window.SE || window.se;
        if (!se || !se.launcher) return 'NO_LAUNCHER';
        var editor = null;
        // 방법 1: _editors 맵에서 직접 획득
        if (se.launcher._editors) {
            var keys = Object.keys(se.launcher._editors);
            for (var k of keys) {
                var e = se.launcher._editors[k];
                if (e && e._documentService) { editor = e; break; }
            }
            if (!editor && keys.length) editor = se.launcher._editors[keys[0]];
        }
        // 방법 2: getEditor(id)
        if (!editor) {
            try { editor = se.launcher.getEditor('blogpc001'); } catch(e) {}
        }
        // 방법 3: getEditor() 인자 없음
        if (!editor) {
            try { editor = se.launcher.getEditor(); } catch(e) {}
        }
        window.__seEditor = editor;
        return editor ? 'OK' : 'FAIL';
    """)


# ──────────────────────────────────────────────────
# 🔧 셀렉터 순회 클릭 헬퍼
# ──────────────────────────────────────────────────
def _try_click_selectors(driver, selectors, by=By.CSS_SELECTOR, timeout=3,
                         post_delay=1.0, scroll=True):
    """셀렉터 리스트를 순회하며 클릭을 시도합니다.

    Returns:
        True if any selector was clicked successfully, False otherwise.
    """
    for sel in selectors:
        try:
            el = WebDriverWait(driver, timeout).until(
                EC.element_to_be_clickable((by, sel)))
            if scroll:
                driver.execute_script(
                    "arguments[0].scrollIntoView({block:'center'});", el)
                time.sleep(0.3)
            try:
                el.click()
            except (ElementNotInteractableException, ElementClickInterceptedException):
                driver.execute_script("arguments[0].click();", el)
            time.sleep(post_delay)
            return True
        except (NoSuchElementException, TimeoutException,
                ElementNotInteractableException, ElementClickInterceptedException):
            continue
    return False


# ──────────────────────────────────────────────────
# 🚀 발행 설정 패널 열기 (즉시/예약 공통)
# ──────────────────────────────────────────────────
def _click_main_publish_button(driver) -> bool:
    """상단 "발행"(= 발행 설정 패널을 여는) 버튼만 클릭한다.

    즉시발행·예약발행 공통 진입점. 낡은 셀렉터와 `contains(.,'발행')` 폴백은 텍스트에
    "발행"을 포함한 "예약 발행 글"·"공개발행" 같은 버튼을 오클릭해 엉뚱한 모달을 열던
    오발행 경로였다(2026-07-09 버그①). 여기서는 오직 정확일치만 사용한다.

    셀렉터 우선순위:
      1) CSS ``button[class*='publish_btn']`` — 해시 클래스(publish_btn__<hash>)도 매칭.
      2) 레거시 CSS 폴백(구 에디터 호환).
      3) JS 폴백: 화면에 보이고 textContent 가 **정확히** '발행'인 button.
      4) XPATH 폴백: ``//button[normalize-space(.)='발행']`` (정확일치 — contains 금지).

    Returns:
        어느 한 방법으로든 클릭에 성공하면 True, 아니면 False.
        (실제로 패널이 열렸는지는 호출부가 ``_publish_panel_is_open`` 으로 검증한다.)
    """
    # 1)·2) CSS 셀렉터 순회 — 현행 해시 클래스 우선, 레거시는 폴백.
    if _try_click_selectors(driver, [
        "button[class*='publish_btn']",
        ".publish_btn", ".se-publish-btn",
        "[data-action='publish']",
        "button[aria-label='발행']",
    ], post_delay=2.0, scroll=False):
        return True

    # 3) JS 폴백: 텍스트가 정확히 "발행"인 보이는 버튼 (예약 발행 글/공개발행 제외).
    try:
        btn = driver.execute_script("""
            var btns = document.querySelectorAll('button');
            for (var b of btns) {
                if ((b.textContent||'').trim() === '발행' && b.offsetParent && b.offsetWidth > 0)
                    return b;
            }
            return null;
        """)
        if btn:
            btn.click()
            time.sleep(2)
            log.info("    ✅ 발행 버튼 클릭 (JS 정확일치)")
            return True
    except WebDriverException:
        pass

    # 4) XPATH 폴백: 정확일치만 — contains(.,'발행') 는 '예약 발행 글' 오클릭 유발이라 금지.
    try:
        btn = driver.find_element(
            By.XPATH, "//button[normalize-space(.)='발행']")
        try:
            btn.click()
        except (ElementNotInteractableException,
                ElementClickInterceptedException):
            driver.execute_script("arguments[0].click();", btn)
        time.sleep(2)
        log.info("    ✅ 발행 버튼 클릭 (XPATH 정확일치)")
        return True
    except (NoSuchElementException, ElementNotInteractableException,
            ElementClickInterceptedException):
        pass

    return False


def _js_click_first(driver, selectors) -> bool:
    """셀렉터 목록에서 처음 발견된 요소를 **JS 로** 클릭한다(폴백 전용).

    발행 패널의 커스텀 라디오처럼 Selenium 의 clickable 판정은 통과하지 못하지만
    JS 클릭에는 정상 반응하는 요소를 위한 좁은 보조 경로다(2026-09-18 라이브 실측).

    Returns:
        클릭한 요소가 있으면 True, 하나도 없으면 False.
    """
    try:
        return bool(driver.execute_script("""
            var sels = arguments[0];
            for (var i = 0; i < sels.length; i++) {
                var e = document.querySelector(sels[i]);
                if (e) { e.click(); return true; }
            }
            return false;
        """, list(selectors)))
    except WebDriverException as e:
        log.debug(f"JS 클릭 폴백 실패(무해): {type(e).__name__}")
        return False


_POINTER_CLICK_JS = """
    var b = arguments[0];
    if (!b) return false;
    var r = b.getBoundingClientRect();
    var o = {bubbles: true, cancelable: true, view: window,
             clientX: r.left + r.width / 2, clientY: r.top + r.height / 2};
    var seq = ['pointerover', 'pointerenter', 'pointermove', 'pointerdown',
               'mousedown', 'pointerup', 'mouseup', 'click'];
    for (var i = 0; i < seq.length; i++) {
        var t = seq[i];
        var E = (t.indexOf('pointer') === 0 && window.PointerEvent)
                ? PointerEvent : MouseEvent;
        b.dispatchEvent(new E(t, o));
    }
    return true;
"""


def _pointer_click_publish_button(driver) -> bool:
    """상단 "발행" 버튼을 **포인터 이벤트 시퀀스**로 누른다(폴백 전용).

    2026-09-18 라이브 실측 — SE ONE 에디터의 발행 버튼은 Selenium 의
    `element.click()` / ActionChains 클릭에 반응하지 않는 경우가 있다(패널이 열리지
    않음). `pointerdown/pointerup` 까지 직접 보내면 열린다(카테고리 토글에서
    2026-07-19 에 확인한 것과 같은 사고 클래스).

    Returns:
        이벤트를 보냈으면 True(패널이 열렸다는 보장은 아님 — 호출부가 검증한다).
    """
    try:
        btn = driver.find_element(By.CSS_SELECTOR, "button[class*='publish_btn']")
    except (NoSuchElementException, WebDriverException):
        return False
    try:
        return bool(driver.execute_script(_POINTER_CLICK_JS, btn))
    except WebDriverException as e:
        log.debug(f"발행 버튼 포인터 클릭 실패(무해): {type(e).__name__}")
        return False


def _close_editor_flayers(driver) -> bool:
    """에디터의 '글감'(se-flayer-unified) 등 펼쳐진 레이어를 닫는다(fail-open).

    2026-09-18 라이브 실측 — 이 레이어가 펼쳐져 있으면 상단 "발행" 버튼 클릭이
    삼켜져 발행 설정 패널이 열리지 않는다. 닫기 버튼을 누르고, 없으면 ESC 를 보낸다.
    발행 패널 자체는 건드리지 않는다(열려 있으면 그대로 둔다).

    Returns:
        닫기 시도를 실제로 한 경우 True(성공 보장 아님), 닫을 레이어가 없으면 False.
    """
    try:
        return bool(driver.execute_script("""
            var acted = false;
            var layers = document.querySelectorAll(
                '[class*="se-flayer-unified-toolbar--expanded"], '
                + '[class*="se-flayer-unified-toolbar-wrapper"]');
            for (var i = 0; i < layers.length; i++) {
                var el = layers[i];
                if (!el.offsetParent) continue;
                var btn = el.querySelector(
                    'button[class*="fold"], button[class*="close"], '
                    + 'button[class*="collapse"], button[class*="shrink"]');
                if (btn) { btn.click(); acted = true; }
            }
            return acted;
        """))
    except WebDriverException as e:
        log.debug(f"에디터 레이어 닫기 스킵(무해): {type(e).__name__}")
        return False


def _publish_panel_is_open(driver, *, attempts: int = 3, delay: float = 0.7) -> bool:
    """발행 설정 패널이 실제로 열렸는지 고유 요소로 검증한다.

    "발행" 버튼을 눌러도 잘못하면 "예약 발행 글" 목록 모달이 열릴 수 있으므로, 발행 설정
    패널에만 존재하는 요소(발행 확정 버튼·발행시간 라디오·카테고리 dropdown)의 존재로
    구분한다. 목록 모달에는 이 요소들이 없다.

    Args:
        attempts: 렌더링 지연 대비 재시도 횟수.
        delay: 재시도 간 대기 초(마지막 시도 후에는 대기하지 않음).

    Returns:
        패널 고유 요소가 하나라도 발견되면 True, 아니면 False.
    """
    # ★ 2026-09-18 라이브 실측: 에디터의 '글감'(se-flayer-unified) 레이어에도
    #   `se-flayer-unified-category-dropdown` 이 있어 아래 일반 카테고리 셀렉터가
    #   **발행 패널이 열리지 않았는데도** 매칭됐다(가짜 성공 → 카테고리·태그·예약시각을
    #   허공에 시도하고 3회 재시도를 태움). 에디터 레이어(se-*) 안의 요소는 제외한다.
    probe = """
        var sels = [
            "[data-testid='seOnePublishBtn']",
            "[data-testid='preTimeRadioBtn']",
            "[data-testid='nowTimeRadioBtn']",
            "[data-testid^='categoryItemText_']",
            "[class*='layer_content_set_publish']",
            "button[class*='confirm_btn']",
            "input[name='radio_time']",
            "#radio_time1", "#radio_time2",
            "input[value='pre']", "input[value='public']",
            "[data-testid='categoryDropdown']",
            "[data-testid='categorySelector']",
            "[class*='category'][class*='selectbox']",
            "[class*='category'][class*='dropdown']"
        ];
        for (var i = 0; i < sels.length; i++) {
            var nodes = document.querySelectorAll(sels[i]);
            for (var j = 0; j < nodes.length; j++) {
                // 에디터 본문 툴바/글감 레이어(se-*) 안의 요소는 발행 패널이 아니다.
                if (nodes[j].closest('[class*="se-flayer"], [class*="se-toolbar"]')) continue;
                return true;
            }
        }
        return false;
    """
    for attempt in range(max(1, attempts)):
        try:
            if bool(driver.execute_script(probe)):
                return True
        except WebDriverException:
            return False
        if attempt < attempts - 1:
            time.sleep(delay)
    return False


def _open_publish_panel(driver) -> bool:
    """발행 설정 패널을 열고 실제로 열렸는지까지 검증한다(즉시/예약 공통).

    절차: overlay/popup 제거 → 상단 "발행" 버튼 정확일치 클릭 → 패널 고유 요소 검증.
    버튼 클릭에 실패했거나 클릭했어도 패널이 확인되지 않으면 False 를 반환한다
    (fail-closed — 엉뚱한 모달을 열어둔 채 카테고리/태그/확인을 헛클릭하지 않는다).
    """
    # 발행 버튼 클릭 전 overlay/popup 제거
    dismiss_popups(driver)
    _close_editor_flayers(driver)
    time.sleep(0.5)

    if not _click_main_publish_button(driver):
        log.warning("    ⚠️  발행 버튼을 찾지/누르지 못함 — 발행 설정 패널 열기 실패")
        return False

    if _publish_panel_is_open(driver):
        log.info("    ✅ 발행 설정 패널 열기 완료")
        return True

    # ★ 2026-09-18 라이브 실측: 일반 클릭이 '성공'으로 보고돼도 SE ONE 에디터의
    #   발행 버튼이 반응하지 않아 패널이 열리지 않는다. 포인터 이벤트 시퀀스로
    #   한 번 더 누른다(열려 있던 '글감' 레이어도 먼저 접는다).
    log.info("    발행 패널 미확인 — 레이어를 접고 포인터 이벤트로 1회 재시도")
    _close_editor_flayers(driver)
    time.sleep(0.6)
    if _pointer_click_publish_button(driver) and _publish_panel_is_open(driver):
        log.info("    ✅ 발행 설정 패널 열기 완료(포인터 재시도)")
        return True

    log.warning(
        "    ⚠️  발행 버튼을 눌렀으나 발행 설정 패널이 확인되지 않음 "
        "(예약 발행 글 목록 등 다른 모달일 수 있음) — 발행 중단"
    )
    return False


# ──────────────────────────────────────────────────
# 📅 예약발행 — 하위 함수들
# ──────────────────────────────────────────────────
def _open_reservation_panel(driver) -> bool:
    """발행 설정 패널을 열고 성공 여부를 반환합니다(즉시발행과 동일 헬퍼 사용)."""
    return _open_publish_panel(driver)


def _set_date_time(driver, dt) -> bool:
    """예약발행 라디오 선택 + 날짜/시간 입력을 수행합니다.

    Returns:
        날짜 검증(date_ok)과 시간 검증(time_ok)이 모두 성공하면 True, 아니면 False.
        fail-closed 정책: 하나라도 실패하면 호출부에서 발행 확정을 중단해야 한다.
    """
    # ── 예약발행 라디오 버튼 클릭 ──
    # 2026 네이버 에디터: id="radio_time2", name="radio_time", value="pre"
    reserve_selected = _try_click_selectors(driver, [
        "#radio_time2",
        "input[value='pre']",
        "label[for='radio_time2']",
        "input[value='reservation']",
        "input[id*='reserve']",
        "label[for*='reserve']",
    ], post_delay=1.0, scroll=False)

    # ★ 2026-09-18 라이브 실측 — 발행 패널의 라디오는 커스텀 스타일이라 Selenium 의
    #   `element_to_be_clickable` 이 통과하지 못해 위 경로가 통째로 실패하고, 그러면
    #   아래 XPath 텍스트 폴백이 '예약 발행 4건' 같은 엉뚱한 요소를 눌러 날짜 input 이
    #   영영 렌더되지 않았다. 텍스트 폴백보다 **먼저** JS 클릭을 시도한다.
    if not reserve_selected:
        reserve_selected = _js_click_first(driver, [
            "#radio_time2",
            "[data-testid='preTimeRadioBtn']",
            "input[value='pre']",
            "label[for='radio_time2']",
        ])
        if reserve_selected:
            time.sleep(1.0)

    if reserve_selected:
        log.info("    ✅ 예약발행 라디오 선택")
    else:
        # XPath 폴백: 텍스트로 예약 옵션 찾기
        for text in ["예약", "예약발행", "예약 발행"]:
            try:
                el = driver.find_element(
                    By.XPATH, f"//*[contains(.,'{text}') and "
                              f"(self::label or self::span or self::div or self::input)]")
                el.click()
                time.sleep(1)
                reserve_selected = True
                log.info(f"    ✅ 예약발행 라디오 선택 (XPath: '{text}')")
                break
            except (NoSuchElementException, ElementNotInteractableException):
                continue
    if not reserve_selected:
        log.warning("    ⚠️  예약발행 라디오 선택 실패")
        # 디버그: 발행 패널 내 요소 출력
        try:
            panel_info = driver.execute_script("""
                return Array.from(document.querySelectorAll(
                    'input[type="radio"], label, [class*="publish"], [class*="reserve"]'
                )).slice(0, 15).map(e =>
                    e.tagName + '.' + e.className.substring(0,40) + ' | ' +
                    (e.textContent || '').trim().substring(0,30));
            """)
            log.info(f"    [DEBUG] 발행 패널 요소: {panel_info[:8]}")
        except WebDriverException:
            log.debug("발행 패널 디버그 정보 수집 실패")

    # ── 날짜 입력 ──
    # 2026 네이버 에디터: input.input_date__* (readOnly — 달력 팝업으로만 변경 가능)
    # 전략: 클릭 → 달력 팝업 → 헤더의 월/년 확인 → next/prev 버튼으로 목표 월까지 이동 → 날짜 셀 클릭
    target_day = dt.day
    target_month = dt.month
    target_year = dt.year
    date_ok = False
    # ★날짜 input 렌더 대기(간헐적 미렌더 타이밍 방어) — 예약 라디오 선택 직후 날짜 input이
    # 아직 안 붙은 상태에서 조회하면 NO_DATE_INPUT 으로 예약이 실패하던 버그(2026-08-31 실측:
    # 같은 셀렉터가 됐다 안 됐다 = 타이밍). 있으면 즉시 진행, 없으면 예약 라디오 재선택 후 재대기.
    def _date_input_present() -> bool:
        try:
            return bool(driver.execute_script(
                'return !!document.querySelector(\'input[class*="input_date"]\');'))
        except WebDriverException:
            return False

    def _wait_date_input(ticks: int = 12) -> bool:
        for _ in range(ticks):
            if _date_input_present():
                return True
            time.sleep(0.5)
        return False

    if not _wait_date_input():
        log.info("    날짜 input 미렌더 — 예약 라디오 재선택 후 재대기")
        if not _try_click_selectors(driver, [
            "#radio_time2", "input[value='pre']", "label[for='radio_time2']",
        ], post_delay=1.0, scroll=False):
            _js_click_first(driver, [
                "#radio_time2", "[data-testid='preTimeRadioBtn']",
                "input[value='pre']", "label[for='radio_time2']",
            ])
            time.sleep(1.0)
        _wait_date_input()
    try:
        date_result = driver.execute_script("""
            var dateInput = document.querySelector('input[class*="input_date"]');
            if (!dateInput) return 'NO_DATE_INPUT';
            // 1) 날짜 input 클릭 → 달력 팝업 열기
            dateInput.click();
            return 'CALENDAR_OPENED';
        """)
        log.info(f"    달력 열기: {date_result}")
        time.sleep(1)

        # 달력 헤더의 현재 월/년 → 목표 월/년까지 next/prev 버튼으로 이동
        # 월 경계를 못 넘어가는 버그(5/31 → 6/1)의 근본 원인:
        # 기존 코드는 day 숫자만 매칭했고, 달력은 항상 현재 월이 열리기 때문에
        # 6월 1일을 클릭하려 해도 5월 1일 셀이 매칭되어 버렸음.
        nav_result = driver.execute_script("""
            var targetY = arguments[0], targetM = arguments[1];  // targetM: 1~12

            function readCurrent() {
                var monthEl = document.querySelector('.ui-datepicker-month');
                var yearEl  = document.querySelector('.ui-datepicker-year');
                if (!monthEl || !yearEl) return null;
                var mVal, yVal;
                if (monthEl.tagName === 'SELECT') {
                    // jQuery UI는 0-indexed (0=1월)
                    mVal = parseInt(monthEl.value, 10) + 1;
                } else {
                    var mtxt = (monthEl.textContent || '').replace(/[^0-9]/g, '');
                    mVal = parseInt(mtxt, 10);
                }
                if (yearEl.tagName === 'SELECT') {
                    yVal = parseInt(yearEl.value, 10);
                } else {
                    var ytxt = (yearEl.textContent || '').replace(/[^0-9]/g, '');
                    yVal = parseInt(ytxt, 10);
                }
                if (!isFinite(mVal) || !isFinite(yVal)) return null;
                return {month: mVal, year: yVal};
            }

            var cur = readCurrent();
            if (!cur) return 'NO_HEADER';

            var steps = (targetY - cur.year) * 12 + (targetM - cur.month);
            if (steps === 0) return 'NAV_OK:0|' + cur.year + '-' + cur.month;

            var selector = steps > 0 ? '.ui-datepicker-next' : '.ui-datepicker-prev';
            var clicked = 0;
            var safetyLimit = Math.min(Math.abs(steps), 24);  // 최대 24개월 이동
            for (var i = 0; i < safetyLimit; i++) {
                var btn = document.querySelector(selector);
                if (!btn || btn.classList.contains('ui-state-disabled')) {
                    return 'NAV_BLOCKED:' + clicked + '/' + steps;
                }
                btn.click();
                clicked++;
            }
            var after = readCurrent();
            var afterStr = after ? (after.year + '-' + after.month) : 'UNKNOWN';
            return 'NAV_OK:' + clicked + '|' + afterStr;
        """, target_year, target_month)
        log.info(f"    달력 월 이동: {nav_result}")
        time.sleep(0.5)

        # 달력에서 올바른 날짜 클릭 — 다른 월 셀(.ui-datepicker-other-month)은 제외
        date_click = driver.execute_script("""
            var targetD = arguments[0];

            // 1순위: 현재 월에 속하는 활성 셀 (jQuery UI Datepicker)
            var cells = document.querySelectorAll(
                'td:not(.ui-datepicker-other-month):not(.ui-datepicker-unselectable) ' +
                'a.ui-state-default, ' +
                'td:not(.ui-datepicker-other-month):not(.ui-datepicker-unselectable) ' +
                'button.ui-state-default');
            for (var c of cells) {
                if (!c.offsetParent) continue;
                var t = (c.textContent || '').trim();
                if (t === String(targetD)) {
                    c.click();
                    return 'CLICKED:' + t;
                }
            }
            // 2순위 폴백: 다른 월/비활성 셀 제외한 모든 visible 요소
            var all = document.querySelectorAll(
                'td:not(.ui-datepicker-other-month):not(.ui-datepicker-unselectable) ' +
                'button, ' +
                'td:not(.ui-datepicker-other-month):not(.ui-datepicker-unselectable) a, ' +
                'td:not(.ui-datepicker-other-month):not(.ui-datepicker-unselectable) ' +
                '[role="gridcell"]');
            for (var el of all) {
                if (!el.offsetParent) continue;
                if ((el.textContent||'').trim() === String(targetD)) {
                    el.click();
                    return 'CLICKED_FB:' + targetD;
                }
            }
            return 'NOT_FOUND';
        """, target_day)
        log.info(f"    달력 날짜 클릭: {date_click}")
        time.sleep(1)

        if 'CLICKED' in str(date_click):
            # 달력에서 날짜를 선택하면 자동으로 input 값 변경
            actual = driver.execute_script(
                "var el = document.querySelector('input[class*=\"input_date\"]'); return el ? el.value : '';")
            # 검증: 년·월·일을 위치 기반으로 추출해 목표와 정확히 비교.
            # (기존 digit-set 멤버십은 '2026.06.01' vs '2026.01.06' 같은 오탐 여지가 있었음)
            # 값을 읽지 못한 경우(input_date 셀렉터 드리프트)는 달력 클릭(CLICKED)·월이동
            # (NAV_OK)이 이미 설정을 확인했으므로 차단하지 않고 진행한다. "읽었는데 틀림"만 차단.
            if not _date_value_readable(actual):
                date_ok = True
                log.warning(
                    f"    ⚠️  날짜 재확인 불가(값 읽기 실패: '{actual}') — 클릭 성공에 근거해 진행")
            elif _date_matches_target(actual, target_year, target_month, target_day):
                date_ok = True
                log.info(f"    ✅ 날짜 입력: {actual}")
            else:
                log.warning(
                    f"    ⛔ 날짜 클릭 후 값 불일치: '{actual}' "
                    f"(목표 {target_year}-{target_month:02d}-{target_day:02d}) — 예약 시각 검증 실패"
                )
        else:
            # 달력이 안 뜨면 → ArrowUp/Down 키로 날짜 변경 시도
            log.info("    달력 미표시 — ArrowDown 키로 날짜 변경 시도")
            date_el = driver.find_element(By.CSS_SELECTOR, "input[class*='input_date']")
            date_el.click()
            time.sleep(0.5)
            # 현재 날짜에서 목표 날짜까지 차이만큼 ArrowDown (월 경계 자동 처리)
            from datetime import date as _date
            diff_days = (dt.date() - _date.today()).days if hasattr(dt, 'date') else 1
            if diff_days > 0:
                for _ in range(diff_days):
                    date_el.send_keys(Keys.ARROW_DOWN)
                    time.sleep(0.2)
                date_el.send_keys(Keys.ENTER)
                time.sleep(0.5)
            actual = date_el.get_attribute("value") or ""
            log.info(f"    ArrowDown 후 날짜: {actual}")
            # 폴백 경로도 실제로 목표 연/월/일이 반영됐는지 위치 기반으로 검증
            if _date_matches_target(actual, target_year, target_month, target_day):
                date_ok = True
            else:
                log.warning(
                    f"    ⛔ ArrowDown 폴백 검증 실패: '{actual}' "
                    f"(목표 {target_year}-{target_month:02d}-{target_day:02d}) — 예약 시각 검증 실패"
                )

    except (NoSuchElementException, ElementNotInteractableException, WebDriverException) as e:
        log.warning(f"    ⚠️  날짜 입력 예외: {e}")
    if not date_ok:
        log.warning(f"    ⚠️  날짜 입력 실패")

    # ── 시간/분 입력 (select 드롭다운) ──
    # 2026 네이버 에디터: select.hour_option__* (00~23), select.minute_option__* (00,10,20,30,40,50)
    hour_str   = f"{dt.hour:02d}"
    # 분은 10분 단위로 내림 (예: 35분 → 30분)
    minute_val = (dt.minute // 10) * 10
    minute_str = f"{minute_val:02d}"
    time_ok = False

    try:
        result = driver.execute_script("""
            var hourVal = arguments[0], minVal = arguments[1];
            var hourSet = false, minSet = false;

            function setSelect(el, val) {
                var nativeSet = Object.getOwnPropertyDescriptor(
                    window.HTMLSelectElement.prototype, 'value');
                if (nativeSet && nativeSet.set) nativeSet.set.call(el, val);
                else el.value = val;
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }

            // 시간 select (hour_option 또는 첫 번째 visible select)
            var hourEl = document.querySelector('select[class*="hour_option"]');
            if (hourEl) { setSelect(hourEl, hourVal); hourSet = true; }

            // 분 select (minute_option 또는 두 번째 visible select)
            var minEl = document.querySelector('select[class*="minute_option"]');
            if (minEl) { setSelect(minEl, minVal); minSet = true; }

            // 폴백: visible select에서 추론
            if (!hourSet || !minSet) {
                var sels = document.querySelectorAll('select');
                for (var s of sels) {
                    if (!s.offsetParent) continue;
                    var opts = Array.from(s.options).map(function(o) { return o.value; });
                    if (!hourSet && opts.indexOf(hourVal) >= 0 && opts.length >= 12) {
                        setSelect(s, hourVal); hourSet = true;
                    } else if (!minSet && opts.indexOf(minVal) >= 0 && opts.length < 12) {
                        setSelect(s, minVal); minSet = true;
                    }
                }
            }
            return {hour: hourSet, min: minSet};
        """, hour_str, minute_str)

        if result and result.get("hour") and result.get("min"):
            time_ok = True
            log.info(f"    ✅ 시간 입력: {hour_str}:{minute_str}")
        else:
            log.warning(f"    ⚠️  시간 입력 부분 실패: hour={result.get('hour')}, min={result.get('min')}")
    except WebDriverException as e:
        log.warning(f"    ⚠️  시간 설정 실패: {e}")

    if not time_ok:
        log.warning("    ⚠️  시간 입력 실패")
    time.sleep(0.5)

    # ── 예약 시간 검증 ── (select 현재 value를 목표값과 실제로 비교, fail-closed)
    # 네이버는 분을 10분 단위로 내림하므로(minute_val), 목표 분도 동일 규칙으로 내림해 비교한다.
    if time_ok:
        try:
            # 검증도 SET 과 동일한 폴백(주 셀렉터 → visible select 추론)으로 값을 읽어
            # "폴백으로 설정 → 주 셀렉터로만 검증 → null" 로 인한 오차단을 방지한다.
            actual = driver.execute_script("""
                var h = document.querySelector('select[class*="hour_option"]');
                var m = document.querySelector('select[class*="minute_option"]');
                var hv = h ? h.value : '', mv = m ? m.value : '';
                if (!hv || !mv) {
                    var sels = document.querySelectorAll('select');
                    for (var s of sels) {
                        if (!s.offsetParent) continue;
                        var opts = Array.from(s.options).map(function(o){return o.value;});
                        if (!hv && opts.length >= 12) hv = s.value;
                        else if (!mv && opts.length < 12) mv = s.value;
                    }
                }
                return {hour: hv, min: mv};
            """)
            actual_hour = _digits_to_int(actual.get("hour", ""))
            actual_min = _digits_to_int(actual.get("min", ""))
            log.info(f"    예약 시간 검증: {actual.get('hour','')}:{actual.get('min','')}")
            if actual_hour is None or actual_min is None:
                # 값을 다시 읽지 못함(셀렉터 드리프트 등). SET 단계가 이미 성공을
                # 확인(hourSet/minSet)했으므로 차단하지 않고 경고만 남긴다.
                log.warning(
                    "    ⚠️  예약 시간 재확인 불가(값 읽기 실패) — 설정 성공에 근거해 진행")
            elif actual_hour != dt.hour or actual_min != minute_val:
                # 확정적 불일치 → 잘못된 시각 발행을 막기 위해 차단(fail-closed).
                time_ok = False
                log.warning(
                    f"    ⛔ 예약 시간 불일치: 화면 "
                    f"{actual.get('hour','')}:{actual.get('min','')} vs "
                    f"목표 {hour_str}:{minute_str} — 예약 시각 검증 실패"
                )
        except WebDriverException as e:
            # 검증 JS 자체가 실패한 것은 우리 도구의 한계이지 시각 오설정의 증거가
            # 아니다. SET 성공에 근거해 진행하되 경고를 남긴다.
            log.warning(f"    ⚠️  예약 시간 검증 JS 실패 — 설정 성공에 근거해 진행: {e}")

    # 날짜·시간 검증이 모두 통과해야만 True (fail-closed)
    return bool(date_ok and time_ok)


def _digits_to_int(value):
    """'07', '00', '7' 등 select value 문자열에서 숫자만 뽑아 int로 변환. 실패 시 None."""
    if value is None:
        return None
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _date_matches_target(actual: str, target_year: int,
                         target_month: int, target_day: int) -> bool:
    """input_date 값(예: '2026.06.01' / '2026-06-01 07:00')에서 연·월·일을 위치 기반으로
    추출해 목표와 정확히 일치하는지 확인한다.

    기존 digit-set 멤버십 방식은 자릿수만 비교해 '2026.06.01'과 '2026.01.06'을 구분하지
    못했다. 여기서는 정규식으로 첫 YYYY.MM.DD(구분자 무관) 패턴을 뽑아 위치 그대로 비교한다.
    값이 비어 있으면(빈 문자열) 판정 불가로 False를 반환한다(fail-closed).
    """
    import re
    if not actual:
        return False
    match = re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", actual)
    if not match:
        return False
    year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return year == target_year and month == target_month and day == target_day


def _date_value_readable(actual: str) -> bool:
    """input_date 읽기값에서 YYYY.MM.DD 패턴을 추출할 수 있는지(=검증 가능 상태인지).

    False 면 값을 아예 읽지 못한 것(셀렉터 드리프트 등)이므로 '불일치'가 아니라
    '검증 불가'로 다뤄, 설정(달력 클릭·월이동)이 성공한 경우 차단하지 않는다.
    """
    import re
    if not actual:
        return False
    return re.search(r"(\d{4})\D+(\d{1,2})\D+(\d{1,2})", actual) is not None


def _confirm_reservation_ex(driver) -> dict:
    """확인/예약 버튼을 클릭하고 발행 결과를 검증한다 (dict 반환 버전, 2026-09-18).

    Returns:
        {"ok": bool, "button_clicked": bool, "signal": str, "evidence": dict|None}
        signal/evidence 의미는 ``_verify_publish_signal`` 참조.
    """
    pre_url = driver.current_url
    confirm_clicked = False

    # 2026 네이버 에디터: confirm_btn__* 클래스, 텍스트="발행"
    for sel in [
        "button[class*='confirm_btn']",
        ".btn_ok", ".se-btn-confirm",
        "button[data-action='reserve']",
        ".se-popup-button-confirm",
    ]:
        try:
            confirm_btn = driver.find_element(By.CSS_SELECTOR, sel)
            if confirm_btn.is_displayed():
                try:
                    confirm_btn.click()
                except (ElementNotInteractableException,
                        ElementClickInterceptedException):
                    driver.execute_script("arguments[0].click();", confirm_btn)
                confirm_clicked = True
                time.sleep(3)
                break
        except (NoSuchElementException, ElementNotInteractableException,
                ElementClickInterceptedException):
            continue

    # JS 폴백: 패널 하단의 "발행" 버튼 (상단 발행 버튼과 구분)
    if not confirm_clicked:
        try:
            btn = driver.execute_script("""
                var btns = document.querySelectorAll('button');
                for (var b of btns) {
                    var t = (b.textContent || '').trim();
                    var cls = (b.className || '');
                    // confirm_btn 또는 패널 내부 발행 버튼 (상단 publish_btn 제외)
                    if (t === '발행' && b.offsetParent && cls.indexOf('publish_btn') < 0) return b;
                }
                return null;
            """)
            if btn:
                driver.execute_script("arguments[0].click();", btn)
                confirm_clicked = True
                time.sleep(3)
                log.info("    ✅ 발행 확인 버튼 클릭 (JS)")
        except WebDriverException:
            pass

    # fail-closed: 확인 버튼을 실제로 누르지 못했으면 발행이 확정되지 않았으므로
    # URL 기반 검증(예약발행은 URL이 안 바뀌는 게 정상)으로 넘어가지 말고 즉시 실패 반환.
    if not confirm_clicked:
        log.warning("  ⛔ 예약 확인 버튼을 찾지/누르지 못함 — 발행 미확정으로 판정")
        return {"ok": False, "button_clicked": False, "signal": "none", "evidence": None}

    # 발행 결과 검증 (예약 경로: URL 변경은 성공 신호로 쓰지 않음)
    ok, signal, evidence = _verify_publish_signal(
        driver, pre_url, confirm_clicked, is_reservation=True)
    return {"ok": ok, "button_clicked": confirm_clicked, "signal": signal, "evidence": evidence}


def _confirm_reservation(driver) -> bool:
    """확인/예약 버튼을 클릭하고 발행 결과를 검증합니다. (``_confirm_reservation_ex`` 의 bool 래퍼)"""
    return _confirm_reservation_ex(driver)["ok"]


# ──────────────────────────────────────────────────
# 📅 예약발행 시간 설정 (핵심 신규 기능)
# ──────────────────────────────────────────────────
def _set_reservation_ex(driver, wait, reserve_datetime: str, tags: list = None,
                        category_label: str = None, fallback=None) -> dict:
    """
    reserve_datetime: "2026-03-05 07:00" 형식
    네이버 스마트에디터의 예약발행 UI를 조작한다 (dict 반환 버전, 2026-09-18).
    tags: 발행 패널 내 태그 입력 영역에 넣을 태그 리스트 (None 또는 [] 허용)
    category_label/fallback: 발행 패널 열린 직후 카테고리 설정용 (2026-05-21 처방).
                              dropdown 옵션은 발행 패널이 열려야 DOM에 등장하므로
                              여기서 호출해야 실제로 적용된다.

    Returns:
        {"ok": bool, "button_clicked": bool, "signal": str, "evidence": dict|None}
        확정(예약) 버튼을 누르기 전 단계의 실패는 항상 button_clicked=False 로 돌아온다
        (이 함수 안에서 확정 버튼 클릭은 오직 ``_confirm_reservation_ex`` 안에서만 일어난다).
    """
    _no_click = {"ok": False, "button_clicked": False, "signal": "none", "evidence": None}

    from datetime import datetime, timedelta
    dt = datetime.strptime(reserve_datetime, "%Y-%m-%d %H:%M")

    # ── 예약 시각 경계 가드 (fail-closed) ──
    # 과거·임박(현재+5분 이전)이거나 과도하게 먼 미래(400일 초과, 연도 오타 방어)면
    # 발행 패널을 열지 않고 즉시 실패. safetyLimit(최대 24개월 이동)로 인해 목표와 다른 달이
    # 조용히 확정되는 사고도 여기서 예방된다.
    now = datetime.now()
    min_lead = timedelta(minutes=5)
    max_horizon = timedelta(days=400)
    if dt < now + min_lead:
        log.warning(
            f"  ⛔ 예약 시각이 과거/임박 — 스킵: {reserve_datetime} "
            f"(현재 {now:%Y-%m-%d %H:%M}, 최소 유예 5분)"
        )
        return dict(_no_click)
    if dt > now + max_horizon:
        log.warning(
            f"  ⛔ 예약 시각이 과도하게 먼 미래(400일 초과) — 스킵: {reserve_datetime} "
            f"(연도 오타 가능성)"
        )
        return dict(_no_click)

    log.info(f"  📅 예약발행 설정: {reserve_datetime}")
    try:
        if not _open_reservation_panel(driver):
            return dict(_no_click)

        # 발행 패널이 열린 직후에만 카테고리 dropdown 옵션 DOM이 존재한다 (2026-05-21 처방).
        # 카테고리는 시각만큼 치명적이지 않으므로 fail-open: 미확정이어도 경고만 남기고 계속.
        if not _set_category(driver, category_label=category_label, fallback=fallback):
            log.warning(
                "  ⚠️ 카테고리 미확정 — 기본 메뉴로 발행될 수 있음 (셀렉터 점검 필요)"
            )

        # 발행 패널이 열린 직후에만 태그 입력 영역 DOM이 존재한다 (2026-05-09 처방).
        _input_tags(driver, tags or [])

        # fail-closed: 예약 시각 설정/검증이 실패하면 확정 버튼을 누르지 않고 실패 반환.
        # 잘못된 시각으로 발행하느니 미발행(재시도 대상)이 낫다.
        if not _set_date_time(driver, dt):
            log.warning("  ⛔ 예약 시각 설정/검증 실패 — 발행 확정 중단")
            return dict(_no_click)

        return _confirm_reservation_ex(driver)

    except (WebDriverException, ValueError, OSError, AttributeError) as e:
        log.error(f"  ❌ 예약발행 설정 실패: {type(e).__name__}: {str(e)[:100]}")
        return dict(_no_click)


def _set_reservation(driver, wait, reserve_datetime: str, tags: list = None,
                     category_label: str = None, fallback=None):
    """네이버 스마트에디터의 예약발행 UI를 조작한다. (``_set_reservation_ex`` 의 bool 래퍼)"""
    return _set_reservation_ex(driver, wait, reserve_datetime, tags=tags,
                               category_label=category_label, fallback=fallback)["ok"]


# ──────────────────────────────────────────────────
# 📤 포스트 업로드 (재시도 + 예약발행 통합)
# ──────────────────────────────────────────────────
def _postwrite_url(post_data: dict = None) -> str:
    """postwrite 에디터 URL — 카테고리 번호를 쿼리로 미리 지정한다.

    발행 패널의 카테고리 dropdown 클릭은 에디터 개편에 취약하고, 클릭 성공
    로그가 실반영을 보장하지 못한다(2026-07-19 실측: '선택 완료' 로그에도
    기본 카테고리로 발행됨). 에디터를 열 때부터 ?categoryNo= 로 지정하면
    발행 패널이 그 카테고리로 시작하므로 이것을 1차 경로로 삼고,
    _set_category 의 dropdown 클릭은 검증·보정용 2차 경로로 유지한다.
    번호를 못 정하면 쿼리 없이 기존 URL (기존 동작 보존).
    """
    base = f"https://blog.naver.com/{NAVER_ID}/postwrite"
    try:
        cat_no = _resolve_category_no(
            (post_data or {}).get("category"), BLOG_CATEGORY_NO)
    except Exception:
        cat_no = None
    if cat_no in (None, ""):
        return base
    return f"{base}?categoryNo={cat_no}"


def upload_post_ex(driver: webdriver.Chrome,
                   post_data: dict,
                   image_paths: list,
                   reserve_datetime: str = None) -> dict:
    """포스트 업로드 — 결과 신호를 함께 돌려주는 버전 (2026-09-18, 계약 §2.7).

    post_data: {title, content(HTML), tags}
    image_paths: 이미지 경로 리스트
    reserve_datetime: "2026-03-05 07:00" — None이면 즉시발행

    Returns:
        {"ok": bool, "phase": "pre_submit"|"post_submit", "signal": str,
         "reservation_evidence": dict|None, "attempts": int, "error": str|None}

    ``settings.UPLOAD_RECONCILE_ON_UNCONFIRMED``(기본 False) 가 꺼져 있으면
    재시도 루프는 기존 ``upload_post`` 와 완전히 동일하게 동작한다(회귀 테스트로 고정).
    켜져 있을 때만 phase == "post_submit"(발행/예약 확정 버튼을 눌렀는데 성공 신호를
    읽지 못한 경우) 실패는 재시도하지 않고 즉시 반환한다 — 중복 발행을 막기 위해서다.
    """
    wait = WebDriverWait(driver, ELEMENT_WAIT)
    mode = f"예약({reserve_datetime})" if reserve_datetime else "즉시발행"
    log.info(f"  📤 업로드 [{mode}]: {post_data['title'][:35]}...")

    # 2026-09-18 마감 결함 B: 확정 후 재시도 금지도 선택 기능이 아니라 무인 운영의
    # 필수 보호다 — 설정이 꺼져 있어도 autonomy_guard 세션 안이면 강제 적용한다.
    try:
        from core.autonomy_guard import guards_active as _guards_active
    except Exception:  # noqa: BLE001 — 가드 모듈 미로드 환경이면 기존 동작(설정값만)
        _guards_active = lambda: False
    gate_on = bool(getattr(_settings_module, "UPLOAD_RECONCILE_ON_UNCONFIRMED", False)) or _guards_active()

    last: dict = {"ok": False, "phase": "pre_submit", "signal": "none",
                  "reservation_evidence": None, "attempts": 0, "error": None}

    for attempt in range(1, MAX_RETRY_COUNT + 2):
        last["attempts"] = attempt
        try:
            result = _do_upload_ex(driver, wait, post_data, image_paths, reserve_datetime)
            ok = bool(result.get("ok"))
            phase = "post_submit" if result.get("button_clicked") else "pre_submit"
            last.update(ok=ok, phase=phase, signal=result.get("signal", "none"),
                        reservation_evidence=result.get("evidence"), error=None)
            if ok:
                return last

            # post_submit 실패(확정 버튼은 눌렀으나 성공 신호를 못 읽음)는 게이트가 켜졌을
            # 때만 재시도를 금지한다 — 같은 글을 다시 올려 중복 발행하는 사고를 차단한다.
            if gate_on and phase == "post_submit":
                log.warning(
                    "  ⛔ 발행/예약 확정 버튼을 눌렀으나 성공 신호 불명 — "
                    "재시도 중단(중복 발행 방지, UPLOAD_RECONCILE_ON_UNCONFIRMED=True)"
                )
                return last

            if attempt <= MAX_RETRY_COUNT:
                log.info(f"  🔄 재시도 {attempt}/{MAX_RETRY_COUNT} ({RETRY_WAIT_SECONDS}초 후)")
                time.sleep(RETRY_WAIT_SECONDS)
                driver.get(_postwrite_url(post_data))
                time.sleep(4)
        except (TimeoutException, NoSuchElementException,
                ElementNotInteractableException, StaleElementReferenceException) as e:
            log.info(f"  ❌ 업로드 오류 (시도 {attempt}): {type(e).__name__}: {str(e)[:80]}")
            # 이 예외들은 확정 버튼 클릭 이전 단계(제목/본문 입력·요소 탐색)에서만 발생한다
            # — _set_reservation_ex/_publish_now_ex 는 자체적으로 이 예외들을 잡아 bool/dict로
            # 돌려주므로(raise 하지 않음) 여기로 새어나온 예외는 항상 pre_submit 이다.
            last.update(ok=False, phase="pre_submit", signal="none",
                        reservation_evidence=None,
                        error=f"{type(e).__name__}: {str(e)[:200]}")
        except Exception as e:
            log.error(f"  ❌ 업로드 예상외 오류 (시도 {attempt}): {type(e).__name__}: {str(e)[:80]}")
            last.update(ok=False, phase="pre_submit", signal="none",
                        reservation_evidence=None,
                        error=f"{type(e).__name__}: {str(e)[:200]}")
            if attempt <= MAX_RETRY_COUNT:
                time.sleep(RETRY_WAIT_SECONDS)

    return last


def upload_post(driver: webdriver.Chrome,
                post_data: dict,
                image_paths: list,
                reserve_datetime: str = None) -> bool:
    """
    post_data: {title, content(HTML), tags}
    image_paths: 이미지 경로 리스트
    reserve_datetime: "2026-03-05 07:00" — None이면 즉시발행

    (``upload_post_ex`` 의 bool 래퍼 — 기존 호출자는 무수정으로 동작한다.)
    """
    return upload_post_ex(driver, post_data, image_paths, reserve_datetime)["ok"]


_draft_handled = False   # 드래프트 팝업이 이미 처리되었는지 추적
_draft_lock = threading.Lock()


def _ensure_editor_ready(driver):
    """
    매 작업 전 호출: 팝업/오버레이/모달이 에디터를 가리고 있으면 제거한다.
    드래프트 복구 팝업은 한 번만 처리하고 이후에는 건너뛴다.
    """
    global _draft_handled
    dismiss_popups(driver)
    with _draft_lock:
        if not _draft_handled:
            result = dismiss_draft_recovery_popup(driver)
            if result:
                _draft_handled = True
                time.sleep(1)
    dismiss_editor_popups(driver)


def _should_insert_images(post_data: dict, image_paths) -> bool:
    """이미지 삽입 여부 판정(순수 함수).

    전역 ``SKIP_IMAGE_INSERT`` 는 불변이다. 사진 인제스트(P0-② Phase 2) 글은
    post_data 에 per-post 신호 ``_force_image_insert=True`` 를 실어, 전역이 켜져
    있어도(=평소 수동 삽입) 그 글 경로에서만 실사진 업로드를 켠다.

    Args:
        post_data: 발행 메타. ``_force_image_insert`` 신호를 읽는다.
        image_paths: 삽입할 이미지 경로 리스트(비어 있으면 항상 False).

    Returns:
        _insert_images 를 호출해야 하면 True.
    """
    if not image_paths:
        return False
    force = bool((post_data or {}).get("_force_image_insert"))
    return force or not SKIP_IMAGE_INSERT


def _images_expected(post_data: dict) -> bool:
    """이 글이 **이미지와 함께** 발행돼야 하는가(순수 판정).

    ``SKIP_IMAGE_INSERT=True`` 는 사용자가 "이미지는 내가 직접 넣겠다"고 끈 상태이므로
    텍스트 전용 발행이 정당하다. 그 반대(기본값 False)는 **이미지를 넣어 달라는 요구**다
    — 이때 이미지가 0장이면 조용히 텍스트만 올리지 않고 발행을 보류한다.
    """
    if (post_data or {}).get("_force_image_insert"):
        return True
    return not SKIP_IMAGE_INSERT


def _prepare_publish_images(driver: object, wait: object, post_data: dict,
                            image_paths: list) -> Optional[int]:
    """Return the required editor image count, or None if insertion is unsafe.

    ★ 모든 발행 경로(신규·보관본·레거시·수동 GUI·무인 운영·자동 스케줄)가 지나는
    **공통 최종 경계**다. 여기서 ``None`` 을 돌려주면 호출자는 발행/예약 확정 버튼을
    누르지 않는다(fail-closed, 아직 아무것도 제출되지 않은 상태).

    2026-09-23 보강 — 예전 게이트는 "경로가 실재하는가"만 봤다. 그래서
    ①내용이 안 채워진 자리표시 카드 ②참고용 외부 사이트 캡처처럼 **파일도 있고
    정상 디코드도 되는** 잘못된 자산이 그대로 발행됐고, ③이미지가 0장인 글은
    사용자가 이미지를 원하는 설정에서도 텍스트만 조용히 올라갔다.
    이제 셋 다 막는다. 판정 범위·한계는 `core.image_asset_guard` 문서 참조 —
    이미지 **내용이 글과 맞는지**는 여기서 판단하지 않는다(과대주장 금지).
    """
    if not _should_insert_images(post_data, image_paths):
        if _images_expected(post_data):
            log.error(
                "이미지가 0장이라 발행을 보류합니다 — 이 글은 이미지와 함께 발행하도록 "
                "설정돼 있습니다. 보관함에서 이미지를 채운 뒤 다시 시도하세요.")
            return None
        return 0
    try:
        if any(not path or not Path(path).is_file() for path in image_paths):
            log.error("이미지 파일이 누락되어 발행 중단 — 보관함 이미지를 확인하세요")
            return None

        # ── 자산 품질·출처 판정(깨진 파일·자리표시 카드·참고 캡처 차단) ──
        try:
            from core import image_asset_guard as _guard
            ok, findings, _publishable = _guard.verify_publishable(
                image_paths,
                title=str((post_data or {}).get("title") or ""),
                category=str((post_data or {}).get("category") or ""))
        except Exception:  # noqa: BLE001 — 판정기 자체가 죽으면 보수적으로 막는다
            log.error("이미지 자산 판정 실패로 발행 중단", exc_info=True)
            return None
        if not ok:
            # 출처를 모르는 자산도 여기서 막힌다(2026-09-23 마감 보완) — 예전에는
            # `unverified_legacy` 를 경고만 남기고 통과시켰고, 그 틈이 이번 사고와
            # 같은 부류(확인되지 않은 이미지)를 그대로 발행되게 했다.
            log.error("발행할 수 없는/확인되지 않은 이미지가 있어 발행을 보류합니다 — %s",
                      _guard.describe(findings))
            return None

        _ensure_editor_ready(driver)
        before = _count_image_components(driver)
        if before < 0:
            log.error("에디터 이미지 수를 확인할 수 없어 발행 중단")
            return None
        if not _insert_images(driver, wait, image_paths):
            log.error("이미지 삽입 실패로 발행 중단")
            return None
        required = before + len(image_paths)
        return required if _verify_publish_images(driver, required) else None
    except (WebDriverException, OSError, ValueError, TypeError):
        log.error("이미지 준비 실패로 발행 중단", exc_info=True)
        return None


def _verify_publish_images(driver: object, required: int) -> bool:
    """Check that all required images remain in the editor before publication."""
    if required == 0:
        return True
    actual = _count_image_components(driver)
    if actual < required:
        log.error("이미지 등록/보존 미확인으로 발행 중단 (필요=%s, 확인=%s)", required, actual)
        return False
    return True


def _run_prepublish_steps(driver, wait, post_data, image_paths) -> bool:
    """확정(발행/예약) 버튼을 누르기 전까지의 모든 준비 단계.

    ``_do_upload``/``_do_upload_ex`` 공용 헬퍼(2026-09-18 추출 — 동작은 이전과 완전히
    동일하고, 두 함수가 같은 소스를 공유하도록 구조만 정리했다). 에디터 로드 대기 →
    발행 대상 신원 게이트 → 팝업 정리 → iframe 전환 → 이미지 준비/삽입 → 본문 입력 →
    이미지 최종 검증까지 수행한다.

    Returns:
        True — 확정 버튼(발행/예약)을 눌러도 되는 상태.
        False — 확정 버튼을 누르기 전 단계에서 실패(pre_submit, 아무것도 확정되지 않음).
    """
    global _draft_handled
    with _draft_lock:
        _draft_handled = False  # 새 업로드마다 리셋

    driver.get(_postwrite_url(post_data))

    # ── 에디터 완전 로드 대기 (sleep 대신 요소 대기) ──
    try:
        WebDriverWait(driver, 20).until(
            EC.presence_of_element_located((By.CSS_SELECTOR,
                ".se-title-text, .se-documentTitle, #subject")))
        log.info("  ✅ 에디터 제목 영역 감지됨")
    except TimeoutException:
        log.warning("  ⚠️  에디터 제목 영역 로드 타임아웃 — 계속 진행")
    time.sleep(1)

    # ── 🔒 발행 대상 신원 게이트 (오발행 방지) — 글 쓰기 전, iframe 전환 전 ──
    # 확실히 다른 계정이면 여기서 중단(아무 것도 쓰기 전). 정확한 계정은 URL 로 항상 통과.
    if not _assert_publish_identity(driver):
        return False

    # ── 에디터 진입 시 팝업 정리 ──
    _ensure_editor_ready(driver)

    # iframe 전환 (새 에디터는 iframe 없음 → 무시)
    for iframe_sel in ["mainFrame", "cafe_main"]:
        try:
            WebDriverWait(driver, 1).until(
                EC.frame_to_be_available_and_switch_to_it((By.ID, iframe_sel)))
            time.sleep(0.5)
            log.debug(f"  iframe 전환: {iframe_sel}")
            break
        except TimeoutException:
            continue
    else:
        log.debug("  iframe 없음 — 직접 접근")

    # ── 에디터 DOM 간단 디버그 ──
    try:
        debug_info = driver.execute_script("""
            var info = {};
            var titleEl = document.querySelector('.se-title-text');
            info.title = titleEl ? titleEl.className.substring(0,80) : 'NOT_FOUND';
            // 본문 영역 찾기 (제목 제외)
            var allP = document.querySelectorAll('.se-text-paragraph');
            var bodyPs = [];
            for (var i = 0; i < allP.length; i++) {
                if (!allP[i].closest('.se-documentTitle') &&
                    !allP[i].closest('.se-section-documentTitle') &&
                    !allP[i].closest('.se-title-text')) {
                    bodyPs.push(allP[i].tagName + '.' + (allP[i].className||'').substring(0,40));
                }
            }
            info.bodyParagraphs = bodyPs;
            info.totalP = allP.length;
            info.ceElements = document.querySelectorAll('[contenteditable=true]').length;
            return JSON.stringify(info);
        """)
        log.info(f"  [DEBUG] 에디터 DOM: {debug_info}")
    except WebDriverException as e:
        log.debug(f"  에디터 DOM 디버그 실패: {e}")

    # 일부 이미지 등록도 True인 기존 삽입 계약과 별도로, 발행은 전부 확인해야 한다.
    required_images = _prepare_publish_images(driver, wait, post_data, image_paths)
    if required_images is None:
        return False

    # ── 제목 + 본문 HTML (setDocumentData로 한번에 설정) ──
    # 본문은 반드시 변환된 HTML(content_html)을 넣어야 표/헤딩/인용이 네이버 컴포넌트로
    # 들어간다. content_html 이 있으면 그걸 우선하고, 없으면 content 로 폴백한다.
    # (예약 메인 경로는 content 에 이미 HTML 을 담아 넘기므로 폴백으로 동일하게 동작.
    #  리프레시 repost 경로는 content=raw 마크다운·content_html=HTML 이라, 예전엔
    #  content 를 읽어 raw 마크다운(##·|표|)이 그대로 발행되던 버그를 여기서 차단.)
    _ensure_editor_ready(driver)
    _body_html = post_data.get("content_html") or post_data.get("content", "")
    body_ok = _paste_html(driver, _body_html, title=post_data["title"])
    if body_ok is False:
        log.error("본문 입력 실패 — 포스트 스킵")
        return False
    time.sleep(1)

    # ── 태그 입력은 발행 패널이 열린 뒤에 수행되어야 한다 (2026-05-09 처방). ──
    # 패널 열기 전에 호출하면 태그 입력 영역 DOM이 존재하지 않아 silent fail 한다.
    # (카테고리도 같은 이유로 패널이 열린 직후 _set_reservation/_publish_now 안에서 처리한다.)
    _ensure_editor_ready(driver)
    if not _verify_publish_images(driver, required_images):
        return False

    return True


def _do_upload(driver, wait, post_data, image_paths, reserve_datetime) -> bool:
    """실제 업로드 로직"""
    if not _run_prepublish_steps(driver, wait, post_data, image_paths):
        return False

    # ── 카테고리 ──
    # post_data["category"] (detect_category 라벨)을 _set_reservation/_publish_now에
    # 전달한다. 카테고리 dropdown 옵션은 발행 패널이 열려야 DOM에 등장하므로
    # 본문 입력 시점이 아닌 발행 패널 열린 직후에 호출되어야 한다 (2026-05-21 처방).
    category_label = post_data.get("category")
    tags = post_data.get("tags", [])

    # ── 예약발행 or 즉시발행 ──
    if reserve_datetime and USE_RESERVATION:
        return _set_reservation(driver, wait, reserve_datetime, tags=tags,
                                category_label=category_label,
                                fallback=BLOG_CATEGORY_NO)
    else:
        return _publish_now(driver, wait, tags=tags,
                            category_label=category_label,
                            fallback=BLOG_CATEGORY_NO)


def _do_upload_ex(driver, wait, post_data, image_paths, reserve_datetime) -> dict:
    """``_do_upload`` 의 dict 반환 버전 (2026-09-18).

    준비 단계(``_run_prepublish_steps``)는 ``_do_upload`` 와 완전히 같은 코드를
    공유한다. 갈라지는 지점은 확정 버튼 이후뿐이며, 여기서는 신호를 함께 돌려주는
    ``_set_reservation_ex``/``_publish_now_ex`` 를 호출한다.

    Returns:
        {"ok": bool, "button_clicked": bool, "signal": str, "evidence": dict|None}
    """
    _no_click = {"ok": False, "button_clicked": False, "signal": "none", "evidence": None}
    if not _run_prepublish_steps(driver, wait, post_data, image_paths):
        return dict(_no_click)

    category_label = post_data.get("category")
    tags = post_data.get("tags", [])

    if reserve_datetime and USE_RESERVATION:
        return _set_reservation_ex(driver, wait, reserve_datetime, tags=tags,
                                   category_label=category_label,
                                   fallback=BLOG_CATEGORY_NO)
    else:
        return _publish_now_ex(driver, wait, tags=tags,
                               category_label=category_label,
                               fallback=BLOG_CATEGORY_NO)


# ──────────────────────────────────────────────────
# ✏️ 제목 입력
# ──────────────────────────────────────────────────
def _input_title(driver, title: str) -> bool:
    """제목 입력 — SE ONE 에디터 제목 영역에 텍스트를 삽입한다.
    SE ONE은 contenteditable을 사용하지 않고 내부 React 시스템을 통해
    입력을 처리하므로, 여러 전략을 순차 시도한다."""

    def _verify_title():
        """제목 검증 — textContent 직접 확인"""
        info = driver.execute_script("""
            var el = document.querySelector('.se-title-text');
            if (!el) return null;
            var text = (el.textContent || '').trim();
            var isEmpty = el.classList.contains('se-is-empty');
            var pEl = el.querySelector('.se-text-paragraph');
            var pText = pEl ? (pEl.textContent || '').trim() : '';
            var ae = document.activeElement;
            var aeInfo = ae ? ae.tagName + '.' + (ae.className||'').substring(0,50) : 'null';
            return {text: text.substring(0,60), pText: pText.substring(0,60),
                    isEmpty: isEmpty, len: text.length, pLen: pText.length,
                    activeElement: aeInfo};
        """)
        log.info(f"  [제목검증] {info}")
        if info:
            p_text = info.get('pText', '')
            if p_text and p_text != '제목' and len(p_text) >= 3:
                return True
            if not info.get('isEmpty') and info.get('len', 0) >= 3:
                return True
        return False

    def _cdp_full_click(x, y):
        """CDP 전체 마우스 이벤트 시퀀스 (mouseMoved → mousePressed → mouseReleased)
        SE 에디터는 mousemove 이벤트도 감지하므로 전체 시퀀스가 필요하다."""
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y
        })
        time.sleep(0.05)
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1
        })
        time.sleep(0.05)
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1
        })

    def _get_title_p_rect():
        """제목 내부 P 요소의 좌표를 가져온다."""
        return driver.execute_script("""
            var el = document.querySelector('.se-title-text .se-text-paragraph')
                  || document.querySelector('.se-title-text p');
            if (!el) return null;
            el.scrollIntoView({block:'center'});
            var r = el.getBoundingClientRect();
            return {x: r.x + r.width/2, y: r.y + r.height/2,
                    left: r.left + 10, top: r.top + r.height/2};
        """)

    # SE 에디터 인스턴스 확보
    _ensure_se_editor(driver)

    # ── 전략 1: setDocumentTitle + DOM 동기화 (결합) ──
    log.info("  전략 1: _documentService.setDocumentTitle() + DOM 동기화")
    try:
        result = driver.execute_script("""
            var editor = window.__seEditor;
            if (!editor || !editor._documentService) return 'NO_DOC_SVC';
            var title = arguments[0];
            var tried = [];

            // 1) 내부 상태 업데이트
            try {
                editor._documentService.setDocumentTitle(title);
                tried.push('internal:OK');
            } catch(e) {
                tried.push('internal:ERR:' + e.message.substring(0, 40));
            }

            // 2) DOM 동기화 — 시각적 업데이트
            var titleDiv = document.querySelector('.se-title-text');
            if (titleDiv) {
                var p = titleDiv.querySelector('.se-text-paragraph') || titleDiv.querySelector('p');
                if (p) {
                    titleDiv.querySelectorAll('.se-placeholder').forEach(function(ph) { ph.remove(); });
                    p.innerHTML = '';
                    var span = document.createElement('span');
                    span.className = 'se-fs- se-ff-';
                    span.textContent = title;
                    p.appendChild(span);
                    tried.push('dom:OK');
                }
                // se-is-empty 제거
                var el = titleDiv;
                while (el) {
                    el.classList.remove('se-is-empty');
                    if (el.classList.contains('se-section-documentTitle') || el.tagName === 'BODY') break;
                    el = el.parentElement;
                }
            }

            // 3) 렌더 트리거 시도
            var renderMethods = ['renderDocument', 'render', 'refresh', 'update', 'invalidate'];
            for (var m of renderMethods) {
                if (typeof editor._documentService[m] === 'function') {
                    try { editor._documentService[m](); tried.push('render:' + m); } catch(e) {}
                }
                if (typeof editor[m] === 'function') {
                    try { editor[m](); tried.push('editorRender:' + m); } catch(e) {}
                }
            }

            var got = editor._documentService.getDocumentTitle();
            return tried.join(', ') + ' | title=' + (got || '').substring(0, 40);
        """, title)
        log.info(f"  [setDocumentTitle+DOM] {result}")
        time.sleep(1)
        if _verify_title():
            log.info("  ✅ 제목 입력 완료 (setDocumentTitle+DOM)")
            return True
    except WebDriverException as e:
        log.info(f"  setDocumentTitle+DOM 실패: {e}")

    # ── 전략 2: _virtualEditable.focus() → 제목 클릭 → clipboard ──
    log.info("  전략 2: VE.focus() + 제목 클릭 + clipboard")
    rect = _get_title_p_rect()
    try:
        focus_result = driver.execute_script("""
            var editor = window.__seEditor;
            if (!editor || !editor._virtualEditable) return 'VE_NOT_FOUND';
            try {
                editor._virtualEditable.focus();
                editor._virtualEditable.showCursor();
                var ae = document.activeElement;
                return 'FOCUS_OK: ' + ae.tagName + '.' + (ae.className||'').substring(0,40) +
                       ' | id=' + (ae.id||'');
            } catch(e) {
                return 'FOCUS_ERR: ' + e.message.substring(0, 60);
            }
        """)
        log.info(f"  [VE focus] {focus_result}")
        time.sleep(0.3)

        # VE.focus() 후 제목 영역 CDP 클릭
        if rect:
            _cdp_full_click(int(rect['x']), int(rect['y']))
            time.sleep(0.5)
            ae = driver.execute_script(
                "var ae=document.activeElement; return ae?ae.tagName+'.'+(ae.className||'').substring(0,40)+' id='+(ae.id||''):'null'")
            log.info(f"  [FOCUS after VE+CDP click] {ae}")

        pyperclip.copy(title)
        ActionChains(driver).key_down(Keys.CONTROL).send_keys('v')\
            .key_up(Keys.CONTROL).perform()
        time.sleep(1.5)
        if _verify_title():
            log.info("  ✅ 제목 입력 완료 (VE focus + clipboard)")
            return True
    except (WebDriverException, OSError) as e:
        log.info(f"  VE focus 실패: {e}")

    # ── 전략 3: getInputElement() → focus → clipboard ──
    log.info("  전략 3: getInputElement()")
    try:
        input_info = driver.execute_script("""
            var editor = window.__seEditor;
            if (!editor || !editor._virtualEditable) return 'VE_NOT_FOUND';
            try {
                // 먼저 focus 활성화
                editor._virtualEditable.focus();
            } catch(e) {}

            try {
                var inputEl = editor._virtualEditable.getInputElement();
                if (!inputEl) return 'INPUT_NULL';
                window.__seInputEl = inputEl;

                // getInputTarget도 시도
                var inputTarget = null;
                try { inputTarget = editor._virtualEditable.getInputTarget(); }
                catch(e) {}

                return {
                    tag: inputEl.tagName,
                    id: inputEl.id || '',
                    ce: inputEl.getAttribute('contenteditable'),
                    w: inputEl.offsetWidth, h: inputEl.offsetHeight,
                    cls: (inputEl.className||'').substring(0,50),
                    inIframe: inputEl.ownerDocument !== document,
                    targetTag: inputTarget ? inputTarget.tagName : 'null'
                };
            } catch(e) {
                return 'GET_ERR: ' + e.message;
            }
        """)
        log.info(f"  [getInputElement] {input_info}")

        if isinstance(input_info, dict):
            # 반환된 input element에 Selenium으로 입력
            input_typed = driver.execute_script("""
                var el = window.__seInputEl;
                if (!el) return 'NO_EL';
                // visible + focusable하게 만들기
                el.style.cssText += '; position:fixed !important; left:0 !important; ' +
                    'top:0 !important; width:400px !important; height:50px !important; ' +
                    'z-index:99999 !important; opacity:1 !important; display:block !important;';
                el.focus();
                return 'READY: ae=' + document.activeElement.tagName +
                       '.' + (document.activeElement.className||'').substring(0,30) +
                       ' id=' + (document.activeElement.id||'');
            """)
            log.info(f"  [inputEl ready] {input_typed}")
            time.sleep(0.3)

            # inputEl이 iframe 내부인 경우
            if input_info.get('inIframe'):
                # iframe 안의 요소이므로 iframe으로 전환 필요
                iframe_id = driver.execute_script("""
                    var el = window.__seInputEl;
                    var doc = el.ownerDocument;
                    var iframes = document.querySelectorAll('iframe');
                    for (var i = 0; i < iframes.length; i++) {
                        try {
                            if (iframes[i].contentDocument === doc) return iframes[i].id;
                        } catch(e) {}
                    }
                    return null;
                """)
                log.info(f"  [inputEl iframe] {iframe_id}")
                if iframe_id:
                    driver.switch_to.frame(iframe_id)
                    time.sleep(0.2)
                    try:
                        body = driver.find_element(By.TAG_NAME, "body")
                        body.click()
                        time.sleep(0.2)
                        pyperclip.copy(title)
                        ActionChains(driver).key_down(Keys.CONTROL).send_keys('v')\
                            .key_up(Keys.CONTROL).perform()
                        time.sleep(1)
                    except (WebDriverException, OSError) as e:
                        log.info(f"  iframe 내 입력 실패: {e}")
                    driver.switch_to.default_content()
                    time.sleep(0.3)
            else:
                # 같은 document의 요소 — 직접 입력
                try:
                    actual_el = driver.execute_script("return window.__seInputEl;")
                    if actual_el:
                        actual_el.click()
                        time.sleep(0.2)
                        pyperclip.copy(title)
                        ActionChains(driver).key_down(Keys.CONTROL).send_keys('v')\
                            .key_up(Keys.CONTROL).perform()
                        time.sleep(1)
                except (WebDriverException, OSError) as e:
                    log.info(f"  inputEl 직접 입력 실패: {e}")

            time.sleep(0.5)
            if _verify_title():
                log.info("  ✅ 제목 입력 완료 (getInputElement)")
                return True
    except (WebDriverException, OSError) as e:
        log.info(f"  getInputElement 실패: {e}")
        try:
            driver.switch_to.default_content()
        except WebDriverException:
            log.debug("default_content 전환 실패")

    # ── 전략 4: _editingService.write() (포커스가 제목에 있어야 함) ──
    log.info("  전략 4: editingService.write()")
    try:
        write_result = driver.execute_script("""
            var editor = window.__seEditor;
            if (!editor) return 'NO_EDITOR';
            var tried = [];

            // 먼저 VE focus
            if (editor._virtualEditable) {
                try { editor._virtualEditable.focus(); } catch(e) {}
            }

            // editingService.write
            if (editor._editingService) {
                var es = editor._editingService;
                try { es.write(arguments[0]); tried.push('write:OK'); }
                catch(e) { tried.push('write:ERR:' + e.message.substring(0,40)); }
            }

            return tried.join(', ') || 'NO_METHOD';
        """, title)
        log.info(f"  [editingService.write] {write_result}")
        time.sleep(1)
        if _verify_title():
            log.info("  ✅ 제목 입력 완료 (editingService.write)")
            return True
    except WebDriverException as e:
        log.info(f"  editingService.write 실패: {e}")

    # ── 전략 5: DOM 직접 수정 (최후의 수단) ──
    log.info("  전략 5: DOM 직접 수정 (내부 상태 미반영 가능)")
    try:
        dom_result = driver.execute_script("""
            var titleDiv = document.querySelector('.se-title-text');
            if (!titleDiv) return 'TITLE_DIV_NOT_FOUND';
            var p = titleDiv.querySelector('.se-text-paragraph') || titleDiv.querySelector('p');
            if (!p) return 'P_NOT_FOUND';

            // 플레이스홀더 SPAN 완전 제거
            var placeholders = titleDiv.querySelectorAll('.se-placeholder');
            for (var i = 0; i < placeholders.length; i++) {
                placeholders[i].parentNode.removeChild(placeholders[i]);
            }

            // P 요소 내용 완전 초기화 후 새 SPAN 생성
            p.innerHTML = '';
            var span = document.createElement('span');
            span.className = 'se-fs- se-ff-';
            span.textContent = arguments[0];
            p.appendChild(span);

            // se-is-empty 클래스 제거
            var el = titleDiv;
            while (el) {
                el.classList.remove('se-is-empty');
                if (el.classList.contains('se-section-documentTitle') ||
                    el.tagName === 'BODY') break;
                el = el.parentElement;
            }

            return 'DOM_MODIFIED: ' + p.textContent.substring(0, 40);
        """, title)
        log.info(f"  [DOM 수정] {dom_result}")
        time.sleep(1)
        if _verify_title():
            log.info("  ✅ 제목 입력 완료 (DOM 직접 수정)")
            return True
    except WebDriverException as e:
        log.info(f"  DOM 수정 실패: {e}")

    # ── 최종 JS 검증: 모든 전략 실패 후 실제 DOM 상태 확인 ──
    try:
        actual = driver.execute_script(
            "var el = document.querySelector('.se-title-text, .se-ff-nanumgothic, [class*=\"title\"] [contenteditable]'); "
            "return el ? el.textContent.trim() : '';"
        )
        if title[:10] in actual:
            log.info("제목 입력 검증 성공 (최종 JS 확인)")
            return True
        else:
            log.warning(f"제목 입력 검증 실패: 기대='{title[:20]}...', 실제='{actual[:20]}...'")
    except WebDriverException:
        log.debug("제목 검증 JS 실행 실패", exc_info=True)

    log.warning("  ❌ 제목 입력 실패 (모든 전략)")
    return False


# ──────────────────────────────────────────────────
# 📝 본문 영역 클릭 (포커스 확보)
# ──────────────────────────────────────────────────
def _click_body_area(driver):
    """제목 영역 밖의 본문 paragraph를 찾아서 클릭"""
    try:
        els = driver.find_elements(By.CSS_SELECTOR, ".se-text-paragraph")
        for el in els:
            in_title = driver.execute_script("""
                return !!arguments[0].closest('.se-documentTitle')
                    || !!arguments[0].closest('.se-section-documentTitle')
                    || !!arguments[0].closest('.se-title-text');
            """, el)
            if in_title:
                continue
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
            el.click()
            time.sleep(0.5)
            return True
    except (NoSuchElementException, ElementNotInteractableException):
        pass
    log.debug("  본문 영역 직접 클릭 실패 — 계속 진행")
    return False


# ──────────────────────────────────────────────────
# 🚀 즉시발행
# ──────────────────────────────────────────────────
def _publish_now_ex(driver, wait, tags: list = None,
                    category_label: str = None, fallback=None) -> dict:
    """
    즉시발행 — dict 반환 버전 (2026-09-18). 발행 버튼 클릭 → 발행 패널이 열린 직후
    카테고리/태그 입력 → 확인 버튼 클릭.
    tags: 발행 패널 내 태그 입력 영역에 넣을 태그 리스트 (None 또는 [] 허용)
    category_label/fallback: 발행 패널 열린 직후 카테고리 설정용 (2026-05-21 처방).

    Returns:
        {"ok": bool, "button_clicked": bool, "signal": str, "evidence": dict|None}
    """
    confirm_clicked = False
    try:
        pre_url = driver.current_url

        # 발행(설정 패널을 여는) 버튼 클릭 + 패널 열림 검증 — 예약발행과 동일 헬퍼.
        # 과거 버그(2026-07-09 버그①): 낡은 CSS(.publish_btn 정확일치)가 해시 클래스를
        # 못 잡고 폴백 XPATH `contains(.,'발행')` 가 "예약 발행 글" 버튼을 오클릭 →
        # 예약글 목록 모달이 열려 카테고리/태그/확인을 못 찾던 오발행 경로를 차단(fail-closed).
        if not _open_publish_panel(driver):
            log.error("  ❌ 발행 설정 패널을 열지 못함 — 발행 중단(fail-closed)")
            return {"ok": False, "button_clicked": False, "signal": "none", "evidence": None}

        # 발행 패널이 열린 직후에만 카테고리 dropdown 옵션 DOM이 존재한다 (2026-05-21 처방).
        # 카테고리는 시각만큼 치명적이지 않으므로 fail-open: 미확정이어도 경고만 남기고 계속.
        if not _set_category(driver, category_label=category_label, fallback=fallback):
            log.warning(
                "  ⚠️ 카테고리 미확정 — 기본 메뉴로 발행될 수 있음 (셀렉터 점검 필요)"
            )

        # 발행 패널이 열린 직후에만 태그 입력 영역 DOM이 존재한다 (2026-05-09 처방).
        # 확인 버튼을 누르기 전에 태그를 넣어야 한다.
        _input_tags(driver, tags or [])

        # 확인(발행 확정) 버튼 클릭 — 발행 설정 패널 하단.
        for sel in ["button[class*='confirm_btn']",
                    ".confirm-btn", ".btn_ok", "button.se-btn-confirm",
                    ".se-popup-button-confirm", ".se-publish-btn-confirm",
                    ".se-publish-confirm"]:
            try:
                confirm = driver.find_element(By.CSS_SELECTOR, sel)
                if confirm.is_displayed():
                    try:
                        confirm.click()
                    except (ElementNotInteractableException,
                            ElementClickInterceptedException):
                        driver.execute_script("arguments[0].click();", confirm)
                    confirm_clicked = True
                    time.sleep(3); break
            except (NoSuchElementException, ElementNotInteractableException):
                continue
        # JS 폴백: 패널 하단의 정확히 "발행"인 버튼 (상단 publish_btn 은 제외 — 오클릭 방지).
        # `contains(.,'발행')` XPATH 는 상단 발행 버튼/예약 발행 글을 오클릭하므로 쓰지 않는다.
        if not confirm_clicked:
            try:
                btn = driver.execute_script("""
                    var btns = document.querySelectorAll('button');
                    for (var b of btns) {
                        var t = (b.textContent || '').trim();
                        var cls = (b.className || '');
                        if (t === '발행' && b.offsetParent && cls.indexOf('publish_btn') < 0)
                            return b;
                    }
                    return null;
                """)
                if btn:
                    driver.execute_script("arguments[0].click();", btn)
                    confirm_clicked = True
                    time.sleep(3)
                    log.info("    ✅ 발행 확인 버튼 클릭 (JS)")
            except WebDriverException:
                pass

        # 발행 후 검증: URL 변경 또는 성공 메시지 확인
        ok, signal, evidence = _verify_publish_signal(driver, pre_url, confirm_clicked)
        return {"ok": ok, "button_clicked": confirm_clicked, "signal": signal, "evidence": evidence}

    except (WebDriverException, ValueError, OSError) as e:
        log.error(f"  ❌ 발행 실패: {type(e).__name__}: {str(e)[:100]}")
        return {"ok": False, "button_clicked": confirm_clicked, "signal": "none", "evidence": None}


def _publish_now(driver, wait, tags: list = None,
                 category_label: str = None, fallback=None) -> bool:
    """즉시발행. (``_publish_now_ex`` 의 bool 래퍼)"""
    result = _publish_now_ex(driver, wait, tags=tags,
                             category_label=category_label, fallback=fallback)
    return result["ok"]


def _verify_publish_signal(driver, pre_url: str, button_clicked: bool,
                           is_reservation: bool = False) -> tuple:
    """
    발행/예약 후 실제로 성공했는지 검증하고, 어떤 신호로 판정했는지(signal)와
    예약 성공의 근거(evidence)를 함께 돌려준다 (2026-09-18, 계약 §2.7).

    bool 판정 로직은 ``_verify_publish_success`` 와 완전히 동일하다 — 이 함수는
    그 판정에 신호·근거를 얹어 돌려주는 확장일 뿐, 어떤 분기도 새로 만들지 않는다.

    Args:
        is_reservation: 예약발행 경로 여부.
            예약발행은 성공해도 에디터 URL이 그대로인 것이 정상이므로, URL 변경을 성공
            신호로 쓰면 안 된다(즉시발행 경로에서만 유효). 예약 경로에서는 "예약되었습니다"류
            성공 토스트/텍스트 존재를 우선 신호로 삼고, 없으면 실패로 판정한다(fail-closed).
            즉시발행 경로는 기존 URL 기반 판정을 유지한다.

    Returns:
        (ok: bool, signal: str, evidence: dict|None)
        signal ∈ {"reservation_id","reservation_list","success_response",
                  "url_changed","toast","none","error_text"}.
        evidence 는 ``reserved`` 판정의 유일한 근거(§2.7) — 예약 경로에서 명확한
        예약 성공 메시지를 읽었을 때만 채워진다. 단순 URL 변경만으로는 evidence=None
        (허위 성공 금지, §11).
    """
    time.sleep(2)

    # 확인 버튼을 못 눌렀으면 발행 자체가 시도되지 않은 것 → 즉시 실패 (경로 공통)
    if not button_clicked:
        log.error("  ❌ 발행 실패 — 확인 버튼을 찾지 못함")
        return False, "none", None

    if is_reservation:
        return _verify_reservation_success_ex(driver, pre_url)

    return _verify_immediate_success_ex(driver, pre_url)


def _verify_publish_success(driver, pre_url: str, button_clicked: bool,
                            is_reservation: bool = False) -> bool:
    """발행/예약 후 실제로 성공했는지 검증한다. (``_verify_publish_signal`` 의 bool 래퍼)"""
    ok, _signal, _evidence = _verify_publish_signal(
        driver, pre_url, button_clicked, is_reservation=is_reservation)
    return ok


# 예약 성공 메시지 중 "예약"이 명시적으로 들어간 것만 reservation_evidence 를 채운다.
# 그 외(발행되었습니다/등록되었습니다/저장되었습니다)는 예약 경로에서도 관측되는 일반
# 토스트라 예약을 명확히 확정하는 근거로 보기엔 약하다 — signal="toast", evidence=None.
_RESERVATION_SPECIFIC_KEYWORDS = ("예약되었습니다", "예약 발행", "예약이 완료")


def _verify_reservation_success_ex(driver, pre_url: str) -> tuple:
    """예약발행 성공 판정 — signal·evidence 를 함께 돌려주는 버전.

    이 환경에서는 예약 확정 시 에디터(postwrite)에서 벗어나 PostList 로 리다이렉트되는
    것이 관측된 정상 성공 신호다(2026-06-29 로그: 20/20 예약이 'URL 변경 확인'으로 성공).
    따라서 (1) 에러 토스트가 있으면 실패, (2) URL 이 postwrite 를 벗어났거나 성공 토스트가
    있으면 성공, (3) 둘 다 아니면 미확정(fail-closed) 으로 판정한다.

    ⚠️ bool 판정(True/False)과 분기 순서는 예전 ``_verify_reservation_success`` 와
    완전히 동일하다 — signal/evidence 태깅만 추가했다.

    Returns:
        (ok, signal, evidence)
    """
    try:
        page_text = driver.execute_script(
            "return document.body ? document.body.innerText.substring(0, 2000) : ''")
    except WebDriverException as e:
        log.debug(f"예약 결과 텍스트 확인 실패: {e}")
        page_text = ""

    error_keywords = ["제목을 입력", "내용을 입력", "제목이 비어", "발행할 수 없"]
    for kw in error_keywords:
        if kw in page_text:
            log.error(f"  ❌ 예약 실패 — 에러 메시지 감지: '{kw}'")
            return False, "error_text", None

    # 주 신호: 에디터(postwrite)에서 벗어난 리다이렉트 = 예약 확정 성공 (관측된 정상 동작).
    # ⚠️ URL 변경만으로는 reserved 의 근거로 삼지 않는다(허위 성공 금지) — evidence=None.
    try:
        cur_url = driver.current_url
        if "postwrite" not in cur_url and cur_url != pre_url:
            log.info(f"  ✅ 예약 완료 (URL 변경 확인: {cur_url[:60]})")
            return True, "url_changed", None
    except WebDriverException as e:
        log.debug(f"예약 URL 확인 실패: {e}")

    # 보조 신호: 성공 토스트/텍스트.
    success_keywords = ["예약되었습니다", "예약 발행", "예약이 완료",
                        "발행되었습니다", "발행 완료", "등록되었습니다", "저장되었습니다"]
    for kw in success_keywords:
        if kw in page_text:
            log.info(f"  ✅ 예약 완료 (성공 메시지: '{kw}')")
            if kw in _RESERVATION_SPECIFIC_KEYWORDS:
                from datetime import datetime as _dt
                evidence = {"signal": "success_response", "detail": kw,
                            "observed_at": _dt.now().isoformat()}
                return True, "success_response", evidence
            return True, "toast", None

    # URL 변경도 성공 토스트도 없으면 성공을 확신할 수 없다 → 미확정(재시도 대상).
    log.warning("  ⛔ 예약 성공 신호(URL 변경/성공 메시지) 없음 — 미확정으로 판정(재시도 대상)")
    return False, "none", None


def _verify_reservation_success(driver, pre_url: str) -> bool:
    """예약발행 성공 판정. (``_verify_reservation_success_ex`` 의 bool 래퍼)"""
    ok, _signal, _evidence = _verify_reservation_success_ex(driver, pre_url)
    return ok


def _verify_immediate_success_ex(driver, pre_url: str) -> tuple:
    """즉시발행 성공 판정 — signal·evidence 를 함께 돌려주는 버전.
    bool 판정(True/False)과 분기 순서는 예전 ``_verify_immediate_success`` 와 완전히
    동일한 URL 기반 로직이다. 즉시발행 경로에는 '예약' 개념이 없으므로 evidence 는
    항상 None(reservation_evidence 는 예약 경로 전용, §2.7).

    Returns:
        (ok, signal, evidence)
    """
    cur_url = driver.current_url

    # 1) URL이 변경되었으면 (postwrite → 다른 페이지) 성공
    if "postwrite" not in cur_url and cur_url != pre_url:
        log.info(f"  ✅ 발행 완료 (URL 변경 확인: {cur_url[:60]})")
        return True, "url_changed", None

    # 2) 페이지에 성공/에러 메시지 확인
    try:
        page_text = driver.execute_script(
            "return document.body ? document.body.innerText.substring(0, 2000) : ''")
        # 에러 메시지 감지
        error_keywords = ["제목을 입력", "내용을 입력", "제목이 비어", "발행할 수 없"]
        for kw in error_keywords:
            if kw in page_text:
                log.error(f"  ❌ 발행 실패 — 에러 메시지 감지: '{kw}'")
                return False, "error_text", None
        # 성공 메시지 감지
        success_keywords = ["발행되었습니다", "등록되었습니다", "예약되었습니다",
                           "발행 완료", "저장되었습니다"]
        for kw in success_keywords:
            if kw in page_text:
                log.info(f"  ✅ 발행 완료 (성공 메시지: '{kw}')")
                return True, "toast", None
    except WebDriverException as e:
        log.debug(f"페이지 텍스트 확인 실패: {e}")

    # 3) URL이 여전히 postwrite이면 — 성공 메시지 재확인
    if "postwrite" in cur_url:
        try:
            page_text = driver.execute_script("return document.body ? document.body.innerText : '';")
            success_keywords = ["예약되었습니다", "예약 발행", "발행되었습니다", "발행 완료", "저장되었습니다"]
            for kw in success_keywords:
                if kw in page_text:
                    log.info(f"  ✅ 발행 성공 메시지 감지: '{kw}'")
                    return True, "toast", None
        except WebDriverException:
            log.debug("발행 확인 JS 실행 실패", exc_info=True)
        log.warning("  ⚠️  발행 후에도 에디터 페이지에 남아있음 — 발행 실패 가능성")
        return False, "none", None

    # 4) URL도 변경되지 않고 메시지도 없으면 불확실 → 실패로 처리
    log.warning("  ⚠️  발행 결과 확인 불가 — 실패로 처리")
    return False, "none", None


def _verify_immediate_success(driver, pre_url: str) -> bool:
    """즉시발행 성공 판정 — 기존 URL 기반 로직 유지. (``_verify_immediate_success_ex`` 의 bool 래퍼)"""
    ok, _signal, _evidence = _verify_immediate_success_ex(driver, pre_url)
    return ok


# ──────────────────────────────────────────────────
# 🖼️ 이미지 삽입
# ──────────────────────────────────────────────────
# 라이브 검증(2026-08-31, 비발행 조사): 네이버 SE 이미지 input 은 1회용 — 한 장 업로드 후
# DOM 에서 제거된다. 그래서 다중 이미지는 "장마다 사진 버튼을 다시 눌러 fresh input 을
# 확보 → 그 input 에 send_keys → 등록까지 폴링" 을 반복해야 4장 전부 등록된다.
# (send_keys 다중 join 은 0장으로 실측 실패 → 절대 재도입 금지.)
_IMG_POLL_TIMEOUT = 15.0     # 장당 등록 최대 대기(초)
_IMG_POLL_INTERVAL = 0.5     # 폴링 간격(초)
_IMG_CONFIRM_SELECTORS = [
    ".btn_ok", ".se-confirm-btn", "button.confirm",
    ".se-popup-button-confirm",
    "[class*='upload'] button", "[class*='confirm'] button",
]


# 업로드 확인 다이얼로그를 우선 탐색할 SE 업로드 팝업 컨테이너(전역 오클릭 축소용).
_IMG_CONFIRM_SCOPES = [
    ".se-popup-dialog", ".se-popup", ".se-image-upload",
    ".se-toolbar-upload", "[class*='se-popup']",
]


def _count_image_components_src(driver):
    """이미지 컴포넌트 수와 그 출처를 함께 반환한다.

    매 호출마다 ``window.__seEditor`` 가 없으면 SE.launcher 에서 인라인 재확보(H2 —
    에디터가 늦게 준비돼도 카운트가 살아나게, idempotent)한 뒤 문서 컴포넌트를 센다.

    Returns:
        (count, src) — src 는 'api'(에디터 문서), 'dom'(DOM 폴백), 'none'(둘 다 실패=-1).
    """
    try:
        res = driver.execute_script("""
            // __seEditor 인라인 재확보(_ensure_se_editor 와 동일 로직, 매 호출 idempotent)
            try {
                if (!(window.__seEditor && window.__seEditor._documentService)) {
                    var se = window.SE || window.se;
                    if (se && se.launcher) {
                        var editor = null;
                        if (se.launcher._editors) {
                            var keys = Object.keys(se.launcher._editors);
                            for (var k = 0; k < keys.length; k++) {
                                var e = se.launcher._editors[keys[k]];
                                if (e && e._documentService) { editor = e; break; }
                            }
                            if (!editor && keys.length) editor = se.launcher._editors[keys[0]];
                        }
                        if (!editor) { try { editor = se.launcher.getEditor('blogpc001'); } catch(e){} }
                        if (!editor) { try { editor = se.launcher.getEditor(); } catch(e){} }
                        if (editor) window.__seEditor = editor;
                    }
                }
                var ed = window.__seEditor;
                if (ed && ed._documentService && ed._documentService.getDocumentData) {
                    var dd = ed._documentService.getDocumentData();
                    var doc = dd && (dd.document || dd);
                    var comps = (doc && doc.components) || [];
                    var c = 0;
                    for (var i = 0; i < comps.length; i++) {
                        if ((comps[i]['@ctype'] || '') === 'image') c++;
                    }
                    return [c, 'api'];
                }
            } catch (e) {}
            try { return [document.querySelectorAll('.se-image-resource').length, 'dom']; } catch (e2) {}
            return [-1, 'none'];
        """)
        if isinstance(res, (list, tuple)) and len(res) == 2:
            try:
                return int(res[0]), str(res[1])
            except (TypeError, ValueError):
                return -1, "none"
        # 하위호환(단순 int 반환하는 fake/구환경)
        if isinstance(res, (int, float)):
            return int(res), "api"
        return -1, "none"
    except WebDriverException:
        return -1, "none"


def _count_image_components(driver) -> int:
    """에디터 문서의 이미지 컴포넌트 수(@ctype==='image'). 실패 시 DOM 폴백, 그래도 실패면 -1."""
    return _count_image_components_src(driver)[0]


def _click_image_confirm(driver) -> bool:
    """업로드 확인 다이얼로그가 떠 있으면 확인 버튼을 클릭한다(단일 JS 왕복).

    SE 업로드 팝업 컨테이너 안을 먼저 찾고(전역 오클릭 축소), 없으면 문서 전역으로 폴백한다.
    실제로 누른 엘리먼트(클래스/텍스트)를 debug 로 남긴다.
    """
    try:
        clicked = driver.execute_script("""
            var sels = arguments[0], scopes = arguments[1];
            function tryClick(root) {
                for (var i = 0; i < sels.length; i++) {
                    var el = root.querySelector(sels[i]);
                    if (el && el.offsetParent !== null) {
                        el.click();
                        return ((el.className || '') + '|' +
                                (el.textContent || '').trim().substring(0, 20));
                    }
                }
                return null;
            }
            for (var s = 0; s < scopes.length; s++) {
                var c = document.querySelector(scopes[s]);
                if (c) { var r = tryClick(c); if (r) return r; }
            }
            return tryClick(document);
        """, _IMG_CONFIRM_SELECTORS, _IMG_CONFIRM_SCOPES)
        if clicked:
            log.debug(f"  이미지 확인 다이얼로그 클릭: {clicked}")
            return True
        return False
    except WebDriverException:
        return False


def _insert_images(driver, wait, paths: list):
    valid_paths = [os.path.abspath(p) for p in paths if os.path.exists(p)]
    if not valid_paths:
        log.warning("  ⚠️  유효한 이미지 파일 없음")
        return False
    try:
        # 순서 의존성: 이 함수는 _do_upload 에서 _paste_html(본문 setDocumentData)·예약설정보다
        # ★먼저★ 호출된다 — 문서가 비어 있어 이미지 외 컴포넌트가 없고 폴링 부담이 작다.
        # 추후 이미지·본문을 인터리브(본문 채운 뒤 삽입)로 통합한다면 컴포넌트 카운트가
        # 무거워져 폴링이 느려질 수 있으니 이 전제를 재검토할 것.
        _ensure_se_editor(driver)   # 컴포넌트 카운트용(idempotent)
        total = len(valid_paths)
        start = _count_image_components(driver)
        base = start if start >= 0 else 0

        # ── 장마다 사진 버튼 재클릭 → 가로챈 그 1회용 input 에 send_keys → 등록 폴링 ──
        # (대표 이미지 = valid_paths[0](썸네일)이 첫 삽입 → 첫 이미지 컴포넌트, 순서 보존.)
        for i, p in enumerate(valid_paths, start=1):
            file_input = _create_file_input_via_intercept(driver)
            captured = file_input is not None
            if file_input is None and i == 1:
                # 첫 장은 기존 노출 input 이 있을 수 있어 폴백 1회 허용(첫 장 한정).
                file_input = _find_and_expose_file_input(driver)
            if file_input is None:
                log.warning(f"  ⚠️  IMG [{i}/{total}] 파일 input 확보 실패 — 남은 이미지 중단")
                break

            before = _count_image_components(driver)
            try:
                file_input.send_keys(p)
            except (WebDriverException, OSError) as e:
                log.warning(f"  ⚠️  IMG [{i}/{total}] send_keys 실패: {type(e).__name__} — 중단")
                break

            # 등록 폴링 — 이미지 컴포넌트 수가 늘 때까지(틱마다 확인 다이얼로그 처리).
            # 에디터 API 카운트(src='api')는 즉시 신뢰, DOM 폴백(src='dom')은 이중 렌더로
            # 튈 수 있어 연속 2틱 유지될 때만 성공으로 판정(디바운스).
            if before < 0:
                time.sleep(2)               # 카운트 불가 — 고정 대기 폴백
                _click_image_confirm(driver)
                imgs = -1
                registered_this = True
            else:
                deadline = time.time() + _IMG_POLL_TIMEOUT
                imgs = before
                stable = 0
                registered_this = False
                while time.time() < deadline:
                    _click_image_confirm(driver)
                    imgs, src = _count_image_components_src(driver)
                    if imgs > before:
                        if src != "dom":
                            registered_this = True
                            break
                        stable += 1                 # DOM 폴백 — 디바운스
                        if stable >= 2:
                            registered_this = True
                            break
                    else:
                        stable = 0
                    time.sleep(_IMG_POLL_INTERVAL)

            log.info(f"  🖼  IMG [{i}/{total}] 버튼={'캡처' if captured else '폴백'} → 등록 imgs={imgs}")
            if not registered_this:
                log.warning(f"  ⚠️  IMG [{i}/{total}] 등록 미확인 — 남은 이미지 중단(fail-open)")
                break
            # ★다음 장 사진 버튼을 누르기 전에 업로드 패널/다이얼로그를 닫고 에디터를 안정화한다.
            # (비발행 조사 재현: 이 settle 없이 곧바로 다음 버튼을 누르면 2번째 intercept 캡처가
            #  실패해 1장만 들어갔다 — 실측 2026-08-31.)
            if i < total:
                _click_image_confirm(driver)
                time.sleep(1.2)

        # ── 최종 확인 다이얼로그 잔여 처리(발행 단계 방해 방지) + 검증 ──
        if _click_image_confirm(driver):
            time.sleep(1)
        final = _count_image_components(driver)
        registered = (final - base) if final >= 0 else total
        log.info(f"  ✅ 이미지 {total}장 중 {max(0, registered)}장 삽입 (최종 컴포넌트={final})")

        # DOM 기반 검증(기존 로그 키워드 유지).
        try:
            img_count = driver.execute_script(
                "return document.querySelectorAll('.se-image-resource, .se-component-image img, img[src*=\"post\"]').length;"
            )
            if img_count and img_count > 0:
                log.info(f"이미지 삽입 검증 성공 ({img_count}개)")
                return True
            log.warning("이미지 삽입 검증 실패: 삽입된 이미지 없음")
            return False
        except WebDriverException:
            log.debug("이미지 검증 JS 실행 실패", exc_info=True)
            return (registered > 0) if final >= 0 else True
    except (WebDriverException, OSError) as e:
        log.warning(f"  ⚠️  이미지 삽입 실패: {type(e).__name__}: {str(e)[:80]}")
        return False


def _find_and_expose_file_input(driver):
    """DOM에 이미 존재하는 input[type=file]을 찾아서 노출시킨다."""
    count = driver.execute_script("""
        var inputs = document.querySelectorAll('input[type="file"]');
        for (var i = 0; i < inputs.length; i++) {
            inputs[i].style.cssText =
                'display:block !important; visibility:visible !important; ' +
                'opacity:1 !important; position:absolute !important; ' +
                'left:0 !important; top:0 !important; width:200px !important; ' +
                'height:30px !important; z-index:99999 !important;';
        }
        return inputs.length;
    """)
    if count and count > 0:
        time.sleep(0.3)
        for fsel in ["input[type='file']", "input[accept*='image']"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, fsel)
                log.debug(f"  기존 파일 input 발견: {fsel} ({count}개)")
                return el
            except NoSuchElementException:
                continue
    return None


def _create_file_input_via_intercept(driver):
    """
    이미지 버튼 클릭 전에 HTMLInputElement.prototype.click을 가로채서
    OS 파일 대화상자가 열리지 않도록 한 뒤, 이미지 버튼을 클릭하여
    에디터가 내부적으로 생성하는 file input을 캡처한다.
    """
    log.debug("  파일 input 없음 — click 가로채기로 생성 유도")

    # 1) input[type=file].click() 가로채기 설치
    driver.execute_script("""
        window.__seInterceptedFileInput = null;
        window.__seOriginalInputClick = HTMLInputElement.prototype.click;
        HTMLInputElement.prototype.click = function() {
            if (this.type === 'file') {
                window.__seInterceptedFileInput = this;
                // OS 대화상자를 열지 않음
                return;
            }
            return window.__seOriginalInputClick.call(this);
        };
    """)

    # 2) 이미지 툴바 버튼 클릭 (에디터가 내부적으로 file input 생성 + click 시도)
    clicked_sel = driver.execute_script("""
        var selectors = [
            '.se-toolbar button[data-type="image"]',
            'button[data-name="image"]', 'button[data-log="SE3.IMAGE"]',
            '.se-toolbar-item-image', '.se-image-toolbar-button',
            '.tool_photo', 'button[title="사진"]',
            '[class*="image"] button', '[class*="photo"] button'
        ];
        for (var s of selectors) {
            var btn = document.querySelector(s);
            if (btn) {
                btn.click();
                return s;
            }
        }
        return null;
    """)
    log.debug(f"  이미지 버튼 클릭: {clicked_sel}")
    time.sleep(2)

    # 3) 가로채기 해제 + 가로챈 '그' input 을 고유 마커로 정확히 반환
    #    (기존엔 find_element("input[type=file]") 로 첫 input 을 잡아, 여러 input 이 있으면
    #     가로챈 실제 input 이 아닌 엉뚱한 걸 반환하던 라이브 확정 버그 → 마커로 특정.)
    intercepted = driver.execute_script("""
        HTMLInputElement.prototype.click = window.__seOriginalInputClick;
        // 이전 호출 마커 잔재 제거(다음 호출과 안 섞이게)
        var prev = document.querySelectorAll("input[data-se-fresh='1']");
        for (var j = 0; j < prev.length; j++) prev[j].removeAttribute('data-se-fresh');
        var el = window.__seInterceptedFileInput;
        if (el) {
            el.setAttribute('data-se-fresh', '1');
            el.style.cssText =
                'display:block !important; visibility:visible !important; ' +
                'opacity:1 !important; position:absolute !important; ' +
                'left:0 !important; top:0 !important; width:200px !important; ' +
                'height:30px !important; z-index:99999 !important;';
            return true;
        }
        return false;
    """)

    if intercepted:
        time.sleep(0.3)
        try:
            el = driver.find_element(By.CSS_SELECTOR, "input[data-se-fresh='1']")
            log.debug("  가로채기로 파일 input 캡처 성공(마커)")
            return el
        except NoSuchElementException:
            log.debug("  가로챈 input 마커 조회 실패")

    # 가로채기 실패 시 — 이미지 버튼이 OS dialog을 직접 열었을 수 있어 ESC 로 닫기 시도.
    log.debug("  가로채기 실패 — ESC 키로 OS 대화상자 닫기 시도")
    try:
        ActionChains(driver).send_keys(Keys.ESCAPE).perform()
        time.sleep(1)
    except WebDriverException:
        log.debug("ESC 키 전송 실패")

    # ★H1: 마커 캡처 실패 시 None 반환 — find_element("input[type=file]") 첫-input 폴백을
    #  ★쓰지 않는다★. 그 폴백은 우리가 고친 오탐 경로(2번째 이후 이미지에서 1번째의 소모된
    #  input 을 집을 수 있음)의 재현이다. 첫 장 폴백은 호출부(_insert_images)가 i==1 에서만
    #  _find_and_expose_file_input 으로 명시 처리하므로 여기 내부 폴백은 중복이자 유해.
    return None


def _create_file_input_manually(driver):
    """
    최후의 수단: JS로 file input을 직접 생성하고,
    change 이벤트를 통해 에디터에 이미지를 주입한다.
    """
    log.debug("  최후의 수단 — JS로 file input 직접 생성")
    driver.execute_script("""
        var input = document.createElement('input');
        input.type = 'file';
        input.accept = 'image/*';
        input.multiple = true;
        input.id = '__se_manual_file_input';
        input.style.cssText =
            'display:block !important; visibility:visible !important; ' +
            'opacity:1 !important; position:absolute !important; ' +
            'left:0 !important; top:0 !important; width:200px !important; ' +
            'height:30px !important; z-index:99999 !important;';
        document.body.appendChild(input);
    """)
    time.sleep(0.5)
    try:
        return driver.find_element(By.CSS_SELECTOR, "#__se_manual_file_input")
    except NoSuchElementException:
        return None


# ──────────────────────────────────────────────────
# 📋 HTML 붙여넣기
# ──────────────────────────────────────────────────
def _paste_html(driver, html: str, title: str = ""):
    """본문 HTML 입력 + 제목 설정 — setDocumentData()로 한번에 처리.
    title이 주어지면 documentTitle 컴포넌트에 제목도 함께 설정한다.
    주의: .se-component-content .se-text-paragraph 는 제목의 <P>도 매칭하므로
    반드시 제목 영역(.se-documentTitle, .se-title-text) 밖의 요소를 찾아야 한다."""

    # __seEditor가 없으면 획득 시도
    _ensure_se_editor(driver)

    def _is_in_title(el):
        """요소가 제목 영역 내부인지 확인"""
        return driver.execute_script("""
            return !!arguments[0].closest('.se-documentTitle')
                || !!arguments[0].closest('.se-section-documentTitle')
                || !!arguments[0].closest('.se-title-text');
        """, el)

    def _verify_body():
        """제목 영역 밖의 텍스트 길이 확인 — 플레이스홀더 제외"""
        result = driver.execute_script("""
            var placeholders = ['글감과 함께 나의 일상을 기록해보세요!',
                                '본문을 입력하세요', '내용을 입력하세요'];
            var els = document.querySelectorAll('.se-text-paragraph');
            var bodyTexts = [];
            var totalLen = 0;
            for (var i = 0; i < els.length; i++) {
                var el = els[i];
                if (el.closest('.se-documentTitle')
                 || el.closest('.se-section-documentTitle')
                 || el.closest('.se-title-text')) continue;
                var t = (el.textContent || '').trim();
                // 플레이스홀더 텍스트 제외
                var isPlaceholder = false;
                for (var p of placeholders) {
                    if (t === p) { isPlaceholder = true; break; }
                }
                // se-placeholder 클래스를 가진 요소만 있는 경우도 제외
                if (el.querySelector('.se-placeholder') && !el.querySelector('span:not(.se-placeholder)')) {
                    isPlaceholder = true;
                }
                if (isPlaceholder) continue;
                totalLen += t.length;
                if (t.length > 0) bodyTexts.push(t.substring(0, 40));
            }
            // se-is-empty 체크
            var bodyComponent = document.querySelector('.se-component:not(.se-documentTitle):not(.se-section-documentTitle)');
            var bodyEmpty = bodyComponent ? bodyComponent.classList.contains('se-is-empty') : true;
            var ae = document.activeElement;
            return {totalLen: totalLen, count: bodyTexts.length,
                    bodyEmpty: bodyEmpty,
                    preview: bodyTexts.slice(0, 3),
                    activeElement: ae ? ae.tagName + '.' + (ae.className||'').substring(0,30) : 'null'};
        """)
        log.info(f"  [본문검증] {result}")
        if result and result.get('totalLen', 0) > 20:
            return True
        return False

    def _find_body_paragraph():
        """제목 영역 밖의 본문 <P> 요소 찾기"""
        # 방법 1: JS로 직접 찾기 (가장 정확)
        el = driver.execute_script("""
            var els = document.querySelectorAll('.se-text-paragraph');
            for (var i = 0; i < els.length; i++) {
                if (!els[i].closest('.se-documentTitle')
                 && !els[i].closest('.se-section-documentTitle')
                 && !els[i].closest('.se-title-text')) {
                    return els[i];
                }
            }
            return null;
        """)
        if el:
            return el
        # 방법 2: 제목이 아닌 컴포넌트 안의 paragraph
        try:
            return driver.find_element(By.CSS_SELECTOR,
                ".se-component:not(.se-documentTitle) .se-text-paragraph")
        except NoSuchElementException:
            pass
        return None

    def _cdp_full_click(x, y):
        """CDP 전체 마우스 이벤트 시퀀스"""
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseMoved", "x": x, "y": y
        })
        time.sleep(0.05)
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mousePressed", "x": x, "y": y,
            "button": "left", "clickCount": 1
        })
        time.sleep(0.05)
        driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
            "type": "mouseReleased", "x": x, "y": y,
            "button": "left", "clickCount": 1
        })

    def _get_body_rect():
        return driver.execute_script("""
            var els = document.querySelectorAll('.se-text-paragraph');
            for (var i = 0; i < els.length; i++) {
                if (!els[i].closest('.se-documentTitle')
                 && !els[i].closest('.se-section-documentTitle')
                 && !els[i].closest('.se-title-text')) {
                    els[i].scrollIntoView({block:'center'});
                    var r = els[i].getBoundingClientRect();
                    return {x: r.x + r.width/2, y: r.y + r.height/2};
                }
            }
            return null;
        """)

    # ── 전략 0: 문서 데이터 구조 심층 분석 ──
    log.info("  본문: 문서 데이터 구조 심층 분석")
    try:
        doc_structure = driver.execute_script("""
            var editor = window.__seEditor;
            if (!editor || !editor._documentService) return 'NO_DOC_SVC';

            var docData = editor._documentService.getDocumentData();
            if (!docData) return 'NO_DOC_DATA';

            var result = { topKeys: Object.keys(docData) };

            // document 객체 내부 탐색
            var doc = docData.document || docData;
            result.docKeys = Object.keys(doc);

            // components 찾기 (여러 경로 시도)
            var components = doc.components || doc.componentList ||
                             docData.components || docData.componentList || [];
            result.compCount = components.length;

            if (components.length > 0) {
                // 첫 번째 컴포넌트의 전체 구조 (500자까지)
                result.comp0 = JSON.stringify(components[0]).substring(0, 500);
                result.comp0Keys = Object.keys(components[0]);
                // 두 번째도 확인 (있으면)
                if (components.length > 1) {
                    result.comp1Keys = Object.keys(components[1]);
                    result.comp1 = JSON.stringify(components[1]).substring(0, 300);
                }
            }

            // documentService의 사용 가능한 메서드 목록
            var ds = editor._documentService;
            var dsMethods = [];
            for (var k in ds) {
                if (typeof ds[k] === 'function' && k.indexOf('_') !== 0) {
                    dsMethods.push(k);
                }
            }
            result.dsMethods = dsMethods.join(',');

            return result;
        """)
        log.info(f"  [문서구조] {doc_structure}")
    except WebDriverException as e:
        log.debug(f"  문서구조 파악 실패: {e}")

    # ── 전략 1: 정확한 SE ONE 컴포넌트 형식으로 setDocumentData ──
    log.info("  본문 전략 1: SE ONE 컴포넌트 구조 + setDocumentData()")
    try:
        set_result = driver.execute_script("""
            var editor = window.__seEditor;
            if (!editor || !editor._documentService) return 'NO_DOC_SVC';
            var ds = editor._documentService;
            var docData = ds.getDocumentData();
            if (!docData) return 'NO_DOC_DATA';

            var doc = docData.document || docData;
            var components = doc.components || [];
            var tried = [];
            var titleText = arguments[1] || '';  // 제목 텍스트

            // UUID 생성 함수
            function uuid() {
                return 'SE-' + 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
                    var r = Math.random()*16|0;
                    return (c==='x' ? r : (r&0x3|0x8)).toString(16);
                });
            }

            // documentTitle 컴포넌트 보존 + ★이미지 컴포넌트 보존(_insert_images로 올린 이미지가
            // setDocumentData 전체 재구성에서 누락돼 사라지던 버그 수정) + 텍스트 컴포넌트 찾기.
            // (이미지를 existingTextComp 로 잡으면 textCtype 이 'image' 로 오염되므로 별도 수집.)
            var titleComp = null;
            var existingTextComp = null;
            var imageComps = [];
            for (var i = 0; i < components.length; i++) {
                var ct = components[i]['@ctype'] || '';
                if (ct === 'documentTitle') titleComp = components[i];
                else if (ct === 'image') imageComps.push(components[i]);
                else if (!existingTextComp) existingTextComp = components[i];
            }

            // ── 제목 컴포넌트에 제목 텍스트 설정 ──
            if (titleComp && titleText) {
                try {
                    titleComp.title[0].nodes[0].value = titleText;
                    tried.push('titleSet:OK');
                } catch(e) {
                    // title 구조가 다르면 직접 구성
                    titleComp.title = [{
                        id: uuid(),
                        nodes: [{id: uuid(), value: titleText, '@ctype': 'textNode'}],
                        style: {align: 'center', '@ctype': 'paragraphStyle'},
                        '@ctype': 'paragraph'
                    }];
                    tried.push('titleSet:rebuilt');
                }
            }
            tried.push('titleComp:' + (titleComp ? 'Y' : 'N'));
            tried.push('textComp:' + (existingTextComp ? existingTextComp['@ctype'] : 'N'));

            // 기존 텍스트 컴포넌트의 @ctype 값 사용 (truncated 방지)
            var textCtype = existingTextComp ? existingTextComp['@ctype'] : 'text';

            // ── HTML → SE ONE 문단 변환 (서식 보존) ──
            var div = document.createElement('div');
            div.innerHTML = arguments[0];

            // 인라인 요소에서 SE ONE 노드 추출 (bold, color 등 보존)
            function extractNodes(el) {
                var nodes = [];
                el.childNodes.forEach(function(child) {
                    if (child.nodeType === 3) { // TEXT_NODE
                        var t = child.textContent;
                        if (t) nodes.push({ id: uuid(), value: t,
                            style: { '@ctype': 'nodeStyle' }, '@ctype': 'textNode' });
                    } else if (child.nodeType === 1) { // ELEMENT_NODE
                        var tag = child.tagName;
                        var text = (child.textContent || '').trim();
                        if (!text) return;
                        var style = { '@ctype': 'nodeStyle' };
                        // 굵은 글씨
                        if (tag === 'STRONG' || tag === 'B' ||
                            child.style.fontWeight === 'bold' || child.style.fontWeight === '700') {
                            style.bold = true;
                        }
                        // 색상
                        if (child.style.color) {
                            style.fontColor = child.style.color;
                        }
                        // SPAN 내부의 스타일 상속
                        if (tag === 'SPAN' || tag === 'EM' || tag === 'A') {
                            if (tag === 'EM') style.italic = true;
                            if (tag === 'A') style.fontColor = '#1a73e8';
                        }
                        // 중첩 요소가 있으면 재귀 추출
                        if (child.children.length > 0) {
                            var sub = extractNodes(child);
                            sub.forEach(function(n) {
                                if (style.bold) n.style.bold = true;
                                if (style.italic) n.style.italic = true;
                                if (style.fontColor && !n.style.fontColor) n.style.fontColor = style.fontColor;
                            });
                            nodes = nodes.concat(sub);
                        } else {
                            nodes.push({ id: uuid(), value: text, style: style, '@ctype': 'textNode' });
                        }
                    }
                });
                return nodes;
            }

            // 블록 요소를 순회하며 문단 수집 (중복 방지: 자식이 블록이면 자식만 처리)
            var bodyParagraphs = [];
            var seen = new Set();
            var blockTags = ['P','DIV','H1','H2','H3','H4','LI','TR','TABLE','UL','OL','BLOCKQUOTE'];

            function processElement(el) {
                // 이미 처리된 요소 스킵
                if (seen.has(el)) return;
                var tag = el.tagName;

                // TABLE: 네이티브 SE ONE 표 컴포넌트로 변환
                if (tag === 'TABLE') {
                    seen.add(el);
                    var trs = el.querySelectorAll('tr');
                    if (trs.length === 0) return;

                    var colCount = 0;
                    trs[0].querySelectorAll('th, td').forEach(function() { colCount++; });
                    if (colCount === 0) return;
                    var colWidth = Math.round(10000 / colCount) / 100;

                    var tableRows = [];
                    trs.forEach(function(tr, ri) {
                        seen.add(tr);
                        var tableCells = [];
                        tr.querySelectorAll('th, td').forEach(function(cell) {
                            var cellText = (cell.textContent || '').trim();
                            var isHeader = cell.tagName === 'TH' || ri === 0;
                            var cellValue = cellText ? [{
                                id: uuid(),
                                nodes: [{id: uuid(), value: cellText,
                                    style: {bold: isHeader, '@ctype': 'nodeStyle'},
                                    '@ctype': 'textNode'}],
                                style: {align: 'left', '@ctype': 'paragraphStyle'},
                                '@ctype': 'paragraph'
                            }] : null;
                            tableCells.push({
                                id: uuid(), colSpan: 1, rowSpan: 1,
                                width: colWidth, height: 43,
                                value: cellValue, '@ctype': 'tableCell'
                            });
                        });
                        tableRows.push({cells: tableCells, '@ctype': 'tableRow'});
                    });

                    // 표 컴포넌트를 별도로 저장 (나중에 components에 추가)
                    if (!window.__seTableComps) window.__seTableComps = [];
                    window.__seTableComps.push({
                        id: uuid(), layout: 'default', align: 'center',
                        width: 100, columnCount: colCount,
                        rows: tableRows, '@ctype': 'table'
                    });
                    // 표 위치 마커 (bodyParagraphs에서 나중에 치환)
                    bodyParagraphs.push({
                        id: '__TABLE_MARKER_' + (window.__seTableComps.length - 1),
                        nodes: [{id: uuid(), value: '', style: {'@ctype':'nodeStyle'}, '@ctype':'textNode'}],
                        style: {align:'left','@ctype':'paragraphStyle'}, '@ctype':'paragraph'
                    });
                    return;
                }

                // UL/OL: 자식 LI만 처리
                if (tag === 'UL' || tag === 'OL') {
                    seen.add(el);
                    el.querySelectorAll(':scope > li').forEach(function(li) { processElement(li); });
                    return;
                }

                // 블록 요소: 자식에 블록이 있으면 자식만 처리 (중복 방지)
                var hasBlockChild = false;
                for (var c = 0; c < el.children.length; c++) {
                    if (blockTags.indexOf(el.children[c].tagName) >= 0) {
                        hasBlockChild = true; break;
                    }
                }
                if (hasBlockChild) {
                    seen.add(el);
                    for (var c = 0; c < el.children.length; c++) {
                        processElement(el.children[c]);
                    }
                    return;
                }

                // 리프 블록 요소: 노드 추출
                seen.add(el);
                var text = (el.textContent || '').trim();
                if (!text) return;

                var nodes = extractNodes(el);
                if (nodes.length === 0) {
                    nodes = [{ id: uuid(), value: text,
                        style: { '@ctype': 'nodeStyle' }, '@ctype': 'textNode' }];
                }

                // H2/H3: 굵은 글씨 + 큰 폰트 + 앞에 빈 줄 삽입 (가독성)
                if (tag === 'H2' || tag === 'H3') {
                    // 소제목 앞: ★섹션 경계 마커 + 빈 줄(이미지 분산 단위 — 표뿐 아니라
                    // H2/H3 섹션마다 bodyComps 를 끊어 이미지가 섹션 사이사이 들어가게 한다).
                    // 마커를 먼저 두어 이전 섹션을 닫고, 빈 줄+소제목은 새 섹션의 첫 문단이 된다
                    // (마커→빈 줄 순서라, 표 직후 소제목이 와도 빈 줄만 든 고아 블록이 안 생김).
                    if (bodyParagraphs.length > 0) {
                        bodyParagraphs.push({
                            id: '__SECTION_BREAK__' + bodyParagraphs.length,
                            nodes: [{id: uuid(), value: '',
                                style: {'@ctype': 'nodeStyle'}, '@ctype': 'textNode'}],
                            style: {align: 'left', '@ctype': 'paragraphStyle'},
                            '@ctype': 'paragraph'
                        });
                        bodyParagraphs.push({
                            id: uuid(), nodes: [{id: uuid(), value: '',
                                style: {'@ctype': 'nodeStyle'}, '@ctype': 'textNode'}],
                            style: {align: 'left', '@ctype': 'paragraphStyle'},
                            '@ctype': 'paragraph'
                        });
                    }
                    nodes.forEach(function(n) {
                        n.style.bold = true;
                        n.style.fontSize = tag === 'H2' ? 18 : 16;
                    });
                }

                // 모든 문단 왼쪽 정렬
                bodyParagraphs.push({
                    id: uuid(), nodes: nodes,
                    style: { align: 'left', '@ctype': 'paragraphStyle' },
                    '@ctype': 'paragraph'
                });
            }

            // 최상위 자식부터 순회
            for (var i = 0; i < div.children.length; i++) {
                processElement(div.children[i]);
            }
            // 자식 요소가 없는 경우 (순수 텍스트)
            if (bodyParagraphs.length === 0) {
                var text = div.textContent || div.innerText || '';
                text.split(/\\n+/).forEach(function(line) {
                    line = line.trim();
                    if (!line) return;
                    bodyParagraphs.push({ id: uuid(),
                        nodes: [{ id: uuid(), value: line,
                            style: { '@ctype': 'nodeStyle' }, '@ctype': 'textNode' }],
                        style: { align: 'left', '@ctype': 'paragraphStyle' },
                        '@ctype': 'paragraph' });
                });
            }
            tried.push('paras:' + bodyParagraphs.length);

            var textComponent = {
                id: uuid(),
                layout: 'default',
                value: bodyParagraphs,
                '@ctype': textCtype
            };

            // 표 마커를 실제 표 컴포넌트로 분리
            // bodyParagraphs에서 __TABLE_MARKER_ 를 찾아 textComponent를 분할
            var tables = window.__seTableComps || [];

            // ── 본문 컴포넌트(text/table)를 별도 배열 bodyComps 로 수집 ──
            //    이미지를 본문 사이에 인터리브하기 위해 대상만 bodyComps 로 모은다.
            //    분할점 = 표 마커(__TABLE_MARKER_) + 섹션 경계(__SECTION_BREAK__) 둘 다.
            //    (표뿐 아니라 H2/H3 섹션마다 별도 text 블록이 되어 이미지가 섹션 사이에 분산.)
            var bodyComps = [];
            var currentParas = [];
            for (var pi = 0; pi < bodyParagraphs.length; pi++) {
                var paraId = bodyParagraphs[pi].id || '';
                if (paraId.indexOf('__TABLE_MARKER_') === 0) {
                    if (currentParas.length > 0) {
                        bodyComps.push({ id: uuid(), layout: 'default',
                            value: currentParas, '@ctype': textCtype });
                        currentParas = [];
                    }
                    var tableIdx = parseInt(paraId.replace('__TABLE_MARKER_', ''));
                    if (tables[tableIdx]) bodyComps.push(tables[tableIdx]);
                } else if (paraId.indexOf('__SECTION_BREAK__') === 0) {
                    // 섹션 경계 — 현재까지를 한 블록으로 끊는다(마커 문단 자체는 버림).
                    if (currentParas.length > 0) {
                        bodyComps.push({ id: uuid(), layout: 'default',
                            value: currentParas, '@ctype': textCtype });
                        currentParas = [];
                    }
                } else {
                    currentParas.push(bodyParagraphs[pi]);
                }
            }
            if (currentParas.length > 0) {
                bodyComps.push({ id: uuid(), layout: 'default',
                    value: currentParas, '@ctype': textCtype });
            }
            if (bodyComps.length === 0) bodyComps.push(textComponent);  // 빈 본문 폴백(기존 동작)
            window.__seTableComps = null;  // 초기화

            // ── 폴백: H2·표 경계가 없는 평문(M<=1)인데 이미지 여럿(N>1)이면 문단을 균등
            //    청크로 나눠 bodyComps 를 늘린다(이미지 전량 상단 클러스터 방지). 표 블록은
            //    건드리지 않고, 문단 여럿을 가진 단일 text 컴포넌트일 때만 청크 분할.
            if (bodyComps.length <= 1 && imageComps.length > 1) {
                var only = bodyComps[0];
                if (only && only['@ctype'] === textCtype && only.value && only.value.length > 1) {
                    var paras = only.value;
                    // 목표 청크 수 = min(N+1, 문단수). 문단을 최대한 균등 배분(앞쪽 extra개가 +1).
                    var chunks = Math.min(imageComps.length + 1, paras.length);
                    var baseSz = Math.floor(paras.length / chunks);
                    var extra = paras.length % chunks;   // 앞 extra 청크는 baseSz+1
                    var rebuilt = [];
                    var pIdx = 0;
                    for (var g = 0; g < chunks; g++) {
                        var sz = baseSz + (g < extra ? 1 : 0);
                        rebuilt.push({ id: uuid(), layout: 'default',
                            value: paras.slice(pIdx, pIdx + sz), '@ctype': textCtype });
                        pIdx += sz;
                    }
                    if (rebuilt.length > 1) {
                        bodyComps = rebuilt;
                        tried.push('imgChunkFallback:' + rebuilt.length);
                    }
                }
            }

            // ── 조립: 제목 + 이미지 N장을 본문 M블록 사이에 균등 분산(인터리브) ──
            //    이미지 순서([0]=썸네일·represent:true) 보존, 위치만 재배치.
            //    올린 이미지 컴포넌트를 setDocumentData 재구성에서 보존하는 취지는 유지하되,
            //    제목 뒤에 전부 몰아넣지 않고 본문 섹션 사이에 흩뿌린다.
            var newComponents = [];
            if (titleComp) newComponents.push(titleComp);
            var N = imageComps.length, M = bodyComps.length;
            if (N) tried.push('imgsKept:' + N);
            if (N > 0 && M > 0) {
                var ii = 0;
                for (var bi = 0; bi < M; bi++) {
                    // 이미지 ii 를 본문블록 bi 앞에 배치: 목표 위치 = floor(ii * M / N)
                    while (ii < N && Math.floor(ii * M / N) === bi) {
                        newComponents.push(imageComps[ii]); ii++;
                    }
                    newComponents.push(bodyComps[bi]);
                }
                // 말미 잔여 이미지 — floor(ii*M/N) 수식상 정상 도달 불가(죽은 안전망).
                // 회귀 감지용으로 발생 시에만 로깅.
                if (ii < N) tried.push('imgTail:' + (N - ii));
                while (ii < N) { newComponents.push(imageComps[ii]); ii++; }
                tried.push('imgsInterleaved:' + N + '/' + M);
            } else {
                // 본문 없거나 이미지 없으면 기존처럼(이미지 먼저 or 본문만)
                for (var k1 = 0; k1 < N; k1++) newComponents.push(imageComps[k1]);
                for (var k2 = 0; k2 < M; k2++) newComponents.push(bodyComps[k2]);
            }

            doc.components = newComponents;
            tried.push('built:' + newComponents.length + 'comps,' + bodyParagraphs.length + 'paras');

            // setDocumentData로 전체 문서 설정
            try {
                ds.setDocumentData(docData);
                tried.push('setDocData:OK');
            } catch(e) {
                tried.push('setDocData:ERR:' + e.message.substring(0, 50));
            }

            // 제목 DOM 재동기화 (setDocumentData가 에디터를 다시 렌더링하므로)
            try {
                var titleText = ds.getDocumentTitle() || '';
                if (titleText) {
                    var titleDiv = document.querySelector('.se-title-text');
                    if (titleDiv) {
                        var p = titleDiv.querySelector('.se-text-paragraph') || titleDiv.querySelector('p');
                        if (p) {
                            titleDiv.querySelectorAll('.se-placeholder').forEach(function(ph) { ph.remove(); });
                            p.innerHTML = '';
                            var span = document.createElement('span');
                            span.className = 'se-fs- se-ff-';
                            span.textContent = titleText;
                            p.appendChild(span);
                        }
                        var el = titleDiv;
                        while (el) {
                            el.classList.remove('se-is-empty');
                            if (el.classList.contains('se-section-documentTitle') || el.tagName === 'BODY') break;
                            el = el.parentElement;
                        }
                    }
                    tried.push('titleSync:OK(' + titleText.substring(0, 20) + ')');
                }
            } catch(e) {
                tried.push('titleSync:ERR');
            }

            // 검증: getContentText로 실제 텍스트 확인
            try {
                var ct = ds.getContentText();
                tried.push('contentText(' + (ct||'').length + '):' + (ct || '').substring(0, 80));
            } catch(e) {
                tried.push('contentText:ERR');
            }

            // 검증: 컴포넌트 수 확인
            try {
                var v = ds.getDocumentData();
                var vDoc = v.document || v;
                var vc = vDoc.components || [];
                tried.push('verify:comps=' + vc.length);
                for (var i = 0; i < vc.length; i++) {
                    tried.push('c' + i + ':' + vc[i]['@ctype'] + '(v=' + (Array.isArray(vc[i].value) ? vc[i].value.length : '?') + ')');
                }
            } catch(e) {}

            return tried.join(' | ');
        """, html, title)
        log.info(f"  [SE컴포넌트] {set_result}")
        time.sleep(2)

        # ── 제목 DOM 강제 렌더링 (setDocumentData 리렌더 완료 후) ──
        if title:
            try:
                title_sync = driver.execute_script("""
                    var titleText = arguments[0];
                    var editor = window.__seEditor;
                    var ds = editor && editor._documentService;

                    // 1) setDocumentTitle API 호출 (내부 상태 확실히 설정)
                    if (ds) {
                        try { ds.setDocumentTitle(titleText); } catch(e) {}
                    }

                    // 2) DOM 강제 업데이트 (React 리렌더 후이므로 유지됨)
                    var titleDiv = document.querySelector('.se-title-text');
                    if (!titleDiv) return 'NO_TITLE_DIV';
                    var p = titleDiv.querySelector('.se-text-paragraph') || titleDiv.querySelector('p');
                    if (!p) return 'NO_P';

                    // 플레이스홀더 제거
                    titleDiv.querySelectorAll('.se-placeholder').forEach(function(ph) { ph.remove(); });

                    // 기존 span 구조를 활용하거나 새로 생성
                    var existingSpan = p.querySelector('span[class*="se-f"]');
                    if (existingSpan) {
                        existingSpan.textContent = titleText;
                    } else {
                        p.innerHTML = '';
                        var span = document.createElement('span');
                        span.className = 'se-ff-nanumgothic se-fs32 __se-node';
                        span.textContent = titleText;
                        p.appendChild(span);
                    }

                    // se-is-empty 제거 (제목 모듈 + 상위 섹션)
                    var el = titleDiv;
                    while (el) {
                        el.classList.remove('se-is-empty');
                        if (el.classList.contains('se-section-documentTitle') || el.tagName === 'BODY') break;
                        el = el.parentElement;
                    }

                    // 3) 에디터 내부 이벤트 트리거 (변경 감지)
                    try {
                        p.dispatchEvent(new Event('input', {bubbles: true}));
                        p.dispatchEvent(new Event('change', {bubbles: true}));
                    } catch(e) {}

                    return 'OK:' + p.textContent.substring(0, 30);
                """, title)
                log.info(f"  [제목DOM] {title_sync}")
            except WebDriverException as e:
                log.debug(f"  제목 DOM 동기화 실패: {e}")

        if _verify_body():
            log.info("  ✅ 본문 입력 완료 (SE 컴포넌트)")
            return True
    except WebDriverException as e:
        log.info(f"  SE 컴포넌트 실패: {e}")

    # ── 전략 2: 실제 클립보드 paste 이벤트 디스패치 ──
    log.info("  본문 전략 2: 실제 ClipboardEvent 디스패치")
    try:
        paste_result = driver.execute_script("""
            var editor = window.__seEditor;
            if (!editor) return 'NO_EDITOR';
            var tried = [];

            // VE focus
            if (editor._virtualEditable) {
                try {
                    editor._virtualEditable.focus();
                    editor._virtualEditable.showCursor();
                } catch(e) {}
            }

            var html = arguments[0];
            var plainDiv = document.createElement('div');
            plainDiv.innerHTML = html;
            var plainText = plainDiv.textContent || '';

            // 방법 1: 진짜 ClipboardEvent 생성 + 디스패치
            try {
                var dt = new DataTransfer();
                dt.setData('text/html', html);
                dt.setData('text/plain', plainText);
                var evt = new ClipboardEvent('paste', {
                    bubbles: true, cancelable: true, clipboardData: dt
                });

                // SE ONE의 입력 타겟에 디스패치
                var targets = [];
                // input_buffer iframe
                var iframes = document.querySelectorAll('iframe[id*="input_buffer"]');
                for (var f of iframes) {
                    try {
                        var fbody = f.contentDocument.body;
                        if (fbody) targets.push(fbody);
                    } catch(e) {}
                }
                // 본문 영역
                var bodyPs = document.querySelectorAll('.se-text-paragraph');
                for (var p of bodyPs) {
                    if (!p.closest('.se-title-text') && !p.closest('.se-documentTitle')) {
                        targets.push(p);
                        break;
                    }
                }
                targets.push(document.body);

                for (var t of targets) {
                    try {
                        var dispatched = t.dispatchEvent(evt);
                        tried.push('dispatch(' + t.tagName + (t.id ? '#'+t.id : '') + '):' + dispatched);
                    } catch(e) {
                        tried.push('dispatch(' + t.tagName + '):ERR:' + e.message.substring(0,30));
                    }
                }
            } catch(e) {
                tried.push('clipboardEvent:ERR:' + e.message.substring(0, 50));
            }

            // 방법 2: editingService.insertByExternalPaste에 진짜 DataTransfer
            try {
                var dt2 = new DataTransfer();
                dt2.setData('text/html', html);
                dt2.setData('text/plain', plainText);
                editor._editingService.insertByExternalPaste(dt2);
                tried.push('insertEP(DataTransfer):OK');
            } catch(e) {
                tried.push('insertEP(DataTransfer):ERR:' + e.message.substring(0, 50));
            }

            return tried.join(' | ');
        """, html)
        log.info(f"  [ClipboardEvent] {paste_result}")
        time.sleep(2)
        if _verify_body():
            log.info("  ✅ 본문 입력 완료 (ClipboardEvent)")
            return True
    except WebDriverException as e:
        log.info(f"  ClipboardEvent 실패: {e}")

    # ── 전략 3: Selenium 클립보드 (본문 클릭 후) ──
    log.info("  본문 전략 3: Selenium 클릭 + 클립보드 붙여넣기")
    try:
        # 본문 영역 Selenium 클릭
        _click_body_area(driver)
        time.sleep(0.3)

        # VE focus 보강
        driver.execute_script("""
            var editor = window.__seEditor;
            if (editor && editor._virtualEditable) {
                try { editor._virtualEditable.focus(); } catch(e) {}
                try { editor._virtualEditable.showCursor(); } catch(e) {}
            }
        """)
        time.sleep(0.3)

        # 클립보드에 HTML 복사 후 붙여넣기
        pyperclip.copy(html)
        ActionChains(driver).key_down(Keys.CONTROL).send_keys('v')\
            .key_up(Keys.CONTROL).perform()
        time.sleep(2)
        if _verify_body():
            log.info("  ✅ 본문 입력 완료 (Selenium clipboard)")
            return True
    except (WebDriverException, OSError) as e:
        log.info(f"  Selenium clipboard 실패: {e}")

    # ── 전략 4: CDP 클릭 + 클립보드 ──
    log.info("  본문 전략 4: CDP click + clipboard")
    body_rect = _get_body_rect()
    if body_rect:
        try:
            _cdp_full_click(int(body_rect['x']), int(body_rect['y']))
            time.sleep(0.5)
            pyperclip.copy(html)
            ActionChains(driver).key_down(Keys.CONTROL).send_keys('v')\
                .key_up(Keys.CONTROL).perform()
            time.sleep(2)
            if _verify_body():
                log.info("  ✅ 본문 입력 완료 (CDP+clipboard)")
                return True
        except (WebDriverException, OSError) as e:
            log.info(f"  CDP+clipboard 실패: {e}")

    log.warning("  ❌ 본문 입력 실패 (모든 전략)")

    # 본문 입력 검증
    try:
        body_len = driver.execute_script(
            "var el = document.querySelector('.se-component-content, .se-text-paragraph'); "
            "return el ? el.innerHTML.length : 0;"
        )
        if body_len > 50:
            log.info(f"본문 입력 검증 성공 (길이: {body_len})")
            return True
        else:
            log.warning(f"본문 입력 검증 실패 (길이: {body_len})")
            return False
    except WebDriverException:
        log.debug("본문 검증 JS 실행 실패 — 낙관적으로 진행", exc_info=True)
        # JS 실행 자체가 불가능한 경우(드라이버 단절 등)는 상위 except가 잡음.
        # 드라이버는 정상이나 DOM 구조가 달라 querySelector가 None을 반환한 경우이므로
        # 이미 앞선 _verify_body() 검증을 통과하지 못한 상태. False로 처리.
        return False


def _input_tags(driver, tags: list):
    """SE ONE 에디터 태그 입력 영역에 태그를 입력한다.

    2026-05-08 사고 후 보강:
      - tags 비어있으면 WARNING 로그 (조용히 스킵하지 않음)
      - 셀렉터 후보 확장 (현행 SE ONE 구조 변화 대응)
      - 모든 후보 실패 시 WARNING 레벨 (DEBUG → WARNING) — GUI 사용자가 인지 가능
      - 성공 시 INFO 로그로 입력 개수 명시
      - 네이버 최대 태그 수 30개로 갱신 (기존 15개)
    """
    if not tags:
        log.warning("  ⚠️  태그 리스트 비어있음 — 입력 스킵 (post_data['tags']를 확인하세요)")
        return

    # SE ONE 태그 입력 영역 셀렉터 후보 (구→신 순)
    selectors = [
        # 신규 후보 — 현행 SE ONE에서 빈번히 쓰이는 패턴
        "input.tag_input",
        "input[name='tag']",
        "input[placeholder*='태그']",
        ".tag_area input[type='text']",
        "[class*='tag'] input[type='text']",
        # 기존 셀렉터 (보존)
        ".tag-input input",
        "#tagInput",
        "[placeholder*='태그']",
    ]

    inp = None
    matched_sel = None
    for sel in selectors:
        try:
            candidate = driver.find_element(By.CSS_SELECTOR, sel)
            # input 또는 그 자손 input 모두 허용
            if candidate.tag_name.lower() != "input":
                try:
                    candidate = candidate.find_element(By.TAG_NAME, "input")
                except NoSuchElementException:
                    continue
            inp = candidate
            matched_sel = sel
            break
        except NoSuchElementException:
            continue
        except (WebDriverException, ElementNotInteractableException) as _sel_err:
            log.debug(f"  셀렉터 '{sel}' 평가 중 예외: {type(_sel_err).__name__}")
            continue

    if inp is None:
        # 발행 패널 내부에만 존재하는 경우 또는 셀렉터 노후 — WARNING으로 즉시 인지
        log.warning(
            f"  ⚠️  태그 입력 영역 못 찾음 — 셀렉터 점검 필요 "
            f"({len(selectors)}종 시도, 태그 {len(tags)}개 미입력). "
            f"네이버 SE ONE DOM이 바뀌었거나 발행 패널을 먼저 열어야 할 수 있습니다."
        )
        return

    # 입력 시도 — 부분 실패해도 가능한 만큼은 입력하고 결과를 보고
    capped = tags[:30]   # 네이버 최대 30개
    inserted = 0
    try:
        inp.click()
        for tag in capped:
            try:
                inp.send_keys(tag)
                inp.send_keys(Keys.RETURN)
                inserted += 1
                time.sleep(0.25)
            except (ElementNotInteractableException, WebDriverException) as _tag_err:
                log.warning(
                    f"  ⚠️  태그 '{tag}' 입력 실패 ({type(_tag_err).__name__}) — 다음 태그 진행"
                )
                continue
    except (WebDriverException, OSError) as e:
        log.warning(
            f"  ⚠️  태그 입력 영역 클릭/입력 중 예외 — 입력된 개수 {inserted}/{len(capped)}: {e}"
        )
        return

    if inserted == 0:
        log.warning(
            f"  ⚠️  태그 영역은 찾았으나 0개 입력됨 (셀렉터: {matched_sel}, 태그 {len(capped)}개 시도)"
        )
    elif inserted < len(capped):
        log.warning(
            f"  ⚠️  태그 일부만 입력됨: {inserted}/{len(capped)}개 (셀렉터: {matched_sel})"
        )
    else:
        log.info(f"  ✅ 태그 입력 완료: {inserted}개 (셀렉터: {matched_sel})")


# ──────────────────────────────────────────────────
# 📂 카테고리 매핑 — 프로필별 category_map.json 2단계 lookup
#   <프로필 config>/category_map.json 우선. default 프로필만 레거시
#   _lib/category_map.json(examplebrand 수동 작성본) 폴백 — named 프로필은
#   다른 블로그의 맵을 절대 빌려 쓰지 않는다(맵 없음 = 매핑 미사용).
# ──────────────────────────────────────────────────
_LEGACY_CATEGORY_MAP_PATH = (
    Path(__file__).resolve().parents[2] / "_lib" / "category_map.json"
)


def _category_map_path():
    """활성 프로필의 카테고리 맵 경로. named 프로필에 맵이 없으면 None."""
    try:
        from config import profile as _pf
        cand = _pf.config_dir() / "category_map.json"
        if cand.exists():
            return cand
        if _pf.active_profile() == _pf.DEFAULT_PROFILE:
            return _LEGACY_CATEGORY_MAP_PATH
        return None
    except Exception:  # noqa: BLE001 — 프로필 계층 자체가 없으면 레거시 동작
        return _LEGACY_CATEGORY_MAP_PATH


def _remember_blog_menus(menus):
    """발행 패널에서 실측한 {메뉴라벨: 번호}를 프로필 카테고리 맵에 자동 학습.

    어떤 계정이든 첫 발행에서 menus 가 채워지므로 수동 맵 작성 없이도
    다음 발행부터 번호 매핑이 실동한다. 기존 detect_to_menu/fallback 은 보존
    (현재 유효한 맵을 시드로 사용 — default 프로필의 레거시 맵도 승계).
    """
    if not menus or not isinstance(menus, dict):
        return
    try:
        from config import profile as _pf
        path = _pf.config_dir() / "category_map.json"
        src = _category_map_path()
        cmap = safe_load_json(src) if (src and src.exists()) else {}
        if not isinstance(cmap, dict):
            cmap = {}
        cur = cmap.get("menus") or cmap.get("examplebrand_menus") or {}
        clean = {}
        for k, v in menus.items():
            try:
                clean[str(k).strip()] = int(v)
            except (TypeError, ValueError):
                continue
        merged = {**cur, **clean}
        if not merged or (merged == cur and path.exists()):
            return
        cmap["menus"] = merged
        cmap.pop("examplebrand_menus", None)
        cmap.setdefault("detect_to_menu", {})
        if not cmap.get("fallback"):
            cmap["fallback"] = next(iter(merged))
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(path), cmap)
        log.info(f"  📂 블로그 카테고리 자동 학습: {len(merged)}개 메뉴 → {path}")
    except Exception as e:  # noqa: BLE001 — 학습 실패가 발행을 깨면 안 됨
        log.debug(f"카테고리 자동 학습 실패(무시): {e}")


def _resolve_category_no(category_label, fallback):
    """detect_category 라벨 → 활성 프로필 블로그 메뉴 번호로 2단계 lookup.

    Args:
        category_label: post_data["category"] (예: "디지털", "영어학습법").
                        None 또는 빈 문자열이면 매핑 시도하지 않고 fallback 반환.
        fallback: int|str|None — 매핑 실패 시 사용할 기본 메뉴 번호
                  (보통 settings.BLOG_CATEGORY_NO).

    Returns:
        매핑된 메뉴 번호(str/int) 또는 None.

    Notes:
        - 맵 부재/손상 시 조용히 fallback 반환 (실서비스 회귀 방지).
        - 메뉴 사전 키는 범용 "menus" 우선, 레거시 "examplebrand_menus" 호환.
    """
    if not category_label:
        return fallback
    map_path = _category_map_path()
    if map_path is None or not map_path.exists():
        log.info(
            f"  ℹ️ category_map: 프로필 카테고리 맵 없음 — fallback 사용 "
            f"(label='{category_label}', config/category_map.json 작성 시 활성)"
        )
        return fallback

    cmap = safe_load_json(map_path)
    if not cmap or not isinstance(cmap, dict):
        log.warning(
            f"  ⚠️ category_map.json 로드 실패 또는 형식 오류 — fallback 사용 "
            f"(label='{category_label}')"
        )
        return fallback

    detect_to_menu = cmap.get("detect_to_menu") or {}
    examplebrand_menus = cmap.get("menus") or cmap.get("examplebrand_menus") or {}
    fallback_menu_label = cmap.get("fallback")

    menu_label = detect_to_menu.get(category_label)
    if not menu_label and category_label in examplebrand_menus:
        menu_label = category_label   # detect 라벨 == 블로그 메뉴명 직매칭(설정 0줄)
    if not menu_label:
        menu_label = fallback_menu_label
    if not menu_label:
        log.info(
            f"  ℹ️ category_map: '{category_label}' 매핑 없음 — fallback 사용"
        )
        return fallback

    menu_no = examplebrand_menus.get(menu_label)
    if menu_no in (None, ""):
        log.info(
            f"  ℹ️ category_map: 메뉴 라벨 '{menu_label}' 번호 미정 — fallback 사용"
        )
        return fallback

    log.info(
        f"  📂 category_map: '{category_label}' → '{menu_label}' (no={menu_no})"
    )
    return menu_no


def _menu_label_for_no(cat_no):
    """category_map.json에서 menu_no(int/str) → menu_label(예: '정부지원') 역방향 조회."""
    if cat_no in (None, ""):
        return None
    map_path = _category_map_path()
    if map_path is None or not map_path.exists():
        return None
    cmap = safe_load_json(map_path)
    if not cmap or not isinstance(cmap, dict):
        return None
    menus = cmap.get("menus") or cmap.get("examplebrand_menus") or {}
    for label, no in menus.items():
        if str(no) == str(cat_no):
            return label
    return None


def _selected_category_matches(driver, menu_label):
    """발행 패널의 카테고리 토글에 표시된 라벨이 menu_label 과 일치하는지 확인.

    2026-07-19 실측: dropdown 옵션 클릭이 '성공' 로그를 남겨도 실제 선택은
    안 바뀌어 기본 카테고리로 발행되는 가짜 성공이 있었다 — 클릭 후 표시
    라벨을 읽어 실반영을 판정한다.

    반환: True(일치) / False(불일치) / None(표시 라벨을 못 읽음 — 판단 불가).
    """
    if not menu_label:
        return None
    try:
        shown = driver.execute_script("""
            var sels = ["[data-testid='categoryDropdown']",
                        "[data-testid='categorySelector']",
                        "button[aria-haspopup='listbox']",
                        "button[aria-haspopup='menu']",
                        "[class*='category'][class*='select']",
                        "[class*='category'] button"];
            for (var i = 0; i < sels.length; i++) {
                var els = document.querySelectorAll(sels[i]);
                for (var j = 0; j < els.length; j++) {
                    var t = (els[j].textContent || '').trim();
                    if (t && t.length <= 60) return t;
                }
            }
            return null;
        """)
    except WebDriverException:
        return None
    if not shown:
        return None
    return menu_label in shown


def _set_category(driver, category_label=None, fallback=None) -> bool:
    """발행 패널에서 카테고리를 선택한다.

    Args:
        driver: Chrome WebDriver
        category_label: post_data["category"] (detect_category 라벨).
                        None이면 매핑 없이 fallback 사용.
        fallback: 매핑 실패 시 사용할 메뉴 번호 (보통 settings.BLOG_CATEGORY_NO).

    Returns:
        카테고리를 실제로 확정(클릭)했으면 True, 번호 미정/모든 셀렉터·텍스트 폴백 실패 시 False.
        호출부는 이 값이 False여도 발행 자체는 계속하되(경고만) — 카테고리는 시각만큼
        치명적이지 않고, testid 변경 시 전체 발행이 마비되는 것을 막기 위한 fail-open 정책.

    Notes:
        - 2026-05-21 SE ONE 발행 패널: data-testid="categoryItemText_{N}" 패턴.
        - 2026-05-26 보강: data-testid 변경 회귀 대비, 텍스트(메뉴 라벨) 기반 폴백 추가.
        - dropdown이 닫혀있을 수 있으므로 토글 후 옵션 클릭 — 양쪽 다 시도.
        - 옵션 element는 span이므로 closest()로 클릭 가능한 부모(li/button) 찾아 JS 클릭.
    """
    cat_no = _resolve_category_no(category_label, fallback)
    if cat_no in (None, ""):
        log.warning("  ⚠️ 카테고리 번호 미정 — 기본 메뉴로 발행")
        return False

    # 카테고리 번호 → examplebrand 메뉴 라벨 (텍스트 매칭용)
    menu_label = _menu_label_for_no(cat_no)

    # 1) 카테고리 dropdown 열기 시도 (이미 열려있어도 무해)
    dropdown_toggles = [
        "[data-testid='categoryDropdown']",
        "[data-testid='categorySelector']",
        # 2026-09-18 실측 DOM: div.option_category__* > div.selectbox__* >
        # button.selectbox_button__* (버튼 자체에는 'category' 가 없어 아래
        # 일반 셀렉터로는 잡히지 않았다 → 목록이 펼쳐지지 않아 현재 선택된
        # 메뉴 1개만 학습되고 나머지 메뉴는 영영 매핑되지 않았다).
        "[class*='option_category'] button[class*='selectbox_button']",
        "[class*='option_category'] button",
        "button[aria-haspopup='listbox']",
        "button[aria-haspopup='menu']",
        ".selectbox_btn",
        ".category_selectbox",
        "[class*='category'][class*='selectbox']",
        "[class*='category'][class*='dropdown']",
    ]
    for t_sel in dropdown_toggles:
        try:
            toggle = driver.find_element(By.CSS_SELECTOR, t_sel)
            try:
                toggle.click()
            except WebDriverException:
                driver.execute_script("arguments[0].click();", toggle)
            time.sleep(0.4)
            break
        except NoSuchElementException:
            continue

    # 1.5) 패널에 노출된 실제 메뉴(라벨→번호)를 프로필 맵에 자동 학습 —
    #      어떤 계정이든 첫 발행에서 menus 가 채워진다(fail-open).
    try:
        _remember_blog_menus(driver.execute_script("""
            var out = {};
            var nodes = document.querySelectorAll("[data-testid^='categoryItemText_']");
            for (var i = 0; i < nodes.length; i++) {
                var t = (nodes[i].textContent || '').trim();
                var m = (nodes[i].getAttribute('data-testid') || '').match(/_(\\d+)$/);
                if (t && m) out[t] = parseInt(m[1], 10);
            }
            return out;
        """))
    except WebDriverException as e:
        log.debug(f"카테고리 메뉴 수집 실패(무시): {e}")

    # 2) 카테고리 옵션 클릭 — 여러 셀렉터 패턴 시도 (번호 기반)
    selectors = [
        f"[data-testid='categoryItemText_{cat_no}']",
        f"[data-testid='categoryItem_{cat_no}']",
        f"[data-testid='categoryListItem_{cat_no}']",
        f"[data-category-no='{cat_no}']",
        f"[data-categoryno='{cat_no}']",
        f"[data-category='{cat_no}']",
        f"[data-no='{cat_no}']",
        f"[data-id='{cat_no}']",
        f"[data-value='{cat_no}']",
        f"input[type='radio'][value='{cat_no}']",
        f"label[for*='{cat_no}']",
        f"#category{cat_no}",
        f"#categoryNo{cat_no}",
        f"#cate_{cat_no}",
    ]
    for sel in selectors:
        try:
            el = driver.find_element(By.CSS_SELECTOR, sel)
        except NoSuchElementException:
            continue
        # span은 클릭 안 받을 수 있으므로 가장 가까운 li/button/role=option 부모 클릭.
        # React 패널 대비 pointer/mouse 이벤트 시퀀스도 함께 발사.
        clicked = driver.execute_script("""
            var el = arguments[0];
            var target = el.closest('li, button, [role="option"], [role="menuitem"]') || el;
            try {
                ['pointerdown','mousedown','pointerup','mouseup'].forEach(function(t){
                    try {
                        target.dispatchEvent(new MouseEvent(t,
                            {bubbles: true, cancelable: true, view: window}));
                    } catch (e) {}
                });
                target.click();
                return true;
            } catch (e) { return false; }
        """, el)
        if clicked:
            time.sleep(0.5)
            # 클릭 성공 ≠ 실반영 — 표시 라벨로 검증(2026-07-19 가짜 성공 실측)
            verified = _selected_category_matches(driver, menu_label)
            if verified is False:
                log.warning(
                    f"  ⚠️ 카테고리 클릭(sel={sel})했으나 표시 라벨 미일치 — 다음 방법 시도"
                )
                continue
            log.info(
                f"  📂 카테고리 선택 완료: '{category_label}' "
                f"(no={cat_no}, sel={sel}, "
                f"{'표시 확인됨' if verified else '표시 미확인(판단 불가)'})"
            )
            return True

    # 3) 번호 매칭 전부 실패 — 메뉴 라벨 텍스트로 폴백 (DOM 셀렉터 변경에 가장 강함)
    if menu_label:
        text_clicked = driver.execute_script("""
            var target = arguments[0];
            // 카테고리 옵션 후보 (드롭다운/리스트 내부)
            var scopes = [
                '[class*="category"]',
                '[data-testid*="category"]',
                '[role="listbox"]',
                '[role="menu"]',
                'ul.category_list',
            ];
            var pool = new Set();
            for (var s of scopes) {
                document.querySelectorAll(s + ' li, ' + s + ' button, ' +
                    s + ' [role="option"], ' + s + ' [role="menuitem"], ' +
                    s + ' label, ' + s + ' a, ' + s + ' span'
                ).forEach(function(e){ pool.add(e); });
            }
            // 후보가 없으면 패널 전체에서 보이는 텍스트 노드 검사
            if (pool.size === 0) {
                document.querySelectorAll(
                    'li, button, [role="option"], [role="menuitem"], label'
                ).forEach(function(e){
                    if (e.offsetParent) pool.add(e);
                });
            }
            // 텍스트 정확 일치 우선 → 부분 일치
            var exact = null, partial = null;
            pool.forEach(function(e) {
                if (!e.offsetParent) return;
                var t = (e.textContent || '').trim();
                if (!t || t.length > 80) return;  // 너무 긴 컨테이너 제외
                if (t === target) { exact = exact || e; }
                else if (t.indexOf(target) !== -1) { partial = partial || e; }
            });
            var hit = exact || partial;
            if (!hit) return 'NO_MATCH';
            var clickTarget = hit.closest('li, button, [role="option"], [role="menuitem"]') || hit;
            try {
                clickTarget.click();
                return (exact ? 'EXACT:' : 'PARTIAL:') + (clickTarget.tagName || '');
            } catch (e) {
                return 'CLICK_FAIL:' + e.message;
            }
        """, menu_label)
        if isinstance(text_clicked, str) and text_clicked.startswith(("EXACT:", "PARTIAL:")):
            time.sleep(0.5)
            verified = _selected_category_matches(driver, menu_label)
            if verified is False:
                log.warning(
                    f"  ⚠️ 카테고리 텍스트 폴백 클릭했으나 표시 라벨 미일치 "
                    f"(mode={text_clicked}, label='{menu_label}')"
                )
            else:
                log.info(
                    f"  📂 카테고리 선택(텍스트 폴백) 완료: '{category_label}' "
                    f"(no={cat_no}, label='{menu_label}', mode={text_clicked}, "
                    f"{'표시 확인됨' if verified else '표시 미확인(판단 불가)'})"
                )
                return True
        log.warning(
            f"  ⚠️ 카테고리 텍스트 폴백 결과: {text_clicked} (라벨='{menu_label}')"
        )

    # 4) 전부 실패 — 디버그용으로 패널 내 카테고리 후보 attrs 덤프
    try:
        debug_info = driver.execute_script("""
            var nodes = document.querySelectorAll(
                '[class*="category"], [data-testid*="category"], ' +
                '[role="listbox"] li, [role="menu"] li, input[type="radio"]'
            );
            var out = [];
            for (var i = 0; i < Math.min(nodes.length, 12); i++) {
                var n = nodes[i];
                var attrs = {};
                for (var k = 0; k < n.attributes.length; k++) {
                    var a = n.attributes[k];
                    if (a.name.indexOf('data-') === 0 || a.name === 'id' ||
                        a.name === 'name' || a.name === 'value' || a.name === 'aria-label') {
                        attrs[a.name] = (a.value || '').substring(0, 60);
                    }
                }
                out.push({
                    tag: n.tagName,
                    cls: (n.className || '').substring(0, 50),
                    txt: (n.textContent || '').trim().substring(0, 40),
                    attrs: attrs
                });
            }
            return out;
        """)
        log.warning(f"  [DEBUG] 카테고리 후보 노드: {debug_info}")
    except WebDriverException as e:
        log.debug(f"카테고리 디버그 덤프 실패: {e}")

    log.warning(
        f"  ⚠️ 카테고리 no={cat_no} 셀렉터/텍스트 모두 매칭 실패 — 첫 메뉴로 발행될 수 있음"
    )
    return False
