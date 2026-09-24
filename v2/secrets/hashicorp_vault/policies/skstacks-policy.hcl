# HashiCorp Vault policy — SKStacks deploy role
#
# Apply to a role used by the Ansible deploy runner or the K8s
# External Secrets Operator to pull secrets for all service scopes.
#
# Apply:
#   vault policy write skstacks-deploy skstacks-policy.hcl
#
# Create AppRole:
#   vault auth enable approle
#   vault write auth/approle/role/skstacks-deploy \
#     token_policies="skstacks-deploy" \
#     token_ttl=1h \
#     token_max_ttl=4h \
#     secret_id_ttl=24h

# Read static secrets from all environments and scopes
path "kv/data/skstacks/*" {
  capabilities = ["read"]
}

# Allow listing keys (needed for template rendering)
path "kv/metadata/skstacks/*" {
  capabilities = ["list", "read"]
}

# Allow transit decryption (if services use Vault transit engine)
path "transit/decrypt/skstacks-*" {
  capabilities = ["update"]
}

# Allow PKI certificate issuance for services
path "pki/issue/skstacks-*" {
  capabilities = ["create", "update"]
}

# Deny write / delete (deploy role is read-only at runtime)
path "kv/data/skstacks/*/delete" {
  capabilities = ["deny"]
}
