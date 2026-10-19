# core/deadline_tracker.py
# 신청 마감 임박 자동 감지 시스템
# 정부24 공고 RSS + 복지로 RSS를 매일 파싱해서
# 마감 2주 이내 지원사업을 엑셀 최상단으로 자동 이동합니다
from __future__ import annotations

import json, re, os, sys, urllib.request, urllib.error
from datetime import datetime, date, timedelta
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
log = get_logger(__name__)

BASE_DIR      = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

DEADLINE_DB   = _DATA_DIR / "logs" / "deadline_data.json"

# 마감 임박 기준 (일 단위)
URGENT_DAYS   = 3    # 3일 이내 → 즉시발행 + 제목에 D-N 뱃지
WARNING_DAYS  = 14   # 14일 이내 → 엑셀 최상단 이동


# ──────────────────────────────────────────────────────
# DB 관리
# ──────────────────────────────────────────────────────
def _load_db() -> list:
    from config import safe_load_json
    return safe_load_json(DEADLINE_DB, default=list)

def _save_db(data: list):
    from config import atomic_write_json
    atomic_write_json(DEADLINE_DB, data)


# ──────────────────────────────────────────────────────
# RSS 파서 (표준 라이브러리만 사용)
# ──────────────────────────────────────────────────────
def _parse_rss(url: str, timeout: int = 10) -> list[dict]:
    """RSS/Atom 피드를 파싱해서 [{title, link, pubDate, description}, ...] 반환"""
    items = []
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/145.0.0.0 Safari/537.36",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")

        # XML 응답 검증 (HTML 에러 페이지 방어)
        if '<rss' not in raw[:500] and '<?xml' not in raw[:500]:
            log.debug(f"RSS 응답이 XML이 아님 (스킵): {url[:50]}")
            return items

        # <item> 블록 추출
        for m in re.finditer(r'<item>([\s\S]*?)</item>', raw, re.IGNORECASE):
            block = m.group(1)
            def _tag(name):
                m2 = re.search(rf'<{name}[^>]*>([\s\S]*?)</{name}>', block, re.IGNORECASE)
                if m2:
                    txt = m2.group(1)
                    txt = re.sub(r'<[^>]+>', '', txt)       # HTML 태그 제거
                    txt = re.sub(r'&amp;', '&', txt)
                    txt = re.sub(r'&lt;', '<', txt)
                    txt = re.sub(r'&gt;', '>', txt)
                    txt = re.sub(r'&nbsp;', ' ', txt)
                    txt = re.sub(r'<!\[CDATA\[|\]\]>', '', txt)
                    return txt.strip()
                return ""
            items.append({
                "title":       _tag("title"),
                "link":        _tag("link"),
                "pubDate":     _tag("pubDate"),
                "description": _tag("description")[:400],
            })
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        log.debug(f"RSS 피드 접근 불가 ({url[:50]}): {e}")
    return items


# ──────────────────────────────────────────────────────
# 날짜 파싱
# ──────────────────────────────────────────────────────
_DATE_PATTERNS = [
    r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})',         # 2026.03.15 / 2026-03-15
    r'(\d{4})년\s*(\d{1,2})월\s*(\d{1,2})일',          # 2026년 3월 15일
    r'(\d{2})[.\-/](\d{2})[.\-/](\d{2})',              # 26.03.15
]

def _extract_deadline(text: str) -> date | None:
    """
    텍스트에서 신청 마감일을 추출합니다.
    "신청기간", "접수기간", "마감", "~" 이후의 날짜를 우선 파싱.
    """
    # 마감 구간: "2026.01.01 ~ 2026.03.15" → 뒷날짜가 마감
    range_m = re.search(
        r'(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})\s*[~～\-]\s*(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})',
        text
    )
    if range_m:
        try:
            return date(int(range_m.group(4)), int(range_m.group(5)), int(range_m.group(6)))
        except ValueError:
            pass

    # 마감/종료 키워드 주변 날짜
    for keyword in ["마감일", "마감", "종료", "접수마감", "신청마감", "신청기한", "까지"]:
        idx = text.find(keyword)
        if idx == -1:
            continue
        nearby = text[max(0, idx-40):idx+60]
        for pat in _DATE_PATTERNS:
            m = re.search(pat, nearby)
            if m:
                try:
                    y = int(m.group(1))
                    if y < 100:
                        y += 2000
                    d_val = date(y, int(m.group(2)), int(m.group(3)))
                    if d_val >= date.today():      # 미래 날짜만
                        return d_val
                except ValueError:
                    pass

    # 전체 텍스트에서 가장 가까운 미래 날짜
    future_dates = []
    for pat in _DATE_PATTERNS:
        for m in re.finditer(pat, text):
            try:
                y = int(m.group(1))
                if y < 100:
                    y += 2000
                d_val = date(y, int(m.group(2)), int(m.group(3)))
                if date.today() <= d_val <= date.today() + timedelta(days=365):
                    future_dates.append(d_val)
            except ValueError:
                pass
    if future_dates:
        return min(future_dates)   # 가장 가까운 미래 날짜

    return None


# ──────────────────────────────────────────────────────
# 공고 관련성 필터
# ──────────────────────────────────────────────────────
_WELFARE_KEYWORDS = [
    "지원금","보조금","장려금","바우처","급여","수당",
    "장학금","대출","보험료","혜택","지원","신청","혜택",
    "청년","복지","의료","돌봄","교육","주거","취업","고용",
]
_EXCLUDE_KEYWORDS = ["채용공고","입찰","물품","공사","용역","구매"]

def _is_welfare_notice(title: str, desc: str) -> bool:
    text = title + " " + desc
    if any(w in text for w in _EXCLUDE_KEYWORDS):
        return False
    return any(w in text for w in _WELFARE_KEYWORDS)


# ──────────────────────────────────────────────────────
# 수집 소스들 (전부 fail-open — 하나가 죽어도 나머지로 계속)
# ──────────────────────────────────────────────────────
RSS_SOURCES = [
    # 대한민국 정책포털 정책정보 RSS — ⚠️ 2026-07-19 실측 404(경로 폐지 추정).
    # 복구될 수 있어 fail-open 으로 남겨둔다(요청 1회 비용뿐).
    "https://www.korea.kr/rss/policy.xml",
    # ── 아래 피드는 2026년 기준 서비스 종료/URL 변경됨 ──
    # "https://www.gov.kr/portal/rcvfvrSvc/listAllBenefitService.do?rss=Y",  # gov.kr — 에러 페이지 반환
    # "https://www.bokjiro.go.kr/ssis-tbu/RSS/twataa009m/index.do",          # 복지로 — 404
    # "https://www.work24.go.kr/cm/c/d/cmCDA000List.do?rss=true",            # 고용24 — 404
]

# ── 소스 2: 복지로 복지서비스 목록 (키리스 JSON, 2026-07-19 실측 200 OK) ──
# SPA 가 쓰는 공개 POST 엔드포인트. ENFC_END_YMD(시행 종료일)가 구조화 필드로 와서
# 날짜 파싱이 필요 없다 — 종료 임박 제도 = "지금 써야 할 마감 임박 글감".
_BOKJIRO_URL = ("https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/"
                "selectWlfareInfo.do")
_BOKJIRO_DETAIL = ("https://www.bokjiro.go.kr/ssis-tbu/twataa/wlfareInfo/"
                   "moveTWAT52011M.do?wlfareInfoId={}")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36")


def _parse_ymd(s) -> date | None:
    """'20261231' → date. 1970(무기한 placeholder)·9999(상시)는 None."""
    s = str(s or "").strip()
    if not re.fullmatch(r"\d{8}", s):
        return None
    try:
        d = date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None
    if d.year <= 1970 or d.year >= 9999:
        return None
    return d


def _bokjiro_page(page: int, timeout: int = 12) -> dict | None:
    """복지로 목록 1페이지 JSON. 실패는 None(호출측 스킵)."""
    body = json.dumps({
        "dmSearchParam": {
            "page": str(page), "onlineYn": "", "searchTerm": "", "tabId": "1",
            "orderBy": "date", "bkjrLftmCycCd": "", "daesang": "", "period": "",
            "age": "", "region": "", "jjim": "", "subject": "",
            "favoriteKeyword": "", "sido": "", "gungu": "",
        },
        "dmScr": {"curScrId": "tbu/app/twat/twata/twataa/TWAT52005M",
                  "befScrId": ""},
    }).encode("utf-8")
    req = urllib.request.Request(
        _BOKJIRO_URL, data=body,
        headers={"User-Agent": _UA, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:  # noqa: BLE001 — 네트워크·파싱 전부 조용히 스킵
        log.debug(f"복지로 목록 조회 실패 (p{page}): {e}")
        return None


def _fetch_bokjiro(pages: int = 3, page_fetch=None) -> list[dict]:
    """복지로 복지서비스 → [{title, description, link, end_date}] (end_date: date|None)."""
    out, seen = [], set()
    for page in range(1, pages + 1):
        data = (page_fetch or _bokjiro_page)(page)
        if not isinstance(data, dict):
            break
        found_any = False
        for key in ("dsServiceList0", "dsServiceList1",
                    "dsServiceList2", "dsServiceList3"):
            for it in (data.get(key) or []):
                wid = (it.get("WLFARE_INFO_ID") or "").strip()
                if not wid or wid in seen:
                    continue
                seen.add(wid)
                found_any = True
                out.append({
                    "title": (it.get("WLFARE_INFO_NM") or "").strip(),
                    "description": (it.get("WLFARE_INFO_OUTL_CN") or "")[:400],
                    "link": _BOKJIRO_DETAIL.format(wid),
                    "end_date": _parse_ymd(it.get("ENFC_END_YMD")),
                })
        if not found_any:
            break
    return out


# ── 소스 3: 보조금24 공공서비스 API (선택 — data.go.kr 무료 키) ──
# 키 없이 호출 시 401(2026-07-19 실측 = 엔드포인트 생존). '신청기한' 텍스트 필드 보유.
_GOV24_URL = "https://api.odcloud.kr/api/gov24/v3/serviceList"


def _gov24_key() -> str:
    k = os.environ.get("GOV24_API_KEY", "")
    if k:
        return k
    try:
        from config import settings as _s
        return str(getattr(_s, "GOV24_API_KEY", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _gov24_qs(page: int, per_page: int, key: str) -> str:
    """쿼리스트링 — data.go.kr '인코딩키'(% 포함)는 재인코딩하면 깨지므로 그대로."""
    import urllib.parse as _up
    base = _up.urlencode({"page": page, "perPage": per_page})
    if "%" in key:
        return f"{base}&serviceKey={key}"
    return f"{base}&{_up.urlencode({'serviceKey': key})}"


def _fetch_gov24(per_page: int = 300, fetch=None, timeout: int = 15) -> list[dict]:
    """보조금24 서비스 목록 → [{title, description, link, end_date=None}]. 키 없으면 []."""
    key = _gov24_key()
    if not key:
        return []
    if fetch is not None:
        rows = fetch(per_page)
    else:
        req = urllib.request.Request(
            f"{_GOV24_URL}?{_gov24_qs(1, per_page, key)}",
            headers={"User-Agent": _UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                rows = (json.loads(resp.read()).get("data") or [])
        except Exception as e:  # noqa: BLE001
            log.debug(f"보조금24 API 조회 실패: {e}")
            return []
    out = []
    for it in rows or []:
        # odcloud 는 한글 필드명 — 영문 변형도 방어적으로 수용
        title = (it.get("서비스명") or it.get("svcNm") or "").strip()
        if not title:
            continue
        period = (it.get("신청기한") or it.get("aplyPrdCn") or "")
        summary = (it.get("서비스목적요약") or it.get("지원내용")
                   or it.get("svcPurpsSumry") or "")
        svc_id = (it.get("서비스ID") or it.get("svcId") or "")
        out.append({
            "title": title,
            "description": (f"{period} {summary}")[:400],
            "link": (f"https://www.gov.kr/portal/rcvfvrSvc/dtlEx/{svc_id}"
                     if svc_id else "https://www.gov.kr"),
            "end_date": None,      # 신청기한은 텍스트 → _extract_deadline 로
        })
    return out

# ── 소스 4: 네이버 뉴스 검색 (뉴스 API P1-a, 2026-07-19) ──
# 뉴스 기사에서 '신청 마감' 공고를 발굴 — RSS 전멸 상태의 가장 살아있는 소스.
# news_radar 의 자격증명·검색·예산 인프라를 그대로 재사용(호출 예산 합산 관리).
# 이 함수는 fetch_deadlines 안에서만 불리고, fetch_deadlines 자체가 opt-in
# 프로필(brand.deadline_enabled)에서만 호출되므로 정부지원 어휘가 비정부
# 프로필로 새지 않는다.
_NEWS_QUERIES = [
    "지원금 신청 마감", "지원사업 접수 마감", "복지 신청 기간",
    "장려금 신청", "바우처 신청 마감", "정부 지원 접수",
]

def _fetch_news_deadlines(search=None, sleep: float = 0.15) -> list[dict]:
    """뉴스 검색으로 마감 공고 후보 수집. 자격증명·예산 없으면 [] (fail-open)."""
    import time as _time
    from core import news_radar
    if not news_radar._enabled():
        return []
    cid, secret = news_radar._creds()
    if not (cid and secret):
        return []
    if news_radar.budget_left() < len(_NEWS_QUERIES):
        return []
    search = search or (lambda q: news_radar._search_news(q, cid, secret))
    out, calls = [], 0
    for q in _NEWS_QUERIES:
        items, status = search(q)
        calls += 1
        if status == "auth":
            break
        for it in items:
            out.append({
                "title": news_radar._plain(it.get("title", "")).strip(),
                "description": news_radar._plain(it.get("description", "")),
                "link": it.get("originallink") or it.get("link") or "",
            })
        _time.sleep(sleep)
    try:
        news_radar.consume_budget(calls)
    except Exception as e:  # noqa: BLE001 — 예산 기록 실패가 수집을 막지 않게
        log.debug(f"뉴스 예산 기록 실패(무시): {e}")
    return out


def _build_item(title: str, desc: str, link: str, source: str,
                deadline: date, today: date) -> dict | None:
    """공통 아이템 생성 — 60일 초과/과거 마감은 None."""
    days_left = (deadline - today).days
    if days_left < 0 or days_left > 60:
        return None
    urgency = (
        "urgent"  if days_left <= URGENT_DAYS  else
        "warning" if days_left <= WARNING_DAYS else
        "normal"
    )
    keyword = re.sub(r'\s*(공고|안내|알림|발표|결과)\s*$', '', title).strip()
    keyword = re.sub(r'^\d{4}년?\s*', '', keyword).strip()
    if len(keyword) > 25:
        keyword = keyword[:24]
    return {
        "title":     title,
        "deadline":  deadline.strftime("%Y-%m-%d"),
        "days_left": days_left,
        "urgency":   urgency,
        "link":      link,
        "source":    source,
        "keyword":   keyword,
        "raw_desc":  desc[:200],
    }


def fetch_deadlines(progress_cb=None) -> list[dict]:
    """
    마감 임박 공고·제도를 4개 소스에서 수집합니다(각 소스 fail-open).

      ① RSS(레거시 — 2026-07 기준 대부분 폐지, 복구 대비 유지)
      ② 복지로 복지서비스 목록(키리스 JSON) — 시행 종료 임박 제도
      ③ 보조금24 공공서비스 API(선택 — GOV24_API_KEY 있을 때만) — 신청기한
      ④ 네이버 뉴스 검색(검색 API 자격증명 있을 때만) — '신청 마감' 보도

    Returns: [{
        "title":      str,
        "deadline":   "YYYY-MM-DD",
        "days_left":  int,
        "urgency":    "urgent"|"warning"|"normal",
        "link":       str,
        "source":     str,
        "keyword":    str,   # 블로그 키워드로 쓸 수 있는 짧은 제목
    }]
    """
    all_items = []
    seen_titles = set()
    today = date.today()
    total_steps = len(RSS_SOURCES) + 3      # + 복지로 + 보조금24 + 뉴스

    def _add(title: str, desc: str, link: str, source: str, deadline: date):
        tkey = re.sub(r"\s+", "", title)
        if not tkey or tkey in seen_titles:
            return
        item = _build_item(title, desc, link, source, deadline, today)
        if item:
            seen_titles.add(tkey)
            all_items.append(item)

    # ── ① RSS (레거시) ──
    for i, url in enumerate(RSS_SOURCES):
        if progress_cb:
            progress_cb(f"RSS 수집 중 ({i+1}/{len(RSS_SOURCES)})...",
                        i + 1, total_steps)
        items = _parse_rss(url)
        source_name = url.split("/")[2]       # 도메인 추출
        for item in items:
            title = item.get("title", "").strip()
            desc  = item.get("description", "") + " " + title
            if not title or not _is_welfare_notice(title, desc):
                continue
            deadline = _extract_deadline(desc + " " + title)
            if deadline:
                _add(title, desc, item.get("link", ""), source_name, deadline)

    # ── ② 복지로 (키리스 JSON — 종료일 구조화 필드) ──
    if progress_cb:
        progress_cb("복지로 수집 중...", len(RSS_SOURCES) + 1, total_steps)
    try:
        for item in _fetch_bokjiro():
            title = item["title"]
            desc = f"{item['description']} {title}"
            if not title or any(w in desc for w in _EXCLUDE_KEYWORDS):
                continue    # 복지로는 전 항목이 복지라 welfare 필터는 불필요
            deadline = item["end_date"] or _extract_deadline(desc)
            if deadline:
                _add(title, desc, item["link"], "bokjiro.go.kr", deadline)
    except Exception as e:  # noqa: BLE001 — 소스 하나의 실패가 전체를 막지 않게
        log.warning(f"복지로 수집 실패(무시): {e}")

    # ── ③ 보조금24 API (선택 — 키 있을 때만) ──
    if progress_cb:
        progress_cb("보조금24 수집 중...", len(RSS_SOURCES) + 2, total_steps)
    try:
        for item in _fetch_gov24():
            title = item["title"]
            desc = f"{item['description']} {title}"
            if not _is_welfare_notice(title, desc):
                continue
            deadline = _extract_deadline(desc)
            if deadline:
                _add(title, desc, item["link"], "gov.kr(보조금24)", deadline)
    except Exception as e:  # noqa: BLE001
        log.warning(f"보조금24 수집 실패(무시): {e}")

    # ── ④ 네이버 뉴스 검색 (자격증명 있을 때만 — '신청 마감' 보도) ──
    if progress_cb:
        progress_cb("뉴스 수집 중...", len(RSS_SOURCES) + 3, total_steps)
    try:
        for item in _fetch_news_deadlines():
            title = item["title"]
            desc = f"{item['description']} {title}"
            if not title or not _is_welfare_notice(title, desc):
                continue
            deadline = _extract_deadline(desc)
            if deadline:
                _add(title, desc, item["link"], "news.naver.com", deadline)
    except Exception as e:  # noqa: BLE001
        log.warning(f"뉴스 마감 수집 실패(무시): {e}")

    # 마감 가까운 순 정렬
    all_items.sort(key=lambda x: x["days_left"])

    # DB 저장
    _save_db(all_items)

    if progress_cb:
        progress_cb("완료", total_steps, total_steps)

    urgent  = sum(1 for x in all_items if x["urgency"] == "urgent")
    warning = sum(1 for x in all_items if x["urgency"] == "warning")
    log.info(f"마감 공고 수집: {len(all_items)}건 "
             f"(긴급 {urgent}건 / 경고 {warning}건)")
    return all_items


def verify_gov24_with(key: str, timeout: int = 12) -> dict:
    """명시적 키로 1건 조회(GUI 연결 테스트용 — 저장 없이 즉시).

    ⚠️ 반환 dict·로그 어디에도 키 값을 담지 않는다.
    Returns: {"ok": bool, "reason": str}
    """
    key = (key or "").strip()
    if not key:
        return {"ok": False, "reason": "인증키를 입력하세요."}
    req = urllib.request.Request(f"{_GOV24_URL}?{_gov24_qs(1, 1, key)}",
                                 headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read())
        total = (data.get("totalCount") or data.get("matchCount")
                 or len(data.get("data") or []))
        return {"ok": True,
                "reason": f"연결 성공 — 공공서비스 {int(total):,}건 조회 가능"}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"ok": False,
                    "reason": "인증 실패(401/403) — data.go.kr 활용신청 승인 여부와 "
                              "'일반 인증키' 복사가 맞는지 확인하세요."}
        return {"ok": False, "reason": f"HTTP {e.code} — 잠시 후 재시도"}
    except (json.JSONDecodeError, ValueError):
        return {"ok": False, "reason": "응답 파싱 실패 — 잠시 후 재시도"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"네트워크 오류: {type(e).__name__}"}


def verify_sources() -> dict:
    """소스별 생존 진단(값 노출 없음) — {rss, bokjiro, gov24}."""
    out = {}
    rss_n = sum(len(_parse_rss(u)) for u in RSS_SOURCES)
    out["rss"] = f"{rss_n}건" if rss_n else "0건(피드 폐지/변경 — 레거시)"
    try:
        bj = _fetch_bokjiro(pages=1)
        dated = sum(1 for b in bj if b["end_date"])
        out["bokjiro"] = f"{len(bj)}건 조회(종료일 보유 {dated}건)"
    except Exception as e:  # noqa: BLE001
        out["bokjiro"] = f"실패: {e}"
    if not _gov24_key():
        out["gov24"] = ("키 없음 — data.go.kr 에서 '보조금24 서비스 목록' 활용신청(무료) 후 "
                        "환경변수 GOV24_API_KEY 설정 시 활성")
    else:
        n = len(_fetch_gov24(per_page=30))
        out["gov24"] = f"{n}건 조회" if n else "키는 있으나 0건 — 키 승인 상태 확인"
    for k, v in out.items():
        print(f"  {k:8s}: {v}")
    return out


# ──────────────────────────────────────────────────────
# 엑셀 우선 배치
# ──────────────────────────────────────────────────────
def inject_urgent_to_excel(excel_path: str, sheet_name: str,
                            deadlines: list[dict] = None) -> int:
    """
    마감 임박 키워드를 엑셀 최상단(A1부터)에 삽입합니다.
    이미 있는 키워드는 건너뜁니다.
    Returns: 삽입된 개수
    """
    from openpyxl import load_workbook
    from openpyxl.styles import PatternFill, Font

    RED_FILL    = PatternFill(start_color="FFCCCC", end_color="FFCCCC", fill_type="solid")
    ORANGE_FILL = PatternFill(start_color="FFE4B5", end_color="FFE4B5", fill_type="solid")
    BOLD_FONT   = Font(bold=True)

    if deadlines is None:
        deadlines = _load_db()
    if not deadlines:
        return 0

    if not os.path.exists(excel_path):
        log.error(f"엑셀 없음: {excel_path}")
        return 0

    wb = load_workbook(excel_path)
    if sheet_name not in wb.sheetnames:
        log.error(f"시트 없음: {sheet_name}")
        return 0
    ws = wb[sheet_name]

    # 기존 키워드 목록 (중복 방지)
    existing = set()
    for row in ws.iter_rows():
        for cell in row:
            if cell.value:
                existing.add(str(cell.value))

    today_str = date.today().strftime("%Y-%m-%d")
    to_insert = []
    for item in deadlines:
        if item.get("urgency") not in ("urgent", "warning"):
            continue
        kw       = item.get("keyword", "").strip()
        days_left = item.get("days_left", 999)
        if not kw:
            continue
        # 제목 구성: 마감 임박 표시 포함
        if days_left <= URGENT_DAYS:
            entry = f"{today_str} — {kw} (D-{days_left} 마감임박)"
        else:
            entry = f"{today_str} — {kw}"
        if entry in existing:
            continue
        to_insert.append((entry, item["urgency"]))

    if not to_insert:
        log.info("삽입할 마감 임박 키워드 없음")
        return 0

    # 기존 행을 아래로 밀고 최상단에 삽입
    ws.insert_rows(1, amount=len(to_insert))
    for i, (entry, urgency) in enumerate(to_insert, 1):
        cell = ws.cell(row=i, column=1, value=entry)
        cell.fill  = RED_FILL if urgency == "urgent" else ORANGE_FILL
        cell.font  = BOLD_FONT
        log.info(f"최상단 삽입: {entry[:50]}")

    wb.save(excel_path)
    log.info(f"{len(to_insert)}개 마감 임박 키워드 엑셀 최상단에 삽입")
    return len(to_insert)


# ──────────────────────────────────────────────────────
# 로드된 DB에서 요약 반환 (GUI용)
# ──────────────────────────────────────────────────────
def get_deadline_summary() -> dict:
    items    = _load_db()
    today    = date.today()
    urgent   = [x for x in items if x.get("urgency") == "urgent"]
    warning  = [x for x in items if x.get("urgency") == "warning"]
    return {
        "urgent":       urgent,
        "warning":      warning,
        "total":        len(items),
        "last_updated": items[0].get("fetched_at", "") if items else "",
    }


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # cp949 콘솔 방어
    except AttributeError:
        pass
    print("── 소스 진단 ──")
    verify_sources()
    print("── 수집 실행 ──")
    items = fetch_deadlines()
    for it in items[:15]:
        print(f"  D-{it['days_left']:>2} {it['deadline']} [{it['source']}] {it['title'][:36]}")
    if len(items) > 15:
        print(f"  … 외 {len(items) - 15}건")
