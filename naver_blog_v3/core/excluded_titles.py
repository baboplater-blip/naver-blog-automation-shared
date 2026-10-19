# core/excluded_titles.py
# 중복으로 확인돼 Skip된 제목을 누적 저장하고, 다음 사이클에서 후보 단계에서 사전 제외한다.
#
# 배경 (2026-05-15):
#   auto_scheduler 가 매 사이클마다 trend_data.json 기준으로 후보를 정렬하는데,
#   100% 중복으로 매번 Skip되는 [1/20]~[7/20] 같은 제목이 trend_data.json에 그대로 남아
#   다음 사이클에서도 또 정렬·후보 진입·Skip을 반복하는 비효율이 있었다.
#   이 모듈은 한 번 Skip된 제목을 logs/excluded_titles.json에 누적하고
#   is_excluded() O(1) 멤버십 검사를 제공하여 후보 단계에서 사전 차감한다.
#
# 파일은 절대 trend_data.json을 건드리지 않는다 — 메모리 상 리스트만 차감.
from __future__ import annotations

import json
import os
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional, Set

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import atomic_write_json, safe_load_json  # noqa: E402
from config.logger import get_logger  # noqa: E402

log = get_logger(__name__)

# ─────────────────────────────────────────────────────
# 경로 상수
# ─────────────────────────────────────────────────────
# _PKG_ROOT = naver_blog_v3/ 디렉토리. tests에서 monkeypatch로 교체 가능.
_PKG_ROOT: Path = Path(__file__).resolve().parent.parent
try:
    from config import profile as _profile  # noqa: E402
    EXCLUDED_TITLES_PATH: Path = _profile.logs_dir() / "excluded_titles.json"
except Exception:  # noqa: BLE001 — 프로필 라우터 실패 시 레거시 하드코딩 경로로 폴백
    EXCLUDED_TITLES_PATH = _PKG_ROOT / "logs" / "excluded_titles.json"

# ─────────────────────────────────────────────────────
# 모듈 캐시 (1회 로드 후 in-memory set)
# ─────────────────────────────────────────────────────
_cache_lock = threading.Lock()
_cache: Optional[Set[str]] = None
_cache_path_snapshot: Optional[Path] = None

# 'YYYY-MM-DD — ' 또는 'YYYY-MM-DD - ' 또는 'YYYY-MM-DD ' prefix 매칭.
# 시각화 대시 문자 종류: 일반 하이픈(-), em dash(—), en dash(–).
_DATE_PREFIX_RE = re.compile(r"^\s*\d{4}-\d{2}-\d{2}\s*[—–\-]?\s*")
_WHITESPACE_RE = re.compile(r"\s+")


# ─────────────────────────────────────────────────────
# 정규화
# ─────────────────────────────────────────────────────
def normalize_title(title: str) -> str:
    """제목 정규화 — 앞쪽 'YYYY-MM-DD — ' prefix 제거 + 공백 정리.

    예:
        "2026-05-15 — 청년 주거지원금"  → "청년 주거지원금"
        "  2026-05-15  청년  지원  "    → "청년 지원"
        ""                                → ""

    Args:
        title: 원본 제목 문자열. None이면 빈 문자열로 처리.

    Returns:
        정규화된 제목. 항상 str (None 미반환).
    """
    if not title:
        return ""
    normalized = _DATE_PREFIX_RE.sub("", title)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


# ─────────────────────────────────────────────────────
# 내부 헬퍼 — 파일 로드/저장
# ─────────────────────────────────────────────────────
def _read_file() -> Set[str]:
    """파일에서 제목 set을 로드. 손상 시 빈 set + 경고 로그."""
    raw = safe_load_json(EXCLUDED_TITLES_PATH, default=None)
    if raw is None:
        return set()
    if not isinstance(raw, dict):
        log.warning(
            f"excluded_titles.json 포맷 이상(dict 아님: {type(raw).__name__}) — 빈 set 사용"
        )
        return set()
    titles = raw.get("titles", [])
    if not isinstance(titles, list):
        log.warning(
            f"excluded_titles.json 'titles' 키가 list 아님({type(titles).__name__}) — 빈 set 사용"
        )
        return set()
    # list 안의 비문자열 항목은 무시 (관용적 처리)
    return {normalize_title(t) for t in titles if isinstance(t, str) and t.strip()}


def _write_file(titles: Set[str]) -> None:
    """제목 set을 atomic write로 디스크에 저장."""
    payload = {
        "titles":     sorted(titles),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "count":      len(titles),
    }
    try:
        atomic_write_json(EXCLUDED_TITLES_PATH, payload)
    except OSError as exc:
        log.error(
            f"excluded_titles.json 저장 실패: {EXCLUDED_TITLES_PATH} — {exc}",
            exc_info=True,
        )


def _ensure_cache_loaded() -> Set[str]:
    """캐시가 비어있거나 EXCLUDED_TITLES_PATH가 바뀌었으면 다시 로드.

    테스트에서 monkeypatch로 EXCLUDED_TITLES_PATH를 바꿔도 자동 무효화되도록
    경로 스냅샷을 함께 보관한다.
    """
    global _cache, _cache_path_snapshot
    with _cache_lock:
        if _cache is None or _cache_path_snapshot != EXCLUDED_TITLES_PATH:
            _cache = _read_file()
            _cache_path_snapshot = EXCLUDED_TITLES_PATH
        return _cache


def _invalidate_cache() -> None:
    """캐시를 비워 다음 호출에서 재로드되도록 한다 (테스트용)."""
    global _cache, _cache_path_snapshot
    with _cache_lock:
        _cache = None
        _cache_path_snapshot = None


# ─────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────
def load_excluded() -> Set[str]:
    """현재 제외 set을 반환. 호출자가 수정해도 캐시는 영향 받지 않도록 사본을 반환.

    파일이 없으면 빈 set, 손상되면 빈 set + 경고 로그.
    """
    titles = _ensure_cache_loaded()
    return set(titles)  # defensive copy


def add_excluded(title: str, *, reason: str = "duplicate") -> None:
    """title을 정규화한 뒤 제외 set에 추가하고 즉시 디스크에 저장.

    이미 등록된 제목이면 no-op (디스크 쓰기 생략).
    저장 실패는 메인 흐름을 막지 않도록 로그만 남긴다.

    Args:
        title: 추가할 제목. None/빈 문자열은 무시.
        reason: 제외 사유 (현재는 로그용. 향후 필터링에 사용 예정).
    """
    if not title:
        return
    normalized = normalize_title(title)
    if not normalized:
        return

    with _cache_lock:
        cache = _cache
        # 캐시가 비어있으면 먼저 채운다 (락 안에서 _ensure_cache_loaded 재진입 회피)
        if cache is None or _cache_path_snapshot != EXCLUDED_TITLES_PATH:
            cache = _read_file()
            globals()["_cache"] = cache
            globals()["_cache_path_snapshot"] = EXCLUDED_TITLES_PATH

        if normalized in cache:
            return  # no-op

        cache.add(normalized)
        # 디스크 저장은 락 안에서 수행 (동시 add 시 경쟁 방지)
        _write_file(cache)

    log.info(f"excluded_titles 추가 [{reason}]: {normalized}")


def is_excluded(title: str) -> bool:
    """title이 제외 set에 있는지 O(1) 검사. 정규화 후 멤버십 확인."""
    if not title:
        return False
    normalized = normalize_title(title)
    if not normalized:
        return False
    cache = _ensure_cache_loaded()
    return normalized in cache


def remove_excluded(title: str) -> bool:
    """title을 제외 set에서 제거(되돌림). 있으면 True, 없으면 False.

    pool_triage 등에서 저수요로 제외한 제목을 복구할 때 사용. 없는 제목은 no-op.
    """
    if not title:
        return False
    normalized = normalize_title(title)
    if not normalized:
        return False
    with _cache_lock:
        cache = _cache
        if cache is None or _cache_path_snapshot != EXCLUDED_TITLES_PATH:
            cache = _read_file()
            globals()["_cache"] = cache
            globals()["_cache_path_snapshot"] = EXCLUDED_TITLES_PATH
        if normalized not in cache:
            return False
        cache.discard(normalized)
        _write_file(cache)
    log.info(f"excluded_titles 제거(복구): {normalized}")
    return True


__all__ = [
    "EXCLUDED_TITLES_PATH",
    "load_excluded",
    "add_excluded",
    "is_excluded",
    "remove_excluded",
    "normalize_title",
]
