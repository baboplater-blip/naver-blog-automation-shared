# core/onboarding_status.py — 신규 사용자 선행조건 진단 (GUI 온보딩 체크리스트용)
"""앱을 처음 켠 사용자가 "무엇을 먼저 설정해야 하는가"를 한눈에 알도록,
발행·측정의 4대 선행조건을 값 노출 없이 항목별로 진단한다.

  ① 계정 입력      — config.settings.NAVER_ID 가 실제 ID 인가(placeholder 아님)
  ② 엑셀 지정      — 키워드 엑셀 파일이 디스크에 실제로 존재하는가
  ③ 네이버 로그인  — 세션 쿠키가 저장돼 있는가(값 아님·존재/개수만)
  ④ 검색 API 스코프 — 검색 API 자격증명(client_id/secret)이 채워져 있는가

이 모듈은 **네트워크를 타지 않는다**(전부 로컬 설정·파일 존재 검사). 검색 API의
실제 스코프 401 여부는 네트워크가 필요하므로 growth 탭의 전용 진단(rank_tracker.verify)이
담당한다 — 여기서는 "자격증명이 비었는지"라는 값싼 프록시만 본다.

전부 fail-open: 어떤 검사가 예외로 실패하면 그 항목만 ``unknown`` 으로 두고
계속 진행한다. 자격증명·쿠키 '값'은 절대 반환하지 않는다(존재/개수만).

반환 dict:
    {
      "items": [                       # 4개 항목, 표시 순서대로
        {"key": "account", "ok": bool, "label": str, "hint": str, "tab": str},
        ...
      ],
      "all_ready": bool,               # 4개 항목 모두 ok 인가
      "missing": ["account", ...],     # ok 가 아닌 항목의 key 목록
    }
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 자격증명 placeholder — crypto._PLACEHOLDER_SECRETS 와 동기(값이 채워지지 않은 상태).
# 한 곳에서만 정의해 판정 기준을 통일한다.
_ACCOUNT_PLACEHOLDERS = ("", "your_naver_id")
_API_PLACEHOLDERS = ("", "your_client_id", "your_client_secret")


def _claude_connected() -> Optional[bool]:
    """Claude(글 생성 AI) 가 연결(설치+로그인)돼 있는지.

    설치됐지만 미로그인이면 False(연결 필요), 설치 자체가 안 됐어도 False.
    판정 모듈이 없으면 None(fail-open, 중립).

    Returns:
        True/False, 또는 판정 불가 시 None.
    """
    try:
        from core.claude_connect import status
    except Exception:  # noqa: BLE001 — 모듈 부재 등이면 중립
        return None
    try:
        return status() == "connected"
    except Exception:  # noqa: BLE001 — 어떤 실패도 흡수
        return None


def _account_configured() -> Optional[bool]:
    """네이버 블로그 계정(NAVER_ID)이 실제 값으로 설정됐는지.

    Returns:
        True/False, 또는 판정 불가 시 None(fail-open).
    """
    try:
        from config.settings import NAVER_ID
    except (ImportError, AttributeError):
        return None
    return (NAVER_ID or "").strip() not in _ACCOUNT_PLACEHOLDERS


def _excel_present() -> Optional[bool]:
    """키워드 엑셀 파일이 디스크에 실제로 존재하는지.

    활성 프로필의 엑셀 경로(brand.excel_path) → settings.KEYWORD_EXCEL_PATH 순으로
    해석하고 파일 존재만 확인한다.

    Returns:
        True/False, 또는 판정 불가 시 None(fail-open).
    """
    path_str = ""
    try:
        from config import brand as _brand
        path_str = _brand.excel_path() or ""
    except Exception:  # noqa: BLE001 — 브랜드 헬퍼 부재 시 settings 폴백
        path_str = ""
    if not path_str:
        try:
            from config.settings import KEYWORD_EXCEL_PATH
            path_str = KEYWORD_EXCEL_PATH or ""
        except (ImportError, AttributeError):
            return None
    if not path_str:
        return False
    try:
        return Path(path_str).exists()
    except OSError as exc:
        log.debug("엑셀 경로 존재 확인 실패: %s", exc, exc_info=True)
        return None


def _session_cookie_present() -> Optional[bool]:
    """네이버 세션 쿠키가 저장돼 있는지(값 노출 없이 존재만).

    Returns:
        True/False, 또는 판정 불가 시 None(fail-open).
    """
    try:
        from core.stats_fetcher import _cookie_header
        return bool(_cookie_header())
    except Exception as exc:  # noqa: BLE001 — 쿠키 경로/프로필 미로드 등 어떤 실패도 흡수
        log.debug("세션 쿠키 확인 실패: %s", exc, exc_info=True)
        return None


def _search_api_credentials_present() -> Optional[bool]:
    """검색 API 자격증명(client_id/secret)이 채워져 있는지(값싼 프록시).

    실제 스코프 401 여부는 네트워크가 필요 — growth 탭의 rank_tracker.verify() 담당.
    여기서는 자격증명이 비었는지(placeholder)만 본다.

    Returns:
        True/False, 또는 판정 불가 시 None(fail-open).
    """
    try:
        from config.settings import (NAVER_API_CLIENT_ID,
                                      NAVER_API_CLIENT_SECRET)
    except (ImportError, AttributeError):
        return None
    cid = (NAVER_API_CLIENT_ID or "").strip()
    secret = (NAVER_API_CLIENT_SECRET or "").strip()
    return cid not in _API_PLACEHOLDERS and secret not in _API_PLACEHOLDERS


def check_onboarding() -> dict:
    """4대 선행조건을 진단해 온보딩 체크리스트 데이터를 반환한다.

    네트워크를 타지 않으며(로컬 설정·파일만), 각 검사는 fail-open 이다.
    판정 불가(None)인 항목은 미충족(orange)으로 취급하지 않고 ``ok`` 를 그대로
    None 으로 남겨 호출자가 중립 표기할 수 있게 한다.

    Returns:
        dict — 모듈 docstring 의 반환 스키마 참조.
    """
    claude = _claude_connected()
    account = _account_configured()
    excel = _excel_present()
    cookie = _session_cookie_present()
    api = _search_api_credentials_present()

    items = [
        {
            "key": "claude",
            "ok": claude,
            "label": "Claude 연결 (글 생성 AI)",
            "hint": "설정 탭의 '🤖 Claude 연결 → 연결하기' 로 로그인하세요.",
            "tab": "settings",
        },
        {
            "key": "account",
            "ok": account,
            "label": "네이버 블로그 계정 입력",
            "hint": "설정 탭에서 네이버 아이디/비밀번호를 입력하세요.",
            "tab": "settings",
        },
        {
            "key": "excel",
            "ok": excel,
            "label": "키워드 엑셀 파일 지정",
            "hint": "설정 탭에서 키워드 엑셀 파일 경로를 지정하세요.",
            "tab": "settings",
        },
        {
            "key": "cookie",
            "ok": cookie,
            "label": "네이버 로그인(세션 쿠키)",
            "hint": "성장 관제탑의 '🔐 네이버 로그인'으로 세션 쿠키를 갱신하세요.",
            "tab": "growth",
        },
        {
            "key": "search_api",
            "ok": api,
            "label": "검색 API 자격증명",
            "hint": "설정 탭에서 검색 API client_id/secret 을 입력하세요.",
            "tab": "settings",
        },
    ]

    # 판정 불가(None)는 '미충족'으로 세지 않는다 — 중립 항목이라 온보딩 카드를
    # 강제로 띄우지 않는다(fail-open). 명시적 False 만 missing 으로 집계.
    missing = [it["key"] for it in items if it["ok"] is False]
    all_ready = all(it["ok"] is True for it in items)

    return {"items": items, "all_ready": all_ready, "missing": missing}
