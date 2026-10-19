# core/stats_fetcher.py — ⑥ 방문자·글별 유입 성과 수집 (Phase 1 · L1 수리)
"""블로그 전체 방문자 + 글별 조회수 + 유입 검색어를 실측해 performance_db 에 적재.

수집 경로 (우선순위·fail-open):
1. 방문자 총계 — 세션쿠키가 있으면 붙여서 다중 엔드포인트 시도, 없으면 익명 시도.
   (기존 익명 NVisitBlog 스크래퍼가 전부 0을 반환하던 문제 → 세션+다중경로로 보강)
2. 글별 조회수·유입검색어 — (a) 세션 기반 크리에이터 통계 훅(엔드포인트 확인되면 활성),
   (b) **수동 인제스트**: 네이버 블로그 '통계' 내보내기(CSV/XLSX)를 ingest_stats_export()
   로 적재. 라이브 훅이 계정별로 확정되기 전까지 (b)가 보장된 경로.

부수효과: 네트워크 읽기 + performance_db/blog_stats.json 쓰기. 발행·변경 없음.
"""

import csv
import html as _html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger
try:
    from config.settings import HTTP_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    HTTP_REQUEST_TIMEOUT = 10
log = get_logger(__name__)
BASE_DIR      = Path(__file__).parent.parent
from config import profile as _nbaprofile
_DATA_DIR = _nbaprofile.app_data_root()

STATS_DB_PATH = _DATA_DIR / "logs" / "blog_stats.json"


def _cookie_path():
    """세션 쿠키 경로 — 활성 프로필별(naver_uploader 와 동일 소스). default 는 레거시
    _runtime/session_cookies.json 와 바이트 동일. 호출 시점 해석."""
    from config import profile as _p
    return _p.cookie_path()

# NVisitorgp4Ajax XML: <visitorcnt id="20260703" cnt="169" />
_VISITOR_DAILY_RE = re.compile(r'<visitorcnt\s+id="(\d{8})"\s+cnt="(\d+)"')


def _load_stats_db() -> dict:
    from config import safe_load_json
    return safe_load_json(STATS_DB_PATH,
                          default=lambda: {"visitors": {}, "posts": [], "keywords": {}})

def _save_stats_db(data: dict):
    from config import atomic_write_json
    atomic_write_json(STATS_DB_PATH, data)


def _cookie_header() -> str:
    """저장된 셀레니움 세션쿠키 → HTTP Cookie 헤더 문자열. 없으면 빈 문자열."""
    from config import safe_load_json
    cookies = safe_load_json(_cookie_path(), default=list)
    if not isinstance(cookies, list) or not cookies:
        return ""
    parts = []
    for c in cookies:
        name, val = c.get("name"), c.get("value")
        dom = (c.get("domain") or "").lower()
        if name and val is not None and ("naver" in dom or not dom):
            parts.append(f"{name}={val}")
    return "; ".join(parts)


# ─────────────────────────────────────────────────────
class NaverBlogStats:
    """네이버 블로그 통계 수집 (세션쿠키 우선, 익명 폴백)."""
    def __init__(self, blog_id: str, access_token: str = "", cookie: str = ""):
        self.blog_id      = blog_id
        self.access_token = access_token
        self.cookie       = cookie or _cookie_header()

    def _headers(self) -> dict:
        h = {
            "User-Agent":  ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
            "Referer":     f"https://m.blog.naver.com/{self.blog_id}",
            "Accept":      "application/json, text/plain, */*",
        }
        if self.access_token:
            h["Authorization"] = f"Bearer {self.access_token}"
        if self.cookie:
            h["Cookie"] = self.cookie
        return h

    def _get(self, url: str) -> str:
        req = urllib.request.Request(url, headers=self._headers())
        with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    def get_daily_visitors(self) -> dict:
        """일별 방문자 {YYYY-MM-DD: count} — NVisitorgp4Ajax(최근 ~5일 XML).

        본인 블로그 로그인 세션쿠키가 살아 있으면 실데이터를 준다(익명·스테일이면 빈 dict).
        형식: <visitorcnt id="20260703" cnt="169" />. 다른 방문자 엔드포인트가 전부 0을
        주던 실측 문제를 해결한 경로(2026-07-04 탐침으로 확인).
        """
        url = f"https://blog.naver.com/NVisitorgp4Ajax.naver?blogId={self.blog_id}"
        try:
            text = self._get(url)
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            log.debug(f"일별 방문자 조회 실패: {e}")
            return {}
        out = {}
        for ymd, cnt in _VISITOR_DAILY_RE.findall(text):
            out[f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"] = int(cnt)
        return out

    def get_visitor_count(self) -> dict:
        """오늘/어제/누적 방문자 수. 일별(NVisitorgp4Ajax) 우선, 실패 시 다중 폴백."""
        # 1) 일별 방문자 — 본인 세션이면 실데이터. today/yesterday 를 여기서 뽑는다.
        daily = self.get_daily_visitors()
        if daily:
            days = sorted(daily)
            return {"today": daily[days[-1]],
                    "yesterday": daily[days[-2]] if len(days) >= 2 else 0,
                    "total": 0,  # 누적은 이 엔드포인트에 없음(일별만)
                    "_source": "NVisitorgp4Ajax", "_daily": daily}
        # 2) 폴백 — 기존 다중 엔드포인트(익명/스테일이면 0)
        candidates = [
            f"https://blog.naver.com/NVisitBlog.nhn?blogId={self.blog_id}",
            f"https://m.blog.naver.com/rego/RealTimeStatistics?blogId={self.blog_id}",
            f"https://m.blog.naver.com/{self.blog_id}",
        ]
        patterns = {
            "today":     [r'"todayVisitCount":(\d+)', r'todayCount["\s:]+(\d+)',
                          r'오늘\s*방문자?\s*(\d[\d,]*)'],
            "yesterday": [r'"yesterdayVisitCount":(\d+)', r'yesterdayCount["\s:]+(\d+)'],
            "total":     [r'"totalVisitCount":(\d+)', r'totalCount["\s:]+(\d+)',
                          r'전체\s*방문자?\s*(\d[\d,]*)'],
        }
        for url in candidates:
            try:
                text = self._get(url)
            except (urllib.error.URLError, TimeoutError, ValueError) as e:
                log.debug(f"방문자 조회 실패({url[:40]}): {e}")
                continue
            out, hit = {}, False
            for field, pats in patterns.items():
                out[field] = 0
                for p in pats:
                    m = re.search(p, text)
                    if m:
                        out[field] = int(m.group(1).replace(",", ""))
                        hit = True
                        break
            if hit and any(out.values()):
                out["_source"] = url.split("//")[-1].split("/")[0]
                return out
        return {"today": 0, "yesterday": 0, "total": 0}

    def get_post_list(self, count: int = 30, *, page: int = 1) -> list:
        """최근 발행 포스트 목록 (log_no + title). url reconcile 용.

        네이버가 실제 쓰는 PostTitleListAsync.naver JSON 엔드포인트 사용.
        (구 PostList.nhn 은 JS 렌더라 스크래핑 불가.) 네이버 JSON 은 이스케이프가
        깨져 json.loads 가 실패하므로 정규식으로 logNo+title 추출.
        """
        try:
            raw = self._get(
                f"https://blog.naver.com/PostTitleListAsync.naver?blogId={self.blog_id}"
                f"&viewdate=&currentPage={page}&categoryNo=0&countPerPage={count}")
            posts = []
            for lg, ti in re.findall(r'"logNo":"(\d+)"[^}]*?"title":"([^"]*)"', raw):
                title = _html.unescape(urllib.parse.unquote(ti).replace("+", " ")).strip()
                if title:
                    posts.append({"log_no": lg, "title": title})
            return posts
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            log.debug(f"포스트 목록 조회 실패: {e}")
            return []

    def get_weekly_visitors(self) -> list:
        db = _load_stats_db()
        visitors = db.get("visitors", {})
        return [{"date": (date.today() - timedelta(days=i)).strftime("%Y-%m-%d"),
                 "count": visitors.get((date.today() - timedelta(days=i))
                                       .strftime("%Y-%m-%d"), {}).get("today", 0)}
                for i in range(6, -1, -1)]


# ─────────────────────────────────────────────────────
# 글별 유입 — 세션 훅(라이브) + 수동 인제스트(보장)
# ─────────────────────────────────────────────────────
def collect_post_stats_via_session(blog_id: str, cookie: str = "") -> list:
    """세션 기반 글별 조회수·유입검색어 수집(라이브 훅).

    크리에이터 통계 엔드포인트는 계정별로 응답 shape 이 다를 수 있어, JSON 이 예상
    구조일 때만 파싱하고 아니면 빈 리스트(사일런트 실패 아님 — 로그로 표기).
    반환: [{"title","log_no","views","referrals":{kw:cnt}}, ...]
    """
    cookie = cookie or _cookie_header()
    if not cookie:
        log.info("[stats] 세션쿠키 없음 → 글별 세션 수집 스킵 (수동 인제스트 사용)")
        return []
    # 후보 엔드포인트 — 확인되면 파서 추가. 현재는 방어적으로 shape 검사만.
    candidates = [
        f"https://creator-advisor.naver.com/api/v6/summary/blog?blogId={blog_id}",
    ]
    hdr = {"User-Agent": "Mozilla/5.0", "Cookie": cookie,
           "Referer": "https://creator-advisor.naver.com/", "Accept": "application/json"}
    for url in candidates:
        try:
            req = urllib.request.Request(url, headers=hdr)
            with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
                payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
            log.debug(f"[stats] 세션 수집 후보 실패({url[:50]}): {e}")
            continue
        rows = _parse_creator_payload(payload)
        if any(r.get("views") is not None for r in rows):
            log.info(f"[stats] 세션 수집 성공 — {len(rows)}편")
            return rows
    log.info("[stats] 세션 수집 응답이 예상 구조 아님 → 수동 인제스트 권장")
    return []


def _parse_creator_payload(payload, *, collected_at: str = "") -> list:
    """크리에이터 통계 JSON → **정규화된** 표준 행(core.metrics_contract 계약).

    구조는 여전히 미확정이라 관대하게 탐색하지만, 값은 관대하게 받지 않는다.
      · 원격 글 번호(log_no)가 없는 행은 **버린다** — 제목만으로 성과를 귀속하지 않는다.
      · 조회수는 결측/무효/진짜 0 을 구분한다(`views=None` 은 0 이 아니다).
      · 이 경로의 숫자가 누적인지 기간인지 **확인된 바 없으므로** basis=unknown 이다
        → 기록은 되지만 비교·환류 입력으로는 쓰이지 않는다(measurement 쪽 게이트).
    반환 행은 기존 키(title/log_no/views/referrals)를 모두 유지한다(하위호환).
    """
    from core import metrics_contract as mc

    rows = []
    data = payload.get("result") or payload.get("data") or payload if isinstance(payload, dict) else payload
    seq = data if isinstance(data, list) else (data.get("posts") if isinstance(data, dict) else None)
    if not isinstance(seq, list):
        return []
    for it in seq:
        if not isinstance(it, dict):
            continue
        norm = mc.normalize_metric_row(it, source="creator_advisor",
                                       collected_at=collected_at,
                                       basis=mc.BASIS_UNKNOWN)
        if not norm["log_no"]:
            continue    # 식별자 없는 행은 어느 글의 수치인지 알 수 없다
        rows.append({"title": norm["title"], "log_no": norm["log_no"],
                     "views": norm["views"], "views_status": norm["views_status"],
                     "basis": norm["basis"], "identity": norm["identity"],
                     "source": norm["source"], "collected_at": norm["collected_at"],
                     "referrals": {}})
    return rows


# 제목 토큰화 — 발행 시 제목이 초안과 달라져도(같은 글) 매칭되도록 토큰 Jaccard 용.
_TOKEN_SPLIT = re.compile(r"[\s·,\.\?!()\[\]'\"“”‘’%~\-/:;·|]+")


def _tokenize(title: str) -> set:
    out = set()
    for t in _TOKEN_SPLIT.split((title or "").lower()):
        t = t.strip()
        if len(t) >= 2 or t.isdigit():
            out.add(t)
    return out


def _fuzzy_best_key(title: str, db_index: list, *, threshold: float = 0.5,
                    margin: float = 0.08) -> str:
    """토큰 Jaccard 로 가장 비슷한 DB 키를 찾는다(발행 제목 ↔ 초안 제목 불일치 대비).

    db_index: [(key, tokenset), ...]. 최고 유사도가 threshold 이상이고 2위와 margin 이상
    벌어질 때만 채택(같은 토픽의 다른 글로 오매칭되는 것을 방지).
    """
    q = _tokenize(title)
    if not q:
        return ""
    best_k, best_s, second = "", 0.0, 0.0
    for k, ts in db_index:
        inter = len(q & ts)
        if not inter:
            continue
        j = inter / len(q | ts)
        if j > best_s:
            second, best_s, best_k = best_s, j, k
        elif j > second:
            second = j
    return best_k if (best_s >= threshold and best_s - second >= margin) else ""


def ingest_stats_export(path: str, *, day: str = "") -> dict:
    """네이버 블로그 '통계' 내보내기(CSV/XLSX)를 performance_db 에 적재 — 보장 경로.

    헤더로 컬럼 자동 감지:
      제목/title, 조회수/views/조회, 검색어/유입검색어/keyword, 유입/방문/visits.
    - 조회수 컬럼 있는 행 → record_views (제목 매칭: 정확→부분→토큰 퍼지)
    - 검색어 컬럼 있는 행 → record_referral
    반환: {"views_rows","referral_rows","matched","unmatched"}.
    """
    from core import performance_db as pdb

    p = Path(path)
    if not p.exists():
        return {"error": f"파일 없음: {path}"}
    rows = _read_tabular(p)
    if not rows:
        return {"error": "행 없음/파싱 실패"}

    data = pdb.load()
    keymap = {k: k for k in data["posts"]}  # normalized key set
    # 토큰 인덱스(퍼지 매칭용) — 발행 제목이 초안과 달라진 글도 잡는다.
    db_index = [(k, _tokenize(rec.get("clean_title") or rec.get("title") or ""))
                for k, rec in data["posts"].items()]
    day = day or date.today().strftime("%Y-%m-%d")
    v_rows = r_rows = matched = unmatched = 0

    from core import metrics_contract as mc

    for row in rows:
        title = _pick(row, ("제목", "title", "글제목", "콘텐츠"))
        kw    = _pick(row, ("유입검색어", "검색어", "keyword", "쿼리", "query"))
        # 수치는 단일 정규화 계약을 쓴다 — 부분 숫자("2026년 집계")가 조회수로
        # 둔갑하거나 결측이 0 으로 적히지 않도록(`_num` 의 느슨한 추출을 여기선 쓰지 않는다).
        views, _vs = mc.coerce_count(_pick(row, ("조회수", "조회", "views", "read")))
        views = views or 0
        # 방문/유입 카운트 — '유입검색어' 컬럼과의 부분문자열 충돌을 exclude 로 차단
        visits, _hs = mc.coerce_count(
            _pick(row, ("유입수", "방문수", "방문", "visits", "유입건수", "건수"),
                  exclude=("검색어", "제목", "keyword")))
        visits = visits or 0
        key = pdb.normalize_key(title) if title else ""
        if key not in data["posts"]:
            # 1) 부분 문자열 → 2) 토큰 퍼지(발행 제목 수정 대비)
            sub = next((k for k in keymap if key and (key in k or k in key)), "")
            key = sub or _fuzzy_best_key(title, db_index)
        if key and key in data["posts"]:
            matched += 1
        else:
            unmatched += 1
            key = ""
        if views and key:
            pdb.record_views(key, views, day=day, data=data)
            v_rows += 1
        if kw and (key or True):
            # 검색어는 글 매칭이 안 돼도 글로벌 수요 신호로 가치 → 매칭될 때만 기록
            if key:
                pdb.record_referral(key, kw, visits or 1, data=data)
                r_rows += 1

    pdb.save(data)
    log.info(f"[stats] 인제스트 완료 — 조회수 {v_rows}행 · 검색어 {r_rows}행 "
             f"· 매칭 {matched}/{matched + unmatched}")
    return {"views_rows": v_rows, "referral_rows": r_rows,
            "matched": matched, "unmatched": unmatched}


def _read_tabular(p: Path) -> list:
    """CSV/XLSX → list[dict] (헤더 기반)."""
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        try:
            from openpyxl import load_workbook
            wb = load_workbook(p, read_only=True, data_only=True)
            ws = wb.active
            it = ws.iter_rows(values_only=True)
            header = [str(h).strip() if h is not None else "" for h in next(it, [])]
            out = []
            for r in it:
                out.append({header[i]: r[i] for i in range(min(len(header), len(r)))})
            return out
        except (ImportError, OSError, ValueError, StopIteration) as e:
            log.warning(f"[stats] XLSX 읽기 실패: {e}")
            return []
    # CSV
    for enc in ("utf-8-sig", "cp949", "utf-8"):
        try:
            with p.open(encoding=enc, newline="") as f:
                return list(csv.DictReader(f))
        except (UnicodeDecodeError, OSError):
            continue
    return []


def _pick(row: dict, names: tuple, *, exclude: tuple = ()) -> str:
    for k, v in row.items():
        kl = str(k).strip().lower()
        if any(x.lower() in kl for x in exclude):
            continue
        for n in names:
            if n.lower() in kl:
                return "" if v is None else str(v).strip()
    return ""


def _num(s) -> int:
    if s is None:
        return 0
    m = re.search(r"\d[\d,]*", str(s))
    return int(m.group(0).replace(",", "")) if m else 0


# ─────────────────────────────────────────────────────
# 수집 오케스트레이션 (auto_scheduler 파이프라인이 호출 — 하위호환)
# ─────────────────────────────────────────────────────
def reconcile_urls_from_postlist(blog_id: str = "", *, pages: int = 12) -> int:
    """공개 블로그 포스트목록(무인증)에서 log_no+제목을 긁어 performance_db url 보정.

    발행 시 url 미기록 이력(232편 url 공백) 때문에 내부링크가 실제 링크를 못 만든다.
    공개 페이지만 읽으므로 로그인/API 불필요. 제목 매칭으로 url/log_no 채움.
    반환: 보정한 편수. fail-open.
    """
    from core import performance_db as pdb
    try:
        if not blog_id:
            from config.settings import NAVER_ID
            blog_id = NAVER_ID
        if not blog_id or blog_id == "your_naver_id":
            return 0
        stats = NaverBlogStats(blog_id)
        data = pdb.load()
        filled = 0
        for _pg in range(1, max(1, pages) + 1):
            posts = stats.get_post_list(count=30, page=_pg)
            if not posts:
                break
            for it in posts:
                log_no, title = it.get("log_no"), it.get("title", "")
                key = pdb.normalize_key(title)
                rec = data["posts"].get(key)
                if not rec:
                    key = next((k for k in data["posts"] if key and (key in k or k in key)), "")
                    rec = data["posts"].get(key) if key else None
                if rec and log_no and not rec.get("url"):
                    rec["url"] = f"https://blog.naver.com/{blog_id}/{log_no}"
                    rec["log_no"] = log_no
                    filled += 1
        if filled:
            pdb.save(data)
        log.info(f"[stats] 공개 포스트목록 url 보정 — {filled}편")
        return filled
    except (urllib.error.URLError, TimeoutError, ImportError, OSError) as e:
        log.debug(f"[stats] url 보정 실패: {e}")
        return 0


def collect_stats(account: dict = None) -> dict:
    """방문자 총계 + 글별 세션 수집을 수행하고 performance_db·blog_stats 에 기록.

    (기존 시그니처 유지 — auto_scheduler 파이프라인 스텝에서 호출)
    반환: 방문자 dict.
    """
    from core import performance_db as pdb

    try:
        blog_id = (account or {}).get("naver_id", "") if account else ""
        token   = (account or {}).get("api_token", "") if account else ""
        if not blog_id:
            from config.settings import NAVER_ID
            blog_id = NAVER_ID
        if not blog_id or blog_id == "your_naver_id":
            return {}

        stats_obj = NaverBlogStats(blog_id, token)
        visitors  = stats_obj.get_visitor_count()

        # 1) blog_stats.json (기존 대시보드 호환)
        db = _load_stats_db()
        today_str = date.today().strftime("%Y-%m-%d")
        db.setdefault("visitors", {})[today_str] = visitors
        # 일별 시리즈(최근 5일)를 각 날짜에 기록 → 방문자 차트·visitor_series 실데이터화
        daily = visitors.get("_daily") or {}
        vdays = sorted(daily)
        for i, d in enumerate(vdays):
            prev = daily[vdays[i - 1]] if i >= 1 else 0
            db["visitors"][d] = {"today": daily[d], "yesterday": prev, "total": 0}
        _save_stats_db(db)

        # 2) performance_db 미러 + 글별 세션 수집
        pdb.record_visitors(visitors, day=today_str)
        for i, d in enumerate(vdays):     # 일별 시리즈를 performance_db 에도 미러
            prev = daily[vdays[i - 1]] if i >= 1 else 0
            pdb.record_visitors({"today": daily[d], "yesterday": prev, "total": 0}, day=d)
        rows = collect_post_stats_via_session(blog_id, stats_obj.cookie)
        if rows:
            data = pdb.load()
            for r in rows:
                _, key = pdb.upsert_post(title=r.get("title", ""), data=data)
                if key and r.get("views"):
                    pdb.record_views(key, r["views"], day=today_str, data=data)
                for kw, cnt in (r.get("referrals") or {}).items():
                    pdb.record_referral(key, kw, cnt, data=data)
            pdb.save(data)

        if not any(visitors.get(k) for k in ("today", "yesterday", "total")):
            log.info("[stats] 방문자 0 — 세션쿠키 로그인 필요 시 claude/네이버 로그인 후 재수집")
        return visitors
    except ImportError as e:
        log.debug(f"통계 수집 스킵 (설정 없음): {e}")
        return {}
    except (urllib.error.URLError, TimeoutError) as e:
        log.warning(f"통계 수집 실패 (네트워크): {e}")
        return {}


def diagnose_visitor_activation(blog_id: str = "") -> dict:
    """방문자·글별 유입 데이터 활성화 상태 진단(비파괴 · 자격증명 평문 노출 없음).

    무엇이 준비됐고 무엇이 없는지 항목별 ok/absent + 다음 행동을 반환한다. 실제 방문자
    1회 조회로 라이브/익명 여부까지 확인. 사용자가 ②세션쿠키 / ③통계 인제스트 중 무엇을
    해야 하는지 정확히 짚어 준다. rank_tracker.verify() 의 방문자판.
    ⚠️ 쿠키 '값'은 절대 반환하지 않는다 — 존재 여부·개수만.
    """
    from core import performance_db as pdb
    from config import safe_load_json

    out: dict = {"checks": {}, "next_actions": []}

    # 1) blog_id 설정 여부
    if not blog_id:
        try:
            from config.settings import NAVER_ID
            blog_id = NAVER_ID
        except (ImportError, AttributeError):
            blog_id = ""
    configured = bool(blog_id) and blog_id != "your_naver_id"
    out["checks"]["blog_id"] = "ok" if configured else "absent"

    # 2) 세션쿠키 존재(값 노출 없이 존재/개수만)
    cookie = _cookie_header()
    raw = safe_load_json(_cookie_path(), default=list)
    out["cookie_count"] = len(raw) if isinstance(raw, list) else 0
    out["checks"]["session_cookie"] = "ok" if cookie else "absent"

    # 3) 라이브 방문자 조회(설정됐을 때만 1회)
    live, visitors = "skipped", {}
    if configured:
        try:
            visitors = NaverBlogStats(blog_id).get_visitor_count()
            live = "live" if any(visitors.get(k) for k in ("today", "yesterday", "total")) else "zero"
        except (urllib.error.URLError, TimeoutError, ValueError):
            live = "error"
    out["checks"]["visitor_fetch"] = live
    out["visitors_sample"] = {k: visitors.get(k, 0) for k in ("today", "yesterday", "total")}

    # 4) performance_db 적재 상태
    data = pdb.load()
    posts_with_views = sum(1 for r in data["posts"].values() if r.get("views"))
    visitor_days = len(data.get("visitors", {}))
    out["checks"]["db_post_views"] = "ok" if posts_with_views else "absent"
    out["checks"]["db_visitor_days"] = "ok" if visitor_days else "absent"
    out["posts_with_views"] = posts_with_views
    out["visitor_days"] = visitor_days

    # 5) 다음 행동(사용자 몫만 정확히 — 쿠키 유무·라이브 결과로 케이스 구분)
    if not configured:
        out["next_actions"].append("config.settings.NAVER_ID 를 실제 블로그 ID 로 설정")
    if not cookie:
        out["next_actions"].append(
            "② 방문자 총계: GUI 에서 네이버 로그인하면 세션쿠키가 _runtime/session_cookies.json 에 "
            "자동 저장됨 → collect_stats() (익명 조회는 0 이 흔함)")
    elif live in ("zero", "error"):
        out["next_actions"].append(
            f"② 방문자 총계: 세션쿠키({out['cookie_count']}개)는 있으나 방문자 조회 0 — "
            "쿠키 만료 가능성. GUI 에서 네이버 재로그인해 쿠키 갱신 후 collect_stats() "
            "(네이버가 비소유자 요청엔 방문자 수를 숨기므로 확실한 건 아래 ③)")
    if not posts_with_views:
        out["next_actions"].append(
            "③ 글별 유입: 네이버 블로그 '통계 > 내보내기'(CSV/XLSX) 다운로드 → "
            "ingest_stats_export('경로.csv')")
    if not out["next_actions"]:
        out["next_actions"].append(
            "활성화 완료 — rank_view_gap() 로 '고순위·저유입(AI요약 가로챔)' 용의자 분석 가능")

    # 6) 종합 상태
    if posts_with_views and visitor_days:
        out["status"] = "active"
    elif live == "live" or cookie:
        out["status"] = "partial"
    else:
        out["status"] = "inactive"
    return out


def fetch_post_stats() -> list:
    """포스트별 성과를 performance_db 우선, 없으면 post_history 로 폴백."""
    from core import performance_db as pdb
    data = pdb.load()
    if data["posts"]:
        out = []
        for rec in data["posts"].values():
            out.append({
                "title": rec.get("clean_title") or rec.get("title"),
                "posted_at": rec.get("posted_at", ""),
                "category": rec.get("category", ""),
                "views": pdb._recent_views(rec, 30),
                "success": True,
            })
        return out
    # 폴백 (구버전)
    from config import safe_load_json
    from core.content_generator import detect_category
    records = safe_load_json(_DATA_DIR / "logs" / "post_history.json", default=list)
    result = []
    for rec in records or []:
        clean = re.sub(r'^\d{4}-\d{2}-\d{2} — ', '', rec.get("title", ""))
        result.append({"title": rec.get("title", ""), "posted_at": rec.get("posted_at", ""),
                       "category": detect_category(clean), "views": 0, "success": True})
    return result


def get_dashboard_data() -> dict:
    """GUI 대시보드용 통계 일괄 반환 (+ performance_db 유입 성과)."""
    from core.duplicate_checker import get_stats as dup_stats
    from core import performance_db as pdb

    db          = _load_stats_db()
    visitors    = db.get("visitors", {})
    today_str   = date.today().strftime("%Y-%m-%d")
    today_v     = visitors.get(today_str, {})

    weekly = [{"date": (date.today() - timedelta(days=i)).strftime("%Y-%m-%d")[-5:],
               "count": visitors.get((date.today() - timedelta(days=i))
                                     .strftime("%Y-%m-%d"), {}).get("today", 0)}
              for i in range(6, -1, -1)]

    post_history = dup_stats()
    try:
        from core.trend_updater import get_top_keywords
        top_keywords = get_top_keywords(5)
    except ImportError:
        top_keywords = []

    perf = pdb.get_summary()
    return {
        "today_visitors":     today_v.get("today",     0),
        "yesterday_visitors": today_v.get("yesterday", 0),
        "total_visitors":     today_v.get("total",     0),
        "weekly_chart":       weekly,
        "total_posts":        post_history.get("total_posted", 0),
        "top_keywords":       top_keywords,
        # Phase 1 신규 — 유입 성과
        "top_posts_by_views": perf["top_posts"],
        "top_referrals":      perf["top_referrals"],
        "opportunities":      perf["opportunities"],
        "keywords_top10":     perf["keywords_top10"],
        "posts_with_views":   perf["posts_with_views"],
    }


# ─────────────────────────────────────────────────────
# 성과 측정 v2 (M4, 2026-09-18) — 읽기 전용 capability 실측 + None-결측 방문자 조회
#
# ⚠️ 기존 함수(get_visitor_count·collect_stats·NaverBlogStats.*)는 시그니처·반환을
# 전혀 바꾸지 않는다. 아래는 전부 **추가**다.
#
# fetch_fn 주입 규약: fetch_fn(url: str) -> str (raw 응답 텍스트). 주어지면 그것만
# 쓰고 실제 네트워크를 열지 않는다 — 테스트는 항상 fetch_fn 을 주입해 네트워크 호출
# 0회를 보장한다. 주어지지 않으면(라이브 전용 경로) _default_fetch() 로 실제 GET.
# ─────────────────────────────────────────────────────
def _default_fetch(url: str, *, cookie: str = "") -> str:
    """실제 네트워크 GET(라이브 전용). 테스트에서는 절대 호출되지 않는다(fetch_fn 주입)."""
    headers = {
        "User-Agent":  ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"),
        "Accept":      "application/json, text/plain, */*",
    }
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=HTTP_REQUEST_TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def _capability_evidence_path(profile: str = None):
    """capability 증거 파일 경로 — 호출 시점에 profile.logs_dir() 를 읽는다(테스트가
    config.profile.logs_dir 를 monkeypatch 해 격리할 수 있도록 모듈 상수로 굳히지 않음)."""
    from config import profile as _p
    return _p.logs_dir(profile) / "stats_capability.json"


def _post_list_url(blog_id: str, count: int) -> str:
    return (f"https://blog.naver.com/PostTitleListAsync.naver?blogId={blog_id}"
            f"&viewdate=&currentPage=1&categoryNo=0&countPerPage={int(count)}")


def _check_post_list(blog_id: str, fetch_fn, *, count: int = 5) -> dict:
    """공개 글 목록(PostTitleListAsync, 무인증)에서 log_no 를 실제로 확보할 수 있는지.

    이 저장소에서 **실제로 확인된 유일한 안정 경로** — reconcile_urls_from_postlist 와
    같은 엔드포인트를 재사용한다. 응답 본문은 `_text` 로 함께 돌려주어 같은 수집 안에서
    두 번 받아오지 않게 한다(중복 호출 제거).
    """
    check, _text = _check_post_list_ex(blog_id, fetch_fn, count=count)
    return check


def _check_post_list_ex(blog_id: str, fetch_fn, *, count: int = 5) -> tuple:
    """`(check, text)` — 검사 결과와 **실제 응답 본문**."""
    name = "post_list"
    if not blog_id:
        return {"name": name, "result": "skipped", "detail": "blog_id 미설정"}, ""
    url = _post_list_url(blog_id, count)
    try:
        text = fetch_fn(url) if fetch_fn else _default_fetch(url)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"name": name, "result": "error", "detail": f"조회 실패: {type(e).__name__}"}, ""
    if not text:
        return {"name": name, "result": "error", "detail": "빈 응답"}, ""
    found = re.findall(r'"logNo":"(\d+)"[^}]*?"title":"([^"]*)"', text)
    if found:
        return {"name": name, "result": "ok", "detail": f"logNo {len(found)}건 파싱 성공"}, text
    return {"name": name, "result": "unproven", "detail": "응답에서 logNo 패턴 매칭 0건"}, text


def _check_visitor_daily(blog_id: str, fetch_fn) -> dict:
    """방문자 일별(NVisitorgp4Ajax) — **기존 세션 쿠키가 있을 때만** 시도.

    쿠키 '값'은 절대 읽어서 로그·반환에 넣지 않는다 — 존재 여부만 확인한다.
    """
    name = "visitor_daily"
    if not blog_id:
        return {"name": name, "result": "skipped", "detail": "blog_id 미설정"}
    cookie = _cookie_header()
    if not cookie:
        return {"name": name, "result": "skipped", "detail": "세션쿠키 없음"}
    url = f"https://blog.naver.com/NVisitorgp4Ajax.naver?blogId={blog_id}"
    try:
        text = fetch_fn(url) if fetch_fn else _default_fetch(url, cookie=cookie)
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return {"name": name, "result": "error", "detail": f"조회 실패: {type(e).__name__}"}
    if _VISITOR_DAILY_RE.search(text or ""):
        return {"name": name, "result": "ok", "detail": "일별 방문자 파싱 성공"}
    return {"name": name, "result": "unproven",
            "detail": "방문자 패턴 매칭 0건(쿠키 만료 또는 비소유자 요청 가능성)"}


# ── 수집 상태(마지막 성공·마지막 시도·다음 재시도) ─────
# 증거 파일(stats_capability.json)은 탐침마다 통째로 덮어써서 "언제 마지막으로 됐는지"를
# 잃는다. 그 이력과 재시도 억제를 여기 따로 남긴다. 쿠키·자격증명은 절대 담기지 않는다.
COLLECT_STATE_FILE: str = "stats_collect_state.json"


def _collect_state_path(profile: str = None):
    from config import profile as _p
    return _p.logs_dir(profile) / COLLECT_STATE_FILE


def load_collect_state(*, profile: str = None) -> dict:
    """수집 상태를 읽는다(네트워크 0 · 부작용 0). 없으면 빈 dict."""
    try:
        from config import safe_load_json
        data = safe_load_json(_collect_state_path(profile), default=dict)
    except Exception:  # noqa: BLE001 — 상태 조회 실패가 수집을 막지 않는다
        return {}
    return data if isinstance(data, dict) else {}


def _save_collect_state(data: dict, *, profile: str = None) -> None:
    try:
        from config import atomic_write_json
        path = _collect_state_path(profile)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(path, data)
    except Exception as exc:  # noqa: BLE001
        log.debug(f"[stats] 수집 상태 기록 실패({type(exc).__name__})")


def _update_collect_state(name: str, *, failure: str = "", detail: str = "",
                          rows: int = 0, profile: str = None) -> dict:
    """한 체크의 시도 결과를 상태 파일에 남기고 갱신된 항목을 돌려준다.

    성공하면 연속 실패 카운터와 재시도 억제를 **즉시 해제**한다 — 한 번 막혔다고
    영원히 안 보는 일이 없어야 한다.
    """
    from datetime import datetime, timedelta

    from core import metrics_contract as mc

    now = datetime.now()
    data = load_collect_state(profile=profile)
    data.setdefault("schema", "nba/stats_collect_state@1")
    try:
        from config import profile as _p
        data["profile"] = _p.active_profile()
    except Exception:  # noqa: BLE001
        pass
    entry = data.get(name) if isinstance(data.get(name), dict) else {}
    entry["last_attempt_at"] = now.isoformat(timespec="seconds")
    if failure:
        attempts = int(entry.get("attempts") or 0) + 1 if entry.get("failure") == failure else 1
        wait = mc.retry_after_seconds(failure, attempts)
        entry.update(failure=failure, detail=str(detail or "")[:200], attempts=attempts,
                     next_attempt_at=(now + timedelta(seconds=wait)).isoformat(timespec="seconds"),
                     retry_after_sec=wait)
        if not entry.get("first_failed_at"):
            entry["first_failed_at"] = entry["last_attempt_at"]
    else:
        entry.update(failure="", detail=str(detail or "")[:200], attempts=0,
                     next_attempt_at="", retry_after_sec=0, first_failed_at="",
                     last_success_at=entry["last_attempt_at"], last_success_rows=int(rows or 0))
    data[name] = entry
    _save_collect_state(data, profile=profile)
    return entry


def _suppressed_until(name: str, *, profile: str = None) -> str:
    """재시도 억제가 걸려 있으면 그 해제 시각(ISO), 아니면 ""."""
    from datetime import datetime

    entry = load_collect_state(profile=profile).get(name)
    if not isinstance(entry, dict) or not entry.get("failure"):
        return ""
    nxt = str(entry.get("next_attempt_at") or "")
    if not nxt:
        return ""
    try:
        return nxt if datetime.fromisoformat(nxt) > datetime.now() else ""
    except ValueError:
        return ""


def _fetch_post_metrics_rows(blog_id: str, fetch_fn, *, collected_at: str = "") -> tuple:
    """크리에이터 어드바이저 후보 엔드포인트에서 글별 조회수 행을 실제로 시도한다.

    이 저장소에 존재하는 유일한 후보(`collect_post_stats_via_session` 과 동일
    엔드포인트)만 쓴다 — **새 URL 을 만들지 않는다**(이 주소가 공식 지원 API 라는
    근거는 없다). 쿠키·blog_id 존재 확인은 호출자 책임.

    반환: `(result, rows, detail, failure)`.
      · result  — "ok"/"unproven"/"error" (기존 계약 그대로)
      · failure — metrics_contract 의 세부 분류(401/403/429/5xx/로그인/파싱/형식)
    """
    from core import metrics_contract as mc

    url = f"https://creator-advisor.naver.com/api/v6/summary/blog?blogId={blog_id}"
    cookie = _cookie_header()
    try:
        text = fetch_fn(url) if fetch_fn else _default_fetch(url, cookie=cookie)
    except urllib.error.HTTPError as e:
        failure = mc.classify_http_status(getattr(e, "code", None))
        return "error", [], f"HTTP {e.code}", failure
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        return "error", [], f"조회 실패: {type(e).__name__}: {e}", mc.classify_exception(e)

    if mc.looks_like_login_page(text):
        return ("error", [], "로그인 화면이 반환됨(세션 만료로 보임)", mc.FAILURE_LOGIN_PAGE)
    try:
        payload = json.loads(text) if text else {}
    except (ValueError, TypeError):
        return "unproven", [], "응답이 JSON 형식이 아님", mc.FAILURE_PARSE

    rows = _parse_creator_payload(payload, collected_at=collected_at)
    usable = [r for r in rows if r.get("views") is not None]
    if usable:
        detail = f"{len(usable)}편 파싱 성공"
        if len(usable) != len(rows):
            detail += f"(지표 결측 {len(rows) - len(usable)}편 제외)"
        return "ok", rows, detail, mc.FAILURE_NONE
    if rows:
        # 글 번호는 읽혔지만 쓸 수 있는 수치가 하나도 없다 — 성공이 아니다.
        return ("unproven", rows, f"글 {len(rows)}건에 유효한 조회수 없음",
                mc.FAILURE_SHAPE)
    if isinstance(payload, dict):
        top_keys = ", ".join(sorted(str(k) for k in payload.keys())[:5]) or "(빈 객체)"
    else:
        top_keys = type(payload).__name__
    return ("unproven", [], f"응답 구조가 글별 지표 형식이 아님(키: {top_keys})",
            mc.FAILURE_SHAPE)


def _check_post_metrics(blog_id: str, fetch_fn=None, *, force: bool = False,
                        collected_at: str = "") -> dict:
    """글별 조회수·유입검색어 자동 수집 — **읽기 전용 실측**(추측으로 결과를 적지 않는다).

    - blog_id 없음 → skipped
    - 세션 쿠키 없음 → needs_login(재시도 경로 안내 포함) — 쿠키 '값'은 절대 반환·
      로그에 넣지 않는다(존재 여부만 확인).
    - **직전 실패의 재시도 대기가 남아 있으면 네트워크를 열지 않는다**(영구 403 을
      매 실행마다 두드리지 않기 위함). 사용자가 명시로 다시 확인할 때(`force=True`)는
      대기를 무시하고 실제로 조회한다.
    - 그 밖에는 이 저장소에 존재하는 유일한 후보 엔드포인트로 **한 번 실제로 읽어 본다**
      (fetch_fn 이 주입되면 그것만 쓰고 네트워크를 열지 않는다).
    """
    from core import metrics_contract as mc

    name = "post_metrics"
    if not blog_id:
        return {"name": name, "result": "skipped", "detail": "blog_id 미설정",
                "failure": mc.FAILURE_NONE}
    if not _cookie_header():
        return {"name": name, "result": "needs_login",
                "detail": "세션 쿠키 없음 — 네이버 로그인(쿠키 갱신) 후 다시 확인 필요",
                "failure": mc.FAILURE_NONE,
                "action": mc.user_action(mc.FAILURE_UNAUTHORIZED),
                "retry": "GUI 무인 운영/성장 탭의 '지금 다시 확인' 또는 python -m core.stats_fetcher --probe"}

    if not force:
        until = _suppressed_until(name)
        if until:
            entry = load_collect_state().get(name) or {}
            failure = str(entry.get("failure") or "")
            return {"name": name, "result": "error", "failure": failure,
                    "suppressed": True, "next_attempt_at": until,
                    "detail": (f"직전 실패({mc.failure_label(failure)}) 재시도 대기 중 — "
                               f"{until} 이후 자동 재시도"),
                    "action": mc.user_action(failure),
                    "retry": "지금 다시 확인하려면 GUI '지금 다시 확인' 또는 "
                             "python -m core.stats_fetcher --probe --force"}

    result, rows, detail, failure = _fetch_post_metrics_rows(
        blog_id, fetch_fn, collected_at=collected_at)
    usable = sum(1 for r in rows if r.get("views") is not None)
    entry = _update_collect_state(name, failure=("" if result == "ok" else (failure or result)),
                                  detail=detail, rows=usable)
    out = {"name": name, "result": result, "detail": detail, "failure": failure,
           "_rows": rows}
    if result != "ok":
        out["action"] = mc.user_action(failure)
        if entry.get("next_attempt_at"):
            out["next_attempt_at"] = entry["next_attempt_at"]
    return out


def _public_check(check: dict) -> dict:
    """증거 파일·반환값에 실을 수 있게 내부 전용 키(`_rows` 등)를 떼어낸다."""
    return {k: v for k, v in (check or {}).items() if not str(k).startswith("_")}


def _write_capability_evidence(checks: list, collected_at: str, *, profile: str = None) -> "Path":
    """capability 증거를 원자 쓰기로 저장한다. **쿠키 값은 절대 포함하지 않는다**
    (checks 의 detail 문자열은 존재/개수만 다룬다 — 위 _check_* 함수들이 보장).

    `profile` 을 함께 적어 **다른 계정의 증거로 환류가 열리는 일**을 막는다
    (measurement.capability_status 가 활성 프로필과 대조한다).
    """
    from config import atomic_write_json

    path = _capability_evidence_path(profile)
    try:
        from config import profile as _p
        profile_name = profile or _p.active_profile()
    except Exception:  # noqa: BLE001
        profile_name = profile or ""
    payload = {"schema": "nba/stats_capability@2", "collected_at": collected_at,
               "profile": profile_name, "checks": [_public_check(c) for c in checks]}
    atomic_write_json(path, payload)
    return path


def _gather_capability(blog_id: str, fetch_fn, *, post_list_count: int = 5,
                       force: bool = False) -> tuple:
    """세 체크를 **각각 한 번씩만** 수행하고 응답 본문까지 함께 돌려준다.

    이전에는 `collect_post_metrics_readonly` 가 탐침을 돌린 뒤 같은 엔드포인트를
    다시 호출해, 한 번의 수집이 글목록 2회·지표 2회를 요청하고 **2차 호출의 실패가
    1차의 ok 에 가려졌다**. 이제 검사와 저장이 같은 응답을 본다.

    반환: `(checks, post_list_text, metrics_rows, collected_at)`
    """
    from datetime import datetime

    collected_at = datetime.now().isoformat(timespec="seconds")
    list_check, list_text = _check_post_list_ex(blog_id, fetch_fn, count=post_list_count)
    visitor_check = _check_visitor_daily(blog_id, fetch_fn)
    metrics_check = _check_post_metrics(blog_id, fetch_fn, force=force,
                                        collected_at=collected_at)
    rows = metrics_check.pop("_rows", []) or []
    return [list_check, visitor_check, metrics_check], list_text, rows, collected_at


def probe_stats_capability(*, blog_id: str = "", fetch_fn=None, force: bool = False) -> dict:
    """읽기 전용 성과 수집 capability 실측(설계 §4.1.1 capability spike, 계약 §2.8).

    세 경로를 확인한다: ①공개 글 목록으로 log_no 확보 ②방문자 일별(세션쿠키 있을
    때만) ③글별 조회수/유입검색어 자동 수집. 네트워크는 fetch_fn 주입으로 전량
    대체 가능 — 테스트는 항상 주입해 실제 네트워크 호출을 0회로 만든다. 결과를
    `profile.logs_dir()/stats_capability.json` 에 남긴다.

    `force=True` 는 사람이 명시로 다시 확인할 때만 쓴다 — 직전 실패의 재시도 대기를
    무시하고 실제로 한 번 조회한다.

    반환: {"ok", "checks":[{"name","result","detail",...}], "collected_at", "evidence_path"}.
    "ok" 는 "post_list" 체크(이 저장소에서 실제로 증명된 경로)가 ok 일 때만 True다.
    """
    checks, _text, _rows, collected_at = _gather_capability(
        blog_id, fetch_fn, post_list_count=5, force=force)
    ok = checks[0]["result"] == "ok"
    evidence_path = _write_capability_evidence(checks, collected_at)
    return {"ok": ok, "checks": [_public_check(c) for c in checks],
            "collected_at": collected_at, "evidence_path": str(evidence_path)}


def collect_post_metrics_readonly(*, blog_id: str = "", fetch_fn=None,
                                  force: bool = False) -> dict:
    """capability 가 증명한 경로만 쓰는 읽기 전용 수집기.

    포스트 ID(log_no) 확보는 항상 시도한다(post_list 가 이 저장소의 안정 경로).
    조회수(post_metrics)는 **같은 호출에서 실제로 받은 행**이 있을 때만 채우고,
    아니면 `views=None` + `manual_required=True` + `reason` 에 needs_login/error/
    unproven/skipped 중 실제 사유를 담는다(추측으로 채우지 않는다 — 계약 §2.8).

    부분 결과도 숨기지 않는다 — 체크가 ok 여도 **그 글의 행이 없으면** 그 글만
    `views=None` + `reason="row_missing"` + `manual_required=True` 로 남는다.
    쿠키 '값'은 이 함수 어디에서도 반환·로그에 넣지 않는다.

    반환: {"ok","reason","posts":[{"log_no","title","views","search_inflow","basis",
           "source","identity","observed_at","manual_required","reason"}],
           "manual_required","collected_at","basis","source","failure","action",
           "next_retry_at","metrics_rows","metrics_matched"}.
    """
    from core import metrics_contract as mc

    checks, list_text, rows, collected_at = _gather_capability(
        blog_id, fetch_fn, post_list_count=30, force=force)
    _write_capability_evidence(checks, collected_at)

    by_name = {c["name"]: c for c in checks}
    post_list_ok = by_name.get("post_list", {}).get("result") == "ok"
    metrics_check = by_name.get("post_metrics", {})
    metrics_ok = metrics_check.get("result") == "ok"
    metrics_reason = "" if metrics_ok else (metrics_check.get("result") or "unproven")
    failure = str(metrics_check.get("failure") or "")

    base = {"collected_at": collected_at,
            "basis": mc.BASIS_UNKNOWN, "source": "creator_advisor",
            "failure": failure, "action": str(metrics_check.get("action") or ""),
            "next_retry_at": str(metrics_check.get("next_attempt_at") or ""),
            "metrics_rows": len(rows), "metrics_matched": 0}

    if not post_list_ok:
        return dict(base, ok=False, reason="capability_unproven", posts=[],
                    manual_required=True)

    metrics_by_log = {r["log_no"]: r for r in rows
                      if r.get("log_no") and r.get("views") is not None}

    posts = []
    matched = 0
    for lg, ti in re.findall(r'"logNo":"(\d+)"[^}]*?"title":"([^"]*)"', list_text or ""):
        title = _html.unescape(urllib.parse.unquote(ti).replace("+", " ")).strip()
        if not title:
            continue
        row = metrics_by_log.get(lg)
        if row is not None:
            matched += 1
            posts.append({"log_no": lg, "title": title, "views": row.get("views"),
                          "search_inflow": None, "basis": row.get("basis", mc.BASIS_UNKNOWN),
                          "source": row.get("source") or "creator_advisor",
                          "identity": mc.IDENTITY_LOG_NO, "observed_at": collected_at,
                          "manual_required": False, "reason": ""})
            continue
        # 체크는 ok 인데 이 글의 행만 없는 경우 = 부분 결과. 0 으로 채우지 않는다.
        reason = metrics_reason or ("row_missing" if metrics_ok else "unproven")
        posts.append({"log_no": lg, "title": title, "views": None,
                      "search_inflow": None, "basis": mc.BASIS_UNKNOWN,
                      "source": "creator_advisor", "identity": mc.IDENTITY_LOG_NO,
                      "observed_at": collected_at, "manual_required": True,
                      "reason": reason})

    base["metrics_matched"] = matched
    return dict(base, ok=bool(posts), reason="" if posts else "no_posts_found",
                posts=posts, manual_required=(not metrics_ok) or matched == 0)


def get_visitor_count_v2(blog_id: str = "", *, cookie: str = "", fetch_fn=None) -> dict:
    """방문자 조회 — **결측을 0 이 아니라 None 으로 돌려주는 신규 경로**(측정 v2 전용).

    기존 `get_visitor_count()` 는 하위호환을 위해 그대로 둔다(모든 폴백이 실패하면
    {"today":0,...} 를 계속 돌려준다 — 그 0 은 호출자에게 "결측"이 아니라
    "0 으로 집계됨"으로 읽혀 measurement 모듈에 부적합하다). 이 함수는 일별
    방문자(NVisitorgp4Ajax) 하나만 시도하고, 실패/미검출을 전부 None 으로 남긴다.
    """
    from datetime import datetime

    collected_at = datetime.now().isoformat(timespec="seconds")
    if not blog_id:
        try:
            from config.settings import NAVER_ID
            blog_id = NAVER_ID
        except (ImportError, AttributeError):
            blog_id = ""
    if not blog_id or blog_id == "your_naver_id":
        return {"today": None, "yesterday": None, "total": None,
                "_source": "unconfigured", "_daily": {}, "collected_at": collected_at}

    url = f"https://blog.naver.com/NVisitorgp4Ajax.naver?blogId={blog_id}"
    try:
        text = fetch_fn(url) if fetch_fn else _default_fetch(url, cookie=cookie or _cookie_header())
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        log.debug(f"[stats_v2] 방문자 조회 실패: {e}")
        return {"today": None, "yesterday": None, "total": None,
                "_source": "NVisitorgp4Ajax", "_daily": {}, "collected_at": collected_at}

    daily = {}
    for ymd, cnt in _VISITOR_DAILY_RE.findall(text or ""):
        daily[f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:]}"] = int(cnt)
    if not daily:
        return {"today": None, "yesterday": None, "total": None,
                "_source": "NVisitorgp4Ajax", "_daily": {}, "collected_at": collected_at}
    days = sorted(daily)
    return {"today": daily[days[-1]],
            "yesterday": daily[days[-2]] if len(days) >= 2 else None,
            "total": None,  # 이 엔드포인트엔 누적치가 없음 — 0 이 아니라 결측
            "_source": "NVisitorgp4Ajax", "_daily": daily, "collected_at": collected_at}


def _cli_resolve_blog_id() -> str:
    try:
        from config.settings import NAVER_ID
    except (ImportError, AttributeError):
        return ""
    if not NAVER_ID or NAVER_ID == "your_naver_id":
        return ""
    return NAVER_ID


def _cli_probe(force: bool = False) -> int:
    """`--probe` — 사람이 재시도할 수 있는 읽기 전용 capability 확인."""
    blog_id = _cli_resolve_blog_id()
    if not blog_id:
        print("blog_id 가 설정되지 않았습니다 — config/settings.py 의 NAVER_ID 를 확인하세요.")
        return 1
    result = probe_stats_capability(blog_id=blog_id, force=force)
    print(f"성과 수집 capability 점검 — {result['collected_at']}")
    all_ok = True
    for chk in result["checks"]:
        print(f"  [{chk['result']}] {chk['name']}: {chk['detail']}")
        if chk.get("action"):
            print(f"      → {chk['action']}")
        elif chk["result"] == "needs_login" and chk.get("retry"):
            print(f"      → {chk['retry']}")
        if chk["result"] not in ("ok", "skipped"):
            all_ok = False
    print(f"증거 저장: {result['evidence_path']}")
    return 0 if (result["ok"] and all_ok) else 1


def _cli_collect(force: bool = False) -> int:
    """`--collect` — 읽기 전용 성과 수집 1회 실행(사람이 읽는 요약)."""
    blog_id = _cli_resolve_blog_id()
    if not blog_id:
        print("blog_id 가 설정되지 않았습니다 — config/settings.py 의 NAVER_ID 를 확인하세요.")
        return 1
    result = collect_post_metrics_readonly(blog_id=blog_id, force=force)
    if not result["ok"]:
        print(f"수집 실패 — 사유: {result['reason']}")
        if result.get("action"):
            print(f"  → {result['action']}")
        return 1
    mode = "자동 수집" if not result["manual_required"] else "수동 확인 필요"
    print(f"{len(result['posts'])}편 확보 — 조회수 {mode} "
          f"(지표 매칭 {result.get('metrics_matched', 0)}편)")
    for post in result["posts"][:10]:
        views = post["views"] if post["views"] is not None else "미확인"
        print(f"  - [{post['log_no']}] {post['title']} — 조회수 {views}")
    if result.get("action"):
        print(f"  → {result['action']}")
    if result.get("next_retry_at"):
        print(f"  다음 자동 재시도: {result['next_retry_at']}")
    return 0 if not result["manual_required"] else 1


def _cli_status() -> int:
    """`--status` — 네트워크 0회. 저장된 수집 상태만 읽어 보여 준다."""
    from core import measurement as ms

    summary = ms.status_summary()
    print(f"성과 수집 상태 — 프로필 {summary.get('profile') or '(미상)'}")
    print(f"  출처: {summary['source_label']}")
    print(f"  마지막 유효 수집: {summary['last_success_at'] or '없음'}")
    print(f"  마지막 시도: {summary['last_attempt_at'] or '없음'}"
          + (f" ({summary['failure_label']})" if summary.get("failure_label") else ""))
    if summary.get("next_retry_at"):
        print(f"  다음 자동 재시도: {summary['next_retry_at']}")
    print(f"  다음 기획 반영: {'적용 중' if summary['feedback_active'] else '보류'}"
          + (f" — {summary['held_label']}" if summary.get("held_label") else ""))
    if summary.get("action"):
        print(f"  할 일: {summary['action']}")
    return 0


if __name__ == "__main__":
    import argparse
    import logging
    logging.basicConfig(level=logging.INFO)

    _parser = argparse.ArgumentParser(description="네이버 블로그 성과 수집 — 읽기 전용")
    _parser.add_argument("--probe", action="store_true", help="capability 재확인(읽기 전용)")
    _parser.add_argument("--collect", action="store_true", help="성과 수집 1회 실행(읽기 전용)")
    _parser.add_argument("--status", action="store_true",
                         help="저장된 수집 상태만 출력(네트워크 0회)")
    _parser.add_argument("--force", action="store_true",
                         help="직전 실패의 재시도 대기를 무시하고 지금 다시 조회")
    _args = _parser.parse_args()

    if _args.status:
        sys.exit(_cli_status())
    elif _args.probe:
        sys.exit(_cli_probe(force=_args.force))
    elif _args.collect:
        sys.exit(_cli_collect(force=_args.force))
    else:
        print(collect_stats())
