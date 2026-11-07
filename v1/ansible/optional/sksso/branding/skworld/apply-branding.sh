#!/bin/bash
# Apply the SKWorld brand to a running Authentik (idempotent).
# Usage: apply-branding.sh [stack]   (default sksso-prod)
# Run on the swarm node that hosts the <stack>_server container. Files were put
# in place by the role (media/public/skworld). Brand, recovery flow and the
# "Forgot password?" link are all set by apply_branding.py. No secrets here.
set -euo pipefail
STACK="${1:-sksso-prod}"
C=$(docker ps -q -f "name=${STACK}_server" | head -1)
[ -n "$C" ] || { echo "no ${STACK}_server container on this node" >&2; exit 1; }
docker exec -e SKWORLD_BRAND_TITLE="${SKWORLD_BRAND_TITLE:-SKWorld}" \
  -e SKWORLD_FLOW_TITLE="${SKWORLD_FLOW_TITLE:-Welcome to the Kingdom}" "$C" \
  sh -c 'ak shell < /media/public/skworld/apply_branding.py' 2>&1 | grep -E '^(brand|flow|stage|missing|Traceback|[A-Za-z]*Error)' || true
