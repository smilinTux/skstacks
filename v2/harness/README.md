# skstacks deploy harness

Proves **descriptor → deploy → it actually works → destroy** against *real* engines —
not just structural unit tests. Each scenario is self-contained and cleans up after
itself. Built to run on `.41` (k3d, Docker Swarm, libvirt/QEMU all present).

| Script | What it proves |
|--------|----------------|
| `k3d-validate.sh` | skrender's K8s manifests pass a **live API server** (`kubectl apply --dry-run=server`, ESO CRDs installed) |
| `swarm-validate.sh` | skrender's compose passes the **real Docker engine** (`docker compose config`) |
| `k3d-deploy.sh` | full loop on K8s: k3d → **ESO operator** → fake secret backend → render+deploy → ExternalSecret syncs → pod Ready → **secret in pod + live HTTP 200** → destroy |
| `swarm-deploy.sh` | full loop on Swarm: render → deploy (unique name, test port) → replica runs → **secret env + live HTTP 200** → remove |
| `vm-deploy.sh` | full loop on a **fresh QEMU/KVM VM node**: cloud-init → k3s → deploy workload → live HTTP 200 → destroy |
| `run-all.sh` | runs the scenarios and prints a PASS/FAIL matrix |

```bash
bash run-all.sh                 # validate + k3d + swarm
bash run-all.sh vm              # add the heavy VM scenario
bash k3d-deploy.sh              # one scenario directly
```

## What it has already caught (closed-loop)
Running these against real engines found bugs structural tests missed, all since fixed:
- K8s Services need a **`name` per port**; a **portless** workload must emit **no Service**.
- ESO graduated to **`external-secrets.io/v1`** (the old `v1beta1` is gone in current charts).
- Cross-platform **env-name consistency** (K8s `envFrom` is verbatim; Swarm uppercases).

The fixture `fixtures/skwhoami` is a real service (`traefik/whoami`) that actually
becomes Ready and answers HTTP, so "verify working" means a live request, not just
"the object was created".

**Proxmox (.13)** is the next target for `vm-deploy` once space is sorted.
