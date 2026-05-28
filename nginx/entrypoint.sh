#!/bin/sh
set -e

CERT_DIR=/etc/nginx/certs
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
  mkdir -p "$CERT_DIR"
  echo "Generating self-signed cert in $CERT_DIR…"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERT_DIR/privkey.pem" \
    -out    "$CERT_DIR/fullchain.pem" \
    -subj   "/CN=aitrader-dashboard"
  chmod 600 "$CERT_DIR/privkey.pem"
fi

: "${DASH_USER:?DASH_USER is required}"
: "${DASH_PASSWORD:?DASH_PASSWORD is required}"

htpasswd -bc /etc/nginx/.htpasswd "$DASH_USER" "$DASH_PASSWORD" >/dev/null

exec nginx -g 'daemon off;'
