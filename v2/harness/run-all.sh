#!/usr/bin/env bash
# Run every skstacks deploy scenario end-to-end and report a matrix. Each scenario is
# self-contained (creates + destroys its own environment). Intended for .41.
#
#   validate  — render → API/engine schema validation (k3d dry-run + docker compose)
#   k3d       — full install→deploy→verify-working→destroy on real Kubernetes
#   swarm     — full deploy→verify-working→remove on real Docker Swarm
#   vm        — provision a fresh QEMU/KVM VM node → k3s → deploy → verify → destroy
#
# Usage: bash run-all.sh [scenario ...]   (default: validate k3d swarm ; add 'vm' for the heavy one)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SCEN=("$@"); [ ${#SCEN[@]} -eq 0 ] && SCEN=(validate k3d swarm)
declare -A RESULT

run(){ local n="$1"; shift; printf '\n\033[1;35m══════ scenario: %s ══════\033[0m\n' "$n"
       if "$@"; then RESULT[$n]=PASS; else RESULT[$n]=FAIL; fi; }

for s in "${SCEN[@]}"; do
  case "$s" in
    validate) run validate bash -c "bash '$HERE/k3d-validate.sh' && bash '$HERE/swarm-validate.sh'" ;;
    k3d)      run k3d   bash "$HERE/k3d-deploy.sh" ;;
    swarm)    run swarm bash "$HERE/swarm-deploy.sh" ;;
    vm)       run vm    bash "$HERE/vm-deploy.sh" ;;
    *) echo "unknown scenario: $s" ;;
  esac
done

printf '\n\033[1;36m══════ RESULTS ══════\033[0m\n'
fail=0
for s in "${SCEN[@]}"; do
  r="${RESULT[$s]:-SKIP}"; [ "$r" = FAIL ] && fail=1
  printf '  %-9s %s\n' "$s" "$r"
done
exit $fail
