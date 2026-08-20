#!/bin/sh
set -eu

if [ "$#" -gt 0 ]; then
    exec "$@"
fi

exec python3 /opt/sandbox/runner.py
