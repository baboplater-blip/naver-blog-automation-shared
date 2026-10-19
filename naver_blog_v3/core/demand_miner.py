# core/demand_miner.py — 라이브 수요 발굴 (Phase 2 · L2)
"""정적 엑셀을 넘어, 실제 사용자가 지금 검색하는 질의를 발굴한다.

네이버 자동완성(공개 엔드포인트, 인증 불필요)으로 시드 키워드를 실제 검색어로
확장한다. 예: '청년도약계좌' → 갈아타기·이자·조건·정부기여금·신청·해지·만기.

- 부수효과: 네트워크 읽기만. fail-open(네트워크 실패 시 빈 결과).
- Phase 1 연결: mine_from_opportunities() 는 performance_db 의 '기회 키워드'(유입되는데
  전용 글 없는 검색어)를 시드로 삼아 인접 수요를 확장 → 최고 ROI 발굴.
- ToS: 자동완성 공개 API + 저빈도(시드당 1콜, 사이 sleep). 대량 크롤 아님.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

AC_URL = "https://ac.search.naver.com/nx/ac"

try:
    from config.settings import HTTP_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    HTTP_REQUEST_TIMEOUT = 10

# 질문형/의도 표지 — 발굴 결과의 intent 라벨링
_INTENT_MARKERS = {
    "how":     ("방법", "하는법", "하는 법", "신청", "발급", "설정", "등록", "가입"),
    "check":   ("조건", "자격", "대상", "여부", "확인", "조회", "기준"),
    "compare": ("vs", "차이", "비교", "장단점", "어디"),
    "cost":    ("금액", "얼마", "이자", "수수료", "비용", "한도", "기여금"),
    "when":    ("기간", "언제", "만기", "날짜", "시기", "마감"),
    "trouble": ("안됨", "오류", "실패", "해지", "취소", "거부", "탈락"),
}


def autocomplete(seed: str, *, timeout: int = HTTP_REQUEST_TIMEOUT) -> list[str]:
    """네이버 자동완성 제안어 리스트. 실패 시 빈 리스트(fail-open)."""
    if not seed or not seed.strip():
        return []
    qs = urllib.parse.urlencode({
        "q": seed.strip(), "st": "100", "r_format": "json", "r_enc": "UTF-8",
        "r_unicode": "0", "t_koreng": "1", "frm": "nv", "ans": "2",
    })
    req = urllib.request.Request(
        f"{AC_URL}?{qs}",
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.naver.com/"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (OSError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        # OSError 는 URLError·소켓 오류 상위 — 네트워크 실패 전반을 fail-open.
        log.debug(f"[demand_miner] 자동완성 실패({seed[:20]}): {e}")
        return []
    out = []
    for group in data.get("items", []) or []:
        for row in group:
            if isinstance(row, list) and row and isinstance(row[0], str):
                s = row[0].strip()
                if s and s not in out:
                    out.append(s)
    return out


def _intent_of(kw: str) -> str:
    low = kw.replace(" ", "")
    for intent, markers in _INTENT_MARKERS.items():
        if any(m.replace(" ", "") in low for m in markers):
            return intent
    return "info"


def _is_expansion(seed: str, cand: str) -> bool:
    """cand 가 seed 를 확장한(수식어가 붙은) 질의인가 — 시드 자체/무관어 제외."""
    s, c = seed.strip(), cand.strip()
    return bool(c) and c != s and (s in c) and len(c) > len(s)


def mine(seeds, *, per_seed: int = 12, sleep: float = 0.2,
         volume_anchor: bool = True) -> list[dict]:
    """시드들을 실제 검색어로 확장 → 후보 리스트(중복 제거, intent·볼륨 라벨).

    반환: [{"keyword","seed","intent","volume","is_question"}, ...]
    """
    if isinstance(seeds, str):
        seeds = [seeds]
    seen: set[str] = set()
    results: list[dict] = []

    vol_fn = None
    if volume_anchor:
        try:
            from core.search_volume import best_volume
            vol_fn = best_volume
        except (ImportError, AttributeError):
            vol_fn = None

    for seed in seeds:
        sugg = autocomplete(seed)
        picked = 0
        for cand in sugg:
            if picked >= per_seed:
                break
            if not _is_expansion(seed, cand):
                continue
            key = cand.replace(" ", "")
            if key in seen:
                continue
            seen.add(key)
            vol = 0
            if vol_fn:
                try:
                    vol = int(vol_fn(cand) or 0)
                except (TypeError, ValueError):
                    vol = 0
            results.append({
                "keyword": cand, "seed": seed, "intent": _intent_of(cand),
                "volume": vol, "is_question": _intent_of(cand) != "info",
            })
            picked += 1
        if sleep:
            time.sleep(sleep)
    return results


def mine_from_opportunities(*, max_seeds: int = 20, per_seed: int = 10) -> list[dict]:
    """Phase 1 의 '기회 키워드'(유입되는데 전용 글 없음)를 시드로 인접 수요 발굴.

    실측 유입이 아직 없으면 performance_db 카탈로그의 primary 키워드를 시드로 폴백.
    """
    try:
        from core import performance_db as pdb
    except ImportError:
        return []
    opps = pdb.adjacent_keyword_opportunities(n=max_seeds)
    seeds = [o["keyword"] for o in opps]
    if not seeds:
        data = pdb.load()
        # 카탈로그에서 카테고리 대표 키워드(짧은=광역) 우선 폴백
        cats: dict[str, str] = {}
        for rec in data.get("posts", {}).values():
            cat = rec.get("category") or "기타"
            kw = rec.get("keyword") or ""
            if kw and (cat not in cats or len(kw) < len(cats[cat])):
                cats[cat] = kw
        seeds = list(cats.values())[:max_seeds]
    if not seeds:
        return []
    log.info(f"[demand_miner] 시드 {len(seeds)}개로 인접 수요 발굴")
    return mine(seeds, per_seed=per_seed)


def load_question_seeds() -> list[str]:
    """질문형 시드(logs/question_seeds.json)를 키워드 문자열로 로드. 없으면 빈 리스트.

    스키마: {"anchor","anchor_volume","seed","question","intent"} — 발행 키워드는 'seed'.
    """
    from config import safe_load_json
    data = safe_load_json(_DATA_DIR / "logs" / "question_seeds.json", default=list)
    out = []
    if isinstance(data, list):
        for d in data:
            if isinstance(d, dict):
                kw = d.get("seed") or d.get("keyword") or d.get("question") or ""
            else:
                kw = str(d)
            if kw:
                out.append(kw)
    return out


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    rows = mine(["청년도약계좌"], per_seed=8)
    for r in rows:
        print(f"  {r['keyword']}  [{r['intent']}] vol={r['volume']}")
