#!/usr/bin/env bash
# skstacks deploy harness — FULL closed loop on real Docker Swarm.
#
#   render skwhoami (swarm) → inject the secret as env → docker stack deploy → wait for
#   the replica to run → VALIDATE: container has the secret env + the service answers
#   real HTTP → docker stack rm.  Uses a unique stack name + a high host port so it
#   never collides with whatever else is on the host swarm.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
V2="${1:-$(cd "$HERE/.." && pwd)}"
STACK="skwhoami_harness"
HOSTPORT=18080
PY="${PYTHON:-python3}"

log(){ printf '\033[1;36m[swarm]\033[0m %s\n' "$*"; }
ok(){  printf '  \033[1;32m✓\033[0m %s\n'  "$*"; }
bad(){ printf '  \033[1;31m✗\033[0m %s\n'  "$*"; FAILED=1; }
FAILED=0

cleanup(){ log "removing stack…"; docker stack rm "$STACK" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# confirm we're on a swarm manager (retry once — `docker info` can blip on a busy host)
is_mgr(){ docker info 2>/dev/null | grep -q "Swarm: active" || docker node ls >/dev/null 2>&1; }
is_mgr || { sleep 3; is_mgr; } || { log "host is not a swarm manager"; exit 1; }

log "1) render skwhoami (swarm) + pin a test host port"
PYTHONPATH="$V2" "$PY" -m skrender.cli "$V2/harness/fixtures/skwhoami" --platform swarm > /tmp/skwhoami-swarm.yml
sed -i "s/- 80:80/- ${HOSTPORT}:80/" /tmp/skwhoami-swarm.yml
ok "rendered (host port ${HOSTPORT} → 80)"

log "2) deploy the stack (secret injected as env)"
export DUMMY_TOKEN="test-token-123"
docker stack deploy -c /tmp/skwhoami-swarm.yml "$STACK" --detach=true >/dev/null 2>&1 && ok "stack deployed"

log "3) wait for the replica to run"
for i in $(seq 1 40); do
  running="$(docker service ls --filter name=${STACK}_whoami --format '{{.Replicas}}' 2>/dev/null)"
  [ "$running" = "1/1" ] && break; sleep 3
done
[ "$running" = "1/1" ] && ok "service running ($running)" || bad "service never reached 1/1 ($running)"

log "4) VALIDATE THE SERVICE WORKS"
cid="$(docker ps --filter "label=com.docker.swarm.service.name=${STACK}_whoami" -q | head -1)"
if [ -n "$cid" ]; then
  # whoami is a scratch image (no shell) — read the configured env via inspect
  envval="$(docker inspect "$cid" --format '{{range .Config.Env}}{{println .}}{{end}}' | grep '^DUMMY_TOKEN=' | cut -d= -f2 | tr -d '\r')"
  [ "$envval" = "test-token-123" ] && ok "secret env in container = '$envval'" || bad "env wrong: '$envval'"
else
  bad "no running container found"
fi
# real HTTP on the published port
body="$(curl -s -m 8 "http://127.0.0.1:${HOSTPORT}/" 2>/dev/null)"
echo "$body" | grep -q "Hostname:" && ok "HTTP 200 — whoami: $(echo "$body" | grep Hostname: | tr -d '\r')" \
                                   || bad "no valid HTTP response: $(echo "$body" | head -1)"

echo
[ "$FAILED" -eq 0 ] && log "✅ SWARM DEPLOY LOOP PASSED — deployed, verified working, removing." \
                     || { log "❌ swarm loop had failures."; exit 1; }
