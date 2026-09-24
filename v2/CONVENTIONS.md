# SKStacks v2 — Conventions

The methodology keystone for extending v2 consistently. Read before adding a new
port, adapter, or platform.

---

## The Core Rule: Ports and Adapters

**Every `sk*` directory is a port.** A port is a stable capability interface -- it
names what the system can do (e.g. `skfence` = edge/ingress, `skcache` = cache/KV).
The port name is permanent and never changes.

**The `provider:` field in `app.yaml` names an adapter.** An adapter is the
underlying technology that fulfills the port today (e.g. Traefik, Valkey, NATS).
Adapters are swappable. Changing the adapter does not break anything that depends
on the port -- that is the entire point.

The canonical reference implementation of this pattern is `secrets/`:
- `secrets/interface.py` = the port (`SKSecretBackend`)
- `secrets/vault_file/`, `secrets/hashicorp_vault/`, `secrets/capauth/` = three adapters

Every other capability follows the same shape.

---

## The 4C Layout

All ports live at `v2/<C>/<port>/`:

| C | Macro-group | Ports |
|---|---|---|
| `cloud/` | Edge, routing, naming, deploy, infra, dweb | `skfence` `skmesh` `skdns` `skcicd` `skinfra` `skdweb` |
| `comms/` | Chat, voice, transport, bus | `skcomms` `skchat` `skvoice` `skbus` |
| `compute/` | Data, cache, object, files, models, automation, observability, backup | `skdata` `skcache` `skobject` `skfiles` `skmodel` `skflow` `skmon` `skpulse` `skbackup` |
| `core/` | Identity, defense, WAF, PKI, secrets | `capauth` `sksso` `sksec` `skwaf` `skca` `skvault` |

**To assign a new port to a category:** ask "what does this port protect or provide?"
- Protects/controls the system boundary or network? -> `cloud/`
- Moves information between humans/agents? -> `comms/`
- Stores, processes, or serves data? -> `compute/`
- Establishes identity, trust, or access control? -> `core/`

---

## Naming Rules

| Thing | Convention | Example |
|---|---|---|
| Port directory | lowercase `sk<noun>` | `skfence`, `skcache`, `skbus` |
| Exception | `capauth` has no `sk` prefix (it predates the convention) | `capauth` |
| `app.yaml` | always `app.yaml`, lowercase | `v2/cloud/skfence/app.yaml` |
| Compose template | `docker-compose.yml.j2` (Jinja2) | `cloud/skfence/docker-compose.yml.j2` |
| Ansible deploy playbook | `deploy.yml` | `cloud/skfence/deploy.yml` |
| Platform directories | lowercase, hyphenated | `platform/docker-swarm/`, `platform/rke2/` |
| OpenTofu modules | lowercase hyphenated | `infra/tofu/modules/hetzner-cluster/` |

**Never use:**
- `docker-composer.yml` (typo -- the correct name is `docker-compose.yml.j2`)
- `v2/tofu/` (stale -- canonical path is `v2/infra/tofu/`)
- `v2/shared/` (removed)
- `v2/apps/` for new ports (pre-4C artifact -- superseded by 4C layout)

---

## App Descriptor Schema

Every port has `app.yaml` with these required fields:
- `name` -- matches directory name
- `capability` -- technology-agnostic description of the *port*
- `description` -- describes the *adapter* (current technology choice)
- `scope` -- secret backend path prefix (usually == name)
- `version` -- adapter version or `CHANGEME_VERSION` for stubs
- `platforms` -- list of target platforms
- `provider` -- recommended sovereign adapter + 1-line rationale
- `alternates` -- interop adapter list (empty list `[]` is valid)
- `secrets` -- key names only, never values; each entry has `key`, `description`, `required`, `sensitive`
- `config` -- non-sensitive runtime config
- `healthcheck` -- endpoint for health probing

Full schema reference: `docs/APP-DESCRIPTOR.md`.

### Provider selection

Use the capability map (`docs/superpowers/specs/2026-06-09-skos-capability-map.md`)
to pick the right provider and alternates. The pattern is always:

    sovereign-default + open-standard + interop-adapter

Example: `skcache`: provider=Valkey (BSD-3, drop-in), alternates=[DragonflyDB (scale)].
The open standard crossing between them is the Redis protocol.

### Secrets rule

Secrets *keys* live in `app.yaml`. Secret *values* are injected by the backend at
deploy time. The three backends share the same `SKSecretBackend` interface:
- `vault-file` -- AES-256 encrypted YAML, git-native (zero infra)
- `hashicorp-vault` -- HA Raft, dynamic secrets, audit log
- `capauth` -- sovereign PGP, skcapstone MCP integration, offline-capable

See `SECRETS.md` and `SECURITY-BACKENDS.md` for backend-specific details.

---

## How to Add a New Port

1. Decide which 4C category it belongs to (see table above).
2. `mkdir v2/<C>/<port>/`
3. Copy `app.yaml` from a neighboring stub. Fill in all required fields.
4. Set `provider` from the capability map; list `alternates`.
5. For Swarm: create `docker-compose.yml.j2` + `deploy.yml`.
6. For K8s: create a Kustomize base or Helm values file under `platform/kubernetes/`.
7. For ArgoCD: add an app manifest to `cicd/argocd/apps/<port>.yaml` (path = `v2/<C>/<port>`).
8. Update `CONVENTIONS.md` port registry.
9. Update `README.md` 4C table if adding a new official port.

## How to Add a New Adapter (for an existing port)

1. The `app.yaml` for the port already exists. Update `provider:` to the new adapter.
2. Add the old adapter to `alternates:` if it is still valid.
3. Create a new `docker-compose.yml.j2` (or K8s values) alongside the existing one.
   Name it `docker-compose.<adapter>.yml.j2` if multiple exist.
4. Update any CI workflow that references the old adapter image name.
5. Document the migration in `docs/` if the swap involves data migration.

---

## skos Architecture Positioning

```
skos (the sovereign OS)
  └── consumes v2 (the deployment engine)
        ├── v2 provides: ports (capability stubs) + adapters (technology choices)
        ├── v2 provides: secrets/ (SKSecretBackend port + 3 adapters)
        ├── v2 provides: platform/ (Swarm / K8s / RKE2 / k3d glue)
        └── v2 provides: infra/tofu/ (OpenTofu provisioning)
```

v2 is **not** skos. skos is a separate repo (`smilinTux/skos`). v2 is a dependency
of skos, not the OS itself. This clean separation means:

- v2 can evolve its deployment engine without touching the OS layer.
- skos can consume a different deployment engine if needed (by swapping v2).
- The `app.yaml` in v2 (deployment spec) and the `app.yaml` in skos (packaging spec)
  serve different purposes -- see `docs/APP-DESCRIPTOR.md` for the exact distinction.

**v1 is prod today.** v2 is the greenfield engine. Do not deploy to production using
v2 until it has been fully validated. When in doubt, check v1.

---

## Path Reference

| Canonical path | What it is |
|---|---|
| `v2/<C>/<port>/app.yaml` | Port capability descriptor |
| `v2/<C>/<port>/docker-compose.yml.j2` | Swarm compose template (Jinja2) |
| `v2/<C>/<port>/deploy.yml` | Ansible deploy playbook for this port |
| `v2/infra/tofu/` | OpenTofu provisioning modules and examples |
| `v2/platform/swarm/` | Docker Swarm platform glue (ansible roles, env, stacks) |
| `v2/platform/kubernetes/` | Kustomize base + environment overlays |
| `v2/platform/rke2/` | RKE2 ansible, Helm values, manifests |
| `v2/platform/k3d/` | k3d local/CI cluster configs |
| `v2/secrets/` | SKSecretBackend implementations (reference adapter pattern) |
| `v2/cicd/` | Forgejo, GitHub, ArgoCD pipeline definitions |
| `v2/overlays/<env>/` | Environment-specific non-secret config overrides |
| `v2/docs/` | v2-specific design documents (this file, APP-DESCRIPTOR.md, etc.) |


## Runtime Storage Paths (deployed nodes)

Two data roots on every node — pick by whether the data can tolerate NFS:

| Path | Backing | Use for |
|---|---|---|
| `/var/data/...` | **NFS** (shared, node-portable) | config, runtime caches/certs/logs, shared assets, backup staging |
| `/var/local/data/...` | **LOCAL disk** (per-node SSD) | **stateful engines** — Postgres, Qdrant, FalkorDB, persistent Redis (any DB/vector/graph; `mmap`+locking hates NFS) |

`/var/local/data` mirrors the `/var/data/{service}-{env}/...` layout exactly (literal
`s#/var/data#/var/local/data#`). Local data does not float across nodes -> the service is
**node-pinned or app-replicated** (Qdrant `replication_factor`, Patroni). `/var/local/data` is
**outside skbackup** -> snapshot into `/var/data/.../database-dump` or add it to the backup mounts
(backup-target = skobject). Provision a dedicated local disk at `/var/local` (Proxmox VMs: add a
virtual disk). Full standard: `v1/docs/VOLUME_MANAGEMENT_STANDARDS.md`.
