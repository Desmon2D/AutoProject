from __future__ import annotations

import argparse
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(ValueError):
    """Raised when the MCP server cannot start with the supplied settings."""


_SPACE_KEY_RE = re.compile(r"^[A-Za-z0-9_~.-]+$")


def normalize_space_key(value: str) -> str:
    key = value.strip()
    if not _SPACE_KEY_RE.fullmatch(key):
        raise ConfigError(f"Invalid Confluence space key: {value!r}")
    return key


@dataclass(frozen=True)
class Settings:
    base_url: str
    auth_type: str
    token: str
    username: str
    password: str
    allowed_spaces: frozenset[str]
    allow_all_visible: bool = False
    ca_bundle: str | None = None
    proxy: str | None = None
    use_env_proxy: bool = True
    timeout_seconds: float = 20.0
    retries: int = 2
    max_results: int = 100
    max_text_chars: int = 30_000

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConfigError("--base-url must be an absolute HTTPS URL")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ConfigError("--base-url must not contain a path, query, or fragment")
        if parsed.username or parsed.password:
            raise ConfigError("Credentials must not be embedded in --base-url")
        if self.auth_type not in {"pat", "basic", "anonymous"}:
            raise ConfigError("--auth-type must be pat, basic, or anonymous")
        if self.auth_type == "pat" and (
            not self.token.strip() or self.token.strip().startswith("${")
        ):
            raise ConfigError("DIT_CONFLUENCE_TOKEN is required when --auth-type=pat")
        if self.auth_type == "basic" and (not self.username.strip() or not self.password):
            raise ConfigError(
                "DIT_CONFLUENCE_USERNAME and DIT_CONFLUENCE_PASSWORD are required "
                "when --auth-type=basic"
            )
        if not self.allow_all_visible and not self.allowed_spaces:
            raise ConfigError("At least one --allow-space or --allow-all-visible is required")
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise ConfigError(f"CA bundle does not exist: {self.ca_bundle}")
        if not 1 <= self.timeout_seconds <= 120:
            raise ConfigError("--timeout must be between 1 and 120 seconds")
        if not 0 <= self.retries <= 5:
            raise ConfigError("--retries must be between 0 and 5")
        if not 10 <= self.max_results <= 200:
            raise ConfigError("--max-results must be between 10 and 200")
        if not 1_000 <= self.max_text_chars <= 100_000:
            raise ConfigError("--max-text-chars must be between 1000 and 100000")

    @property
    def rest_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/rest/api"

    def space_allowed(self, space_key: str) -> bool:
        folded = space_key.strip().casefold()
        return self.allow_all_visible or folded in {
            key.casefold() for key in self.allowed_spaces
        }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only MCP server for itpm-wiki.mos.ru")
    parser.add_argument("--base-url", default="https://itpm-wiki.mos.ru")
    parser.add_argument("--auth-type", choices=("pat", "basic", "anonymous"), default="pat")
    parser.add_argument("--allow-space", action="append", default=[], metavar="KEY")
    parser.add_argument(
        "--allow-all-visible",
        action="store_true",
        help="Allow every Confluence space visible to the configured identity",
    )
    parser.add_argument("--ca-bundle", help="Path to the corporate CA bundle in PEM format")
    parser.add_argument("--proxy", help="Explicit HTTPS proxy URL")
    parser.add_argument(
        "--no-env-proxy",
        action="store_true",
        help="Ignore HTTP_PROXY, HTTPS_PROXY, and ALL_PROXY for Confluence requests",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--max-results", type=int, default=100)
    parser.add_argument("--max-text-chars", type=int, default=30_000)
    parser.add_argument("--doctor", action="store_true", help="Check Confluence connectivity, then exit")
    parser.add_argument("--verbose", action="store_true")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    return Settings(
        base_url=args.base_url.rstrip("/"),
        auth_type=args.auth_type,
        token=os.environ.get("DIT_CONFLUENCE_TOKEN", ""),
        username=os.environ.get("DIT_CONFLUENCE_USERNAME", ""),
        password=os.environ.get("DIT_CONFLUENCE_PASSWORD", ""),
        allowed_spaces=frozenset(normalize_space_key(value) for value in args.allow_space),
        allow_all_visible=args.allow_all_visible,
        ca_bundle=args.ca_bundle,
        proxy=args.proxy,
        use_env_proxy=not args.no_env_proxy,
        timeout_seconds=args.timeout,
        retries=args.retries,
        max_results=args.max_results,
        max_text_chars=args.max_text_chars,
    )
