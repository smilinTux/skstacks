#!/bin/bash
# Apply the SKWorld brand to a running Authentik (idempotent).
# Usage: apply-branding.sh [stack]   (default sksso-prod)
# Run on the swarm node that hosts the <stack>_server container. Files were put
# in place by the role (media/public/skworld, custom-templates). Brand, recovery
# flow, the "Forgot password?" link, the recovery email template and subject and
# the flow titles are all set by apply_branding.py. No secrets here.
set -euo pipefail
STACK="${1:-sksso-prod}"
C=$(docker ps -q -f "name=${STACK}_server" | head -1)
[ -n "$C" ] || { echo "no ${STACK}_server container on this node" >&2; exit 1; }
ENVS=(-e SKWORLD_BRAND_TITLE="${SKWORLD_BRAND_TITLE:-SKWorld}"
      -e SKWORLD_FLOW_TITLE="${SKWORLD_FLOW_TITLE:-Welcome to the Kingdom}")
# Optional: unset keeps apply_branding.py's default "<brand title>: reset your password".
if [ -n "${SKWORLD_RECOVERY_SUBJECT:-}" ]; then
  ENVS+=(-e SKWORLD_RECOVERY_SUBJECT="$SKWORLD_RECOVERY_SUBJECT")
fi
docker exec "${ENVS[@]}" "$C" \
  sh -c 'ak shell < /media/public/skworld/apply_branding.py' 2>&1 | grep -E '^(brand|flow|stage|missing|Traceback|[A-Za-z]*Error)' || true
