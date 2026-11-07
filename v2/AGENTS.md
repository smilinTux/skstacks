# AGENTS.md (skstacks v2)

v2 is the deployment engine skos calls into to provision, deploy and manage capabilities:
security-backend-agnostic, multi-platform (`platform/{swarm,kubernetes,rke2,k3d}`), GitOps-first.
The root [AGENTS.md](../AGENTS.md) applies; this file only adds what differs for `v2/`.

## Scope

- **v2 is not yet hosting production workloads.** Live deployments run on [v1](../v1/README.md).
  Use v2 for capability port definitions (`<C>/<port>/app.yaml`), adapter swaps, platform-agnostic
  service descriptors and GitOps / ArgoCD / OpenTofu greenfield patterns. For work on running iron,
  default to v1.
- **Public template:** no real secrets, all `CHANGEME_*` placeholders.

## Ports and adapters

- Every `sk*` directory under `cloud/`, `comms/`, `compute/` and `core/` (the 4C layout) is a
  **port**, a stable capability interface. The `provider:` field in its `app.yaml` names the
  recommended **adapter**. Swapping an adapter must not affect other ports.
- `secrets/` is the reference implementation: `interface.py` is the port, each subdirectory is an
  adapter, `factory.py` selects one.
- `infra/tofu/` is the only provisioning home (the old top-level `v2/tofu/` is gone).

## Read first

1. [README.md](./README.md): what is new vs v1, secret-backend and platform matrices.
2. [ARCHITECTURE.md](./ARCHITECTURE.md): the full architecture.
3. [CONVENTIONS.md](./CONVENTIONS.md): naming, ports/adapters, how to add a port or adapter. Read
   before extending v2.
4. [docs/APP-DESCRIPTOR.md](./docs/APP-DESCRIPTOR.md): the `app.yaml` schema.
5. [DEPLOYMENT.md](./DEPLOYMENT.md), [SECRETS.md](./SECRETS.md),
   [SECURITY-BACKENDS.md](./SECURITY-BACKENDS.md): per-platform deploys and secret backends.

## Do not mix v1 and v2 patterns

| Concern | v1 | v2 |
|---|---|---|
| Secrets | Ansible vault YAML | Pluggable backends under `secrets/` |
| Platforms | Swarm only | Swarm / K8s / RKE2 / k3d |
| Service config | Per-service Ansible vars | Unified `app.yaml` plus platform overlays |
| K8s secret sync | n/a | External Secrets Operator bridge |
