from __future__ import annotations

import argparse
from collections.abc import Mapping
import os
from pathlib import Path
import subprocess
import sys

from dotenv import dotenv_values


SAFE_ENVIRONMENT_NAMES = {
    "ALL_PROXY",
    "APPDATA",
    "COMSPEC",
    "HOMEDRIVE",
    "HOMEPATH",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "LOG_LEVEL",
    "NO_PROXY",
    "PATH",
    "PATHEXT",
    "PROCESSOR_ARCHITECTURE",
    "PROGRAMDATA",
    "PROGRAMFILES",
    "PROGRAMFILES(X86)",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TZ",
    "USERDOMAIN",
    "USERNAME",
    "USERPROFILE",
    "WINDIR",
}


def safe_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    source = os.environ if source is None else source
    allowed = {name.casefold() for name in SAFE_ENVIRONMENT_NAMES}
    return {name: value for name, value in source.items() if name.casefold() in allowed}


def main() -> int:
    parser = argparse.ArgumentParser(description="Launch one MCP with scoped credentials")
    parser.add_argument("--secrets-file", type=Path, required=True)
    parser.add_argument("--secret", action="append", default=[])
    parser.add_argument("--command", required=True)
    parser.add_argument("args", nargs=argparse.REMAINDER)
    options = parser.parse_args()

    credentials = dotenv_values(options.secrets_file)
    environment = safe_environment()
    for name in options.secret:
        value = credentials.get(name)
        if value:
            environment[name] = value

    command_args = options.args[1:] if options.args[:1] == ["--"] else options.args
    try:
        return subprocess.call([options.command, *command_args], env=environment)
    except OSError as error:
        print(f"Unable to start MCP executable: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
