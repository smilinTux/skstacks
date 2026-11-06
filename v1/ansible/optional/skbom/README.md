# skbom

**Deploys [OWASP Dependency-Track](https://github.com/DependencyTrack/dependency-track) (Apache-2.0) on the swarm: an SBOM store that matches every uploaded SBOM continuously against NVD/GHSA/OSV.** This is the image-patching half of the wider [skpatch program](../../../../docs/skpatch-program.md); the host-patching half (Linux package updates over SSH) is the separate `skpatch` role. **Maturity tier:** T0, Classical (see "Honest claims"). **Version phase:** framework `v1` tree (see this repo's own [Versions table](../../../../README.md#versions): "Published, one service at a time", not the generic ecosystem `VERSION_LIFECYCLE` "Legacy" phase).

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
stored integration credential unreadable, the same risk skpatch's own
`LUDASH_ENCRYPTION_KEY` carries).

## Configuration knobs (vault)

The full reference (every key, every default) is SOP §6. The ones that
matter most when reading this role for the first time:

| Key | Default | What it controls |
|---|---|---|
| `CLUSTERNAME` | inventory `cluster_name` | Part of the default host (`skbom[-env].<cluster>.<domain>`), same convention as every other optional role. |
| `DOMAIN` | inventory `domain` | The base domain; see `CLOUDFLARED` to drop the cluster segment. |
| `DB_PASSWORD` | required, no default | Postgres password for the stack's own database. Secret. |
| `PLACEMENT_CONSTRAINTS` | `["node.labels.skbom == true"]` | Where apiserver/frontend/Postgres land. Label a node before deploying. |
| `OIDC_ENABLED` | `false` | Dependency-Track's own OIDC login against Authentik. All-or-nothing with `OIDC_ISSUER`/`OIDC_CLIENT_ID`. |
| `EXTRA_JAVA_OPTIONS` | `-Xmx3g` | apiserver JVM heap; the node this targets has 8GB. |
| `DATA_NODE` | `""` | Required if any of the three data paths is local disk, not shared storage. |

## Related projects / See also

- ⬆️ **Depends on:** [skfence / skfenceha](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skfence), Traefik ingress every router here assumes.
- ⬇️ **Part of:** [the skpatch program](../../../../docs/skpatch-program.md): this role is the image-patching half's CVE register; the planned `skflow` `f/skpatch` flows feed it SBOMs on a schedule.
- ↔️ **Sibling:** [skpatch](../skpatch/), the host-patching half of the same program.
- ↔️ **Sibling:** [skflow](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skflow), Windmill, the engine the program's `f/skpatch`/`f/skrelease`/`f/ops` flows run on; this role's Postgres image default is copied from it on purpose.
- ↔️ **Sibling:** [skgym](../skgym/), another two-image role behind one host, routed differently: its `web` proxies to `api` internally, this role's Traefik routes to both services directly.
- 📐 **Standards:** [sk-standards](https://github.com/smilinTux/sk-standards), the doc/SOP, architecture & data-flow, and cryptography standards this README and its SOP follow.

*License: this role is Apache-2.0 (the framework's own license). The
upstream application it deploys, Dependency-Track, is also Apache-2.0,
referenced by image reference only: nothing from it is vendored.*
