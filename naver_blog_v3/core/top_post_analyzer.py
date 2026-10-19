# core/top_post_analyzer.py — 상위 노출 글 구조 학습 (순위 모니터 P2, 2026-07-19)
"""워치리스트 키워드의 검색 상위 블로그 글을 구조 신호만 추출해 '승자 프로파일'로
집계하고, 그 실측 규격을 생성 프롬프트에 주입해 구조를 고도화한다.

폐루프: 워치리스트 키워드 → TOP N 글 수집(공식 블로그검색 API로 목록,
본문은 공개 모바일 페이지 저빈도 fetch) → 구조 신호 추출 → 중앙값 프로파일
(logs/winner_profiles.json, TTL 7일) → ①content_generator 프롬프트 규격 힌트
②reports/{date}_winner_structure.md

원칙:
- **구조 신호만** 저장(글자수·이미지·표·FAQ·중제목·목록·제목 패턴). 본문 표현은
  저장·모방하지 않는다 — 표절·유사도 리스크 0 (duplicate_checker 정책과 일관).
- 전 경로 fail-open: 조회 실패 글은 건너뛰고, 프로파일 없으면 힌트도 없음.
- 주입식 fetcher — 실서버 없이 결정론적 테스트.
- 저빈도: 키워드당 본문 N=3, 캐시 7일, 요청 간 sleep.
"""
from __future__ import annotations

import os
import re
import sys
import time
import urllib.request
from datetime import date

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

from config import profile as _nbaprofile

_DATA_DIR = _nbaprofile.app_data_root()
PROFILES_PATH = _DATA_DIR / "logs" / "winner_profiles.json"
_TTL_SEC = 7 * 24 * 3600
_TOP_N = 3
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

_TAG = re.compile(r"<[^>]+>")
_BLOG_LINK = re.compile(
    r"https?://(?:m\.)?blog\.naver\.com/([A-Za-z0-9_.-]+)/(\d+)", re.IGNORECASE)

try:
    from config.settings import API_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    API_REQUEST_TIMEOUT = 10


# ─────────────────────────────────────────────────────
# 구조 신호 추출 (본문 HTML → 숫자만)
# ─────────────────────────────────────────────────────
def extract_signals(html: str, title: str = "") -> dict | None:
    """공개 글 HTML 에서 구조 신호를 추출. 본문 텍스트는 버린다(숫자만 반환).

    반환 None = 본문 영역을 못 찾음(비공개·마크업 변경).
    """
    if not html:
        return None
    # 스마트에디터 본문 영역 우선, 없으면 전체(구 에디터·외부 스킨 폴백)
    m = re.search(r'class="se-main-container"[^>]*>(.*)', html, re.DOTALL)
    body = m.group(1) if m else html
    # 본문 뒤 페이지 크롬(공감·댓글·작성자·추천글)과 스크립트를 잘라낸다 —
    # 안 자르면 글자수가 3~10배 부풀어(라이브 실측 28,853자) 규격 힌트를 오염시킨다.
    body = re.sub(r"<script\b.*?</script>", " ", body,
                  flags=re.DOTALL | re.IGNORECASE)
    body = re.sub(r"<style\b.*?</style>", " ", body,
                  flags=re.DOTALL | re.IGNORECASE)
    low_all = body.lower()
    cut = len(body)
    for marker in ("u_likeit", "commentbox", "blog_authorarea", "post_btn",
                   "recommendlist", "postlistbottom", "sympathy"):
        idx = low_all.find(marker)
        if idx > 500:
            cut = min(cut, idx)
    body = body[:cut]
    text = _TAG.sub(" ", body)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) < 200:          # 본문이라 보기 어려움
        return None
    low = body.lower()
    sig = {
        "chars": len(text),
        "images": low.count("se-image") or len(re.findall(r"<img\b", low)),
        "tables": len(re.findall(r"<table\b", low)),
        "headings": (low.count("se-sectiontitle")
                     or len(re.findall(r"<h[23]\b", low))),
        "lists": len(re.findall(r"<[ou]l\b", low)),
        "faq": len(re.findall(r"Q\s*\d\s*[.)]|자주\s*묻는|FAQ", body, re.IGNORECASE)),
        "title_len": len(title),
        "title_has_number": bool(re.search(r"\d", title)),
        "title_has_year": bool(re.search(r"20\d{2}", title)),
    }
    return sig


def _median(nums: list) -> int:
    s = sorted(nums)
    if not s:
        return 0
    m = len(s) // 2
    return int(s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2)


def aggregate_profile(keyword: str, samples: list[dict]) -> dict | None:
    """샘플 신호들 → 중앙값 프로파일. 샘플 없으면 None."""
    samples = [s for s in samples if s]
    if not samples:
        return None
    keys = ("chars", "images", "tables", "headings", "lists", "faq", "title_len")
    prof = {k: _median([s.get(k, 0) for s in samples]) for k in keys}
    prof["samples"] = len(samples)
    prof["title_number_share"] = round(
        sum(1 for s in samples if s.get("title_has_number")) / len(samples), 2)
    prof["keyword"] = keyword
    return prof


# ─────────────────────────────────────────────────────
# 수집 (API 목록 → 공개 모바일 본문)
# ─────────────────────────────────────────────────────
def _top_posts_via_api(keyword: str, n: int = _TOP_N) -> list[dict]:
    """블로그검색 API 상위 n 개 {title, link}. 자격증명 없으면 []."""
    try:
        from core.rank_tracker import _creds, _search
        cid, secret, _bid = _creds()
        if not (cid and secret):
            return []
        items, status = _search(keyword, cid, secret)
        if status != "ok":
            return []
        out = []
        for it in items[:n * 2]:              # 비공개·파싱 실패 여유분
            link = it.get("link") or ""
            if _BLOG_LINK.search(link):
                out.append({"title": _TAG.sub("", it.get("title") or ""),
                            "link": link})
            if len(out) >= n:
                break
        return out
    except Exception as e:  # noqa: BLE001
        log.debug(f"[top_post] API 목록 실패 ({keyword[:20]}): {e}")
        return []


def _fetch_post_html(url: str) -> str | None:
    """공개 글 본문 HTML(모바일 페이지 — 정적 렌더). 실패 None."""
    m = _BLOG_LINK.search(url)
    if not m:
        return None
    mobile = f"https://m.blog.naver.com/{m.group(1)}/{m.group(2)}"
    req = urllib.request.Request(mobile, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=API_REQUEST_TIMEOUT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        log.debug(f"[top_post] 본문 조회 실패 ({url[:40]}): {e}")
        return None


# ─────────────────────────────────────────────────────
# 프로파일 캐시
# ─────────────────────────────────────────────────────
def _load_profiles() -> dict:
    from config import safe_load_json
    d = safe_load_json(PROFILES_PATH, default=dict)
    return d if isinstance(d, dict) else {}


def _save_profiles(d: dict) -> None:
    from config import atomic_write_json
    PROFILES_PATH.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(str(PROFILES_PATH), d)


def analyze_keyword(keyword: str, *, fetcher=None, post_lister=None,
                    sleep: float = 1.5, now: float | None = None) -> dict | None:
    """키워드 하나의 승자 프로파일을 만들고 캐시. 캐시 유효하면 그대로 반환."""
    now = now or time.time()
    profiles = _load_profiles()
    entry = profiles.get(keyword)
    if (isinstance(entry, dict) and isinstance(entry.get("cached_at"), (int, float))
            and (now - entry["cached_at"]) <= _TTL_SEC and entry.get("profile")):
        return entry["profile"]

    posts = (post_lister or _top_posts_via_api)(keyword)
    samples = []
    for p in posts:
        html = (fetcher or _fetch_post_html)(p["link"])
        sig = extract_signals(html or "", p.get("title", ""))
        if sig:
            samples.append(sig)
        time.sleep(0 if fetcher else sleep)
    prof = aggregate_profile(keyword, samples)
    profiles[keyword] = {"cached_at": now, "profile": prof}
    _save_profiles(profiles)
    return prof


def analyze_watchlist(*, fetcher=None, post_lister=None) -> dict:
    """워치리스트 전체 분석(캐시 7일이라 실제 조회는 만료분만). {analyzed, cached}."""
    from core import rank_watchlist
    analyzed = cached = 0
    now = time.time()
    profiles = _load_profiles()
    for kw in rank_watchlist.keywords():
        entry = profiles.get(kw)
        if (isinstance(entry, dict)
                and isinstance(entry.get("cached_at"), (int, float))
                and (now - entry["cached_at"]) <= _TTL_SEC):
            cached += 1
            continue
        if analyze_keyword(kw, fetcher=fetcher, post_lister=post_lister,
                           now=now) is not None:
            analyzed += 1
    log.info(f"[top_post] 승자 프로파일 분석 {analyzed} · 캐시 {cached}")
    return {"analyzed": analyzed, "cached": cached}


# ─────────────────────────────────────────────────────
# 소비: 프롬프트 힌트 · 리포트
# ─────────────────────────────────────────────────────
def profile_for(text: str) -> dict | None:
    """text(제목/주제)에 앵커된 키워드의 캐시 프로파일. 네트워크 0."""
    profiles = _load_profiles()
    if not profiles:
        return None
    # 정확 일치 → 헤드 키워드 앵커 순
    entry = profiles.get(text.strip())
    if isinstance(entry, dict) and entry.get("profile"):
        return entry["profile"]
    try:
        from core.search_volume import best_keyword
        head, _v = best_keyword(text)
    except Exception:  # noqa: BLE001
        head = ""
    if head:
        for kw, ent in profiles.items():
            if not (isinstance(ent, dict) and ent.get("profile")):
                continue
            if head in kw.replace(" ", "") or head in kw:
                return ent["profile"]
    return None


def prompt_hint(text: str) -> str:
    """content_generator 에 주입할 실측 규격 한 블록. 프로파일 없으면 ""."""
    p = profile_for(text)
    if not p or p.get("samples", 0) < 2:      # 표본 1개는 규격이라 하기 어려움
        return ""
    # 파싱 오염 가드 — 본문 추출이 페이지 크롬을 삼키면 글자수가 비정상으로
    # 커진다. 생성 프롬프트에 잘못된 규격을 넣느니 안 넣는 게 낫다.
    if not (500 <= p.get("chars", 0) <= 8000):
        return ""
    parts = [f"글자 약 {p['chars']:,}자", f"이미지 {p['images']}장"]
    if p.get("tables"):
        parts.append(f"표 {p['tables']}개")
    if p.get("faq"):
        parts.append(f"FAQ {p['faq']}문항")
    if p.get("headings"):
        parts.append(f"중제목 {p['headings']}개")
    return ("[상위 노출 실측 규격 — 이 주제 검색 상위 글 중앙값]\n"
            f"- {' · '.join(parts)} (표본 {p['samples']}편)\n"
            "- 위 규격은 분량·구성 참고용입니다. 표현·문장은 절대 모방하지 말고 "
            "기존 지침(품질 게이트·금지어)을 우선하세요.\n"
            # 이 블록은 프롬프트 맨 끝(JSON 계약 뒤)에 붙어 모델이 마지막으로 읽는다.
            # 승자 글자수가 우리 상한보다 클 수 있어(실측 3,404자 사례) 여기서 명시하지
            # 않으면 상한을 밀어내 _trim_content 가 섹션째 잘라낸다. 숫자는 박지 않는다
            # (상한은 [글 스타일] 1번이 단일 소스).
            "- ★ 특히 글자 수는 위 [글 스타일]의 목표·상한이 우선입니다. "
            "이 실측 글자 수가 더 크더라도 상한을 넘기지 마세요.")


def render_report(reports_dir=None) -> str:
    """워치리스트 승자 프로파일 표 → reports/{date}_winner_structure.md. 반환 파일명."""
    from pathlib import Path
    base = Path(__file__).resolve().parent.parent
    if reports_dir:
        reports = Path(reports_dir)
    else:  # 프로필별 reports/ (2026-07-19 격리 감사)
        try:
            from config import profile as _nbaprofile
            reports = _nbaprofile.reports_dir()
        except Exception:  # noqa: BLE001
            reports = base.parent / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    today = date.today().strftime("%Y-%m-%d")
    profiles = _load_profiles()
    rows = [e["profile"] for e in profiles.values()
            if isinstance(e, dict) and e.get("profile")]
    o = [f"# 🏆 상위 노출 글 구조 프로파일 — {today}\n"]
    if rows:
        o += ["| 키워드 | 표본 | 글자수 | 이미지 | 표 | FAQ | 중제목 | 제목에 숫자 |",
              "|---|---|---|---|---|---|---|---|"]
        for p in sorted(rows, key=lambda x: -x.get("chars", 0)):
            o.append(f"| {p['keyword']} | {p['samples']} | {p['chars']:,} | "
                     f"{p['images']} | {p['tables']} | {p['faq']} | "
                     f"{p['headings']} | {int(p.get('title_number_share', 0) * 100)}% |")
        o.append("\n> 이 중앙값 규격은 생성 프롬프트에 자동 주입됩니다"
                 "(구조 참고만 — 표현 모방 금지).")
    else:
        o.append("> 아직 분석된 프로파일이 없어요 — 워치리스트 등록 후 "
                 "`python -m core.top_post_analyzer` 또는 순위 모니터 실행.")
    name = f"{today}_winner_structure.md"
    (reports / name).write_text("\n".join(o), encoding="utf-8")
    return name


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    r = analyze_watchlist()
    print(f"분석 {r['analyzed']} · 캐시 {r['cached']} → reports/{render_report()}")
