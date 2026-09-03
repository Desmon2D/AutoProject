from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when the server cannot start safely with the supplied settings."""


def normalize_namespace(value: str) -> str:
    """Normalize a GitLab group or project path for policy comparisons."""
    return "/".join(part for part in value.strip().strip("/").split("/") if part).casefold()


@dataclass(frozen=True)
class Settings:
    base_url: str
    token: str
    auth_type: str
    allowed_projects: frozenset[str]
    allowed_groups: tuple[str, ...]
    allow_all_visible: bool = False
    ca_bundle: str | None = None
    proxy: str | None = None
    timeout_seconds: float = 20.0
    retries: int = 2
    max_file_bytes: int = 262_144
    max_tree_entries: int = 500
    max_diff_chars: int = 60_000
    max_job_log_chars: int = 40_000

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConfigError("--base-url must be an absolute HTTPS URL")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ConfigError("--base-url must not contain a path, query, or fragment")
        if not self.token.strip():
            raise ConfigError("DIT_GIT_TOKEN is required")
        if self.auth_type not in {"private-token", "bearer"}:
            raise ConfigError("--auth-type must be private-token or bearer")
        if not self.allow_all_visible and not self.allowed_projects and not self.allowed_groups:
            raise ConfigError("At least one --allow-project, --allow-group, or --allow-all-visible is required")
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise ConfigError(f"CA bundle does not exist: {self.ca_bundle}")
        if not 1 <= self.timeout_seconds <= 120:
            raise ConfigError("--timeout must be between 1 and 120 seconds")
        if not 0 <= self.retries <= 5:
            raise ConfigError("--retries must be between 0 and 5")
        if not 4_096 <= self.max_file_bytes <= 1_048_576:
            raise ConfigError("--max-file-bytes must be between 4096 and 1048576")
        if not 50 <= self.max_tree_entries <= 2_000:
            raise ConfigError("--max-tree-entries must be between 50 and 2000")
        if not 10_000 <= self.max_diff_chars <= 200_000:
            raise ConfigError("--max-diff-chars must be between 10000 and 200000")
        if not 4_000 <= self.max_job_log_chars <= 100_000:
            raise ConfigError("--max-job-log-chars must be between 4000 and 100000")

    @property
    def api_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/api/v4"

    def project_allowed(self, path_with_namespace: str) -> bool:
        if self.allow_all_visible:
            return True
        candidate = normalize_namespace(path_with_namespace)
        if candidate in self.allowed_projects:
            return True
        return any(candidate.startswith(f"{group}/") for group in self.allowed_groups)

    def group_allowed(self, group_path: str) -> bool:
        if self.allow_all_visible:
            return True
        candidate = normalize_namespace(group_path)
        return any(candidate == group or candidate.startswith(f"{group}/") for group in self.allowed_groups)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only MCP server for git.mos.ru")
    parser.add_argument("--base-url", default="https://git.mos.ru")
    parser.add_argument("--auth-type", choices=("private-token", "bearer"), default="private-token")
    parser.add_argument("--allow-project", action="append", default=[], metavar="GROUP/PROJECT")
    parser.add_argument("--allow-group", action="append", default=[], metavar="GROUP")
    parser.add_argument(
        "--allow-all-visible",
        action="store_true",
        help="Allow every project visible to DIT_GIT_TOKEN",
    )
    parser.add_argument("--ca-bundle", help="Path to the corporate CA bundle in PEM format")
    parser.add_argument("--proxy", help="HTTPS proxy URL")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-file-bytes", type=int, default=262_144)
    parser.add_argument("--max-tree-entries", type=int, default=500)
    parser.add_argument("--max-diff-chars", type=int, default=60_000)
    parser.add_argument("--max-job-log-chars", type=int, default=40_000)
    parser.add_argument("--doctor", action="store_true", help="Check GitLab connectivity, then exit")
    parser.add_argument("--verbose", action="store_true")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    projects = frozenset(filter(None, (normalize_namespace(value) for value in args.allow_project)))
    groups = tuple(sorted(set(filter(None, (normalize_namespace(value) for value in args.allow_group)))))
    return Settings(
        base_url=args.base_url.rstrip("/"),
        token=os.environ.get("DIT_GIT_TOKEN", ""),
        auth_type=args.auth_type,
        allowed_projects=projects,
        allowed_groups=groups,
        allow_all_visible=args.allow_all_visible,
        ca_bundle=args.ca_bundle,
        proxy=args.proxy,
        timeout_seconds=args.timeout,
        retries=args.retries,
        max_file_bytes=args.max_file_bytes,
        max_tree_entries=args.max_tree_entries,
        max_diff_chars=args.max_diff_chars,
        max_job_log_chars=args.max_job_log_chars,
    )
