# core/blog_fetcher.py — 네이버 블로그 공개 글 목록 조회 (무인증, stdlib 전용)
"""블로그 ID 만으로 최근 발행 글 목록(제목·URL·날짜)을 가져온다.

경쟁 블로그 분석(competitor_analyzer)의 데이터 소스 — shorts_studio 의
studio/naver_blog.py 를 하네스로 이식한 것. 네이버가 실제 쓰는
PostTitleListAsync.naver JSON 엔드포인트를 사용한다. 응답이 이스케이프가 깨진
유사 JSON 이라 json.loads 대신 정규식으로 추출한다.

원칙:
- **무인증**: 자격증명 없이 공개 글 목록만 읽는다(비공개 블로그는 조회 불가).
- **stdlib 전용**: urllib 만 사용(외부 의존성 0).
- **입력 검증**: blog_id 는 영숫자/-/_ 만 허용해 URL 주입을 차단한다.
- **저빈도·예의**: countPerPage 30 캡(그 이상은 네이버가 5편 위젯으로 폴백).
  대량 조회는 호출측이 page 로 넘기고 요청 사이 sleep 을 둔다(ToS).
- 실패 시 NaverBlogError(명확한 예외). 호출측(competitor_analyzer)이 잡아
  fail-open 한다.
"""
from __future__ import annotations

import html as _html
import re
import urllib.parse
import urllib.request
from typing import Dict, List

from config.logger import get_logger

log = get_logger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
_POST_RE = re.compile(r'"logNo":"(\d+)"[^}]*?"title":"([^"]*)"')
_DATE_RE = re.compile(r'"logNo":"(\d+)"[^}]*?"addDate":"([^"]*)"')
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,30}$")

# countPerPage 상한 — 그 이상은 목록이 아닌 "최근 5편" 위젯으로 폴백한다(실측).
_MAX_COUNT = 30
_DEFAULT_TIMEOUT = 15


class NaverBlogError(RuntimeError):
    """블로그 글 목록 조회 실패(입력 오류·네트워크·빈 목록)."""


def is_valid_blog_id(blog_id: str) -> bool:
    """blog_id 가 안전한 형식(영숫자/-/_ 2~30자)인지. URL 주입 방지 게이트."""
    return bool(_ID_RE.match((blog_id or "").strip()))


def _decode_title(raw: str) -> str:
    """PostTitleListAsync 의 URL/HTML 이중 인코딩된 문자열을 사람이 읽는 형태로."""
    return _html.unescape(urllib.parse.unquote(raw).replace("+", " ")).strip()


def fetch_posts(blog_id: str, *, count: int = 30, page: int = 1,
                timeout: int = _DEFAULT_TIMEOUT) -> List[Dict[str, str]]:
    """블로그 최근 발행 글 목록 — [{"title", "url", "log_no", "date"}].

    Args:
        blog_id: 네이버 블로그 ID(영숫자/-/_ 만). 예: "your_blog_id".
        count: 가져올 글 수(1~30 캡). 30 이상은 page 로 넘긴다.
        page: 페이지 번호(1부터).
        timeout: 요청 타임아웃(초).

    Returns:
        최신순 글 목록. 각 항목은 title·url·log_no·date 키를 가진다.

    Raises:
        NaverBlogError: blog_id 형식 오류·네트워크 실패·빈 목록(비공개 블로그).
    """
    blog_id = (blog_id or "").strip()
    if not _ID_RE.match(blog_id):
        raise NaverBlogError("블로그 ID 형식이 올바르지 않습니다 (예: your_blog_id)")

    count = max(1, min(int(count), _MAX_COUNT))
    page = max(1, int(page))
    url = (f"https://blog.naver.com/PostTitleListAsync.naver?blogId={blog_id}"
           f"&viewdate=&currentPage={page}"
           f"&categoryNo=0&countPerPage={count}")
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001 — 네트워크 계열 전부 사용자 메시지로
        raise NaverBlogError(f"네이버 응답 실패: {exc}") from exc

    dates = dict(_DATE_RE.findall(raw))
    posts: List[Dict[str, str]] = []
    for log_no, title_raw in _POST_RE.findall(raw):
        title = _decode_title(title_raw)
        if not title:
            continue
        posts.append({
            "log_no": log_no,
            "title": title,
            "url": f"https://blog.naver.com/{blog_id}/{log_no}",
            "date": _decode_title(dates.get(log_no, "")),
        })
    if not posts:
        raise NaverBlogError("글 목록이 비어 있습니다 — 블로그 ID 를 확인하세요 "
                             "(비공개 블로그는 조회 불가)")
    log.info(f"[blog_fetcher] {blog_id} — {len(posts)}편")
    return posts


def fetch_posts_safe(blog_id: str, *, count: int = 30, page: int = 1,
                     timeout: int = _DEFAULT_TIMEOUT) -> List[Dict[str, str]]:
    """fetch_posts 의 fail-open 래퍼 — 어떤 실패든 빈 리스트를 반환한다.

    경쟁 블로그를 배치로 훑을 때 한 블로그의 조회 실패가 전체를 멈추지 않도록.
    """
    try:
        return fetch_posts(blog_id, count=count, page=page, timeout=timeout)
    except NaverBlogError as exc:
        log.debug(f"[blog_fetcher] {blog_id} 조회 실패(무시): {exc}")
        return []
    except Exception as exc:  # noqa: BLE001 — 예상 밖 오류도 흡수(fail-open)
        log.debug(f"[blog_fetcher] {blog_id} 예상 밖 오류(무시): {exc}")
        return []
