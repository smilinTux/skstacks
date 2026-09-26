# Changelog

All notable changes to the SKStacks framework. Tags: `skstacks-vMAJOR.MINOR.PATCH`.
Each entry says what an instance must do, if anything.

## Unreleased (v2.19.0)

- v1 core: **skfenceha** (HA Traefik edge: docker-socket-proxy per manager, worker-tier Traefik, dynamic middlewares/TLS, error pages) published as the HA alternative to skfence. `skfenceha.ACME_ENABLED` defaults to `false` like skfence (self-signed cert); set `true` for real Let's Encrypt certs via Cloudflare DNS-01. `skfenceha.RATE_LIMIT_ENABLED` defaults to `true` (skfence itself defaults it off); CrowdSec bouncer support is opt-in (`skfenceha.CROWDSEC_ENABLED`, default `false`, expects an external bouncer/LAPI reachable as `crowdsec-bouncer@file`). Each tier's `cloud-edge-<env>`/`cloud-socket-proxy-<env>` overlay subnets are pinned to a distinct `172.16.x.0/24` (`cloud-public-<env>` is the existing shared network); an instance whose cluster already uses one of those ranges overrides them via `skfenceha_<env>_networks`, the same override pattern skfence itself uses. Also carries forward the v2.18.0 fix for skfence routers rendering `tls: {}` when ACME is off.
- v1: **skdesk** (RustDesk relay: hbbs/hbbr) and **skseek** (Perplexica) published as optional services. skdesk runs with host networking (RustDesk's relay protocol needs the real host ports, and `docker stack deploy` cannot apply `network_mode` the way `docker run` does), so it is pinned to a single node via placement constraint and restarts unconditionally. skseek depends on **skpeek** (SearXNG) already being deployed in the same environment; its overlay subnet must match skpeek's own `deploy_skpeek-<env>.yml`, not a fresh allocation.
- v1: **skmail** (docker-mailserver + Rspamd + ClamAV + Fail2ban) published as an optional service. `skmail.DMS_VERSION` defaults to `16.0.1` (an instance vault var, not a literal in the compose template), so mail server upgrades are opt-in per instance.
- v1: **skbook** (BookStack + MariaDB) published as an optional, single-manager-deploy service. SSO/OIDC is off by default (`skbook.ENABLE_SSO`, then `skbook.OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`/`OIDC_ISSUER`, no framework defaults); MariaDB creds are `skbook.DB_USERNAME`/`skbook.DB_PASSWORD` (BookStack's actual `.env` keys, not the `DB_USER`/`DB_PASS` pair an earlier pass assumed).
- v1: **skboard** (Vikunja, sqlite, single-manager deploy) published as an optional service.
- v1: **sksso** gains `sksso.csrf_extra_origins` (list, default `[]`) for extra Authentik CSRF trusted origins beyond the instance's own domain, needed when Authentik is reached through more than one hostname.
- v1: **skdesk** compose template's image line now reads from the namespaced `skdesk.IMAGE_TAG` instead of a bare unnamespaced var, so a per-instance override is honoured instead of silently ignored on deploy.
- v1: all remaining floating `:latest` image refs (~22, across skdash, skfence, skfenceha, skform, skgraph, skhub, skmail, skmon, skorch, skpdf, skpeek, skpulse, sksync and skvector) are now pinned to prod-matched versions, or prod's exact digest where no matching tag exists; `v1/tests/test_no_latest_images.py` asserts every remote image carries an explicit tag or digest and rejects moving tags (`latest`, `lts`, `stable`, `release`, `edge`, `main`).
- v1: **skport** (Portainer CE, pinned to `2.45.1`) and **skwhoami** (traefik/whoami, pinned to `v1.12.0`) published as new optional services. skport reaches the Swarm API via the existing socket-proxy service (skfence or skfenceha), never a direct docker.sock mount, and bakes no admin password (first-run setup is interactive). Both need only `CLUSTERNAME`/`DOMAIN` in their vault.
- v1 core: **sksec** (CrowdSec agent + traefik bouncer, pinned to `v1.6.11`/`0.5.0`) and v1 optional **skmesh** (Netbird management/signal/relay/dashboard/coturn + Postgres, ported from the NAM/skstack01 estate) published. `skmesh.DASHBOARD_IMAGE` is an instance override for the dashboard image, defaulting to netbird's dashboard pinned by digest.

Instance action: skfenceha is a drop-in HA alternative to skfence (do not run both on the same cluster); set `ACME_ENABLED: true` for real certs and `CROWDSEC_ENABLED: true` only if the estate runs a CrowdSec bouncer, and review each tier's `cloud-edge-<env>`/`cloud-socket-proxy-<env>` subnet against any existing cluster's own `172.16.x.0/24` allocations before first deploy. skdesk, skseek, skmail, skbook and skboard are all new optional services, each needing its own vault (`v1/group_vars/<env>/<svc>-<env>-<domain-dashed>-<cluster>_vault.yml`) per its README; skseek additionally requires skpeek already deployed in the same environment. skmail defaults to DMS 16.0.1; set `skmail.DMS_VERSION` to pin elsewhere. skbook needs `skbook.DB_USERNAME`/`skbook.DB_PASSWORD` and, only if using SSO, the `OIDC_*` vars from its README. sksso's `csrf_extra_origins` defaults to empty (current behavior unchanged); set it only when Authentik is reached through more than one origin. skdesk IMAGE_TAG override is now honoured; instances that already set `skdesk.IMAGE_TAG` will pick it up on next deploy instead of the framework default. All images are now pinned to an explicit tag or digest and a test rejects any floating/moving tag going forward; no instance action needed unless an instance vault set its own `:latest`/`lts`/`stable`/`release`/`edge`/`main` override, in which case pin it to a real version. skport and skwhoami are new optional services, each needing its own vault (`v1/group_vars/<env>/<svc>-<env>-<domain-dashed>-<cluster>_vault.yml`) with `CLUSTERNAME`/`DOMAIN`; skport additionally requires skfence or skfenceha already deployed for its socket-proxy. sksec and skmesh are new services, each needing their own vault per README; skmesh's Postgres creds and Netbird setup key have no literal defaults. Set `skmesh.DASHBOARD_IMAGE` only to override the pinned dashboard digest.

## Unreleased (v2.18.0)

- v1: **skstor** meta/data now bind-mount onto `/var/data/skstor-<env>/{meta,data}`, the shared filesystem every other v1 service already requires, instead of a named Docker volume that stranded data on whichever manager the replicas=1 Garage service last ran on. `garage.toml.j2` gains `skstor.db_engine` (default `sqlite`, was a hardcoded `lmdb`; Garage's own docs call LMDB "prone to database corruption after an unclean shutdown", which a Swarm reschedule is), and `skstor.meta_dir`/`skstor.data_dir` (default the bind-mount paths above; override to a node-local path plus a placement constraint to trade shared storage for LMDB's speed).
- v1: **skhub** gains `skhub.storage_backend: skstor` as a one-line shortcut for S3 against this cluster's own in-cluster skstor Garage service (host/port/region default to the in-cluster service, only `skhub.s3_bucket_name`/`s3_access_key`/`s3_secret_key` are required), and joins the `skstor-<env>` overlay network only when that backend is selected. Default remains `local` (NFS-backed `/var/data/skhub-<env>/data`, unchanged); the existing free-form `skhub.storage_backend: s3` mode is unchanged.
- v1: **skorch** (n8n + pgvector Postgres + Redis + postgres-backup) published. Its optional gpg-signer sidecar (`skorch.enable_gpg_signer`, default `false`) is a REST API for detached PGP signing with pre-provisioned keys; image defaults to `ghcr.io/smilintux/skstacks-gpg-signer`, built by CI on `skstacks-v*` tags and `workflow_dispatch` (`.github/workflows/gpg-signer-image.yml`). No literal default for `skorch.GPG_SIGNER_API_SECRET` (required only when the sidecar is enabled); its `/import-key` endpoint is gated behind `GPG_SIGNER_ALLOW_KEY_IMPORT` (default off).

Instance action: skstor is new (Garage, data on `/var/data/skstor-<env>`, sqlite metadata, one-time layout bootstrap in deploy); skhub's default storage is unchanged (local), `storage_backend: skstor` is opt-in and first-install only; skorch's gpg-signer is opt-in (`skorch.enable_gpg_signer`, needs trustee keys in `GNUPGHOME` and `GPG_SIGNER_API_SECRET`; the old literal default secret is gone, it fails closed).

## Unreleased

- v1 core: **skfence** (Traefik + docker-socket-proxy + certs-dumper + error pages), the first core-tier service. `skfence.ACME_ENABLED` defaults to `false` (Traefik self-signed cert, works with no public DNS); `true` enables Let's Encrypt via Cloudflare DNS-01 with no literal secret defaults.
- v1 core: **skha** (keepalived VRRP VIP failover). A documented exception to the single-selected-manager rule: it runs per host, marked `# skstacks: per-host` and scoped to core by a test.
- v1: **skdash** (Dashy) with generic Traefik discovery driven by `skdash.discovery.*` instance vars, replacing the estate-specific discovery script.
- v1: **skhub** (Nextcloud + MariaDB + Redis + ClamAV + notify_push + whiteboard + imaginary + db backup). Collabora and Talk HPB are feature flags, default off (they need public DNS + TLS). `skhub.CLUSTERNAME` / `skhub.DOMAIN` are required.
- v1: **skgallery** (Immich + pgvector Postgres + Redis) and **skgit** (Forgejo + Postgres, opt-in Actions runners via Docker-in-Docker).
- v1: **sksso** (Authentik server/worker/postgres/redis).
- v1 tests: no two services share a subnet (`test_unique_subnets.py`, cloud-public-* exempt). sksso moves to 172.16.104/105/106 (prod/staging/dev) and sksync-staging to 172.16.107.
- CI: scheduled image-availability canary (`v1/scripts/list_images.py`, `v1/scripts/check_images.sh`). A Docker Hub `toomanyrequests` is reported as RATE-LIMITED (inconclusive), not a failure.
- v1: **skhub** router now supports `skhub.router_middlewares` (list, default `[]`, appended after the framework's own `skhub,skhub-dav` middlewares on the `skhub-secure` router) and `skhub.tls_options` (string, default unset -> no label).
- v1: **skdash** router gets the same `skdash.router_middlewares` (default `[]`) and `skdash.tls_options` (default unset) hooks on its secure router. Its compose template also gains `restart_policy` (`on-failure`, 10s delay, 5 attempts) and `rollback_config` (parallelism 1, 10s delay), resource limits/reservations moved to `skdash.RESOURCES_LIMITS_CPUS`/`RESOURCES_LIMITS_MEMORY`/`RESOURCES_RESERVATIONS_CPUS`/`RESOURCES_RESERVATIONS_MEMORY` (defaults unchanged: 0.5/512M limits, 0.1/128M reservations), and a sticky-cookie `sameSite` hook via `skdash.sticky_samesite` (default unset -> no label, current framework behavior).
- v1: **skhub** and **skdash** both gain `router_middlewares_pre` (list, default `[]`), prepended before the built-in middlewares on the secure router; `router_middlewares` keeps appending after them. Needed for middleware chains where order matters (e.g. a request-buffering middleware that must run before the app's own headers/rewrite middlewares).
- v1: **skdash** `restart_policy` and `rollback_config` gain more instance-overridable fields: `skdash.restart_policy_condition` (default `on-failure`), `skdash.restart_policy_max_attempts` and `skdash.restart_policy_window` (defaults `5`/`120s`, set to an empty string to omit the key entirely), and opt-in `skdash.rollback_failure_action` / `skdash.rollback_monitor` / `skdash.rollback_max_failure_ratio` (all unset by default, matching current framework behavior of no extra rollback fields).
- v1: **skgit**'s `start-runners.sh` fails closed (skips runner startup, by design) when `SKGIT_DIND_NODE` is unset -- but the dev/staging/prod deploy playbooks called it from a `shell` task without ever setting that variable, so runners silently never started on ANY node regardless of the instance's vault. Each playbook's post-deploy task now passes `SKGIT_DIND_NODE: "{{ skgit.DIND_NODE | default('') }}"` through the task's `environment`.

Instance action: none required for skhub/skdash; all new hooks default to current framework behavior. Set the new vars only where an instance needs a middleware, TLS options, resource sizing, cookie policy, or restart/rollback semantics the defaults do not already provide. For skgit, set `skgit.DIND_NODE` to the single node that should run DIND + runners (empty/unset keeps the existing fail-safe skip).

Instance action: a new service needs its vault (`<tier>/group_vars/<env>/<svc>-<env>-<domain-dashed>-<cluster>_vault.yml`) with the required vars listed in its README. **sksso** and **sksync-staging** instances already deployed on the old subnets must recreate those networks (or pin the old subnet via the playbook's network list in their own fork) before redeploying.

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
