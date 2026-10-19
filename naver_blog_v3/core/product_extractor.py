# core/product_extractor.py — 🛒 쇼핑커넥트 링크 → 상품 데이터 (하이브리드, 2026-07-20)
"""제휴/상품 링크 한 개를 상품 데이터로 변환하는 세 번째 콘텐츠 소스의 '추출기'.

데이터 소스(2026-08 갱신):
  ① 링크 페이지의 **공식 노출 메타**(og:*, JSON-LD Product) 우선 파싱.
  ② 둘 다 실패하면 ``{"ok": False, "need_manual": True}`` — GUI 수동 입력으로 폴백.

⚠️ 쇼핑 검색 API(shop.json)는 2026-08 네이버가 오픈검색에서 **종료(SE05
  "존재하지 않는 검색 api")** — 더 이상 작동 안 함. 같은 자격증명으로 blog.json·
  news.json 은 200 OK 이나 shop.json 만 404 SE05(키·스코프 문제 아님, 플랫폼 종료).
  따라서 상품 데이터는 og/JSON-LD(①)+수동 입력 폴백으로만 채운다. 보강 다리(②
  쇼핑 API)는 첫 404 에서 자동 비활성(discontinued)되고 이후 네트워크를 타지 않는다.
  코드 경로는 네이버 복구 가능성·테스트 seam 을 위해 **삭제하지 않고 남겨 둔다**
  (프로세스 재시작 시 재프로브 → 복구 시 자가회복).

STEP 0 실측(2026-07-20, 종료 전 기록):
  • 스마트스토어 상품 URL 직접 fetch: nid.naver.com 로그인 벽으로 리다이렉트 →
    공식 메타 노출 안 됨(로그인 우회는 ToS 위반이라 시도 안 함). → 메타는 기회적
    (공개 og 를 서버가 열어 주는 페이지에서만), 그 외는 수동 입력 폴백.

ToS 준수(하드 게이트):
  • 공식 노출 메타(네이버가 SEO/공유용으로 자체 노출)만 읽는다. 로그인 페이지로
    리다이렉트되면 그 페이지를 파싱하지 않는다(로그인 우회 금지).
  • 딥 DOM 스크래핑·대량 크롤 금지. 사용자 개시 1건씩, 요청 사이 스로틀(sleep).
  • 전 경로 fail-open — 어떤 실패도 예외를 전파하지 않고 need_manual/부분결과로 알린다.

테스트 이음매(seam): 네트워크 3부분(page_fetcher·shop_searcher·image_downloader)을
주입식으로 두어 실호출 없이 결정론적으로 검증한다(serp_fetch 관습과 동일).
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
from typing import Callable, List, Optional, Tuple

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config.logger import get_logger

log = get_logger(__name__)

SHOP_SEARCH_URL = "https://openapi.naver.com/v1/search/shop.json"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

try:
    from config.settings import API_REQUEST_TIMEOUT
except (ImportError, AttributeError):
    API_REQUEST_TIMEOUT = 10

# 요청 사이 최소 간격(초) — 사용자 개시 1건이라도 API+이미지 다운로드가 이어지므로 예의.
_THROTTLE_SEC = 0.3

# 쇼핑 검색 API 종료(SE05) 감지 플래그. 첫 404 에서 True 로 set → 이후 호출은 네트워크를
# 타지 않고 즉시 ([], "discontinued") 반환(프로세스당 1회만 경고·무의미한 404 반복 제거).
# 프로세스 재시작 시 False 로 재프로브되어 네이버 복구 시 자가회복한다.
_shop_discontinued = False

# 상품 페이지로 인정하는 URL 패턴(리다이렉트 체인에서 상품 URL 식별).
_PRODUCT_URL_RE = re.compile(r"(?:smartstore|shopping|brand)\.naver\.com|/products/|/catalog/", re.I)
_PRODUCT_ID_RE = re.compile(r"/(?:products|catalog)/(\d+)")
_TAG_RE = re.compile(r"<[^>]+>")

# ── 타입: 주입 가능한 네트워크 이음매 ──────────────────────────────────
# page_fetcher(url) -> (status_code, final_url, html). status 0 = 네트워크 오류.
PageFetcher = Callable[[str], Tuple[int, str, str]]
# shop_searcher(query) -> (items, status). status: "ok" | "auth" | "error".
ShopSearcher = Callable[[str], Tuple[list, str]]
# image_downloader(urls, dest_dir, cap) -> [local_abs_path...].
ImageDownloader = Callable[[List[str], Path, int], List[str]]


# ──────────────────────────────────────────────────────────────────────
# 자격증명 / 소소한 헬퍼 (순수)
# ──────────────────────────────────────────────────────────────────────
def _creds() -> Tuple[str, str]:
    """쇼핑 검색 API 자격증명(순위/뉴스와 동일 앱). 없으면 빈 문자열."""
    try:
        from config.settings import NAVER_API_CLIENT_ID, NAVER_API_CLIENT_SECRET
        cid = (NAVER_API_CLIENT_ID or "").strip()
        if cid == "your_client_id":
            return "", ""
        return cid, (NAVER_API_CLIENT_SECRET or "").strip()
    except (ImportError, AttributeError):
        return "", ""


def _image_max_default() -> int:
    try:
        from config import settings as _s
        return max(1, int(getattr(_s, "PRODUCT_IMAGE_MAX", 8)))
    except (ImportError, AttributeError, ValueError, TypeError):
        return 8


def _strip_tags(text: str) -> str:
    """쇼핑 API title 의 <b> 하이라이트 등 태그 제거 + 엔티티 정리."""
    plain = _TAG_RE.sub("", text or "")
    for ent, ch in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                    ("&quot;", '"'), ("&#39;", "'"), ("&nbsp;", " ")):
        plain = plain.replace(ent, ch)
    return re.sub(r"\s+", " ", plain).strip()


def _to_int(value: object) -> Optional[int]:
    """가격 등 숫자 문자열을 int 로(실패 시 None — 지어내지 않음)."""
    if value is None:
        return None
    try:
        digits = re.sub(r"[^\d]", "", str(value))
        return int(digits) if digits else None
    except (ValueError, TypeError):
        return None


def is_valid_url(value: object) -> bool:
    """http(s) 스킴 + 호스트를 갖춘 진짜 URL 인지. 제휴 링크 무결성의 단일 판정처.

    상품명 텍스트가 실수로 링크 자리에 흘러도 URL 로 오인하지 않게 한다(C-1 방지).
    """
    try:
        parsed = urllib.parse.urlparse(str(value or "").strip())
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except (ValueError, TypeError):
        return False


def _is_login_url(url: str) -> bool:
    low = (url or "").lower()
    return "nid.naver.com" in low or "nidlogin" in low or "/login" in low


def _is_product_url(url: str) -> bool:
    return bool(url) and bool(_PRODUCT_URL_RE.search(url))


def _extract_product_id(url: str) -> str:
    m = _PRODUCT_ID_RE.search(url or "")
    return m.group(1) if m else ""


def _tokenize(text: str) -> set:
    """제목 토큰 집합(한글/영숫자 2자+) — 상품 매칭용 자카드에 사용."""
    return {t for t in re.findall(r"[가-힣]{2,}|[A-Za-z0-9]{2,}", (text or "").lower())}


# ──────────────────────────────────────────────────────────────────────
# 링크 해석 — 원본 제휴 링크 보존 + 상품 URL/ID 도달 (로그인 우회 안 함)
# ──────────────────────────────────────────────────────────────────────
class _ChainRedirectHandler(urllib.request.HTTPRedirectHandler):
    """리다이렉트 체인을 기록하고 **로그인 페이지에서 멈춘다**(ToS: 로그인 우회 금지)."""

    def __init__(self) -> None:
        super().__init__()
        self.chain: List[str] = []

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        self.chain.append(newurl)
        if _is_login_url(newurl):
            return None  # 더 따라가지 않음 → urllib 이 3xx 응답을 그대로 반환
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _resolve_link(link: str) -> Tuple[str, str]:
    """제휴/상품 링크 → (product_url, product_id). 원본은 호출부가 보존한다.

    직접 상품 URL(이미 /products/·/catalog/ 포함)이면 네트워크를 타지 않는다(순수).
    naver.me 등 불투명 단축 링크만 리다이렉트를 따라가 상품 URL 을 찾는다(로그인 벽
    직전까지). 어떤 실패도 원본 링크를 그대로 돌려준다(fail-open).
    """
    if _is_product_url(link):
        return link, _extract_product_id(link)
    # 불투명 단축 링크 — 리다이렉트 체인에서 상품 URL 을 찾는다.
    handler = _ChainRedirectHandler()
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(link, headers={"User-Agent": _BROWSER_UA})
    try:
        opener.open(request, timeout=API_REQUEST_TIMEOUT).close()
    except urllib.error.HTTPError:
        pass  # 3xx→로그인 중단 등 — 체인만 있으면 충분
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.debug("[product_extractor] 링크 해석 네트워크 실패(원본 유지): %s", exc)
        return link, ""
    for candidate in reversed(handler.chain):
        if _is_product_url(candidate) and not _is_login_url(candidate):
            return candidate, _extract_product_id(candidate)
    return link, ""


# ──────────────────────────────────────────────────────────────────────
# 공식 노출 메타 파싱 (순수 — 테스트 직접 대상)
# ──────────────────────────────────────────────────────────────────────
_JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S)


def _meta_content(html: str, key: str) -> Optional[str]:
    """<meta property=key content=...> 또는 name=key 의 content 를 뽑는다."""
    pattern = re.compile(
        r'<meta[^>]+(?:property|name)=["\']%s["\'][^>]*>' % re.escape(key), re.I)
    m = pattern.search(html or "")
    if not m:
        return None
    c = re.search(r'content=["\']([^"\']*)["\']', m.group(0), re.I)
    return c.group(1).strip() if c and c.group(1).strip() else None


def _walk_jsonld_product(node: object) -> Optional[dict]:
    """JSON-LD 트리(dict/list/@graph)에서 @type == Product 노드를 찾는다."""
    if isinstance(node, list):
        for item in node:
            found = _walk_jsonld_product(item)
            if found:
                return found
        return None
    if isinstance(node, dict):
        node_type = node.get("@type")
        types = node_type if isinstance(node_type, list) else [node_type]
        if any(str(t).lower() == "product" for t in types):
            return node
        if "@graph" in node:
            return _walk_jsonld_product(node.get("@graph"))
    return None


def parse_official_meta(html: str) -> dict:
    """공개 HTML 에서 og:*/JSON-LD Product 를 뽑아 상품 부분 데이터로.

    Returns:
        {name, price, images, rating, review_count, description} 중 확인된 키만.
        아무것도 못 찾으면 빈 dict. 절대 예외를 던지지 않는다(fail-open).
        데이터에 없는 값은 넣지 않는다(무근거 날조 방지 — 하드 게이트).
    """
    if not html:
        return {}
    result: dict = {}
    try:
        name = _meta_content(html, "og:title")
        image = _meta_content(html, "og:image")
        desc = _meta_content(html, "og:description")
        price = _meta_content(html, "product:price:amount")
        if name:
            result["name"] = _strip_tags(name)
        if image:
            result["images"] = [image]
        if desc:
            result["description"] = _strip_tags(desc)
        if _to_int(price) is not None:
            result["price"] = _to_int(price)

        for block in _JSONLD_RE.findall(html):
            try:
                data = json.loads(block.strip())
            except (ValueError, TypeError):
                continue
            product = _walk_jsonld_product(data)
            if not product:
                continue
            if not result.get("name") and product.get("name"):
                result["name"] = _strip_tags(str(product["name"]))
            offers = product.get("offers")
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and result.get("price") is None:
                if _to_int(offers.get("price")) is not None:
                    result["price"] = _to_int(offers.get("price"))
            rating = product.get("aggregateRating")
            if isinstance(rating, dict):
                try:
                    if rating.get("ratingValue") is not None:
                        result.setdefault("rating", float(rating["ratingValue"]))
                except (ValueError, TypeError):
                    pass
                if _to_int(rating.get("reviewCount")) is not None:
                    result.setdefault("review_count", _to_int(rating.get("reviewCount")))
            img = product.get("image")
            if img and not result.get("images"):
                result["images"] = [img] if isinstance(img, str) else list(img)[:1]
            break
    except Exception as exc:  # noqa: BLE001 — 파싱은 무엇이 와도 fail-open
        log.debug("[product_extractor] 메타 파싱 예외(fail-open): %s", exc)
    return result


# ──────────────────────────────────────────────────────────────────────
# 쇼핑 검색 API (기본 구현) — 주입되면 이 함수는 안 쓰인다
# ──────────────────────────────────────────────────────────────────────
def _default_page_fetcher(url: str) -> Tuple[int, str, str]:
    """공개 상품 페이지 fetch → (status, final_url, html). 로그인 리다이렉트도 그대로 보고."""
    handler = _ChainRedirectHandler()
    opener = urllib.request.build_opener(handler)
    request = urllib.request.Request(url, headers={
        "User-Agent": _BROWSER_UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9",
    })
    try:
        with opener.open(request, timeout=API_REQUEST_TIMEOUT) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            final_url = resp.geturl()
            body = resp.read().decode(charset, errors="replace")
            return getattr(resp, "status", 200) or 200, final_url, body
    except urllib.error.HTTPError as exc:
        return exc.code, getattr(exc, "url", url) or url, ""
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        log.debug("[product_extractor] 상품 페이지 fetch 실패(fail-open): %s", exc)
        return 0, url, ""


def _default_shop_searcher(query: str) -> Tuple[list, str]:
    """쇼핑 검색 API 호출 → (items, status). status: ok | auth | error | discontinued.

    자격증명이 없으면 네트워크를 타지 않고 "auth" 로 즉시 반환한다.
    쇼핑 검색 API 는 2026-08 종료(SE05) — 첫 404 이후에는 네트워크를 타지 않고
    "discontinued" 로 즉시 반환한다(무의미한 404 반복 제거, 프로세스 재시작 시 재프로브).
    """
    global _shop_discontinued
    # 종료가 이미 확인됐으면 네트워크 없이 즉시 반환(프로세스당 404 1회로 제한).
    if _shop_discontinued:
        return [], "discontinued"
    cid, secret = _creds()
    if not (cid and secret):
        return [], "auth"
    qs = urllib.parse.urlencode({"query": query, "display": 10, "sort": "sim"})
    request = urllib.request.Request(
        f"{SHOP_SEARCH_URL}?{qs}",
        headers={"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": secret},
    )
    try:
        with urllib.request.urlopen(request, timeout=API_REQUEST_TIMEOUT) as resp:
            data = json.loads(resp.read())
        return (data.get("items", []) or []), "ok"
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 404 = 엔드포인트 종료(SE05). 전송성 오류가 아니라 플랫폼 종료이므로
            # 플래그를 세워 이후 호출은 네트워크 없이 즉시 discontinued 로 처리한다.
            _shop_discontinued = True
            log.warning("[product_extractor] 쇼핑 검색 API 종료(SE05) — "
                        "상품 보강 비활성, og/수동 폴백")
            return [], "discontinued"
        if exc.code in (401, 403):
            log.warning("[product_extractor] 쇼핑 검색 API 인증 실패 %s — "
                        "api_client_id/secret·쇼핑 스코프(개발자센터) 확인 필요", exc.code)
            return [], "auth"
        log.warning("[product_extractor] 쇼핑 검색 API HTTP %s (%s)", exc.code, query[:20])
        return [], "error"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        log.debug("[product_extractor] 쇼핑 검색 실패 (%s): %s", query[:20], exc)
        return [], "error"


def _normalize_item(item: dict) -> dict:
    """쇼핑 API item → 정규화된 상품 부분 데이터(태그 제거·가격 int·카테고리 결합)."""
    cats = [item.get(f"category{i}") for i in range(1, 5)]
    category_hint = " > ".join(c for c in cats if c)
    image = item.get("image") or ""
    return {
        "name": _strip_tags(item.get("title") or ""),
        "price": _to_int(item.get("lprice")),
        "price_high": _to_int(item.get("hprice")),
        "mall": _strip_tags(item.get("mallName") or ""),
        "brand": _strip_tags(item.get("brand") or item.get("maker") or ""),
        "category_hint": category_hint,
        "images": [image] if image else [],
        "product_url": item.get("link") or "",
        "product_id": _extract_product_id(item.get("link") or ""),
    }


# 정확 ID 매칭이 없을 때 채택할 최소 제목 유사도(자카드). 이 아래면 '무관 상품'으로
# 보고 채택하지 않는다 — 엉뚱한 상품의 가격/판매처/평점을 표에 사실처럼 인쇄하는 것 방지(H-2).
_MIN_MATCH_SCORE = 0.15


def _best_item(items: list, product_id: str, query: str) -> Optional[dict]:
    """검색 결과에서 최적 매칭 상품을 고른다(오채택 방지).

    우선순위: ①링크가 같은 상품 URL(/products/{product_id}) — 정확 일치(무조건 채택)
              ②제목 토큰 자카드가 임계(_MIN_MATCH_SCORE) 이상인 최상위.
    둘 다 실패하면 **None**(items[0] 무조건 채택하지 않음 — 무관 상품 데이터 오인쇄 방지).
    """
    if not items:
        return None
    if product_id:
        for item in items:
            if _extract_product_id(item.get("link") or "") == product_id:
                return _normalize_item(item)
    query_tokens = _tokenize(query)
    if query_tokens:
        best, best_score = None, 0.0
        for item in items:
            title_tokens = _tokenize(_strip_tags(item.get("title") or ""))
            if not title_tokens:
                continue
            overlap = len(query_tokens & title_tokens)
            union = len(query_tokens | title_tokens) or 1
            score = overlap / union
            if score > best_score:
                best, best_score = item, score
        if best is not None and best_score >= _MIN_MATCH_SCORE:
            return _normalize_item(best)
    return None


# ──────────────────────────────────────────────────────────────────────
# 이미지 다운로드 (원격 URL → 로컬 절대경로 → image_paths)
# ──────────────────────────────────────────────────────────────────────
# (파일 헤더 시그니처, 확장자) — 유효한 이미지 바이트만 저장(깨진 파일 업로드 방지).
_IMAGE_SIGNATURES = (
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
)


# 상품 이미지 1장 최대 바이트(10MB). 오류 페이지·거대 파일로 메모리/디스크가 폭주하지
# 않게 Content-Length 사전 차단 + 실제 읽기 상한을 함께 건다(M-2).
_MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _sniff_ext(data: bytes) -> Optional[str]:
    for signature, ext in _IMAGE_SIGNATURES:
        if data.startswith(signature):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return None


def _download_images(urls: List[str], dest_dir: Path, cap: int) -> List[str]:
    """상품 이미지 URL 들을 dest_dir/product_images/NN.ext 로 저장.

    - cap 장까지만. 실패(비이미지·네트워크)한 URL 은 조용히 스킵(발행을 막지 않음).
    - 파일 시그니처로 실제 이미지 바이트만 저장(HTML 오류 페이지 등 배제).

    Returns:
        저장된 로컬 절대경로 리스트(순서 유지). 어떤 실패도 예외를 던지지 않는다.
    """
    saved: List[str] = []
    try:
        img_dir = Path(dest_dir) / "product_images"
        img_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        log.warning("[product_extractor] 이미지 폴더 생성 실패(스킵): %s", exc)
        return saved

    seen: set = set()
    for url in urls:
        if len(saved) >= max(1, cap):
            break
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            request = urllib.request.Request(url, headers={
                "User-Agent": _BROWSER_UA, "Referer": "https://shopping.naver.com/"})
            with urllib.request.urlopen(request, timeout=API_REQUEST_TIMEOUT) as resp:
                # Content-Length 사전 차단(있으면) — 거대 파일 다운로드 자체를 회피.
                declared = resp.headers.get("Content-Length")
                if declared and declared.isdigit() and int(declared) > _MAX_IMAGE_BYTES:
                    log.debug("[product_extractor] 이미지 과대(%s bytes) 스킵: %s",
                              declared, url[:60])
                    continue
                # 실제 읽기 상한 — Content-Length 를 안 주는 서버도 상한 밖은 버린다.
                data = resp.read(_MAX_IMAGE_BYTES + 1)
            if len(data) > _MAX_IMAGE_BYTES:
                log.debug("[product_extractor] 이미지 상한 초과 스킵: %s", url[:60])
                continue
            ext = _sniff_ext(data)
            if ext is None:
                log.debug("[product_extractor] 비이미지 응답 스킵: %s", url[:60])
                continue
            dest = img_dir / f"{len(saved) + 1:02d}{ext}"
            dest.write_bytes(data)
            saved.append(str(dest.resolve()))
            time.sleep(_THROTTLE_SEC)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                OSError, ValueError) as exc:
            log.debug("[product_extractor] 이미지 다운로드 실패(스킵) %s: %s", url[:60], exc)
            continue
    return saved


# ──────────────────────────────────────────────────────────────────────
# 결과 조립 헬퍼
# ──────────────────────────────────────────────────────────────────────
def _empty_result(affiliate_url: str, product_url: str, product_id: str) -> dict:
    return {
        "ok": False,
        "need_manual": False,
        "name": "",
        "price": None,
        "price_high": None,
        "discount": None,
        "review_count": None,
        "rating": None,
        "images": [],
        "image_paths": [],
        "mall": "",
        "brand": "",
        "product_url": product_url,
        "affiliate_url": affiliate_url,
        "category_hint": "",
        "source": "",
        "reason": "",
        "product_id": product_id,
    }


def _manual_result(affiliate_url: str, product_url: str, product_id: str,
                   reason: str) -> dict:
    result = _empty_result(affiliate_url, product_url, product_id)
    result["need_manual"] = True
    result["reason"] = reason
    result["source"] = "manual"
    return result


# ──────────────────────────────────────────────────────────────────────
# 공개 API
# ──────────────────────────────────────────────────────────────────────
def extract_product(link: str,
                    *,
                    product_name: Optional[str] = None,
                    run_dir: Optional[object] = None,
                    image_max: Optional[int] = None,
                    page_fetcher: Optional[PageFetcher] = None,
                    shop_searcher: Optional[ShopSearcher] = None,
                    image_downloader: Optional[ImageDownloader] = None) -> dict:
    """쇼핑커넥트 제휴/상품 링크 → 상품 데이터(하이브리드).

    Args:
        link: 사용자가 붙인 제휴 링크 또는 상품 URL(원본은 affiliate_url 로 보존).
        product_name: 수동 상품명(메타가 로그인 벽으로 막혔을 때 사용자가 입력) — 있으면
                      이 이름으로 쇼핑 API 를 조회해 가격·이미지를 보강한다.
        run_dir: 이미지 저장 루트(runs/{slug}). None 이면 이미지는 다운로드하지 않고
                 원격 URL 만 담는다(GUI 미리보기용).
        image_max: 다운로드할 최대 이미지 수(기본 settings.PRODUCT_IMAGE_MAX).
        page_fetcher/shop_searcher/image_downloader: 테스트 주입 이음매(기본은 실 네트워크).

    Returns:
        상품 데이터 dict. 핵심 키:
        {ok, need_manual, name, price, price_high, discount, review_count, rating,
         images, image_paths, mall, brand, product_url, affiliate_url,
         category_hint, source, reason, product_id}.
        ok=False & need_manual=True 이면 자동 추출 실패(GUI 수동 입력 유도).
    """
    fetch_page = page_fetcher or _default_page_fetcher
    search_shop = shop_searcher or _default_shop_searcher
    download = image_downloader or _download_images
    cap = image_max if image_max is not None else _image_max_default()

    raw_link = (link or "").strip()
    # C-1: affiliate_url 은 **URL 검증을 통과한 값만** 채택한다. 상품명 텍스트가 링크
    # 자리에 들어와도(스마트스토어 수동 흐름) 절대 제휴 링크로 쓰지 않는다(커미션 무결성).
    affiliate_url = raw_link if is_valid_url(raw_link) else ""
    if not raw_link and not (product_name or "").strip():
        return _manual_result("", "", "", reason="링크가 비어 있습니다.")

    # 링크가 진짜 URL 일 때만 해석·메타 fetch 를 시도한다(비URL 은 API 검색으로만 진행).
    if is_valid_url(raw_link):
        product_url, product_id = _resolve_link(raw_link)
    else:
        product_url, product_id = "", ""

    # ① 공식 노출 메타 — 기회적(로그인 벽/비200 이면 조용히 건너뜀, 로그인 우회 안 함).
    meta: dict = {}
    if is_valid_url(product_url):
        try:
            status, final_url, html = fetch_page(product_url)
            if status and 200 <= status < 300 and not _is_login_url(final_url or ""):
                meta = parse_official_meta(html)
                if final_url and _is_product_url(final_url):
                    product_url = final_url
                    product_id = product_id or _extract_product_id(final_url)
            else:
                log.debug("[product_extractor] 공식 메타 스킵(status=%s, login=%s)",
                          status, _is_login_url(final_url or ""))
        except Exception as exc:  # noqa: BLE001 — 주입 fetcher 가 무엇이든 fail-open
            log.debug("[product_extractor] 메타 fetch 예외(fail-open): %s", exc)

    time.sleep(_THROTTLE_SEC)

    # ② 쇼핑 검색 API 보강 — 검색어(메타 상품명 우선, 없으면 수동 상품명).
    # status 가 "discontinued"(2026-08 종료)/"auth"/"error" 면 items 가 비어 api_item=None →
    # 아래 병합에서 자동으로 og 메타·수동 상품명 폴백으로 이어진다(동작 변화 0).
    query = (meta.get("name") or (product_name or "")).strip()
    api_item: Optional[dict] = None
    api_status = "skip"
    if query:
        try:
            items, api_status = search_shop(query)
            api_item = _best_item(items, product_id, query)
        except Exception as exc:  # noqa: BLE001
            log.debug("[product_extractor] 쇼핑 API 예외(fail-open): %s", exc)

    # ③ 병합 — 이름 확보 못 하면 수동 입력 폴백(둘 다 실패).
    name = (meta.get("name") or (api_item or {}).get("name")
            or (product_name or "")).strip()
    if not name:
        reason = ("상품 정보를 자동으로 불러오지 못했습니다"
                  "(공식 메타는 로그인 벽, 검색 API 매칭 실패). 상품명을 직접 입력해 주세요.")
        return _manual_result(affiliate_url, product_url, product_id, reason)

    api_item = api_item or {}
    price = meta.get("price") if meta.get("price") is not None else api_item.get("price")
    price_high = api_item.get("price_high")
    rating = meta.get("rating")
    review_count = meta.get("review_count")
    mall = api_item.get("mall") or ""
    brand = api_item.get("brand") or ""
    category_hint = api_item.get("category_hint") or ""
    if not product_url:
        product_url = api_item.get("product_url") or affiliate_url

    # 이미지 URL 합치기(메타 → API, 중복 제거).
    image_urls: List[str] = []
    for url in (meta.get("images") or []) + (api_item.get("images") or []):
        if url and url not in image_urls:
            image_urls.append(url)

    # 출처 라벨(정직성) — 실제로 어느 다리가 채웠는지.
    if meta and api_item:
        source = "meta+api"
    elif meta:
        source = "meta"
    elif api_item:
        source = "api"
    else:
        source = "manual"

    image_paths: List[str] = []
    if image_urls and run_dir is not None:
        try:
            image_paths = download(image_urls, Path(run_dir), cap)
        except Exception as exc:  # noqa: BLE001 — 이미지 실패가 추출 전체를 막지 않게
            log.debug("[product_extractor] 이미지 다운로드 예외(fail-open): %s", exc)

    result = _empty_result(affiliate_url, product_url, product_id)
    result.update({
        "ok": True,
        "need_manual": False,
        "name": name,
        "price": price,
        "price_high": price_high,
        "rating": rating,
        "review_count": review_count,
        "images": image_urls[:max(1, cap)],
        "image_paths": image_paths,
        "mall": mall,
        "brand": brand,
        "category_hint": category_hint,
        "source": source,
        "reason": "" if (price or image_urls) else "가격·이미지 미확보 — 필요 시 수동 보완",
    })
    log.info("[product_extractor] 추출 완료: '%s' (source=%s, price=%s, imgs=%d)",
             name[:30], source, price, len(image_paths))
    return result


__all__ = [
    "SHOP_SEARCH_URL",
    "PageFetcher",
    "ShopSearcher",
    "ImageDownloader",
    "parse_official_meta",
    "extract_product",
]
