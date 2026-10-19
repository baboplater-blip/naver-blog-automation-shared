# gui/ui_prefs.py — GUI 전용 사용자 취향 저장소 (신규, auto-dev 5호 2026-09-02)
"""팔레트(라이트/다크) 같은 화면 취향을 저장한다.

config/settings.py 의 user_settings.json(자격증명·발행 설정)과는 별개의
작은 저장소다 — 프로필별로 격리되며(config.profile.logs_dir()), 자격증명을
전혀 다루지 않는다. 모든 실패는 fail-open: get() 은 default, set() 은
예외를 던지지 않고 False 만 반환한다 — 취향 저장 실패가 앱 흐름을 막으면
안 된다.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# config.profile 임포트 자체가 실패할 때만 쓰는 최후 폴백 경로.
_FALLBACK_PATH = Path(__file__).resolve().parent.parent / "logs" / "ui_prefs.json"


def path() -> Path:
    """저장 파일 경로.

    우선순위(``config/logger.py::_resolve_log_dir`` 와 동일한 관례):
    ``NBA_LOG_DIR`` 환경변수(테스트 격리 훅) > 활성 프로필 ``logs_dir()``
    > 레거시 폴백. 실측(auto-dev 9호): pytest 가 ``NBA_PROFILE=default`` 로
    두는데 default 프로필엔 별도 프로필 폴더가 없어 ``profile.logs_dir()``
    이 그대로 실서비스 ``naver_blog_v3/logs`` 를 가리켜, 설정 탭 테마
    라디오(트레이스 콜백)가 테스트 중 실 ``ui_prefs.json`` 을 덮어쓰는 누출이
    있었다 — ``NBA_LOG_DIR`` 을 최우선으로 확인해 막는다(라이브 GUI 는 이
    환경변수를 안 쓰므로 동작 불변).
    """
    override = os.environ.get("NBA_LOG_DIR", "").strip()
    if override:
        return Path(override) / "ui_prefs.json"
    try:
        from config import profile as _profile
        return _profile.logs_dir() / "ui_prefs.json"
    except Exception as exc:  # noqa: BLE001 — 프로필 모듈 부재 등 어떤 실패도 흡수
        log.debug("[ui_prefs] profile.logs_dir() 사용 불가, 폴백 경로 사용: %s", exc)
        return _FALLBACK_PATH


def _load() -> dict:
    """현재 저장 파일 전체를 dict 로 읽는다. 손상·부재 시 빈 dict(fail-open)."""
    try:
        from config import safe_load_json
        data = safe_load_json(path(), default={})
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.debug("[ui_prefs] 로드 실패(fail-open, 빈 값으로 계속): %s", exc,
                  exc_info=True)
        return {}


def get(key: str, default: Any = None) -> Any:
    """저장된 값을 읽는다. 파일이 없거나 손상돼도 default 를 그대로 반환한다."""
    try:
        return _load().get(key, default)
    except Exception as exc:  # noqa: BLE001 — get() 은 절대 예외를 던지지 않는다
        log.debug("[ui_prefs] get(%r) 실패(fail-open): %s", key, exc, exc_info=True)
        return default


def set(key: str, value: Any) -> bool:
    """값을 저장한다(tmp→replace 원자적 쓰기, config.atomic_write_json 재사용).

    Returns:
        저장 성공 여부. 실패해도 예외를 던지지 않는다 — 호출자는 반환값만
        보면 된다(팔레트 저장 실패가 화면 전환 자체를 막아서는 안 된다).
    """
    try:
        from config import atomic_write_json
        data = _load()
        data[key] = value
        atomic_write_json(path(), data)
        return True
    except Exception as exc:  # noqa: BLE001 — 저장 실패도 fail-open
        log.warning("[ui_prefs] set(%r) 저장 실패(fail-open): %s", key, exc,
                    exc_info=True)
        return False
