#!/usr/bin/env bash
# Smoke test for the nginx reverse proxy.
# Assumes the docker-compose stack is up and DASH_USER/DASH_PASSWORD are set in .env.
set -euo pipefail

if [ ! -f .env ]; then
  echo "FAIL: .env not found at repo root" >&2
  exit 1
fi
# shellcheck disable=SC1091
. .env

assert() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "OK   $label → $actual"
  else
    echo "FAIL $label expected=$expected actual=$actual" >&2
    exit 1
  fi
}

# 1. http should redirect to https
code=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/)
assert "http→https redirect" "301" "$code"

# 2. https without auth → 401
code=$(curl -k -s -o /dev/null -w "%{http_code}" https://localhost/)
assert "https no-auth"       "401" "$code"

# 3. https with bad password → 401
code=$(curl -k -s -o /dev/null -w "%{http_code}" -u "${DASH_USER}:wrong-pass" https://localhost/)
assert "https bad-password"  "401" "$code"

# 4. https with correct creds → 200
code=$(curl -k -s -o /dev/null -w "%{http_code}" -u "${DASH_USER}:${DASH_PASSWORD}" https://localhost/)
assert "https good-creds"    "200" "$code"

echo "ALL OK"
