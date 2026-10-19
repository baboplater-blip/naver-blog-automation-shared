# core/serp_fetch.py — 공유 SERP 조회 인프라 (개선안 #6·#7 공통 기반, 2026-07-08)
"""네이버 검색 결과(SERP)를 실제로 조회하는 단일 진입점.

#7(순위 KPI 교정)과 #6(AEO 실인용 관측)이 둘 다 "SERP 를 실제로 조회"하므로,
중복 구현을 막기 위해 조회 계층을 여기 한곳에 모은다. 두 소비자의 요구가 다르다:

- #7: 블로그 검색 **JSON API**(`openapi.naver.com/v1/search/blog.json`)로 순위 조회.
  자격증명 필요, 구조화된 items 반환. (기존 rank_tracker._search 를 이관·일반화)
- #6: 통합검색 **HTML**(`search.naver.com/search.naver`)에서 AI요약/스마트블록 영역을
  긁어 우리 글 인용 여부 판정. 자격증명 불필요, HTML 파싱(구조상 취약).

⚠️ HTML 스크랩은 네이버 마크업 변경에 취약하다. 그래서 **fetcher 를 인터페이스로 분리**해
주입식으로 만든다 — 테스트는 실서버 없이 결정론적으로 돌고, 마크업이 바뀌어도 파서만
교체하면 된다.

설계 원칙(전 경로 fail-open):
- 자격증명·네트워크가 없으면 예외를 던지지 않고 상태 문자열로 알린다.
- 어떤 실패도 발행/운영 루프를 막지 않는다(호출측이 상태를 보고 스킵).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

BLOG_SEARCH_URL = "https://openapi.naver.com/v1/search/blog.json"
INTEGRATED_SEARCH_URL = "https://search.naver.com/search.naver"
BLOG_DISPLAY = 100  # 블로그 검색 API 최대. 100위 안에 없으면 '미노출'로 간주.

# 통합검색 HTML 요청용 브라우저 UA(자동완성 스크래퍼와 동일 관습).
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

try:
    from config.settings import API_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    API_REQUEST_TIMEOUT = 10

# fetcher 시그니처: (url, headers) -> (status_code, body_text)
#   status_code 0 = 네트워크/기타 오류(HTTP 상태 없음).
HtmlFetcher = Callable[[str, dict], "tuple[int, str]"]


# ─────────────────────────────────────────────────────
# 블로그 검색 JSON API (#7 순위 조회)
# ─────────────────────────────────────────────────────
def blog_search(keyword: str, client_id: str, client_secret: str, *,
                display: int = BLOG_DISPLAY) -> "tuple[list, str]":
    """블로그 검색 API 호출 → (items, status).

    status: "ok" | "auth"(401/403) | "error"(기타/네트워크).
    자격증명이 비어 있으면 네트워크를 타지 않고 "auth" 로 즉시 반환한다.
    """
    if not (client_id and client_secret):
        return [], "auth"
    query_string = urllib.parse.urlencode(
        {"query": keyword, "display": display, "sort": "sim"}
    )
    request = urllib.request.Request(
        f"{BLOG_SEARCH_URL}?{query_string}",
        headers={"X-Naver-Client-Id": client_id,
                 "X-Naver-Client-Secret": client_secret},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return (data.get("items", []) or []), "ok"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            log.warning("[serp_fetch] 블로그 검색 API 인증 실패 %s — "
                        "client_id/secret·검색 스코프 확인 필요", exc.code)
            return [], "auth"
        log.warning("[serp_fetch] 블로그 검색 API HTTP %s (%s)", exc.code, keyword[:20])
        return [], "error"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.debug("[serp_fetch] 블로그 검색 실패 (%s): %s", keyword[:20], exc)
        return [], "error"


# ─────────────────────────────────────────────────────
# 통합검색 HTML (#6 AI요약 인용 관측)
# ─────────────────────────────────────────────────────
def _default_html_fetcher(url: str, headers: dict) -> "tuple[int, str]":
    """기본 통합검색 HTML fetcher. (status_code, body) 반환.

    네트워크/기타 오류는 status_code=0 으로 알린다(예외 던지지 않음 — fail-open).
    """
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=API_REQUEST_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            body = resp.read().decode(charset, errors="replace")
            return getattr(resp, "status", 200) or 200, body
    except urllib.error.HTTPError as exc:
        log.debug("[serp_fetch] 통합검색 HTTP %s (%s)", exc.code, url[:40])
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.debug("[serp_fetch] 통합검색 조회 실패 (%s): %s", url[:40], exc)
        return 0, ""


def fetch_integrated_search(keyword: str, *,
                            fetcher: Optional[HtmlFetcher] = None) -> "tuple[str, str]":
    """통합검색 HTML 조회 → (html, status).

    status: "ok"(본문 있음) | "empty"(200이나 본문 빔) | "error"(비200/네트워크).
    fetcher 를 주입하면 실서버 없이 결정론적으로 테스트할 수 있다(구조상 취약한
    스크랩을 인터페이스 뒤로 격리).
    """
    fetch = fetcher or _default_html_fetcher
    query_string = urllib.parse.urlencode({"query": keyword})
    url = f"{INTEGRATED_SEARCH_URL}?{query_string}"
    headers = {"User-Agent": _BROWSER_UA, "Referer": "https://www.naver.com/"}
    try:
        status_code, body = fetch(url, headers)
    except Exception as exc:  # noqa: BLE001 — 주입 fetcher 가 무엇이든 fail-open 보장
        log.debug("[serp_fetch] 주입 fetcher 예외(fail-open): %s", exc)
        return "", "error"
    if status_code and 200 <= status_code < 300:
        return (body, "ok") if body else ("", "empty")
    return "", "error"


__all__ = [
    "BLOG_SEARCH_URL",
    "INTEGRATED_SEARCH_URL",
    "BLOG_DISPLAY",
    "HtmlFetcher",
    "blog_search",
    "fetch_integrated_search",
]
