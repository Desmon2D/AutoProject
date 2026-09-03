from __future__ import annotations

import re
import time
from collections import Counter
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from .config import Settings, normalize_project_key


class DitJiraError(RuntimeError):
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


_ISSUE_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*)-([1-9][0-9]*)$")
_SAFE_PATH_RE = re.compile(r"^[A-Za-z0-9_./{}-]+$")
_RETRYABLE_STATUS = frozenset({429, 502, 503, 504})
_DEFAULT_FIELDS = (
    "summary",
    "project",
    "status",
    "issuetype",
    "priority",
    "assignee",
    "reporter",
    "created",
    "updated",
    "resolution",
    "labels",
    "components",
    "fixVersions",
    "versions",
    "duedate",
    "description",
    "parent",
    "subtasks",
    "issuelinks",
)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))


def _bounded_text(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]", True


def _safe_value(value: Any, *, text_limit: int, depth: int = 0) -> Any:
    """Bound arbitrary custom-field values without assuming their schema."""
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
            if key in {"avatarUrls"}:
                continue
            result[str(key)] = _safe_value(item, text_limit=text_limit, depth=depth + 1)
        return result
    return _bounded_text(value, text_limit)[0]


def _person(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in ("key", "name", "displayName", "emailAddress", "active", "timeZone")
        if value.get(key) is not None
    }


def _status(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    category = value.get("statusCategory") if isinstance(value.get("statusCategory"), dict) else {}
    description, description_truncated = _bounded_text(value.get("description"), 4_000)
    result: dict[str, Any] = {
        "id": value.get("id"),
        "name": value.get("name"),
        "description": description,
        "description_truncated": description_truncated,
    }
    if category:
        result["category"] = {
            key: category.get(key)
            for key in ("id", "key", "name", "colorName")
            if category.get(key) is not None
        }
    return result


class JiraClient:
    """Policy-enforcing client for Jira Server REST API v2.

    The transport deliberately exposes GET only. Some Jira reads also have POST
    variants, but they are not used so the read-only guarantee remains obvious
    at the HTTP boundary.
    """

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        headers = {
            "Accept": "application/json",
            "User-Agent": "dit-jira-mcp/0.1.0",
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
        self._field_cache: tuple[float, list[dict[str, Any]]] | None = None

    def close(self) -> None:
        self._client.close()

    def _url(self, api: str, path: str) -> str:
        clean = path.strip("/")
        if not clean or not _SAFE_PATH_RE.fullmatch(clean) or ".." in clean.split("/"):
            raise DitJiraError("invalid_request", "Unsafe Jira REST path")
        base = self.settings.agile_url if api == "agile" else self.settings.rest_url
        return f"{base}/{clean}"

    def _request_get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        api: str = "rest",
    ) -> httpx.Response:
        url = self._url(api, path)
        last_error: Exception | None = None
        for attempt in range(self.settings.retries + 1):
            try:
                response = self._client.get(url, params=params)
            except httpx.RequestError as exc:
                last_error = exc
                if attempt >= self.settings.retries:
                    raise DitJiraError("network_error", f"Cannot connect to {self.settings.base_url}") from exc
                time.sleep(min(0.5 * (2**attempt), 3.0))
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < self.settings.retries:
                retry_after = response.headers.get("Retry-After", "").strip()
                delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0.5 * (2**attempt)
                time.sleep(min(delay, 5.0))
                continue
            if 300 <= response.status_code < 400:
                raise DitJiraError(
                    "authentication_required",
                    "Jira redirected the request to a login page; use a PAT or valid basic credentials",
                    status=response.status_code,
                )
            if response.is_error:
                self._raise_response_error(response)
            return response

        raise DitJiraError("network_error", "Jira request failed") from last_error

    def _raise_response_error(self, response: httpx.Response) -> None:
        status = response.status_code
        message = "Jira request failed"
        details: Any | None = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            messages = payload.get("errorMessages")
            errors = payload.get("errors")
            if isinstance(messages, list) and messages:
                message = "; ".join(str(item) for item in messages[:5])
            elif isinstance(payload.get("message"), str):
                message = payload["message"]
            if isinstance(errors, dict):
                details = {str(key): str(value) for key, value in list(errors.items())[:20]}

        code = {
            400: "invalid_request",
            401: "authentication_failed",
            403: "permission_denied",
            404: "not_found",
            429: "rate_limited",
        }.get(status, "upstream_error")
        if status == 401:
            message = "Jira rejected the configured credentials"
        elif status == 403:
            message = "The configured Jira identity cannot access this resource"
        elif status == 404:
            message = "Jira resource was not found or is not visible to the configured identity"
        elif status == 429:
            message = "Jira rate limit was reached; retry later"
        raise DitJiraError(code, message, status=status, details=details)

    def _json(self, response: httpx.Response) -> Any:
        content_type = response.headers.get("content-type", "").casefold()
        if "json" not in content_type:
            raise DitJiraError(
                "invalid_response",
                f"Jira returned non-JSON content (HTTP {response.status_code})",
                status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise DitJiraError("invalid_response", "Jira returned malformed JSON") from exc

    def _project_key_from_issue(self, issue_key: str) -> str:
        match = _ISSUE_KEY_RE.fullmatch(issue_key.strip())
        if not match:
            raise DitJiraError("invalid_issue_key", f"Invalid Jira issue key: {issue_key!r}")
        project = match.group(1).upper()
        self._require_project(project)
        return project

    def _require_project(self, project_key: str) -> str:
        try:
            project = normalize_project_key(project_key)
        except ValueError as exc:
            raise DitJiraError("invalid_project", str(exc)) from exc
        if not self.settings.project_allowed(project):
            raise DitJiraError("project_not_allowed", f"Project {project} is outside the MCP allowlist")
        return project

    def _scope_jql(self, jql: str) -> str:
        query = jql.strip()
        if len(query) > 8_000:
            raise DitJiraError("invalid_jql", "JQL must not exceed 8000 characters")
        if self.settings.allow_all_visible:
            return query or "ORDER BY updated DESC"
        project_clause = ", ".join(sorted(self.settings.allowed_projects))
        scope = f"project in ({project_clause})"
        return f"{scope} AND ({query})" if query else f"{scope} ORDER BY updated DESC"

    def _field_catalog(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not refresh and self._field_cache and now - self._field_cache[0] < 300:
            return self._field_cache[1]
        payload = self._json(self._request_get("field"))
        if not isinstance(payload, list):
            raise DitJiraError("invalid_response", "Jira returned an invalid field catalog")
        fields = [item for item in payload if isinstance(item, dict) and item.get("id")]
        self._field_cache = (now, fields)
        return fields

    def _resolve_fields(self, requested: Iterable[str] | None) -> list[str]:
        catalog = self._field_catalog()
        by_id = {str(item.get("id")): item for item in catalog}
        if requested is None:
            return [field_id for field_id in _DEFAULT_FIELDS if field_id in by_id]

        values = [str(value).strip() for value in requested if str(value).strip()]
        if not values:
            raise DitJiraError("invalid_fields", "fields must contain at least one field ID or name")
        if any(value in {"*all", "*navigable"} for value in values):
            if len(values) != 1:
                raise DitJiraError("invalid_fields", "*all or *navigable must be requested alone")
            return values

        resolved: list[str] = []
        for value in values:
            if value in by_id:
                resolved.append(value)
                continue
            folded = value.casefold()
            matches = [
                item
                for item in catalog
                if str(item.get("name") or "").casefold() == folded
                or folded in {str(clause).casefold() for clause in item.get("clauseNames") or []}
            ]
            unique_ids = sorted({str(item["id"]) for item in matches})
            if not unique_ids:
                raise DitJiraError(
                    "unknown_field",
                    f"Unknown Jira field {value!r}; call jira_list_fields first",
                )
            if len(unique_ids) > 1:
                raise DitJiraError(
                    "ambiguous_field",
                    f"Jira field name {value!r} is not unique; use one of its field IDs",
                    details={"field_ids": unique_ids},
                )
            resolved.append(unique_ids[0])
        return list(dict.fromkeys(resolved))

    def _field_names_and_schema(self) -> tuple[dict[str, str], dict[str, Any]]:
        names: dict[str, str] = {}
        schema: dict[str, Any] = {}
        for item in self._field_catalog():
            field_id = str(item.get("id"))
            names[field_id] = str(item.get("name") or field_id)
            if isinstance(item.get("schema"), dict):
                schema[field_id] = item["schema"]
        return names, schema

    def _issue_summary(
        self,
        issue: dict[str, Any],
        *,
        names: dict[str, Any] | None = None,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fields = issue.get("fields") if isinstance(issue.get("fields"), dict) else {}
        catalog_names, catalog_schema = self._field_names_and_schema()
        field_names = {**catalog_names, **(names or {})}
        field_schema = {**catalog_schema, **(schema or {})}
        normalized_fields: list[dict[str, Any]] = []
        for field_id, value in fields.items():
            if value is None:
                continue
            entry: dict[str, Any] = {
                "id": field_id,
                "name": field_names.get(field_id, field_id),
                "value": _safe_value(value, text_limit=self.settings.max_text_chars),
            }
            if isinstance(field_schema.get(field_id), dict):
                entry["schema"] = field_schema[field_id]
            normalized_fields.append(entry)

        project_value = fields.get("project") if isinstance(fields.get("project"), dict) else {}
        issue_type = fields.get("issuetype") if isinstance(fields.get("issuetype"), dict) else {}
        result: dict[str, Any] = {
            "id": issue.get("id"),
            "key": issue.get("key"),
            "url": f"{self.settings.base_url}/browse/{issue.get('key')}",
            "project": {
                key: project_value.get(key)
                for key in ("id", "key", "name")
                if project_value.get(key) is not None
            },
            "summary": fields.get("summary"),
            "status": _status(fields.get("status")),
            "issue_type": {
                key: issue_type.get(key)
                for key in ("id", "name", "subtask", "description")
                if issue_type.get(key) is not None
            },
            "fields": normalized_fields,
        }
        return result

    def server_info(self) -> dict[str, Any]:
        payload = self._json(self._request_get("serverInfo"))
        if not isinstance(payload, dict):
            raise DitJiraError("invalid_response", "Jira returned invalid server information")
        return {
            key: payload.get(key)
            for key in (
                "baseUrl",
                "version",
                "versionNumbers",
                "deploymentType",
                "buildNumber",
                "buildDate",
                "serverTitle",
            )
            if payload.get(key) is not None
        }

    def current_user(self) -> dict[str, Any]:
        payload = self._json(self._request_get("myself"))
        if not isinstance(payload, dict):
            raise DitJiraError("invalid_response", "Jira returned invalid user information")
        return _person(payload) or {}

    def list_projects(
        self,
        query: str = "",
        *,
        include_archived: bool = False,
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        payload = self._json(self._request_get("project", params={"expand": "description,lead"}))
        if not isinstance(payload, list):
            raise DitJiraError("invalid_response", "Jira returned an invalid project list")
        needle = query.strip().casefold()
        projects = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").upper()
            if not key or not self.settings.project_allowed(key):
                continue
            if not include_archived and item.get("archived") is True:
                continue
            if needle and needle not in key.casefold() and needle not in str(item.get("name") or "").casefold():
                continue
            description, truncated = _bounded_text(item.get("description"), 4_000)
            projects.append(
                {
                    "id": item.get("id"),
                    "key": key,
                    "name": item.get("name"),
                    "description": description,
                    "description_truncated": truncated,
                    "project_type": item.get("projectTypeKey"),
                    "archived": bool(item.get("archived")),
                    "lead": _person(item.get("lead")),
                    "url": f"{self.settings.base_url}/browse/{key}",
                }
            )
        projects.sort(key=lambda item: (str(item.get("name") or "").casefold(), item["key"]))
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        page = projects[start : start + page_size]
        return {
            "projects": page,
            "start_at": start,
            "max_results": page_size,
            "total": len(projects),
            "is_last": start + len(page) >= len(projects),
        }

    def list_fields(
        self,
        query: str = "",
        *,
        custom_only: bool = False,
        searchable_only: bool = False,
        refresh: bool = False,
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        fields = self._field_catalog(refresh=refresh)
        name_counts = Counter(str(item.get("name") or "").casefold() for item in fields)
        needle = query.strip().casefold()
        result = []
        for item in fields:
            if custom_only and not item.get("custom"):
                continue
            if searchable_only and not item.get("searchable"):
                continue
            haystack = " ".join(
                [str(item.get("id") or ""), str(item.get("name") or ""), *(str(x) for x in item.get("clauseNames") or [])]
            ).casefold()
            if needle and needle not in haystack:
                continue
            name = str(item.get("name") or item.get("id"))
            result.append(
                {
                    "id": item.get("id"),
                    "name": name,
                    "custom": bool(item.get("custom")),
                    "searchable": bool(item.get("searchable")),
                    "navigable": bool(item.get("navigable")),
                    "orderable": bool(item.get("orderable")),
                    "clause_names": item.get("clauseNames") or [],
                    "schema": item.get("schema") or {},
                    "ambiguous_name": name_counts[name.casefold()] > 1,
                }
            )
        result.sort(key=lambda item: (str(item["name"]).casefold(), str(item["id"])))
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        page = result[start : start + page_size]
        return {
            "fields": page,
            "start_at": start,
            "max_results": page_size,
            "total": len(result),
            "is_last": start + len(page) >= len(result),
            "note": "Field display names are not guaranteed to be unique; use id in later calls.",
        }

    def project_schema(
        self,
        project_key: str,
        *,
        include_components: bool = True,
        include_versions: bool = True,
    ) -> dict[str, Any]:
        project = self._require_project(project_key)
        encoded = quote(project, safe="")
        detail = self._json(self._request_get(f"project/{encoded}"))
        statuses = self._json(self._request_get(f"project/{encoded}/statuses"))
        if not isinstance(detail, dict) or not isinstance(statuses, list):
            raise DitJiraError("invalid_response", "Jira returned invalid project metadata")
        issue_types = []
        for issue_type in statuses:
            if not isinstance(issue_type, dict):
                continue
            issue_types.append(
                {
                    "id": issue_type.get("id"),
                    "name": issue_type.get("name"),
                    "subtask": bool(issue_type.get("subtask")),
                    "statuses": [
                        _status(status)
                        for status in issue_type.get("statuses") or []
                        if isinstance(status, dict)
                    ],
                }
            )
        result: dict[str, Any] = {
            "project": {
                "id": detail.get("id"),
                "key": detail.get("key"),
                "name": detail.get("name"),
                "project_type": detail.get("projectTypeKey"),
                "archived": bool(detail.get("archived")),
                "lead": _person(detail.get("lead")),
            },
            "issue_types": issue_types,
            "note": "Statuses are returned per issue type and must not be inferred from category names.",
        }
        if include_components:
            components = self._json(self._request_get(f"project/{encoded}/components"))
            result["components"] = _safe_value(components, text_limit=4_000)
        if include_versions:
            versions = self._json(self._request_get(f"project/{encoded}/versions"))
            result["versions"] = _safe_value(versions, text_limit=4_000)
        return result

    def search_issues(
        self,
        jql: str = "",
        *,
        fields: list[str] | None = None,
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        resolved_fields = self._resolve_fields(fields)
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        query = self._scope_jql(jql)
        payload = self._json(
            self._request_get(
                "search",
                params={
                    "jql": query,
                    "startAt": start,
                    "maxResults": page_size,
                    "fields": ",".join(resolved_fields),
                    "expand": "names,schema",
                    "validateQuery": "true",
                },
            )
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("issues"), list):
            raise DitJiraError("invalid_response", "Jira returned invalid search results")
        names = payload.get("names") if isinstance(payload.get("names"), dict) else {}
        schema = payload.get("schema") if isinstance(payload.get("schema"), dict) else {}
        issues = [
            self._issue_summary(item, names=names, schema=schema)
            for item in payload["issues"]
            if isinstance(item, dict)
        ]
        return {
            "jql": query,
            "issues": issues,
            "start_at": payload.get("startAt", start),
            "max_results": payload.get("maxResults", page_size),
            "total": payload.get("total", len(issues)),
            "warning_messages": payload.get("warningMessages") or [],
        }

    def get_issue(
        self,
        issue_key: str,
        *,
        fields: list[str] | None = None,
        include_changelog: bool = False,
        changelog_limit: int = 20,
    ) -> dict[str, Any]:
        self._project_key_from_issue(issue_key)
        key = issue_key.strip().upper()
        resolved_fields = self._resolve_fields(fields)
        expand = ["names", "schema"]
        if include_changelog:
            expand.append("changelog")
        payload = self._json(
            self._request_get(
                f"issue/{quote(key, safe='')}",
                params={"fields": ",".join(resolved_fields), "expand": ",".join(expand)},
            )
        )
        if not isinstance(payload, dict):
            raise DitJiraError("invalid_response", "Jira returned invalid issue data")
        result = self._issue_summary(
            payload,
            names=payload.get("names") if isinstance(payload.get("names"), dict) else {},
            schema=payload.get("schema") if isinstance(payload.get("schema"), dict) else {},
        )
        if include_changelog and isinstance(payload.get("changelog"), dict):
            changelog = payload["changelog"]
            histories = changelog.get("histories") if isinstance(changelog.get("histories"), list) else []
            result["changelog"] = {
                "histories": [self._history(item) for item in histories[: _clamp(changelog_limit, 1, 100)]],
                "total": changelog.get("total", len(histories)),
                "truncated": len(histories) > _clamp(changelog_limit, 1, 100),
            }
        return result

    def _history(self, item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        changes = []
        for change in item.get("items") or []:
            if not isinstance(change, dict):
                continue
            changes.append(
                {
                    key: change.get(key)
                    for key in ("field", "fieldtype", "fieldId", "from", "fromString", "to", "toString")
                    if change.get(key) is not None
                }
            )
        return {
            "id": item.get("id"),
            "author": _person(item.get("author")),
            "created": item.get("created"),
            "changes": changes,
        }

    def issue_changelog(self, issue_key: str, *, start_at: int = 0, limit: int = 50) -> dict[str, Any]:
        self._project_key_from_issue(issue_key)
        key = issue_key.strip().upper()
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        try:
            payload = self._json(
                self._request_get(
                    f"issue/{quote(key, safe='')}/changelog",
                    params={"startAt": start, "maxResults": page_size},
                )
            )
        except DitJiraError as exc:
            if exc.status != 404:
                raise
            issue = self._json(
                self._request_get(
                    f"issue/{quote(key, safe='')}",
                    params={"fields": "summary", "expand": "changelog"},
                )
            )
            payload = issue.get("changelog") if isinstance(issue, dict) else None
        if not isinstance(payload, dict):
            raise DitJiraError("invalid_response", "Jira returned invalid changelog data")
        histories = payload.get("values") if isinstance(payload.get("values"), list) else payload.get("histories")
        histories = histories if isinstance(histories, list) else []
        page = histories if "values" in payload else histories[start : start + page_size]
        return {
            "issue_key": key,
            "histories": [self._history(item) for item in page[:page_size]],
            "start_at": payload.get("startAt", start),
            "max_results": payload.get("maxResults", page_size),
            "total": payload.get("total", len(histories)),
        }

    def issue_comments(self, issue_key: str, *, start_at: int = 0, limit: int = 50) -> dict[str, Any]:
        self._project_key_from_issue(issue_key)
        key = issue_key.strip().upper()
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        payload = self._json(
            self._request_get(
                f"issue/{quote(key, safe='')}/comment",
                params={"startAt": start, "maxResults": page_size, "orderBy": "-created"},
            )
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("comments"), list):
            raise DitJiraError("invalid_response", "Jira returned invalid comment data")
        comments = []
        for item in payload["comments"]:
            if not isinstance(item, dict):
                continue
            body, truncated = _bounded_text(item.get("body"), self.settings.max_text_chars)
            comments.append(
                {
                    "id": item.get("id"),
                    "author": _person(item.get("author")),
                    "update_author": _person(item.get("updateAuthor")),
                    "created": item.get("created"),
                    "updated": item.get("updated"),
                    "body": body,
                    "body_truncated": truncated,
                    "visibility": _safe_value(item.get("visibility"), text_limit=1_000),
                }
            )
        return {
            "issue_key": key,
            "comments": comments,
            "start_at": payload.get("startAt", start),
            "max_results": payload.get("maxResults", page_size),
            "total": payload.get("total", len(comments)),
        }

    def issue_transitions(self, issue_key: str, *, include_fields: bool = True) -> dict[str, Any]:
        self._project_key_from_issue(issue_key)
        key = issue_key.strip().upper()
        payload = self._json(
            self._request_get(
                f"issue/{quote(key, safe='')}/transitions",
                params={"expand": "transitions.fields" if include_fields else "transitions"},
            )
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("transitions"), list):
            raise DitJiraError("invalid_response", "Jira returned invalid transition data")
        transitions = []
        for item in payload["transitions"]:
            if not isinstance(item, dict):
                continue
            transition = {
                "id": item.get("id"),
                "name": item.get("name"),
                "to": _status(item.get("to")),
                "has_screen": item.get("hasScreen"),
            }
            if include_fields and isinstance(item.get("fields"), dict):
                transition["fields"] = [
                    {
                        "id": field_id,
                        "name": meta.get("name", field_id) if isinstance(meta, dict) else field_id,
                        "required": bool(meta.get("required")) if isinstance(meta, dict) else False,
                        "schema": meta.get("schema") or {} if isinstance(meta, dict) else {},
                        "allowed_values": _safe_value(
                            meta.get("allowedValues") or [] if isinstance(meta, dict) else [],
                            text_limit=2_000,
                        ),
                    }
                    for field_id, meta in item["fields"].items()
                ]
            transitions.append(transition)
        return {
            "issue_key": key,
            "transitions": transitions,
            "note": "This is a read-only description of currently available workflow transitions; no transition is executed.",
        }

    def favourite_filters(self, *, limit: int = 50) -> dict[str, Any]:
        if not self.settings.allow_all_visible:
            raise DitJiraError(
                "project_policy_unsupported",
                "Favourite filters can span projects and are available only with --allow-all-visible",
            )
        payload = self._json(self._request_get("filter/favourite"))
        if not isinstance(payload, list):
            raise DitJiraError("invalid_response", "Jira returned invalid favourite filters")
        page_size = _clamp(limit, 1, self.settings.max_results)
        filters = []
        for item in payload[:page_size]:
            if not isinstance(item, dict):
                continue
            description, truncated = _bounded_text(item.get("description"), 4_000)
            filters.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "description": description,
                    "description_truncated": truncated,
                    "owner": _person(item.get("owner")),
                    "jql": item.get("jql"),
                    "favourite": bool(item.get("favourite")),
                    "view_url": item.get("viewUrl"),
                }
            )
        return {"filters": filters, "count": len(filters), "truncated": len(payload) > page_size}

    def list_boards(
        self,
        project_key: str,
        *,
        name: str = "",
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        project = self._require_project(project_key)
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        params: dict[str, Any] = {
            "projectKeyOrId": project,
            "startAt": start,
            "maxResults": page_size,
        }
        if name.strip():
            params["name"] = name.strip()[:255]
        payload = self._json(self._request_get("board", params=params, api="agile"))
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
            raise DitJiraError("invalid_response", "Jira Software returned invalid board data")
        boards = []
        for item in payload["values"]:
            if not isinstance(item, dict):
                continue
            location = item.get("location") if isinstance(item.get("location"), dict) else {}
            boards.append(
                {
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "type": item.get("type"),
                    "location": {
                        key: location.get(key)
                        for key in ("projectId", "projectKey", "projectName", "displayName")
                        if location.get(key) is not None
                    },
                }
            )
        total = payload.get("total")
        fallback_is_last = (
            start + len(boards) >= total
            if isinstance(total, int)
            else len(boards) < page_size
        )
        return {
            "project_key": project,
            "boards": boards,
            "start_at": payload.get("startAt", start),
            "max_results": payload.get("maxResults", page_size),
            "total": total if isinstance(total, int) else len(boards),
            "is_last": bool(payload.get("isLast", fallback_is_last)),
        }

    def list_sprints(
        self,
        project_key: str,
        board_id: int,
        *,
        state: str = "",
        start_at: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        project = self._require_project(project_key)
        try:
            board_id = int(board_id)
        except (TypeError, ValueError) as exc:
            raise DitJiraError("invalid_board", "board_id must be a positive integer") from exc
        if board_id <= 0:
            raise DitJiraError("invalid_board", "board_id must be positive")
        configuration = self._json(self._request_get(f"board/{board_id}/configuration", api="agile"))
        if not isinstance(configuration, dict):
            raise DitJiraError("invalid_response", "Jira Software returned invalid board configuration")
        location = configuration.get("location") if isinstance(configuration.get("location"), dict) else {}
        board_project = str(location.get("projectKey") or "").upper()
        if not self.settings.allow_all_visible and board_project != project:
            raise DitJiraError(
                "board_not_allowed",
                "The board is not located in the requested allowlisted project",
            )
        states = [part.strip().casefold() for part in state.split(",") if part.strip()]
        invalid_states = sorted(set(states) - {"future", "active", "closed"})
        if invalid_states:
            raise DitJiraError("invalid_state", "Sprint state must contain future, active, or closed")
        start = _clamp(start_at, 0, 1_000_000)
        page_size = _clamp(limit, 1, self.settings.max_results)
        params: dict[str, Any] = {"startAt": start, "maxResults": page_size}
        if states:
            params["state"] = ",".join(dict.fromkeys(states))
        payload = self._json(self._request_get(f"board/{board_id}/sprint", params=params, api="agile"))
        if not isinstance(payload, dict) or not isinstance(payload.get("values"), list):
            raise DitJiraError("invalid_response", "Jira Software returned invalid sprint data")
        return {
            "project_key": project,
            "board_id": board_id,
            "sprints": _safe_value(payload["values"], text_limit=4_000),
            "start_at": payload.get("startAt", start),
            "max_results": payload.get("maxResults", page_size),
            "total": payload.get("total", len(payload["values"])),
            "is_last": bool(payload.get("isLast", False)),
        }

    def probe(self) -> dict[str, Any]:
        info = self.server_info()
        user: dict[str, Any] | None = None
        if self.settings.auth_type != "anonymous":
            user = self.current_user()
        fields = self._field_catalog(refresh=True)
        projects = self.list_projects(limit=1)
        agile: dict[str, Any]
        try:
            response = self._json(
                self._request_get("board", params={"startAt": 0, "maxResults": 1}, api="agile")
            )
            agile = {"available": isinstance(response, dict)}
        except DitJiraError as exc:
            agile = {"available": False, "reason": exc.code, "status": exc.status}
        return {
            "server": info,
            "authenticated_user": user,
            "field_count": len(fields),
            "custom_field_count": sum(1 for item in fields if item.get("custom")),
            "visible_project_count": projects.get("total", 0),
            "jira_software": agile,
            "policy": {
                "allow_all_visible": self.settings.allow_all_visible,
                "allowed_projects": sorted(self.settings.allowed_projects),
            },
        }
