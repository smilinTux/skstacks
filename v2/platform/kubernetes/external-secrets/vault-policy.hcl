# Vault policy — ESO secrets reader (secrets/ KV mount)
# coord task: 55d31c18
#
# Grants the External Secrets Operator service account read-only access
# to the "secrets" KV-v2 mount. Bound to the skstacks-eso Kubernetes auth role.
#
# This policy covers the "secrets" mount (secrets/{env}/{service}/{key}).
# For the legacy "kv" mount (kv/data/skstacks/*), see:
#   secrets/hashicorp_vault/policies/rke2-node-policy.hcl
#
# Apply:
#   vault policy write eso-secrets-reader \
#     platform/kubernetes/external-secrets/vault-policy.hcl
#
# Bind to the Kubernetes auth role (run after vault auth enable kubernetes):
#   vault write auth/kubernetes/role/skstacks-eso \
#     bound_service_account_names=external-secrets \
#     bound_service_account_namespaces=external-secrets \
#     token_policies="eso-secrets-reader" \
#     token_ttl=10m \
#     token_max_ttl=1h
#
# Test (from a pod with the external-secrets service account):
#   VAULT_TOKEN=$(vault write -field=token auth/kubernetes/login \
#     role=skstacks-eso \
#     jwt=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token))
#   VAULT_TOKEN=$VAULT_TOKEN vault kv get secrets/prod/postgres

# Read secret data at any path under secrets/
path "secrets/data/*" {
  capabilities = ["read"]
}

# List secret paths (needed for ExternalSecretStore discovery and bulk sync)
path "secrets/metadata/*" {
  capabilities = ["list", "read"]
}

# Deny write and delete — ESO is strictly read-only at runtime
path "secrets/data/*/delete" {
  capabilities = ["deny"]
}
path "secrets/delete/*" {
  capabilities = ["deny"]
}
path "secrets/destroy/*" {
  capabilities = ["deny"]
}
path "secrets/undelete/*" {
  capabilities = ["deny"]
}

# Allow ESO to renew its own token before expiry
path "auth/token/renew-self" {
  capabilities = ["update"]
}

# Allow ESO to look up its own token (for health checks)
path "auth/token/lookup-self" {
  capabilities = ["read"]
}
