#!/usr/bin/env bash
# =============================================================================
# skstacks/v2/secrets/hashicorp_vault/bootstrap.sh
#
# Idempotent Vault Raft 3-node bootstrap
# =============================================================================
#
# Responsibilities (in order):
#   1.  Detect Vault initialization status — skip init if already done
#   2.  vault operator init  (5 shares, threshold 3)  →  vault-init.json
#   3.  Unseal all 3 Raft nodes with 3 of the 5 Shamir keys
#   4.  Set VAULT_TOKEN from init output (if not already in env)
#   5.  Wait for Raft leader election
#   6.  Enable file audit log
#   7.  Enable KV-v2 secrets engine at secret/
#   8.  Enable JWT auth method; configure with GitHub OIDC discovery URL
#   9.  Write per-env read/write policies (skstacks-{env}-read/write)
#   10. Write per-env GitHub Actions JWT roles (github-{env}-read/write)
#   11. Enable AppRole auth; create per-env deploy roles
#   12. Print final status summary
#
# Idempotency: every step checks the current Vault state before acting.
# Dry-run:     --dry-run prints all vault/kubectl commands without executing.
#
# Usage:
#   ./bootstrap.sh [OPTIONS]
#
# Options:
#   --dry-run            Print commands without executing them
#   --mode bare|k8s      Execution mode (default: auto-detect)
#   --namespace NS       Kubernetes namespace for vault pods (default: vault)
#   --github-org ORG     GitHub org/user for OIDC role bindings
#   --envs LIST          Comma-separated env names (default: dev,staging,prod)
#   --vault-addr ADDR    Vault address (default: $VAULT_ADDR or https://127.0.0.1:8200)
#   --kv-mount MOUNT     KV-v2 mount path (default: secret)
#   --init-output FILE   Path to save vault operator init JSON (default: ./vault-init.json)
#   --help               Show this help and exit
#
# Prerequisites — bare-metal:
#   • vault CLI on PATH
#   • VAULT_ADDR env var set (or --vault-addr flag)
#   • jq installed
#
# Prerequisites — Kubernetes / RKE2:
#   • kubectl on PATH with kubeconfig pointed at the right cluster
#   • vault-0, vault-1, vault-2 pods running in --namespace
#   • jq installed
#
# Security note:
#   vault-init.json contains unseal keys AND the root token in plaintext.
#   Treat it like a private key. Store it offline in encrypted storage
#   (ansible-vault, age+USB, HSM) immediately after bootstrapping.
#   Delete it from disk once all unseal keys are distributed to key holders.
#   Revoke the root token once operational auth methods are confirmed working.
#
# KV path convention (matches backend.py):
#   secret/data/skstacks/{env}/{scope}/{key}
#
# =============================================================================
set -euo pipefail

# ── Defaults ─────────────────────────────────────────────────────────────────
DRY_RUN=false
MODE=""                             # "bare" | "k8s" — auto-detected if empty
K8S_NAMESPACE="vault"
GITHUB_ORG=""
ENVS="dev,staging,prod"
VAULT_ADDR_ARG="${VAULT_ADDR:-https://127.0.0.1:8200}"
KV_MOUNT="secret"                   # KV-v2 mount point (task spec: secret/)
JWT_MOUNT="jwt"                     # JWT auth mount path
APPROLE_MOUNT="approle"             # AppRole auth mount path
KEY_SHARES=5                        # Total Shamir shares to generate
KEY_THRESHOLD=3                     # Minimum shares needed to unseal
VAULT_NODES=("vault-0" "vault-1" "vault-2")  # Raft peer pod names (K8s) or hostnames
INIT_OUTPUT="./vault-init.json"

# ── Colours ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; YELLOW='\033[1;33m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'

# ── Logging helpers ──────────────────────────────────────────────────────────
log()  { echo -e "${GREEN}[bootstrap]${NC} $*"; }
info() { echo -e "${CYAN}[info]${NC} $*"; }
warn() { echo -e "${YELLOW}[warn]${NC} $*" >&2; }
die()  { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

usage() {
  # Print the header comment block (lines starting with #, up to first blank)
  sed -n '/^# Usage:/,/^# =====/{ /^# =====/d; s/^# \{0,1\}//; p }' "$0"
  exit 0
}

# ── Dry-run wrapper ──────────────────────────────────────────────────────────
# run_cmd: execute or echo a command depending on --dry-run flag.
# All vault / kubectl calls go through this so --dry-run is comprehensive.
run_cmd() {
  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY-RUN] $(printf '%q ' "$@")"
  else
    "$@"
  fi
}

# ── Vault command router ─────────────────────────────────────────────────────
# vault_cmd: run 'vault ...' on the leader node, routing via kubectl exec
# in k8s mode or calling vault directly in bare mode.
vault_cmd() {
  if [[ "$MODE" == "k8s" ]]; then
    run_cmd kubectl exec -n "$K8S_NAMESPACE" vault-0 -- vault "$@"
  else
    run_cmd vault "$@"
  fi
}

# vault_cmd_node: run 'vault ...' on a specific named node (used for per-node unseal).
vault_cmd_node() {
  local node="$1"; shift
  if [[ "$MODE" == "k8s" ]]; then
    run_cmd kubectl exec -n "$K8S_NAMESPACE" "$node" -- vault "$@"
  else
    # Bare-metal: override VAULT_ADDR to reach each peer by its hostname.
    # Requires DNS resolution for vault-{0,1,2}.vault-internal (or adjust hostnames).
    VAULT_ADDR="https://${node}.vault-internal:8200" run_cmd vault "$@"
  fi
}

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)         DRY_RUN=true ;;
    --mode)            MODE="$2";          shift ;;
    --namespace)       K8S_NAMESPACE="$2"; shift ;;
    --github-org)      GITHUB_ORG="$2";    shift ;;
    --envs)            ENVS="$2";          shift ;;
    --vault-addr)      VAULT_ADDR_ARG="$2"; shift ;;
    --kv-mount)        KV_MOUNT="$2";      shift ;;
    --init-output)     INIT_OUTPUT="$2";   shift ;;
    --help|-h)         usage ;;
    *) die "Unknown argument: $1  (use --help for usage)" ;;
  esac
  shift
done

export VAULT_ADDR="$VAULT_ADDR_ARG"

# ── Auto-detect execution mode ────────────────────────────────────────────────
# If MODE wasn't specified, probe for kubectl + the vault namespace to decide.
if [[ -z "$MODE" ]]; then
  if command -v kubectl &>/dev/null && kubectl get namespace "$K8S_NAMESPACE" &>/dev/null 2>&1; then
    MODE="k8s"
    log "Auto-detected mode: k8s  (namespace=${K8S_NAMESPACE})"
  else
    MODE="bare"
    log "Auto-detected mode: bare  (VAULT_ADDR=${VAULT_ADDR})"
  fi
fi

# ── Dependency checks ─────────────────────────────────────────────────────────
command -v jq &>/dev/null || die "jq not found — install it: https://stedolan.github.io/jq/"
if [[ "$MODE" == "k8s" ]]; then
  command -v kubectl &>/dev/null || die "kubectl not found"
else
  command -v vault &>/dev/null || die "vault CLI not found — see https://developer.hashicorp.com/vault/downloads"
fi

# ── Helper: read a field from the init JSON file ──────────────────────────────
read_init_field() {
  local jq_expr="$1"
  [[ -f "$INIT_OUTPUT" ]] || die "Init output not found: ${INIT_OUTPUT}"
  jq -r "$jq_expr" "$INIT_OUTPUT"
}

# ── Helper: get vault status JSON (tolerates sealed/uninitialized exit codes) ─
vault_status_json() {
  if [[ "$MODE" == "k8s" ]]; then
    kubectl exec -n "$K8S_NAMESPACE" vault-0 -- vault status -format=json 2>/dev/null || true
  else
    vault status -format=json 2>/dev/null || true
  fi
}

# ── Helper: get vault status JSON for a specific node ────────────────────────
vault_status_node_json() {
  local node="$1"
  if [[ "$MODE" == "k8s" ]]; then
    kubectl exec -n "$K8S_NAMESPACE" "$node" -- vault status -format=json 2>/dev/null || true
  else
    VAULT_ADDR="https://${node}.vault-internal:8200" \
      vault status -format=json 2>/dev/null || true
  fi
}

# =============================================================================
# STEP 1 — Check Vault initialization status
# =============================================================================
# vault status exits 0=active, 1=sealed/error, 2=uninitialized.
# We always capture JSON output and parse it so exit code ambiguity is avoided.
# =============================================================================
log "=== STEP 1: Check initialization status ==="

INITIALIZED=false
if [[ "$DRY_RUN" == "true" ]]; then
  warn "[DRY-RUN] Would check: vault status -format=json  (assuming NOT initialized)"
else
  STATUS_JSON=$(vault_status_json)
  if echo "$STATUS_JSON" | jq -e '.initialized == true' &>/dev/null; then
    INITIALIZED=true
    log "Vault is already initialized — skipping operator init."
  else
    log "Vault is NOT initialized — proceeding with init."
  fi
fi

# =============================================================================
# STEP 2 — Initialize Vault (leader / vault-0 only)
# =============================================================================
# vault operator init generates KEY_SHARES Shamir key shares. Any KEY_THRESHOLD
# of them can reconstruct the master key and unseal Vault. The root token is
# also generated here. This step only runs on the first node; remaining Raft
# peers join automatically via retry_join in the storage config.
#
# Output is written to INIT_OUTPUT as JSON. Protect this file immediately.
# =============================================================================
log "=== STEP 2: vault operator init (${KEY_SHARES} shares, threshold ${KEY_THRESHOLD}) ==="

if [[ "$INITIALIZED" == "false" ]]; then
  log "Initializing Vault (leader: vault-0) ..."

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY-RUN] vault operator init \\"
    echo "              -key-shares=${KEY_SHARES} \\"
    echo "              -key-threshold=${KEY_THRESHOLD} \\"
    echo "              -format=json > ${INIT_OUTPUT}"
    echo "  [DRY-RUN] chmod 600 ${INIT_OUTPUT}"
  else
    if [[ "$MODE" == "k8s" ]]; then
      kubectl exec -n "$K8S_NAMESPACE" vault-0 -- \
        vault operator init \
          -key-shares="${KEY_SHARES}" \
          -key-threshold="${KEY_THRESHOLD}" \
          -format=json > "$INIT_OUTPUT"
    else
      vault operator init \
        -key-shares="${KEY_SHARES}" \
        -key-threshold="${KEY_THRESHOLD}" \
        -format=json > "$INIT_OUTPUT"
    fi
    chmod 600 "$INIT_OUTPUT"
    log "Init output saved to: ${INIT_OUTPUT}"
    warn "================================================================"
    warn " CRITICAL: Move ${INIT_OUTPUT} to OFFLINE encrypted storage NOW."
    warn " DO NOT commit it to git or leave it on this machine."
    warn "================================================================"
  fi
else
  log "Already initialized — skipping."
fi

# =============================================================================
# STEP 3 — Unseal all 3 Raft nodes
# =============================================================================
# Each Vault node maintains its own sealed state; unsealing one does NOT unseal
# the others. We submit KEY_THRESHOLD keys to every node sequentially.
#
# In bare-metal mode, each node is reached via its vault-internal hostname.
# In K8s mode, we kubectl-exec into each pod.
#
# Idempotency: we query each node's sealed status first; skip if already open.
# =============================================================================
log "=== STEP 3: Unseal ${#VAULT_NODES[@]} Raft nodes (${KEY_THRESHOLD}/${KEY_SHARES} keys each) ==="

for node in "${VAULT_NODES[@]}"; do
  log "Checking unseal status: ${node}"

  if [[ "$DRY_RUN" == "true" ]]; then
    for i in $(seq 0 $((KEY_THRESHOLD - 1))); do
      echo "  [DRY-RUN] [${node}] vault operator unseal <unseal_key_${i}>"
    done
    continue
  fi

  # Query this node's sealed status
  NODE_STATUS=$(vault_status_node_json "$node")
  if echo "$NODE_STATUS" | jq -e '.sealed == false' &>/dev/null; then
    log "Node ${node} is already unsealed — skipping."
    continue
  fi

  # Submit the first KEY_THRESHOLD unseal keys (base64-encoded)
  for i in $(seq 0 $((KEY_THRESHOLD - 1))); do
    UNSEAL_KEY=$(read_init_field ".unseal_keys_b64[${i}]")
    vault_cmd_node "$node" operator unseal "$UNSEAL_KEY"
  done

  log "Node ${node} unsealed."
done

# =============================================================================
# STEP 4 — Set VAULT_TOKEN from init output
# =============================================================================
# After init, the root token is in vault-init.json. We export it so all
# subsequent vault commands authenticate automatically.
#
# NOTE: The root token should be revoked once operational auth methods (JWT /
# AppRole) are confirmed working. Use 'vault token revoke <root_token>'.
# =============================================================================
log "=== STEP 4: Authenticate with root token ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] export VAULT_TOKEN=\$(jq -r .root_token ${INIT_OUTPUT})"
else
  if [[ -z "${VAULT_TOKEN:-}" ]]; then
    if [[ -f "$INIT_OUTPUT" ]]; then
      VAULT_TOKEN=$(read_init_field ".root_token")
      export VAULT_TOKEN
      log "VAULT_TOKEN set from ${INIT_OUTPUT}"
    else
      die "VAULT_TOKEN is not set and ${INIT_OUTPUT} was not found. Cannot authenticate."
    fi
  else
    log "Using pre-existing VAULT_TOKEN from environment."
  fi
fi

# =============================================================================
# STEP 5 — Wait for Raft leader election
# =============================================================================
# After all nodes are unsealed, Raft performs leader election before the
# cluster is fully operational. We poll vault status until active_time is
# populated (indicating an elected leader), with a 60-second timeout.
# =============================================================================
log "=== STEP 5: Wait for Raft leader election (timeout: 60s) ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] Poll vault status -format=json until ha_enabled=true and active_time!=''"
else
  log "Polling for Raft leader ..."
  ELECTED=false
  for attempt in $(seq 1 12); do
    CLUSTER_STATUS=$(vault_status_json)
    # ha_enabled=true + active_time non-empty means we have a leader
    if echo "$CLUSTER_STATUS" | jq -e '.ha_enabled == true and (.active_time // "" | length > 0)' &>/dev/null; then
      ELECTED=true
      log "Raft leader elected (attempt ${attempt}/12)."
      break
    fi
    log "  Attempt ${attempt}/12 — not ready yet, sleeping 5s ..."
    sleep 5
  done
  [[ "$ELECTED" == "true" ]] || die "Raft leader not elected after 60s. Check node logs."
fi

# =============================================================================
# STEP 6 — Enable audit logging
# =============================================================================
# The file audit device writes a structured JSON log of every request and
# response (with secret values redacted). This is required for SOC2 / CIS
# compliance and for post-incident forensics. Path inside the container is
# /vault/logs/audit.log — ensure the auditStorage PVC is mounted there.
#
# Idempotency: vault audit list is checked; enable is skipped if present.
# =============================================================================
log "=== STEP 6: Enable file audit device ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] vault audit enable file file_path=/vault/logs/audit.log"
else
  AUDIT_LIST=$(vault_cmd audit list -format=json 2>/dev/null || echo "{}")
  if echo "$AUDIT_LIST" | jq -e '."file/" != null' &>/dev/null; then
    log "Audit device 'file/' already enabled — skipping."
  else
    vault_cmd audit enable file file_path=/vault/logs/audit.log
    log "Audit logging enabled → /vault/logs/audit.log"
  fi
fi

# =============================================================================
# STEP 7 — Enable KV-v2 secrets engine at ${KV_MOUNT}/
# =============================================================================
# KV-v2 adds versioning, soft-delete, check-and-set (CAS), and per-version
# metadata compared to KV-v1. All SKStacks secrets are stored under:
#   ${KV_MOUNT}/data/skstacks/{env}/{scope}/{key}
#
# This matches the path convention in backend.py and the policy HCL files.
#
# Idempotency: vault secrets list is checked first; mount is skipped if present.
# =============================================================================
log "=== STEP 7: Enable KV-v2 at ${KV_MOUNT}/ ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] vault secrets enable -path=${KV_MOUNT} -version=2 kv"
else
  SECRETS_LIST=$(vault_cmd secrets list -format=json 2>/dev/null || echo "{}")
  if echo "$SECRETS_LIST" | jq -e ".\"${KV_MOUNT}/\" != null" &>/dev/null; then
    log "KV engine already mounted at ${KV_MOUNT}/ — skipping."
  else
    vault_cmd secrets enable -path="${KV_MOUNT}" -version=2 kv
    log "KV-v2 enabled at ${KV_MOUNT}/."
  fi
fi

# =============================================================================
# STEP 8 — Enable JWT auth method and configure GitHub OIDC
# =============================================================================
# GitHub Actions emits short-lived OIDC JWTs signed by:
#   https://token.actions.githubusercontent.com
#
# We configure Vault's JWT auth method to validate those tokens using GitHub's
# OIDC discovery endpoint, which auto-fetches the JWKS (public keys).
#
# In CI workflows, the job exchanges its ACTIONS_ID_TOKEN_REQUEST_TOKEN for a
# Vault token by calling:
#   vault write auth/${JWT_MOUNT}/login role=github-{env}-write jwt=<token>
#
# The config write is NOT idempotent-safe to skip (the config may drift), so
# we always write it even when the mount already exists.
#
# Idempotency of enable: checked via auth list; only the enable is skipped.
# =============================================================================
log "=== STEP 8: Enable JWT auth and configure GitHub OIDC ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] vault auth enable -path=${JWT_MOUNT} jwt"
  echo "  [DRY-RUN] vault write auth/${JWT_MOUNT}/config \\"
  echo "              oidc_discovery_url=https://token.actions.githubusercontent.com \\"
  echo "              bound_issuer=https://token.actions.githubusercontent.com"
else
  AUTH_LIST=$(vault_cmd auth list -format=json 2>/dev/null || echo "{}")
  if echo "$AUTH_LIST" | jq -e ".\"${JWT_MOUNT}/\" != null" &>/dev/null; then
    log "JWT auth already enabled at ${JWT_MOUNT}/ — skipping enable."
  else
    vault_cmd auth enable -path="${JWT_MOUNT}" jwt
    log "JWT auth method enabled at ${JWT_MOUNT}/."
  fi

  # Configure with GitHub's OIDC discovery URL.
  # Using oidc_discovery_url instead of a static jwks_url means Vault
  # auto-refreshes signing keys when GitHub rotates them.
  vault_cmd write "auth/${JWT_MOUNT}/config" \
    oidc_discovery_url="https://token.actions.githubusercontent.com" \
    bound_issuer="https://token.actions.githubusercontent.com"
  log "JWT auth configured for GitHub OIDC."
fi

# =============================================================================
# STEP 9 — Write per-environment read/write policies
# =============================================================================
# For each environment we create two policies:
#
#   skstacks-{env}-read   — read-only; for deploy runners, ESO, Vault Agent
#   skstacks-{env}-write  — full CRUD; for CI pipelines that provision secrets
#
# Policies are intentionally scoped to a single env (principle of least
# privilege). Cross-env reads are blocked unless explicitly granted.
#
# vault policy write is naturally idempotent — it overwrites if the policy
# already exists, which keeps the desired state in sync on re-runs.
# =============================================================================
log "=== STEP 9: Write per-environment policies ==="

IFS=',' read -ra ENV_LIST <<< "$ENVS"

for env in "${ENV_LIST[@]}"; do
  env="$(echo "$env" | tr -d '[:space:]')"  # strip stray whitespace
  log "Writing policies for env: ${env}"

  # ── Read policy ─────────────────────────────────────────────────────────────
  READ_POLICY="skstacks-${env}-read"
  READ_HCL=$(cat <<POLICY
# Policy: ${READ_POLICY}
# Read-only access to KV-v2 secrets for environment: ${env}
# Used by: Ansible deploy runner, K8s External Secrets Operator, Vault Agent

# Read secret data for this environment (all scopes)
path "${KV_MOUNT}/data/skstacks/${env}/*" {
  capabilities = ["read"]
}

# List keys and read metadata (required for ESO sync and template rendering)
path "${KV_MOUNT}/metadata/skstacks/${env}/*" {
  capabilities = ["list", "read"]
}

# Allow the token to renew itself (prevents unexpected expiry mid-deploy)
path "auth/token/renew-self" {
  capabilities = ["update"]
}

# Allow the token to look up its own metadata (health check probes)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
POLICY
  )

  # ── Write policy ─────────────────────────────────────────────────────────────
  WRITE_POLICY="skstacks-${env}-write"
  WRITE_HCL=$(cat <<POLICY
# Policy: ${WRITE_POLICY}
# Read + write access to KV-v2 secrets for environment: ${env}
# Used by: CI/CD pipelines (GitHub Actions, Forgejo) that provision secrets

# Full CRUD on secret data — create, update, read, delete latest version
path "${KV_MOUNT}/data/skstacks/${env}/*" {
  capabilities = ["create", "read", "update", "delete"]
}

# Manage metadata: list keys, read version history, delete all versions
path "${KV_MOUNT}/metadata/skstacks/${env}/*" {
  capabilities = ["list", "read", "delete"]
}

# Soft-delete specific versions (marks deleted but retains data)
path "${KV_MOUNT}/delete/skstacks/${env}/*" {
  capabilities = ["update"]
}

# Permanently destroy specific versions (data is removed)
path "${KV_MOUNT}/destroy/skstacks/${env}/*" {
  capabilities = ["update"]
}

# Restore a soft-deleted version
path "${KV_MOUNT}/undelete/skstacks/${env}/*" {
  capabilities = ["update"]
}

# Token self-management
path "auth/token/renew-self" {
  capabilities = ["update"]
}
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
POLICY
  )

  if [[ "$DRY_RUN" == "true" ]]; then
    echo "  [DRY-RUN] vault policy write ${READ_POLICY} - <<HCL"
    echo "$READ_HCL" | sed 's/^/    /'
    echo "  HCL"
    echo "  [DRY-RUN] vault policy write ${WRITE_POLICY} - <<HCL"
    echo "$WRITE_HCL" | sed 's/^/    /'
    echo "  HCL"
  else
    echo "$READ_HCL"  | vault_cmd policy write "$READ_POLICY"  -
    log "Policy written: ${READ_POLICY}"
    echo "$WRITE_HCL" | vault_cmd policy write "$WRITE_POLICY" -
    log "Policy written: ${WRITE_POLICY}"
  fi
done

# =============================================================================
# STEP 10 — Write GitHub Actions JWT roles per environment
# =============================================================================
# Each JWT role maps a set of GitHub OIDC claims to a Vault policy.
# Roles are intentionally split by intent (read vs write) and env.
#
# GitHub OIDC claim reference:
#   sub  = "repo:{org}/{repo}:environment:{env}"   (environment-gated)
#         or "repo:{org}/{repo}:ref:refs/heads/main"  (branch-gated)
#   aud  = "https://github.com/{org}"              (set in the workflow)
#   iss  = "https://token.actions.githubusercontent.com"
#
# Role token_ttl is kept at 15m — GitHub Actions jobs finish well within that
# window, and the short TTL limits the blast radius of a leaked token.
#
# Idempotency: vault write on a role is a full overwrite (desired state wins).
#
# Skipped if --github-org is not provided.
# =============================================================================
log "=== STEP 10: Write GitHub Actions JWT roles ==="

if [[ -z "$GITHUB_ORG" ]]; then
  warn "No --github-org specified — skipping JWT role creation."
  warn "Re-run with --github-org <org> to create GitHub Actions JWT roles."
else
  for env in "${ENV_LIST[@]}"; do
    env="$(echo "$env" | tr -d '[:space:]')"
    log "Writing JWT roles for env: ${env}"

    # ── Read role ─────────────────────────────────────────────────────────────
    # Bound to the GitHub 'environment' claim (any repo in the org that
    # targets this environment via 'environment: {env}' in the workflow).
    # Suitable for: read-only deploy previews, monitoring, audit jobs.
    READ_ROLE="github-${env}-read"
    READ_BOUND_SUBJECT="repo:${GITHUB_ORG}/*:environment:${env}"

    # ── Write role ────────────────────────────────────────────────────────────
    # Bound to main-branch pushes only. Adjust the glob if you use release/*
    # or a different protected-branch naming convention.
    WRITE_ROLE="github-${env}-write"
    WRITE_BOUND_SUBJECT="repo:${GITHUB_ORG}/*:ref:refs/heads/main"

    if [[ "$DRY_RUN" == "true" ]]; then
      echo "  [DRY-RUN] vault write auth/${JWT_MOUNT}/role/${READ_ROLE} \\"
      echo "              role_type=jwt \\"
      echo "              bound_audiences=https://github.com/${GITHUB_ORG} \\"
      echo "              bound_claims_type=glob \\"
      echo "              bound_claims=\"sub=${READ_BOUND_SUBJECT}\" \\"
      echo "              user_claim=actor \\"
      echo "              token_policies=skstacks-${env}-read \\"
      echo "              token_ttl=15m token_max_ttl=30m"
      echo "  [DRY-RUN] vault write auth/${JWT_MOUNT}/role/${WRITE_ROLE} \\"
      echo "              role_type=jwt \\"
      echo "              bound_audiences=https://github.com/${GITHUB_ORG} \\"
      echo "              bound_claims_type=glob \\"
      echo "              bound_claims=\"sub=${WRITE_BOUND_SUBJECT}\" \\"
      echo "              user_claim=actor \\"
      echo "              token_policies=skstacks-${env}-write \\"
      echo "              token_ttl=15m token_max_ttl=30m"
      continue
    fi

    # Read role: any workflow targeting this environment can read secrets
    vault_cmd write "auth/${JWT_MOUNT}/role/${READ_ROLE}" \
      role_type=jwt \
      bound_audiences="https://github.com/${GITHUB_ORG}" \
      bound_claims_type=glob \
      bound_claims="sub=${READ_BOUND_SUBJECT}" \
      user_claim=actor \
      token_policies="skstacks-${env}-read" \
      token_ttl=15m \
      token_max_ttl=30m
    log "JWT role written: ${READ_ROLE}"

    # Write role: only main-branch CD pipelines can write secrets
    vault_cmd write "auth/${JWT_MOUNT}/role/${WRITE_ROLE}" \
      role_type=jwt \
      bound_audiences="https://github.com/${GITHUB_ORG}" \
      bound_claims_type=glob \
      bound_claims="sub=${WRITE_BOUND_SUBJECT}" \
      user_claim=actor \
      token_policies="skstacks-${env}-write" \
      token_ttl=15m \
      token_max_ttl=30m
    log "JWT role written: ${WRITE_ROLE}"
  done
fi

# =============================================================================
# STEP 11 — Enable AppRole auth and create per-env deploy roles
# =============================================================================
# AppRole is used by non-GitHub automation: Ansible deploy runners, Vault Agent
# sidecars, and scripts that can't obtain a GitHub OIDC token.
#
# Each env gets a 'skstacks-{env}-deploy' role with read-only access and a
# 7-day secret_id_ttl so CI can rotate the secret_id weekly via a scheduled job.
#
# How to obtain credentials for a role:
#   vault read  auth/approle/role/skstacks-prod-deploy/role-id
#   vault write auth/approle/role/skstacks-prod-deploy/secret-id  (wrapped, preferred)
#
# Idempotency: auth enable is skipped if already present; vault write on a role
# is a full overwrite (safe to re-run).
# =============================================================================
log "=== STEP 11: Enable AppRole auth and create deploy roles ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] vault auth enable -path=${APPROLE_MOUNT} approle"
  for env in "${ENV_LIST[@]}"; do
    env="$(echo "$env" | tr -d '[:space:]')"
    echo "  [DRY-RUN] vault write auth/${APPROLE_MOUNT}/role/skstacks-${env}-deploy \\"
    echo "              token_policies=skstacks-${env}-read \\"
    echo "              token_ttl=1h token_max_ttl=4h secret_id_ttl=168h"
  done
else
  AUTH_LIST=$(vault_cmd auth list -format=json 2>/dev/null || echo "{}")
  if echo "$AUTH_LIST" | jq -e ".\"${APPROLE_MOUNT}/\" != null" &>/dev/null; then
    log "AppRole auth already enabled at ${APPROLE_MOUNT}/ — skipping enable."
  else
    vault_cmd auth enable -path="${APPROLE_MOUNT}" approle
    log "AppRole auth enabled at ${APPROLE_MOUNT}/."
  fi

  for env in "${ENV_LIST[@]}"; do
    env="$(echo "$env" | tr -d '[:space:]')"
    ROLE_NAME="skstacks-${env}-deploy"
    # vault write is a full overwrite — idempotent and desired-state-safe
    vault_cmd write "auth/${APPROLE_MOUNT}/role/${ROLE_NAME}" \
      token_policies="skstacks-${env}-read" \
      token_ttl=1h \
      token_max_ttl=4h \
      secret_id_ttl=168h  # 7-day secret_id; rotate via scheduled CI job
    log "AppRole role written: ${ROLE_NAME}"
  done
fi

# =============================================================================
# STEP 12 — Summary
# =============================================================================
log "=== STEP 12: Bootstrap summary ==="

if [[ "$DRY_RUN" == "true" ]]; then
  echo "  [DRY-RUN] vault status"
  echo "  [DRY-RUN] vault secrets list"
  echo "  [DRY-RUN] vault auth list"
  echo "  [DRY-RUN] vault policy list"
else
  echo ""
  info "--- Vault status ---"
  vault_cmd status || true
  echo ""
  info "--- Secrets engines ---"
  vault_cmd secrets list
  echo ""
  info "--- Auth methods ---"
  vault_cmd auth list
  echo ""
  info "--- Policies ---"
  vault_cmd policy list
fi

log ""
log "Bootstrap complete."
log ""

# Post-bootstrap checklist printed unconditionally (DRY_RUN or not)
if [[ -f "$INIT_OUTPUT" && "$DRY_RUN" != "true" ]]; then
  warn "================================================================"
  warn " CRITICAL: ${INIT_OUTPUT} contains root token + unseal keys."
  warn " 1. Transfer to OFFLINE encrypted storage (ansible-vault / age)."
  warn " 2. Distribute unseal keys to ${KEY_THRESHOLD} separate key holders."
  warn " 3. Delete this file from the bootstrap machine."
  warn " 4. Revoke root token once AppRole / OIDC auth is verified:"
  warn "      vault token revoke \$(jq -r .root_token ${INIT_OUTPUT})"
  warn "================================================================"
fi

log "Next steps:"
log "  • Add Kubernetes auth for ESO (run inside the cluster):"
log "      vault auth enable kubernetes"
log "      vault write auth/kubernetes/config \\"
log "        kubernetes_host=https://\${KUBERNETES_SERVICE_HOST}:443 \\"
log "        token_reviewer_jwt=@/var/run/secrets/kubernetes.io/serviceaccount/token \\"
log "        kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
log "      vault write auth/kubernetes/role/skstacks-eso \\"
log "        bound_service_account_names=external-secrets \\"
log "        bound_service_account_namespaces=external-secrets \\"
log "        token_policies=skstacks-prod-read \\"
log "        token_ttl=10m"
log ""
log "  • In GitHub Actions workflows, login via:"
log "      - uses: hashicorp/vault-action@v3"
log "        with:"
log "          url: \${{ vars.VAULT_ADDR }}"
log "          method: jwt"
log "          path: ${JWT_MOUNT}"
log "          role: github-prod-write"
log "          jwtGithubAudience: https://github.com/${GITHUB_ORG:-YOUR_ORG}"
log ""
log "  • Retrieve AppRole credentials for Ansible:"
log "      vault read  auth/${APPROLE_MOUNT}/role/skstacks-prod-deploy/role-id"
log "      vault write -wrap-ttl=5m auth/${APPROLE_MOUNT}/role/skstacks-prod-deploy/secret-id"
