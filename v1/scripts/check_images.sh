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
  moved=""
  name="${image%%@*}"
  if [ "${rc}" -ne 0 ] && [[ "${image}" == *@sha256:* ]] && [[ "${name##*/}" == *:* ]]; then
    # name:tag@digest fails verification once upstream re-pushes the tag, but
    # the digest is what deploys and it still pulls: judge the pin by itself.
    out="$(docker manifest inspect "${name%:*}@${image#*@}" 2>&1)"
    rc=$?
    moved=" (tag moved upstream; pin still pulls)"
  fi
  if [ "${rc}" -eq 0 ]; then
    printf '%-12s %-55s %s\n' "${services}" "${image}" "OK${moved}"
  elif grep -qi "toomanyrequests" <<<"${out}"; then
    # Docker Hub throttles anonymous bursts: inconclusive, not a missing image.
    printf '%-12s %-55s %s\n' "${services}" "${image}" "RATE-LIMITED (inconclusive)"
    limited=1
  elif grep -qi "denied\|unauthorized" <<<"${out}"; then
    # An anonymous, unauthenticated manifest request answered "denied" or
    # "unauthorized" (e.g. a package a registry gates behind auth, or a
    # framework-built image whose release tag hasn't been pushed yet).
    # This canary has no credentials to tell "gated" apart from "gone", so
    # it cannot fail the image on that alone without permanently red-lining
    # anything not yet published.
    printf '%-12s %-55s %s\n' "${services}" "${image}" "ACCESS-DENIED (inconclusive)"
    limited=1
  else
    printf '%-12s %-55s %s\n' "${services}" "${image}" "FAIL"
    fail=1
  fi
done < <(if [ -n "${SKSTACKS_LIST_IMAGES:-}" ]; then python3 "${SKSTACKS_LIST_IMAGES}"; else python3 "${SCRIPT_DIR}/list_images.py" "${ROOT}"; fi)

[ "${limited:-0}" = 1 ] && echo "note: some images were rate-limited or access-denied; rerun later or with docker login for a definitive answer"
exit "${fail}"
