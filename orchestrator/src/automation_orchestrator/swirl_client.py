from __future__ import annotations

import base64
import json
import os
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .models import SwirlSearchResponse, SwirlSearchResult


class SwirlSearchError(RuntimeError):
    pass


def _text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return " ".join(value.split())[:limit]


def _number(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _result_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    structured = payload.get("structured")
    if isinstance(structured, dict):
        payload = structured
    direct = payload.get("results")
    if not isinstance(direct, list):
        direct = payload.get("json_results")
    if not isinstance(direct, list):
        return []

    flattened: list[dict[str, Any]] = []
    for item in direct:
        if not isinstance(item, dict):
            continue
        nested = item.get("json_results")
        if isinstance(nested, list):
            source = item.get("searchprovider") or item.get("source")
            for child in nested:
                if isinstance(child, dict):
                    flattened.append({"_parent_source": source, **child})
        else:
            flattened.append(item)
    return flattened


def normalize_swirl_response(
    query: str,
    payload: dict[str, Any],
    *,
    max_results: int,
) -> SwirlSearchResponse:
    structured = payload.get("structured")
    metadata = structured if isinstance(structured, dict) else payload
    search_id = metadata.get("search_id") or metadata.get("id")
    if search_id is None:
        info = metadata.get("info")
        search = info.get("search") if isinstance(info, dict) else None
        if isinstance(search, dict):
            search_id = search.get("id")
    results: list[SwirlSearchResult] = []
    seen: set[tuple[str, str]] = set()
    for item in _result_items(payload):
        url = _text(item.get("url") or item.get("link") or item.get("uri"), limit=4000)
        title = _text(item.get("title") or item.get("name") or url, limit=1000)
        if not url or not title:
            continue
        key = (url, title)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            SwirlSearchResult(
                title=title,
                snippet=_text(
                    item.get("snippet")
                    or item.get("body")
                    or item.get("description")
                    or item.get("content"),
                    limit=2000,
                ),
                url=url,
                source=_text(
                    item.get("source")
                    or item.get("searchprovider")
                    or item.get("provider")
                    or item.get("_parent_source")
                    or "unknown",
                    limit=300,
                ),
                updated_at=_text(
                    item.get("date_published")
                    or item.get("date_updated")
                    or item.get("updated_at"),
                    limit=100,
                )
                or None,
                score=_number(
                    next(
                        (
                            item[name]
                            for name in ("relevancy_score", "swirl_score", "score")
                            if item.get(name) is not None
                        ),
                        None,
                    )
                ),
            )
        )
        if len(results) >= max_results:
            break
    return SwirlSearchResponse(
        query=query,
        search_id=str(search_id) if search_id is not None else None,
        results=results,
    )


class SwirlClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 30,
        max_results: int = 20,
        opener: Callable[..., Any] = urlopen,
    ):
        if not base_url.strip().startswith(("http://", "https://")):
            raise ValueError("SWIRL base URL must use http or https")
        if not username or not password:
            raise ValueError("SWIRL username and password are required")
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.timeout_seconds = timeout_seconds
        self.max_results = max(1, min(max_results, 50))
        self.opener = opener

    @classmethod
    def from_environment(cls) -> SwirlClient | None:
        base_url = os.environ.get("SWIRL_BASE_URL", "").strip()
        username = os.environ.get("SWIRL_USERNAME", "").strip()
        password = os.environ.get("SWIRL_PASSWORD", "")
        if not (base_url or username or password):
            return None
        if not (base_url and username and password):
            raise ValueError(
                "SWIRL_BASE_URL, SWIRL_USERNAME and SWIRL_PASSWORD must be set together"
            )
        return cls(
            base_url,
            username,
            password,
            timeout_seconds=float(os.environ.get("SWIRL_TIMEOUT_SECONDS", "30")),
            max_results=int(os.environ.get("SWIRL_MAX_RESULTS", "20")),
        )

    def search(
        self,
        query: str,
        *,
        providers: list[str] | None = None,
        max_results: int = 10,
    ) -> SwirlSearchResponse:
        query = query.strip()
        if not query or len(query) > 2000:
            raise SwirlSearchError("SWIRL query must contain 1..2000 characters")
        limit = max(1, min(max_results, self.max_results))
        parameters: dict[str, str | int] = {"qs": query, "result_count": limit}
        if providers:
            parameters["providers"] = ",".join(providers[:20])
        credentials = base64.b64encode(f"{self.username}:{self.password}".encode()).decode("ascii")
        request = Request(
            f"{self.base_url}/api/swirl/search/?{urlencode(parameters)}",
            headers={"Accept": "application/json", "Authorization": f"Basic {credentials}"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(5_000_001)
        except HTTPError as exc:
            raise SwirlSearchError(f"SWIRL request failed with HTTP {exc.code}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise SwirlSearchError(f"SWIRL request failed: {exc}") from exc
        if len(raw) > 5_000_000:
            raise SwirlSearchError("SWIRL response exceeds 5000000 bytes")
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SwirlSearchError("SWIRL returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise SwirlSearchError("SWIRL response must be a JSON object")
        return normalize_swirl_response(query, payload, max_results=limit)
