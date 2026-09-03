from __future__ import annotations

import html
import json
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import Request, urlopen

IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
BRANCH_TITLE = "Рабочая ветка: "
COMMIT_TITLE = "Коммит реализации: "
REPOSITORY_TITLE = "Рабочий репозиторий: "


class PlaneClientError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class PlaneClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        workspace_slug: str,
        state_ids: dict[str, str | None],
        *,
        gitea_public_base_url: str | None = None,
        timeout_seconds: float = 30,
        opener: Callable[..., Any] = urlopen,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Plane base URL must use HTTP or HTTPS")
        if not token:
            raise ValueError("Plane API token is required")
        if not IDENTIFIER_PATTERN.fullmatch(workspace_slug):
            raise ValueError("Plane workspace slug is invalid")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.workspace_slug = workspace_slug
        self.state_ids = state_ids
        self.gitea_public_base_url = (
            gitea_public_base_url.rstrip("/") if gitea_public_base_url else None
        )
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    @classmethod
    def from_environment(cls) -> PlaneClient | None:
        base_url = os.environ.get("PLANE_BASE_URL", "").strip()
        token = os.environ.get("PLANE_API_TOKEN", "").strip()
        workspace_slug = os.environ.get("PLANE_WORKSPACE_SLUG", "").strip()
        gitea_public_base_url = os.environ.get("GITEA_PUBLIC_BASE_URL", "").strip()
        if not (base_url or token or workspace_slug):
            return None
        if not (base_url and token and workspace_slug):
            raise ValueError(
                "PLANE_BASE_URL, PLANE_API_TOKEN and PLANE_WORKSPACE_SLUG must be set together"
            )
        return cls(
            base_url,
            token,
            workspace_slug,
            {
                "development_started": _first_csv("PLANE_IN_DEVELOPMENT_STATE_IDS"),
                "development_review": _first_csv("PLANE_DEVELOPMENT_REVIEW_STATE_IDS"),
                "manual_implementation_fix_required": _first_csv(
                    "PLANE_DEVELOPMENT_REVIEW_STATE_IDS"
                ),
                "approved_for_testing": _first_csv("PLANE_TESTING_STATE_IDS"),
                "return_to_development": _first_csv("PLANE_READY_STATE_IDS"),
                "accepted": _first_csv("PLANE_COMPLETED_STATE_IDS"),
                "rejected": _first_csv("PLANE_CANCELLED_STATE_IDS"),
                "cancelled": _first_csv("PLANE_CANCELLED_STATE_IDS"),
            },
            timeout_seconds=float(os.environ.get("PLANE_TIMEOUT_SECONDS", "30")),
            gitea_public_base_url=gitea_public_base_url or None,
        )

    def record_result(
        self,
        *,
        project_id: str,
        issue_id: str,
        workflow_id: str,
        recommendation: str,
        summary: str,
        details: dict[str, Any],
    ) -> dict[str, Any]:
        for label, value in {
            "project id": project_id,
            "issue id": issue_id,
            "workflow id": workflow_id,
        }.items():
            if not IDENTIFIER_PATTERN.fullmatch(value):
                raise PlaneClientError(f"Plane {label} is invalid")
        if recommendation not in {
            "development_started",
            "development_review",
            "approved_for_testing",
            "move_to_testing",
            "return_to_development",
            "accepted",
            "rejected",
            "cancelled",
            "manual_test_fix_required",
            "manual_implementation_fix_required",
        }:
            raise PlaneClientError("Plane recommendation is invalid")

        root = (
            f"/api/v1/workspaces/{quote(self.workspace_slug, safe='')}"
            f"/projects/{quote(project_id, safe='')}/work-items/{quote(issue_id, safe='')}"
        )
        state_id = self.state_ids.get(recommendation)
        state_required = {
            "development_started",
            "development_review",
            "approved_for_testing",
            "return_to_development",
            "accepted",
            "rejected",
            "cancelled",
            "manual_implementation_fix_required",
        }
        if recommendation in state_required and not state_id:
            raise PlaneClientError(f"Plane state is not configured for {recommendation}")
        source_links_updated = 0
        if recommendation in {"development_review", "move_to_testing"}:
            change = details.get("implementation_change")
            if isinstance(change, dict):
                source_links_updated = self._store_implementation_source(
                    root,
                    repository=change.get("repository"),
                    branch=change.get("branch"),
                    commit=change.get("commit"),
                )
        comment_body = self._comment_html(summary, workflow_id, recommendation, details)
        comment_created = self._create_comment(
            f"{root}/comments/",
            {
                "comment_html": comment_body,
                "external_source": "autoproject",
                "external_id": f"{workflow_id}:{recommendation}",
            },
        )
        state_updated = False
        if state_id:
            self._request("PATCH", f"{root}/", {"state": state_id})
            state_updated = True
        return {
            "recommendation": recommendation,
            "comment_created": comment_created,
            "state_updated": state_updated,
            "state_id": state_id,
            "source_links_updated": source_links_updated,
        }

    def get_implementation_source(
        self,
        *,
        project_id: str,
        issue_id: str,
    ) -> dict[str, str] | None:
        root = self._work_item_root(project_id, issue_id)
        payload = self._request("GET", f"{root}/links/")
        links = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(links, list):
            raise PlaneClientError("Plane returned an invalid work item link list")
        branch = None
        commit = None
        for link in links:
            title = link.get("title") if isinstance(link, dict) else None
            if not isinstance(title, str):
                continue
            if title.startswith(BRANCH_TITLE):
                branch = title[len(BRANCH_TITLE) :].strip()
            elif title.startswith(COMMIT_TITLE):
                commit = title[len(COMMIT_TITLE) :].strip().lower()
        if branch is None and commit is None:
            return None
        self._validate_ref(branch)
        if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
            raise PlaneClientError("Plane work item has an invalid implementation commit link")
        return {"implementation_ref": branch, "implementation_commit": commit}

    def get_repository_source(
        self,
        *,
        project_id: str,
        issue_id: str,
    ) -> dict[str, str] | None:
        if self.gitea_public_base_url is None:
            raise PlaneClientError("GITEA_PUBLIC_BASE_URL is required for repository links")
        root = self._work_item_root(project_id, issue_id)
        payload = self._request("GET", f"{root}/links/")
        links = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(links, list):
            raise PlaneClientError("Plane returned an invalid work item link list")
        candidates: dict[str, tuple[str, str]] = {}
        for link in links:
            if not isinstance(link, dict):
                continue
            url = link.get("url")
            if not isinstance(url, str):
                continue
            repository = self._repository_from_url(url)
            if repository is not None:
                candidates[repository.casefold()] = (repository, url)
        if not candidates:
            return None
        if len(candidates) > 1:
            raise PlaneClientError("Plane work item has multiple Gitea repository links")
        repository, source_url = next(iter(candidates.values()))
        return {"full_name": repository, "source_url": source_url}

    def _repository_from_url(self, value: str) -> str | None:
        base = urlsplit(self.gitea_public_base_url or "")
        candidate = urlsplit(value.strip())
        if (
            candidate.scheme.casefold() != base.scheme.casefold()
            or candidate.hostname != base.hostname
            or candidate.port != base.port
            or candidate.username is not None
            or candidate.password is not None
            or candidate.query
            or candidate.fragment
        ):
            return None
        base_parts = [part for part in base.path.split("/") if part]
        candidate_parts = [unquote(part) for part in candidate.path.split("/") if part]
        if candidate_parts[: len(base_parts)] != base_parts:
            return None
        repository_parts = candidate_parts[len(base_parts) :]
        if len(repository_parts) != 2:
            return None
        repository = "/".join(repository_parts)
        return repository if REPOSITORY_PATTERN.fullmatch(repository) else None

    def _store_implementation_source(
        self,
        root: str,
        *,
        repository: Any,
        branch: Any,
        commit: Any,
    ) -> int:
        if self.gitea_public_base_url is None:
            raise PlaneClientError("GITEA_PUBLIC_BASE_URL is required for Plane source links")
        if not isinstance(repository, str) or not REPOSITORY_PATTERN.fullmatch(repository):
            raise PlaneClientError("implementation repository is invalid")
        self._validate_ref(branch)
        if not isinstance(commit, str) or re.fullmatch(r"[0-9a-fA-F]{40}", commit) is None:
            raise PlaneClientError("implementation commit is invalid")
        owner, name = repository.split("/", 1)
        branch_url = (
            f"{self.gitea_public_base_url}/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/src/branch/{quote(branch, safe='')}"
        )
        commit_url = (
            f"{self.gitea_public_base_url}/{quote(owner, safe='')}/{quote(name, safe='')}"
            f"/commit/{commit.lower()}"
        )
        changed = 0
        repository_url = (
            f"{self.gitea_public_base_url}/{quote(owner, safe='')}/{quote(name, safe='')}"
        )
        changed += self._upsert_link(root, REPOSITORY_TITLE, repository, repository_url)
        changed += self._upsert_link(root, BRANCH_TITLE, branch, branch_url)
        changed += self._upsert_link(root, COMMIT_TITLE, commit.lower(), commit_url)
        return changed

    def _upsert_link(self, root: str, prefix: str, value: str, url: str) -> int:
        payload = self._request("GET", f"{root}/links/")
        links = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(links, list):
            raise PlaneClientError("Plane returned an invalid work item link list")
        title = f"{prefix}{value}"
        existing_by_url = next(
            (
                link
                for link in links
                if isinstance(link, dict) and link.get("url") == url
            ),
            None,
        )
        if existing_by_url is not None:
            # A repository URL is normally supplied by the user and its title is not
            # part of the repository-source contract. Preserve that title and avoid
            # a duplicate URL, which Plane rejects with HTTP 400.
            if prefix == REPOSITORY_TITLE or existing_by_url.get("title") == title:
                return 0
            existing = existing_by_url
        else:
            existing = next(
                (
                    link
                    for link in links
                    if isinstance(link, dict)
                    and isinstance(link.get("title"), str)
                    and link["title"].startswith(prefix)
                ),
                None,
            )
        if existing is None:
            self._request("POST", f"{root}/links/", {"title": title, "url": url})
            return 1
        if existing.get("title") == title and existing.get("url") == url:
            return 0
        link_id = existing.get("id")
        if not isinstance(link_id, str) or not IDENTIFIER_PATTERN.fullmatch(link_id):
            raise PlaneClientError("Plane returned an invalid work item link id")
        self._request("PATCH", f"{root}/links/{quote(link_id, safe='')}/", {"title": title, "url": url})
        return 1

    def _work_item_root(self, project_id: str, issue_id: str) -> str:
        for label, value in {"project id": project_id, "issue id": issue_id}.items():
            if not IDENTIFIER_PATTERN.fullmatch(value):
                raise PlaneClientError(f"Plane {label} is invalid")
        return (
            f"/api/v1/workspaces/{quote(self.workspace_slug, safe='')}"
            f"/projects/{quote(project_id, safe='')}/work-items/{quote(issue_id, safe='')}"
        )

    @staticmethod
    def _validate_ref(ref: Any) -> None:
        if (
            not isinstance(ref, str)
            or not REF_PATTERN.fullmatch(ref)
            or ".." in ref
            or "//" in ref
            or ref.endswith(("/", ".lock"))
        ):
            raise PlaneClientError("Plane work item has an invalid implementation branch link")

    def _create_comment(self, path: str, body: dict[str, Any]) -> bool:
        try:
            self._request("POST", path, body)
            return True
        except PlaneClientError as exc:
            if exc.status_code == 409:
                return False
            raise

    @staticmethod
    def _comment_html(
        summary: str,
        workflow_id: str,
        recommendation: str,
        details: dict[str, Any],
    ) -> str:
        rows = [
            f"<p><strong>Автоматизация:</strong> {html.escape(summary[:2000])}</p>",
            f"<p>Процесс: <code>{html.escape(workflow_id)}</code><br>",
            f"Результат: <code>{html.escape(recommendation)}</code></p>",
        ]
        safe_details = json.dumps(details, ensure_ascii=False, sort_keys=True)[:8000]
        if safe_details != "{}":
            rows.append(f"<pre>{html.escape(safe_details)}</pre>")
        return "".join(rows)

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
        request = Request(
            f"{self.base_url}{path}",
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                "X-Api-Key": self.token,
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read()
            return json.loads(raw) if raw else None
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise PlaneClientError(
                f"Plane returned HTTP {exc.code}: {detail}", status_code=exc.code
            ) from exc
        except (URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlaneClientError(f"Plane request failed: {exc}") from exc


def _first_csv(name: str) -> str | None:
    return next(
        (item.strip() for item in os.environ.get(name, "").split(",") if item.strip()),
        None,
    )
