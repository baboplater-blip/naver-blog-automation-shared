"""Bounded, metadata-only adapters for explicitly requested public news sources."""
from __future__ import annotations

import csv
import http.client
import io
import ipaddress
import queue
import re
import socket
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any, Callable, Dict, Iterable, List, Optional

MAX_BYTES = 1_000_000
TIMEOUT_SECONDS = 8
MAX_CALLS = 12
MAX_DNS_WORKERS = 4
DNS_TIMEOUT_SECONDS = 2
_DNS_SLOTS = threading.BoundedSemaphore(MAX_DNS_WORKERS)
KST = timezone(timedelta(hours=9))
SEOUL_EVENTS_URL = "http://openapi.seoul.go.kr:8088/{key}/json/culturalEventInfo/1/100/"
TOUR_FESTIVAL_URL = "https://apis.data.go.kr/B551011/KorService2/searchFestival2"
NASA_JPL_RSS = "https://www.jpl.nasa.gov/feeds/news/"


class SourceError(ValueError):
    """A source could not be fetched or safely interpreted."""


def _public_host(host: str, timeout: float = DNS_TIMEOUT_SECONDS) -> List[str]:
    if not host or host.lower() == "localhost":
        raise SourceError("주소의 호스트가 공개 주소가 아닙니다")
    try:
        addresses = [ipaddress.ip_address(host.split("%", 1)[0])]
    except ValueError:
        if not _DNS_SLOTS.acquire(timeout=max(0.01, timeout)):
            raise SourceError("DNS 확인 시간 제한")
        resolved: queue.Queue[Any] = queue.Queue(maxsize=1)

        def resolve() -> None:
            try:
                result = [ipaddress.ip_address(item[4][0].split("%", 1)[0])
                          for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)]
                resolved.put((result, None))
            except OSError as exc:
                resolved.put((None, exc))
            finally:
                _DNS_SLOTS.release()

        threading.Thread(target=resolve, name="news-dns-check", daemon=True).start()
        try:
            addresses, error = resolved.get(timeout=max(0.01, timeout))
        except queue.Empty as exc:
            raise SourceError("DNS 확인 시간 제한") from exc
        if error is not None:
            raise SourceError("호스트를 확인할 수 없습니다") from error
    if not addresses or any(not address.is_global for address in addresses):
        raise SourceError("비공개 또는 특수 주소는 사용할 수 없습니다")
    return list(dict.fromkeys(str(address) for address in addresses))


_SENSITIVE_QUERY_KEYS = re.compile(r"(?:api|service|client)?(?:key|secret|token|auth|credential|password)$|access_token", re.I)


def validate_public_url(url: str, *, allow_sensitive_query: bool = False,
                        dns_timeout: float = DNS_TIMEOUT_SECONDS) -> str:
    """Validate and return an HTTP(S) URL whose host resolves only publicly."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise SourceError("URL이 비어 있거나 너무 깁니다")
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or parsed.username or parsed.password:
        raise SourceError("공개 HTTP(S) URL만 사용할 수 있습니다")
    if not parsed.hostname:
        raise SourceError("URL 호스트가 없습니다")
    if not allow_sensitive_query:
        for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = re.sub(r"[^a-z0-9]+", "", key.lower())
            if _SENSITIVE_QUERY_KEYS.search(normalized_key):
                raise SourceError("자격증명처럼 보이는 URL 매개변수는 사용할 수 없습니다")
    _public_host(parsed.hostname, timeout=dns_timeout)
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/",
                                    parsed.query, ""))


def safe_metadata_url(url: Any) -> str:
    """Keep a source link only when it is syntactically public and contains no credentials."""
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        return ""
    parsed = urllib.parse.urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return ""
    for key, _ in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        normalized_key = re.sub(r"[^a-z0-9]+", "", key.lower())
        if _SENSITIVE_QUERY_KEYS.search(normalized_key):
            return ""
    try:
        address = ipaddress.ip_address(parsed.hostname)
        if not address.is_global:
            return ""
    except ValueError:
        pass
    return urllib.parse.urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/",
                                    parsed.query, parsed.fragment))


def _has_credential_headers(headers: Optional[Dict[str, str]]) -> bool:
    for name, value in (headers or {}).items():
        normalized = re.sub(r"[^a-z0-9]+", "", str(name).casefold())
        if value and (normalized in {"authorization", "proxyauthorization"}
                      or any(marker in normalized for marker in
                             ("apikey", "clientid", "clientsecret", "token", "credential"))):
            return True
    return False


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self._address, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, address: str, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._address = address

    def connect(self) -> None:
        raw = socket.create_connection((self._address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except (OSError, ssl.SSLError):
            raw.close()
            raise


def _request_once(url: str, timeout: float, max_bytes: int, headers: Dict[str, str],
                  method: str, body: Optional[bytes]) -> tuple[int, Dict[str, str], bytes]:
    parsed = urllib.parse.urlsplit(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    deadline = time.monotonic() + timeout
    addresses = _public_host(parsed.hostname or "", timeout=min(DNS_TIMEOUT_SECONDS, timeout))
    last_error: Optional[OSError] = None
    for address in addresses:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        connection = (_PinnedHTTPSConnection(parsed.hostname or "", port, address, remaining)
                      if parsed.scheme == "https" else
                      _PinnedHTTPConnection(parsed.hostname or "", port, address, remaining))
        response_timer: Optional[threading.Timer] = None
        request_socket: Optional[socket.socket] = None
        response_timed_out = threading.Event()
        try:
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            connection.connect()
            request_socket = connection.sock
            if request_socket is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("요청 시간 제한")
                request_socket.settimeout(remaining)

                def stop_slow_response() -> None:
                    response_timed_out.set()
                    try:
                        request_socket.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        request_socket.close()
                    except OSError:
                        pass

                response_timer = threading.Timer(remaining, stop_slow_response)
                response_timer.daemon = True
                response_timer.start()
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            if response_timed_out.is_set():
                raise SourceError("응답 시간 제한")
            chunks: List[bytes] = []
            body_size = 0
            while body_size <= max_bytes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("응답 시간 제한")
                if request_socket is not None:
                    request_socket.settimeout(remaining)
                chunk = response.read1(min(65536, max_bytes + 1 - body_size))
                if response_timed_out.is_set():
                    raise SourceError("응답 시간 제한")
                if not chunk:
                    break
                chunks.append(chunk)
                body_size += len(chunk)
            if body_size > max_bytes:
                raise SourceError("응답 크기 제한을 넘었습니다")
            payload = b"".join(chunks)
            return response.status, {key.lower(): value for key, value in response.getheaders()}, payload
        except SourceError:
            raise
        except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
            if response_timed_out.is_set():
                raise SourceError("응답 시간 제한") from exc
            last_error = exc if isinstance(exc, OSError) else OSError(type(exc).__name__)
        finally:
            if response_timer is not None:
                response_timer.cancel()
            connection.close()
    if last_error is not None:
        raise SourceError(f"요청 실패: {type(last_error).__name__}") from last_error
    raise SourceError("요청 시간 제한 초과")


def fetch_bytes(url: str, *, transport: Optional[Callable[..., Any]] = None,
                timeout: int = TIMEOUT_SECONDS, max_bytes: int = MAX_BYTES,
                headers: Optional[Dict[str, str]] = None, method: str = "GET",
                body: Optional[bytes] = None, allow_redirects: bool = True,
                allow_sensitive_query: bool = False) -> bytes:
    """Fetch a bounded public response; test transports receive url/timeout/max_bytes."""
    started = time.monotonic()
    try:
        timeout = int(timeout)
        max_bytes = int(max_bytes)
    except (TypeError, ValueError, OverflowError) as exc:
        raise SourceError("요청 예산이 올바르지 않습니다") from exc
    if not 1 <= timeout <= 20 or not 1 <= max_bytes <= MAX_BYTES:
        raise SourceError("요청 예산이 허용 범위를 벗어났습니다")
    parsed_url = urllib.parse.urlsplit(url) if isinstance(url, str) else None
    if parsed_url and parsed_url.scheme.lower() != "https" and (
            allow_sensitive_query or _has_credential_headers(headers)):
        if parsed_url.hostname == "openapi.seoul.go.kr":
            raise SourceError("API 키를 평문 연결로 보낼 수 없습니다. 서울 문화행사는 CSV를 이용하세요")
        raise SourceError("자격증명은 HTTPS 연결에서만 전송할 수 있습니다")
    safe_url = validate_public_url(url, allow_sensitive_query=allow_sensitive_query,
                                   dns_timeout=min(DNS_TIMEOUT_SECONDS, timeout))
    try:
        if transport is not None:
            response = transport(safe_url, timeout=timeout, max_bytes=max_bytes,
                                 headers=dict(headers or {}), method=method, body=body)
            if isinstance(response, bytes):
                payload = response
            elif isinstance(response, str):
                payload = response.encode("utf-8")
            elif isinstance(response, tuple) and len(response) >= 2:
                status, body = response[:2]
                if int(status) < 200 or int(status) >= 300:
                    raise SourceError(f"HTTP 상태 {int(status)}")
                payload = body if isinstance(body, bytes) else str(body).encode("utf-8")
            else:
                raise SourceError("응답 형식이 올바르지 않습니다")
        else:
            request_headers = {"User-Agent": "NaverBlogTopicCollector/1.0", **(headers or {})}
            current_url = safe_url
            for redirect_count in range(4):
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    raise SourceError("요청 시간 제한")
                status, response_headers, payload = _request_once(
                    current_url, remaining, max_bytes, request_headers, method, body)
                if status in {301, 302, 303, 307, 308}:
                    location = response_headers.get("location")
                    if not allow_redirects or not location or redirect_count == 3:
                        raise SourceError("리디렉션이 허용되지 않거나 제한을 넘었습니다")
                    next_url = urllib.parse.urlsplit(urllib.parse.urljoin(current_url, location))
                    current_scheme = urllib.parse.urlsplit(current_url).scheme.lower()
                    if current_scheme == "https" and next_url.scheme.lower() != "https":
                        raise SourceError("HTTPS에서 평문 HTTP로의 리디렉션은 허용되지 않습니다")
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        raise SourceError("요청 시간 제한")
                    current_url = validate_public_url(urllib.parse.urlunsplit(next_url),
                                                      allow_sensitive_query=allow_sensitive_query,
                                                      dns_timeout=min(DNS_TIMEOUT_SECONDS, remaining))
                    continue
                if status < 200 or status >= 300:
                    raise SourceError(f"HTTP 상태 {status}")
                break
        if len(payload) > max_bytes:
            raise SourceError("응답 크기 제한을 넘었습니다")
        return payload
    except SourceError:
        raise
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise SourceError(f"요청 실패: {type(exc).__name__}") from exc


def _parse_date(value: Any) -> Optional[str]:
    if not value:
        return None
    raw = str(value).strip()
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        parsed = None
    if parsed is None:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y.%m.%d"):
                try:
                    parsed = datetime.strptime(raw, pattern).replace(tzinfo=KST)
                    break
                except ValueError:
                    continue
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    # Keep the provider's offset so calendar event dates do not shift to the prior
    # UTC day; comparison helpers normalize to UTC when they compare instants.
    return parsed.isoformat(timespec="seconds")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_feed(payload: bytes, source_name: str, feed_url: str) -> List[Dict[str, Any]]:
    """Parse RSS/Atom metadata only; never return descriptions or article bodies."""
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise SourceError("RSS/Atom 형식이 손상되었습니다") from exc
    items: List[Dict[str, Any]] = []
    for entry in root.iter():
        if _local_name(entry.tag) not in {"item", "entry"}:
            continue
        fields = {_local_name(child.tag): child for child in list(entry)}
        title = " ".join("".join(fields.get("title", ET.Element("x")).itertext()).split())
        link_node = fields.get("link")
        url = ""
        if link_node is not None:
            url = (link_node.attrib.get("href") or (link_node.text or "")).strip()
            url = safe_metadata_url(urllib.parse.urljoin(feed_url, url)) if url else ""
        pub = next((fields[key] for key in ("pubdate", "published", "updated")
                    if key in fields), None)
        published = _parse_date("".join(pub.itertext()).strip() if pub is not None else "")
        if title and url:
            items.append({"title": title[:300], "url": url,
                          "provider_time": published, "source_name": source_name})
    return items


def parse_google_trends_csv(text: str, source_name: str = "Google Trends") -> List[Dict[str, Any]]:
    """Parse exported Trends CSV rows, accepting date/query and date/value layouts."""
    rows = list(csv.reader(io.StringIO(text.lstrip("\ufeff"))))
    if len(rows) < 2:
        raise SourceError("Google Trends CSV에 데이터가 없습니다")
    header = [cell.strip() for cell in rows[0]]
    if len(header) < 2:
        raise SourceError("Google Trends CSV 헤더가 올바르지 않습니다")
    items = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        date_value = row[0].strip()
        parsed = _parse_date(date_value)
        if parsed is None:
            continue
        # Google export shape is Week,<query names...>, followed by interest values.
        if header[0].strip().lower() in {"week", "date", "날짜"} and len(header) == len(row):
            for query, value in zip(header[1:], row[1:]):
                if query.strip() and value.strip() not in {"", "<1", "0", "-"}:
                    items.append({"title": query.strip()[:300], "url": "", "provider_time": parsed,
                                  "source_name": source_name, "kind": "trend"})
        else:
            # Also accept simple date,query CSV exports used by local tools.
            query = row[1].strip()
            if query and query.lower() not in {"<1", "0", "-"}:
                items.append({"title": query[:300], "url": "", "provider_time": parsed,
                              "source_name": source_name, "kind": "trend"})
    return items


class _MetaParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.metadata: Dict[str, str] = {}
        self.title: List[str] = []
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.metadata[key.lower()] = values["content"]
        elif tag.lower() == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)


def extract_page_metadata(payload: bytes, page_url: str, source_name: str = "원문 URL") -> Dict[str, Any]:
    """Extract title and published metadata only; page text is discarded."""
    parser = _MetaParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    title = parser.metadata.get("og:title") or parser.metadata.get("twitter:title") or " ".join(parser.title)
    date_value = next((parser.metadata[key] for key in
                       ("article:published_time", "datepublished", "pubdate", "date")
                       if parser.metadata.get(key)), None)
    if not title.strip():
        raise SourceError("페이지에서 제목 메타데이터를 찾지 못했습니다")
    return {"title": re.sub(r"\s+", " ", title).strip()[:300], "url": safe_metadata_url(page_url),
            "provider_time": _parse_date(date_value), "source_name": source_name}


def parse_seoul_events(payload: bytes) -> List[Dict[str, Any]]:
    """Parse Seoul culturalEventInfo JSON using its published STRTDATE/END_DATE schema."""
    import json
    try:
        data = json.loads(payload.decode("utf-8-sig"))
        rows = data["culturalEventInfo"]["row"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise SourceError("서울 문화행사 응답 스키마가 올바르지 않습니다") from exc
    return parse_seoul_csv_rows(rows)


def parse_seoul_csv(text: str) -> List[Dict[str, Any]]:
    """Parse exported Seoul event CSV with the official dataset headers."""
    import csv
    import io
    try:
        rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
    except (csv.Error, TypeError) as exc:
        raise SourceError("서울 행사 CSV 형식이 올바르지 않습니다") from exc
    return parse_seoul_csv_rows(rows)


def parse_seoul_csv_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Normalize Seoul event rows without inventing event or source details."""
    result = []
    for row in rows:
        title = str(row.get("TITLE") or row.get("title") or row.get("제목") or "").strip()
        if not title:
            continue
        result.append({"title": title[:300], "url": str(row.get("ORG_LINK") or row.get("org_link") or "").strip(),
                       "kind": "event",
                       "provider_time": _parse_date(row.get("REG_DATE") or row.get("DATE") or row.get("provider_time")),
                       "event_start": _parse_date(row.get("STRTDATE") or row.get("event_start")),
                       "event_end": _parse_date(row.get("END_DATE") or row.get("event_end")),
                       "location": " ".join(part for part in
                                            ("서울", str(row.get("GUNAME") or row.get("guname") or "").strip(),
                                             str(row.get("PLACE") or row.get("place") or "").strip()) if part),
                       "source_name": "서울 문화행사"})
    if not result:
        raise SourceError("가져올 서울 행사 행이 없습니다")
    return result


def parse_tour_events(payload: bytes) -> List[Dict[str, Any]]:
    """Parse TourAPI searchFestival2 items without conflating event and provider dates."""
    import json
    try:
        data = json.loads(payload.decode("utf-8-sig"))
        rows = data["response"]["body"]["items"].get("item", [])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        raise SourceError("TourAPI 응답 스키마가 올바르지 않습니다") from exc
    if isinstance(rows, dict):
        rows = [rows]
    result = []
    for row in rows:
        title = str(row.get("title") or "").strip()
        if title:
            result.append({"title": title[:300], "url": str(row.get("eventurl") or "").strip(),
                           "kind": "event",
                           "provider_time": None, "event_start": _parse_date(row.get("eventstartdate")),
                           "event_end": _parse_date(row.get("eventenddate")),
                           "location": " ".join(str(row.get(key) or "").strip()
                                                for key in ("addr1", "addr2") if row.get(key)),
                           "source_name": "한국관광공사 TourAPI"})
    return result


def naver_news_items(payload: bytes, source_name: str = "네이버 뉴스") -> List[Dict[str, Any]]:
    """Normalize Naver news JSON items while keeping publication time separate."""
    import json
    try:
        data = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceError("네이버 뉴스 응답이 올바르지 않습니다") from exc
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise SourceError("네이버 뉴스 응답 항목 형식이 올바르지 않습니다")
    rows = data["items"]
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise SourceError("네이버 뉴스 항목 형식이 올바르지 않습니다")
        title = re.sub(r"<[^>]+>", "", str(row.get("title") or "")).strip()
        url = safe_metadata_url(str(row.get("originallink") or row.get("link") or "").strip())
        if title and url:
            result.append({"title": title[:300], "url": url,
                           "provider_time": _parse_date(row.get("pubDate")),
                           "source_name": source_name, "kind": "news"})
    return result
