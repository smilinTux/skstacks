#!/bin/sh
# entrypoint.sh — substitute env vars into coturn config, then start the server.
#
# Required env vars:
#   COTURN_EXTERNAL_IP   — public IP of this host (required for NAT traversal)
#   COTURN_AUTH_SECRET   — HMAC secret for time-limited credentials
#
# Optional env vars (have sane defaults):
#   COTURN_TLS_CERT      — path to TLS certificate  (default: /etc/coturn/certs/fullchain.pem)
#   COTURN_TLS_PKEY      — path to TLS private key  (default: /etc/coturn/certs/privkey.pem)
set -e

: "${COTURN_EXTERNAL_IP:?COTURN_EXTERNAL_IP must be set to the server public IP}"
: "${COTURN_AUTH_SECRET:?COTURN_AUTH_SECRET must be set (openssl rand -hex 32)}"

COTURN_TLS_CERT="${COTURN_TLS_CERT:-/etc/coturn/certs/fullchain.pem}"
COTURN_TLS_PKEY="${COTURN_TLS_PKEY:-/etc/coturn/certs/privkey.pem}"

export COTURN_EXTERNAL_IP COTURN_AUTH_SECRET COTURN_TLS_CERT COTURN_TLS_PKEY

mkdir -p /var/log/coturn

# Produce final config from template
envsubst < /etc/coturn/turnserver.conf.tmpl > /etc/coturn/turnserver.conf

exec turnserver -c /etc/coturn/turnserver.conf "$@"
