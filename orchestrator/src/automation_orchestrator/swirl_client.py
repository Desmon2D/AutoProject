from __future__ import annotations

import base64
import html
import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen

from .models import SwirlSearchResponse, SwirlSearchResult


class SwirlSearchError(RuntimeError):
    pass


@dataclass(frozen=True)
class SwirlContentRoute:
    provider_id: int
    source: str
    url_template: str
    content_path: str | None
    content_format: str


def _text(value: Any, *, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return " ".join(value.split())[:limit]


def _plain_text(value: Any, *, limit: int) -> str:
    text = _text(value, limit=limit * 2)
    text = re.sub(r"\bstrong\b\s*(?=<em\b)", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"(</em>)\s*\bstrong\b", r"\1 ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]{1,200}>", " ", text)
    text = " ".join(html.unescape(text).split())
    return re.sub(r"\s+([.,;:!?])", r"\1", text)[:limit]


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
        title = _plain_text(item.get("title") or item.get("name") or url, limit=1000)
        if not url or not title:
            continue
        key = (url, title)
        if key in seen:
            continue
        seen.add(key)
        item_payload = item.get("payload")
        if not isinstance(item_payload, dict):
            item_payload = {}
        results.append(
            SwirlSearchResult(
                title=title,
                snippet=_plain_text(
                    item.get("snippet")
                    or item.get("body")
                    or item.get("description")
                    or item.get("content"),
                    limit=2000,
                ),
                url=url,
                source=_plain_text(
                    item.get("source")
                    or item.get("searchprovider")
                    or item.get("provider")
                    or item.get("_parent_source")
                    or "unknown",
                    limit=300,
                ),
                document_id=_text(
                    item.get("document_id")
                    or item_payload.get("document_id")
                    or item_payload.get("id"),
                    limit=500,
                )
                or None,
                updated_at=_plain_text(
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
        allowed_content_origins: list[str] | None = None,
        content_routes: list[SwirlContentRoute] | None = None,
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
        self.allowed_content_origins = frozenset(
            self._origin(value) for value in (allowed_content_origins or [])
        )
        self.content_routes = {
            route.source.casefold(): route for route in (content_routes or [])
        }
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
        routes_value = os.environ.get("SWIRL_CONTENT_ROUTES_JSON", "").strip()
        try:
            routes_payload = json.loads(routes_value) if routes_value else []
        except json.JSONDecodeError as exc:
            raise ValueError("SWIRL_CONTENT_ROUTES_JSON must be valid JSON") from exc
        if not isinstance(routes_payload, list):
            raise TypeError("SWIRL_CONTENT_ROUTES_JSON must be a JSON array")
        routes: list[SwirlContentRoute] = []
        for item in routes_payload:
            if not isinstance(item, dict):
                raise TypeError("SWIRL content route must be a JSON object")
            provider_id = item.get("provider_id")
            source = item.get("source")
            url_template = item.get("url_template")
            content_path = item.get("content_path")
            content_format = item.get("format", "text")
            if (
                not isinstance(provider_id, int)
                or provider_id < 1
                or not isinstance(source, str)
                or not source.strip()
                or not isinstance(url_template, str)
                or "{id}" not in url_template
                or (content_path is not None and not isinstance(content_path, str))
                or not isinstance(content_format, str)
            ):
                raise ValueError("SWIRL content route is invalid")
            routes.append(
                SwirlContentRoute(
                    provider_id=provider_id,
                    source=source.strip(),
                    url_template=url_template,
                    content_path=content_path,
                    content_format=_text(content_format, limit=50),
                )
            )
        return cls(
            base_url,
            username,
            password,
            timeout_seconds=float(os.environ.get("SWIRL_TIMEOUT_SECONDS", "30")),
            max_results=int(os.environ.get("SWIRL_MAX_RESULTS", "20")),
            allowed_content_origins=[
                value.strip()
                for value in os.environ.get("SWIRL_CONTENT_ALLOWED_ORIGINS", "").split(",")
                if value.strip()
            ],
            content_routes=routes,
        )

    @staticmethod
    def _origin(value: str) -> str:
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("SWIRL content origin must use http or https")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("SWIRL content origin must not include credentials or a query")
        port = f":{parsed.port}" if parsed.port is not None else ""
        return f"{parsed.scheme.lower()}://{parsed.hostname.lower()}{port}"

    def _request_bytes(
        self,
        path: str,
        *,
        parameters: dict[str, str | int] | None = None,
        max_bytes: int,
    ) -> bytes:
        query = f"?{urlencode(parameters)}" if parameters else ""
        credentials = base64.b64encode(
            f"{self.username}:{self.password}".encode()
        ).decode("ascii")
        request = Request(
            f"{self.base_url}{path}{query}",
            headers={"Accept": "application/json", "Authorization": f"Basic {credentials}"},
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(max_bytes + 1)
        except HTTPError as exc:
            raise SwirlSearchError(f"SWIRL request failed with HTTP {exc.code}") from exc
        except (TimeoutError, URLError, OSError) as exc:
            raise SwirlSearchError(f"SWIRL request failed: {exc}") from exc
        if len(raw) > max_bytes:
            raise SwirlSearchError(f"SWIRL response exceeds {max_bytes} bytes")
        return raw

    def _request_json(
        self,
        path: str,
        *,
        parameters: dict[str, str | int] | None = None,
        max_bytes: int = 5_000_000,
    ) -> Any:
        raw = self._request_bytes(path, parameters=parameters, max_bytes=max_bytes)
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SwirlSearchError("SWIRL returned invalid JSON") from exc

    @staticmethod
    def _content_at_path(payload: Any, path: str | None) -> Any:
        value = payload
        if not path:
            return value
        for part in path.split("."):
            if not isinstance(value, dict) or part not in value:
                raise SwirlSearchError(f"SWIRL fetched document is missing field: {path}")
            value = value[part]
        return value

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
        payload = self._request_json("/api/swirl/search/", parameters=parameters)
        if not isinstance(payload, dict):
            raise SwirlSearchError("SWIRL response must be a JSON object")
        return normalize_swirl_response(query, payload, max_results=limit)

    def fetch_document(
        self,
        result: SwirlSearchResult,
        *,
        max_characters: int = 12_000,
    ) -> SwirlSearchResult:
        if not result.document_id:
            raise SwirlSearchError("SWIRL result has no document identifier")
        route = self.content_routes.get(result.source.casefold())
        if route is None:
            raise SwirlSearchError(
                f"SWIRL source does not define a full-content route: {result.source}"
            )
        upstream_url = route.url_template.replace(
            "{id}", quote(result.document_id, safe="")
        )
        if "{" in upstream_url or "}" in upstream_url:
            raise SwirlSearchError("SWIRL content URL template contains unresolved fields")
        try:
            origin = self._origin(upstream_url)
        except ValueError as exc:
            raise SwirlSearchError("SWIRL content route has an invalid URL") from exc
        if origin not in self.allowed_content_origins:
            raise SwirlSearchError(
                f"SWIRL content origin is not allowed: {origin}"
            )
        payload = self._request_json(
            "/api/swirl/fetch-document/",
            parameters={"url": upstream_url, "provider_id": route.provider_id},
            max_bytes=2_000_000,
        )
        content = self._content_at_path(payload, route.content_path)
        if not isinstance(content, str) or not content.strip():
            raise SwirlSearchError("SWIRL fetched document has no textual content")
        limit = max(1000, min(max_characters, 50_000))
        return result.model_copy(
            update={
                "content": content[:limit],
                "content_fetched": True,
                "content_format": route.content_format,
                "content_truncated": len(content) > limit,
            }
        )
