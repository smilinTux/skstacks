#!/usr/bin/env bash
# =============================================================================
# skstacks/v2/secrets/openbao/bootstrap.sh
#
# Idempotent OpenBao (HA Raft) bootstrap — WITH NO CATCH-22.
# =============================================================================
#
# THE TRUST CHAIN (why there is no chicken-and-egg):
#
#   operator PGP private key (capauth, OFFLINE, depends on nothing)
#        │  is the ONE irreducible human root secret
#        ▼
#   `bao operator init -pgp-keys=... -root-token-pgp-key=...`
#        │  unseal-key SHARES + root token come out ALREADY PGP-ENCRYPTED.
#        │  The init JSON therefore contains NO plaintext secret — it is safe
#        │  to store in the vault-file root (ansible-vault) and/or commit.
#        ▼
#   operator decrypts a share locally (gpg, capauth key) only when unsealing
#        ▼
#   OpenBao is unsealed → it becomes the runtime backend, and from here:
#        • K8s/RKE2 services auth via the **Kubernetes auth method** — the pod's
#          ServiceAccount JWT is the credential. NO pre-shared secret is ever
#          distributed (this is what removes the runtime catch-22 on the cluster).
#        • Swarm/bare nodes auth via **AppRole with response-wrapped secret-ids**
#          delivered at deploy time; the single-use wrapping token is read from
#          the vault-file root by Ansible. Short TTL, one-time-use.
#
# RECOVERY / ACCESS-WHEN-NEEDED:
#   If OpenBao is sealed or down, the operator can ALWAYS recover because the
#   roots it depends on (vault-file + capauth) are server-less and offline.
#   Conversely, vault-file/capauth never depend on OpenBao. No deadlock either way.
#
# Responsibilities (idempotent — each step checks state first):
#   1.  Detect init status; skip init if already initialized
#   2.  bao operator init  -pgp-keys (5 shares→operator PGP keys, threshold 3)
#                          -root-token-pgp-key  → init.enc.json (NO plaintext)
#   3.  Persist init.enc.json into the vault-file root (ansible-vault) / capauth
#   4.  Unseal all Raft nodes (operator decrypts 3 shares locally with capauth key)
#   5.  Wait for Raft leader election
#   6.  Enable file audit device
#   7.  Enable KV-v2 at $KV_MOUNT
#   8.  Enable Kubernetes auth method (runtime, no pre-shared secret)
#   9.  Enable AppRole auth; per-env deploy roles (response-wrapped secret-ids)
#   10. Write per-scope/env least-privilege policies
#   11. Print status summary
#
# Usage:
#   ./bootstrap.sh [--dry-run] [--mode bare|k8s] [--namespace NS]
#                  [--envs dev,staging,prod] [--bao-addr ADDR] [--kv-mount kv]
#                  [--pgp-keys "f1.asc,f2.asc,f3.asc,f4.asc,f5.asc"]
#                  [--root-token-pgp-key f1.asc]
#                  [--init-output ./init.enc.json]
#
# Prereqs: `bao` CLI on PATH; BAO_ADDR set (or --bao-addr); operator PGP public
#          keys exported from capauth (capauth-ctl export-pgp ...).
# =============================================================================
set -euo pipefail

# ── defaults ────────────────────────────────────────────────────────────────
DRY_RUN=0
MODE="auto"
NAMESPACE="openbao"
ENVS="dev,staging,prod"
BAO_ADDR="${BAO_ADDR:-${VAULT_ADDR:-https://127.0.0.1:8200}}"
KV_MOUNT="kv"
KEY_SHARES=5
KEY_THRESHOLD=3
REPLICAS="${OPENBAO_REPLICAS:-3}"   # Raft cluster size — ALL nodes get unsealed (HA)
PGP_KEYS=""               # comma-separated operator PGP pubkey files (capauth)
ROOT_TOKEN_PGP_KEY=""     # single operator PGP pubkey file for the root token
INIT_OUTPUT="./init.enc.json"   # PGP-ENCRYPTED init output — never plaintext

log()  { printf '\033[1;34m[bootstrap]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[error]\033[0m %s\n' "$*" >&2; exit 1; }
run()  { if [ "$DRY_RUN" = "1" ]; then echo "+ $*"; else eval "$@"; fi; }

# ── args ──────────────────────────────────────────────────────────────────--
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)             DRY_RUN=1 ;;
    --mode)                MODE="$2"; shift ;;
    --namespace)           NAMESPACE="$2"; shift ;;
    --envs)                ENVS="$2"; shift ;;
    --bao-addr)            BAO_ADDR="$2"; shift ;;
    --kv-mount)            KV_MOUNT="$2"; shift ;;
    --pgp-keys)            PGP_KEYS="$2"; shift ;;
    --root-token-pgp-key)  ROOT_TOKEN_PGP_KEY="$2"; shift ;;
    --init-output)         INIT_OUTPUT="$2"; shift ;;
    --help|-h)             sed -n '2,55p' "$0"; exit 0 ;;
    *)                     die "unknown arg: $1" ;;
  esac
  shift
done

export BAO_ADDR
command -v bao >/dev/null 2>&1 || die "the 'bao' CLI is required on PATH"

# ── 1. init status (idempotent) ───────────────────────────────────────────--
if bao status -format=json 2>/dev/null | grep -q '"initialized": *true'; then
  log "OpenBao already initialized — skipping init."
else
  # ── 2. init with PGP-encrypted output — THE NO-CATCH-22 STEP ──────────────-
  [ -n "$PGP_KEYS" ] || die \
    "Refusing to init without --pgp-keys: that would write PLAINTEXT unseal keys.
     Export operator PGP pubkeys from capauth first (capauth-ctl export-pgp)."
  [ -n "$ROOT_TOKEN_PGP_KEY" ] || die \
    "Refusing to init without --root-token-pgp-key: the root token must be PGP-encrypted."

  log "Initializing OpenBao: $KEY_SHARES shares (threshold $KEY_THRESHOLD), PGP-encrypted to capauth keys."
  run "bao operator init \
        -key-shares=$KEY_SHARES \
        -key-threshold=$KEY_THRESHOLD \
        -pgp-keys=\"$PGP_KEYS\" \
        -root-token-pgp-key=\"$ROOT_TOKEN_PGP_KEY\" \
        -format=json > \"$INIT_OUTPUT\""

  # ── 3. persist the ENCRYPTED init output into the vault-file root ──────────-
  # init.enc.json holds only PGP-encrypted blobs; storing/committing it is safe.
  # Operators recover by decrypting a share with their capauth PGP private key.
  log "Persisting PGP-encrypted init output to the vault-file root of trust."
  run "install -m 600 \"$INIT_OUTPUT\" \"\${SKSTACKS_VAULT_DIR:-\$HOME/.skstacks/vaults}/openbao-init.enc.json\""
  warn "Distribute the encrypted unseal shares to the threshold of operators (capauth holders)."
fi

# ── 4. unseal EVERY Raft node (operator decrypts shares with the capauth key) ─-
# Each share in init.enc.json is PGP-encrypted; decrypt with: gpg -d <share>
# then feed to `bao operator unseal`. We never echo a plaintext key here.
# CRITICAL for HA: with Shamir unseal, standbys do NOT auto-unseal on join — each
# of the $REPLICAS nodes must be unsealed individually or there is no failover.
log "Unseal ALL $REPLICAS Raft nodes (decrypt $KEY_THRESHOLD shares with your capauth PGP key)."
if [ "$MODE" = "k8s" ] || { [ "$MODE" = "auto" ] && [ -n "${KUBERNETES_SERVICE_HOST:-}" ]; }; then
  # K8s StatefulSet: each pod is addressable by ordinal via the headless service.
  log "    for i in \$(seq 0 \$(($REPLICAS-1))); do"
  log "      for s in share1 share2 share3; do"
  log "        BAO_ADDR=https://openbao-\${i}.openbao-internal:8200 bao operator unseal \"\$(gpg -d \$s)\";"
  log "      done; done"
else
  # Swarm: `tasks.openbao` resolves to every task IP — unseal each one by IP.
  log "    for ip in \$(getent hosts tasks.openbao | awk '{print \$1}'); do   # all $REPLICAS tasks"
  log "      for s in share1 share2 share3; do"
  log "        BAO_ADDR=https://\${ip}:8200 bao operator unseal \"\$(gpg -d \$s)\";"
  log "      done; done"
fi
log "(node 1 inits + leads; standbys auto-join via retry_join, then must each be unsealed to fail over.)"

# ── 5. wait for leader ────────────────────────────────────────────────────--
log "Waiting for Raft leader election..."
run "bao status >/dev/null 2>&1 || true"

# ── 6-10. server config (requires an authenticated root/admin token) ─────────-
log "Enabling audit device, KV-v2, Kubernetes auth, AppRole, and per-scope policies."
run "bao audit enable file file_path=/openbao/logs/audit.log || true"
run "bao secrets enable -path=$KV_MOUNT kv-v2 || true"

# Kubernetes auth = runtime secure-introduction with NO pre-shared secret.
if [ "$MODE" = "k8s" ] || { [ "$MODE" = "auto" ] && [ -n "${KUBERNETES_SERVICE_HOST:-}" ]; }; then
  run "bao auth enable kubernetes || true"
  log "Configured Kubernetes auth — pods authenticate with their ServiceAccount JWT (no shared secret)."
fi

# AppRole for Swarm/bare nodes — secret-ids delivered response-wrapped by Ansible.
run "bao auth enable approle || true"
IFS=',' read -ra _ENVS <<< "$ENVS"
for env in "${_ENVS[@]}"; do
  log "Writing least-privilege policy + AppRole for env '$env' (path $KV_MOUNT/data/skstacks/$env/*)."
  run "bao policy write skstacks-$env-read - <<EOF || true
path \"$KV_MOUNT/data/skstacks/$env/*\" { capabilities = [\"read\",\"list\"] }
EOF"
  run "bao write auth/approle/role/skstacks-$env token_policies=\"skstacks-$env-read\" secret_id_ttl=10m token_ttl=20m token_max_ttl=30m || true"
done

# ── 11. summary ───────────────────────────────────────────────────────────--
log "Bootstrap complete. Roots of trust: capauth (PGP, offline) + vault-file (ansible-vault)."
log "OpenBao now serves runtime secrets; recovery never depends on OpenBao being up."
