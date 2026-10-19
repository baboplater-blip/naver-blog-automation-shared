"""Pure request builders and runtime provider selection for Naver Open APIs."""
from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional
from urllib.parse import urlencode

LEGACY_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
HUB_NEWS_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
LEGACY_DATALAB_URL = "https://openapi.naver.com/v1/datalab/search"
HUB_DATALAB_URL = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"


def resolve_provider(value: Optional[str] = None) -> str:
    """Resolve only explicitly supported provider names; invalid names fail closed."""
    if value is None:
        try:
            from config.crypto import load_secret
            stored_provider = load_secret("naver_api_provider", default=None)
        except ImportError:
            stored_provider = None
        if stored_provider:
            value = stored_provider
    if value is None:
        try:
            from config import settings
            value = getattr(settings, "NAVER_API_PROVIDER", "legacy")
        except (ImportError, AttributeError):
            value = "legacy"
    provider = str(value).strip().lower()
    if provider not in {"legacy", "api_hub"}:
        raise ValueError("NAVER_API_PROVIDER must be 'legacy' or 'api_hub'")
    return provider


def request_headers(provider: str, client_id: str, client_secret: str) -> Dict[str, str]:
    """Build auth headers without reading credentials or settings."""
    provider = resolve_provider(provider)
    if not client_id or not client_secret:
        raise ValueError("Naver API credentials are required")
    if provider == "legacy":
        return {"X-Naver-Client-Id": client_id,
                "X-Naver-Client-Secret": client_secret,
                "Accept": "application/json"}
    return {"X-NCP-APIGW-API-KEY-ID": client_id,
            "X-NCP-APIGW-API-KEY": client_secret,
            "Accept": "application/json"}


def news_request(query: str, client_id: str, client_secret: str,
                 provider: Optional[str] = None, *, display: int = 30) -> Dict[str, Any]:
    """Build a Naver news request descriptor for the selected provider."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not 1 <= int(display) <= 100:
        raise ValueError("display must be between 1 and 100")
    selected = resolve_provider(provider)
    params = {"query": query.strip(), "display": int(display), "sort": "date"}
    return {"method": "GET", "url": f"{LEGACY_NEWS_URL}?{urlencode(params)}"
            if selected == "legacy" else f"{HUB_NEWS_URL}?{urlencode(params)}",
            "headers": request_headers(selected, client_id, client_secret),
            "body": None, "provider": selected}


def datalab_request(body: Mapping[str, Any], client_id: str, client_secret: str,
                    provider: Optional[str] = None) -> Dict[str, Any]:
    """Build the legacy or API HUB DataLab POST request."""
    if not isinstance(body, Mapping):
        raise ValueError("body must be a mapping")
    selected = resolve_provider(provider)
    return {"method": "POST", "url": LEGACY_DATALAB_URL if selected == "legacy" else HUB_DATALAB_URL,
            "headers": {**request_headers(selected, client_id, client_secret),
                        "Content-Type": "application/json; charset=utf-8"},
            "body": json.dumps(dict(body), ensure_ascii=False).encode("utf-8"),
            "provider": selected}


def runtime_credentials(provider: Optional[str] = None) -> tuple[str, str]:
    """Read selected provider credentials at call time; never expose them in errors."""
    selected = resolve_provider(provider)
    if selected == "api_hub":
        try:
            from config.crypto import load_secret
            return (str(load_secret("api_hub_key_id", default="") or ""),
                    str(load_secret("api_hub_key", default="") or ""))
        except ImportError:
            return "", ""
    try:
        from config import settings
    except ImportError:
        return "", ""
    id_name = "NAVER_API_CLIENT_ID"
    secret_name = "NAVER_API_CLIENT_SECRET"
    return str(getattr(settings, id_name, "") or ""), str(getattr(settings, secret_name, "") or "")
