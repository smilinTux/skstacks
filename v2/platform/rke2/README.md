# SKStacks v2 — RKE2 Platform

RKE2 (Rancher Kubernetes Engine 2) is the recommended Kubernetes distribution
for SKStacks sovereign deployments. It ships with CIS-benchmark hardening,
embedded etcd, and Rancher integration out of the box.

---

## Why RKE2

- **CIS Kubernetes benchmark** hardened by default
- **FIPS 140-2** compliant mode available
- **Air-gap install** supported natively
- **Embedded etcd** — no external database required
- **containerd** runtime (hardened) — no Docker daemon on nodes
- **Automatic upgrades** via Rancher system-upgrade-controller
- Integrates with **Rancher Fleet** for GitOps at scale

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  RKE2 Cluster                                                   │
│                                                                 │
│  Server nodes (control-plane + etcd)                            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                      │
│  │ server-1 │  │ server-2 │  │ server-3 │  ← Raft quorum       │
│  │ :6443    │  │ :6443    │  │ :6443    │                      │
│  └──────────┘  └──────────┘  └──────────┘                      │
│       ↑                                                         │
│  VIP  │  (Keepalived or kube-vip on :6443)                     │
│       ↓                                                         │
│  Agent nodes (workers)                                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                      │
│  │ worker-1 │  │ worker-2 │  │ worker-3 │                      │
│  └──────────┘  └──────────┘  └──────────┘                      │
│                                                                 │
│  Auto-deployed system manifests                                 │
│  /var/lib/rancher/rke2/server/manifests/                        │
│  ├── metallb.yaml          ← LoadBalancer for bare metal        │
│  ├── cert-manager.yaml     ← TLS (ACME + Vault PKI)            │
│  ├── ingress-nginx.yaml    ← HTTP/S ingress                     │
│  ├── longhorn.yaml         ← Distributed block storage          │
│  └── external-secrets.yaml ← Secret backend bridge             │
│                                                                 │
│  GitOps (ArgoCD)                                                │
│  cicd/argocd/app-of-apps.yaml → manages all service apps       │
└─────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

### Hardware minimums

| Role | CPU | RAM | Disk | Count |
|------|-----|-----|------|-------|
| Server (control-plane) | 4 vCPU | 8 GB | 100 GB SSD | 3 (HA) |
| Agent (worker) | 4 vCPU | 16 GB | 200 GB SSD | 3+ |

### OS

- RHEL 8/9 / AlmaLinux 8/9 / Rocky Linux 8/9 (recommended for CIS)
- Ubuntu 22.04 / 24.04 LTS
- Debian 12

### Network

```
Control-plane VIP:     192.168.x.10/24   (Keepalived or kube-vip)
Pod CIDR:             10.42.0.0/16
Service CIDR:         10.43.0.0/16
MetalLB pool:         192.168.x.200-250  (bare-metal LB addresses)

Required ports:
  Server ↔ Server:  2379-2380/tcp (etcd)
  Server ↔ Agent:   9345/tcp (RKE2 join), 6443/tcp (kube API)
  All nodes:        10250/tcp (kubelet), 8472/udp (Canal/VXLAN)
  MetalLB:          7946/tcp+udp (L2 speaker)
```

---

## Quick Deploy

### 1. Configure inventory

```bash
cp ansible/inventory.example.yml ansible/inventory.yml
$EDITOR ansible/inventory.yml
```

### 2. Install first server node

```bash
cd platform/rke2
ansible-playbook -i ansible/inventory.yml ansible/install-rke2-server.yml \
  --limit rke2_servers[0]
```

### 3. Join remaining server nodes

```bash
ansible-playbook -i ansible/inventory.yml ansible/install-rke2-server.yml \
  --limit rke2_servers[1:]
```

### 4. Install agent nodes

```bash
ansible-playbook -i ansible/inventory.yml ansible/install-rke2-agent.yml
```

### 5. Deploy system manifests

```bash
ansible-playbook -i ansible/inventory.yml ansible/deploy-manifests.yml
```

### 6. Bootstrap ArgoCD + app-of-apps

```bash
kubectl apply -f ../../cicd/argocd/bootstrap.yaml
kubectl apply -f ../../cicd/argocd/app-of-apps.yaml
```

---

## File Layout

```
platform/rke2/
├── README.md                        ← this file
├── ansible/
│   ├── inventory.example.yml        ← node inventory template
│   ├── install-rke2-server.yml      ← server node setup
│   ├── install-rke2-agent.yml       ← worker node setup
│   ├── deploy-manifests.yml         ← push system manifests
│   ├── upgrade-rke2.yml             ← rolling upgrade playbook
│   └── roles/
│       ├── rke2-common/             ← shared prereqs (OS hardening, etc.)
│       ├── rke2-server/             ← server-specific config
│       └── rke2-agent/              ← agent-specific config
│
├── manifests/                       ← auto-deployed at cluster boot
│   ├── metallb.yaml
│   ├── metallb-pool.yaml.example
│   ├── cert-manager.yaml
│   ├── ingress-nginx.yaml
│   ├── longhorn.yaml                ← Longhorn storage (replicas=3, /var/lib/longhorn)
│   └── external-secrets.yaml
│
├── scripts/
│   └── longhorn-preflight.sh        ← pre-flight check: open-iscsi, disk, kernel
│
└── helm/
    ├── longhorn-values.yaml
    ├── rancher-values.yaml
    └── vault-values.yaml            ← see ../../secrets/hashicorp_vault/helm/
```

---

## Secret Backend Integration

### vault-file (Ansible renders at deploy time)

Secrets are resolved by Ansible before deploying Kubernetes manifests.
Values are written to K8s Secrets directly via `kubectl` — no runtime
secret resolution needed. Use only when cluster has no ESO.

### hashicorp-vault (recommended for RKE2)

1. Deploy Vault to the cluster: `secrets/hashicorp_vault/helm/vault-values.yaml`
2. Install ESO: `manifests/external-secrets.yaml`
3. Configure `ClusterSecretStore`:
   ```yaml
   # overlays/prod/vault-cluster-secret-store.yaml
   apiVersion: external-secrets.io/v1beta1
   kind: ClusterSecretStore
   metadata:
     name: vault-store
   spec:
     provider:
       vault:
         server: "https://vault.cluster.local:8200"
         path: "kv"
         version: "v2"
         auth:
           kubernetes:
             mountPath: "kubernetes"
             role: "skstacks-eso"
   ```
4. Each service namespace gets an `ExternalSecret` CRD pointing to its scope.

### capauth (sovereign/offline)

1. Run `skcapstone` agent on each worker node (or as a DaemonSet).
2. Deploy the `capauth-eso-provider` plugin (see `secrets/capauth/k8s/`).
3. Configure `ClusterSecretStore` with `provider.plugin` pointing to the
   skcapstone Unix socket.

---

## Networking: Canal vs. Calico

RKE2 ships **Canal** (Flannel + Calico network policy) by default.
For production sovereign stacks, consider **Calico** in full mode for:
- Richer NetworkPolicy (egress + FQDN-based rules)
- BGP peering with physical network
- Encryption (WireGuard data-plane)

Configure via the RKE2 server config:
```yaml
# /etc/rancher/rke2/config.yaml
cni: calico
```

---

## Storage: Longhorn

Longhorn provides distributed block storage on bare metal. Each node that has
the Longhorn agent can contribute local disks to a replicated pool.

```yaml
# manifests/longhorn.yaml (key settings)
defaultSettings:
  defaultReplicaCount: 3                     # HA: one replica per worker
  defaultDataPath: /var/lib/longhorn         # dedicated data directory
  storageMinimalAvailablePercentage: 10      # refuse scheduling below 10% free
  storageOverProvisioningPercentage: 200     # allow thin-provisioning up to 2×
  nodeDownPodDeletionPolicy: delete-both-statefulset-and-deployment-pod
persistence:
  defaultClass: true                         # Longhorn is the default StorageClass
  defaultClassReplicaCount: 3
  reclaimPolicy: Retain                      # volumes survive PVC deletion
  defaultFsType: ext4
```

### Minimum Disk Requirements

> **Replica factor = 3**: every 1 GB of usable storage consumes 3 GB of raw
> disk spread across 3 worker nodes. Size each worker accordingly.

| Node Role | Total Disk (min) | Reserved for Longhorn | Filesystem | Drive Type |
|-----------|-----------------|----------------------|------------|------------|
| Server (control-plane) | 100 GB | 0 GB ¹ | — | SSD |
| Agent (worker) — dev/lab | 150 GB | 50 GB | ext4 / XFS | SSD |
| Agent (worker) — production | 300 GB | 200 GB | ext4 / XFS | SSD ² |

**¹** Longhorn does not schedule replicas on control-plane nodes (tainted with
`node-role.kubernetes.io/control-plane`). Control-plane disks only need to
accommodate the OS, etcd snapshots, and container images.

**²** HDDs are technically supported but strongly discouraged — random I/O is
3–5× slower, causing severe tail latency for database workloads. NVMe is
preferred for production. SATA SSDs are acceptable for low-traffic clusters.

**Total raw storage rule of thumb** (production, replicas=3):

```
usable_capacity_GB = sum(worker_longhorn_disk_GB) / 3
```

Example: 3 workers × 200 GB = 600 GB raw → ~200 GB usable (after 10% reserve).

### Pre-Flight Check

Run before the first Longhorn deployment to verify every node meets
prerequisites (open-iscsi, disk space, kernel version):

```bash
# Against the default inventory
./scripts/longhorn-preflight.sh

# Custom inventory, non-default SSH key, 100 GB disk minimum
./scripts/longhorn-preflight.sh \
  -i ansible/inventory.yml \
  -k ~/.ssh/cluster_ed25519 \
  --min-disk 100
```

Checks performed:

| Check | Level | Notes |
|-------|-------|-------|
| open-iscsi / iscsi-initiator-utils installed | REQUIRED | iSCSI is the Longhorn block transport |
| iscsid service active | REQUIRED | Must be enabled + running |
| Kernel >= 5.4 | REQUIRED | Longhorn CSI driver requires kernel 5.4+ |
| >= MIN_DISK_GB free on `/var/lib/longhorn` | REQUIRED | Default threshold: 50 GB |
| multipathd without device blacklist | WARN | Can corrupt Longhorn devices |
| nfs-common / nfs-utils missing | WARN | Only needed for NFS backup targets |

Fix commands (run on each node):

```bash
# Debian / Ubuntu
apt install open-iscsi nfs-common
systemctl enable --now iscsid

# RHEL / Rocky / AlmaLinux
dnf install iscsi-initiator-utils nfs-utils
systemctl enable --now iscsid
```

> `rke2-common` Ansible role installs and enables `open-iscsi`/
> `iscsi-initiator-utils` automatically. Run the pre-flight check
> **after** running the common role to confirm the service is active.

---

## OS Hardening (automated by rke2-common role)

The `rke2-common` Ansible role applies:

- CIS Level 1 kernel parameters (`sysctl`)
- `firewalld` / `ufw` rule enforcement
- SELinux enforcing (RHEL) or AppArmor (Ubuntu/Debian)
- `auditd` rules for K8s CIS compliance
- Disable unnecessary services (avahi, bluetooth, cups)
- SSH hardening (no root login, pubkey only, `AllowUsers`)
- Automatic security updates (unattended-upgrades / dnf-automatic)
- Kernel module blocklist (usb-storage if not needed, etc.)
- `/proc` and `/sys` mount hardening
