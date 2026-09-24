# ESO — Kubernetes secret sync for SKStacks v2

How each secret backend reaches a Kubernetes workload:

| Backend | K8s sync mechanism | Notes |
|---|---|---|
| **openbao** (default server) | **ESO `vault` provider** → `ClusterSecretStore` (this dir) | Kubernetes auth = no pre-shared secret. OpenBao is wire-compatible, so ESO's Vault provider is the documented path (no native OpenBao provider). |
| hashicorp-vault | ESO `vault` provider (same as openbao) | Swap `server:` to the Vault address. |
| **sops-age** | **ksops / argocd-vault-plugin / Flux SOPS decryption** (NOT ESO) | GitOps lane: age-encrypted YAML decrypted by the GitOps controller, not ESO. |
| vault-file / capauth | Ansible-rendered Secret at deploy (off-cluster) | Server-less lanes; not K8s-native sync. |

## Apply

```bash
# 1. Install ESO (once)
helm install external-secrets external-secrets/external-secrets -n external-secrets --create-namespace
# 2. Ensure OpenBao Kubernetes auth role 'skstacks' is bound to the ESO SA
#    (openbao/bootstrap.sh enables kubernetes auth; create the role + policy)
# 3. Apply the store + your ExternalSecrets
kubectl apply -k v2/secrets/eso/
```

The `openbao` ClusterSecretStore authenticates with the ESO controller's
ServiceAccount JWT (`auth.kubernetes`) — there is **no pre-shared token**, which
keeps the no-catch-22 property end-to-end (see `../BOOTSTRAP.md`).
