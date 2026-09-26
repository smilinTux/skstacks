# Changelog

All notable changes to the SKStacks framework. Tags: `skstacks-vMAJOR.MINOR.PATCH`.
Each entry says what an instance must do, if anything.

## skstacks-v2.16.0 - 2026-09-26

- v1: **sksync** (Syncthing), **skpulse** (Uptime Kuma) and **skpeek**
  (SearXNG + valkey) published.
- tofu libvirt-cluster: `registry_mirrors` writes Docker's daemon.json (e.g.
  `mirror.gcr.io`), so test clusters behind one public IP stop hitting Docker
  Hub's anonymous pull limit.
- sksync configures its firewall from the one selected manager (it ran on
  every manager); skpeek's SearXNG `secret_key` is required (`skpeek.SECRET_KEY`,
  it had a literal default).

Instance action: set `sksync.*` (incl. SYNCWALLET, ENCRYPTION_TOKEN, UUID),
`skpulse.*` and `skpeek.SECRET_KEY`; shared `/var/data` as for the other v1
services.

## skstacks-v2.15.0 - 2026-09-26

- v1: **skmon** (Prometheus, Grafana, Loki, Promtail, cAdvisor, node-exporter,
  Alertmanager, Jaeger) published. Scrape extra services with
  `skmon.EXTRA_SCRAPE_TARGETS` (static) or `skmon.EXTRA_SCRAPE_CONFIGS`
  (complete scrape_config entries, e.g. DNS service discovery).
- v1: **skform** (OpenTofu CLI container) published. Its image is
  `ghcr.io/opentofu/opentofu` (the Docker Hub name never existed) and it idles
  with `sleep infinity` as the entrypoint.
- v1: fixes found by the end-to-end test: skmon's deploy script only found its
  files in prod; skform's compose was invalid YAML with `SKSTOR_ENABLED` false.
- v1: new tests: deploy scripts look where playbooks write; compose templates
  render to valid YAML the way Ansible renders them.

Instance action: skmon needs `GRAFANA_ADMIN_PASSWORD`, `GRAFANA_SECRET_KEY`
and the other `skmon.*` keys listed in its playbooks; move bespoke Prometheus
jobs into `EXTRA_SCRAPE_CONFIGS`. skform needs its `skform.*` keys.

## skstacks-v2.14.0 - 2026-09-25

- v1: **skmem-pg** (Postgres 17 + pgvector + pg_search + Apache AGE) published,
  with dev and staging playbooks authored to match prod. Its init SQL is a
  template: set `skmem_pg_default_agent` / `skmem_pg_graph` (defaults
  `default` / `agent_knowledge`).
- v1: **skreg** (Docker Registry v2) published. It now deploys from one
  selected manager like every other service (it ran on every manager at once
  and raced itself on the shared `/var/data`).
- v1: secrets fail closed. No secret-looking variable may fall back to a
  literal default (new test). `skreg`'s `vault_skreg_registry_http_secret`
  and `skpdf`'s `SECURITY_INITIAL_PASSWORD` (when login is enabled) are now
  required.
- v1: tests require every deploy playbook to select one manager.
- skred: the denylist no longer reads a git worktree's `.git` pointer file
  (it broke the pre-push hook in worktrees).

Instance action: set `skmem_pg_password`, `vault_skreg_registry_http_secret`,
and `skpdf.SECURITY_INITIAL_PASSWORD` if skpdf login is enabled; shared
`/var/data` as for the other v1 services.

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
