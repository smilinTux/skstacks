# SKStacks v2 — Architecture

## Design Principles

1. **Secret backend is a plug** — swap vault-file → HashiCorp Vault → CapAuth
   without changing service templates.
2. **Platform is a target** — the same service descriptor renders to
   docker-compose, K8s manifests, or Helm values.
3. **Secrets never live in Git** — example/template files contain placeholder
   tokens (`CHANGEME_*`). Real values come from the selected backend at deploy time.
4. **Least privilege by default** — each service gets its own secret scope.
5. **GitOps-ready** — ArgoCD / Flux can drive the K8s/RKE2 side while
   Ansible handles node-level config.

---

## System Layers

```
┌──────────────────────────────────────────────────────────────────┐
│  Operator / CI Pipeline                                          │
│  ansible-playbook | kubectl apply | argocd sync                  │
└──────────────────────────┬───────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────┐
│  Secret Resolution Layer                                         │
│                                                                  │
│  SKSecretBackend (interface.py)                                  │
│     │                                                            │
│     ├── VaultFileBackend        (ansible-vault AES-256)          │
│     ├── HashiCorpVaultBackend   (HCP Vault API, dynamic secrets) │
│     └── CapAuthBackend          (PGP / skcapstone MCP)           │
└──────────────────────────┬───────────────────────────────────────┘
                           │  resolved plain-text (in memory only)
┌──────────────────────────▼───────────────────────────────────────┐
│  Service Template Layer                                          │
│                                                                  │
│  core/skfence/docker-compose.yml.j2                              │
│  core/sksec/docker-compose.yml.j2                                │
│  core/sksso/docker-compose.yml.j2                                │
│  core/skbackup/docker-compose.yml.j2                             │
│  core/skha/keepalived.conf.j2                                    │
└──────────────────────────┬───────────────────────────────────────┘
                           │  rendered manifests (never stored)
┌──────────────────────────▼───────────────────────────────────────┐
│  Platform Layer                                                  │
│                                                                  │
│  Docker Swarm                 Kubernetes / RKE2                  │
│  ─────────────               ────────────────                    │
│  docker stack deploy          kubectl apply -k                   │
│  (rendered compose)           (Kustomize + ESO)                  │
│                               argocd sync                        │
└──────────────────────────────────────────────────────────────────┘
```

---

## Secret Scoping Model

Every service is assigned a **secret scope** — a path prefix in the chosen
backend. No service can read another service's secrets.

```
# vault-file
~/.vault_pass_env/.{scope}_{env}_vault_pass
group_vars/{env}/{scope}-{env}_vault.yml

# HashiCorp Vault
kv/data/skstacks/{env}/{scope}/*
  └─ kv/data/skstacks/prod/skfence/*
  └─ kv/data/skstacks/prod/sksec/*
  └─ kv/data/skstacks/prod/custom-app/*

# CapAuth
~/.skstacks/secrets/{env}/{scope}.gpg   (encrypted with agent key)
  └─ prod/skfence.gpg
  └─ prod/sksec.gpg
```

---

## App Descriptor (`app.yaml`)

Every service — core or custom — is described by an `app.yaml`. This file
contains **no secrets**, only references to secret keys.

```yaml
# v2/core/skfence/app.yaml
name: skfence
scope: skfence          # secret backend path prefix
version: "3.3"
platforms: [docker-swarm, kubernetes, rke2]

secrets:
  - key: cloudflare_dns_token
    description: "Cloudflare DNS API token for ACME DNS-01 challenge"
    rotation_days: 90
  - key: dashboard_user
    description: "Traefik dashboard basic-auth user"
  - key: dashboard_password_hash
    description: "Traefik dashboard bcrypt password hash"
    sensitive: true

config:
  DOMAIN: "${SKSTACKS_DOMAIN}"
  CLUSTERNAME: "${SKSTACKS_CLUSTER}"
  CERT_RESOLVER: main
  TLS_OPTIONS: default@file
  LOG_LEVEL: INFO
  RATE_LIMIT_AVERAGE: "100"
  RATE_LIMIT_BURST: "50"

networks:
  - cloud-edge
  - cloud-public
  - cloud-socket-proxy
```

---

## Multi-Environment Overlay Model

Environment-specific values (domain, cluster name, network CIDRs) live in
`overlays/{env}/values.yaml`. They never include raw secret values — those
come from the secret backend.

```
overlays/
├── prod/
│   └── values.yaml          # DOMAIN=your-domain.com, CLUSTER=skstack01, etc.
├── staging/
│   └── values.yaml
└── dev/
    └── values.yaml
```

---

## Kubernetes / RKE2 Secret Flow

For K8s and RKE2 deployments, the **External Secrets Operator (ESO)** bridges
the chosen secret backend into native K8s Secrets.

```
┌─────────────────────────────────────────────────────────────────┐
│  External Secrets Operator                                      │
│                                                                 │
│  ExternalSecret (CRD)                                           │
│    spec.secretStoreRef → HashiCorp Vault / AWS SM / CapAuth     │
│    spec.data[].remoteRef.key = "skstacks/prod/skfence/..."      │
│    ↓                                                            │
│  Syncs to → native k8s Secret (skfence-secrets)                │
│                                                                 │
│  Pod references k8s Secret via envFrom / volumeMount            │
└─────────────────────────────────────────────────────────────────┘
```

For the CapAuth backend, a lightweight ESO provider plugin communicates with
the local `skcapstone` MCP server to decrypt PGP-encrypted secret blobs.

---

## RKE2 Platform Architecture

```
┌───────────────────────────────────────────────────────────────┐
│  RKE2 Cluster                                                 │
│                                                               │
│  Server nodes (control plane + etcd, odd count ≥ 3)          │
│  ├─ RKE2 server process                                       │
│  ├─ embedded etcd (HA raft)                                   │
│  ├─ kube-apiserver, scheduler, controller-manager            │
│  └─ containerd (runtime)                                      │
│                                                               │
│  Agent nodes (workers)                                        │
│  ├─ RKE2 agent process                                        │
│  └─ containerd (runtime)                                      │
│                                                               │
│  Auto-deployed manifests (/var/lib/rancher/rke2/server/manifests/)│
│  ├─ metallb          — bare-metal LoadBalancer                │
│  ├─ cert-manager     — TLS (ACME + Vault PKI)                 │
│  ├─ ingress-nginx    — HTTP ingress (replaces Traefik)        │
│  ├─ external-secrets — secret backend bridge                  │
│  └─ longhorn         — distributed block storage             │
│                                                               │
│  GitOps layer (ArgoCD)                                        │
│  └─ app-of-apps.yaml → manages all service Applications      │
└───────────────────────────────────────────────────────────────┘
```

### Why RKE2 over vanilla K8s?

| Feature | Vanilla K8s | RKE2 |
|---------|-------------|------|
| CIS-benchmark hardened | Optional | **Built-in** |
| etcd embedded | External required | **Built-in** |
| FIPS 140-2 compliant | No | **Supported** |
| Air-gap install | Complex | **Native** |
| Rancher integration | Manual | **Native** |
| Runtime | user choice | containerd (hardened) |
| Upgrade strategy | rolling, manual | **Automated via channel** |

---

## CI/CD Pipeline Model

```
Code push
  │
  ├─ Forgejo / GitHub / GitLab triggers workflow
  │
  ├─ Build stage
  │   ├─ Build container image
  │   ├─ Sign image with Cosign
  │   └─ Push to private registry (SKReg / GHCR)
  │
  ├─ Test stage
  │   ├─ Lint Ansible playbooks (ansible-lint)
  │   ├─ Lint K8s manifests (kube-score, kubelinter)
  │   └─ Security scan (Trivy, grype)
  │
  └─ Deploy stage (environment-gated)
      ├─ dev: auto-deploy on push
      ├─ staging: auto-deploy on push to main
      └─ prod: manual gate + ArgoCD sync
```

---

## Network Topology (all platforms)

```
Internet
  │
  ▼ 443/80
┌─────────────────────┐
│  SKFence (Traefik)  │  ← reverse proxy, TLS termination, ACME
│  or ingress-nginx   │
└─────────┬───────────┘
          │
     ┌────▼──────────────────────────────┐
     │  cloud-public overlay network     │
     └────┬──────┬──────────────┬────────┘
          │      │              │
        SKSSO  SKMON         App services
        (SSO)  (Grafana)     (custom)
          │
     ┌────▼─────────────────────────────┐
     │  cloud-edge overlay network      │  ← service mesh
     └────┬──────────────────────────── ┘
          │
       Socket Proxy (Docker API — read-only)
```

All inter-service traffic stays on private overlay networks.
Public exposure is only via SKFence/ingress-nginx.

---

## Migration Path: v1 → v2

1. **Keep v1 running.** v2 is parallel, not a drop-in replacement.
2. **Choose secret backend.** vault-file is the zero-effort migration path.
3. **Export existing vaults** with `vault-file/ansible/vault_export.yml` → creates
   portable `secrets.json` (encrypted).
4. **Import to new backend** with `secrets/migrate.py --from vault-file --to hashicorp-vault`.
5. **Deploy v2 services** alongside v1, validate, then cut over DNS.
6. **Decommission v1** service by service.
