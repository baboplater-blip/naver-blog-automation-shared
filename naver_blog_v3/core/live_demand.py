# core/live_demand.py — 검색량 게이트 widen 레이어 (2026-07-15)
"""엑셀에 없는 제목을 네이버 자동완성으로 '실검색 수요' 판정해 게이트를 넓힌다.

배경: search_volume(엑셀)은 사장님이 관리하는 좁은 키워드 화이트리스트라, 실제로 검색되는
온-브랜드 주제(주거급여·보금자리론·전세보증금·검정고시 등)를 볼륨 0 으로 오판해 발행을 막고
영구 제외(excluded_titles)했다. 이 모듈은 제목의 핵심어(앵커)를 네이버 자동완성 공개
엔드포인트에 물어 '사람들이 실제로 검색하는 stem' 이면 통과시킨다 → 키워드 목록이 자동 확장된다.

판정 3값 (auto_scheduler 게이트가 해석):
  True  — 자동완성에 뜨는 실검색어 → 통과(widen)
  False — 조회는 됐는데 확정적으로 비었음 → 수요 약함(스킵 + 영구 제외 대상)
  None  — 조회 실패(네트워크 등) → 불확정(이번만 스킵, 영구 제외 금지 → 다음 재시도)

⚠️ demand_miner.autocomplete 는 네트워크 실패와 '진짜 빈 결과'를 둘 다 [] 로 뭉갠다. 여기선
그 둘을 구분해야(None vs False) 실검색어를 조회 못 했다는 이유로 영구 제외하는 사고를 막으므로,
자체 fetch(_fetch)로 실패=None / 성공=list 를 분리한다.

ToS: 공개 자동완성 엔드포인트 + 파일 캐시(TTL) + 호출 간 sleep 으로 저빈도. demand_miner 동일 소스.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import List, Optional

from config.logger import get_logger

log = get_logger(__name__)

_AC_URL = "https://ac.search.naver.com/nx/ac"
_TTL_SEC = 30 * 24 * 3600      # 캐시 유효기간 30일
_MAX_ANCHORS = 3              # 제목당 최대 자동완성 호출 수(짧은 앵커 우선)
_NET_SLEEP = 0.1             # 실제 네트워크 호출 전 대기(ToS 예의)
_FETCH_TIMEOUT = 8

# 인구·수식 접두어 — 단독 앵커로는 과대매칭('청년' 무엇이든 통과)이라 후순위, 바이그램엔 허용.
_GENERIC = {
    "청년", "성인", "어르신", "노인", "고령자", "고등학생", "대학생", "중학생",
    "맞벌이", "다자녀", "신혼", "은퇴자", "주부", "프리랜서", "자영업자",
    "사회초년생", "저소득", "무주택", "기초", "차상위",
}
# 앵커로 부적합한 일반·기능어 — 단독 앵커에서 제외.
_STOP = {
    "vs", "및", "총정리", "정리", "가이드", "방법", "신청", "조건", "자격",
    "한눈에", "체크리스트", "얼마", "언제", "받는", "핵심", "비교", "차이",
    "단계별", "안내", "확인", "항목", "해결", "대응법", "대처법", "즉시",
}

_YEAR_RE = re.compile(r"20\d{2}")
_CLEAN_RE = re.compile(r"[^가-힣A-Za-z0-9 ]")  # 한글·영숫자·공백만
_NUM_HEAD_RE = re.compile(r"^\d")

# ── 캐시 ────────────────────────────────────────────────────────────
_cache: Optional[dict] = None


def _cache_path() -> Path:
    try:
        from config import profile as _p
        return _p.logs_dir() / "live_demand_cache.json"
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "logs" / "live_demand_cache.json"


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        from config import safe_load_json
        d = safe_load_json(_cache_path(), default=dict)
        _cache = d if isinstance(d, dict) else {}
    except Exception:  # noqa: BLE001
        _cache = {}
    return _cache


def _save_cache() -> None:
    if _cache is None:
        return
    try:
        from config import atomic_write_json
        p = _cache_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(p, _cache)
    except Exception as e:  # noqa: BLE001
        log.debug(f"[live_demand] 캐시 저장 실패(무시): {e}")


def reset_cache() -> None:
    """테스트용 — 메모리 캐시 무효화."""
    global _cache
    _cache = None


# ── 앵커 추출 ────────────────────────────────────────────────────────
def _candidate_anchors(title: str) -> List[str]:
    """제목에서 자동완성에 물어볼 짧은 앵커(검색 stem) 1~3개. 앞쪽·비수식어 우선."""
    t = _YEAR_RE.sub(" ", title or "")
    t = _CLEAN_RE.sub(" ", t)
    toks = [w for w in t.split() if len(w) >= 2 and not _NUM_HEAD_RE.match(w)]
    if not toks:
        return []
    anchors: List[str] = []

    def _add(a: str) -> None:
        a = a.strip()
        if a and a not in anchors and a not in _STOP:
            anchors.append(a)

    # 첫 '비수식·비기능' 토큰을 주제 앵커로.
    idx0 = 0
    for i, w in enumerate(toks):
        if w not in _GENERIC and w not in _STOP:
            idx0 = i
            break
    _add(toks[idx0])
    if idx0 + 1 < len(toks):
        _add(toks[idx0] + " " + toks[idx0 + 1])
    # 맨 앞이 수식어였다면 (앞토큰+주제) 바이그램도 — 주제가 뒤에 올 때 대비.
    if idx0 != 0:
        _add(toks[0] + " " + toks[idx0])
    return anchors[:_MAX_ANCHORS]


# ── 자동완성 조회 (실패=None / 성공=list) ────────────────────────────
def _fetch(anchor: str, *, timeout: int = _FETCH_TIMEOUT) -> Optional[List[str]]:
    """자동완성 제안어. 네트워크·파싱 실패=None, 성공=리스트([] 가능)."""
    if not anchor or not anchor.strip():
        return []
    qs = urllib.parse.urlencode({
        "q": anchor.strip(), "st": "100", "r_format": "json", "r_enc": "UTF-8",
        "r_unicode": "0", "t_koreng": "1", "frm": "nv", "ans": "2",
    })
    req = urllib.request.Request(
        f"{_AC_URL}?{qs}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.naver.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        log.debug(f"[live_demand] 자동완성 조회 실패({anchor[:20]}): {e}")
        return None
    out: List[str] = []
    for group in data.get("items", []) or []:
        for row in group:
            if isinstance(row, list) and row and isinstance(row[0], str):
                s = row[0].strip()
                if s and s not in out:
                    out.append(s)
    return out


def _is_real_stem(anchor: str, sugg: List[str]) -> bool:
    """제안어가 앵커를 실제로 완성/포함하면 실검색 stem 으로 인정(무관어 오탐 방지)."""
    a = anchor.replace(" ", "")
    if not sugg or not a:
        return False
    return any(a in s.replace(" ", "") or s.replace(" ", "") in a for s in sugg)


def _check_anchor(anchor: str) -> Optional[bool]:
    """앵커가 실검색 stem 인지 캐시/네트워크로 판정. True/False/None(=조회실패)."""
    key = anchor.replace(" ", "")
    if not key:
        return False
    cache = _load_cache()
    hit = cache.get(key)
    now = time.time()
    if isinstance(hit, dict) and "ok" in hit and (now - hit.get("ts", 0)) < _TTL_SEC:
        return bool(hit["ok"])
    if _NET_SLEEP:
        time.sleep(_NET_SLEEP)
    sugg = _fetch(anchor)
    if sugg is None:
        return None  # 조회 실패 — 캐시하지 않음(다음에 재시도)
    ok = _is_real_stem(anchor, sugg)
    cache[key] = {"ok": ok, "ts": now}
    _save_cache()
    return ok


# ── 공개 API ─────────────────────────────────────────────────────────
def live_demand_ok(title: str) -> Optional[bool]:
    """제목이 실검색 수요에 앵커됐는지. True(실검색)/False(수요약함)/None(조회실패)."""
    anchors = _candidate_anchors(title)
    if not anchors:
        return False
    saw_none = False
    for a in anchors:
        r = _check_anchor(a)
        if r is True:
            return True
        if r is None:
            saw_none = True
    return None if saw_none else False
