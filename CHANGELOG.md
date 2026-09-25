# Changelog

All notable changes to the SKStacks framework. Tags: `skstacks-vMAJOR.MINOR.PATCH`.
Each entry says what an instance must do, if anything.

## skstacks-v2.13.0 - 2026-09-25

- v1: third published v1 service, **skpdf** (Stirling-PDF), with dev and
  staging playbooks brought to prod parity (dropped the self-referencing
  vault-select vars).

Instance action: if you adopt v1 skpdf, set its vault values including
`CLUSTERNAME` and `DOMAIN` (both now required, no estate default), and
share `/var/data` across all nodes the same way skgraph and skvector
require.

## skstacks-v2.12.0 - 2026-09-25

- v1: second published v1 service, **skvector** (Qdrant), with dev and
  staging playbooks brought to prod parity (dropped the removed-`ipam`
  docker_network task, added the control script symlink task).

Instance action: if you adopt v1 skvector, set its vault values including
`CLUSTERNAME` and `DOMAIN` (both now required, no estate default), and
share `/var/data` across all nodes the same way skgraph requires.

## skstacks-v2.11.0 - 2026-09-25

- tofu: `v2/infra/tofu/modules/libvirt-cluster` (local KVM VMs, Ubuntu 24.04
  with the HWE kernel, Docker; inventory in the swarm platform's layout) and
  `examples/libvirt-swarm`.
- v1: first published v1 service, **skgraph** (FalkorDB), with the shared
  tasks it uses. `select_vault_file.yml` honours `skstacks_vault_dir` so an
  instance keeps its own vaults.

- swarm platform: `deploy.yml` found none of its roles (fixed with a
  `playbooks/roles` link) and its plays are now tagged `bootstrap`, `ha`,
  `traefik`; `--tags bootstrap` builds just the swarm.
- k3d platform: `scripts/create.sh` and `clusters/ci.yaml` were rejected by
  k3d v5 (`--name`, `kubeAPI` under `options`); both fixed.
- CI: `framework tests` workflow runs the v1, swarm, k3d and tofu suites.

Instance action: none unless you adopt v1 skgraph; then set
`skstacks_vault_dir`, share `/var/data` across all nodes, and see
`v1/README.md`. If you ran the swarm `deploy.yml` with your own role path
workaround, you can drop it.

## skstacks-v2.10.0 - 2026-09-24

- skred.denylist: a non-UTF-8 denylist exits 2; unreadable files are
  findings; FIFOs and sockets are skipped instead of hanging; UTF-16 text is
  searched.
- skred gate: exits 2 when every target is out of scope (nothing scanned)
  instead of passing.
- skmesh: cloudflared, netbird and newt Deployments run with a read-only root
  filesystem, no privilege escalation, minimal capabilities and the
  RuntimeDefault seccomp profile.
- INSTANCE-MODEL guide: status section brought up to date; clone steps
  re-disable framework push.

Instance action: netbird and newt now mount emptyDirs for their state
paths; if you patched these manifests, re-apply your changes on top.

## skstacks-v2.9.0 - 2026-09-24

First public release of the v2 framework.

- Framework + instance model: see `v2/docs/INSTANCE-MODEL.md`.
- skred scope defaults are loopback-only; declare yours with
  `SKRED_SCOPE_DOMAINS` / `SKRED_SCOPE_CIDRS`.
- `skred.denylist` estate gate in CI.

Instance action: none (first release). Pin `framework/` to this tag.
