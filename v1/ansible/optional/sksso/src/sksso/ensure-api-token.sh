#!/bin/bash
# Ensure the automation API token in sksso's vault (sksso.api_token) exists
# in Authentik, so the deploys of other services can register themselves
# with Authentik through its API (skmesh AUTH_MODE: authentik does).
#
# One-time bootstrap per cluster, idempotent: re-running re-asserts the same
# user, group membership and token key. Run it as root on a node that runs
# an sksso-<env> worker (or server) task, token on stdin:
#
#   <print sksso.api_token from the vault> | ssh <node> "sudo bash /path/ensure-api-token.sh <env>"
#
# The token never appears on a command line, in a process environment or in
# the output. It creates (or keeps) the service account
# "skstacks-automation" in the "authentik Admins" group and the API-intent,
# non-expiring token "skstacks-automation-api" with exactly that key.
set -euo pipefail

ENV_NAME="${1:?usage: ensure-api-token.sh <env>  (token on stdin)}"
CONTAINER="${SKSSO_CONTAINER:-}"
[ -n "$CONTAINER" ] || CONTAINER=$(docker ps -q --filter "name=sksso-${ENV_NAME}_worker" | head -n1)
[ -n "$CONTAINER" ] || CONTAINER=$(docker ps -q --filter "name=sksso-${ENV_NAME}_server" | head -n1)
if [ -z "$CONTAINER" ]; then
    echo "no sksso-${ENV_NAME} worker/server container on this node; run it where one is" >&2
    exit 2
fi

docker exec -i "$CONTAINER" ak shell -c '
import sys
from authentik.core.models import Group, Token, TokenIntents, User, UserTypes

key = sys.stdin.read().strip()
if len(key) < 32:
    raise SystemExit("api_token on stdin is missing or shorter than 32 characters")
user, created = User.objects.get_or_create(
    username="skstacks-automation",
    defaults={"name": "SKStacks automation", "type": UserTypes.SERVICE_ACCOUNT,
              "path": "goauthentik.io/service-accounts"},
)
if created:
    user.set_unusable_password()
    user.save()
Group.objects.get(name="authentik Admins").users.add(user)
_, tcreated = Token.objects.update_or_create(
    identifier="skstacks-automation-api",
    defaults={"user": user, "intent": TokenIntents.INTENT_API, "expiring": False, "key": key},
)
print("skstacks-automation:", "created" if created else "present",
      "| api token:", "created" if tcreated else "updated")
'
