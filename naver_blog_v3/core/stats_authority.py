# core/stats_authority.py — 공식 통계 권위 근거 (한국은행 ECOS + KOSIS, 2026-08-16)
"""금리·물가·환율·고용·인구 등 공식 통계 수치를 생성 프롬프트에 권위 근거로 주입.

왜: 금융·복지 글의 신뢰(AEO 권위 신호)는 '공식 수치'에서 온다. 이 모듈은
    한국은행/통계청 공식 값을 대줘 무근거 수치를 실측 통계로 대체한다.

소스(무료 키):
  ① 한국은행 ECOS — KeyStatisticList(100대 지표). 단일 호출로 기준금리·소비자물가·
     환율·GDP·실업률 등 현재값을 통계표코드 없이 받는다(주제 무지 설계의 핵심).
     키: ECOS_API_KEY (ecos.bok.or.kr, 무료).
  ② 통계청 KOSIS(선택) — 키워드로 관련 통계표를 찾아 '참고 통계 존재'를 링크로 안내
     (값은 표코드가 필요해 참조 포인터만). 키: KOSIS_API_KEY (kosis.kr, 무료).

용도:
  · prompt_hint(text) — 키워드에 맞는 공식 지표(값·단위·시점·출처) 한 블록 주입(캐시만).
  · build_index()     — ECOS 100대 지표를 하루 1회 캐시(logs/stats_authority.json).

원칙(전 모듈 계승):
  - opt-in: 주입은 STATS_AUTHORITY_ENABLED=True 일 때만(기본 False).
  - fail-open: 키 없음/네트워크/파싱 실패 → 빈 결과, 파이프라인 무영향.
  - 주입식 fetch — 실서버 없이 결정론적 테스트.
  - 일 1회 인덱스 캐시 — ECOS 는 단일 호출이라 비용 무시 가능하나 예의상 캐시.
  - ToS: 공개 통계 API. 지표 값·단위·시점만 인용(대량 재배포 아님).

- 수동:  python -m core.stats_authority [키워드]
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Callable, Optional

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

from config import profile as _nbaprofile

_DATA_DIR = _nbaprofile.app_data_root()
INDEX_PATH = _DATA_DIR / "logs" / "stats_authority.json"

_ECOS_URL = "https://ecos.bok.or.kr/api/KeyStatisticList/{key}/json/kr/1/100/"
_KOSIS_SEARCH_URL = "https://kosis.kr/openapi/statisticsSearch.do"

_TOKEN_RE = re.compile(r"[가-힣A-Za-z0-9]+")
_DATE_FMT = "%Y-%m-%d %H:%M"

try:
    from config.settings import API_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    API_REQUEST_TIMEOUT = 10


def _setting(name: str, default):
    try:
        from config import settings as _s
        return getattr(_s, name, default)
    except ImportError:
        return default


def _enabled() -> bool:
    return bool(_setting("STATS_AUTHORITY_ENABLED", False))


def _ttl_hours() -> int:
    return int(_setting("STATS_AUTHORITY_TTL_HOURS", 24))


def _ecos_key() -> str:
    return (os.environ.get("ECOS_API_KEY", "")
            or str(_setting("ECOS_API_KEY", "") or ""))


def _kosis_key() -> str:
    return (os.environ.get("KOSIS_API_KEY", "")
            or str(_setting("KOSIS_API_KEY", "") or ""))


UrlFetcher = Callable[[str], "tuple[int, str]"]


def _default_fetcher(url: str) -> "tuple[int, str]":
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=API_REQUEST_TIMEOUT) as resp:
            return getattr(resp, "status", 200) or 200, resp.read().decode(
                "utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        log.debug("[stats] HTTP %s", exc.code)
        return exc.code, ""
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.debug("[stats] 조회 실패: %s", exc)
        return 0, ""


def _cycle_to_period(cycle: str) -> str:
    """ECOS CYCLE(YYYYMMDD/YYYYMM/YYYY) → 사람이 읽는 시점 문자열."""
    c = str(cycle or "").strip()
    if re.fullmatch(r"\d{8}", c):
        return f"{c[:4]}.{c[4:6]}.{c[6:8]}"
    if re.fullmatch(r"\d{6}", c):
        return f"{c[:4]}.{c[4:6]}"
    if re.fullmatch(r"\d{4}", c):
        return c
    return c


# ── ① ECOS 100대 지표 ────────────────────────────────────────────────────
def ecos_key_stats(*, fetcher: Optional[UrlFetcher] = None) -> "list[dict]":
    """ECOS KeyStatisticList → [{name, category, value, unit, period}]. 실패 시 []."""
    key = _ecos_key()
    if not key:
        return []
    url = _ECOS_URL.format(key=urllib.parse.quote(key, safe=""))
    fetch = fetcher or _default_fetcher
    try:
        status, body = fetch(url)
    except Exception as exc:  # noqa: BLE001
        log.debug("[stats] ECOS 주입 fetcher 예외(무시): %s", exc)
        return []
    if not (status and 200 <= status < 300 and body):
        return []
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(data, dict):          # 비객체 JSON → fail-open
        return []
    # 오류 응답: {"RESULT": {"CODE": "INFO-...", "MESSAGE": ...}}
    if "RESULT" in data and "KeyStatisticList" not in data:
        log.warning("[stats] ECOS 응답 오류: %s",
                    (data.get("RESULT") or {}).get("MESSAGE", ""))
        return []
    node = data.get("KeyStatisticList")
    rows = (node.get("row") if isinstance(node, dict) else None) or []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        name = (r.get("KEYSTAT_NAME") or "").strip()
        value = (r.get("DATA_VALUE") or "").strip()
        if not (name and value):
            continue
        out.append({
            "name": name,
            "category": (r.get("CLASS_NAME") or "").strip(),
            "value": value,
            "unit": (r.get("UNIT_NAME") or "").strip(),
            "period": _cycle_to_period(r.get("CYCLE", "")),
        })
    return out


# ── 인덱스 캐시 ───────────────────────────────────────────────────────────
def _load_index() -> dict:
    from config import safe_load_json
    data = safe_load_json(INDEX_PATH, default=dict)
    return data if isinstance(data, dict) else {}


def _index_fresh(rec: dict) -> bool:
    try:
        age_h = (datetime.now()
                 - datetime.strptime(rec.get("built_at", ""), _DATE_FMT)
                 ).total_seconds() / 3600
    except (TypeError, ValueError):
        return False
    return 0 <= age_h <= _ttl_hours()


def build_index(*, fetcher: Optional[UrlFetcher] = None,
                force: bool = False) -> dict:
    """ECOS 100대 지표를 (재)구축·캐시. 신선하면 스킵. 반환: {count, built_at}."""
    cur = _load_index()
    if not force and _index_fresh(cur) and cur.get("indicators"):
        return {"count": len(cur["indicators"]), "built_at": cur.get("built_at"),
                "cached": True}
    inds = ecos_key_stats(fetcher=fetcher)
    if not inds:
        return {"count": 0, "built_at": cur.get("built_at", ""), "cached": False}
    rec = {"built_at": datetime.now().strftime(_DATE_FMT), "indicators": inds}
    try:
        from config import atomic_write_json
        INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(str(INDEX_PATH), rec)
        log.info("[stats] ECOS 지표 캐시: %d건", len(inds))
    except Exception as e:  # noqa: BLE001
        log.debug("[stats] 인덱스 저장 실패(무시): %s", e)
    return {"count": len(inds), "built_at": rec["built_at"], "cached": False}


def _tokens(text: str) -> set:
    return {t for t in _TOKEN_RE.findall(text or "") if len(t) >= 2}


def facts_for(keyword: str, *, limit: int = 3,
              fetcher: Optional[UrlFetcher] = None,
              auto_build: bool = True) -> "list[dict]":
    """키워드에 매칭되는 공식 지표 상위 limit 건. 못 찾으면 [].

    매칭: 키워드 토큰이 (지표명 + 분류) 토큰에 겹치는 수. 0 이면 제외.
    """
    kw_tokens = _tokens(keyword)
    if not kw_tokens:
        return []
    idx = _load_index()
    if (not idx.get("indicators")) and auto_build:
        build_index(fetcher=fetcher)
        idx = _load_index()
    inds = idx.get("indicators") or []
    scored = []
    for ind in inds:
        hay = _tokens(ind.get("name", "")) | _tokens(ind.get("category", ""))
        overlap = len(kw_tokens & hay)
        if overlap:
            scored.append((overlap, ind))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [ind for _o, ind in scored[:limit]]


# ── ② KOSIS 관련 통계표 참조(선택 — 값이 아니라 존재/링크 포인터) ──────────
def kosis_tables(keyword: str, *, limit: int = 3,
                 fetcher: Optional[UrlFetcher] = None) -> "list[dict]":
    """KOSIS 통계표 검색 → [{title, org, link}]. 키 없으면 [] (참조 포인터만)."""
    key = _kosis_key()
    if not key or not keyword.strip():
        return []
    qs = urllib.parse.urlencode({
        "method": "getList", "apiKey": key, "searchNm": keyword,
        "format": "json", "jsonVD": "Y",
    })
    fetch = fetcher or _default_fetcher
    try:
        status, body = fetch(f"{_KOSIS_SEARCH_URL}?{qs}")
    except Exception as exc:  # noqa: BLE001
        log.debug("[stats] KOSIS 주입 fetcher 예외(무시): %s", exc)
        return []
    if not (status and 200 <= status < 300 and body):
        return []
    try:
        rows = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows[:limit]:
        if not isinstance(r, dict):
            continue
        title = (r.get("TBL_NM") or r.get("STAT_NM") or "").strip()
        if title:
            out.append({"title": title,
                        "org": (r.get("ORG_NM") or "").strip(),
                        "link": (r.get("LINK_URL") or "").strip()})
    return out


# ── prompt_hint (content_generator 주입) ─────────────────────────────────
def prompt_hint(text: str = "") -> str:
    """생성 프롬프트 주입용 '공식 통계 근거' 한 블록. ECOS 지표는 캐시만(네트워크 0).

    STATS_AUTHORITY_ENABLED=True + 매칭 지표가 있을 때만 반환. 아니면 "".
    """
    if not _enabled():
        return ""
    try:
        matches = facts_for(text, auto_build=False)   # 주입 경로는 캐시만
    except Exception as e:  # noqa: BLE001
        log.debug("[stats] prompt_hint 실패(무시): %s", e)
        return ""
    if not matches:
        return ""
    lines = ["[공식 통계 근거 — 한국은행 ECOS 실측 지표]",
             "아래는 공식 통계 값이다. 관련 수치는 이 값을 우선 사용하고 임의로 바꾸지 말 것"
             "(시점을 함께 밝혀 신뢰를 높여라):"]
    for m in matches:
        unit = f" {m['unit']}" if m.get("unit") else ""
        period = f" ({m['period']} 기준)" if m.get("period") else ""
        lines.append(f"· {m['name']}: {m['value']}{unit}{period}")
    return "\n".join(lines)


def stats() -> dict:
    idx = _load_index()
    return {"enabled": _enabled(),
            "has_ecos_key": bool(_ecos_key()), "has_kosis_key": bool(_kosis_key()),
            "index_count": len(idx.get("indicators") or []),
            "index_fresh": _index_fresh(idx), "path": str(INDEX_PATH)}


__all__ = ["ecos_key_stats", "build_index", "facts_for", "kosis_tables",
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
    print("\n── ECOS 지표 캐시 ──")
    print(json.dumps(build_index(), ensure_ascii=False, indent=2))
    if args:
        kw = args[0]
        print(f"\n── 공식 통계 근거: {kw} ──")
        for m in facts_for(kw):
            print(f"  · {m['name']}: {m['value']} {m.get('unit','')} "
                  f"({m.get('period','')})")
