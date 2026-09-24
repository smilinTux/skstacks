#!/usr/bin/env bash
# Delete a k3d cluster.
# Usage: K3D_CLUSTER_NAME=my-cluster ./scripts/destroy.sh
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

echo "==> Deleting k3d cluster '${K3D_CLUSTER_NAME}'"
k3d cluster delete "${K3D_CLUSTER_NAME}"
echo "==> Cluster '${K3D_CLUSTER_NAME}' deleted."
