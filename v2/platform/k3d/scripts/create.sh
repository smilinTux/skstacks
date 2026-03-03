#!/usr/bin/env bash
# Create a k3d cluster using the selected config profile.
# Usage: K3D_CONFIG=staging K3D_CLUSTER_NAME=my-cluster ./scripts/create.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORM_DIR="$(dirname "${SCRIPT_DIR}")"

# Load .env if present (project root takes precedence, then platform dir)
for envfile in "${PLATFORM_DIR}/../../.env" "${PLATFORM_DIR}/.env"; do
  if [[ -f "${envfile}" ]]; then
    # shellcheck source=/dev/null
    set -a; source "${envfile}"; set +a
  fi
done

K3D_CONFIG="${K3D_CONFIG:-local}"
K3D_CLUSTER_NAME="${K3D_CLUSTER_NAME:-skstacks}"

CLUSTER_CONFIG="${PLATFORM_DIR}/clusters/${K3D_CONFIG}.yaml"

if [[ ! -f "${CLUSTER_CONFIG}" ]]; then
  echo "ERROR: cluster config not found: ${CLUSTER_CONFIG}" >&2
  echo "Available configs:" >&2
  ls "${PLATFORM_DIR}/clusters/" >&2
  exit 1
fi

# Verify prerequisites
for cmd in k3d kubectl; do
  if ! command -v "${cmd}" &>/dev/null; then
    echo "ERROR: '${cmd}' is required but not installed." >&2
    echo "  k3d:     https://k3d.io/v5.6.0/#installation" >&2
    echo "  kubectl: https://kubernetes.io/docs/tasks/tools/" >&2
    exit 1
  fi
done

echo "==> Creating k3d cluster '${K3D_CLUSTER_NAME}' (config: ${K3D_CONFIG})"
k3d cluster create \
  --config "${CLUSTER_CONFIG}" \
  --name "${K3D_CLUSTER_NAME}"

echo "==> Merging kubeconfig..."
"${SCRIPT_DIR}/kubeconfig-merge.sh"

echo ""
echo "==> Cluster '${K3D_CLUSTER_NAME}' is ready."
kubectl cluster-info --context "k3d-${K3D_CLUSTER_NAME}"
echo ""
echo "Next steps:"
echo "  kubectl apply -k platform/k3d/overlays/local/"
echo "  kubectl get nodes"
