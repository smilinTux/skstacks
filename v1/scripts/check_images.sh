#!/usr/bin/env bash
# Image-availability canary: every image a published v1 service references
# must still be anonymously pullable. Motivation: MinIO's images silently
# stopped being servable (the quay repo was deleted, Docker Hub went
# auth-only) and a framework service pointing at an unpullable image is
# broken for every instance built from it; an earlier service also
# referenced a Docker Hub name that never existed. This only checks the
# manifest (no pull, no container), so it is cheap enough to run weekly.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="${1:-$SCRIPT_DIR/../..}"

# `docker manifest inspect` is stable in modern docker but some builds still
# gate it behind the CLI experimental flag; harmless to set either way.
export DOCKER_CLI_EXPERIMENTAL=enabled

fail=0
printf '%-12s %-55s %s\n' "SERVICE" "IMAGE" "STATUS"
printf '%-12s %-55s %s\n' "-------" "-----" "------"

while IFS=$'\t' read -r services image; do
  [ -z "${services}" ] && continue
  if [[ "${image}" == *"<unresolved>"* ]]; then
    printf '%-12s %-55s %s\n' "${services}" "${image}" "SKIP (no default tag)"
    continue
  fi
  out="$(docker manifest inspect "${image}" 2>&1)"
  rc=$?
  if [ "${rc}" -eq 0 ]; then
    printf '%-12s %-55s %s\n' "${services}" "${image}" "OK"
  elif grep -qi "toomanyrequests" <<<"${out}"; then
    # Docker Hub throttles anonymous bursts: inconclusive, not a missing image.
    printf '%-12s %-55s %s\n' "${services}" "${image}" "RATE-LIMITED (inconclusive)"
    limited=1
  else
    printf '%-12s %-55s %s\n' "${services}" "${image}" "FAIL"
    fail=1
  fi
done < <(if [ -n "${SKSTACKS_LIST_IMAGES:-}" ]; then python3 "${SKSTACKS_LIST_IMAGES}"; else python3 "${SCRIPT_DIR}/list_images.py" "${ROOT}"; fi)

[ "${limited:-0}" = 1 ] && echo "note: some images were rate-limited; rerun later or with docker login for a definitive answer"
exit "${fail}"
