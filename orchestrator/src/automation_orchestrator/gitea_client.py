from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REF_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
WORKFLOW_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class GiteaClientError(RuntimeError):
    pass


class GiteaClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        allowed_repositories: set[str],
        *,
        timeout_seconds: float = 30,
        opener: Callable[..., Any] = urlopen,
    ):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("Gitea base URL must use HTTP or HTTPS")
        if not token:
            raise ValueError("Gitea token is required")
        if not allowed_repositories:
            raise ValueError("at least one Gitea repository must be allowed")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.allowed_repositories = {item.lower() for item in allowed_repositories}
        self.timeout_seconds = timeout_seconds
        self.opener = opener

    @classmethod
    def from_environment(cls) -> GiteaClient | None:
        base_url = os.environ.get("GITEA_BASE_URL", "").strip()
        token = os.environ.get("GITEA_TOKEN", "").strip()
        allowed = {
            item.strip()
            for item in os.environ.get("GITEA_ALLOWED_REPOSITORIES", "").split(",")
            if item.strip()
        }
        if not (base_url or token or allowed):
            return None
        if not (base_url and token and allowed):
            raise ValueError(
                "GITEA_BASE_URL, GITEA_TOKEN and GITEA_ALLOWED_REPOSITORIES must be set together"
            )
        return cls(
            base_url,
            token,
            allowed,
            timeout_seconds=float(os.environ.get("GITEA_TIMEOUT_SECONDS", "30")),
        )

    def create_final_pull_request(
        self,
        *,
        repository: str,
        head: str,
        commit: str,
        workflow_id: str,
        title: str,
    ) -> dict[str, Any]:
        self._validate_repository(repository)
        self._validate_ref(head)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise GiteaClientError("final pull request commit must be a full Git hash")
        if not WORKFLOW_PATTERN.fullmatch(workflow_id):
            raise GiteaClientError("workflow id is invalid")

        owner, name = repository.split("/", 1)
        root = f"/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        metadata = self._request("GET", root)
        base = metadata.get("default_branch") if isinstance(metadata, dict) else None
        if not isinstance(base, str):
            raise GiteaClientError("Gitea repository has no default branch")
        self._validate_ref(base)

        idempotency_key = f"{workflow_id}-final-pull-request"
        marker = f"<!-- automation-idempotency-key: {idempotency_key} -->"
        open_pulls = self._request(
            "GET", f"{root}/pulls?{urlencode({'state': 'open', 'limit': 100})}"
        )
        for pull in open_pulls if isinstance(open_pulls, list) else []:
            if not isinstance(pull, dict) or marker not in str(pull.get("body", "")):
                continue
            self._validate_existing_pull(pull, head=head, base=base)
            return self._normalize_pull(pull, repository, head, base, commit, reused=True)

        body = (
            "Automated implementation and tests passed the configured checks.\n\n"
            f"<!-- automation-workflow: {workflow_id} -->\n"
            f"{marker}"
        )
        pull = self._request(
            "POST",
            f"{root}/pulls",
            {
                "title": title.strip()[:255] or f"Validated changes for {workflow_id}",
                "head": head,
                "base": base,
                "body": body,
            },
        )
        if not isinstance(pull, dict):
            raise GiteaClientError("Gitea returned an invalid pull request")
        return self._normalize_pull(pull, repository, head, base, commit, reused=False)

    def verify_branch(self, *, repository: str, branch: str, commit: str) -> None:
        self._validate_repository(repository)
        self._validate_ref(branch)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise GiteaClientError("branch commit must be a full Git hash")
        owner, name = repository.split("/", 1)
        root = f"/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        payload = self._request("GET", f"{root}/branches/{quote(branch, safe='')}")
        remote_commit = payload.get("commit") if isinstance(payload, dict) else None
        remote_id = remote_commit.get("id") if isinstance(remote_commit, dict) else None
        if remote_id != commit:
            raise GiteaClientError("remote branch does not point to the reported commit")

    def default_branch(self, repository: str) -> str:
        root = self._repository_root(repository)
        payload = self._request("GET", root)
        branch = payload.get("default_branch") if isinstance(payload, dict) else None
        if not isinstance(branch, str) or not branch:
            raise GiteaClientError("Gitea repository has no default branch")
        self._validate_ref(branch)
        return branch

    def verify_descendant(
        self,
        *,
        repository: str,
        ancestor: str,
        descendant: str,
        max_commits: int = 1000,
    ) -> None:
        self._validate_repository(repository)
        if not re.fullmatch(r"[0-9a-fA-F]{40}", ancestor) or not re.fullmatch(
            r"[0-9a-fA-F]{40}", descendant
        ):
            raise GiteaClientError("ancestry check requires full Git hashes")
        root = self._repository_root(repository)
        expected = ancestor.lower()
        pending = [descendant.lower()]
        visited: set[str] = set()
        while pending and len(visited) < max_commits:
            commit = pending.pop()
            if commit == expected:
                return
            if commit in visited:
                continue
            visited.add(commit)
            payload = self._request("GET", f"{root}/git/commits/{commit}")
            parents = payload.get("parents") if isinstance(payload, dict) else None
            if not isinstance(parents, list):
                raise GiteaClientError("Gitea returned invalid commit ancestry")
            pending.extend(
                str(parent.get("sha", "")).lower()
                for parent in parents
                if isinstance(parent, dict)
                and re.fullmatch(r"[0-9a-fA-F]{40}", str(parent.get("sha", "")))
            )
        raise GiteaClientError("reported commit is not descended from the required base commit")

    def download_archive(self, *, repository: str, commit: str) -> bytes:
        if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
            raise GiteaClientError("archive commit must be a full Git hash")
        root = self._repository_root(repository)
        return self._request_bytes("GET", f"{root}/archive/{commit}.tar.gz")

    def _repository_root(self, repository: str) -> str:
        self._validate_repository(repository)
        owner, name = repository.split("/", 1)
        return f"/api/v1/repos/{quote(owner, safe='')}/{quote(name, safe='')}"

    def _request_bytes(self, method: str, path: str) -> bytes:
        request = Request(
            f"{self.base_url}{path}",
            method=method,
            headers={
                "Accept": "application/octet-stream",
                "Authorization": f"token {self.token}",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise GiteaClientError(f"Gitea returned HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError) as exc:
            raise GiteaClientError(f"Gitea request failed: {exc}") from exc

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            f"{self.base_url}{path}",
            data=payload,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"token {self.token}",
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
            raise GiteaClientError(f"Gitea returned HTTP {exc.code}: {detail}") from exc
        except (URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GiteaClientError(f"Gitea request failed: {exc}") from exc

    def _validate_repository(self, repository: str) -> None:
        if not REPOSITORY_PATTERN.fullmatch(repository):
            raise GiteaClientError("repository name is invalid")
        if repository.lower() not in self.allowed_repositories:
            raise GiteaClientError("repository is not allowed")

    @staticmethod
    def _validate_ref(ref: str) -> None:
        if (
            not REF_PATTERN.fullmatch(ref)
            or ".." in ref
            or "//" in ref
            or ref.endswith(("/", ".lock"))
        ):
            raise GiteaClientError("Git ref is invalid")

    @staticmethod
    def _validate_existing_pull(pull: dict[str, Any], *, head: str, base: str) -> None:
        pull_head = pull.get("head")
        pull_base = pull.get("base")
        if not isinstance(pull_head, dict) or not isinstance(pull_base, dict):
            raise GiteaClientError("existing pull request has invalid refs")
        if pull_head.get("ref") != head or pull_base.get("ref") != base:
            raise GiteaClientError("idempotency marker belongs to different pull request refs")

    @staticmethod
    def _normalize_pull(
        pull: dict[str, Any],
        repository: str,
        head: str,
        base: str,
        commit: str,
        *,
        reused: bool,
    ) -> dict[str, Any]:
        index = pull.get("number") or pull.get("index")
        url = pull.get("html_url") or pull.get("url")
        if type(index) is not int or index < 1 or not isinstance(url, str):
            raise GiteaClientError("Gitea returned an invalid pull request reference")
        return {
            "repository": repository,
            "index": index,
            "url": url,
            "base": base,
            "head": head,
            "commit": commit,
            "reused": reused,
        }
