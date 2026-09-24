#!/usr/bin/env bash
# skstacks deploy harness — validate descriptor→deploy against a REAL Kubernetes API.
#
# Spins up a disposable k3d (k3s-in-Docker) cluster, renders the core-slice services
# with skrender, and runs `kubectl apply --dry-run=server` so the live API server
# validates every manifest (schema, required fields, CRDs). Tears the cluster down on
# exit. This catches what the unit tests can't — real K8s API rejection.
#
# Usage:  bash k3d-validate.sh [/path/to/v2]   (defaults to the dir two up from here)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
V2="${1:-$(cd "$HERE/.." && pwd)}"
CLUSTER="skstacks-harness"
PY="${PYTHON:-python3}"
# auto-discover every descriptor that declares a deploy: block
mapfile -t SERVICES < <(cd "$V2" && "$PY" -c "import glob,yaml; [print(f.replace('/app.yaml','')) for f in sorted(glob.glob('*/*/app.yaml')) if (yaml.safe_load(open(f)) or {}).get('deploy')]")

log(){ printf '\033[1;36m[harness]\033[0m %s\n' "$*"; }
ok(){  printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
bad(){ printf '  \033[1;31m✗\033[0m %s\n' "$*"; }

cleanup(){ log "tearing down k3d cluster…"; k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true; }
trap cleanup EXIT

log "creating disposable k3d cluster '$CLUSTER'…"
k3d cluster delete "$CLUSTER" >/dev/null 2>&1 || true
k3d cluster create "$CLUSTER" --no-lb --wait --timeout 180s >/dev/null
kubectl config use-context "k3d-$CLUSTER" >/dev/null
kubectl wait --for=condition=Ready node --all --timeout=120s >/dev/null
ok "cluster up: $(kubectl get nodes -o name | wc -l) node(s)"

log "installing External Secrets Operator (helm — brings the v1 CRDs)…"
helm repo add external-secrets https://charts.external-secrets.io >/dev/null 2>&1 || true
helm repo update >/dev/null 2>&1
if helm upgrade --install external-secrets external-secrets/external-secrets \
     -n external-secrets --create-namespace --set installCRDs=true --wait --timeout 180s >/dev/null 2>&1; then
  ok "ESO installed (external-secrets.io/v1 CRDs present)"
else
  bad "couldn't install ESO — ExternalSecret will validate client-side only"
fi

fails=0
for s in "${SERVICES[@]}"; do
  name="$(basename "$s")"
  log "render + validate: $name"
  kubectl create namespace "$name" --dry-run=client -o yaml | kubectl apply -f - >/dev/null 2>&1 || true
  if ! PYTHONPATH="$V2" "$PY" -m skrender.cli "$V2/$s" --platform k8s > "/tmp/${name}-k8s.yaml" 2>/tmp/${name}-err; then
    bad "$name: skrender failed: $(cat /tmp/${name}-err)"; fails=$((fails+1)); continue
  fi
  if kubectl apply --dry-run=server -f "/tmp/${name}-k8s.yaml" >/tmp/${name}-apply 2>&1; then
    ok "$name: $(grep -c configured\\\|created /tmp/${name}-apply 2>/dev/null || echo all) object(s) accepted by the API server"
    sed 's/^/      /' /tmp/${name}-apply
  else
    bad "$name: API server REJECTED a manifest:"; sed 's/^/      /' /tmp/${name}-apply; fails=$((fails+1))
  fi
done

echo
if [ "$fails" -eq 0 ]; then
  log "✅ all $(( ${#SERVICES[@]} )) services validated against a live Kubernetes API."
else
  log "❌ $fails service(s) failed validation."; exit 1
fi
