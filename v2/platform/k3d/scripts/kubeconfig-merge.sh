#!/usr/bin/env bash
# Merge k3d cluster kubeconfig into ~/.kube/config and switch context.
# Safe to run multiple times (idempotent — k3d merges, not overwrites).
#
# Usage: K3D_CLUSTER_NAME=my-cluster ./scripts/kubeconfig-merge.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(dirname "${SCRIPT_DIR}")"

# Load .env if present
for envfile in "${PLATFORM_DIR}/../../.env" "${PLATFORM_DIR}/.env"; do
  if [[ -f "${envfile}" ]]; then
    # shellcheck source=/dev/null
    set -a; source "${envfile}"; set +a
  fi
done

K3D_CLUSTER_NAME="${K3D_CLUSTER_NAME:-skstacks}"

echo "==> Merging kubeconfig for 'k3d-${K3D_CLUSTER_NAME}' into ~/.kube/config"
k3d kubeconfig merge "${K3D_CLUSTER_NAME}" --kubeconfig-merge-default

echo "==> Switching context to k3d-${K3D_CLUSTER_NAME}"
kubectl config use-context "k3d-${K3D_CLUSTER_NAME}"

echo "==> Active context: $(kubectl config current-context)"
