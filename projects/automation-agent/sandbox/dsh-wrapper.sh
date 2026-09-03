#!/bin/sh
set -eu

exec node --expose-internals \
    /usr/local/lib/node_modules/@deepseek-ai/dsh/lib/bin.js \
    "$@"
