"""Bounded, read-only HTTPS retrieval for official policy source pages."""
from __future__ import annotations

import html
import http.client
import ipaddress
import re
import queue
import socket
import ssl
import threading
import time
from html.parser import HTMLParser
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit, urlunsplit

from config.logger import get_logger

log = get_logger(__name__)

_OFFICIAL_SUFFIXES = ("gov.kr", "go.kr")
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS = 2
_MAX_SOURCES = 3
_MAX_RESPONSE_BYTES = 256 * 1024
_MAX_TEXT_CHARS = 6000
_REQUEST_TIMEOUT_SECONDS = 8.0
_READ_CHUNK_BYTES = 8192
_HTML_TAGS_TO_SKIP = frozenset({"script", "style", "noscript", "svg"})
_CHALLENGE_MARKERS = (
    "verify you are human", "checking your browser", "just a moment",
    "captcha", "cloudflare", "access denied", "automated requests",
    "자동입력 방지", "보안문자를 입력", "로봇이 아닙니다",
    "로그인이 필요합니다", "로그인 후 이용", "sign in to continue",
    "log in to continue",
)
_ERROR_MARKERS = (
    "404 not found", "페이지를 찾을 수 없습니다", "요청하신 페이지가 없습니다",
    "요청하신 페이지를 찾을 수 없습니다", "서비스를 찾을 수 없습니다",
    "서비스 이용에 불편을 드려", "시스템 오류가 발생", "500 internal server error",
    "internal server error", "page not found",
)
_DNS_WORKERS = 2
_DNS_PENDING = 4
_DNS_QUEUE = queue.Queue(maxsize=_DNS_PENDING)
_DNS_START_LOCK = threading.Lock()
_DNS_STARTED = False


class _DNSRequest:
    def __init__(self, hostname: str) -> None:
        self.hostname = hostname
        self.done = threading.Event()
        self.records = None
        self.failed = False


def _dns_worker() -> None:
    while True:
        request = _DNS_QUEUE.get()
        try:
            request.records = socket.getaddrinfo(
                request.hostname, 443, type=socket.SOCK_STREAM)
        except (OSError, socket.gaierror):
            request.failed = True
        finally:
            request.done.set()
            _DNS_QUEUE.task_done()


def _ensure_dns_workers() -> None:
    global _DNS_STARTED
    if _DNS_STARTED:
        return
    with _DNS_START_LOCK:
        if _DNS_STARTED:
            return
        for _worker_index in range(_DNS_WORKERS):
            threading.Thread(target=_dns_worker, name="official-source-dns",
                             daemon=True).start()
        _DNS_STARTED = True


class _DeadlineGuard:
    """Interrupt tracked sockets when a request is cancelled or expires."""

    def __init__(self, deadline: float, stop_flag=None) -> None:
        self.deadline = deadline
        self.stop_flag = stop_flag
        self._lock = threading.Lock()
        self._sockets = set()
        self._closed = threading.Event()
        self._watchdog = threading.Thread(target=self._watch, daemon=True,
                                          name="official-source-deadline")
        self._watchdog.start()

    def expired(self) -> bool:
        return (time.monotonic() >= self.deadline
                or (self.stop_flag is not None and self.stop_flag.is_set()))

    def check(self) -> None:
        if self.expired():
            raise TimeoutError("official source request cancelled or expired")

    def track(self, sock) -> None:
        with self._lock:
            self._sockets.add(sock)
        if self.expired():
            self.interrupt()
            self.check()

    def interrupt(self) -> None:
        with self._lock:
            sockets = tuple(self._sockets)
        for tracked in sockets:
            try:
                tracked.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                tracked.close()
            except OSError:
                pass

    def _watch(self) -> None:
        while not self._closed.wait(0.02):
            if self.expired():
                self.interrupt()
                return

    def close(self) -> None:
        self._closed.set()
        self._watchdog.join(timeout=0.1)
        self.interrupt()


class _TextExtractor(HTMLParser):
    """Extract visible text while excluding executable and non-content elements."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.casefold() in _HTML_TAGS_TO_SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in _HTML_TAGS_TO_SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self._parts.append(data.strip())

    def text(self) -> str:
        return re.sub(r"\s+", " ", html.unescape(" ".join(self._parts))).strip()


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a previously validated IP while retaining HTTPS SNI/cert checks."""

    def __init__(self, hostname: str, address: str, deadline: float,
                 guard: _DeadlineGuard) -> None:
        timeout = max(0.01, deadline - time.monotonic())
        super().__init__(hostname, port=443, timeout=timeout,
                         context=ssl.create_default_context())
        self._pinned_address = address
        self._deadline = deadline
        self._guard = guard

    def connect(self) -> None:
        self._guard.check()
        family = (socket.AF_INET6 if ipaddress.ip_address(self._pinned_address).version == 6
                  else socket.AF_INET)
        raw_socket = socket.socket(family, socket.SOCK_STREAM)
        try:
            self._guard.track(raw_socket)
            raw_socket.settimeout(max(0.01, self._deadline - time.monotonic()))
            raw_socket.connect((self._pinned_address, self.port))
            self._guard.check()
            tls_socket = self._context.wrap_socket(
                raw_socket, server_hostname=self.host, do_handshake_on_connect=False)
            self.sock = tls_socket
            self._guard.track(self.sock)
            self._guard.check()
            self.sock.settimeout(max(0.01, self._deadline - time.monotonic()))
            self.sock.do_handshake()
            self._guard.check()
        except (OSError, ssl.SSLError):
            raw_socket.close()
            raise


def _parse_official_detail_url(url: str) -> Optional[Tuple[str, str]]:
    """Return normalized URL and hostname only for strict HTTPS official details."""
    if (not isinstance(url, str) or len(url) > 2048
            or any(ord(character) < 32 or ord(character) == 127 for character in url)):
        return None
    try:
        parsed = urlsplit(url.strip())
        hostname = (parsed.hostname or "").lower()
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return None
    if (parsed.scheme.casefold() != "https" or not hostname or parsed.username is not None
            or parsed.password is not None or port not in (None, 443)
            or hostname.endswith(".") or not any(
                hostname == suffix or hostname.endswith("." + suffix)
                for suffix in _OFFICIAL_SUFFIXES)):
        return None
    path = parsed.path or "/"
    if path == "/" or re.fullmatch(r"/(?:index|main|home)(?:\.[a-z0-9]+)?/?", path,
                                    flags=re.IGNORECASE):
        return None
    normalized = urlunsplit(("https", hostname, path, parsed.query, ""))
    return normalized, hostname


def _resolve_public_address(hostname: str, deadline: float,
                            stop_flag=None) -> Optional[str]:
    """Resolve a host and reject the entire result if any address is non-public."""
    _ensure_dns_workers()
    request = _DNSRequest(hostname)
    try:
        _DNS_QUEUE.put_nowait(request)
    except queue.Full:
        return None
    while not request.done.wait(0.02):
        if time.monotonic() >= deadline or (stop_flag is not None and stop_flag.is_set()):
            return None
    if request.failed or request.records is None:
        log.debug("Official source DNS lookup failed")
        return None
    records = request.records
    addresses: List[str] = []
    for record in records:
        raw_address = record[4][0]
        try:
            parsed_address = ipaddress.ip_address(raw_address)
        except ValueError:
            return None
        if not parsed_address.is_global:
            return None
        canonical = str(parsed_address)
        if canonical not in addresses:
            addresses.append(canonical)
    return addresses[0] if addresses else None


def _read_bounded_body(response: http.client.HTTPResponse,
                       connection: _PinnedHTTPSConnection,
                       deadline: float, guard: _DeadlineGuard) -> Optional[bytes]:
    """Read at most the configured response size, respecting cancellation/deadline."""
    content_length = response.getheader("Content-Length")
    if content_length:
        try:
            if int(content_length) > _MAX_RESPONSE_BYTES:
                return None
        except ValueError:
            return None
    body = bytearray()
    while True:
        guard.check()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        guard.check()
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        chunk = response.read(min(_READ_CHUNK_BYTES, _MAX_RESPONSE_BYTES + 1 - len(body)))
        if not chunk:
            break
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            return None
    return bytes(body)


def _fetch_page(url: str, stop_flag=None) -> Optional[Dict[str, str]]:
    """Fetch one official page with pinned DNS, bounded redirects, bytes, and time."""
    deadline = time.monotonic() + _REQUEST_TIMEOUT_SECONDS
    guard = _DeadlineGuard(deadline, stop_flag)
    current_url = url
    visited: Set[str] = set()
    redirects = 0
    try:
        while True:
            guard.check()
            validated = _parse_official_detail_url(current_url)
            if validated is None:
                return None
            current_url, hostname = validated
            if current_url in visited:
                return None
            visited.add(current_url)
            address = _resolve_public_address(hostname, deadline, stop_flag)
            if address is None:
                return None
            guard.check()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            parsed = urlsplit(current_url)
            request_target = parsed.path or "/"
            if parsed.query:
                request_target += "?" + parsed.query
            connection = _PinnedHTTPSConnection(hostname, address, deadline, guard)
            try:
                guard.check()
                connection.request("GET", request_target, headers={
                    "Host": hostname,
                    "User-Agent": "NaverBlogAutomation-OfficialSourceReader/1.0",
                    "Accept": "text/html, text/plain;q=0.9",
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                })
                guard.check()
                response = connection.getresponse()
                guard.check()
                if response.status in _REDIRECT_STATUSES:
                    location = response.getheader("Location")
                    if not location or redirects >= _MAX_REDIRECTS:
                        return None
                    next_url = urljoin(current_url, location)
                    if _parse_official_detail_url(next_url) is None:
                        return None
                    redirects += 1
                    current_url = next_url
                    connection.close()
                    continue
                if response.status != 200:
                    return None
                content_type = (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
                if content_type not in {"text/html", "text/plain", "application/xhtml+xml"}:
                    return None
                content_encoding = (response.getheader("Content-Encoding") or "identity").strip().lower()
                if content_encoding not in {"", "identity"}:
                    return None
                body = _read_bounded_body(response, connection, deadline, guard)
                if not body:
                    return None
                charset = response.headers.get_content_charset() or "utf-8"
                try:
                    decoded = body.decode(charset, errors="replace")
                except LookupError:
                    decoded = body.decode("utf-8", errors="replace")
                if content_type == "text/plain":
                    text = re.sub(r"\s+", " ", decoded).strip()
                else:
                    extractor = _TextExtractor()
                    try:
                        extractor.feed(decoded)
                        extractor.close()
                    except (ValueError, AssertionError):
                        return None
                    text = extractor.text()
                if not text:
                    return None
                folded = text.casefold()
                if any(marker in folded for marker in _CHALLENGE_MARKERS + _ERROR_MARKERS):
                    return None
                guard.check()
                return {"url": current_url, "text": text[:_MAX_TEXT_CHARS]}
            except (OSError, http.client.HTTPException, ssl.SSLError, ValueError):
                log.warning("Official source read failed", exc_info=True)
                return None
            finally:
                try:
                    connection.close()
                except OSError:
                    log.warning("Official source connection cleanup failed", exc_info=True)
    except TimeoutError:
        return None
    finally:
        guard.close()


def read_official_sources(urls, *, stop_flag=None,
                          max_sources: int = 3) -> List[Dict[str, str]]:
    """Read up to three unique official detail URLs; invalid or failed pages are skipped."""
    if (not isinstance(urls, (list, tuple)) or not isinstance(max_sources, int)
            or isinstance(max_sources, bool) or max_sources <= 0):
        return []
    source_limit = min(_MAX_SOURCES, max_sources)
    sources: List[Dict[str, str]] = []
    seen: Set[str] = set()
    attempted = 0
    for candidate in urls:
        if stop_flag is not None and stop_flag.is_set():
            break
        if not isinstance(candidate, str):
            continue
        validated = _parse_official_detail_url(candidate)
        if validated is None:
            continue
        normalized, _hostname = validated
        if normalized in seen:
            continue
        seen.add(normalized)
        if attempted >= source_limit:
            break
        attempted += 1
        page = _fetch_page(normalized, stop_flag=stop_flag)
        if stop_flag is not None and stop_flag.is_set():
            return []
        if page is not None:
            sources.append(page)
            if len(sources) >= source_limit:
                break
    return sources
