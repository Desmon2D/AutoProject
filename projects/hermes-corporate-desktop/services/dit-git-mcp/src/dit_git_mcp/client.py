from __future__ import annotations

import base64
import binascii
import codecs
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx

from .config import Settings, normalize_namespace


_SECRET_LINE_RE = re.compile(
    r"(?im)^(\s*(?:export\s+)?[A-Z0-9_.-]*(?:TOKEN|PASSWORD|PASSWD|SECRET|API_KEY|PRIVATE_KEY)"
    r"[A-Z0-9_.-]*\s*[=:]\s*).+$"
)
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:glpat-[A-Za-z0-9_-]{8,}|ghp_[A-Za-z0-9_]{8,}|sk-[A-Za-z0-9_-]{8,})\b"
)
_SENSITIVE_BASENAMES = frozenset({
    ".env",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
})
_SENSITIVE_SUFFIXES = (".jks", ".key", ".keystore", ".p12", ".pfx", ".pem")


class DitGitError(RuntimeError):
    """Safe error suitable for returning through the MCP boundary."""

    def __init__(self, code: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        error: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.status is not None:
            error["status"] = self.status
        return error


@dataclass(frozen=True)
class ProjectRef:
    identifier: str
    path: str


def _clamp(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(int(value), maximum))


def _pagination(response: httpx.Response) -> dict[str, Any]:
    def number(name: str) -> int | None:
        raw = response.headers.get(name, "").strip()
        return int(raw) if raw.isdigit() else None

    return {
        "page": number("x-page"),
        "next_page": number("x-next-page"),
        "total": number("x-total"),
    }


def _is_sensitive_path(path: str) -> bool:
    basename = path.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if basename in _SENSITIVE_BASENAMES or basename.endswith(_SENSITIVE_SUFFIXES):
        return True
    if basename.startswith(".env.") and not basename.endswith((".example", ".sample", ".template")):
        return True
    return False


def _redact_secrets(text: str) -> tuple[str, bool]:
    redacted, line_count = _SECRET_LINE_RE.subn(r"\1[REDACTED]", text)
    redacted, value_count = _SECRET_VALUE_RE.subn("[REDACTED]", redacted)
    return redacted, bool(line_count or value_count)


def _decode_text(raw: bytes) -> tuple[str, str]:
    if raw.startswith(codecs.BOM_UTF8):
        return raw.decode("utf-8-sig"), "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return raw.decode("utf-16"), "utf-16"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        raise DitGitError("unsupported_encoding", "File is not UTF-8 or BOM-marked UTF-16 text") from exc


def _bounded_text(value: Any, limit: int) -> tuple[str, bool]:
    text = str(value or "")
    return text[:limit], len(text) > limit


class GitLabClient:
    """Small, policy-enforcing client for the GitLab v4 read API."""

    _RETRYABLE_STATUS = frozenset({429, 502, 503, 504})

    def __init__(self, settings: Settings, *, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        headers = {"Accept": "application/json", "User-Agent": "dit-git-mcp/0.1.0"}
        if settings.auth_type == "bearer":
            headers["Authorization"] = f"Bearer {settings.token}"
        else:
            headers["PRIVATE-TOKEN"] = settings.token

        verify: bool | str = settings.ca_bundle or True
        self._client = httpx.Client(
            headers=headers,
            timeout=settings.timeout_seconds,
            verify=verify,
            proxy=settings.proxy,
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> GitLabClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        if method.upper() != "GET":
            raise DitGitError("read_only_violation", "DIT Git MCP permits GET requests only")
        url = f"{self.settings.api_url}/{path.lstrip('/')}"
        last_error: Exception | None = None

        for attempt in range(self.settings.retries + 1):
            try:
                response = self._client.request(method, url, params=params)
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt >= self.settings.retries:
                    raise DitGitError("upstream_unreachable", "Git service is unavailable or timed out") from exc
                time.sleep(min(0.25 * (2**attempt), 2.0))
                continue

            if response.status_code in self._RETRYABLE_STATUS and attempt < self.settings.retries:
                retry_after = response.headers.get("retry-after", "")
                delay = float(retry_after) if retry_after.replace(".", "", 1).isdigit() else 0.25 * (2**attempt)
                time.sleep(min(delay, 3.0))
                continue
            if response.is_success:
                return response
            raise self._api_error(response)

        raise DitGitError("upstream_unreachable", "Git service is unavailable") from last_error

    @staticmethod
    def _api_error(response: httpx.Response) -> DitGitError:
        messages = {
            401: ("unauthorized", "Git credentials were rejected"),
            403: ("forbidden", "Git service denied access"),
            404: ("not_found", "Git object was not found or is not visible"),
            429: ("rate_limited", "Git service rate limit was reached"),
        }
        code, message = messages.get(response.status_code, ("upstream_error", "Git service request failed"))
        return DitGitError(code, message, status=response.status_code)

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise DitGitError("invalid_response", "Git service returned invalid JSON") from exc

    @staticmethod
    def _project_summary(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload.get("id"),
            "path": payload.get("path_with_namespace") or "",
            "name": payload.get("name") or "",
            "description": payload.get("description") or "",
            "default_branch": payload.get("default_branch") or "",
            "topics": payload.get("topics") or payload.get("tag_list") or [],
            "visibility": payload.get("visibility") or "",
            "archived": bool(payload.get("archived")),
            "empty_repo": bool(payload.get("empty_repo")),
            "created_at": payload.get("created_at") or "",
            "last_activity_at": payload.get("last_activity_at") or "",
            "web_url": payload.get("web_url") or "",
        }

    @staticmethod
    def _commit_summary(payload: dict[str, Any], *, include_message: bool = False) -> dict[str, Any]:
        result = {
            "id": payload.get("id") or "",
            "short_id": payload.get("short_id") or "",
            "title": payload.get("title") or "",
            "author_name": payload.get("author_name") or "",
            "authored_date": payload.get("authored_date") or "",
            "committer_name": payload.get("committer_name") or "",
            "committed_date": payload.get("committed_date") or "",
            "parent_ids": payload.get("parent_ids") or [],
            "web_url": payload.get("web_url") or "",
        }
        if include_message:
            result["message"] = str(payload.get("message") or "")[:4_000]
        if isinstance(payload.get("stats"), dict):
            result["stats"] = {
                "additions": payload["stats"].get("additions"),
                "deletions": payload["stats"].get("deletions"),
                "total": payload["stats"].get("total"),
            }
        return result

    @staticmethod
    def _user_summary(payload: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        return {
            "id": payload.get("id"),
            "username": payload.get("username") or "",
            "name": payload.get("name") or "",
        }

    @classmethod
    def _merge_request_summary(cls, payload: dict[str, Any], *, include_description: bool = False) -> dict[str, Any]:
        result = {
            "iid": payload.get("iid"),
            "title": payload.get("title") or "",
            "state": payload.get("state") or "",
            "draft": bool(payload.get("draft") or payload.get("work_in_progress")),
            "source_branch": payload.get("source_branch") or "",
            "target_branch": payload.get("target_branch") or "",
            "author": cls._user_summary(payload.get("author")),
            "assignees": [cls._user_summary(item) for item in payload.get("assignees") or [] if isinstance(item, dict)],
            "reviewers": [cls._user_summary(item) for item in payload.get("reviewers") or [] if isinstance(item, dict)],
            "labels": payload.get("labels") or [],
            "created_at": payload.get("created_at") or "",
            "updated_at": payload.get("updated_at") or "",
            "merged_at": payload.get("merged_at") or "",
            "detailed_merge_status": payload.get("detailed_merge_status") or payload.get("merge_status") or "",
            "has_conflicts": bool(payload.get("has_conflicts")),
            "blocking_discussions_resolved": payload.get("blocking_discussions_resolved"),
            "user_notes_count": payload.get("user_notes_count"),
            "web_url": payload.get("web_url") or "",
        }
        if include_description:
            description, truncated = _bounded_text(payload.get("description"), 8_000)
            description, redacted = _redact_secrets(description)
            result.update(
                {
                    "description": description,
                    "description_truncated": truncated,
                    "description_redacted": redacted,
                }
            )
        pipeline = payload.get("head_pipeline") or payload.get("pipeline")
        if isinstance(pipeline, dict):
            result["pipeline"] = {
                "id": pipeline.get("id"),
                "status": pipeline.get("status") or "",
                "ref": pipeline.get("ref") or "",
                "sha": pipeline.get("sha") or "",
                "web_url": pipeline.get("web_url") or "",
            }
        return result

    @staticmethod
    def _pipeline_summary(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload.get("id"),
            "iid": payload.get("iid"),
            "status": payload.get("status") or "",
            "source": payload.get("source") or "",
            "ref": payload.get("ref") or "",
            "sha": payload.get("sha") or "",
            "created_at": payload.get("created_at") or "",
            "updated_at": payload.get("updated_at") or "",
            "web_url": payload.get("web_url") or "",
        }

    @staticmethod
    def _job_summary(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": payload.get("id"),
            "name": payload.get("name") or "",
            "stage": payload.get("stage") or "",
            "status": payload.get("status") or "",
            "failure_reason": payload.get("failure_reason") or "",
            "allow_failure": bool(payload.get("allow_failure")),
            "ref": payload.get("ref") or "",
            "duration": payload.get("duration"),
            "queued_duration": payload.get("queued_duration"),
            "started_at": payload.get("started_at") or "",
            "finished_at": payload.get("finished_at") or "",
            "web_url": payload.get("web_url") or "",
        }

    def _project_payload(self, resolved: ProjectRef) -> dict[str, Any]:
        payload = self._json(self._request("GET", f"projects/{resolved.identifier}"))
        if not isinstance(payload, dict) or not payload.get("path_with_namespace"):
            raise DitGitError("invalid_response", "Git service returned an invalid project record")
        actual_path = normalize_namespace(str(payload["path_with_namespace"]))
        if not self.settings.project_allowed(actual_path):
            raise DitGitError("project_not_allowed", "Project is outside the configured allowlist")
        return payload

    def _resolved_ref(self, resolved: ProjectRef, ref: str | None) -> tuple[str, str]:
        resolved_ref = (ref or "").strip()
        if not resolved_ref:
            project = self._project_payload(resolved)
            resolved_ref = str(project.get("default_branch") or "HEAD")
        commit = self._json(
            self._request(
                "GET",
                f"projects/{resolved.identifier}/repository/commits/{quote(resolved_ref, safe='')}",
            )
        )
        if not isinstance(commit, dict) or not commit.get("id"):
            raise DitGitError("invalid_response", "Git service returned an invalid ref")
        return resolved_ref, str(commit["id"])

    def _bounded_diffs(self, payload: Any, *, limit: int = 30) -> tuple[list[dict[str, Any]], bool]:
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned invalid diff data")
        remaining = self.settings.max_diff_chars
        results: list[dict[str, Any]] = []
        truncated = len(payload) > limit
        for item in payload[:limit]:
            if not isinstance(item, dict):
                continue
            patch = str(item.get("diff") or "")
            allowed = min(12_000, max(0, remaining))
            shown = patch[:allowed]
            item_truncated = len(patch) > len(shown) or bool(item.get("too_large") or item.get("collapsed"))
            results.append(
                {
                    "old_path": item.get("old_path") or "",
                    "new_path": item.get("new_path") or "",
                    "new_file": bool(item.get("new_file")),
                    "renamed_file": bool(item.get("renamed_file")),
                    "deleted_file": bool(item.get("deleted_file")),
                    "diff": shown,
                    "truncated": item_truncated,
                }
            )
            remaining -= len(shown)
            if remaining <= 0:
                truncated = True
                break
        return results, truncated

    def probe(self) -> dict[str, Any]:
        payload = self._json(self._request("GET", "user"))
        return {
            "base_url": self.settings.base_url,
            "user": payload.get("username", "") if isinstance(payload, dict) else "",
            "name": payload.get("name", "") if isinstance(payload, dict) else "",
        }

    def _resolve_project(self, project: str | int) -> ProjectRef:
        raw = str(project).strip()
        if not raw:
            raise DitGitError("invalid_argument", "project is required")

        if not raw.isdigit():
            project_path = normalize_namespace(raw)
            if not self.settings.project_allowed(project_path):
                raise DitGitError("project_not_allowed", "Project is outside the configured allowlist")
            return ProjectRef(identifier=quote(project_path, safe=""), path=project_path)

        payload = self._json(self._request("GET", f"projects/{raw}"))
        if not isinstance(payload, dict) or not payload.get("path_with_namespace"):
            raise DitGitError("invalid_response", "Git service returned an invalid project record")
        project_path = normalize_namespace(str(payload["path_with_namespace"]))
        if not self.settings.project_allowed(project_path):
            raise DitGitError("project_not_allowed", "Project is outside the configured allowlist")
        return ProjectRef(identifier=raw, path=project_path)

    def get_project(self, project: str | int) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        payload = self._project_payload(resolved)
        return self._project_summary(payload)

    def search_projects(self, query: str = "", *, group: str | None = None, page: int = 1, limit: int = 20) -> dict[str, Any]:
        page = _clamp(page, 1, 10_000)
        limit = _clamp(limit, 1, 50)
        query = query.strip()

        # An exact-project-only policy should never scan every project visible
        # to the token. Resolve the configured paths directly, paginate the
        # filtered set locally, and avoid leaking the upstream global count.
        if group is None and self.settings.allowed_projects and not self.settings.allowed_groups:
            projects = []
            needle = query.casefold()
            for project_path in sorted(self.settings.allowed_projects):
                resolved = ProjectRef(identifier=quote(project_path, safe=""), path=project_path)
                try:
                    item = self._project_summary(self._project_payload(resolved))
                except DitGitError as exc:
                    if exc.code == "not_found":
                        continue
                    raise
                haystack = " ".join((str(item["path"]), str(item["name"]), str(item["description"]))).casefold()
                if not needle or needle in haystack:
                    projects.append(item)
            start = (page - 1) * limit
            selected = projects[start : start + limit]
            next_page = page + 1 if start + limit < len(projects) else None
            return {
                "projects": selected,
                "count": len(selected),
                "pagination": {"page": page, "next_page": next_page, "total": len(projects)},
            }

        params: dict[str, Any] = {
            "simple": "true",
            "order_by": "last_activity_at",
            "sort": "desc",
            "page": page,
            "per_page": limit,
        }
        if query:
            params["search"] = query[:200]

        if group:
            normalized_group = normalize_namespace(group)
            if not self.settings.group_allowed(normalized_group):
                raise DitGitError("group_not_allowed", "Group is outside the configured allowlist")
            path = f"groups/{quote(normalized_group, safe='')}/projects"
            params["include_subgroups"] = "true"
        else:
            path = "projects"

        response = self._request("GET", path, params=params)
        payload = self._json(response)
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned an invalid project list")

        projects = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            project_path = str(item.get("path_with_namespace", ""))
            if not self.settings.project_allowed(project_path):
                continue
            projects.append(self._project_summary(item))

        return {"projects": projects, "count": len(projects), "pagination": _pagination(response)}

    def list_tree(
        self,
        project: str | int,
        *,
        path: str = "",
        ref: str | None = None,
        page: int = 1,
        limit: int = 100,
        depth: int = 2,
        max_entries: int = 300,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        clean_path = path.strip().strip("/")
        if "\x00" in clean_path:
            raise DitGitError("invalid_argument", "path contains an invalid character")
        page = _clamp(page, 1, 10_000)
        limit = _clamp(limit, 1, 100)
        depth = _clamp(depth, 1, 5)
        max_entries = _clamp(max_entries, 1, self.settings.max_tree_entries)
        resolved_ref, commit_sha = self._resolved_ref(resolved, ref)
        current_page = page
        entries: list[dict[str, Any]] = []
        next_page: int | None = None
        scanned_pages = 0
        last_response: httpx.Response | None = None

        while len(entries) < max_entries and scanned_pages < 20:
            per_page = min(limit, max_entries - len(entries))
            params: dict[str, Any] = {
                "path": clean_path,
                "page": current_page,
                "per_page": per_page,
                "recursive": "true" if depth > 1 else "false",
                "ref": commit_sha,
            }
            response = self._request("GET", f"projects/{resolved.identifier}/repository/tree", params=params)
            last_response = response
            payload = self._json(response)
            if not isinstance(payload, list):
                raise DitGitError("invalid_response", "Git service returned an invalid repository tree")

            for item in payload:
                if not isinstance(item, dict):
                    continue
                item_path = str(item.get("path") or "")
                relative = item_path[len(clean_path) + 1 :] if clean_path and item_path.startswith(f"{clean_path}/") else item_path
                item_depth = relative.count("/") + 1 if relative else 0
                if item_depth > depth:
                    continue
                entries.append(
                    {
                        "id": item.get("id"),
                        "name": item.get("name") or "",
                        "path": item_path,
                        "type": item.get("type") or "",
                        "mode": item.get("mode") or "",
                        "depth": item_depth,
                    }
                )
                if len(entries) >= max_entries:
                    break

            scanned_pages += 1
            raw_next = response.headers.get("x-next-page", "").strip()
            if raw_next.isdigit():
                next_page = int(raw_next)
            elif len(payload) == per_page:
                next_page = current_page + 1
            else:
                next_page = None
            if next_page is None or len(entries) >= max_entries:
                break
            current_page = next_page

        truncated = next_page is not None or scanned_pages >= 20
        return {
            "project": resolved.path,
            "path": clean_path,
            "ref": resolved_ref,
            "commit_sha": commit_sha,
            "depth": depth,
            "entries": entries,
            "count": len(entries),
            "truncated": truncated,
            "pagination": {
                "page": page,
                "next_page": next_page if truncated else None,
                "total": _pagination(last_response).get("total") if last_response is not None else None,
            },
        }

    def read_file(
        self,
        project: str | int,
        file_path: str,
        *,
        ref: str | None = None,
        start_line: int = 1,
        end_line: int | None = None,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        clean_path = file_path.strip().strip("/")
        if not clean_path or "\x00" in clean_path:
            raise DitGitError("invalid_argument", "file_path is required")
        if _is_sensitive_path(clean_path):
            raise DitGitError("sensitive_path", "Reading this credential or private-key path is blocked by policy")
        if start_line < 1 or (end_line is not None and end_line < start_line):
            raise DitGitError("invalid_argument", "Requested line range is invalid")
        params = {"ref": (ref or "HEAD")[:255]}
        response = self._request(
            "GET",
            f"projects/{resolved.identifier}/repository/files/{quote(clean_path, safe='')}",
            params=params,
        )
        payload = self._json(response)
        if not isinstance(payload, dict) or payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
            raise DitGitError("invalid_response", "Git service returned an unsupported file payload")

        declared_size = payload.get("size")
        if isinstance(declared_size, int) and declared_size > self.settings.max_file_bytes:
            raise DitGitError(
                "file_too_large",
                f"File exceeds the configured {self.settings.max_file_bytes}-byte limit",
            )
        try:
            raw = base64.b64decode(payload["content"], validate=True)
        except (binascii.Error, ValueError) as exc:
            raise DitGitError("invalid_response", "Git service returned invalid base64 file content") from exc
        if len(raw) > self.settings.max_file_bytes:
            raise DitGitError("file_too_large", f"File exceeds the configured {self.settings.max_file_bytes}-byte limit")
        if b"\x00" in raw:
            raise DitGitError("binary_file", "Binary files are not returned by this MCP server")
        text, encoding = _decode_text(raw)

        lines = text.splitlines()
        if lines and start_line > len(lines):
            raise DitGitError("range_out_of_bounds", f"start_line exceeds the file's {len(lines)} lines")
        if not lines:
            return {
                "project": resolved.path,
                "path": clean_path,
                "ref": payload.get("ref") or ref or "HEAD",
                "blob_id": payload.get("blob_id") or "",
                "last_commit_id": payload.get("last_commit_id") or "",
                "size": len(raw),
                "encoding": encoding,
                "start_line": 1,
                "end_line": 0,
                "total_lines": 0,
                "truncated": False,
                "redacted": False,
                "content": "",
            }
        start = start_line
        requested_end = end_line if end_line is not None else start + 499
        end = _clamp(requested_end, start, min(max(start, len(lines)), start + 999))
        content = "\n".join(lines[start - 1 : end])
        content, redacted = _redact_secrets(content)
        return {
            "project": resolved.path,
            "path": clean_path,
            "ref": payload.get("ref") or ref or "HEAD",
            "blob_id": payload.get("blob_id") or "",
            "last_commit_id": payload.get("last_commit_id") or "",
            "size": len(raw),
            "encoding": encoding,
            "start_line": start,
            "end_line": end,
            "total_lines": len(lines),
            "truncated": end < len(lines),
            "redacted": redacted,
            "content": content,
        }

    def read_files(
        self,
        project: str | int,
        file_paths: list[str],
        *,
        ref: str | None = None,
        max_lines_per_file: int = 300,
    ) -> dict[str, Any]:
        if not isinstance(file_paths, list) or not file_paths:
            raise DitGitError("invalid_argument", "file_paths must contain at least one path")
        if len(file_paths) > 10:
            raise DitGitError("invalid_argument", "At most 10 files can be read per call")
        resolved = self._resolve_project(project)
        max_lines_per_file = _clamp(max_lines_per_file, 1, 500)
        results = []
        total_chars = 0
        truncated = False
        for raw_path in file_paths:
            path = str(raw_path)
            try:
                item = self.read_file(
                    resolved.path,
                    path,
                    ref=ref,
                    start_line=1,
                    end_line=max_lines_per_file,
                )
                content = str(item.get("content") or "")
                remaining = max(0, self.settings.max_diff_chars - total_chars)
                if len(content) > remaining:
                    item["content"] = content[:remaining]
                    item["truncated"] = True
                    truncated = True
                total_chars += len(str(item.get("content") or ""))
                results.append({"ok": True, "data": item})
                if total_chars >= self.settings.max_diff_chars:
                    truncated = True
                    break
            except DitGitError as exc:
                results.append({"ok": False, "path": path, "error": exc.as_dict()})
        return {
            "project": resolved.path,
            "ref": ref or "HEAD",
            "files": results,
            "count": len(results),
            "truncated": truncated or len(results) < len(file_paths),
        }

    def search_code(
        self,
        project: str | int,
        query: str,
        *,
        ref: str | None = None,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        clean_query = query.strip()
        if len(clean_query) < 2:
            raise DitGitError("invalid_argument", "query must contain at least two characters")
        page = _clamp(page, 1, 10_000)
        limit = _clamp(limit, 1, 50)
        params: dict[str, Any] = {
            "scope": "blobs",
            "search": clean_query[:256],
            "page": page,
            "per_page": limit,
        }
        if ref:
            params["ref"] = ref[:255]
        response = self._request("GET", f"projects/{resolved.identifier}/search", params=params)
        payload = self._json(response)
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned invalid search results")
        matches = [
            {
                "path": item.get("path") or item.get("filename") or "",
                "ref": item.get("ref") or ref or "",
                "start_line": item.get("startline"),
                "snippet": str(item.get("data") or "")[:2_000],
            }
            for item in payload
            if isinstance(item, dict)
        ]
        return {
            "project": resolved.path,
            "query": clean_query,
            "matches": matches,
            "count": len(matches),
            "pagination": _pagination(response),
        }

    def list_refs(
        self,
        project: str | int,
        *,
        kind: str = "branches",
        search: str = "",
        page: int = 1,
        limit: int = 50,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        if kind not in {"branches", "tags"}:
            raise DitGitError("invalid_argument", "kind must be branches or tags")
        page = _clamp(page, 1, 10_000)
        limit = _clamp(limit, 1, 100)
        params: dict[str, Any] = {"page": page, "per_page": limit}
        if search.strip():
            params["search"] = search.strip()[:200]
        response = self._request("GET", f"projects/{resolved.identifier}/repository/{kind}", params=params)
        payload = self._json(response)
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned invalid refs")
        refs = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            entry = {
                "name": item.get("name") or "",
                "commit": self._commit_summary(commit),
                "protected": bool(item.get("protected")),
            }
            if kind == "branches":
                entry.update({"default": bool(item.get("default")), "merged": bool(item.get("merged"))})
            else:
                message, message_truncated = _bounded_text(item.get("message"), 2_000)
                entry.update(
                    {
                        "message": message,
                        "message_truncated": message_truncated,
                        "target": item.get("target") or "",
                        "release": item.get("release") or None,
                    }
                )
            refs.append(entry)
        return {
            "project": resolved.path,
            "kind": kind,
            "refs": refs,
            "count": len(refs),
            "pagination": _pagination(response),
        }

    def list_commits(
        self,
        project: str | int,
        *,
        ref: str | None = None,
        path: str = "",
        author: str = "",
        since: str | None = None,
        until: str | None = None,
        page: int = 1,
        limit: int = 30,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        page = _clamp(page, 1, 10_000)
        limit = _clamp(limit, 1, 100)
        params: dict[str, Any] = {"page": page, "per_page": limit, "with_stats": "true"}
        if ref:
            params["ref_name"] = ref[:255]
        if path.strip():
            params["path"] = path.strip().strip("/")[:1_000]
        if author.strip():
            params["author"] = author.strip()[:200]
        if since:
            params["since"] = since[:64]
        if until:
            params["until"] = until[:64]
        response = self._request("GET", f"projects/{resolved.identifier}/repository/commits", params=params)
        payload = self._json(response)
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned invalid commits")
        commits = [self._commit_summary(item) for item in payload if isinstance(item, dict)]
        return {
            "project": resolved.path,
            "ref": ref or "default",
            "commits": commits,
            "count": len(commits),
            "pagination": _pagination(response),
        }

    def get_commit(
        self,
        project: str | int,
        sha: str,
        *,
        include_diff: bool = False,
        diff_limit: int = 30,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        clean_sha = sha.strip()
        if not clean_sha:
            raise DitGitError("invalid_argument", "sha is required")
        payload = self._json(
            self._request(
                "GET",
                f"projects/{resolved.identifier}/repository/commits/{quote(clean_sha[:255], safe='')}",
                params={"stats": "true"},
            )
        )
        if not isinstance(payload, dict) or not payload.get("id"):
            raise DitGitError("invalid_response", "Git service returned an invalid commit")
        result = {
            "project": resolved.path,
            "commit": self._commit_summary(payload, include_message=True),
        }
        if include_diff:
            diff_limit = _clamp(diff_limit, 1, 100)
            diff_response = self._request(
                "GET",
                f"projects/{resolved.identifier}/repository/commits/{quote(str(payload['id']), safe='')}/diff",
                params={"page": 1, "per_page": diff_limit},
            )
            diff_payload = self._json(diff_response)
            diffs, truncated = self._bounded_diffs(diff_payload, limit=diff_limit)
            result["diffs"] = diffs
            result["diffs_truncated"] = truncated or bool(diff_response.headers.get("x-next-page", "").strip())
        return result

    def compare_refs(
        self,
        project: str | int,
        from_ref: str,
        to_ref: str,
        *,
        straight: bool = True,
        commit_limit: int = 50,
        diff_limit: int = 30,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        if not from_ref.strip() or not to_ref.strip():
            raise DitGitError("invalid_argument", "Both from_ref and to_ref are required")
        commit_limit = _clamp(commit_limit, 1, 100)
        diff_limit = _clamp(diff_limit, 1, 100)
        payload = self._json(
            self._request(
                "GET",
                f"projects/{resolved.identifier}/repository/compare",
                params={
                    "from": from_ref.strip()[:255],
                    "to": to_ref.strip()[:255],
                    "straight": "true" if straight else "false",
                },
            )
        )
        if not isinstance(payload, dict):
            raise DitGitError("invalid_response", "Git service returned an invalid comparison")
        raw_commits = payload.get("commits") if isinstance(payload.get("commits"), list) else []
        commits = [self._commit_summary(item) for item in raw_commits[:commit_limit] if isinstance(item, dict)]
        diffs, diffs_truncated = self._bounded_diffs(payload.get("diffs") or [], limit=diff_limit)
        return {
            "project": resolved.path,
            "from_ref": from_ref.strip(),
            "to_ref": to_ref.strip(),
            "compare_timeout": bool(payload.get("compare_timeout")),
            "compare_same_ref": bool(payload.get("compare_same_ref")),
            "commits": commits,
            "commits_truncated": len(raw_commits) > len(commits),
            "diffs": diffs,
            "diffs_truncated": diffs_truncated,
        }

    def list_merge_requests(
        self,
        project: str | int,
        *,
        state: str = "opened",
        search: str = "",
        source_branch: str = "",
        target_branch: str = "",
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        if state not in {"opened", "closed", "merged", "all"}:
            raise DitGitError("invalid_argument", "state must be opened, closed, merged, or all")
        page = _clamp(page, 1, 10_000)
        limit = _clamp(limit, 1, 50)
        params: dict[str, Any] = {
            "scope": "all",
            "state": state,
            "order_by": "updated_at",
            "sort": "desc",
            "page": page,
            "per_page": limit,
        }
        if search.strip():
            params.update({"search": search.strip()[:200], "in": "title,description"})
        if source_branch.strip():
            params["source_branch"] = source_branch.strip()[:255]
        if target_branch.strip():
            params["target_branch"] = target_branch.strip()[:255]
        response = self._request("GET", f"projects/{resolved.identifier}/merge_requests", params=params)
        payload = self._json(response)
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned invalid merge requests")
        items = [self._merge_request_summary(item) for item in payload if isinstance(item, dict)]
        return {
            "project": resolved.path,
            "merge_requests": items,
            "count": len(items),
            "pagination": _pagination(response),
        }

    def get_merge_request(
        self,
        project: str | int,
        iid: int,
        *,
        include_commits: bool = False,
        include_diffs: bool = False,
        include_discussions: bool = False,
        include_approvals: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        if int(iid) < 1:
            raise DitGitError("invalid_argument", "iid must be positive")
        limit = _clamp(limit, 1, 50)
        base = f"projects/{resolved.identifier}/merge_requests/{int(iid)}"
        payload = self._json(self._request("GET", base))
        if not isinstance(payload, dict) or not payload.get("iid"):
            raise DitGitError("invalid_response", "Git service returned an invalid merge request")
        result: dict[str, Any] = {
            "project": resolved.path,
            "merge_request": self._merge_request_summary(payload, include_description=True),
        }

        if include_commits:
            commits_response = self._request("GET", f"{base}/commits", params={"page": 1, "per_page": limit})
            commits_payload = self._json(commits_response)
            if not isinstance(commits_payload, list):
                raise DitGitError("invalid_response", "Git service returned invalid merge request commits")
            result["commits"] = [
                self._commit_summary(item) for item in commits_payload[:limit] if isinstance(item, dict)
            ]
            result["commits_truncated"] = bool(commits_response.headers.get("x-next-page", "").strip())

        if include_diffs:
            try:
                diffs_response = self._request("GET", f"{base}/diffs", params={"page": 1, "per_page": limit})
                diffs_payload = self._json(diffs_response)
            except DitGitError as exc:
                if exc.code != "not_found":
                    raise
                # Compatibility with older self-managed GitLab versions.
                legacy = self._json(self._request("GET", f"{base}/changes"))
                if not isinstance(legacy, dict):
                    raise DitGitError("invalid_response", "Git service returned invalid merge request changes")
                diffs_response = None
                diffs_payload = legacy.get("changes") or []
            diffs, truncated = self._bounded_diffs(diffs_payload, limit=limit)
            result["diffs"] = diffs
            result["diffs_truncated"] = truncated or bool(
                diffs_response is not None and diffs_response.headers.get("x-next-page", "").strip()
            )

        if include_discussions:
            discussions_response = self._request("GET", f"{base}/discussions", params={"page": 1, "per_page": limit})
            discussions_payload = self._json(discussions_response)
            if not isinstance(discussions_payload, list):
                raise DitGitError("invalid_response", "Git service returned invalid merge request discussions")
            discussions = []
            for discussion in discussions_payload[:limit]:
                if not isinstance(discussion, dict):
                    continue
                notes = []
                for note in (discussion.get("notes") or [])[:10]:
                    if not isinstance(note, dict):
                        continue
                    body, body_truncated = _bounded_text(note.get("body"), 2_000)
                    body, redacted = _redact_secrets(body)
                    notes.append(
                        {
                            "id": note.get("id"),
                            "author": self._user_summary(note.get("author")),
                            "body": body,
                            "body_truncated": body_truncated,
                            "body_redacted": redacted,
                            "system": bool(note.get("system")),
                            "resolvable": bool(note.get("resolvable")),
                            "resolved": bool(note.get("resolved")),
                            "created_at": note.get("created_at") or "",
                        }
                    )
                discussions.append({"id": discussion.get("id"), "individual_note": bool(discussion.get("individual_note")), "notes": notes})
            result["discussions"] = discussions
            result["discussions_truncated"] = bool(discussions_response.headers.get("x-next-page", "").strip())

        if include_approvals:
            try:
                approvals = self._json(self._request("GET", f"{base}/approvals"))
                if isinstance(approvals, dict):
                    result["approvals"] = {
                        "approved": bool(approvals.get("approved")),
                        "approvals_required": approvals.get("approvals_required"),
                        "approvals_left": approvals.get("approvals_left"),
                        "approved_by": [
                            self._user_summary(item.get("user"))
                            for item in approvals.get("approved_by") or []
                            if isinstance(item, dict)
                        ],
                    }
            except DitGitError as exc:
                if exc.code not in {"not_found", "forbidden"}:
                    raise
                result["approvals"] = {"available": False, "reason": exc.code}
        return result

    def list_pipelines(
        self,
        project: str | int,
        *,
        status: str = "",
        ref: str = "",
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        page = _clamp(page, 1, 10_000)
        limit = _clamp(limit, 1, 100)
        params: dict[str, Any] = {"page": page, "per_page": limit, "order_by": "id", "sort": "desc"}
        if status.strip():
            params["status"] = status.strip()[:32]
        if ref.strip():
            params["ref"] = ref.strip()[:255]
        response = self._request("GET", f"projects/{resolved.identifier}/pipelines", params=params)
        payload = self._json(response)
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned invalid pipelines")
        pipelines = [self._pipeline_summary(item) for item in payload if isinstance(item, dict)]
        return {
            "project": resolved.path,
            "pipelines": pipelines,
            "count": len(pipelines),
            "pagination": _pagination(response),
        }

    def get_pipeline(
        self,
        project: str | int,
        pipeline_id: int,
        *,
        include_jobs: bool = True,
        job_status: str = "",
        job_limit: int = 50,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        if int(pipeline_id) < 1:
            raise DitGitError("invalid_argument", "pipeline_id must be positive")
        base = f"projects/{resolved.identifier}/pipelines/{int(pipeline_id)}"
        payload = self._json(self._request("GET", base))
        if not isinstance(payload, dict) or not payload.get("id"):
            raise DitGitError("invalid_response", "Git service returned an invalid pipeline")
        result: dict[str, Any] = {"project": resolved.path, "pipeline": self._pipeline_summary(payload)}
        if include_jobs:
            job_limit = _clamp(job_limit, 1, 100)
            params: dict[str, Any] = {"page": 1, "per_page": job_limit, "include_retried": "true"}
            if job_status.strip():
                params["scope[]"] = job_status.strip()[:32]
            jobs_response = self._request("GET", f"{base}/jobs", params=params)
            jobs_payload = self._json(jobs_response)
            if not isinstance(jobs_payload, list):
                raise DitGitError("invalid_response", "Git service returned invalid pipeline jobs")
            result["jobs"] = [self._job_summary(item) for item in jobs_payload if isinstance(item, dict)]
            result["jobs_truncated"] = bool(jobs_response.headers.get("x-next-page", "").strip())
        return result

    def get_job_log(
        self,
        project: str | int,
        job_id: int,
        *,
        max_chars: int = 20_000,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        if int(job_id) < 1:
            raise DitGitError("invalid_argument", "job_id must be positive")
        max_chars = _clamp(max_chars, 1_000, self.settings.max_job_log_chars)
        response = self._request("GET", f"projects/{resolved.identifier}/jobs/{int(job_id)}/trace")
        text = response.text
        truncated = len(text) > max_chars
        shown = text[-max_chars:]
        shown, redacted = _redact_secrets(shown)
        return {
            "project": resolved.path,
            "job_id": int(job_id),
            "content": shown,
            "truncated_from_start": truncated,
            "redacted": redacted,
        }

    def blame_file(
        self,
        project: str | int,
        file_path: str,
        *,
        ref: str | None = None,
        start_line: int = 1,
        end_line: int = 100,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        clean_path = file_path.strip().strip("/")
        if not clean_path or _is_sensitive_path(clean_path):
            raise DitGitError("sensitive_path", "File path is empty or blocked by policy")
        if start_line < 1 or end_line < start_line or end_line - start_line >= 500:
            raise DitGitError("invalid_argument", "Blame range must contain between 1 and 500 lines")
        params = {
            "ref": (ref or "HEAD")[:255],
            "range[start]": start_line,
            "range[end]": end_line,
        }
        payload = self._json(
            self._request(
                "GET",
                f"projects/{resolved.identifier}/repository/files/{quote(clean_path, safe='')}/blame",
                params=params,
            )
        )
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned invalid blame data")
        ranges = []
        returned_lines = 0
        for item in payload:
            if not isinstance(item, dict):
                continue
            lines = [str(line) for line in item.get("lines") or []]
            returned_lines += len(lines)
            content, redacted = _redact_secrets("\n".join(lines))
            ranges.append(
                {
                    "commit": self._commit_summary(item.get("commit") or {}),
                    "line_count": len(lines),
                    "content": content,
                    "redacted": redacted,
                }
            )
        return {
            "project": resolved.path,
            "path": clean_path,
            "ref": ref or "HEAD",
            "start_line": start_line,
            "end_line": end_line,
            "returned_lines": returned_lines,
            "ranges": ranges,
        }

    def list_releases(
        self,
        project: str | int,
        *,
        page: int = 1,
        limit: int = 20,
    ) -> dict[str, Any]:
        resolved = self._resolve_project(project)
        page = _clamp(page, 1, 10_000)
        limit = _clamp(limit, 1, 50)
        response = self._request(
            "GET",
            f"projects/{resolved.identifier}/releases",
            params={"page": page, "per_page": limit, "order_by": "released_at", "sort": "desc"},
        )
        payload = self._json(response)
        if not isinstance(payload, list):
            raise DitGitError("invalid_response", "Git service returned invalid releases")
        releases = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            description, truncated = _bounded_text(item.get("description"), 6_000)
            description, redacted = _redact_secrets(description)
            commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
            releases.append(
                {
                    "tag_name": item.get("tag_name") or "",
                    "name": item.get("name") or "",
                    "description": description,
                    "description_truncated": truncated,
                    "description_redacted": redacted,
                    "created_at": item.get("created_at") or "",
                    "released_at": item.get("released_at") or "",
                    "upcoming_release": bool(item.get("upcoming_release")),
                    "author": self._user_summary(item.get("author")),
                    "commit": self._commit_summary(commit),
                    "_links": item.get("_links") or {},
                }
            )
        return {
            "project": resolved.path,
            "releases": releases,
            "count": len(releases),
            "pagination": _pagination(response),
        }
