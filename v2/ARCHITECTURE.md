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

```mermaid
flowchart TD
    OP["Operator / CI Pipeline\nansible-playbook, kubectl apply, argocd sync"]

    subgraph SRL["Secret Resolution Layer"]
        IF["SKSecretBackend\ninterface.py"]
        VF["VaultFileBackend\nAES-256, ansible-vault\ngit-native encrypted files"]
        HV["HashiCorpVaultBackend\nHA Raft, dynamic secrets\nAPI-driven, audit log"]
        CA["CapAuthBackend\nPGP blobs, skcapstone MCP\noffline-capable, sovereign"]
        IF --> VF
        IF --> HV
        IF --> CA
    end

    subgraph STL["Service Template Layer"]
        T1["core/skfence/docker-compose.yml.j2"]
        T2["core/sksec/docker-compose.yml.j2"]
        T3["core/sksso/docker-compose.yml.j2"]
        T4["core/skbackup/docker-compose.yml.j2"]
        T5["core/skha/keepalived.conf.j2"]
    end

    subgraph PL["Platform Layer"]
        SW["Docker Swarm\ndocker stack deploy"]
        K8["Kubernetes / RKE2\nkubectl apply -k, Kustomize + ESO"]
        AG["ArgoCD GitOps\nargocd sync"]
    end

    OP --> SRL
    SRL --> STL
    STL --> PL
    K8 --> AG
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

```mermaid
flowchart LR
    APP["app.yaml\nsecrets refs only, no raw values"]
    VYP["overlays/prod/values.yaml\nDOMAIN, CLUSTER, CIDR"]
    VYS["overlays/staging/values.yaml"]
    VYD["overlays/dev/values.yaml"]
    SB["Secret Backend\nvault-file, Vault, or CapAuth"]
    REND["Rendered Manifest\nephemeral, never stored"]

    APP --> REND
    VYP --> REND
    VYS --> REND
    VYD --> REND
    SB -->|resolved secrets in-memory| REND
```

---

## Kubernetes / RKE2 Secret Flow

For K8s and RKE2 deployments, the **External Secrets Operator (ESO)** bridges
the chosen secret backend into native K8s Secrets.

```mermaid
sequenceDiagram
    participant D as Deploy Tool
    participant B as Secret Backend
    participant ESO as Ext Secrets Operator
    participant K8S as Kubernetes API
    participant P as Pod

    D->>ESO: apply ExternalSecret CRD (secretStoreRef + remoteRef.key)
    ESO->>B: authenticate (AppRole / K8s JWT / PGP)
    B-->>ESO: token granted
    ESO->>B: read secret kv/data/skstacks/ENV/SCOPE/KEY
    B-->>ESO: plaintext value (in-memory only)
    ESO->>K8S: create/update native Secret
    K8S-->>P: mount as env var / volume
    Note over B,ESO: Periodic resync via refreshInterval or force-sync annotation
```

For the CapAuth backend, a lightweight ESO provider plugin communicates with
the local `skcapstone` MCP server to decrypt PGP-encrypted secret blobs.

---

## RKE2 Platform Architecture

```mermaid
flowchart TD
    subgraph CP["Control Plane - odd count 3+"]
        S1["server-1\nkube-apiserver\nscheduler\netcd :2379"]
        S2["server-2\nkube-apiserver\nscheduler\netcd :2379"]
        S3["server-3\nkube-apiserver\nscheduler\netcd :2379"]
        S1 <-->|Raft| S2
        S2 <-->|Raft| S3
        S3 <-->|Raft| S1
    end

    VIP["VIP :6443\nKeepalived or kube-vip"] --> CP

    subgraph WN["Worker Nodes"]
        W1["worker-1\ncontainerd"]
        W2["worker-2\ncontainerd"]
        W3["worker-3\ncontainerd"]
    end

    CP --> WN

    subgraph AM["Auto-deployed Manifests"]
        metallb["metallb - bare-metal LoadBalancer"]
        certmgr["cert-manager - TLS ACME + Vault PKI"]
        nginx["ingress-nginx - HTTP/S ingress"]
        eso["external-secrets - secret backend bridge"]
        longhorn["longhorn - distributed block storage"]
    end

    WN --> AM

    subgraph GO["GitOps Layer"]
        AR["ArgoCD\napp-of-apps.yaml"]
        APPS["Service Applications\nskfence, sksec, sksso, skbackup"]
        AR --> APPS
    end

    AM --> GO
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

```mermaid
flowchart TD
    PUSH["Code Push\nPR or main branch"]

    subgraph CI["CI Trigger - Forgejo / GitHub / GitLab"]
        BUILD["Build Stage\n- Build container image\n- Sign with Cosign\n- Push to SKReg / GHCR"]
        TEST["Test Stage\n- ansible-lint playbooks\n- kube-score / kubelinter\n- Trivy / grype security scan"]
        BUILD --> TEST
    end

    PUSH --> CI

    subgraph DEPLOY["Deploy Stage - environment-gated"]
        DEV["dev\nauto-deploy on push"]
        STG["staging\nauto-deploy on push to main"]
        PROD["prod\nmanual gate + ArgoCD sync"]
    end

    TEST --> DEV
    TEST --> STG
    STG -->|approval| PROD
```

---

## Network Topology (all platforms)

```mermaid
flowchart TD
    INT["Internet"]
    SF["SKFence - Traefik v3 or ingress-nginx\nTLS termination, ACME, rate-limit"]
    INT -->|443 / 80| SF

    subgraph PUB["traefik-public overlay network"]
        SSO["SKSSO\nAuthentik SSO\nLDAP/SAML/OIDC"]
        MON["SKMON\nGrafana / Prometheus"]
        APPS["App Services\ncustom"]
    end

    SF --> PUB

    subgraph EDGE["traefik-internal overlay network - service mesh"]
        SKSEC["SKSEC\nCrowdSec IDS\nTraefik bouncer"]
        SOCK["Socket Proxy\nDocker API read-only"]
    end

    PUB --> EDGE
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

```mermaid
flowchart LR
    V1["SKStacks v1\nAnsible-vault only\nDocker Swarm"]
    EXP["vault_export.yml\nto secrets.json encrypted"]
    MIG["secrets/migrate.py\n--from vault-file\n--to hashicorp-vault"]
    V2["SKStacks v2\nPluggable backends\nSwarm + K8s + RKE2 + k3d"]

    V1 -->|"keep v1 running"| EXP
    EXP --> MIG
    MIG --> V2
    V1 -.->|"parallel deploy,\nvalidate, cut DNS"| V2
```
