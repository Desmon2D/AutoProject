from __future__ import annotations

import re
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings, normalize_space_key


class DitConfluenceError(RuntimeError):
    """Safe error suitable for returning across the MCP boundary."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int | None = None,
        details: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.details = details

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.status is not None:
            result["status"] = self.status
        if self.details not in (None, "", [], {}):
            result["details"] = self.details
        return result


_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./{}~%=-]+$")
_CONTENT_ID_RE = re.compile(r"^[1-9][0-9]*$")
_PROPERTY_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,255}$")
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_ORDER_BY_RE = re.compile(r"\s+ORDER\s+BY\s+.+$", re.IGNORECASE | re.DOTALL)
_VERSION_RE = re.compile(r'<meta\s+name="ajs-version-number"\s+content="([^"]+)"', re.I)
_BUILD_RE = re.compile(r'<meta\s+name="ajs-build-number"\s+content="([^"]+)"', re.I)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))


def _bounded_text(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]", True


def _safe_value(value: Any, *, text_limit: int, depth: int = 0) -> Any:
    """Bound arbitrary app-defined metadata without assuming its schema."""
    if depth >= 8:
        return "[maximum nesting depth reached]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _bounded_text(value, text_limit)[0]
    if isinstance(value, list):
        result = [_safe_value(item, text_limit=text_limit, depth=depth + 1) for item in value[:200]]
        if len(value) > 200:
            result.append({"_truncated_items": len(value) - 200})
        return result
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= 200:
                result["_truncated_keys"] = len(value) - 200
                break
            if key in {"profilePicture", "thumbnailLink", "downloadLink"}:
                continue
            result[str(key)] = _safe_value(item, text_limit=text_limit, depth=depth + 1)
        return result
    return _bounded_text(value, text_limit)[0]


class _HtmlTextExtractor(HTMLParser):
    _BLOCK_TAGS = frozenset(
        {
            "address",
            "article",
            "aside",
            "blockquote",
            "br",
            "div",
            "dl",
            "dt",
            "dd",
            "figcaption",
            "figure",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "header",
            "hr",
            "li",
            "main",
            "nav",
            "ol",
            "p",
            "pre",
            "section",
            "table",
            "tr",
            "ul",
        }
    )
    _SKIP_TAGS = frozenset({"script", "style", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self._SKIP_TAGS:
            self.skip_depth += 1
        elif not self.skip_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")
        elif not self.skip_depth and tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag: str) -> None:
        if tag in self._SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif not self.skip_depth and tag in self._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)

    def text(self) -> str:
        value = "".join(self.parts).replace("\xa0", " ")
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n[ \t]+", "\n", value)
        value = re.sub(r"[ \t]{2,}", " ", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def _html_to_text(value: Any) -> str:
    parser = _HtmlTextExtractor()
    try:
        parser.feed(str(value or ""))
        parser.close()
        return parser.text()
    except Exception:
        return re.sub(r"<[^>]+>", " ", str(value or "")).strip()


def _user(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in ("type", "username", "userKey", "accountId", "displayName")
        if value.get(key) is not None
    }


def _space_summary(value: Any, base_url: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    key = str(value.get("key") or "")
    description = value.get("description") if isinstance(value.get("description"), dict) else {}
    plain = description.get("plain") if isinstance(description.get("plain"), dict) else {}
    description_text, truncated = _bounded_text(plain.get("value"), 4_000)
    return {
        "id": value.get("id"),
        "key": key,
        "name": value.get("name"),
        "type": value.get("type"),
        "status": value.get("status"),
        "description": description_text,
        "description_truncated": truncated,
        "homepage_id": (
            value.get("homepage", {}).get("id")
            if isinstance(value.get("homepage"), dict)
            else None
        ),
        "url": f"{base_url}/display/{quote(key, safe='~.-_')}" if key else None,
    }


class ConfluenceClient:
    """Policy-enforcing GET-only client for Confluence Server REST API."""

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        headers = {
            "Accept": "application/json",
            "User-Agent": "dit-confluence-mcp/0.1.0",
        }
        auth: httpx.Auth | None = None
        if settings.auth_type == "pat":
            headers["Authorization"] = f"Bearer {settings.token}"
        elif settings.auth_type == "basic":
            auth = httpx.BasicAuth(settings.username, settings.password)
        self._client = httpx.Client(
            headers=headers,
            auth=auth,
            timeout=settings.timeout_seconds,
            verify=settings.ca_bundle or True,
            proxy=settings.proxy,
            trust_env=settings.use_env_proxy,
            follow_redirects=False,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def _url(self, path: str, *, root: bool = False) -> str:
        clean = path.strip("/")
        if not clean or not _SAFE_PATH_RE.fullmatch(clean) or ".." in clean.split("/"):
            raise DitConfluenceError("invalid_request", "Unsafe Confluence path")
        base = self.settings.base_url if root else self.settings.rest_url
        return f"{base}/{clean}"

    def _request_get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        root: bool = False,
        accept_json: bool = True,
    ) -> httpx.Response:
        url = self._url(path, root=root)
        last_error: Exception | None = None
        for attempt in range(self.settings.retries + 1):
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= self.settings.retries:
                    raise DitConfluenceError(
                        "network_error", f"Cannot connect to {self.settings.base_url}"
                    ) from exc
                time.sleep(min(0.5 * (2**attempt), 3.0))
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < self.settings.retries:
                retry_after = response.headers.get("Retry-After", "").strip()
                delay = (
                    float(retry_after)
                    if retry_after.replace(".", "", 1).isdigit()
                    else 0.5 * (2**attempt)
                )
                time.sleep(min(delay, 5.0))
                continue
            if 300 <= response.status_code < 400:
                raise DitConfluenceError(
                    "authentication_required",
                    "Confluence redirected the request to a login page; use a PAT or valid basic credentials",
                    status=response.status_code,
                )
            if response.is_error:
                self._raise_response_error(response)
            if accept_json and "json" not in response.headers.get("content-type", "").casefold():
                raise DitConfluenceError(
                    "invalid_response",
                    f"Confluence returned non-JSON content (HTTP {response.status_code})",
                    status=response.status_code,
                )
            return response
        raise DitConfluenceError("network_error", "Confluence request failed") from last_error

    def _raise_response_error(self, response: httpx.Response) -> None:
        status = response.status_code
        message = "Confluence request failed"
        details: Any | None = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if isinstance(payload.get("message"), str):
                message = payload["message"]
            data = payload.get("data")
            if isinstance(data, dict):
                details = _safe_value(data, text_limit=2_000)
        code = {
            400: "invalid_request",
            401: "authentication_failed",
            403: "permission_denied",
            404: "not_found",
            409: "conflict",
            429: "rate_limited",
        }.get(status, "confluence_error")
        if status == 401:
            message = "Confluence rejected the configured credentials"
        elif status == 403:
            message = "The configured Confluence identity cannot read this resource"
        elif status == 404:
            message = "Confluence resource was not found or is not visible to this identity"
        elif status == 429:
            message = "Confluence rate limit was reached; retry later"
        raise DitConfluenceError(code, message, status=status, details=details)

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise DitConfluenceError("invalid_response", "Confluence returned malformed JSON") from exc

    def _content_id(self, value: str | int) -> str:
        content_id = str(value).strip()
        if not _CONTENT_ID_RE.fullmatch(content_id):
            raise DitConfluenceError("invalid_content_id", f"Invalid Confluence content ID: {value!r}")
        return content_id

    def _require_space(self, value: str) -> str:
        try:
            key = normalize_space_key(value)
        except ValueError as exc:
            raise DitConfluenceError("invalid_space", str(exc)) from exc
        if not self.settings.space_allowed(key):
            raise DitConfluenceError("space_not_allowed", f"Space {key} is outside the MCP allowlist")
        return key

    def _assert_content_allowed(self, payload: dict[str, Any]) -> str:
        space = payload.get("space") if isinstance(payload.get("space"), dict) else {}
        key = str(space.get("key") or "")
        if not key:
            raise DitConfluenceError(
                "space_unknown", "Confluence did not return a space key for this content"
            )
        return self._require_space(key)

    def _ensure_content_allowed(self, content_id: str) -> str:
        if self.settings.allow_all_visible:
            return ""
        payload = self._json(
            self._request_get(f"content/{content_id}", params={"expand": "space"})
        )
        if not isinstance(payload, dict):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid content metadata")
        return self._assert_content_allowed(payload)

    def _scope_cql(self, cql: str) -> str:
        query = cql.strip()
        if len(query) > 8_000:
            raise DitConfluenceError("invalid_cql", "CQL must not exceed 8000 characters")
        if not query:
            query = "type in (page, blogpost) AND status = current ORDER BY lastmodified DESC"
        if self.settings.allow_all_visible:
            return query
        match = _ORDER_BY_RE.search(query)
        ordering = match.group(0).strip() if match else ""
        condition = query[: match.start()].strip() if match else query
        keys = ", ".join(f'"{key}"' for key in sorted(self.settings.allowed_spaces))
        scoped = f"space in ({keys}) AND ({condition})"
        return f"{scoped} {ordering}" if ordering else scoped

    def _content_summary(self, value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        space = value.get("space") if isinstance(value.get("space"), dict) else {}
        history = value.get("history") if isinstance(value.get("history"), dict) else {}
        version = value.get("version") if isinstance(value.get("version"), dict) else {}
        last_updated = (
            history.get("lastUpdated")
            if isinstance(history.get("lastUpdated"), dict)
            else version
        )
        labels = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
        labels = labels.get("labels") if isinstance(labels.get("labels"), dict) else {}
        label_results = labels.get("results") if isinstance(labels.get("results"), list) else []
        content_id = str(value.get("id") or "")
        return {
            "id": value.get("id"),
            "type": value.get("type"),
            "status": value.get("status"),
            "title": value.get("title"),
            "space": {
                key: space.get(key)
                for key in ("id", "key", "name", "type", "status")
                if space.get(key) is not None
            },
            "version": {
                "number": version.get("number"),
                "when": version.get("when"),
                "by": _user(version.get("by")),
                "message": _bounded_text(version.get("message"), 2_000)[0],
            },
            "last_updated": last_updated.get("when") if isinstance(last_updated, dict) else None,
            "labels": [
                item.get("name") for item in label_results if isinstance(item, dict) and item.get("name")
            ],
            "url": f"{self.settings.base_url}/pages/viewpage.action?pageId={content_id}"
            if content_id
            else None,
        }

    def server_info(self) -> dict[str, Any]:
        response = self._request_get("login.action", root=True, accept_json=False)
        version = _VERSION_RE.search(response.text)
        build = _BUILD_RE.search(response.text)
        access_mode = self._json(self._request_get("accessmode"))
        return {
            "base_url": self.settings.base_url,
            "product": "Confluence Server",
            "version": version.group(1) if version else None,
            "build_number": build.group(1) if build else None,
            "access_mode": access_mode,
        }

    def current_user(self) -> dict[str, Any]:
        payload = self._json(self._request_get("user/current"))
        if not isinstance(payload, dict):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid user information")
        return _user(payload) or {}

    def list_spaces(
        self,
        query: str = "",
        *,
        space_type: str = "",
        status: str = "current",
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        params: dict[str, Any] = {
            "start": start,
            "limit": page_size,
            "expand": "description.plain,homepage",
        }
        if space_type:
            if space_type not in {"global", "personal"}:
                raise DitConfluenceError("invalid_space_type", "space_type must be global or personal")
            params["type"] = space_type
        if status:
            if status not in {"current", "archived"}:
                raise DitConfluenceError("invalid_status", "status must be current or archived")
            params["status"] = status
        payload = self._json(self._request_get("space", params=params))
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise DitConfluenceError("invalid_response", "Confluence returned an invalid space list")
        needle = query.strip().casefold()
        spaces = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if not key or not self.settings.space_allowed(key):
                continue
            if needle and needle not in key.casefold() and needle not in str(item.get("name") or "").casefold():
                continue
            spaces.append(_space_summary(item, self.settings.base_url))
        return {
            "spaces": spaces,
            "start_at": payload.get("start", start),
            "max_results": payload.get("limit", page_size),
            "returned": len(spaces),
            "source_size": payload.get("size", len(payload["results"])),
            "next_available": bool(payload.get("_links", {}).get("next"))
            if isinstance(payload.get("_links"), dict)
            else False,
            "note": "When query or a local allowlist is used, returned is after local filtering.",
        }

    def get_space(self, space_key: str) -> dict[str, Any]:
        key = self._require_space(space_key)
        payload = self._json(
            self._request_get(
                f"space/{quote(key, safe='')}",
                params={"expand": "description.plain,homepage,metadata.labels"},
            )
        )
        if not isinstance(payload, dict):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid space metadata")
        result = _space_summary(payload, self.settings.base_url)
        result["metadata"] = _safe_value(payload.get("metadata") or {}, text_limit=4_000)
        return result

    def search_content(
        self,
        cql: str = "",
        *,
        start_at: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        query = self._scope_cql(cql)
        payload = self._json(
            self._request_get(
                "content/search",
                params={
                    "cql": query,
                    "start": start,
                    "limit": page_size,
                    "expand": "space,version,history.lastUpdated,metadata.labels",
                },
            )
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid CQL results")
        results = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            space = item.get("space") if isinstance(item.get("space"), dict) else {}
            if not self.settings.space_allowed(str(space.get("key") or "")):
                continue
            results.append(self._content_summary(item))
        return {
            "cql": query,
            "results": results,
            "start_at": payload.get("start", start),
            "max_results": payload.get("limit", page_size),
            "source_size": payload.get("size", len(payload["results"])),
            "next_available": bool(payload.get("_links", {}).get("next"))
            if isinstance(payload.get("_links"), dict)
            else False,
        }

    def search_text(
        self,
        text: str,
        *,
        space_key: str = "",
        content_type: str = "",
        start_at: int = 0,
        limit: int = 25,
    ) -> dict[str, Any]:
        term = text.strip()
        if not term or len(term) > 500:
            raise DitConfluenceError("invalid_search_text", "text must contain 1 to 500 characters")
        escaped = term.replace("\\", "\\\\").replace('"', '\\"')
        clauses = [f'siteSearch ~ "{escaped}"', "status = current"]
        if space_key:
            key = self._require_space(space_key)
            escaped_key = key.replace("\\", "\\\\").replace('"', '\\"')
            clauses.append(f'space = "{escaped_key}"')
        if content_type:
            if content_type not in {"page", "blogpost", "comment", "attachment"}:
                raise DitConfluenceError(
                    "invalid_content_type",
                    "content_type must be page, blogpost, comment, or attachment",
                )
            clauses.append(f"type = {content_type}")
        cql = " AND ".join(clauses) + " ORDER BY lastmodified DESC"
        return self.search_content(cql, start_at=start_at, limit=limit)

    def get_content(
        self,
        content_id: str | int,
        *,
        body_format: str = "view",
        include_ancestors: bool = True,
        include_labels: bool = True,
        include_markup: bool = False,
    ) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        if body_format not in {"view", "storage", "export_view"}:
            raise DitConfluenceError(
                "invalid_body_format", "body_format must be view, storage, or export_view"
            )
        expands = [f"body.{body_format}", "space", "version", "history"]
        if include_ancestors:
            expands.append("ancestors")
        if include_labels:
            expands.append("metadata.labels")
        payload = self._json(
            self._request_get(f"content/{item_id}", params={"expand": ",".join(expands)})
        )
        if not isinstance(payload, dict):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid content data")
        self._assert_content_allowed(payload)
        result = self._content_summary(payload)
        body = payload.get("body") if isinstance(payload.get("body"), dict) else {}
        selected = body.get(body_format) if isinstance(body.get(body_format), dict) else {}
        markup = str(selected.get("value") or "")
        text, truncated = _bounded_text(_html_to_text(markup), self.settings.max_text_chars)
        result["body"] = {
            "representation": selected.get("representation", body_format),
            "text": text,
            "text_truncated": truncated,
        }
        if include_markup:
            raw, raw_truncated = _bounded_text(markup, self.settings.max_text_chars)
            result["body"]["markup"] = raw
            result["body"]["markup_truncated"] = raw_truncated
        if include_ancestors:
            ancestors = payload.get("ancestors") if isinstance(payload.get("ancestors"), list) else []
            result["ancestors"] = [self._content_summary(item) for item in ancestors]
        result["extensions"] = _safe_value(payload.get("extensions") or {}, text_limit=2_000)
        return result

    def list_children(
        self,
        content_id: str | int,
        *,
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        self._ensure_content_allowed(item_id)
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        payload = self._json(
            self._request_get(
                f"content/{item_id}/child/page",
                params={
                    "start": start,
                    "limit": page_size,
                    "expand": "space,version,history.lastUpdated,metadata.labels",
                },
            )
        )
        return self._content_page(payload, parent_id=item_id, start=start, limit=page_size)

    def list_comments(
        self,
        content_id: str | int,
        *,
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        self._ensure_content_allowed(item_id)
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        payload = self._json(
            self._request_get(
                f"content/{item_id}/child/comment",
                params={
                    "start": start,
                    "limit": page_size,
                    "expand": "body.view,version,history,space",
                },
            )
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid comments")
        comments = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            body = item.get("body") if isinstance(item.get("body"), dict) else {}
            view = body.get("view") if isinstance(body.get("view"), dict) else {}
            text, truncated = _bounded_text(
                _html_to_text(view.get("value")), self.settings.max_text_chars
            )
            summary = self._content_summary(item)
            summary["text"] = text
            summary["text_truncated"] = truncated
            history = item.get("history") if isinstance(item.get("history"), dict) else {}
            summary["created_by"] = _user(history.get("createdBy"))
            summary["created_date"] = history.get("createdDate")
            comments.append(summary)
        return self._page_result(payload, "comments", comments, start, page_size, content_id=item_id)

    def list_attachments(
        self,
        content_id: str | int,
        *,
        filename: str = "",
        media_type: str = "",
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        self._ensure_content_allowed(item_id)
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        params: dict[str, Any] = {
            "start": start,
            "limit": page_size,
            "expand": "version,history,metadata,space",
        }
        if filename.strip():
            params["filename"] = filename.strip()[:255]
        if media_type.strip():
            params["mediaType"] = media_type.strip()[:255]
        payload = self._json(
            self._request_get(f"content/{item_id}/child/attachment", params=params)
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid attachments")
        attachments = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            extensions = item.get("extensions") if isinstance(item.get("extensions"), dict) else {}
            links = item.get("_links") if isinstance(item.get("_links"), dict) else {}
            attachments.append(
                {
                    **self._content_summary(item),
                    "filename": item.get("title"),
                    "media_type": extensions.get("mediaType"),
                    "file_size": extensions.get("fileSize"),
                    "comment": _bounded_text(extensions.get("comment"), 2_000)[0],
                    "download_url": (
                        f"{self.settings.base_url}{links['download']}"
                        if isinstance(links.get("download"), str)
                        and links["download"].startswith("/")
                        else None
                    ),
                }
            )
        return self._page_result(
            payload, "attachments", attachments, start, page_size, content_id=item_id
        )

    def list_versions(
        self,
        content_id: str | int,
        *,
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        self._ensure_content_allowed(item_id)
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        payload = self._json(
            self._request_get(
                f"content/{item_id}/version",
                params={"start": start, "limit": page_size, "expand": "content"},
            )
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid versions")
        versions = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            versions.append(
                {
                    "number": item.get("number"),
                    "when": item.get("when"),
                    "by": _user(item.get("by")),
                    "message": _bounded_text(item.get("message"), 2_000)[0],
                    "minor_edit": bool(item.get("minorEdit")),
                }
            )
        return self._page_result(payload, "versions", versions, start, page_size, content_id=item_id)

    def list_labels(
        self,
        content_id: str | int,
        *,
        prefix: str = "",
        start_at: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        self._ensure_content_allowed(item_id)
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        params: dict[str, Any] = {"start": start, "limit": page_size}
        if prefix:
            if prefix not in {"global", "my", "team"}:
                raise DitConfluenceError("invalid_label_prefix", "prefix must be global, my, or team")
            params["prefix"] = prefix
        payload = self._json(self._request_get(f"content/{item_id}/label", params=params))
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid labels")
        labels = [
            {
                key: item.get(key)
                for key in ("id", "prefix", "name")
                if item.get(key) is not None
            }
            for item in payload["results"]
            if isinstance(item, dict)
        ]
        return self._page_result(payload, "labels", labels, start, page_size, content_id=item_id)

    def list_properties(
        self,
        content_id: str | int,
        *,
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        self._ensure_content_allowed(item_id)
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        payload = self._json(
            self._request_get(
                f"content/{item_id}/property",
                params={"start": start, "limit": page_size, "expand": "version"},
            )
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid properties")
        properties = []
        for item in payload["results"]:
            if not isinstance(item, dict):
                continue
            properties.append(
                {
                    "id": item.get("id"),
                    "key": item.get("key"),
                    "value": _safe_value(
                        item.get("value"), text_limit=self.settings.max_text_chars
                    ),
                    "version": _safe_value(item.get("version") or {}, text_limit=2_000),
                }
            )
        return self._page_result(
            payload, "properties", properties, start, page_size, content_id=item_id
        )

    def get_property(self, content_id: str | int, property_key: str) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        self._ensure_content_allowed(item_id)
        key = property_key.strip()
        if not _PROPERTY_KEY_RE.fullmatch(key):
            raise DitConfluenceError(
                "invalid_property_key",
                "property_key must contain only letters, digits, dot, underscore, colon, or hyphen",
            )
        payload = self._json(
            self._request_get(
                f"content/{item_id}/property/{quote(key, safe='')}",
                params={"expand": "version"},
            )
        )
        if not isinstance(payload, dict):
            raise DitConfluenceError("invalid_response", "Confluence returned an invalid property")
        return {
            "content_id": item_id,
            "id": payload.get("id"),
            "key": payload.get("key"),
            "value": _safe_value(payload.get("value"), text_limit=self.settings.max_text_chars),
            "version": _safe_value(payload.get("version") or {}, text_limit=2_000),
        }

    def get_restrictions(self, content_id: str | int) -> dict[str, Any]:
        item_id = self._content_id(content_id)
        self._ensure_content_allowed(item_id)
        payload = self._json(
            self._request_get(
                f"content/{item_id}/restriction/byOperation",
                params={"expand": "restrictions.user,restrictions.group"},
            )
        )
        return {
            "content_id": item_id,
            "restrictions": _safe_value(payload, text_limit=self.settings.max_text_chars),
        }

    def _content_page(
        self,
        payload: Any,
        *,
        parent_id: str,
        start: int,
        limit: int,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise DitConfluenceError("invalid_response", "Confluence returned invalid child content")
        items = [self._content_summary(item) for item in payload["results"]]
        return self._page_result(payload, "children", items, start, limit, content_id=parent_id)

    @staticmethod
    def _page_result(
        payload: dict[str, Any],
        key: str,
        values: list[Any],
        start: int,
        limit: int,
        *,
        content_id: str,
    ) -> dict[str, Any]:
        links = payload.get("_links") if isinstance(payload.get("_links"), dict) else {}
        return {
            "content_id": content_id,
            key: values,
            "start_at": payload.get("start", start),
            "max_results": payload.get("limit", limit),
            "source_size": payload.get("size", len(values)),
            "next_available": bool(links.get("next")),
        }

    def probe(self) -> dict[str, Any]:
        server = self.server_info()
        user = self.current_user()
        try:
            spaces = self.list_spaces(limit=10)
            spaces_probe: dict[str, Any] = {
                "available": True,
                "visible_count": len(spaces["spaces"]),
            }
        except DitConfluenceError as exc:
            spaces_probe = {
                "available": False,
                "reason": exc.code,
                "status": exc.status,
            }
        try:
            content = self.search_content(limit=1)
            content_probe: dict[str, Any] = {
                "available": True,
                "visible_count": len(content["results"]),
            }
        except DitConfluenceError as exc:
            content_probe = {
                "available": False,
                "reason": exc.code,
                "status": exc.status,
            }
        return {
            "server": server,
            "authenticated_user": None if user.get("type") == "anonymous" else user,
            "spaces_api": spaces_probe,
            "content_search_api": content_probe,
            "policy": {
                "allow_all_visible": self.settings.allow_all_visible,
                "allowed_spaces": sorted(self.settings.allowed_spaces),
            },
        }
