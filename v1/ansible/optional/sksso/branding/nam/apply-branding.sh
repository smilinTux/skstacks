#!/bin/bash
# Apply the NAM brand to a running Authentik (idempotent).
# Usage: apply-branding.sh [stack]   (default sksso-prod)
# Run on the swarm node that hosts the <stack>_server container. Files were put
# in place by the role (media/public/nam, custom-templates). No secrets here.
set -euo pipefail
STACK="${1:-sksso-prod}"
C=$(docker ps -q -f "name=${STACK}_server" | head -1)
[ -n "$C" ] || { echo "no ${STACK}_server container on this node" >&2; exit 1; }
docker exec -e NAM_BRAND_TITLE="${NAM_BRAND_TITLE:-NAM - Native Asset Management}" "$C" \
  sh -c 'ak shell < /media/public/nam/apply_branding.py' 2>&1 | grep -E '^(brand|stage|flow title|missing|Traceback|[A-Za-z]*Error)' || true
