#!/bin/bash
# Point every Authentik email at the shared branded templates (idempotent).
# Usage: apply-email-branding [pack] [stack]   (stack default sksso-prod)
# Run on the swarm node that hosts the <stack>_server container. The role put
# the rendered templates in custom-templates/email/ and apply_email.py in
# media/public/<pack>/ (sksso branding/_email, rendered from <pack>/email.yml).
# pack defaults to the only media/public/*/apply_email.py in the container.
# No secrets here.
set -euo pipefail
PACK="${1:-}"
STACK="${2:-sksso-prod}"
C=$(docker ps -q -f "name=${STACK}_server" | head -1)
[ -n "$C" ] || { echo "no ${STACK}_server container on this node" >&2; exit 1; }
if [ -z "$PACK" ]; then
  mapfile -t found < <(docker exec "$C" sh -c 'ls /media/public/*/apply_email.py 2>/dev/null' || true)
  [ "${#found[@]}" -eq 1 ] || { echo "found ${#found[@]} apply_email.py, name the pack: $0 <pack> [stack]" >&2; exit 1; }
  PACK=$(basename "$(dirname "${found[0]}")")
fi
case "$PACK" in *[!A-Za-z0-9_.-]*|.*|'') echo "bad pack name: $PACK" >&2; exit 1;; esac
docker exec "$C" sh -c "test -f /media/public/$PACK/apply_email.py" \
  || { echo "no /media/public/$PACK/apply_email.py (does the pack have email.yml?)" >&2; exit 1; }
docker exec "$C" sh -c "ak shell < /media/public/$PACK/apply_email.py" 2>&1 \
  | grep -E '^(stage|otp stage|transport|flow title|missing|Traceback|[A-Za-z]*Error)' || true
