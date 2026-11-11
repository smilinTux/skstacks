#!/bin/sh
# traefik-certs-watch: regenerates a Traefik file-provider TLS config
# (tls.certificates) from traefik-certs-dumper's PEM output, on a poll
# loop, only rewriting the destination when its content actually changes.
#
# Why this exists: traefik-worker reads acme.json once at container start
# and never again (see this role's README, "Cert reload without a
# restart"). A cert renewed or newly issued by traefik-acme never reaches
# a running worker without a restart -- unless the worker learns about it
# through a mechanism Traefik itself watches for changes: the file
# provider. This sidecar is that mechanism.
#
# Why every manager node runs its own copy of this script, instead of one
# shared instance dumping straight into the NFS-backed dynamic directory:
# NFS gives no change notification to a DIFFERENT node than the one that
# wrote the file. Confirmed on the NAM estate, 2026-10-24: a write to the
# shared dynamic config directory on node A never fired node B's inotify
# watch (see CHANGELOG and docs/runbooks). A single shared writer would
# only ever wake up the node it happens to run on.
#
# Running this on every node turns that limitation to our advantage
# instead of fighting it: each node's own write to the shared directory is
# always a LOCAL write as far as that SAME node's own Traefik file
# provider watch is concerned, and a local write always fires fsnotify,
# backing store notwithstanding -- this is the same local-write/local-watch
# relationship that already makes the ACME master's own traefik-acme and
# traefik-certs-dumper see each other's local writes on acme.json fine
# today. No leader election, no cross-node signaling, no Docker socket
# access of any kind: every node redundantly computes the same output from
# the same shared, read-only /certs input and -- only when the result
# actually changes -- atomically replaces the shared destination file.
#
# Traefik v3.7's file provider has no polling fallback for environments
# where fsnotify can't see a remote write (confirmed from provider source:
# watcher.Add() is only ever called on the configured top-level directory,
# and only the process that performs a write gets notified of it). This
# script supplies the polling Traefik itself does not have, on the read
# side (checking /certs for changes), while staying inside the write
# pattern Traefik's own watch already handles correctly.
set -eu

CERTS_DIR="${CERTS_DIR:-/certs}"
DEST_FILE="${DEST_FILE:-/dynamic-out/certs-generated.yml}"
INTERVAL="${INTERVAL:-15}"
TMP_FILE="${DEST_FILE}.tmp.$$"
LAST_HASH=""

echo "traefik-certs-watch: watching ${CERTS_DIR}, writing ${DEST_FILE} every ${INTERVAL}s" >&2

while true; do
  entries=""
  for domain_dir in "${CERTS_DIR}"/*/; do
    [ -d "${domain_dir}" ] || continue
    cert="${domain_dir}certificate.crt"
    key="${domain_dir}privatekey.key"
    if [ -s "${cert}" ] && [ -s "${key}" ]; then
      entries="${entries}    - certFile: ${cert}
      keyFile: ${key}
"
    fi
  done

  {
    echo "# Managed by skfenceha's traefik-certs-watch sidecar - do not edit by hand."
    echo "# Regenerated every ${INTERVAL}s from ${CERTS_DIR} (traefik-certs-dumper output)."
    echo "tls:"
    if [ -n "${entries}" ]; then
      echo "  certificates:"
      printf '%s' "${entries}"
    else
      echo "  certificates: []"
    fi
  } > "${TMP_FILE}"

  # Only replace the destination (and so only trigger Traefik's file-watch
  # reload) when the generated content actually changed. Avoids needless
  # reload churn on every poll cycle and reduces the window where two
  # nodes both race to rewrite the same shared file at once.
  NEW_HASH=$(sha256sum "${TMP_FILE}")
  NEW_HASH="${NEW_HASH%% *}"
  if [ "${NEW_HASH}" != "${LAST_HASH}" ]; then
    mv -f "${TMP_FILE}" "${DEST_FILE}"
    LAST_HASH="${NEW_HASH}"
    echo "$(date 2>/dev/null) traefik-certs-watch: ${DEST_FILE} updated (sha256 ${NEW_HASH})" >&2
  else
    rm -f "${TMP_FILE}"
  fi

  sleep "${INTERVAL}"
done
