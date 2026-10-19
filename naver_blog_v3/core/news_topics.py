"""Profile-scoped, explicitly collected topic cards with offline imports."""
from __future__ import annotations

import csv
import difflib
import hashlib
import io
import json
import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, unquote, urlencode

from config import atomic_write_json, safe_load_json
from config import profile as _profile
from core import news_sources
from core.naver_api import news_request

KST = timezone(timedelta(hours=9))
PREFS_DEFAULT: Dict[str, Any] = {
    "interests": [], "region": "", "feeds": [], "naver_enabled": False,
    "tour_enabled": False, "seoul_enabled": False, "recent_days": 1,
    "upcoming_days": 30, "max_articles": 120, "auto_enabled": False,
}
_TEXT_LIMIT = 300
_log = logging.getLogger(__name__)
READ_ERROR = object()


def _read_json_readonly(path: Path, default: Any = None) -> Any:
    """Read JSON without the backup/repair writes performed by config.safe_load_json."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return default() if callable(default) else default
    except (OSError, UnicodeDecodeError) as exc:
        _log.warning("읽기 전용 JSON 로드 실패(%s): %s", Path(path).name, type(exc).__name__)
        return READ_ERROR
    try:
        return json.loads(text)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _log.warning("읽기 전용 JSON 손상(%s): %s", Path(path).name, type(exc).__name__)
        return READ_ERROR


def _profile_name(profile: Optional[str]) -> str:
    name = profile or _profile.active_profile()
    if not _profile.is_valid_name(name):
        raise ValueError("프로필 이름이 올바르지 않습니다")
    return name


def _prefs_path(profile: Optional[str]) -> Path:
    return _profile.app_data_root(_profile_name(profile)) / "config" / "news_topics.json"


def _cache_path(profile: Optional[str]) -> Path:
    return _profile.app_data_root(_profile_name(profile)) / "logs" / "news_topics.json"


def _attempt_path(profile: Optional[str]) -> Path:
    return _profile.app_data_root(_profile_name(profile)) / "logs" / "news_topics_attempt.json"


def _save_attempt(profile: Optional[str], attempted_at: str, operation: str,
                  failed_sources: List[str], errors: List[Dict[str, str]]) -> None:
    atomic_write_json(_attempt_path(profile), {
        "attempted_at": attempted_at, "operation": operation,
        "failed_sources": list(dict.fromkeys(failed_sources)), "errors": errors,
        "failed": bool(errors),
    })


def _criteria_signature(preferences: Dict[str, Any]) -> str:
    public_criteria = {key: preferences.get(key) for key in
                       ("interests", "region", "feeds", "naver_enabled", "tour_enabled",
                        "seoul_enabled", "recent_days", "upcoming_days", "max_articles")}
    canonical = json.dumps(public_criteria, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clean_text(value: Any, limit: int = _TEXT_LIMIT) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", "".join(ch for ch in value if ord(ch) >= 32)).strip()[:limit]


def _normalize_preferences(values: Any) -> Dict[str, Any]:
    if not isinstance(values, dict):
        raise ValueError("설정은 객체여야 합니다")
    interests = values.get("interests", [])
    feeds = values.get("feeds", [])
    if not isinstance(interests, list) or not isinstance(feeds, list):
        raise ValueError("interests와 feeds는 목록이어야 합니다")
    normalized_feeds = []
    for feed in feeds[:30]:
        if not isinstance(feed, dict):
            continue
        name, url = _clean_text(feed.get("name"), 100), _clean_text(feed.get("url"), 2048)
        if name and url:
            safe_url = news_sources.safe_metadata_url(url)
            if not safe_url:
                raise ValueError("feeds에는 공개 HTTP(S) URL이 필요합니다")
            normalized_feeds.append({"name": name, "url": safe_url, "enabled": bool(feed.get("enabled", False))})
    recent_days = int(values.get("recent_days", 1))
    if recent_days not in {1, 7}:
        raise ValueError("recent_days는 1 또는 7이어야 합니다")
    upcoming_days = int(values.get("upcoming_days", 30))
    max_articles = int(values.get("max_articles", 120))
    if not 0 <= upcoming_days <= 90 or not 1 <= max_articles <= 120:
        raise ValueError("행사 기간 또는 최대 기사 수가 허용 범위를 벗어났습니다")
    return {
        "interests": list(dict.fromkeys(_clean_text(item, 100) for item in interests if _clean_text(item, 100)))[:30],
        "region": _clean_text(values.get("region"), 80), "feeds": normalized_feeds,
        "naver_enabled": bool(values.get("naver_enabled", False)),
        "tour_enabled": bool(values.get("tour_enabled", False)),
        "seoul_enabled": bool(values.get("seoul_enabled", False)),
        "recent_days": recent_days, "upcoming_days": upcoming_days,
        "max_articles": max_articles, "auto_enabled": bool(values.get("auto_enabled", False)),
    }


def load_preferences(profile: Optional[str] = None, *, read_only: bool = False) -> Dict[str, Any]:
    """Load public collection preferences for one profile; source defaults are OFF."""
    loader = _read_json_readonly if read_only else safe_load_json
    value = loader(_prefs_path(profile), default=dict)
    if value is READ_ERROR:
        return dict(PREFS_DEFAULT)
    try:
        return _normalize_preferences(value) if isinstance(value, dict) else dict(PREFS_DEFAULT)
    except (ValueError, TypeError):
        return dict(PREFS_DEFAULT)


def save_preferences(values: Dict[str, Any], profile: Optional[str] = None) -> Dict[str, Any]:
    """Atomically save preferences and report success separately from stored values."""
    try:
        normalized = _normalize_preferences(values)
    except (ValueError, TypeError, OverflowError):
        return {"saved": False, "reason": "invalid_preferences"}
    try:
        atomic_write_json(_prefs_path(profile), normalized)
    except (OSError, ValueError, TypeError):
        return {**normalized, "saved": False, "reason": "save_failed"}
    return {**normalized, "saved": True, "reason": "saved"}


def _card(item: Dict[str, Any], collected_at: str) -> Dict[str, Any]:
    title = _clean_text(item.get("title"))
    url = news_sources.safe_metadata_url(_clean_text(item.get("url"), 2048))
    provider_time = item.get("provider_time")
    event_start, event_end = item.get("event_start"), item.get("event_end")
    identity = "|".join((title.casefold(), url.casefold(), str(provider_time or ""),
                         str(event_start or "")))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    keywords = [word for word in re.findall(r"[가-힣A-Za-z0-9]{2,}", title)[:8]]
    event_dates = [value for value in (event_start, event_end) if value]
    effective_date = item.get("effective_date")
    location = _clean_text(item.get("location"), 180)
    why_now = (f"행사 시작일 {event_start[:10]}이 등록되어 있습니다. 일정은 원문에서 확인하세요."
               if event_start else
               f"게시 시각 {provider_time[:10]} 자료입니다. 원문에서 현재 정보인지 확인하세요."
               if provider_time else "게시 시각과 실제 적용일을 확인할 원문 자료가 등록되었습니다.")
    return {
        "id": digest, "title": title, "kind": _clean_text(item.get("kind"), 20) or ("event" if event_dates else "news"),
        "source_name": _clean_text(item.get("source_name"), 100), "url": url,
        "provider_time": provider_time, "event_start": event_start, "event_end": event_end,
        "effective_date": effective_date, "collected_at": collected_at,
        "keywords": keywords,
        "location": location,
        "reader_question": f"{title}에서 독자가 확인해야 할 조건과 최신 내용은 무엇일까요?"[:120],
        "why_now": why_now, "sources": [{"name": _clean_text(item.get("source_name"), 100),
                                           "url": url, "provider_time": provider_time}],
        "verified": False,
    }


def _date_value(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=KST)


def _snapshot_age(snapshot: Dict[str, Any], now: Optional[datetime] = None) -> Optional[int]:
    collected = _date_value(snapshot.get("collected_at"))
    if collected is None:
        return None
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=KST)
    return max(0, int((instant.astimezone(timezone.utc) - collected.astimezone(timezone.utc)).total_seconds()))


def _mark_card_freshness(cards: List[Dict[str, Any]], now: Optional[datetime] = None,
                        criteria_signature: Optional[str] = None) -> List[Dict[str, Any]]:
    marked = []
    instant = now or datetime.now(timezone.utc)
    for original in cards:
        card = dict(original)
        age = _snapshot_age({"collected_at": card.get("collected_at")}, instant)
        card["cache_age_seconds"] = age
        card["stale"] = bool(card.get("stale")) or age is None or age > 24 * 3600
        if criteria_signature is not None and card.get("criteria_signature", criteria_signature) != criteria_signature:
            card["stale"] = True
            card["stale_reason"] = "collection_preferences_changed"
        marked.append(card)
    return marked


def _deduplicate(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    for card in cards:
        card_day = (card.get("event_start") or card.get("provider_time") or "")[:10]
        matched = None
        for existing in result:
            existing_day = (existing.get("event_start") or existing.get("provider_time") or "")[:10]
            both_days_known_different = bool(card_day and existing_day and card_day != existing_day)
            same_url = (bool(card.get("url") and card["url"].rstrip("/").casefold() ==
                             (existing.get("url") or "").rstrip("/").casefold())
                        and not both_days_known_different)
            title_ratio = difflib.SequenceMatcher(None, card.get("title", "").casefold(),
                                                  existing.get("title", "").casefold()).ratio()
            same_title_day = bool(card_day and card_day == existing_day and title_ratio >= 0.86)
            if same_url or same_title_day:
                matched = existing
                break
        if matched is None:
            result.append(card)
        else:
            known = {(src.get("url"), src.get("name")) for src in matched["sources"]}
            for source in card["sources"]:
                if (source.get("url"), source.get("name")) not in known:
                    matched["sources"].append(source)
    return result


def _source_error(name: str, exc: BaseException) -> Dict[str, str]:
    # Exception strings can contain keyed URLs or upstream text; expose only type and source label.
    if str(exc) == "API 키를 평문 연결로 보낼 수 없습니다. 서울 문화행사는 CSV를 이용하세요":
        return {"source": _clean_text(name, 100), "message": str(exc)}
    return {"source": _clean_text(name, 100), "message": f"수집 실패 ({type(exc).__name__})"}


def collect_topics(preferences: Optional[Dict[str, Any]] = None, profile: Optional[str] = None,
                   now: Optional[datetime] = None, transport: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Explicitly collect enabled feeds/APIs, preserving successful sources on partial failure."""
    prefs = _normalize_preferences(preferences if preferences is not None else load_preferences(profile))
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=KST)
    collected_at = instant.astimezone(timezone.utc).isoformat(timespec="seconds")
    signature = _criteria_signature(prefs)
    raw: List[Dict[str, Any]] = []
    errors: List[Dict[str, str]] = []
    successful_sources: set[str] = set()
    failed_sources: set[str] = set()
    current_sources = {feed["name"] for feed in prefs["feeds"] if feed["enabled"]}
    if prefs["naver_enabled"]:
        current_sources.add("네이버 뉴스")
    if prefs["seoul_enabled"]:
        current_sources.add("서울 문화행사")
    if prefs["tour_enabled"]:
        current_sources.add("한국관광공사 TourAPI")
    calls = 0
    bytes_used = 0
    deadline = time.monotonic() + 40

    def fetch_source(name: str, url: str, parser: Callable[[bytes], List[Dict[str, Any]]],
                     *, headers: Optional[Dict[str, str]] = None,
                     allow_sensitive_query: bool = False,
                     allow_redirects: bool = True) -> None:
        nonlocal calls, bytes_used
        remaining_time = deadline - time.monotonic()
        remaining_bytes = 4 * news_sources.MAX_BYTES - bytes_used
        if calls >= news_sources.MAX_CALLS or remaining_time <= 0 or remaining_bytes <= 0:
            errors.append({"source": name, "message": "수집 예산 제한"})
            failed_sources.add(name)
            return
        calls += 1
        try:
            payload = news_sources.fetch_bytes(
                url, transport=transport, headers=headers or {}, allow_redirects=allow_redirects and not bool(headers),
                allow_sensitive_query=allow_sensitive_query,
                timeout=max(1, min(news_sources.TIMEOUT_SECONDS, int(remaining_time))),
                max_bytes=min(news_sources.MAX_BYTES, remaining_bytes))
            bytes_used += len(payload)
            raw.extend(parser(payload))
            successful_sources.add(name)
        except (news_sources.SourceError, ValueError, TypeError) as exc:
            errors.append(_source_error(name, exc))
            failed_sources.add(name)

    for feed in prefs["feeds"]:
        if feed["enabled"]:
            feed_url = feed["url"]
            fetch_source(feed["name"], feed_url,
                         lambda payload, n=feed["name"], u=feed_url: news_sources.parse_feed(payload, n, u))
    if prefs["naver_enabled"]:
        try:
            from core.naver_api import resolve_provider, runtime_credentials
            if not prefs["interests"]:
                raise ValueError("news_interests_missing")
            provider = resolve_provider()
            client_id, client_secret = runtime_credentials(provider)
            if not client_id or not client_secret:
                raise ValueError("credentials_missing")
            for interest in prefs["interests"]:
                remaining_time = deadline - time.monotonic()
                remaining_bytes = 4 * news_sources.MAX_BYTES - bytes_used
                if calls >= news_sources.MAX_CALLS or remaining_time <= 0 or remaining_bytes <= 0:
                    failed_sources.add("네이버 뉴스")
                    errors.append({"source": "네이버 뉴스", "message": "수집 예산 제한"})
                    break
                query = " ".join(part for part in (interest, prefs["region"]) if part)
                request = news_request(query, client_id, client_secret, provider, display=30)
                calls += 1
                payload = news_sources.fetch_bytes(
                    request["url"], transport=transport, headers=request["headers"], allow_redirects=False,
                    timeout=max(1, min(news_sources.TIMEOUT_SECONDS, int(remaining_time))),
                    max_bytes=min(news_sources.MAX_BYTES, remaining_bytes))
                bytes_used += len(payload)
                raw.extend(news_sources.naver_news_items(payload))
                successful_sources.add("네이버 뉴스")
        except (ValueError, news_sources.SourceError, TypeError) as exc:
            errors.append(_source_error("네이버 뉴스", exc))
            failed_sources.add("네이버 뉴스")
    if prefs["seoul_enabled"]:
        try:
            from config.crypto import load_secret
            key = load_secret("seoul_api_key", default="")
            if not key:
                raise ValueError("credentials_missing")
            url = news_sources.SEOUL_EVENTS_URL.format(key=quote(unquote(str(key)), safe=""))
            fetch_source("서울 문화행사", url, news_sources.parse_seoul_events,
                         allow_sensitive_query=True, allow_redirects=False)
        except (ValueError, news_sources.SourceError, TypeError) as exc:
            errors.append(_source_error("서울 문화행사", exc))
            failed_sources.add("서울 문화행사")
    if prefs["tour_enabled"]:
        try:
            from config.crypto import load_secret
            service_key = load_secret("tourapi_service_key", default="")
            if not service_key:
                raise ValueError("credentials_missing")
            start_day = instant.astimezone(KST).strftime("%Y%m%d")
            end_day = (instant.astimezone(KST) + timedelta(days=prefs["upcoming_days"])).strftime("%Y%m%d")
            query = urlencode({"MobileOS": "ETC", "MobileApp": "NaverBlogAuto",
                               "eventStartDate": start_day, "eventEndDate": end_day,
                               "serviceKey": unquote(str(service_key)), "numOfRows": 100, "pageNo": 1,
                               "_type": "json", "arrange": "A"})
            fetch_source("한국관광공사 TourAPI", f"{news_sources.TOUR_FESTIVAL_URL}?{query}",
                         news_sources.parse_tour_events, allow_sensitive_query=True,
                         allow_redirects=False)
        except (ValueError, news_sources.SourceError, TypeError) as exc:
            errors.append(_source_error("한국관광공사 TourAPI", exc))
            failed_sources.add("한국관광공사 TourAPI")

    cutoff = instant.astimezone(timezone.utc) - timedelta(days=prefs["recent_days"])
    cards = []
    today = instant.astimezone(KST).date()
    event_horizon = today + timedelta(days=prefs["upcoming_days"])
    for item in raw:
        provider_dt, start_dt = _date_value(item.get("provider_time")), _date_value(item.get("event_start"))
        if provider_dt and provider_dt < cutoff and not start_dt:
            continue
        end_dt = _date_value(item.get("event_end"))
        if start_dt or end_dt:
            start_date = start_dt.astimezone(KST).date() if start_dt else end_dt.astimezone(KST).date()
            end_date = end_dt.astimezone(KST).date() if end_dt else start_date
            if end_date < start_date:
                errors.append({"source": _clean_text(item.get("source_name"), 100),
                               "message": "행사 날짜 범위가 올바르지 않아 제외했습니다"})
                continue
            if end_date < today or start_date > event_horizon:
                continue
        if prefs["region"]:
            region = prefs["region"].casefold()
            searchable = " ".join(str(item.get(key) or "") for key in
                                  ("title", "location")).casefold()
            if region not in searchable:
                continue
        card = _card(item, collected_at)
        card["criteria_signature"] = signature
        cards.append(card)
    cards = _deduplicate(cards)[:prefs["max_articles"]]
    previous = safe_load_json(_cache_path(profile), default=dict)
    old_items = previous.get("items", []) if isinstance(previous, dict) else []
    if successful_sources:
        retained = [item for item in old_items if isinstance(item, dict)
                    and item.get("source_name") in failed_sources
                    and item.get("source_name") in current_sources
                    and item.get("criteria_signature", previous.get("criteria_signature")) == signature]
        cards = _deduplicate(cards + retained)[:prefs["max_articles"]]
        marked_cards = _mark_card_freshness(cards, instant, signature)
        for card in marked_cards:
            if card.get("source_name") in failed_sources:
                card["from_cache"] = True
                card["refresh_failed"] = True
                card["stale"] = True
                card["stale_reason"] = "refresh_failed"
        result = {"items": marked_cards, "errors": errors,
                  "collected_at": collected_at, "attempted_at": collected_at,
                  "cache_age_seconds": 0, "stale": any(card["stale"] for card in marked_cards),
                  "criteria_signature": signature}
        atomic_write_json(_cache_path(profile), result)
        _save_attempt(profile, collected_at, "collect", sorted(failed_sources), errors)
    else:
        _save_attempt(profile, collected_at, "collect", sorted(failed_sources or current_sources), errors)
        old_snapshot = previous if isinstance(previous, dict) else {}
        prior = load_snapshot(profile)
        result = {"items": prior.get("items", []), "errors": errors,
                  "collected_at": old_snapshot.get("collected_at"), "attempted_at": collected_at,
                  "cache_age_seconds": prior.get("cache_age_seconds"), "stale": True,
                  "last_attempt_failed": True,
                  "criteria_signature": old_snapshot.get("criteria_signature")}
    return result


def load_snapshot(profile: Optional[str] = None,
                  effective_preferences: Optional[Dict[str, Any]] = None,
                  *, read_only: bool = False) -> Dict[str, Any]:
    """Load last collection with explicit cache age and stale marker."""
    loader = _read_json_readonly if read_only else safe_load_json
    snapshot = loader(_cache_path(profile), default=dict)
    cache_read_failed = snapshot is READ_ERROR
    if not isinstance(snapshot, dict):
        snapshot = {}
    age_seconds = _snapshot_age(snapshot)
    items = snapshot.get("items", []) if isinstance(snapshot.get("items"), list) else []
    marked_items = _mark_card_freshness(items)
    current_signature = _criteria_signature(
        _normalize_preferences(effective_preferences)
        if effective_preferences is not None else load_preferences(profile, read_only=read_only))
    stored_signature = snapshot.get("criteria_signature")
    for item in marked_items:
        card_signature = item.get("criteria_signature", stored_signature)
        if card_signature != current_signature:
            item["stale"] = True
            item["stale_reason"] = "collection_preferences_changed"
    attempt_path = _attempt_path(profile)
    attempt = loader(attempt_path, default=dict)
    attempt_read_failed = attempt is READ_ERROR
    attempt = attempt if isinstance(attempt, dict) else {}
    attempt_at = _date_value(attempt.get("attempted_at"))
    collected_dt = _date_value(snapshot.get("collected_at"))
    attempt_is_newer = bool(attempt_at and (collected_dt is None or attempt_at >= collected_dt))
    failed_sources = set(attempt.get("failed_sources", [])) if attempt_is_newer else set()
    for item in marked_items:
        if item.get("source_name") in failed_sources:
            item["from_cache"] = True
            item["refresh_failed"] = True
            item["stale"] = True
            item["stale_reason"] = "refresh_failed"
        if attempt_read_failed:
            item["refresh_failed"] = True
            item["stale"] = True
            item["stale_reason"] = "attempt_state_unreadable"
    return {"items": marked_items,
            "errors": snapshot.get("errors", []) if isinstance(snapshot.get("errors"), list) else [],
            "collected_at": snapshot.get("collected_at"), "cache_age_seconds": age_seconds,
            "stale": cache_read_failed or age_seconds is None or age_seconds > 24 * 3600
            or any(item["stale"] for item in marked_items)
            or bool(attempt_is_newer and attempt.get("failed")) or attempt_read_failed,
            "read_failed": cache_read_failed or attempt_read_failed,
            "attempted_at": attempt.get("attempted_at") if attempt_is_newer else None,
            "last_attempt_failed": bool(attempt_is_newer and attempt.get("failed")),
            "attempt_errors": attempt.get("errors", []) if attempt_is_newer else []}


def add_manual_url(url: str, profile: Optional[str] = None,
                   transport: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Explicitly fetch and add metadata for one public original URL; body text is discarded."""
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    signature = _criteria_signature(load_preferences(profile))
    errors: List[Dict[str, str]] = []
    items = []
    try:
        safe_url = news_sources.validate_public_url(url)
        metadata = news_sources.extract_page_metadata(news_sources.fetch_bytes(safe_url, transport=transport), safe_url)
        items = [_card(metadata, collected_at)]
        items[0]["criteria_signature"] = signature
    except (news_sources.SourceError, ValueError, TypeError) as exc:
        errors.append(_source_error("원문 URL", exc))
    snapshot = load_snapshot(profile)
    merged = _deduplicate(list(snapshot.get("items", [])) + items)
    if items:
        marked = _mark_card_freshness(merged, criteria_signature=signature)
        result = {"items": marked, "errors": errors,
                  "collected_at": collected_at, "attempted_at": collected_at,
                  "cache_age_seconds": 0, "stale": any(card["stale"] for card in marked),
                  "criteria_signature": signature}
        atomic_write_json(_cache_path(profile), result)
        _save_attempt(profile, collected_at, "manual_url", [], [])
    else:
        _save_attempt(profile, collected_at, "manual_url", ["원문 URL"], errors)
        snapshot = load_snapshot(profile)
        result = {"items": snapshot.get("items", []), "errors": errors,
                  "collected_at": snapshot.get("collected_at"), "attempted_at": collected_at,
                  "cache_age_seconds": snapshot.get("cache_age_seconds"),
                  "stale": True, "last_attempt_failed": True}
    return result


def import_csv(text: str, kind: str, profile: Optional[str] = None) -> Dict[str, Any]:
    """Import a Trends export or explicit metadata CSV without network access."""
    collected_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    signature = _criteria_signature(load_preferences(profile))
    errors: List[Dict[str, str]] = []
    raw: List[Dict[str, Any]] = []
    try:
        if kind in {"google_trends", "trends"}:
            raw = news_sources.parse_google_trends_csv(text)
        elif kind == "seoul":
            raw = news_sources.parse_seoul_csv(text)
        elif kind in {"news", "events", "topics"}:
            rows = list(csv.DictReader(io.StringIO(text.lstrip("\ufeff"))))
            for row in rows:
                title = _clean_text(row.get("title") or row.get("제목"))
                if not title:
                    continue
                raw.append({"title": title, "url": _clean_text(row.get("url") or row.get("link"), 2048),
                            "provider_time": news_sources._parse_date(row.get("provider_time") or row.get("published_at")),
                            "event_start": news_sources._parse_date(row.get("event_start")),
                            "event_end": news_sources._parse_date(row.get("event_end")),
                            "effective_date": news_sources._parse_date(row.get("effective_date")),
                            "kind": "event" if kind == "events" else "news",
                            "source_name": _clean_text(row.get("source") or row.get("source_name") or "CSV", 100)})
        else:
            raise ValueError("지원하지 않는 CSV 종류입니다")
        if not raw:
            raise ValueError("가져올 행이 없습니다")
    except (ValueError, news_sources.SourceError, csv.Error, TypeError) as exc:
        errors.append(_source_error("CSV", exc))
    items = [_card(item, collected_at) for item in raw]
    for item in items:
        item["criteria_signature"] = signature
    previous = load_snapshot(profile)
    merged = _deduplicate(list(previous.get("items", [])) + items)
    if items:
        marked = _mark_card_freshness(merged, criteria_signature=signature)
        result = {"items": marked, "errors": errors,
                  "collected_at": collected_at, "attempted_at": collected_at,
                  "cache_age_seconds": 0, "stale": any(card["stale"] for card in marked),
                  "criteria_signature": signature}
        atomic_write_json(_cache_path(profile), result)
        _save_attempt(profile, collected_at, "csv_import", [], [])
    else:
        _save_attempt(profile, collected_at, "csv_import", ["CSV"], errors)
        previous = load_snapshot(profile)
        result = {"items": previous.get("items", []), "errors": errors,
                  "collected_at": previous.get("collected_at"), "attempted_at": collected_at,
                  "cache_age_seconds": previous.get("cache_age_seconds"),
                  "stale": True, "last_attempt_failed": True}
    return result


def create_draft(card: Dict[str, Any], *, title: Optional[str] = None,
                 reader_question: Optional[str] = None, profile: Optional[str] = None,
                 stop_flag: Any = None, generator: Optional[Callable[..., Any]] = None,
                 saver: Optional[Callable[..., Any]] = None) -> Dict[str, Any]:
    """Generate and save one explicitly selected topic as a draft, without image/publish actions."""
    if not isinstance(card, dict) or not _clean_text(card.get("title")):
        return {"saved": False, "slug": None, "reason": "invalid_card"}
    active = _profile.active_profile()
    if profile is not None and _profile_name(profile) != active:
        return {"saved": False, "slug": None, "reason": "profile_mismatch"}
    clean_title = _clean_text(title if title is not None else card.get("title"))
    question = _clean_text(reader_question if reader_question is not None else card.get("reader_question"), 120)
    if not clean_title:
        return {"saved": False, "slug": None, "reason": "empty_title"}
    if stop_flag is not None and getattr(stop_flag, "is_set", lambda: False)():
        return {"saved": False, "slug": None, "reason": "stopped"}
    context = {key: card.get(key) for key in ("id", "title", "kind", "source_name", "url", "location", "provider_time",
                                               "event_start", "event_end", "effective_date", "collected_at",
                                               "why_now", "sources")}
    context["reader_question"] = question
    context["editorial_title"] = clean_title
    context["verified"] = False
    try:
        if generator is None:
            from core.content_generator import generate_blog_post
            generator = generate_blog_post
        post = generator(clean_title, stop_flag=stop_flag, topic_context=context)
        if not isinstance(post, dict) or not isinstance(post.get("content"), str) or not post["content"].strip():
            return {"saved": False, "slug": None, "reason": "generation_failed"}
        if post.get("_failed") or (stop_flag is not None and getattr(stop_flag, "is_set", lambda: False)()):
            return {"saved": False, "slug": None, "reason": "stopped" if stop_flag and stop_flag.is_set() else "generation_failed"}
        brief = post.get("editorial_brief")
        if not isinstance(brief, dict):
            brief = post.get("_editorial_brief")
        if isinstance(brief, dict):
            brief["topic_context"] = context
        else:
            post["editorial_brief"] = {"topic_context": context}
        if saver is None:
            from core.draft_store import save_draft
            saver = save_draft
        slug = f"news-{uuid.uuid4().hex}"
        saved_path = saver(slug, post, [])
        if not saved_path:
            return {"saved": False, "slug": None, "reason": "save_failed"}
        return {"saved": True, "slug": slug, "reason": "saved"}
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        return {"saved": False, "slug": None, "reason": f"error_{type(exc).__name__}"}
