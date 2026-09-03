from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from websockets.sync.client import connect

class SudirAuthError(RuntimeError):
    pass


def _find_edge(explicit: str | None = None) -> Path:
    candidates = [
        Path(explicit) if explicit else None,
        Path(os.environ.get("ProgramFiles(x86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("ProgramFiles", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            return candidate.resolve()
    raise SudirAuthError("Microsoft Edge was not found; pass --edge-path")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _debug_json(port: int, path: str) -> Any:
    try:
        response = httpx.get(f"http://127.0.0.1:{port}{path}", timeout=1.0, trust_env=False)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError):
        return None


def _browser_cookies(port: int) -> tuple[dict[str, str], ...]:
    version = _debug_json(port, "/json/version")
    if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
        raise SudirAuthError("Cannot connect to the isolated Edge session")
    with connect(str(version["webSocketDebuggerUrl"]), open_timeout=5, close_timeout=2) as websocket:
        websocket.send(json.dumps({"id": 1, "method": "Storage.getCookies"}))
        while True:
            message = json.loads(websocket.recv(timeout=5))
            if message.get("id") == 1:
                if message.get("error"):
                    raise SudirAuthError("Edge refused to return the portal session")
                raw = message.get("result", {}).get("cookies", [])
                break
    cookies: list[dict[str, str]] = []
    for cookie in raw:
        domain = str(cookie.get("domain", "")).lstrip(".").casefold()
        if domain != "staff.mos.ru":
            continue
        name = str(cookie.get("name", ""))
        value = str(cookie.get("value", ""))
        if name and value:
            cookies.append(
                {
                    "name": name,
                    "value": value,
                    "domain": domain,
                    "path": str(cookie.get("path", "/")),
                }
            )
    if not cookies:
        raise SudirAuthError("SUDIR returned to the portal, but no staff.mos.ru session cookie was found")
    return tuple(cookies)


def authorize_sudir(*, edge_path: str | None = None, timeout_seconds: int = 300) -> tuple[dict[str, str], ...]:
    if not 30 <= timeout_seconds <= 900:
        raise SudirAuthError("--auth-timeout must be between 30 and 900 seconds")
    edge = _find_edge(edge_path)
    port = _free_port()
    profile = Path(tempfile.mkdtemp(prefix="dit-staff-sudir-"))
    start_url = "https://staff.mos.ru/mira/Do?doaction=ExternalLogin"
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            [
                str(edge),
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-extensions",
                f"--app={start_url}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + timeout_seconds
        saw_sudir = False
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SudirAuthError("The authorization window was closed before login completed")
            pages = _debug_json(port, "/json/list")
            if isinstance(pages, list):
                urls = [str(page.get("url", "")) for page in pages if isinstance(page, dict)]
                saw_sudir = saw_sudir or any(url.startswith("https://sudir.mos.ru/") for url in urls)
                returned = any(
                    url.startswith("https://staff.mos.ru/mira/") and "doaction=ExternalLogin" not in url
                    for url in urls
                )
                if saw_sudir and returned:
                    time.sleep(1)
                    return _browser_cookies(port)
            time.sleep(0.5)
        raise SudirAuthError("Timed out waiting for SUDIR authorization")
    finally:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        # This directory was created by this function under the OS temp directory.
        shutil.rmtree(profile, ignore_errors=True)
