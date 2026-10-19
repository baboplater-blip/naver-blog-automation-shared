# core/query_evidence.py — 실측 하위검색어 근거 레이어 (2026-07-15)
"""제목·주제를 '사람이 실제로 친 검색어'에 앵커시키는 근거 원천.

네이버 검색광고 API로 발굴한 실검색어 풀(logs/searchad_harvest_pool.json — 키워드×
월간검색량×경쟁도)을 읽어, 어떤 주제(topic)의 **실제 하위 검색어 top-k**를 볼륨 순으로
돌려준다. 제목 생성기(페이블5 배치)가 이 근거로 제목을 쓰면, LLM 상상이 아니라
실측 수요 표현에 맞춘 정밀 제목이 나온다.

- 순수 조회(파일 읽기)만. 풀에 매칭이 없으면 라이브 harvest 로 폴백(있을 때).
- fail-open: 풀 부재/오류면 빈 리스트 → 호출부는 근거 없이도 동작.
- intent 라벨은 demand_miner._intent_of 를 재사용(how/check/compare/cost/when/trouble/info).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import List, Optional

from config.logger import get_logger

log = get_logger(__name__)

_POOL: Optional[list] = None


def _pool_path() -> Path:
    try:
        from config import profile as _p
        return _p.app_data_root() / "logs" / "searchad_harvest_pool.json"
    except Exception:  # noqa: BLE001
        return (Path(__file__).resolve().parent.parent
                / "logs" / "searchad_harvest_pool.json")


def _despace(s: str) -> str:
    return re.sub(r"[^가-힣A-Za-z0-9]", "", (s or "")).lower()


def load_pool() -> list:
    """근거 풀 로드(캐시). [{keyword, volume, comp, domain}]. 없으면 빈 리스트."""
    global _POOL
    if _POOL is not None:
        return _POOL
    p = _pool_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        _POOL = data if isinstance(data, list) else []
    except (OSError, ValueError):
        _POOL = []
    return _POOL


def reset_cache() -> None:
    global _POOL
    _POOL = None


def pool_size() -> int:
    return len(load_pool())


def _intent(kw: str) -> str:
    try:
        from core.demand_miner import _intent_of
        return _intent_of(kw)
    except Exception:  # noqa: BLE001
        return "info"


def related_queries(topic: str, *, k: int = 15, min_vol: int = 0,
                    use_live: bool = True) -> List[dict]:
    """topic 을 포함하는 실제 하위 검색어 top-k(볼륨 내림차순).

    반환: [{"keyword","volume","comp","intent"}]  (topic 자체는 제외 — 확장어만).
    풀에 매칭이 없고 use_live 면 라이브 harvest 로 폴백(검색광고 켜져 있을 때).
    """
    dt = _despace(topic)
    if not dt:
        return []
    hits = []
    for r in load_pool():
        kw = r.get("keyword", "")
        dk = _despace(kw)
        if dk == dt or dt not in dk:      # 자기 자신 제외 + 확장어만
            continue
        vol = int(r.get("volume", 0) or 0)
        if vol < min_vol:
            continue
        hits.append({"keyword": kw, "volume": vol,
                     "comp": r.get("comp", ""), "intent": _intent(kw)})
    if not hits and use_live:
        hits = _live_related(topic, min_vol=min_vol)
    hits.sort(key=lambda h: h["volume"], reverse=True)
    return hits[:k]


def _live_related(topic: str, *, min_vol: int = 0) -> List[dict]:
    """풀 미스 시 라이브 harvest 폴백(검색광고 미설정이면 빈 리스트)."""
    try:
        from core.searchad_volume import harvest
    except Exception:  # noqa: BLE001
        return []
    dt = _despace(topic)
    out = []
    for r in harvest(topic):
        dk = _despace(r.get("keyword", ""))
        if dk == dt or dt not in dk:
            continue
        vol = int(r.get("volume", 0) or 0)
        if vol < min_vol:
            continue
        out.append({"keyword": r["keyword"], "volume": vol,
                    "comp": r.get("comp", ""), "intent": _intent(r["keyword"])})
    return out


def has_evidence(topic: str, *, min_count: int = 1, min_vol: int = 0) -> bool:
    """topic 에 실측 하위검색어 근거가 min_count 개 이상 있는가(라이브 폴백 없이 풀만)."""
    return len(related_queries(topic, k=min_count, min_vol=min_vol,
                               use_live=False)) >= min_count
