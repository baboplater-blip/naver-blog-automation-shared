# core/searchad_volume.py — 네이버 검색광고 API 월간 검색수 (실측 볼륨) (2026-07-15)
"""네이버 검색광고 '키워드도구' API로 키워드의 월간 검색수(PC+모바일)를 조회한다.

엑셀(좁은 화이트리스트)·자동완성(실검색 여부만)을 넘어선 **진짜 절대 검색량** 소스.
effective_volume 이 엑셀 미등록 키워드에 대해 floor(500) 대신 실측 볼륨을 쓰게 해
랭킹 정확도를 올린다. 캐시(TTL) + 완전 fail-open(키 없거나 오류면 None → 호출부가 폴백).

인증: HMAC-SHA256 서명.
  X-Timestamp: epoch ms
  X-API-KEY:   액세스 라이선스(api_key)
  X-Customer:  고객 ID
  X-Signature: base64(HMAC_SHA256(secret_key, "{ts}.{method}.{path}"))

자격증명은 Fernet 암호화 저장(SENSITIVE_KEYS). 이 모듈은 값을 로그에 남기지 않는다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
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

_BASE = "https://api.searchad.naver.com"
_PATH = "/keywordstool"
_TIMEOUT = 8
_NET_SLEEP = 0.15  # 호출 간 최소 간격(QPS 예의)

_cache: Optional[dict] = None
_last_net = [0.0]  # 마지막 네트워크 호출 시각(프로세스 로컬 스로틀)


# ── 설정/자격증명 ────────────────────────────────────────────────────
def _cfg():
    from config import settings as s
    return s


def available() -> bool:
    """검색광고 볼륨을 쓸 수 있는가 — ENABLED + 3개 자격증명 모두 존재."""
    s = _cfg()
    if not getattr(s, "SEARCHAD_API_ENABLED", False):
        return False
    return all(bool(getattr(s, k, "")) for k in
               ("SEARCHAD_API_KEY", "SEARCHAD_SECRET_KEY", "SEARCHAD_CUSTOMER_ID"))


def _ttl_sec() -> int:
    return int(getattr(_cfg(), "SEARCHAD_CACHE_TTL_DAYS", 7)) * 24 * 3600


# ── 캐시 ────────────────────────────────────────────────────────────
def _cache_path() -> Path:
    try:
        from config import profile as _p
        return _p.logs_dir() / "searchad_volume_cache.json"
    except Exception:  # noqa: BLE001
        return Path(__file__).resolve().parent.parent / "logs" / "searchad_volume_cache.json"


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
        log.debug(f"[searchad] 캐시 저장 실패(무시): {e}")


def reset_cache() -> None:
    global _cache
    _cache = None


# ── 파싱 ────────────────────────────────────────────────────────────
def _num(v) -> int:
    """'< 10' / '1,230' / 정수 → int. 파싱 불가면 0."""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        digits = re.sub(r"[^\d]", "", v)
        return int(digits) if digits else 0
    return 0


def _sign(ts: str, method: str, path: str, secret: str) -> str:
    msg = f"{ts}.{method}.{path}".encode("utf-8")
    return base64.b64encode(
        hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).digest()
    ).decode("utf-8")


def _http_keywordstool(keyword: str, api_key: str, secret: str, customer: str):
    """키워드도구 GET 원요청. (status, keywordList) 반환.

    status: HTTP 상태코드(200/401/403/…) 또는 -1(네트워크/타임아웃/파싱 실패).
    keywordList: 성공 시 리스트(빈 리스트 포함), 실패 시 None.
    ⚠️ 어떤 경로에서도 api_key·secret·customer 값을 로그에 남기지 않는다.
    """
    ts = str(int(time.time() * 1000))
    sig = _sign(ts, "GET", _PATH, secret)
    # 키워드도구는 공백 없는 키워드를 기대 — 공백 제거해서 조회
    kw = keyword.replace(" ", "")
    qs = urllib.parse.urlencode({"hintKeywords": kw, "showDetail": "1"})
    req = urllib.request.Request(
        f"{_BASE}{_PATH}?{qs}",
        headers={
            "X-Timestamp": ts,
            "X-API-KEY": api_key,
            "X-Customer": customer,
            "X-Signature": sig,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
        return int(getattr(resp, "status", 200) or 200), (data.get("keywordList") or [])
    except urllib.error.HTTPError as e:
        # 인증/권한 오류 등 — 상태코드만 남기고 본문(키가 echo될 수 있음)은 버린다.
        log.debug(f"[searchad] HTTP {e.code} ({kw[:16]})")
        return int(e.code), None
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as e:
        log.debug(f"[searchad] 조회 실패({kw[:16]}): {type(e).__name__}")
        return -1, None


def _parse_best(rows: list, keyword: str) -> int:
    """keywordList 에서 정확 일치 우선, 없으면 첫 연관어의 월간검색수(PC+모바일)."""
    kk = keyword.replace(" ", "")
    best = None
    for row in rows:
        rk = str(row.get("relKeyword", "")).replace(" ", "")
        tot = _num(row.get("monthlyPcQcCnt")) + _num(row.get("monthlyMobileQcCnt"))
        if rk == kk:
            return tot  # 정확 일치 우선
        if best is None:
            best = tot  # 첫 연관어(가장 근접) 보관
    return best if best is not None else 0


def _fetch_volume(keyword: str) -> Optional[int]:
    """단일 키워드의 월간검색수(PC+모바일). 네트워크/인증 실패=None."""
    s = _cfg()
    status, rows = _http_keywordstool(
        keyword,
        getattr(s, "SEARCHAD_API_KEY", ""),
        getattr(s, "SEARCHAD_SECRET_KEY", ""),
        str(getattr(s, "SEARCHAD_CUSTOMER_ID", "")),
    )
    if rows is None:
        return None  # 인증/네트워크 실패 — 폴백 신호
    if not rows:
        return 0
    return _parse_best(rows, keyword)


def verify_with(api_key: str, secret: str, customer: str,
                keyword: str = "청년월세지원") -> dict:
    """명시적 자격증명으로 1건 실측 조회(GUI/CLI 검증용).

    저장 전 입력값을 곧바로 시험할 수 있게 settings 를 거치지 않고 인자로 받는다.
    ⚠️ 반환 dict·로그 어디에도 키 값을 담지 않는다(사람이 읽을 사유 문구만).

    Returns:
        {"ok": bool, "volume": int|None, "reason": str}
    """
    api_key = (api_key or "").strip()
    secret = (secret or "").strip()
    customer = str(customer or "").strip()
    if not (api_key and secret and customer):
        return {"ok": False, "volume": None,
                "reason": "API KEY·비밀키·고객 ID 세 값을 모두 입력하세요."}
    status, rows = _http_keywordstool(keyword, api_key, secret, customer)
    if status == 200 and rows is not None:
        vol = _parse_best(rows, keyword) if rows else 0
        return {"ok": True, "volume": int(vol),
                "reason": f"연결 성공 — '{keyword}' 월간검색수 {int(vol):,} (PC+모바일)"}
    if status in (401, 403):
        return {"ok": False, "volume": None,
                "reason": ("인증 실패 — 고객 ID와 액세스 라이선스·비밀키가 서로 맞는지 "
                           "확인하세요(같은 라이선스에서 발급된 3값이어야 합니다).")}
    if status == 400:
        return {"ok": False, "volume": None,
                "reason": "요청 오류(400) — 키를 재확인하거나 잠시 후 다시 시도하세요."}
    if status and status > 0:
        return {"ok": False, "volume": None,
                "reason": f"서버 응답 오류(HTTP {status}) — 잠시 후 다시 시도하세요."}
    return {"ok": False, "volume": None,
            "reason": "네트워크 오류 — 인터넷 연결을 확인하세요."}


# ── 앵커 추출(live_demand 재사용) ────────────────────────────────────
def _anchors(text: str) -> List[str]:
    try:
        from core.live_demand import _candidate_anchors
        return _candidate_anchors(text)
    except Exception:  # noqa: BLE001 — 폴백: 통짜 텍스트
        return [text] if text else []


# ── 공개 API ─────────────────────────────────────────────────────────
def monthly_volume(text: str) -> Optional[int]:
    """제목/키워드의 실측 월간검색수. 앵커 중 최댓값. 캐시. 조회 전부 실패면 None."""
    if not available():
        return None
    anchors = _anchors(text)
    if not anchors:
        return None
    cache = _load_cache()
    now = time.time()
    ttl = _ttl_sec()
    best: Optional[int] = None
    saw_value = False
    for a in anchors:
        key = a.replace(" ", "")
        if not key:
            continue
        hit = cache.get(key)
        if isinstance(hit, dict) and "vol" in hit and (now - hit.get("ts", 0)) < ttl:
            v = hit["vol"]
        else:
            if _NET_SLEEP and (now - _last_net[0]) < _NET_SLEEP:
                time.sleep(_NET_SLEEP)
            v = _fetch_volume(a)
            _last_net[0] = time.time()
            if v is None:
                continue  # 이 앵커 조회 실패 — 캐시 안 함
            cache[key] = {"vol": int(v), "ts": now}
            _save_cache()
        saw_value = True
        if best is None or int(v) > best:
            best = int(v)
    return best if saw_value else None


def harvest(seed: str) -> List[dict]:
    """씨앗 키워드로 연관 키워드를 발굴한다(키워드 리서치 모드).

    검색광고 키워드도구는 hintKeyword 하나로 연관 키워드 수십~수백 개를 각각의
    월간검색수(PC+모바일)·경쟁도와 함께 돌려준다. 이 함수는 그 전체 목록을 반환한다.
    (monthly_volume 이 '정확 일치 볼륨 1개'만 쓰는 것과 달리, 여기선 목록 전체가 산출물.)

    Returns:
        [{"keyword": str, "volume": int, "comp": str}, ...]  (실패/미설정=[])
        comp: 경쟁정도 라벨('낮음'/'중간'/'높음' 또는 빈 문자열).
    """
    if not available():
        return []
    s = _cfg()
    if _NET_SLEEP and (time.time() - _last_net[0]) < _NET_SLEEP:
        time.sleep(_NET_SLEEP)
    status, rows = _http_keywordstool(
        seed,
        getattr(s, "SEARCHAD_API_KEY", ""),
        getattr(s, "SEARCHAD_SECRET_KEY", ""),
        str(getattr(s, "SEARCHAD_CUSTOMER_ID", "")),
    )
    _last_net[0] = time.time()
    if not rows:
        return []
    out: List[dict] = []
    for row in rows:
        kw = str(row.get("relKeyword", "")).strip()
        if not kw:
            continue
        vol = _num(row.get("monthlyPcQcCnt")) + _num(row.get("monthlyMobileQcCnt"))
        comp = str(row.get("compIdx", "") or "").strip()
        out.append({"keyword": kw, "volume": int(vol), "comp": comp})
    return out
