#!/bin/sh
set -euo pipefail

APP_ENV="${APP_ENV:-prod}"

# Container-side log directory (bind-mounted from host).
# Host mount (both worker + acme):
#   /var/data/logs/skfenceha-${APP_ENV}/traefik  ->  /logs
LOG_BASE="/logs"

# NODE_HOSTNAME is injected via the compose environment (Swarm's
# {{.Node.Hostname}} templating).
if [ -z "${NODE_HOSTNAME:-}" ]; then
  echo "Warning: NODE_HOSTNAME not set, falling back to 'unknown-node'" >&2
  NODE_HOSTNAME="unknown-node"
fi

echo "NODE_HOSTNAME: $NODE_HOSTNAME" >&2

# Role logic: workers vs ACME master.
if [ "${TRAEFIK_WORKER_MODE:-false}" = "true" ]; then
  ROLE_DIR="worker"
else
  ROLE_DIR="acme"
fi

# Shared directory structure, distinct filenames per node: better for log
# collectors (Loki/Promtail) that watch a single directory.
LOG_DIR="${LOG_BASE}/${ROLE_DIR}"

echo "ROLE_DIR: $ROLE_DIR" >&2
echo "LOG_DIR: $LOG_DIR" >&2

mkdir -p "$LOG_DIR"

if [ -n "${TRAEFIK_UID:-}" ] && [ -n "${TRAEFIK_GID:-}" ]; then
  chown "${TRAEFIK_UID}:${TRAEFIK_GID}" "$LOG_DIR" 2>/dev/null || true
fi
chmod 2775 "$LOG_DIR" 2>/dev/null || true

ERROR_LOG="${LOG_DIR}/traefik-${NODE_HOSTNAME}.log"
ACCESS_LOG="${LOG_DIR}/access-${NODE_HOSTNAME}.log"

echo "ERROR_LOG: $ERROR_LOG" >&2
echo "ACCESS_LOG: $ACCESS_LOG" >&2

# ---------------------------------------------------------------------------
# Build a per-node runtime config.
#
# Traefik's three static-configuration sources (file, CLI arguments, env
# vars) are mutually exclusive: passing --configFile together with
# --log.filepath/--accesslog.filepath means Traefik uses the file and
# silently discards every other flag. The log paths must therefore live IN
# the config file. That file is shared over NFS by every node, so it can't
# carry a per-node filename; this copies it and injects this node's paths at
# startup. Filenames stay hostname-qualified so every node can write into
# the same shared directory without clobbering the others.
#
# Duplicate YAML keys are a hard error in Traefik's parser, so `level` is
# REPLACED in place rather than appended.
# ---------------------------------------------------------------------------
BASE_CONFIG="/etc/traefik/traefik.yml"
RUNTIME_CONFIG="/tmp/traefik-runtime.yml"

awk -v err="$ERROR_LOG" -v acc="$ACCESS_LOG" -v lvl="${LOG_LEVEL:-INFO}" '
  /^log:[[:space:]]*$/      { print; print "  filePath: \"" err "\""; inlog=1; next }
  /^accessLog:[[:space:]]*$/ { print; print "  filePath: \"" acc "\""; inlog=0; next }
  /^[^[:space:]#]/          { inlog=0 }
  inlog && /^[[:space:]]+level:/ { print "  level: " lvl; next }
                            { print }
' "$BASE_CONFIG" > "$RUNTIME_CONFIG"

# The two roles ship different base configs: traefik-worker.yml.j2 carries no
# log:/accessLog: blocks either, so append whole blocks when the key is
# absent rather than assuming it is there.
grep -qE '^log:' "$BASE_CONFIG" || \
  printf '\nlog:\n  level: %s\n  format: json\n  filePath: "%s"\n' "${LOG_LEVEL:-INFO}" "$ERROR_LOG" >> "$RUNTIME_CONFIG"
grep -qE '^accessLog:' "$BASE_CONFIG" || \
  printf '\naccessLog:\n  format: json\n  filePath: "%s"\n' "$ACCESS_LOG" >> "$RUNTIME_CONFIG"

# Fail loudly rather than silently reverting to stdout again.
grep -q "filePath: \"${ERROR_LOG}\"" "$RUNTIME_CONFIG" || {
  echo "FATAL: could not inject log filePath into $RUNTIME_CONFIG" >&2
  exit 1
}
grep -q "filePath: \"${ACCESS_LOG}\"" "$RUNTIME_CONFIG" || {
  echo "FATAL: could not set accessLog filePath in $RUNTIME_CONFIG" >&2
  exit 1
}

echo "RUNTIME_CONFIG: $RUNTIME_CONFIG (per-node log paths injected)" >&2

exec traefik --configFile="$RUNTIME_CONFIG"
