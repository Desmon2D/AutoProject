from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .credentials import CredentialStoreError, load_portal_cookies


class ConfigError(ValueError):
    """Raised when the server cannot start safely with supplied settings."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    portal_cookies: tuple[dict[str, str], ...]
    ca_bundle: str | None = None
    proxy: str | None = None
    trust_env: bool = True
    timeout_seconds: float = 20.0
    max_links: int = 100
    max_text_chars: int = 16_000

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or parsed.hostname != "cfc.mos.ru":
            raise ConfigError("--base-url must use https://cfc.mos.ru")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ConfigError("--base-url must not contain credentials, a query, or a fragment")
        if not self.portal_cookies:
            raise ConfigError("CFC authorization is required; run dit-cfc-mcp --authorize")
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise ConfigError(f"CA bundle does not exist: {self.ca_bundle}")
        if not 1 <= self.timeout_seconds <= 120:
            raise ConfigError("--timeout must be between 1 and 120 seconds")
        if not 1 <= self.max_links <= 200:
            raise ConfigError("--max-links must be between 1 and 200")
        if not 1_000 <= self.max_text_chars <= 50_000:
            raise ConfigError("--max-text-chars must be between 1000 and 50000")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only MCP server for cfc.mos.ru")
    parser.add_argument("--base-url", default="https://cfc.mos.ru/")
    parser.add_argument("--ca-bundle", help="Path to a corporate CA bundle in PEM format")
    parser.add_argument("--proxy", help="Explicit HTTPS proxy URL")
    parser.add_argument(
        "--no-env-proxy",
        action="store_true",
        help="Ignore HTTP_PROXY/HTTPS_PROXY/NO_PROXY and connect directly unless --proxy is set",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-links", type=int, default=100)
    parser.add_argument("--max-text-chars", type=int, default=16_000)
    parser.add_argument("--authorize", action="store_true", help="Authorize in an isolated Edge window")
    parser.add_argument("--edge-path", help="Path to msedge.exe for browser authorization")
    parser.add_argument("--auth-timeout", type=int, default=300)
    parser.add_argument("--delete-session", action="store_true")
    parser.add_argument("--doctor", action="store_true", help="Read the portal home page, then exit")
    parser.add_argument("--verbose", action="store_true")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    try:
        cookies = load_portal_cookies()
    except CredentialStoreError:
        cookies = ()
    return Settings(
        base_url=args.base_url.rstrip("/") + "/",
        portal_cookies=cookies,
        ca_bundle=args.ca_bundle,
        proxy=args.proxy,
        trust_env=not args.no_env_proxy,
        timeout_seconds=args.timeout,
        max_links=args.max_links,
        max_text_chars=args.max_text_chars,
    )

