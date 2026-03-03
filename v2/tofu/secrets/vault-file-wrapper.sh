#!/usr/bin/env bash
# SKStacks v2 — vault-file → OpenTofu environment wrapper
#
# Decrypts the tofu scope vault and exports cloud provider credentials
# as TF_VAR_* environment variables before running tofu.
#
# Usage:
#   eval $(./tofu/secrets/vault-file-wrapper.sh --env prod)
#   tofu apply
#
#   # Or: wrap the tofu command directly
#   ./tofu/secrets/vault-file-wrapper.sh --env prod -- tofu apply
#
# Required:
#   - ansible-vault installed (pip install ansible-core)
#   - Vault file at: ~/.skstacks/vaults/{env}/tofu-{env}_vault.yml
#   - Password file at: ~/.vault_pass_env/.tofu_{env}_vault_pass
#     (or fallback: ~/.vault_pass_env/.{env}_vault_pass)

set -euo pipefail

ENV="prod"
VAULT_DIR="${SKSTACKS_VAULT_DIR:-$HOME/.skstacks/vaults}"
PASS_DIR="${SKSTACKS_VAULT_PASS_DIR:-$HOME/.vault_pass_env}"
REMAINDER=()

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) ENV="$2"; shift 2 ;;
    --) shift; REMAINDER=("$@"); break ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ── Find vault and password files ─────────────────────────────────────────────
VAULT_FILE="${VAULT_DIR}/${ENV}/tofu-${ENV}_vault.yml"
PASS_FILE="${PASS_DIR}/.tofu_${ENV}_vault_pass"
[[ -f "$PASS_FILE" ]] || PASS_FILE="${PASS_DIR}/.${ENV}_vault_pass"

if [[ ! -f "$VAULT_FILE" ]]; then
  echo "ERROR: Vault file not found: $VAULT_FILE" >&2
  echo "Create it with:" >&2
  echo "  ansible-vault create --vault-password-file $PASS_FILE $VAULT_FILE" >&2
  exit 1
fi

if [[ ! -f "$PASS_FILE" ]]; then
  echo "ERROR: Vault password file not found: $PASS_FILE" >&2
  exit 1
fi

# ── Decrypt and export ────────────────────────────────────────────────────────
DECRYPTED=$(ansible-vault decrypt \
  --vault-password-file "$PASS_FILE" \
  --output=- "$VAULT_FILE" 2>/dev/null)

# Parse YAML key: value pairs and export as TF_VAR_*
while IFS=': ' read -r key value; do
  [[ -z "$key" || "$key" == '#'* ]] && continue
  # Strip vault_ prefix convention
  tf_key="${key#vault_tofu_}"
  # Export as TF_VAR_ (OpenTofu reads these automatically)
  export "TF_VAR_${tf_key}=${value//\"/}"
  echo "export TF_VAR_${tf_key}=<redacted>"
done <<< "$DECRYPTED"

echo "# Vault-file secrets exported for env=${ENV}" >&2

# ── Run remainder command if given ────────────────────────────────────────────
if [[ ${#REMAINDER[@]} -gt 0 ]]; then
  exec "${REMAINDER[@]}"
fi
