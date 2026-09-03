from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .credentials import (
    DEFAULT_CREDENTIAL_TARGET,
    CredentialStoreError,
    load_credential,
    load_portal_cookies,
)


class ConfigError(ValueError):
    """Raised when the server cannot start safely with the supplied settings."""


@dataclass(frozen=True)
class Settings:
    base_url: str
    login: str
    password: str
    portal_cookies: tuple[dict[str, str], ...] = ()
    ca_bundle: str | None = None
    proxy: str | None = None
    trust_env: bool = True
    timeout_seconds: float = 20.0
    max_items: int = 50
    max_text_chars: int = 12_000
    max_response_chars: int = 80_000

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ConfigError("--base-url must be an absolute HTTPS URL")
        if parsed.query or parsed.fragment:
            raise ConfigError("--base-url must not contain a query or fragment")
        if not self.portal_cookies and (not self.login.strip() or not self.password):
            raise ConfigError("SUDIR authorization is required; run dit-staff-mcp --authorize-sudir")
        if self.ca_bundle and not Path(self.ca_bundle).is_file():
            raise ConfigError(f"CA bundle does not exist: {self.ca_bundle}")
        if not 1 <= self.timeout_seconds <= 120:
            raise ConfigError("--timeout must be between 1 and 120 seconds")
        if not 1 <= self.max_items <= 200:
            raise ConfigError("--max-items must be between 1 and 200")
        if not 1_000 <= self.max_text_chars <= 50_000:
            raise ConfigError("--max-text-chars must be between 1000 and 50000")
        if not 10_000 <= self.max_response_chars <= 250_000:
            raise ConfigError("--max-response-chars must be between 10000 and 250000")

    @property
    def api_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/service/v2"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Read-only MCP server for staff.mos.ru")
    parser.add_argument("--base-url", default="https://staff.mos.ru/mira")
    parser.add_argument("--ca-bundle", help="Path to a corporate CA bundle in PEM format")
    parser.add_argument("--proxy", help="Explicit HTTPS proxy URL")
    parser.add_argument(
        "--no-env-proxy",
        action="store_true",
        help="Ignore HTTP_PROXY/HTTPS_PROXY/NO_PROXY and connect directly unless --proxy is set",
    )
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--max-items", type=int, default=50)
    parser.add_argument("--max-text-chars", type=int, default=12_000)
    parser.add_argument("--max-response-chars", type=int, default=80_000)
    parser.add_argument("--credential-target", default=DEFAULT_CREDENTIAL_TARGET, help=argparse.SUPPRESS)
    parser.add_argument(
        "--save-credentials",
        action="store_true",
        help="Prompt, verify, and save credentials in Windows Credential Manager",
    )
    parser.add_argument(
        "--authorize-sudir",
        action="store_true",
        help="Open an isolated Edge window and authorize through SUDIR",
    )
    parser.add_argument("--edge-path", help="Path to msedge.exe for SUDIR authorization")
    parser.add_argument("--auth-timeout", type=int, default=300, help="Seconds to wait for SUDIR authorization")
    parser.add_argument(
        "--delete-credentials",
        action="store_true",
        help="Delete this MCP's entry from Windows Credential Manager",
    )
    parser.add_argument("--doctor", action="store_true", help="Authenticate and read the current profile, then exit")
    parser.add_argument("--verbose", action="store_true")
    return parser


def settings_from_args(args: argparse.Namespace) -> Settings:
    login = os.environ.get("DIT_STAFF_LOGIN", "").strip()
    password = os.environ.get("DIT_STAFF_PASSWORD", "")
    portal_cookies: tuple[dict[str, str], ...] = ()
    try:
        portal_cookies = load_portal_cookies()
    except CredentialStoreError:
        portal_cookies = ()
    if not portal_cookies and (not login or not password):
        try:
            stored = load_credential(args.credential_target)
        except CredentialStoreError:
            stored = None
        if stored:
            login = login or stored.login
            password = password or stored.password
    return Settings(
        base_url=args.base_url.rstrip("/"),
        login=login,
        password=password,
        portal_cookies=portal_cookies,
        ca_bundle=args.ca_bundle,
        proxy=args.proxy,
        trust_env=not args.no_env_proxy,
        timeout_seconds=args.timeout,
        max_items=args.max_items,
        max_text_chars=args.max_text_chars,
        max_response_chars=args.max_response_chars,
    )
