#!/bin/bash
# Create or update ONE Forgejo OAuth2 auth source (skgit.AUTH_MODE: oidc).
#
# Run on the swarm node that runs the Forgejo task, by the `script` module of
# the skgit deploy playbook. Idempotent: `forgejo admin auth list` finds the
# source by NAME; found, it is updated in place (`update-oauth --id N`),
# absent, it is created (`add-oauth`). It never deletes anything and never
# touches a source of another name, so an auth source the framework did not
# create is left alone (a same-named source of another TYPE is refused, not
# overwritten).
#
# Inputs (environment, set by the playbook):
#   FORGEJO_SERVICE   swarm service name, e.g. skgit-prod_forgejo
#   SOURCE_NAME       auth source name (also in the callback URL)
#   CLIENT_ID         OIDC client id (the Authentik application slug)
#   OIDC_CLIENT_SECRET  client secret, handed to the container by name
#                     (`docker exec -e VAR`), so it is in no docker argv
#   DISCOVERY_URL     .../.well-known/openid-configuration
# Output: one line "OIDC_SOURCE_CREATED ..." or "OIDC_SOURCE_UPDATED ..." on
# success; on failure a message with the secret redacted and exit 1. The
# secret is never printed.
set -u -o pipefail

fail() {
  local msg=$1
  msg=${msg//"${OIDC_CLIENT_SECRET:-@@none@@}"/<redacted>}
  echo "$msg"
  exit 1
}

for v in FORGEJO_SERVICE SOURCE_NAME CLIENT_ID OIDC_CLIENT_SECRET DISCOVERY_URL; do
  [ -n "${!v:-}" ] || fail "oidc-auth-source: $v is empty"
done

CID=$(docker ps -q -f "name=${FORGEJO_SERVICE}." | head -n1)
[ -n "$CID" ] || fail "no running ${FORGEJO_SERVICE} container found on this node"

if ! LIST=$(docker exec "$CID" forgejo admin auth list 2>&1); then
  fail "forgejo admin auth list failed: $(printf '%s' "$LIST" | tail -n 3 | tr '\n' ' ')"
fi

# Find the source by name. The name may hold spaces, so it is matched as a
# prefix of the row after the id, and the rest of the row must start with an
# auth type word (this keeps "SKStack SSO" from matching "SKStack SSO 2").
# Prints "<id> <type>" for the first match.
FOUND=$(printf '%s\n' "$LIST" | awk -v n="$SOURCE_NAME" '
  NR > 1 && match($0, /^[ \t]*[0-9]+[ \t]+/) {
    id = $0; sub(/^[ \t]*/, "", id); sub(/[ \t].*$/, "", id)
    rest = substr($0, RLENGTH + 1)
    if (substr(rest, 1, length(n)) != n) next
    tail = substr(rest, length(n) + 1)
    if (tail !~ /^[ \t]+(OAuth2|PAM|LDAP|SMTP|SSPI)/) next
    sub(/^[ \t]+/, "", tail); sub(/[ \t].*$/, "", tail)
    print id, tail
    exit
  }')

COMMON=(--name "$SOURCE_NAME" --provider openidConnect --key "$CLIENT_ID"
        --auto-discover-url "$DISCOVERY_URL"
        --scopes openid --scopes email --scopes profile)

# The secret is expanded inside the container from its environment.
run_forgejo() {
  docker exec -e OIDC_CLIENT_SECRET "$CID" sh -c \
    'exec forgejo admin auth "$@" --secret "$OIDC_CLIENT_SECRET"' sh "$@" 2>&1
}

if [ -n "$FOUND" ]; then
  ID=${FOUND%% *}
  TYPE=${FOUND#* }
  [ "$TYPE" = "OAuth2" ] || fail "an auth source named '$SOURCE_NAME' (id $ID) exists but is of type $TYPE, not OAuth2: refusing to touch it (rename it or pick another skgit.OIDC_SOURCE_NAME)"
  if OUT=$(run_forgejo update-oauth --id "$ID" "${COMMON[@]}"); then
    echo "OIDC_SOURCE_UPDATED id=$ID name=$SOURCE_NAME"
    exit 0
  fi
  fail "forgejo admin auth update-oauth failed: $(printf '%s' "$OUT" | tail -n 3 | tr '\n' ' ')"
fi

if OUT=$(run_forgejo add-oauth "${COMMON[@]}"); then
  echo "OIDC_SOURCE_CREATED name=$SOURCE_NAME"
  exit 0
fi
fail "forgejo admin auth add-oauth failed: $(printf '%s' "$OUT" | tail -n 3 | tr '\n' ' ')"
