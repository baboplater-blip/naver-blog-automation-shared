# core/rank_tracker.py — 발행 키워드 실제 순위 추적 (Phase 1 · L1)
"""내 글이 각 키워드에서 네이버 검색 몇 위인가? — 상위노출율(랭킹)의 실측.

네이버 검색 API(블로그 검색, 기존 트렌드용 자격증명 재사용)로 키워드를 조회해
내 블로그 글의 순위를 찾는다. 공식 API 순위는 통합검색 노출과 100% 동일하진
않지만 '정확도(sim)' 정렬은 C-Rank·DIA 관련성의 신뢰할 만한 프록시다.

- 부수적 이득: 검색 결과의 link 로 글의 실제 url/log_no 를 발견해 performance_db 에
  reconcile(발행 시 url 이 비어 있던 232편을 채운다).
- fail-open: 자격증명 없으면 스킵. 네트워크 오류는 해당 키워드만 건너뜀.
- ToS: 공식 API 사용 + 저빈도(키워드당 1콜, 사이 sleep). 대량 크롤 아님.
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
from datetime import date
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

BASE_DIR = Path(__file__).parent.parent
SEARCH_URL = "https://openapi.naver.com/v1/search/blog.json"
DISPLAY = 100          # API 최대. 100위 안에 없으면 '미노출'로 간주
_TAG = re.compile(r"<[^>]+>")
_LOGNO = re.compile(r"blog\.naver\.com/[^/]+/(\d+)")

try:
    from config.settings import API_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    API_REQUEST_TIMEOUT = 10


def _min_volume() -> int:
    """순위 KPI 대상 최소 검색량. 제목 전체(초-롱테일)를 순위로 세던 허영지표를
    막기 위해, 실제 검색 수요가 있는 헤드 키워드만 추적한다(#7)."""
    try:
        from config import settings as _s
        return int(getattr(_s, "RANK_KPI_MIN_VOLUME", 500))
    except (ImportError, AttributeError, ValueError):
        return 500


def _creds() -> tuple[str, str, str]:
    """(client_id, client_secret, blog_id) 반환. 없으면 빈 문자열."""
    try:
        from config.settings import (NAVER_API_CLIENT_ID, NAVER_API_CLIENT_SECRET,
                                      NAVER_ID)
        return NAVER_API_CLIENT_ID or "", NAVER_API_CLIENT_SECRET or "", NAVER_ID or ""
    except (ImportError, AttributeError):
        return "", "", ""


# ── 구체 롱테일 쿼리 추적(2026-08-01) ──────────────────────────────────────
# 헤드 키워드(초경쟁·천장)만 재던 한계를 보강한다. 글이 실제 노출·트래픽을 얻는
# 구체 쿼리(예 '주거급여 지급일')의 순위를 별 tab("blog_specific")에 함께 기록해
# 니어미스(#11~30, 1페이지 문턱)를 탐지 가능하게 한다.
#
# '구체 쿼리' 소스 = query_evidence(검색광고 API 실측 하위검색어 풀). 선택 근거:
#   · 진짜 월간검색량이 붙은 '사람이 실제로 친 검색어'라 헤드-구체 경계(500~3,000)를
#     볼륨으로 정확히 겨눌 수 있다(엑셀 volume_index 는 헤드 위주 화이트리스트라
#     구체 다어절 쿼리가 드물고, live_demand 는 True/False 만 줘 볼륨 밴드 선택 불가).
#   · 풀-only 조회(use_live=False)라 글당 추가 네트워크 없음(API 예산 보호).
#   · fail-open: 풀 부재/무매칭이면 헤드+의도수식어 폴백 → 그마저 없으면 스킵.
_INTENT_MODIFIERS = (
    "신청방법", "신청기간", "신청기한", "지급일", "지급시기", "신청", "자격",
    "조건", "대상", "금액", "한도", "방법", "기간", "서류", "후기", "조회",
    "계산", "시기", "종류", "혜택", "소급", "인상",
)


def _specific_settings() -> tuple[bool, int, int]:
    """(구체쿼리 추적 on/off, 볼륨 하한, 볼륨 상한). 실패 시 안전 기본값."""
    enabled, lo, hi = True, 500, 3000
    try:
        from config import settings as _s
        enabled = bool(getattr(_s, "RANK_TRACK_SPECIFIC", True))
        lo = int(getattr(_s, "RANK_SPECIFIC_MIN_VOLUME", 500))
        hi = int(getattr(_s, "RANK_SPECIFIC_MAX_VOLUME", 3000))
    except (ImportError, AttributeError, ValueError, TypeError):
        pass
    if hi < lo:
        lo, hi = hi, lo
    return enabled, lo, hi


def _query_targeted_by_title(query: str, title_key: str) -> bool:
    """query 의 모든 어절이 제목(정규화 키)에 나타나면 True.

    부분 연속문자열이 아니라 어절 부분집합으로 판정한다 — '주거급여 지급일'처럼
    제목에서 두 어절이 떨어져('…주거급여 임차급여 지급일…') 있어도 인정.
    """
    parts = [_norm(p) for p in query.split()]
    parts = [p for p in parts if p]
    if not parts:
        return False
    return all(p in title_key for p in parts)


def _specific_from_evidence(title: str, head_kw: str,
                            lo: int, hi: int) -> tuple[str, int]:
    """검색광고 실측 하위검색어 중 제목이 타깃하는 구체 쿼리 (쿼리, 볼륨).

    밴드 [lo, hi] 안(천장 아래)이면 그중 최대 볼륨(트래픽 최대), 전부 천장 초과면
    최소 볼륨(경쟁 최소)을 고른다. 매칭 없으면 ("", 0). 예외는 상위서 처리.
    """
    from core import query_evidence

    title_key = _norm(title)
    head_key = _norm(head_kw)
    if not title_key or not head_key:
        return "", 0
    cands: list[tuple[str, int]] = []
    for row in query_evidence.related_queries(head_kw, k=40, min_vol=lo,
                                              use_live=False):
        kw = (row.get("keyword") or "").strip()
        vol = int(row.get("volume", 0) or 0)
        kw_key = _norm(kw)
        if not kw_key or kw_key == head_key or kw_key == title_key:
            continue  # 헤드 그 자체·제목 전체(허영)는 구체 쿼리가 아님
        if not _query_targeted_by_title(kw, title_key):
            continue  # 글이 실제로 이 쿼리를 타깃하지 않으면 순위 추적 의미 없음
        cands.append((kw, vol))
    if not cands:
        return "", 0
    in_band = [c for c in cands if c[1] <= hi]   # min_vol=lo 라 이미 >= lo 보장
    if in_band:
        in_band.sort(key=lambda c: c[1], reverse=True)   # 천장 아래 최대 트래픽
        return in_band[0]
    cands.sort(key=lambda c: c[1])               # 전부 천장 초과 → 최소 경쟁
    return cands[0]


def _specific_from_modifier(title: str, head_kw: str) -> str:
    """폴백: 제목에 든 의도 수식어(지급일·신청방법 등)를 헤드에 붙인 구체 쿼리.

    실측 근거는 없지만(볼륨 0) 헤드보다 구체적이라 추적 가치가 있다. 수식어가
    없으면 "" (구체 쿼리 스킵 → 헤드만 추적).
    """
    head_key = _norm(head_kw)
    title_key = _norm(title)
    if not head_key or not title_key:
        return ""
    for mod in _INTENT_MODIFIERS:
        if _norm(mod) in title_key:
            cand = f"{head_kw} {mod}".strip()
            if _norm(cand) != head_key:
                return cand
    return ""


def _pick_specific_query(title: str, head_kw: str,
                         lo: int, hi: int) -> tuple[str, int, bool]:
    """제목이 타깃하는 구체 롱테일 쿼리 (쿼리, 볼륨, estimated).

    estimated=False = 검색광고 실측 하위검색어(볼륨 근거 有), True = 수식어 폴백
    (볼륨 0, 추정). 없으면 ("", 0, False). 예외 무전파(fail-open).
    """
    try:
        kw, vol = _specific_from_evidence(title, head_kw, lo, hi)
        if kw:
            return kw, vol, False
    except Exception as e:  # noqa: BLE001 — 근거 조회 실패는 폴백으로
        log.debug(f"[rank_tracker] 구체쿼리(근거) 추출 실패(무시): {e}")
    try:
        kw = _specific_from_modifier(title, head_kw)
        if kw:
            return kw, 0, True
    except Exception as e:  # noqa: BLE001 — 폴백 실패는 스킵
        log.debug(f"[rank_tracker] 구체쿼리(수식어) 추출 실패(무시): {e}")
    return "", 0, False


def _track_specific_for_post(rec: dict, title: str, head_kw: str, key: str,
                             blog_id: str, cid: str, secret: str,
                             band_lo: int, band_hi: int, today: str,
                             sleep: float, pdb, data: dict) -> tuple[int, int, int]:
    """글 하나의 구체 롱테일 쿼리 순위를 조회·기록. (구체노출, 니어미스, url보정) 증분.

    **헤드 순위 게이트와 독립** — 헤드가 권외/무수요여도 실행한다(니어미스가 가장
    절실한 저경쟁·니치 헤드 글이 배제되지 않게). API 예산: 구체=헤드거나 없으면
    재조회하지 않는다. 어떤 실패도 (0,0,0) 반환(헤드 경로 무영향).
    """
    try:
        spec_kw, spec_vol, estimated = _pick_specific_query(
            title, head_kw, band_lo, band_hi)
    except Exception as e:  # noqa: BLE001
        log.debug(f"[rank_tracker] 구체쿼리 선정 실패(무시): {e}")
        return 0, 0, 0
    if not spec_kw or _norm(spec_kw) == _norm(head_kw):
        return 0, 0, 0   # 없음/헤드와 동일 → 재조회 안 함(API 예산)

    s_items, s_status = _search(spec_kw, cid, secret)
    time.sleep(sleep)    # 네트워크 호출 후 저빈도 예의(ToS)
    if s_status != "ok" or not s_items:
        return 0, 0, 0
    s_rank, s_url = _find_mine(s_items, blog_id, key)
    if not s_rank:       # 특정 글 매칭 실패 → 키워드 대표 순위
        s_rank, s_url = _find_mine(s_items, blog_id, "")
    if not s_rank:
        return 0, 0, 0
    pdb.record_rank(key, spec_kw, s_rank, tab=pdb.SPECIFIC_TAB, day=today,
                    volume=spec_vol, estimated=estimated, data=data)
    reconciled = 0
    if s_url and not rec.get("url"):
        rec["url"] = s_url
        m = _LOGNO.search(s_url)
        if m and not rec.get("log_no"):
            rec["log_no"] = m.group(1)
        reconciled = 1
    near = 1 if 11 <= s_rank <= 30 else 0
    return 1, near, reconciled


def _norm(s: str) -> str:
    from core.performance_db import normalize_key
    return normalize_key(s)


def _search(keyword: str, cid: str, secret: str) -> tuple[list, str]:
    """블로그 검색 → (items, status). status: ok | auth | error."""
    qs = urllib.parse.urlencode({"query": keyword, "display": DISPLAY, "sort": "sim"})
    req = urllib.request.Request(
        f"{SEARCH_URL}?{qs}",
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return (data.get("items", []) or []), "ok"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            log.warning(f"[rank_tracker] 검색 API 인증 실패 {e.code} — "
                        f"api_client_id/secret 확인 필요")
            return [], "auth"
        log.warning(f"[rank_tracker] 검색 API HTTP {e.code} ({keyword[:20]})")
        return [], "error"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        log.debug(f"[rank_tracker] 검색 실패 ({keyword[:20]}): {e}")
        return [], "error"


def _find_mine(items: list[dict], blog_id: str,
               post_title_key: str = "") -> tuple[int, str]:
    """items 에서 내 블로그 글의 (순위, url) 찾기. 못 찾으면 (0, "").

    post_title_key 가 주어지면 제목이 일치하는 글의 순위(특정 글 랭킹).
    없으면 내 블로그의 최상위 글 순위(키워드 대표 랭킹).
    """
    blog_id_l = (blog_id or "").lower()
    for idx, it in enumerate(items, start=1):
        link = (it.get("link") or "") + " " + (it.get("bloggerlink") or "")
        if blog_id_l and blog_id_l not in link.lower():
            continue
        url = it.get("link") or ""
        if post_title_key:
            title_plain = _TAG.sub("", it.get("title") or "")
            if _norm(title_plain) == post_title_key:
                return idx, url
            continue  # 제목 매칭 모드면 내 글이라도 제목 다르면 통과
        return idx, url  # 대표 모드: 내 블로그 첫 등장
    return 0, ""


def check_rank(keyword: str, *, post_title: str = "") -> dict:
    """단일 키워드 순위 조회. {rank, url, found}. 자격증명 없으면 found=False."""
    cid, secret, blog_id = _creds()
    if not (cid and secret and blog_id and blog_id != "your_naver_id"):
        return {"rank": 0, "url": "", "found": False, "reason": "no_credentials"}
    items, status = _search(keyword, cid, secret)
    if status == "auth":
        return {"rank": 0, "url": "", "found": False, "reason": "auth_failed"}
    if not items:
        return {"rank": 0, "url": "", "found": False, "reason": "no_results"}
    tkey = _norm(post_title) if post_title else ""
    rank, url = _find_mine(items, blog_id, tkey)
    return {"rank": rank, "url": url, "found": rank > 0,
            "reason": "" if rank else "not_in_top100"}


def run_rank_tracking(*, max_keywords: int = 120, sleep: float = 0.25,
                      progress_cb=None) -> dict:
    """performance_db 의 글마다 primary 키워드 순위를 조회해 기록.

    - 특정 글 순위(제목 매칭) 우선, 매칭 실패 시 키워드 대표 순위로 폴백.
    - 검색 결과 url 로 글의 url/log_no reconcile.
    반환: {"checked", "ranked", "top10", "reconciled", "skipped"}.
    """
    from core import performance_db as pdb

    cid, secret, blog_id = _creds()
    if not (cid and secret and blog_id and blog_id != "your_naver_id"):
        log.warning("[rank_tracker] 자격증명 없음 — 순위 추적 스킵 (fail-open)")
        return {"checked": 0, "ranked": 0, "top10": 0, "reconciled": 0,
                "skipped": 0, "reason": "no_credentials"}

    data = pdb.load()
    if not data["posts"]:
        pdb.sync_from_history()
        data = pdb.load()

    from core import search_volume

    today = date.today().strftime("%Y-%m-%d")
    checked = ranked = top10 = reconciled = skipped = unmeasured = 0
    spec_ranked = near_miss = 0
    min_vol = _min_volume()
    has_volume_data = bool(search_volume.volume_index())
    track_specific, band_lo, band_hi = _specific_settings()

    # 오늘 아직 순위 기록 없는 글 우선 → 매일 조금씩 커버
    items = list(data["posts"].items())

    def _needs_today(rec: dict) -> bool:
        for hist in (rec.get("rank_history") or {}).values():
            if hist and hist[-1].get("date") == today:
                return False
        return True

    pending = [(k, r) for k, r in items if _needs_today(r)] or items

    for i, (key, rec) in enumerate(pending[:max_keywords]):
        title = rec.get("clean_title") or rec.get("title") or rec.get("keyword") or ""
        # #7: 제목 전체(30자 초-롱테일)를 순위로 세면 매칭 글이 자기 하나뿐 → 필연 1위(허영).
        #     제목에서 검색량 보유 헤드 키워드를 뽑아 그 키워드의 실제 경쟁 순위만 추적한다.
        if has_volume_data:
            kw, kw_volume = search_volume.best_keyword(title)
            if not kw or kw_volume < min_vol:
                unmeasured += 1               # 헤드는 순위 KPI 대상 아님(무수요)
                # 그러나 헤드가 무수요·저경쟁일수록 구체 롱테일 쿼리가 1페이지에 들
                # 확률이 높다 — 니어미스가 가장 절실한 글이므로 구체 추적은 헤드 볼륨
                # 게이트와 **독립**으로 실행한다(kw 가 비면 helper 가 알아서 스킵).
                if track_specific:
                    sr, nm, rc = _track_specific_for_post(
                        rec, title, kw, key, blog_id, cid, secret,
                        band_lo, band_hi, today, sleep, pdb, data)
                    spec_ranked += sr
                    near_miss += nm
                    reconciled += rc
                if progress_cb:
                    progress_cb(i + 1, min(len(pending), max_keywords), title[:20], 0)
                continue
        else:
            # 검색량 엑셀이 아예 없으면 구 동작 유지(볼륨 미상=0) — fail-open
            kw = rec.get("keyword") or rec.get("clean_title") or ""
            kw_volume = 0
            if not kw:
                skipped += 1
                continue
        checked += 1
        found, status = _search(kw, cid, secret)
        if status == "auth":
            log.warning("[rank_tracker] 인증 실패 — 추적 중단 (api_client_secret 확인)")
            return {"checked": checked, "ranked": ranked, "top10": top10,
                    "reconciled": reconciled, "skipped": skipped,
                    "unmeasured": unmeasured, "reason": "auth_failed"}
        rank, url = _find_mine(found, blog_id, key)
        if not rank:  # 특정 글 매칭 실패 → 키워드 대표 순위
            rank, url = _find_mine(found, blog_id, "")
        if rank:
            ranked += 1
            if rank <= 10:
                top10 += 1
            pdb.record_rank(key, kw, rank, tab="blog", day=today,
                            volume=kw_volume, data=data)
            if url and not rec.get("url"):
                rec["url"] = url
                m = _LOGNO.search(url)
                if m and not rec.get("log_no"):
                    rec["log_no"] = m.group(1)
                reconciled += 1
        if progress_cb:
            progress_cb(i + 1, min(len(pending), max_keywords), kw, rank)
        time.sleep(sleep)

        # 구체 롱테일 쿼리 순위(신규, tab=SPECIFIC_TAB) — 헤드가 잡힌 글도 병행 추적.
        if track_specific and has_volume_data:
            sr, nm, rc = _track_specific_for_post(
                rec, title, kw, key, blog_id, cid, secret,
                band_lo, band_hi, today, sleep, pdb, data)
            spec_ranked += sr
            near_miss += nm
            reconciled += rc

    pdb.save(data)
    log.info(f"[rank_tracker] 조회 {checked} · 노출 {ranked} · TOP10 {top10} · "
             f"url보정 {reconciled} · 미측정(무수요) {unmeasured} · "
             f"구체쿼리노출 {spec_ranked}(니어미스 {near_miss})")
    return {"checked": checked, "ranked": ranked, "top10": top10,
            "reconciled": reconciled, "skipped": skipped, "unmeasured": unmeasured,
            "specific_ranked": spec_ranked, "near_miss": near_miss}


def verify() -> dict:
    """검색 API 스코프 활성화 여부를 명확히 진단(값 노출 없음).

    - ok: 검색 API 200 → 순위 추적 가동 가능
    - scope: 401 Scope → 개발자센터에 '검색' API 미등록
    - auth: 기타 401/403 → 자격증명 재확인
    - no_credentials / no_network
    """
    cid, secret, blog_id = _creds()
    if not (cid and secret):
        print("❌ 자격증명 없음 — user_settings 의 api_client_id/secret 확인")
        return {"status": "no_credentials"}
    items, st = _search("네이버", cid, secret)
    if st == "ok":
        print("✅ 검색 API 200 OK — 스코프 활성화됨! 순위 추적 가동 가능: python -m core.rank_tracker")
        return {"status": "ok"}
    if st == "auth":
        print("⚠️ 401 Scope Status Invalid — 개발자센터에서 이 앱에 '검색' API 사용설정 필요\n"
              "   https://developers.naver.com → 내 애플리케이션 → API 설정 → '검색' 추가 → 저장\n"
              "   (저장 후 1~5분 전파. DataLab 은 되는데 검색만 막힌 상태 = 스코프 문제)")
        return {"status": "scope"}
    print("❓ 네트워크/기타 오류 — 잠시 후 재시도")
    return {"status": "no_network"}


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    if "--verify" in sys.argv or "verify" in sys.argv:
        verify()
    else:
        v = verify()
        if v.get("status") == "ok":
            print(run_rank_tracking(max_keywords=30))
