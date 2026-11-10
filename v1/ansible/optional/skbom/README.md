# skbom

**Deploys [OWASP Dependency-Track](https://github.com/DependencyTrack/dependency-track) (Apache-2.0) on the swarm: an SBOM store that matches every uploaded SBOM continuously against NVD/GHSA/OSV.** This is the image-patching half of the wider [skpatch program](../../../../docs/skpatch-program.md); the host-patching half (Linux package updates over SSH) is the separate `skpatch-hosts` role. **Maturity tier:** T0, Classical (see "Honest claims"). **Version phase:** framework `v1` tree (see this repo's own [Versions table](../../../../README.md#versions): "Published, one service at a time", not the generic ecosystem `VERSION_LIFECYCLE` "Legacy" phase).

Opt-in: nothing renders or changes for an instance that never runs
`deploy_skbom-<env>.yml`.

## Quickstart

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skbom/deploy_skbom-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Full detail (build, test, deploy, configuration, API, troubleshooting) is
in **[SOP.md](./SOP.md)**.

## Upstream, license and image

- **License.** Dependency-Track itself is Apache-2.0: both images are
  vendored by reference only, nothing is rebuilt.
- **Images.** `dependencytrack/apiserver` and `dependencytrack/frontend`,
  release **`4.14.6`** (the latest release on the 4.x line; v5 is a
  separate, incompatible major line upstream maintains in parallel, see
  "Why 4.x, not 5.x" below), both multi-arch and pinned by the
  manifest-list digest. Postgres is `postgres:17.7-alpine`, the exact
  same literal [skflow](../skflow/) defaults to, so the two roles share a
  layer on any node that runs both; the dump sidecar uses `postgres:17.7`
  (the Debian variant, because its dump loop needs bash), also matching
  skflow. The play refuses any of the four without `@sha256:`.
- **Why 4.x, not 5.x.** At the time this role was written, Dependency-
  Track 5.x had shipped (5.2.1) alongside 4.x still receiving stable
  releases (4.14.6) and support through v4's documented end-of-life in
  December 2026. 4.x is the conservative choice for a first release of
  this role; a 5.x migration is a separate, deliberate change (new
  architecture, its own upgrade path), not a drive-by version bump.
  `APISERVER_IMAGE`/`FRONTEND_IMAGE` let an instance move to 5.x on its
  own schedule without waiting on this role.
- **Phone-home.** Not audited for this role. Review the instance's own
  Dependency-Track settings after the first deploy if this matters for a
  given estate.

## How this fits the program

`skbom` is the CVE register the wider [skpatch program](../../../../docs/skpatch-program.md)
is built around: a Trivy SBOM scan (planned, an `skflow` `f/skpatch` flow)
uploads a CycloneDX SBOM per image per estate, and Dependency-Track
matches it continuously against NVD/GHSA/OSV instead of a hand-kept
register that can only ever be as current as the last person who updated
it. See `docs/skpatch-program.md` for the cadence/policy table and what
is live today versus still planned (this role itself, and its upstream
images, are live the day it is deployed; the automation that feeds it
SBOMs on a schedule is still planned).

## Design

### Two services, one host, split by path

`apiserver` (the Java API, holds the data and does the CVE matching) and
`frontend` (a static SPA, nginx, that calls the API from the browser) are
two separate Swarm services, not one service proxying to the other
(unlike [skgym](../skgym/), whose `web` container's own nginx config
proxies `/api/` to its `api` sibling internally). Traefik is told about
both directly: `PathPrefix(`/api`)` routes to `apiserver`, everything
else routes to `frontend`, each with its own
`traefik.http.services.<name>.loadbalancer.server.port=8080` label on
its own service. `frontend`'s `API_BASE_URL` points the browser at this
same host, so the browser's own `/api/...` calls land on `apiserver`
through the same router split.

### Data

| Path | What |
|---|---|
| `/var/data/skbom-<env>/apiserver-data` (`APISERVER_DATA_PATH`) | apiserver's `/data`: its own secret/keystore (encrypts any integration credentials it stores) and uploaded plugin state. Owned `1000:1000` (the image runs as uid 1000 directly, no root entrypoint step to chown it itself, confirmed against the live image). |
| `/var/data/runtime/skbom-<env>/postgres` (`POSTGRES_PATH`) | Postgres data: every SBOM, every project, every finding. |
| `/var/data/skbom-<env>/database-dump` (`DUMP_PATH`) | `pg_dumpall` archives for skbackup. |

`/var/data` is shared storage on every node, so any service can land on
any node matching `PLACEMENT_CONSTRAINTS` (default
`node.labels.skbom == true`, **not** the bare `node.role == worker` most
other optional roles default to: this app's apiserver needs a
3-4G-heap-sized node, see "Sizing" below, so it is placed only on a node
an operator has explicitly labelled, not any worker at random). To keep
Postgres on a node's local disk instead, set `POSTGRES_PATH`/`DUMP_PATH`
to local paths **and** `DATA_NODE` to that node; the same rule covers
`APISERVER_DATA_PATH`. The play refuses a local path without `DATA_NODE`
(Swarm would otherwise reschedule apiserver or Postgres onto an empty
directory: apiserver would mint a fresh secret/keystore and Postgres
would `initdb` a fresh, empty cluster, losing every stored finding).

### Sizing

`apiserver`'s default `EXTRA_JAVA_OPTIONS` is `-Xmx3g` and its default
memory limit is `4096M` (reservation `1024M`), sized for an 8GB node with
headroom for Postgres and the frontend alongside it. Label the intended
node `skbom=true` (`docker node update --label-add skbom=true <node>`)
before deploying, or the default placement constraint matches nothing
and the stack never schedules.

### Health checks

- `apiserver`: `GET /health` on `:8080`, unauthenticated. Confirmed
  against a live container (not assumed from documentation, which does
  not cover this): a healthy instance with Postgres reachable answers
  `{"status":"UP","checks":[{"name":"database","status":"UP",...}]}`.
  The apiserver image ships `curl` (Debian trixie base); `/api/version`
  is a second unauthenticated endpoint that also works, used in this
  README's verification command.
- `frontend`: `wget --spider` on `/` (nginx, Alpine base, ships both
  `curl` and `wget`).
- `postgres`: `pg_isready`.

Neither image needed the Node-one-liner workaround
[sksec-web](../sksec-web/) needed for a thin image with no HTTP client at
all; both ship a real one, confirmed by inspecting the actual images, not
assumed from either role's own precedent.

### OIDC login (optional, off by default)

Dependency-Track has its own OIDC login (not a forward-auth proxy like
most dashboards in this catalog): set `OIDC_ENABLED: true` plus
`OIDC_ISSUER` and `OIDC_CLIENT_ID` (one Authentik OIDC application,
public/SPA client, trusted by both `apiserver` and `frontend` with the
same issuer and client id). The play refuses `OIDC_ENABLED: true` with
either of those two empty; it does not refuse enabling OIDC alongside the
local username/password login, which Dependency-Track keeps available
side by side (same as most of its own documented deployments). Without
`OIDC_ENABLED`, the app's own local admin login is the only path in.

### Backups

`postgres-db-backup` runs `pg_dumpall` every `BACKUP_FREQUENCY` (seconds,
default 86400) into `DUMP_PATH`, the identical dump loop skflow's own
sidecar runs (temp file renamed only on success, pruned only after a good
dump). Point skbackup at `/var/data/skbom-<env>/database-dump`. The
`apiserver-data` directory (the secret/keystore) is not covered by this
loop; add it to skbackup separately if an instance wants it recoverable
without regenerating a fresh keystore (which, note, makes any previously
stored integration credential unreadable, the same risk skpatch-hosts's own
`LUDASH_ENCRYPTION_KEY` carries).

## Configuration (vault)

`skbom:` in `optional/group_vars/<env>/skbom-<env>-<domain dashed>-<cluster>_vault.yml`
(or `skbom-<env>_vault.yml`). Every key this role reads; SOP.md §6 carries
the same block alongside the build/test/deploy/troubleshooting procedure.

```yaml
skbom:
  # REQUIRED (no default)
  DB_PASSWORD: ""                           # 16+ chars; Postgres password. Secret.

  # optional
  CLUSTERNAME: cluster1                     # default: inventory cluster_name
  DOMAIN: example.com                       # default: inventory domain
  CLOUDFLARED: false                        # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                              # public host; default skbom[-dev|-staging].<base domain>
  APISERVER_IMAGE: ""                       # default dependencytrack/apiserver:4.14.6@sha256:...; must carry @sha256:
  FRONTEND_IMAGE: ""                        # default dependencytrack/frontend:4.14.6@sha256:...; must carry @sha256:
  POSTGRES_IMAGE: ""                        # default postgres:17.7-alpine@sha256:...; must carry @sha256: (same literal as skflow's default)
  POSTGRES_BACKUP_IMAGE: ""                 # dump sidecar, default postgres:17.7@sha256:...; must carry @sha256:
  DB_USERNAME: dtrack                       # Postgres role name AND ALPINE_DATABASE_USERNAME
  DB_NAME: dtrack                           # Postgres database name
  EXTRA_JAVA_OPTIONS: "-Xmx3g"               # apiserver JVM heap; the target node has 8GB
  LOGGING_LEVEL: INFO                       # apiserver LOGGING_LEVEL
  TZ: UTC
  # OIDC login (Dependency-Track's own, not a forward-auth proxy) - OFF by
  # default. All-or-nothing: OIDC_ENABLED: true needs OIDC_ISSUER and
  # OIDC_CLIENT_ID both set, or the deploy refuses.
  OIDC_ENABLED: false
  OIDC_ISSUER: ""                           # e.g. https://sso.example.com/application/o/skbom/
  OIDC_CLIENT_ID: ""                        # one Authentik OIDC app (public/SPA client), used by apiserver AND frontend
  OIDC_USERNAME_CLAIM: preferred_username    # apiserver ALPINE_OIDC_USERNAME_CLAIM
  OIDC_TEAMS_CLAIM: groups                  # apiserver ALPINE_OIDC_TEAMS_CLAIM
  OIDC_USER_PROVISIONING: true              # apiserver ALPINE_OIDC_USER_PROVISIONING
  OIDC_TEAM_SYNCHRONIZATION: false          # apiserver ALPINE_OIDC_TEAM_SYNCHRONIZATION
  OIDC_LOGIN_BUTTON_TEXT: "Login with SSO"  # frontend's login button label
  EXTRA_ENV: {}                             # extra apiserver env vars (map), written to apiserver.env
  PLACEMENT_CONSTRAINTS: ["node.labels.skbom == true"]   # NOT node.role == worker; label a node first
  DATA_NODE: ""                             # required when APISERVER_DATA_PATH/POSTGRES_PATH/DUMP_PATH are outside /var/data
  APISERVER_DATA_PATH: ""                   # default /var/data/skbom-<env>/apiserver-data
  POSTGRES_PATH: ""                         # default /var/data/runtime/skbom-<env>/postgres
  DUMP_PATH: ""                             # default /var/data/skbom-<env>/database-dump
  TRAEFIK_MIDDLEWARES: ""                   # default default-no-error-pages@file (both routers)
  TLS_OPTIONS: ""                           # default intermediate@file; "none" = no tls.options label
  APISERVER_CPUS: "2.00"
  APISERVER_MEMORY_LIMIT: 4096M
  APISERVER_MEMORY_RESERVATION: 1024M
  FRONTEND_REPLICAS: 1
  FRONTEND_CPUS: "0.50"
  FRONTEND_MEMORY_LIMIT: 256M
  FRONTEND_MEMORY_RESERVATION: 64M
  POSTGRES_CPUS: "1.00"
  POSTGRES_MEMORY_LIMIT: 1024M
  POSTGRES_MEMORY_RESERVATION: 256M
  HEALTH_CHECK_INTERVAL: 30s
  HEALTH_CHECK_TIMEOUT: 10s
  HEALTH_CHECK_RETRIES: 3
  HEALTH_CHECK_START_PERIOD: 90s            # apiserver's first boot runs schema migrations
  BACKUP_FREQUENCY: 86400                   # seconds between dumps
  BACKUP_KEEP_DAYS: 7                       # BACKUP_NUM_KEEP is accepted as a fallback
  LOG_MAX_SIZE: 10m
  LOG_MAX_FILES: 3
  AUTHENTIK_TILE: true                      # false opts out of the portal tile
  AUTHENTIK_TILE_SLUG: ""                   # default skbom[-env]
  AUTHENTIK_TILE_NAME: "SKBom (Dependency-Track)"
  AUTHENTIK_TILE_GROUP: "skadmin"
  AUTHENTIK_TILE_ICON: "fa://fa-shield-halved"
  AUTHENTIK_TILE_ICON_FILE: "icon.png"
  AUTHENTIK_TILE_ICON_NAME: "skbom"
  AUTHENTIK_TILE_OPEN_IN_NEW_TAB: true
  AUTHENTIK_TILE_DESCRIPTION: ""
```

## Related projects / See also

- ⬆️ **Depends on:** [skfence / skfenceha](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skfence), Traefik ingress every router here assumes.
- ⬇️ **Part of:** [the skpatch program](../../../../docs/skpatch-program.md): this role is the image-patching half's CVE register; the planned `skflow` `f/skpatch` flows feed it SBOMs on a schedule.
- ↔️ **Sibling:** [skpatch-hosts](../skpatch-hosts/), the host-patching half of the same program.
- ↔️ **Sibling:** [skflow](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skflow), Windmill, the engine the program's `f/skpatch`/`f/skrelease`/`f/ops` flows run on; this role's Postgres image default is copied from it on purpose.
- ↔️ **Sibling:** [skgym](../skgym/), another two-image role behind one host, routed differently: its `web` proxies to `api` internally, this role's Traefik routes to both services directly.
- 📐 **Standards:** [sk-standards](https://github.com/smilinTux/sk-standards), the doc/SOP, architecture & data-flow, and cryptography standards this README and its SOP follow.

*License: this role is Apache-2.0 (the framework's own license). The
upstream application it deploys, Dependency-Track, is also Apache-2.0,
referenced by image reference only: nothing from it is vendored.*
