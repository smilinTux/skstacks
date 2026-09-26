# SKStacks v1 Service Catalog

v1 is the Docker Swarm + Ansible framework the current production clusters run
on (see `v1/README.md` for the instance contract: inventory, vault layout,
`/var/data` requirement). It grew from a handful of services to **30** across
four tiers of work landing in quick succession, so this catalog is the single
place to see what exists, what it needs, and how to deploy it.

Every service is deployed the same way:

```bash
ansible-playbook -i envs/<env>/inventory.ini \
  framework/v1/ansible/<tier>/<service>/deploy_<service>-<env>.yml \
  -e target_manager_group=<manager-group> \
  --vault-password-file ~/.vault_pass_env/.<instance>_<env>_vault_pass
```

- `<tier>` is `core` or `optional` (see the table).
- `<manager-group>` is `swarm_managers` for nearly every service; `skfenceha`
  and `skha` need an instance-specific manager group instead (see their own
  sections) because they run per-host or per-tier, not on one selected
  manager.
- A service's vault file lives at
  `<skstacks_vault_dir>/<tier>/group_vars/<env>/<service>-<env>-<domain with
  dashes>-<cluster>_vault.yml` (falling back to `<service>-<env>_vault.yml`),
  per `v1/README.md`'s "Instance contract".
- `/var/data` must be shared storage across every Swarm node (NFS or
  equivalent). See `v1/README.md`.

"First released" below cites the `skstacks-vX.Y.Z` tag from `CHANGELOG.md`
that first published the service. Entries marked **pending** are on this
branch's `## Unreleased` CHANGELOG section and have not shipped in a numbered
tag yet.

## At a glance

| Service | Tier | What it is | Upstream (pinned) | Depends on | Exposes | Storage under `/var/data` | Required vault vars | Notable opt-in flags | First released |
|---|---|---|---|---|---|---|---|---|---|
| [skfence](#skfence) | core | Single-node Traefik edge (reverse proxy/TLS) | Traefik `v3.6.2` | none | `:80`/`:443`, `:8082` ping, dashboard | `runtime/skfence-<env>/{acme,certs}`, `config/skfence-<env>` | `CLOUDFLARE_EMAIL`+token (if ACME on) | `ACME_ENABLED` (false) | pending |
| [skfenceha](#skfenceha) | core | HA multi-manager Traefik edge | Traefik `v3.6.2` | none (alt. to skfence) | `:80`/`:443`/`:222`(ssh)/`:8082`, ACME host `:8080` | `runtime/skfenceha-<env>/{acme,certs}` | `CLOUDFLARE_EMAIL`+token (if ACME on) | `CROWDSEC_ENABLED` (false), `RATE_LIMIT_ENABLED` (true) | v2.19.0 |
| [skha](#skha) | core | Keepalived VRRP floating VIP | keepalived (apt, unpinned) | none | VIP only, no container ports | none (host config) | `vip`, `auth_pass` | `health_checks` list | pending |
| [sksec](#sksec) | core | CrowdSec agent + Traefik bouncer | crowdsec `v1.6.11` + bouncer `0.5.0` | skfence or skfenceha | forwardAuth middleware only, no public router | `config/sksec-<env>/` | `CROWDSEC_AGENT_HOST`, `COLLECTIONS` | `TRAEFIK_HA_MODE` (false) | v2.19.0 |
| [skboard](#skboard) | optional | Vikunja task board (sqlite) | Vikunja `0.24.6` | none | `skboard[-env].<cluster>.<domain>` | `skboard-<env>/data` | `JWT_SECRET`, `CLUSTERNAME`, `DOMAIN` | mailer (off) | v2.19.0 |
| [skbook](#skbook) | optional | BookStack wiki + MariaDB | BookStack `v26.09-ls285`, MariaDB `10.11` | none | `skbook[-env].<cluster>.<domain>` | `skbook-<env>/bookstack/config`, `runtime/skbook-<env>/db` | `MYSQL_ROOT_PASSWORD`, `DB_USERNAME`/`DB_PASSWORD`, `APP_KEY` | `ENABLE_SSO` (false) | v2.19.0 |
| [skdash](#skdash) | optional | Dashy service dashboard, Traefik-API discovery | Dashy `4.7.10` | none | `skdash[-env].<cluster>.<domain>` | `skdash-<env>/config` | `CLUSTERNAME`, `DOMAIN`; `discovery.traefik_api_url`/`_token` if discovery on | `discovery.enabled` (true) | pending |
| [skdesk](#skdesk) | optional | RustDesk relay (hbbs/hbbr) | rustdesk-server (digest-pinned) | none | host ports 21115-21119 (host networking) | `skdesk-<env>/data` | `PLACEMENT_HOSTNAME` | `ENCRYPTED_ONLY` (true) | v2.19.0 |
| [skform](#skform) | optional | OpenTofu CLI container (idle, `docker exec`-driven) | OpenTofu `1.10.7` | skstor (only if remote state) | none (no Traefik) | `skform-<env>/{state,workspaces,plugins}` | `STATE_BACKEND_TYPE`/`PATH`, `CLUSTERNAME`, `DOMAIN` | per-provider `PROVIDER_*_ENABLED` | v2.15.0 |
| [skgallery](#skgallery) | optional | Immich photo/video management | Immich `v2.7.5` + pgvector/vectorchord Postgres | none | `skgallery[-env].<cluster>.<domain>` | `skgallery-<env>/{upload,thumbs,profile,model-cache}` | `DB_PASSWORD`, `CLUSTERNAME`, `DOMAIN` | `OAUTH_ENABLED`/`SMTP_ENABLED` (false) | pending |
| [skgit](#skgit) | optional | Forgejo git + CI runners | Forgejo `15.0.9`, runner `11.1.2` | none | `skgit[-env].<cluster>.<domain>`, ssh `:222` | `skgit-<env>/{data,config}`, `runtime/skgit-<env>/{dind-data,runner-data}` | `POSTGRES_PASSWORD`, `SHARED_SECRET`, `SECRET_KEY`, `ADMIN_EMAIL`/`PASSWORD` | Actions runner only if `SKGIT_DIND_NODE` set | pending |
| [skgraph](#skgraph) | optional | FalkorDB graph database (Redis protocol) | FalkorDB `v4.18.8` | none | `:16379` via ingress mesh (Redis protocol, no UI) | `skgraph-<env>/data` | `FALKORDB_PASSWORD`, `CLUSTERNAME`, `DOMAIN` | none notable | **v2.11.0** |
| [skhub](#skhub) | optional | Nextcloud file sync/collaboration | Nextcloud `31.0.14`, MariaDB `10.11.7` | skstor (only if `storage_backend: skstor`) | `skhub[-env].<cluster>.<domain>` | `skhub-<env>/{data,html,config}` | `nextcloud_admin_user/_password`, `mysql_*`, `CLUSTERNAME`, `DOMAIN` | `storage_backend` (local), `enable_collabora`/`enable_talk_hpb` (false) | pending |
| [skmail](#skmail) | optional | Full SMTP/IMAP mail server | docker-mailserver `16.0.1` | skfence/skfenceha (for ACME cert) | host ports 25/587/465/143/993 | `skmail-<env>/docker-data/dms/{maildata,mailstate}` | `CLUSTERNAME`, `DOMAIN` | `DMS_VERSION` (16.0.1), Rspamd/ClamAV/Fail2ban (all on) | v2.19.0 |
| [skmem-pg](#skmem-pg) | optional | Postgres 17 + pgvector + pg_search + AGE | Postgres 17 custom image `pg17-bm25-age` | none | internal only, no Traefik | `runtime/skmem-pg-<env>/postgres` | `skmem_pg_password` | `skmem_pg_default_agent`/`skmem_pg_graph` | v2.14.0 |
| [skmesh](#skmesh) | optional | Netbird self-hosted WireGuard mesh VPN | netbird `0.79.0` (mgmt/signal/relay) | sksso (OIDC app) | `skmesh.<cluster>.<domain>` (path-routed) | `skmesh-<env>/database-dump` | `NETBIRD_*` secrets, `POSTGRES_PASSWORD`, `SSO_HOST_IP` | `DEPLOY_COTURN` (false) | v2.19.0 |
| [skmon](#skmon) | optional | Observability stack (metrics/logs/tracing) | Prometheus `v3.15.0`, Grafana `13.2.2`, Loki `3.5.4`, Jaeger `1.76.0` | none | `prometheus\|skmon\|alertmanager\|jaeger[-env].<cluster>.<domain>` | `skmon-<env>/{prometheus,grafana,loki-data,...}` | `GRAFANA_ADMIN_USER/_PASSWORD`, `GRAFANA_SECRET_KEY`, `CLUSTERNAME`, `DOMAIN` | `SMTP_ENABLED` (false), `EXTRA_SCRAPE_CONFIGS` | v2.15.0 |
| [skorch](#skorch) | optional | n8n workflow automation | n8n (digest-pinned) + pgvector Postgres | none | `skorch[-env].<cluster>.<domain>` | `skorch-<env>/{n8n_data,database-backup,gpg-keys}` | `POSTGRES_USER/_PASSWORD/_DB`, `REDIS_PASSWORD` | `enable_gpg_signer` (false) | v2.18.0 |
| [skpdf](#skpdf) | optional | Stirling-PDF toolkit | Stirling-PDF `2.14.3` | none | `skpdf[-env].<cluster>.<domain>` | `skpdf-<env>/{trainingData,customFiles}` | `CLUSTERNAME`, `DOMAIN`; `SECURITY_INITIAL_PASSWORD` if login on | `SECURITY_ENABLELOGIN` (false) | v2.13.0 |
| [skpeek](#skpeek) | optional | SearXNG metasearch engine | SearXNG (digest-pinned) + Valkey `8-alpine` | none | `skpeek[-env].<cluster>.<domain>` | `skpeek-<env>/etc` | `SECRET_KEY` | none notable | v2.16.0 |
| [skport](#skport) | optional | Portainer CE (Swarm web UI) | Portainer CE `2.45.1` | skfence or skfenceha (socket-proxy) | `skport[-env].<cluster>.<domain>` | `skport-<env>/data` | `CLUSTERNAME`, `DOMAIN` | none (no baked admin password) | v2.19.0 |
| [skpulse](#skpulse) | optional | Uptime Kuma status monitoring | Uptime Kuma `2.5.5` | none | `skpulse[-env].<cluster>.<domain>` | `skpulse-<env>/uptime-kuma` | `CLUSTERNAME`, `DOMAIN` | manual `discover-traefik-frontends.sh` helper | v2.16.0 |
| [skreg](#skreg) | optional | Docker Registry v2 | `registry:2` (floating) | none | `:5000` (no Traefik/TLS) | `skreg-<env>/data` | `vault_skreg_registry_http_secret` | none | v2.14.0 |
| [skseek](#skseek) | optional | Perplexica AI search UI | Perplexica (digest-pinned) | **skpeek** (must be deployed first, same env) | `skseek[-env].<cluster>.<domain>` | `skseek-<env>/data` | none framework-required (model keys all opt-in) | model provider keys (all off) | v2.19.0 |
| [sksso](#sksso) | optional | Authentik OIDC identity provider | Authentik `2024.4.2` | none | `sso[-env].<cluster or bare>.<domain>` | `sksso-<env>/media`, `runtime/sksso-<env>/postgres` | `postgres_user/_password`, `authentik_secret_key`, `CLUSTERNAME`, `DOMAIN` | `csrf_extra_origins` ([]) | pending |
| [skstor](#skstor) | optional | Garage S3-compatible object storage | Garage `v2.4.1` (digest-pinned) | none (consumed by skhub/skform/skbackup) | `skstor[-env].<cluster>.<domain>`, S3 API `:3900` | `skstor-<env>/{meta,data}` | `rpc_secret`, `CLUSTERNAME`, `DOMAIN` | `REPLICATION_FACTOR` (1), `db_engine` (sqlite) | v2.18.0 |
| [sksync](#sksync) | optional | Syncthing continuous file sync | Syncthing `2.0.13` (hardcoded) | none | `sksync[-env].<cluster>.<domain>`, `:22000` tcp/udp | `sksync-<env>/sync-data` | `SYNCWALLET`, `ENCRYPTION_TOKEN`, `UUID`, `TZ` | none | v2.16.0 |
| [skvector](#skvector) | optional | Qdrant vector database | Qdrant `v1.17.0` | none | `skvector[-env].<cluster>.<domain>` (HTTP+gRPC) | `skvector-<env>/{storage,snapshots}` | `CLUSTERNAME`, `DOMAIN` | `QDRANT_API_KEY` (unset = no auth) | v2.12.0 |
| [skwhoami](#skwhoami) | optional | traefik/whoami echo/test service | traefik/whoami `v1.12.0` | none | 3x `skwhoami0{1,2,3}[-env].<cluster>.<domain>` | none (stateless) | `CLUSTERNAME`, `DOMAIN` | none | v2.19.0 |
| [skbackup](#skbackup) | optional | Duplicati framework-wide backup | linuxserver/duplicati `v2.4.0.0` (tag+digest) | none (destinations often point at skstor) | `skbackup[-env].<cluster>.<domain>` | reads `/var/data` read-only; `config/skbackup-<env>/jobs` | `settings_encryption_key`, `ui_password`, `CLUSTERNAME`, `DOMAIN` | `skbackup.jobs` ([]), `ACME_ENABLED` (false) | v2.19.0 |

---

## Core services

### skfence

Single-node Traefik edge: reverse proxy, TLS termination, a
`docker-socket-proxy` sidecar (Traefik never touches `docker.sock` directly),
`certs-dumper`, and branded error pages. Every other service's Traefik labels
assume one of skfence or skfenceha is running.

**Prerequisites**: none. Deploy this (or skfenceha) first on a new cluster.

**Minimal vault snippet** (`core/group_vars/prod/skfence-prod-<domain>-<cluster>_vault.yml`):

```yaml
skfence:
  ACME_ENABLED: false            # true for real Let's Encrypt certs
  # CLOUDFLARE_EMAIL: "changeme@example.com"   # required only if ACME_ENABLED: true
  # CLUSTERNAME / DOMAIN fall back to the inventory's own cluster_name/domain
```

**Deploy**:

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/core/skfence/deploy_skfence-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

**Health check**: `curl http://<manager-ip>:8082/ping`; certs-dumper carries
its own container healthcheck.

**Gotchas**: single replica, so there is exactly one ACME lease per cluster.
Run skfenceha instead on a multi-manager cluster that needs HA. Rate limiting
is unconditional (always on) in skfence's own dynamic-middlewares template;
there is no `RATE_LIMIT_ENABLED` toggle here (that knob only exists on
skfenceha). No service README exists yet, though skfenceha's own comments
reference one.

### skfenceha

The HA alternative to skfence for multi-manager clusters: a dedicated
ACME-master Traefik instance plus a `global`-mode worker tier, dynamic
middlewares/TLS, and the same error-pages/certs-dumper sidecars. **Do not run
skfence and skfenceha on the same cluster.**

**Prerequisites**: a multi-manager Swarm cluster; a manager group specific to
this instance (not the shared `swarm_managers`).

**Minimal vault snippet**:

```yaml
skfenceha:
  ACME_ENABLED: false
  RATE_LIMIT_ENABLED: true       # skfenceha defaults this ON (skfence has no such toggle)
  CROWDSEC_ENABLED: false        # true only if sksec's bouncer is deployed on this cluster
  # CLOUDFLARE_EMAIL / vault_skfenceha_cloudflare_dns_token required only if ACME_ENABLED: true
```

**Deploy**:

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/core/skfenceha/deploy_skfenceha-prod.yml \
  -e target_manager_group=<instance>-managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

**Health check**: `curl http://<manager-ip>:8082/ping` against any worker.

**Gotchas**: exactly one manager must carry the `traefik.acme.master=true`
node label. The playbook itself relocates this label to the run's selected
manager and strips it from every other node in `target_manager_group` on
each run, so a manual label never sticks. `cf_token_update.sh` and
`create_user_password.sh` are operator helpers, not part of the deploy
itself. Each tier's overlay subnet is pinned to a distinct `172.16.x.0/24`;
review against any existing cluster allocation before first deploy.

### skha

Keepalived VRRP floating VIP failover. This is the framework's one documented
**per-host exemption**: it runs on every host in `target_manager_group`, not
a single selected manager, because VRRP identity (priority, peers, interface)
is legitimately per-node.

**Prerequisites**: skfence or skfenceha already deployed if you want the
default health check (port 80/443) to mean anything.

**Minimal vault snippet**:

```yaml
skha:
  vip: "203.0.113.10/24"    # REQUIRED, no default
  auth_pass: "changeme"     # REQUIRED, same value on every manager
```

**Deploy**:

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/core/skha/deploy_skha-prod.yml \
  -e target_manager_group=<instance>-managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

**Health check**: `ip -4 addr show <interface>` on each manager. Exactly one
should carry the VIP as a secondary address.

**Gotchas**: the node labeled `traefik.acme.master=true` runs as `state
MASTER` with **no health checks**, so the VIP never migrates off it mid
certificate-renewal; `nopreempt` means a failed-over VIP does not
automatically return once the higher-priority node recovers. See
[skha's own README](../v1/ansible/core/skha/README.md) for the full detail.

### sksec

CrowdSec agent plus a Traefik forwardAuth bouncer: behavioral intrusion
detection and automated blocking for every router that opts a
`crowdsec-bouncer@file` middleware in.

**Prerequisites**: skfence or skfenceha already deployed (sksec auto-detects
which one is running from the live Docker services, falling back to
on-disk config).

**Minimal vault snippet**:

```yaml
sksec:
  APP_ENV: "prod"
  CROWDSEC_AGENT_HOST: "sksec-prod_crowdsec:8080"
  COLLECTIONS: "crowdsecurity/linux crowdsecurity/traefik"
  TRAEFIK_HA_MODE: false   # true only on a skfenceha (multi-node) cluster
```

**Deploy**:

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/core/sksec/deploy_sksec-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

**Health check**: `cscli` commands in `src/sksec/crowdsec_commands`; confirm
the bouncer is registered (`cscli bouncers list`).

**Gotchas**: sksec only stands up the agent and bouncer. To actually put
skfenceha's routers behind it, also set `skfenceha.CROWDSEC_ENABLED: true`
(off by default there). `CROWDSEC_BOUNCER_API_KEY` is auto-generated and
persisted on first deploy if left unset, so re-running the playbook does not
rotate it.

---

## Optional services

### skboard

Vikunja Kanban/task board, single container, sqlite (no database sidecar).

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
skboard:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  JWT_SECRET: "changeme"   # pin this; rotating it invalidates every session
```

**Deploy**: `deploy_skboard-<env>.yml`, standard invocation.

**Health check**: no container healthcheck ships (the image has no
shell/curl). Check `docker service ps <stack>_vikunja` or hit the router URL.

**Gotchas**: single-manager, sqlite-on-shared-storage deploy; after 3
restart failures in 120s Swarm stops retrying and the service sits at 0
replicas until forced. See its [README](../v1/ansible/optional/skboard/README.md).

### skbook

BookStack wiki + MariaDB + a `mysqldump` backup sidecar.

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
skbook:
  MYSQL_ROOT_PASSWORD: "changeme"
  DB_DATABASE: "bookstack"
  DB_USERNAME: "bookstack"      # BookStack's actual .env key
  DB_PASSWORD: "changeme"
  APP_KEY: "base64:changeme"    # generate once, pin it
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  ENABLE_SSO: false              # true needs OIDC_CLIENT_ID/_SECRET/_ISSUER, no defaults
```

**Deploy**: `deploy_skbook-<env>.yml`, standard invocation.

**Health check**: container healthcheck `curl -sf http://localhost/`
(30s/5 retries); db healthcheck `mysqladmin ping`.

**Gotchas**: BookStack reads `/config/.env` as a literal dotenv file, and its
real keys are `DB_USERNAME`/`DB_PASSWORD`. An earlier internal pass used
`DB_USER`/`DB_PASS` and broke DB connectivity entirely (fixed; see
CHANGELOG). See its [README](../v1/ansible/optional/skbook/README.md) before
enabling SSO.

### skdash

Dashy service dashboard with generic Traefik-API discovery (auto-populates
sections from live routers) and a static fallback.

**Prerequisites**: none. Discovery degrades to the static `sections` list
if its Traefik API target is unreachable.

**Minimal vault snippet**:

```yaml
skdash:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  discovery:
    enabled: true
    traefik_api_url: "https://skfence.<cluster>.example.com"
    traefik_api_token: ""     # set "" explicitly if the API needs no auth
    exclude_patterns: ["^skdash"]
    group_map:
      - { pattern: "skfence|skfenceha|traefik", group: "Core Infrastructure" }
```

**Deploy**: `deploy_skdash-<env>.yml`, standard invocation.

**Health check**: container healthcheck (`node /app/services/healthcheck`).

**Gotchas**: no service README yet, but the design is fully documented in
[`v1/docs/skdash-discovery.md`](../v1/docs/skdash-discovery.md), including the
exact vault block to reproduce a fully-discovered dashboard.

### skdesk

Self-hosted RustDesk relay (hbbs ID/rendezvous server + hbbr relay).

**Prerequisites**: a specific node to pin it to (RustDesk's protocol needs
real host ports, so this runs on Swarm's built-in `host` network via a
placement constraint, not an overlay network).

**Minimal vault snippet**:

```yaml
skdesk:
  PLACEMENT_HOSTNAME: "<node-hostname>"   # REQUIRED, no default
  ENCRYPTED_ONLY: "1"
```

**Deploy**: `deploy_skdesk-<env>.yml`, standard invocation.

**Health check**: `docker service ps <stack>_hbbs`; no container healthcheck.

**Gotchas**: uniquely excluded from the framework's unique-subnet test (it
has no overlay network at all). Its compose image line now reads the
namespaced `skdesk.IMAGE_TAG`, fixed in v2.19.0 (a bare, unnamespaced var
was silently ignoring per-instance overrides before that).

### skform

An idle OpenTofu (Terraform-compatible) CLI container, driven by `docker
exec`, not a web service.

**Prerequisites**: none, unless using skstor for remote state.

**Minimal vault snippet**:

```yaml
skform:
  STATE_BACKEND_TYPE: "local"   # or "s3" against skstor
  STATE_BACKEND_PATH: "/state"
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  APP_ENV: "prod"
  SKSTOR_ENABLED: false
```

**Deploy**: `deploy_skform-<env>.yml`, standard invocation.

**Health check**: `docker exec <container> tofu version`.

**Gotchas**: unlike most other services, its required-looking vars have no
explicit fail-closed guard (`fail:`/`assert:`). An incomplete vault renders
blank values into `skform.env` instead of stopping the deploy (see
Inconsistencies below). Every cloud-provider credential block is opt-in via
its own `PROVIDER_*_ENABLED` flag.

### skgallery

Immich photo/video management: server, ML worker, pgvector/vectorchord
Postgres, Redis, and a `postgres-backup` sidecar.

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
skgallery:
  DB_PASSWORD: "changeme"
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  OAUTH_ENABLED: false
  SMTP_ENABLED: false
```

**Deploy**: `deploy_skgallery-<env>.yml`, standard invocation.

**Health check**: container healthchecks on all four core services (server
`curl /api/server/ping`, ML `:3003/ping`, redis `redis-cli ping`, postgres
`pg_isready`).

**Gotchas**: the only service in the catalog whose playbook installs
`python3-pip` + the Docker SDK on the target host (needed to query service
info post-deploy). Restart windows are long (600s, up to 20 attempts) to
tolerate slow ML/DB cold starts.

### skgit

Forgejo (Git + CI) with opt-in Actions runners via Docker-in-Docker.

**Prerequisites**: a specific node if you want CI runners (DIND needs
node-local storage, not shared `/var/data`).

**Minimal vault snippet**:

```yaml
skgit:
  POSTGRES_PASSWORD: "changeme"
  SHARED_SECRET: "changeme"
  SECRET_KEY: "changeme"
  INTERNAL_TOKEN: "changeme"
  LFS_JWT_SECRET: "changeme"
  OAUTH2_JWT_SECRET: "changeme"
  ADMIN_EMAIL: "admin@example.com"
  ADMIN_PASSWORD: "changeme"
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  # DIND_NODE: "<node-hostname>"   # unset = runners skip startup (fail-safe)
```

**Deploy**: `deploy_skgit-<env>.yml`, standard invocation.

**Health check**: compose healthcheck `curl http://localhost:3000/`; for
runners, `docker exec skgit-<env>-dind docker info`.

**Gotchas**: `SKGIT_DIND_NODE` must be set to the one node that should run
DIND + runners; leaving it unset is a deliberate fail-safe skip, not a bug. A
`dind-cleanup` sidecar prunes every 2h to keep DIND's node-local root
filesystem from filling.

### skgraph

FalkorDB (Redis-protocol graph database). No web UI, no Traefik routing.

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
skgraph:
  FALKORDB_PASSWORD: "changeme"
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  APP_ENV: "prod"
```

**Deploy**: `deploy_skgraph-<env>.yml`, standard invocation.

**Health check**: `redis-cli -a <password> -p 16379 ping` from any node (it
is published via the Swarm ingress mesh, reachable at `<any-node-ip>:16379`).

**Gotchas**: bind-mounts at the image's real data directory,
`/var/lib/falkordb/data` (not `/data`). A documented trap in the template
itself. The only access control is the Redis password; firewall the
published port if it shouldn't be public.

### skhub

Nextcloud file sync/collaboration: Nextcloud, MariaDB, Redis, ClamAV,
notify_push, whiteboard, imaginary, and a db-backup sidecar.

**Prerequisites**: skstor already deployed, only if using
`storage_backend: skstor`.

**Minimal vault snippet**:

```yaml
skhub:
  nextcloud_admin_user: "admin"
  nextcloud_admin_password: "changeme"
  mysql_root_password: "changeme"
  mysql_user: "nextcloud"
  mysql_password: "changeme"
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  storage_backend: "local"   # or "skstor" (see below) or "s3"
  enable_collabora: false
  enable_talk_hpb: false
```

**Deploy**: `deploy_skhub-<env>.yml`, standard invocation.

**Health check**: compose healthcheck `curl -sf http://localhost/status.php`.

**Gotchas**: `storage_backend: skstor` is a first-install-only shortcut.
Nextcloud only reads `OBJECTSTORE_S3_*` on first install, so switching it on
a running instance is **not supported**. See the
[README](../v1/ansible/optional/skhub/README.md) for the full storage-backend
matrix.

### skmail

Full SMTP/IMAP mail server: docker-mailserver + Rspamd + ClamAV + Fail2ban.

**Prerequisites**: skfence or skfenceha already deployed (skmail reads its
`acme.json` for TLS) and a node labeled `mail-vip` externally (e.g. by a
keepalived notify script) that the playbook only verifies, not sets.

**Minimal vault snippet**:

```yaml
skmail:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  # DMS_VERSION defaults to 16.0.1; set to pin a different docker-mailserver release
```

**Deploy**: `deploy_skmail-<env>.yml`, standard invocation.

**Health check**: compose healthcheck `ss -tlnp | grep ':25'`.

**Gotchas**: needs real host ports (25/587/465/143/993) and matching DNS
(MX/SPF/DKIM) records. It is not just a `docker stack deploy` away from
working mail. docker-mailserver upgrades are never automatic even though
`DMS_VERSION` has a default; bumping it on an existing instance can jump a
major version unexpectedly. See the
[README](../v1/ansible/optional/skmail/README.md).

### skmem-pg

Postgres 17 with pgvector, ParadeDB `pg_search` (BM25 full-text) and Apache
AGE (graph) extensions. The shared "skmemory" store.

**Prerequisites**: none.

**Minimal vault snippet** (note: flat, underscore-separated vars, since a dash
in `skmem-pg` cannot be a Jinja attribute name):

```yaml
skmem_pg_password: "changeme"
skmem_pg_default_agent: "default"
skmem_pg_graph: "agent_knowledge"
```

**Deploy**: `deploy_skmem-pg-<env>.yml`, standard invocation.

**Health check**: compose healthcheck `pg_isready -U postgres`; the init SQL
also creates `hybrid_search_docs`/`hybrid_search_memories` functions an
operator can call directly to confirm the extensions loaded.

**Gotchas**: init SQL runs only once on a fresh data volume (standard
Postgres semantics). Re-running the playbook against an existing data dir
does not pick up schema changes.

### skmesh

Netbird self-hosted WireGuard mesh VPN: management, signal, relay, dashboard,
Postgres, and optional bundled coturn, fronted by Traefik and authenticating
against sksso.

**Prerequisites**: an Authentik OIDC application named `skmesh` on this
cluster's sksso instance. **Prod-only**. No dev/staging playbook ships for
this service.

**Minimal vault snippet**:

```yaml
skmesh:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  NETBIRD_AUTH_CLIENT_ID: "<authentik-oidc-client-id>"
  NETBIRD_DATASTORE_ENC_KEY: "changeme"   # openssl rand -base64 32
  NETBIRD_RELAY_AUTH_SECRET: "changeme"   # openssl rand -base64 32
  POSTGRES_PASSWORD: "changeme"
  TURN_PASSWORD: "changeme"
  TURN_SECRET: "changeme"
  NETBIRD_RELAY_ENDPOINT: "rels://skmesh.example.com:443"
  SSO_HOST_IP: "<ip management container uses to reach sksso>"
  DEPLOY_COTURN: false
```

**Deploy**:

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skmesh/deploy_skmesh-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

**Health check**: dashboard reachable at its Traefik hostname; management
gRPC/API responds.

**Gotchas**: **prod-only**, unlike every other optional service. Add this
stack's hostname to `sksso.csrf_extra_origins` if sksso enforces CSRF-trusted
origins, or login redirects are rejected. See the
[README](../v1/ansible/optional/skmesh/README.md).

### skmon

Full observability stack: Prometheus, Grafana, Loki, Promtail, cAdvisor,
node-exporter, Alertmanager, Jaeger.

**Prerequisites**: none. It scrapes and logs the rest of the cluster
automatically via `global`-mode Promtail/cAdvisor/node-exporter.

**Minimal vault snippet**:

```yaml
skmon:
  GRAFANA_ADMIN_USER: "admin"
  GRAFANA_ADMIN_PASSWORD: "changeme"
  GRAFANA_SECRET_KEY: "changeme"
  GRAFANA_ROOT_URL: "https://skmon.example.com"
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
```

**Deploy**: `deploy_skmon-<env>.yml`, standard invocation.

**Health check**: per-component endpoints (`/-/healthy` Prometheus and
Alertmanager, `/api/health` Grafana, `/healthz` cAdvisor).

**Gotchas**: every dashboard hostname sits behind a separate `traefikAuth@file`
basic-auth middleware in addition to Grafana's own login. Operators need
both credentials. Jaeger uses local Badger storage, not safe to scale beyond
one replica. Extra scrape targets go in `skmon.EXTRA_SCRAPE_CONFIGS`, not a
bespoke Prometheus job.

### skorch

n8n workflow automation with an optional detached-PGP-signing sidecar.

**Prerequisites**: none; the gpg-signer sidecar needs pre-provisioned keys in
`GNUPGHOME` if enabled.

**Minimal vault snippet**:

```yaml
skorch:
  POSTGRES_USER: "n8n"
  POSTGRES_PASSWORD: "changeme"
  POSTGRES_DB: "n8n"
  REDIS_PASSWORD: "changeme"
  enable_gpg_signer: false
  # GPG_SIGNER_API_SECRET: "changeme"   # required only if enable_gpg_signer: true
```

**Deploy**: `deploy_skorch-<env>.yml`, standard invocation.

**Health check**: `docker service ls` (n8n itself has no compose-level
healthcheck; only Postgres does). The gpg-signer sidecar exposes
`GET /health`.

**Gotchas**: the gpg-signer's `/import-key` endpoint is gated behind
`GPG_SIGNER_ALLOW_KEY_IMPORT` (default off). Keys are meant to be
pre-provisioned, not imported over the API by default. See
`v1/ansible/optional/skorch/src/gpg-signer/README.md` for the sidecar's own
auth model.

### skpdf

Stirling-PDF: merge, split, OCR, convert, sign.

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
skpdf:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  SECURITY_ENABLELOGIN: false
  # SECURITY_INITIAL_PASSWORD: "changeme"   # required only if SECURITY_ENABLELOGIN: true
```

**Deploy**: `deploy_skpdf-<env>.yml`, standard invocation.

**Health check**: no container-level healthcheck ships. Verify via
`curl -f https://skpdf.<cluster>.<domain>/` or `docker service ls`.

**Gotchas**: `LARGE_FILE_UPLOAD` (default true) drives an upload-buffering
Traefik middleware that must be attached to the router for
`MAX_UPLOAD_SIZE`/`MAX_RESPONSE_SIZE` to actually take effect (a prior gap
here was fixed; see the template's own dated comment).

### skpeek

SearXNG self-hosted metasearch engine, backed by Valkey.

**Prerequisites**: none, though **skseek** depends on this being deployed
first if you plan to run both.

**Minimal vault snippet**:

```yaml
skpeek:
  SECRET_KEY: "changeme"   # REQUIRED, no default (fails closed)
```

**Deploy**: `deploy_skpeek-<env>.yml`, standard invocation.

**Health check**: compose healthcheck `wget --spider http://localhost:8080/`.

**Gotchas**: if you plan to also deploy skseek, do not let skseek's playbook
allocate a fresh overlay subnet for the `skpeek-<env>` network. It must
match this service's own `deploy_skpeek-<env>.yml` allocation exactly.

### skport

Portainer CE, a web UI for the Swarm cluster itself.

**Prerequisites**: skfence or skfenceha already deployed (skport reaches the
Swarm API through the existing socket-proxy service, never a direct
`docker.sock` mount).

**Minimal vault snippet**:

```yaml
skport:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
```

**Deploy**: `deploy_skport-<env>.yml`, standard invocation.

**Health check**: `docker service logs <stack>_portainer`;
`curl -I https://skport.<cluster>.<domain>`.

**Gotchas**: bakes no admin password by design. Portainer's own first-run
setup wizard is interactive and times out after 5 minutes, so complete it
promptly after first deploy. `target_manager_group` has no default in this
playbook; it must always be passed explicitly.

### skpulse

Uptime Kuma status monitoring.

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
skpulse:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
```

**Deploy**: `deploy_skpulse-<env>.yml`, standard invocation.

**Health check**: `docker service ls --filter name=skpulse-<env>_skpulse`
shows `1/1`.

**Gotchas**: admin credentials are set via Uptime Kuma's own first-run web
UI, not vault. A separate `discover-traefik-frontends.sh` helper (not run by
the playbook) can auto-register every Traefik-fronted service as a monitor
via the Uptime Kuma API, given an `UPTIME_KUMA_API_TOKEN`.

### skreg

Docker Registry v2, a private image registry.

**Prerequisites**: none.

**Minimal vault snippet** (flat var name, not namespaced like most services):

```yaml
vault_skreg_registry_http_secret: "changeme"   # REQUIRED, no default
```

**Deploy**: `deploy_skreg-<env>.yml`, standard invocation.

**Health check**: `curl http://<manager-ip>:5000/v2/`.

**Gotchas**: not fronted by Traefik at all. No TLS, no auth beyond the
registry's own token secret, on a raw published port. Every deploy
deliberately destroys and recreates the registry service (`docker service
rm` then redeploy). Expect a brief visible gap in `docker service ls`
during a redeploy.

### skseek

Perplexica: an AI-powered search UI that answers from live search results.

**Prerequisites**: **skpeek must already be deployed in the same
environment**, since skseek reads SearXNG's API and its own overlay subnet
must match skpeek's exactly, not a fresh allocation.

**Minimal vault snippet**:

```yaml
# No framework-required vars; enable at least one model provider or every
# search-with-AI feature is effectively disabled:
skseek:
  MODELS:
    OPENAI_API_KEY: "changeme"   # or GROQ_API_KEY / ANTHROPIC_API_KEY / etc.
```

**Deploy**: `deploy_skseek-<env>.yml`, standard invocation.

**Health check**: container healthcheck `curl -sf http://localhost:3000/`.

**Gotchas**: the skpeek dependency is enforced only by a comment in the
playbook, not a deploy-time check. If skpeek isn't up yet, skseek's
`SEARXNG_API_URL` never resolves and it silently has no search backend.

### sksso

Authentik: OIDC identity provider and forward-auth, server/worker/postgres/redis.

**Prerequisites**: none. Self-contained.

**Minimal vault snippet**:

```yaml
sksso:
  postgres_user: "authentik"
  postgres_password: "changeme"
  authentik_secret_key: "changeme"
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  csrf_extra_origins: []   # add any extra hostname Authentik is reached through
```

**Deploy**: `deploy_sksso-<env>.yml`, standard invocation.

**Health check**: `curl -sf https://sso.<cluster>.<domain>/-/health/live/`.

**Gotchas**: never add the forward-auth middleware to a route that an app's
own native OIDC callback needs. Authentik's own guidance, called out
explicitly in this [README](../v1/ansible/optional/sksso/README.md). Add any
other service's hostname here (e.g. skmesh's) via `csrf_extra_origins` before
that service tries to log in through this instance.

### skstor

Garage, S3-compatible object storage, single node. The framework's answer
after MinIO's upstream died (see
[`docs/decisions/skstor-backend.md`](../docs/decisions/skstor-backend.md)).

**Prerequisites**: none. Consumed by skhub (`storage_backend: skstor`),
skform (remote OpenTofu state) and skbackup (a common destination).

**Minimal vault snippet**:

```yaml
skstor:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  rpc_secret: "changeme"   # openssl rand -hex 32; node-to-node RPC auth, NOT an S3 credential
```

**Deploy**: `deploy_skstor-<env>.yml`, standard invocation (the service's own
README does not show a deploy command; use the framework's standard pattern
above).

**Health check**: container healthcheck runs `garage status`; the deploy
script itself polls for `1/1` replicas and fails if Garage doesn't come up.

**Gotchas**: the one-time single-node cluster layout (`garage layout
assign`/`apply`) **is** automated on first deploy, but bucket and key
creation (`garage key create`, `garage bucket create`, `garage bucket
allow`) is **not**. Each consumer needs a one-time manual bootstrap step per
environment (see the [README](../v1/ansible/optional/skstor/README.md) for
the exact commands). `REPLICATION_FACTOR` defaults to `1`: zero redundancy.

### sksync

Syncthing, continuous file synchronization.

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
sksync:
  SYNCWALLET: "changeme"
  ENCRYPTION_TOKEN: "changeme"
  UUID: "changeme"
  TZ: "UTC"
```

**Deploy**: `deploy_sksync-<env>.yml`, standard invocation.

**Health check**: `curl -I https://sksync.<cluster>.<domain>`; confirm peers
can reach `:22000` tcp+udp.

**Gotchas**: firewall rules for the selected manager are re-applied on every
deploy but not removed from a previously-selected manager after a failover.
Stale open ports can accumulate over time on nodes that used to hold this
service.

### skvector

Qdrant, a vector database for embeddings/similarity search.

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
skvector:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  # QDRANT_API_KEY: "changeme"   # unset = no authentication at all
```

**Deploy**: `deploy_skvector-<env>.yml`, standard invocation.

**Health check**: `curl https://skvector.<cluster>.<domain>/healthz`.

**Gotchas**: with `QDRANT_API_KEY` unset, the instance runs with **no
authentication**. Set it unless the overlay network is the only thing
reaching it. gRPC is on a separate router/port (`:6334`) from the HTTP API
(`:6333`).

### skwhoami

`traefik/whoami`: a trivial HTTP echo service for verifying Traefik + TLS +
service discovery on a fresh cluster.

**Prerequisites**: none.

**Minimal vault snippet**:

```yaml
skwhoami:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
```

**Deploy**: `deploy_skwhoami-<env>.yml`, standard invocation.

**Health check**: `curl -s https://skwhoami01.<cluster>.<domain>` (and
`02`/`03`). Each should echo its own hostname and request headers.

**Gotchas**: deploys three independent instances (`skwhoami01/02/03`) on
unconstrained placement, useful for proving load distribution across nodes,
not just that one router works.

### skbackup

Duplicati: the framework's own backup service. Reads `/var/data` read-only
and ships encrypted, deduplicated snapshots to a destination you control
(often skstor).

**Prerequisites**: none, but a destination (skstor, another S3, SFTP, local
path) you already control.

**Minimal vault snippet**:

```yaml
skbackup:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  settings_encryption_key: "changeme12"   # min 8 alphanumeric chars, no default
  ui_password: "changeme"                  # no default. Upstream's own "changeme" fallback is never used
  jobs: []   # declarative jobs are RENDERED, not imported (see gotcha)
```

**Deploy**: `deploy_skbackup-<env>.yml`, standard invocation.

**Health check**: `duplicati-cli test <destination_url> --passphrase=... all`
verifies a destination end-to-end.

**Gotchas**: `skbackup.jobs` entries are **rendered** to JSON, never
auto-imported. Duplicati has no reliable non-interactive import path, so
each rendered job needs a one-time manual **Import from a file** in the web
UI per instance. By default it backs up other services' own DB dump files
(e.g. skorch's/skgallery's `postgres-backup` output), not their live
database engine storage directly. Copying a live Postgres/MariaDB data
directory mid-write is not a consistent backup. See the
[README](../v1/ansible/optional/skbackup/README.md) for the full exclude-list
and destination-URL conventions.

---

## Inconsistencies found while researching this catalog

These are documentation/config drift found while reading every service's
README, playbooks and templates. Nothing below was changed as part of this
catalog; flagging for a follow-up cleanup pass.

**Fail-open vs. fail-closed `CLUSTERNAME`/`DOMAIN`.** Most services
(skfence, skfenceha, skgit, skgraph, skhub, skmail, skmon, skpdf, skstor,
sksso) render `CLUSTERNAME`/`DOMAIN` with no framework default, so a missing
vault key is a hard, deploy-stopping error. skport, skpulse, skwhoami,
sksync, and skorch/skpeek instead fall back to placeholder values
(placeholder cluster/domain names baked into the template). A forgotten
vault entry there deploys successfully onto a wrong hostname instead of
failing. skdash and skgallery sit in between: they reference their own
`CLUSTERNAME`/`DOMAIN` directly (fail closed) but, unlike most services, do
not fall back to the inventory-level `cluster_name`/`domain` the way
skfence/skfenceha/skform/skdesk/skboard/skbook do.

**Vault variable naming convention breaks in three places.** Nearly every
service namespaces vault vars as `<svc>.<KEY>`. skreg instead uses a flat
`vault_skreg_registry_http_secret`. skmem-pg uses flat `skmem_pg_*` vars
(unavoidable. `skmem-pg`'s hyphen can't be a Jinja attribute name), but its
own network-override line reads the dotted `skmem_pg.networks`, which will
never match its own flat vault convention. That particular override is
effectively dead. skpeek's and skorch's image/tuning vars (`skpeek_image`,
`redis_image`, `uwsgi_workers`) are flat rather than namespaced like every
comparable knob elsewhere.

**Network-override variable naming is inconsistent across services.**
skhub/skmon use a single dotted `<svc>.networks` var across all three envs.
skgraph uses `skgraph.networks` in prod but flat `skgraph_dev_networks`/
`skgraph_staging_networks` in dev/staging. skorch and skpeek use flat
`<svc>_<env>_networks` in all three envs. An operator copying one service's
vault pattern to another will silently get the wrong (default) subnet.

**Deploy-command style diverges from `v1/README.md`'s own documented
pattern** in two service READMEs: skport and skwhoami show
`ansible-playbook -e env=prod -i v1/ansible/shared/hosts ...` (a different
inventory path, no `target_manager_group`, no per-instance vault-file
naming) instead of the framework convention every other README follows.
skstor's README has no deploy-command example at all. This catalog uses the
framework's documented convention consistently for all 30 services above.

**`target_manager_group` has no default in three playbooks**
(skport, skpulse, skwhoami). It must always be passed explicitly with
`-e target_manager_group=...`, unlike the majority which default to
`swarm_managers`.

**Image pinning strategy is inconsistent.** Most services pin a
vault-overridable version var. skreg uses the floating `registry:2` tag with
no digest and no override at all. sksync hardcodes `syncthing/syncthing:2.0.13`
directly in its compose template with no vault-overridable var whatsoever.
skhub's `redis:alpine` is also a floating tag in practice, even though the
CHANGELOG's v2.19.0 entry claims "all remaining floating `:latest` image
refs ... are now pinned". The test that claim references
(`test_no_latest_images.py`) only rejects the literal strings
`latest`/`lts`/`stable`/`release`/`edge`/`main`, so `:alpine` passes the gate
while still being a moving tag.

**skform breaks the framework's fail-closed convention.** Every other
service with genuinely required vars guards them with an explicit
`fail:`/`assert:` task or a no-default Jinja reference. skform's
required-looking vars (`TOFU_VERSION`, `STATE_BACKEND_TYPE`,
`STATE_BACKEND_PATH`, `CLUSTERNAME`, `DOMAIN`, `APP_ENV`) have neither.
An incomplete vault renders blank/undefined values into `skform.env` rather
than stopping the deploy before it starts.

**skfence's rate-limiting default is misdescribed.** The v2.19.0 CHANGELOG
entry says "`skfenceha.RATE_LIMIT_ENABLED` defaults to `true` (skfence
itself defaults it off)". skfence's own dynamic-middlewares template has no
`RATE_LIMIT_ENABLED` toggle at all. Rate limiting is unconditionally on
there. skfence isn't defaulting it off; it simply never gained the on/off
knob skfenceha introduced.

**skgallery's telemetry default contradicts its own inline comment.** The
template comments "OPTIONAL: Disable telemetry (default: true for privacy)"
directly above a line whose actual rendered default is `'all'`. Full
telemetry enabled by default, the opposite of what the comment implies.

**A nonexistent README is cited by name.** Both skfence's and skfenceha's
deploy-playbook headers point to `v1/ansible/core/skfence/README.md` as the
place documenting why the two services aren't interchangeable; that file
does not exist (only each service's `src/error-pages/README.md`, which
documents the shared error-pages sidecar, not the service itself).

**skbackup's CHANGELOG entry mislabels its own tier.** The `## Unreleased
(v2.19.0)` entry reads "v1 core: **skbackup** ... published", but the
service lives at `v1/ansible/optional/skbackup/`, not under `core/`. This
catalog lists it correctly as optional.

**skgit's admin-bootstrap task is mislabeled.** Its task name/comment says
"(DEV/STAGING ONLY)" but the identical task (gated only by
`skgit.admin_enabled | default(false)`) is present verbatim in
`deploy_skgit-prod.yml` too. A documentation mismatch, not a behavior bug,
since the flag still defaults it off everywhere.

**skvector's dev/staging playbooks lag prod slightly.** They use
differently-named network-override facts (`skvector_dev_networks`/
`skvector_staging_networks` vs. prod's `skvector.networks`) and are missing
a `- config` tag prod's compose-template task carries. A residue of an
earlier pass that brought dev/staging only partway to prod parity.

**The top-level `README.md` is stale relative to this catalog.** It still
states "*(v1 is private)* Frozen" under the version table, which no longer
reflects reality now that 30 v1 services are published and actively
developed on `integration/*` branches. Not changed here (out of scope for
this catalog), but worth a follow-up correction.
