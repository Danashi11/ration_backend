#!/usr/bin/env bash
# Share the local app through an ngrok tunnel, e.g. for a professor to test it.
#
#   ./share.sh            # uses NGROK_DOMAIN from .env if set, else a random ngrok address
#   PORT=8002 ./share.sh  # another local port
#
# Runs with debug off behind gunicorn, so visitors never see Django's error pages.
# Uses your local database (db.sqlite3). Ctrl+C stops the app and the link.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8001}"
PY=venv/bin/python
GUNICORN=venv/bin/gunicorn

fail() { echo "share.sh: $*" >&2; exit 1; }

[ -x "$PY" ] || fail "no virtual environment found. Run: python3 -m venv venv && venv/bin/pip install -r requirements.txt"
[ -x "$GUNICORN" ] || fail "gunicorn is missing. Run: venv/bin/pip install -r requirements.txt"
command -v ngrok >/dev/null || fail "ngrok is not installed. See https://ngrok.com/download"

ngrok_config="$(ngrok config check 2>/dev/null | grep -o '/[^ ]*ngrok.yml' | head -1 || true)"
if [ -z "$ngrok_config" ] || ! grep -q '^ *authtoken:' "$ngrok_config"; then
  fail "ngrok isn't signed in. Run: ngrok config add-authtoken <token from https://dashboard.ngrok.com>"
fi

# Read one value from .env without loading the rest (it sets DJANGO_DEBUG=true for development).
env_value() { [ -f .env ] && grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '"'"'" || true; }
NGROK_DOMAIN="${NGROK_DOMAIN:-$(env_value NGROK_DOMAIN)}"

# A stable secret key for shared sessions, kept out of git.
if [ ! -s .share-secret-key ]; then
  "$PY" -c 'import secrets; print(secrets.token_urlsafe(50))' > .share-secret-key
  chmod 600 .share-secret-key
fi

if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  fail "port $PORT is already in use. Try: PORT=8002 ./share.sh"
fi

export DJANGO_DEBUG=false
export DJANGO_NGROK=true
export DJANGO_SECRET_KEY="$(cat .share-secret-key)"

echo "Preparing the app..."
"$PY" manage.py migrate --no-input -v 0
"$PY" manage.py collectstatic --no-input -v 0

"$GUNICORN" config.wsgi:application --bind "127.0.0.1:$PORT" --workers 2 --access-logfile - --log-level warning &
APP_PID=$!
trap 'kill "$APP_PID" 2>/dev/null; wait "$APP_PID" 2>/dev/null; echo; echo "Stopped. The link no longer works."' EXIT INT TERM

for _ in $(seq 1 30); do
  curl -s -o /dev/null "http://127.0.0.1:$PORT/login/" && break
  sleep 0.5
done
kill -0 "$APP_PID" 2>/dev/null || fail "the app didn't start. See the error above."

echo
if [ -n "$NGROK_DOMAIN" ]; then
  echo "Share this link:  https://$NGROK_DOMAIN"
  ngrok http "$PORT" --url "https://$NGROK_DOMAIN" --log stderr --log-level warn
else
  echo "No NGROK_DOMAIN in .env, so ngrok picks a random address. Copy the https link from the ngrok screen."
  ngrok http "$PORT"
fi
