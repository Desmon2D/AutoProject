#!/usr/bin/python3
from __future__ import annotations

import os
import socket
import sys


def main() -> int:
    socket_path = os.environ.get("AUTOMATION_GIT_AUTH_SOCKET", "").strip()
    if not socket_path:
        return 1
    prompt = sys.argv[1] if len(sys.argv) > 1 else ""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall(prompt.encode("utf-8"))
        client.shutdown(socket.SHUT_WR)
        response = client.recv(4096)
    if not response:
        return 1
    sys.stdout.write(response.decode("utf-8") + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
