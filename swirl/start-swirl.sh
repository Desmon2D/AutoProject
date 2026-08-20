#!/bin/sh
set -eu

: "${SWIRL_USERNAME:?SWIRL_USERNAME must be set}"
: "${SWIRL_PASSWORD:?SWIRL_PASSWORD must be set}"

mkdir -p /state
if [ ! -s /state/db.sqlite3 ]; then
    cp /app/db.sqlite3.dist /state/db.sqlite3
fi

python swirl.py --no-version-check setup
python manage.py shell < /opt/automation-swirl/configure_admin.py

bookstack_base_url="${BOOKSTACK_BASE_URL:-}"
bookstack_token_id="${BOOKSTACK_TOKEN_ID:-}"
bookstack_token_secret="${BOOKSTACK_TOKEN_SECRET:-}"
if [ -n "$bookstack_base_url$bookstack_token_id$bookstack_token_secret" ]; then
    if [ -z "$bookstack_base_url" ] || [ -z "$bookstack_token_id" ] || [ -z "$bookstack_token_secret" ]; then
        echo "BOOKSTACK_BASE_URL, BOOKSTACK_TOKEN_ID and BOOKSTACK_TOKEN_SECRET must be set together" >&2
        exit 1
    fi
    python manage.py shell < /opt/automation-swirl/configure_bookstack.py
fi

swirl_public_url="${SWIRL_PUBLIC_URL:-http://localhost:8083}"
mkdir -p /app/static/api/config
jq --arg public_url "$swirl_public_url" '
    .default
    | .swirlBaseURL = ($public_url + "/swirl")
    | .msalConfig.auth.redirectUri = ($public_url + "/galaxy/microsoft-callback")
    | .oidcConfig.Microsoft.redirectUri = ($public_url + "/galaxy/oidc-callback")
    | .oidcConfig.Google.redirectUri = ($public_url + "/galaxy/oidc-callback")
' /app/config-swirl-demo.db.json > /app/static/api/config/default

if [ "${SWIRL_ENABLE_BEAT:-false}" = "true" ]; then
    python swirl.py --no-version-check start celery-worker celery-beats
else
    python swirl.py --no-version-check start celery-worker
fi
exec daphne -b 0.0.0.0 -p 8000 swirl_server.asgi:application
