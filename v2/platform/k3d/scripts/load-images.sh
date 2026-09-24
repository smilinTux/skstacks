#!/usr/bin/env bash
# Import one or more local Docker images into a k3d cluster.
# Useful during development to avoid pushing to a registry.
#
# Usage:   ./scripts/load-images.sh [image:tag ...]
# Example: ./scripts/load-images.sh myapp:latest nginx:alpine
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

if [[ $# -eq 0 ]]; then
  echo "Usage: $0 [image:tag ...]" >&2
  echo "Example: $0 myapp:latest nginx:alpine" >&2
  exit 1
fi

echo "==> Importing images into cluster '${K3D_CLUSTER_NAME}': $*"
k3d image import "$@" -c "${K3D_CLUSTER_NAME}"
echo "==> Images imported successfully."
