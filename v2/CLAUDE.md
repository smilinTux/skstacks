# SKStacks v2 — Claude Code Context

## Role: v2 is the deployment engine skos consumes

**skos** = the sovereign OS layer. **v2** = the deployment engine skos calls
into to provision, deploy, and manage capabilities.

v2 is **security-backend-agnostic, multi-platform** (Swarm + K8s + RKE2 + k3d),
GitOps-first. **Public-template repository** — no real secrets, all `CHANGEME_*`
placeholders.

**v2 is not yet hosting production workloads.** All live deployments are still on
[v1](../v1/CLAUDE.md). Use v2 for:
- Scaffolding and refining capability port definitions (the `<C>/<port>/app.yaml` stubs)
- Architecting the adapter swap (e.g. MinIO → Garage, Redis → Valkey, no WAF → Coraza)
- Designing platform-agnostic service descriptors for the 4C port set
- GitOps / ArgoCD / OpenTofu greenfield patterns

For real prod work (running iron), default to v1.

## Ports / Adapters model

Every `sk*` directory is a **port** — a stable capability interface. The `provider:`
field in `app.yaml` names the recommended **adapter** (underlying technology).
Swapping adapters does not affect other ports. `secrets/` is the reference
implementation: `interface.py` = port, three subdirs = three adapters.

## The 4C layout

```
v2/
  cloud/      ← skfence  skmesh  skdns  skcicd  skinfra  skdweb
  comms/      ← skcomms  skchat  skvoice  skbus
  compute/    ← skdata  skcache  skobject  skfiles  skmodel  skflow  skmon  skpulse  skbackup
  core/       ← capauth  sksso  sksec  skwaf  skca  skvault
  secrets/    ← shared seam: vault_file / hashicorp_vault / capauth adapters
  infra/tofu/ ← OpenTofu provisioning (was v2/tofu/ — moved here 2026-06-09)
  platform/   ← swarm / kubernetes / rke2 / k3d
```

Note: `infra/tofu/` is the canonical provisioning home. The old top-level `tofu/`
was removed. The empty `shared/` placeholder was also removed.

## Authoritative docs (read these first, in order)

1. **[`README.md`](./README.md)** — overview of what's new vs v1, secret-backend
   matrix, platform matrix, mermaid architecture diagram, quick start.
2. **[`ARCHITECTURE.md`](./ARCHITECTURE.md)** — full architecture spec.
3. **[`CONVENTIONS.md`](./CONVENTIONS.md)** — naming rules, ports/adapters pattern,
   how to add a new port or adapter, path reference. Read before extending v2.
4. **[`docs/APP-DESCRIPTOR.md`](./docs/APP-DESCRIPTOR.md)** — the `app.yaml` schema,
   field reference, and the distinction between the v2 descriptor and the skos
   foundation descriptor.
5. **[`DEPLOYMENT.md`](./DEPLOYMENT.md)** — deployment workflows per platform.
6. **[`SECRETS.md`](./SECRETS.md)** — secret-backend selection guide
   (vault-file vs HashiCorp Vault vs CapAuth/PGP).
7. **[`SECURITY-BACKENDS.md`](./SECURITY-BACKENDS.md)** — backend-specific
   integration details.


## Repo layout (v2)

```
v2/
├── cloud/        # Ports: skfence skmesh skdns skcicd skinfra skdweb
├── comms/        # Ports: skcomms skchat skvoice skbus
├── compute/      # Ports: skdata skcache skobject skfiles skmodel skflow skmon skpulse skbackup
├── core/         # Ports: capauth sksso sksec skwaf skca skvault
├── secrets/      # SKSecretBackend implementations (vault_file / hashicorp_vault / capauth)
├── infra/        # Provisioning home
│   └── tofu/     # OpenTofu modules + examples (was v2/tofu/, moved 2026-06-09)
├── platform/     # Per-platform deployment glue (swarm / kubernetes / rke2 / k3d)
├── cicd/         # Forgejo + GitHub + GitLab + ArgoCD pipelines
├── overlays/     # Cross-cutting environment overlays (prod / staging / dev)
└── tests/        # Test harnesses for secret backends
```

## Key v2-vs-v1 differences (don't mix patterns)

| Concern | v1 way | v2 way |
|---|---|---|
| Secrets | Ansible vault YAML in repo | Pluggable: vault-file / Vault / CapAuth |
| Platforms | Swarm only | Swarm / K8s / RKE2 / k3d |
| Service config | Per-service Ansible vars | Unified `app.yaml` + platform overlays |
| K8s secret sync | n/a | External Secrets Operator bridge |
| CI/CD | Forgejo Actions | Forgejo + GitHub + GitLab + ArgoCD |

## When in doubt

- If the service runs on real iron right now → it's in v1.
- If you're sketching a future enterprise pattern → v2.
- The host-level `~/.claude/CLAUDE.md` (Lumina/skmemory) applies here too.
