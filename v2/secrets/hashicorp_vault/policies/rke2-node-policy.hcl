# HashiCorp Vault policy — RKE2 node / Kubernetes auth
#
# Applied to the Vault Kubernetes auth role. Each RKE2 pod with the right
# service account JWT gets this policy, allowing ESO to sync secrets.
#
# Apply:
#   vault policy write rke2-node rke2-node-policy.hcl
#
# Create K8s auth role:
#   vault write auth/kubernetes/role/skstacks-eso \
#     bound_service_account_names=external-secrets \
#     bound_service_account_namespaces=external-secrets \
#     token_policies="rke2-node" \
#     token_ttl=10m
#
# Enable Kubernetes auth:
#   vault auth enable kubernetes
#   vault write auth/kubernetes/config \
#     kubernetes_host="https://$KUBERNETES_PORT_443_TCP_ADDR:443" \
#     token_reviewer_jwt="$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)" \
#     kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
#     issuer="https://kubernetes.default.svc.cluster.local"

# ESO reads static secrets for all scopes
path "kv/data/skstacks/*" {
  capabilities = ["read"]
}
path "kv/metadata/skstacks/*" {
  capabilities = ["list", "read"]
}

# ESO can renew its own token
path "auth/token/renew-self" {
  capabilities = ["update"]
}
