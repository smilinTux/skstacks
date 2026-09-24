#!/usr/bin/env bash
# skstacks deploy harness — FULL closed loop on a live k3d cluster.
#
#   create cluster → install ESO (real operator) → fake secret backend → render+deploy
#   skwhoami → wait for the ExternalSecret to sync + the pod to roll out → VALIDATE THE
#   SERVICE ACTUALLY WORKS (secret landed, HTTP 200, real whoami body) → destroy.
#
# Proves the whole pipeline: descriptor → skrender → ESO → Secret → pod env → live HTTP.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
V2="${1:-$(cd "$HERE/.." && pwd)}"
CLUSTER="skstacks-deploy"
NS="skwhoami"
PY="${PYTHON:-python3}"

log(){ printf '\033[1;36m[deploy]\033[0m %s\n' "$*"; }
ok(){  printf '  \033[1;32m✓\033[0m %s\n'  "$*"; }
bad(){ printf '  \033[1;31m✗\033[0m %s\n'  "$*"; FAILED=1; }
FAILED=0

cleanup(){ log "destroying cluster…"; k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

log "1) create k3d cluster '$CLUSTER'"
k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
k3d cluster create "$CLUSTER" --no-lb --wait --timeout 180s >/dev/null 2>&1
kubectl config use-context "k3d-$CLUSTER" >/dev/null
kubectl wait --for=condition=Ready node --all --timeout=120s >/dev/null && ok "cluster ready"

log "2) install External Secrets Operator (helm)"
helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
helm repo update >/dev/null 2>&1
if helm upgrade --install external-secrets external-secrets/external-secrets \
     -n external-secrets --create-namespace --set installCRDs=true --wait --timeout 180s >/dev/null 2>&1; then
  ok "ESO installed"
else
  bad "ESO install failed"; exit 1
fi

log "3) fake secret backend (ClusterSecretStore 'skstacks-backend')"
kubectl apply -f - >/dev/null <<'YAML'
apiVersion: external-secrets.io/v1
kind: ClusterSecretStore
metadata: { name: skstacks-backend }
spec:
  provider:
    fake:
      data:
        - key: "skwhoami/dummy_token"
          value: "test-token-123"
YAML
ok "secret store created"

log "4) render + deploy skwhoami"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
PYTHONPATH="$V2" "$PY" -m skrender.cli "$V2/harness/fixtures/skwhoami" --platform k8s > /tmp/skwhoami-k8s.yaml
kubectl apply -f /tmp/skwhoami-k8s.yaml >/dev/null && ok "applied $(grep -c '^kind:' /tmp/skwhoami-k8s.yaml) objects"

diagnose(){
  echo "----- DIAGNOSTICS -----"
  echo "## ExternalSecret:"; kubectl get externalsecret skwhoami -n "$NS" -o yaml 2>&1 | grep -A30 "status:" | head -30
  echo "## ClusterSecretStore:"; kubectl get clustersecretstore skstacks-backend -o yaml 2>&1 | grep -A12 "status:" | head -12
  echo "## synced secret?:"; kubectl get secret skwhoami-secrets -n "$NS" 2>&1 | head -2
  echo "## ESO controller logs (tail):"; kubectl logs -n external-secrets -l app.kubernetes.io/name=external-secrets --tail=20 2>&1 | tail -20
  echo "-----------------------"
}

log "5) wait for ExternalSecret to sync + rollout"
if kubectl wait --for=condition=Ready externalsecret/skwhoami -n "$NS" --timeout=90s >/dev/null 2>&1; then
  ok "ExternalSecret synced"
else
  bad "ExternalSecret did not sync"; diagnose
fi
kubectl rollout status deploy/skwhoami -n "$NS" --timeout=120s >/dev/null 2>&1 \
  && ok "deployment rolled out (pod Ready ⇒ envFrom secret resolved)" || bad "rollout failed"

log "6) VALIDATE THE SERVICE WORKS"
# 6a) the secret actually materialized with the right value
VAL="$(kubectl get secret skwhoami-secrets -n "$NS" -o jsonpath='{.data.DUMMY_TOKEN}' 2>/dev/null | base64 -d 2>/dev/null)"
[ "$VAL" = "test-token-123" ] && ok "secret DUMMY_TOKEN landed = '$VAL'" || bad "secret value wrong: '$VAL'"

# 6b) the service answers real HTTP from inside the cluster (DNS + routing + pod)
BODY="$(kubectl run curltest --image=curlimages/curl:8.10.1 --restart=Never -n "$NS" --rm -i --quiet \
        --command -- curl -s -m 8 "http://skwhoami.${NS}.svc.cluster.local/" 2>/dev/null)"
if echo "$BODY" | grep -q "Hostname:"; then
  ok "HTTP 200 from the service — whoami replied: $(echo "$BODY" | grep Hostname: | head -1 | tr -d '\r')"
else
  bad "service did not answer correctly. body: $(echo "$BODY" | head -2)"
fi

echo
[ "$FAILED" -eq 0 ] && log "✅ FULL DEPLOY LOOP PASSED — installed, deployed, verified working, destroying." \
                     || { log "❌ deploy loop had failures."; exit 1; }
