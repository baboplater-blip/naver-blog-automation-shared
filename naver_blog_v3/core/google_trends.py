# core/google_trends.py — Google Trends 계절성·급상승 (pytrends, 2026-08-16)
"""Google Trends 로 주제의 계절성(성수기)·급상승 하위질의를 보조 신호로 수집.

⚠️ 보조 신호(정직): DataLab(이미 보유)과 겹치는 부분이 있고, pytrends 는 **비공식**
   라이브러리라 Google 이 마크업/레이트리밋을 바꾸면 깨질 수 있다. 그래서:
   - 기본 OFF(GOOGLE_TRENDS_ENABLED), pytrends 미설치면 자동 무효(fail-open).
   - 발행/생성 경로를 절대 막지 않는다(신호가 없으면 그냥 주입 안 함).
   - 클라이언트를 주입 가능하게 두어 pytrends·pandas 없이도 테스트가 돈다.

무료: pytrends 는 무료(비공식). 키·비용 0. 단 과다 호출 시 429 → 저빈도·캐시 필수.

용도:
  · seasonality(kw)   — 관심도 시계열 → 성수기 월·현재 추세(rising/flat/falling).
  · rising_queries(kw)— 급상승 연관 질의(신흥 하위 주제 = 선점 기회).
  · prompt_hint(text) — '지금 관심 상승 중 + 성수기 + 신흥 하위주제' 한 블록(캐시만).

원칙(전 모듈 계승): opt-in · fail-open · 주입식 클라이언트 · 일 캐시 · 프로필 격리.

- 수동:  python -m core.google_trends [키워드]   (pytrends 설치 시에만 실동)
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime
from typing import Callable, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

from config import profile as _nbaprofile

_DATA_DIR = _nbaprofile.app_data_root()
CACHE_PATH = _DATA_DIR / "logs" / "google_trends.json"
_DATE_FMT = "%Y-%m-%d %H:%M"
_MONTH_NAMES = ["", "1월", "2월", "3월", "4월", "5월", "6월",
                "7월", "8월", "9월", "10월", "11월", "12월"]
# 급상승 질의(외부 Google 검색어)를 프롬프트에 넣기 전 방어 — 명령형/URL/연락처 배제.
_INJECTION_RE = re.compile(
    r"(무시|ignore|시스템\s*프롬프트|system\s*prompt|지시(를|사항|에)|instruction|"
    r"프롬프트|당신은|https?://|t\.me/|카톡|텔레그램|\b010[-\s]?\d{3,4}[-\s]?\d{4}\b)",
    re.IGNORECASE)


def _setting(name: str, default):
    try:
        from config import settings as _s
        return getattr(_s, name, default)
    except ImportError:
        return default


def _enabled() -> bool:
    return bool(_setting("GOOGLE_TRENDS_ENABLED", False))


def _ttl_days() -> int:
    return int(_setting("GOOGLE_TRENDS_TTL_DAYS", 7))


# ── 주입식 클라이언트 ─────────────────────────────────────────────────────
# 클라이언트 시그니처: (keyword) -> {"interest": [{"date": "YYYY-MM", "value": int}],
#                                    "rising": [query_str, ...]}  또는 None(실패).
TrendClient = Callable[[str], Optional[dict]]


def available() -> bool:
    """pytrends 설치 여부(기본 클라이언트 실동 가능한지)."""
    try:
        import pytrends  # noqa: F401
        return True
    except Exception:  # noqa: BLE001 — 미설치/임포트 오류
        return False


def _default_client(keyword: str) -> Optional[dict]:
    """pytrends 기반 기본 클라이언트. 미설치·오류·레이트리밋 → None(fail-open)."""
    try:
        from pytrends.request import TrendReq
    except Exception:  # noqa: BLE001
        return None
    try:
        py = TrendReq(hl="ko-KR", tz=540)
        py.build_payload([keyword], timeframe="today 12-m", geo="KR")
        interest = []
        df = py.interest_over_time()
        if df is not None and not df.empty and keyword in df.columns:
            for idx, val in df[keyword].items():
                try:
                    period = idx.strftime("%Y-%m")
                except Exception:  # noqa: BLE001
                    period = str(idx)[:7]
                interest.append({"date": period, "value": int(val)})
        rising = []
        rq = py.related_queries() or {}
        node = (rq.get(keyword) or {}).get("rising")
        if node is not None and not node.empty and "query" in node.columns:
            rising = [str(q) for q in node["query"].tolist()][:8]
        return {"interest": interest, "rising": rising}
    except Exception as e:  # noqa: BLE001 — 네트워크·레이트리밋·파싱 전부 fail-open
        log.debug("[gtrends] pytrends 조회 실패(무시): %s", e)
        return None


# ── 분석 ──────────────────────────────────────────────────────────────────
def _analyze(raw: dict) -> dict:
    """클라이언트 원자료 → {peak_months, current, trend, rising}."""
    if not isinstance(raw, dict):
        return {"peak_months": [], "current": None, "trend": "flat", "rising": []}
    interest = [p for p in (raw.get("interest") or [])
                if isinstance(p, dict) and isinstance(p.get("value"), (int, float))]
    rising = []
    for q in (raw.get("rising") or []):
        q = re.sub(r"\s+", " ", str(q)).strip()
        if q and len(q) <= 40 and not _INJECTION_RE.search(q):
            rising.append(q)
    peak_months, current, trend = [], None, "flat"
    if interest:
        # 월별 평균으로 성수기 산출
        by_month: dict = {}
        for p in interest:
            m = str(p.get("date", ""))[5:7]
            if re.fullmatch(r"\d{2}", m):
                by_month.setdefault(int(m), []).append(float(p["value"]))
        if by_month:
            avg = {m: sum(v) / len(v) for m, v in by_month.items()}
            top = max(avg.values())
            if top > 0:
                peak_months = [_MONTH_NAMES[m] for m in sorted(avg)
                               if avg[m] >= top * 0.85]
        # 현재 추세: 마지막 값 vs 직전 3개월 평균
        values = [float(p["value"]) for p in interest]
        current = int(values[-1])
        if len(values) >= 4:
            prior = sum(values[-4:-1]) / 3
            if prior > 0:
                if current >= prior * 1.2:
                    trend = "rising"
                elif current <= prior * 0.8:
                    trend = "falling"
    return {"peak_months": peak_months[:4], "current": current,
            "trend": trend, "rising": rising[:8]}


def seasonality(keyword: str, *, client: Optional[TrendClient] = None) -> dict:
    """계절성·추세 분석. 실패 시 빈 필드(fail-open)."""
    if not keyword.strip():
        return {}
    raw = (client or _default_client)(keyword)
    if not isinstance(raw, dict):
        return {}
    return _analyze(raw)


def rising_queries(keyword: str, *, client: Optional[TrendClient] = None) -> "list[str]":
    """급상승 연관 질의만. 실패 시 []."""
    return seasonality(keyword, client=client).get("rising", [])


# ── 캐시 (prompt_hint 네트워크 0) ─────────────────────────────────────────
def _load_cache() -> dict:
    from config import safe_load_json
    data = safe_load_json(CACHE_PATH, default=dict)
    return data if isinstance(data, dict) else {}


def _age_days(collected_at: str):
    try:
        return (datetime.now()
                - datetime.strptime(collected_at, _DATE_FMT)).total_seconds() / 86400
    except (TypeError, ValueError):
        return None


def refresh_cache(keyword: str, *, client: Optional[TrendClient] = None) -> dict:
    """계절성·급상승을 수집해 캐시 적립. 반환: 분석 dict."""
    key = (keyword or "").strip()
    if not key:
        return {}
    result = seasonality(key, client=client)
    if not (result.get("peak_months") or result.get("rising")
            or result.get("current") is not None):
        return result
    try:
        from config import atomic_write_json
        data = _load_cache()
        data[key] = {**result, "collected_at": datetime.now().strftime(_DATE_FMT)}
        if len(data) > 300:
            def _key_age(k):
                a = _age_days(data[k].get("collected_at", ""))
                return a if a is not None else 1e9
            for k in sorted(data, key=_key_age, reverse=True)[:len(data) - 300]:
                del data[k]
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(CACHE_PATH), data)
        log.info("[gtrends] 적립: %s (성수기 %s · 급상승 %d)", key,
                 result.get("peak_months"), len(result.get("rising") or []))
    except Exception as e:  # noqa: BLE001
        log.debug("[gtrends] 적립 실패(무시): %s", e)
    return result


def _lookup(keyword: str) -> Optional[dict]:
    rec = _load_cache().get((keyword or "").strip())
    if not isinstance(rec, dict):
        return None
    age = _age_days(rec.get("collected_at", ""))
    if age is None or age > _ttl_days():
        return None
    return rec


def _anchor_for(text: str) -> str:
    try:
        from core.search_volume import best_keyword
        kw, vol = best_keyword(text or "")
        return kw if (kw and vol > 0) else ""
    except Exception:  # noqa: BLE001
        return ""


def prompt_hint(text: str = "") -> str:
    """생성 프롬프트 주입용 '계절성·신흥 하위주제' 한 블록. 캐시만(네트워크 0).

    GOOGLE_TRENDS_ENABLED=True + 캐시 히트일 때만. 아니면 "".
    """
    if not _enabled():
        return ""
    rec = _lookup(text) or _lookup(_anchor_for(text))
    if not rec:
        return ""
    peak = rec.get("peak_months") or []
    rising = rec.get("rising") or []
    trend = rec.get("trend")
    if not (peak or rising or trend in ("rising", "falling")):
        return ""
    lines = ["[검색 계절성·신흥 하위주제 — Google Trends 보조 신호]"]
    if trend == "rising":
        lines.append("· 지금 검색 관심이 상승 중 — 도입부에 시의성을 살려라"
                     "(단, 구체 수치·'급증' 같은 단정은 근거 있을 때만).")
    elif trend == "falling":
        lines.append("· 검색 관심이 하락 추세 — 상시 유용한 정보 중심으로 구성하라.")
    if peak:
        lines.append(f"· 성수기(관심 최고): {', '.join(peak)} — 해당 시기 관련 안내를 보강.")
    if rising:
        lines.append("· 신흥 하위주제(선점 기회 — 관련 있으면 소제목/문단 추가): "
                     + ", ".join(rising[:6]))
    return "\n".join(lines)


def stats() -> dict:
    data = _load_cache()
    fresh = sum(1 for k in data if _lookup(k))
    return {"enabled": _enabled(), "pytrends_available": available(),
            "cache_entries": len(data), "fresh": fresh,
            "ttl_days": _ttl_days(), "path": str(CACHE_PATH)}


__all__ = ["available", "seasonality", "rising_queries", "refresh_cache",
           "prompt_hint", "stats"]


if __name__ == "__main__":
    from config.profile_boot import pin_profile_from_argv
    pin_profile_from_argv()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    print("── 진단 ──")
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
    if args and available():
        kw = args[0]
        print(f"\n── 계절성·급상승: {kw} ──")
        print(json.dumps(seasonality(kw), ensure_ascii=False, indent=2))
