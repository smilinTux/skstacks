# skrac

[MeshCentral](https://github.com/Ylianst/MeshCentral) on the swarm: remote
access and device management (agent-based, browser-based remote desktop
and terminal, file transfer, device inventory), replacing the orphaned
hand-run MeshCentral instance this role was built to retire. Opt-in:
nothing renders or changes for an instance that never runs
`deploy_skrac-<env>.yml`.

## Upstream, license and image

- **License.** MeshCentral is Apache-2.0. Nothing is vendored or rebuilt:
  the role runs the unmodified upstream image.
- **Image.** `ghcr.io/ylianst/meshcentral`, release **`1.2.6`** (published
  2026-09-24, the current stable GitHub release at the time this role was
  written; not a pre-release), multi-arch (`amd64`, `arm64`, `arm/v7`,
  `arm/v6`), pinned by the manifest-list digest; the play refuses an image
  without `@sha256:`. If a later stable release moves ahead of what the
  official `ghcr.io/ylianst/meshcentral` image publishes, pick the newest
  tag the official image actually ships (do not build a private image to
  chase a newer GitHub tag).
- **Phone-home.** Nothing on the server calls out by default. The admin
  UI shows a "check for updates" affordance against GitHub; installed
  agents only ever talk back to this server, never upstream. OIDC login
  (when enabled) talks to the configured Authentik instance only.

## Design

### Services

| Service | Image | Replicas | What |
|---|---|---|---|
| `server` | `meshcentral` | **1, always** | Node, the whole server: the NeDB database, every device/user record and, critically, the agent root certificate, all under one data directory. Two replicas on one data dir would corrupt the database and desync agent sessions: the replica count is not a knob, and updates are `stop-first`. |
| `backup` | `meshcentral` (same digest, no extra pull) | 1 | `skrac-backup loop`: a tar.gz of the data dir every `BACKUP_FREQUENCY` seconds into `BACKUP_PATH`. `BACKUP_ENABLED: false` removes it. |

Traefik (skfenceha, or skfence) routes `https://<host>/` in TWO routers
(see "Traefik, agents and the reverse-proxy trap" below): `<router>-agent`
for the agent/installer paths, and `<router>` for everything else (the web
UI, local and OIDC login, the REST API). Networks: `skrac-<env>` (server,
backup) and `cloud-public-<env>` (server only: `backup` is never on the
Traefik network).

### Placement and data

- `DATA_PATH` (default `/var/data/skrac-<env>/data`, NFS on a typical
  instance) is bind-mounted at `/opt/meshcentral/meshcentral-data`:
  `config.json`, the NeDB database, TLS/agent certificates. Root `0700`.
- `FILES_PATH` (default `/var/data/skrac-<env>/files`) is bind-mounted at
  `/opt/meshcentral/meshcentral-files`: files users upload or share through
  the server. Root `0700`.
- For local disk, set `DATA_PATH` (and `FILES_PATH`/`BACKUP_PATH`) outside
  `/var/data` and `DATA_NODE` to the node that holds it: `server` and
  `backup` get `node.hostname == <DATA_NODE>`, the play creates the
  directories there, and it refuses a local path without `DATA_NODE`
  (local data on a floating service means every agent, user and the agent
  root certificate are gone after the next reschedule, and every
  already-enrolled agent then refuses to talk to the "new" server). Same
  rule as sksso/skhub/skgym.
- `PLACEMENT_CONSTRAINTS` (server, backup) defaults to
  `["node.role == worker"]`.

### Secrets vs settings

MeshCentral generates everything it needs itself straight into the data
dir on first boot (session signing keys, the agent root certificate): there
is no equivalent of an openGym-style `SESSION_SECRET` to seed, and the
`backup` sidecar already covers all of it. The one vault secret this role
requires is `OIDC_CLIENT_SECRET`, and only in `AUTH_MODE: oidc` (at least
16 characters; the play fails loudly when it, `OIDC_ISSUER` or
`OIDC_CLIENT_ID` is missing, never prints the value). Everything else is a
plain vault key with a default.

### Sign-in, SSO and the first admin

MeshCentral has its own native OIDC client
([docs.meshcentral.com/meshcentral/openidConnectStrategy](https://docs.meshcentral.com/meshcentral/openidConnectStrategy/)),
unlike skgym/openGym: it is the identity provider's relying party itself,
so there is no Traefik forward-auth step for the web UI (`SSO_MIDDLEWARE`
below is a separate, optional, extra layer, off by default).

`AUTH_MODE: oidc` against this cluster's sksso (Authentik):

1. In Authentik, create an **OAuth2/OIDC provider** (confidential client,
   authorization code flow): redirect URI `https://<skrac host>/auth-oidc-callback`
   (strict match: MeshCentral requires an exact match), any available
   signing key. Create an **application** using that provider, slug
   `skrac` (or whatever `AUTHENTIK_APP_SLUG` is set to), launch URL
   `https://<skrac host>/`. Copy the generated client ID and client secret
   into the vault as `OIDC_CLIENT_ID` / `OIDC_CLIENT_SECRET`.

   **Bonus automation:** `AUTHENTIK_PROVISION: true` runs
   `tasks/authentik.yml` on every deploy, which creates or updates that
   same provider and application through the Authentik API (idempotent,
   modelled on skmesh's `tasks/authentik.yml`), using the vault's own
   `OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` as the provider's client
   id/secret, so the vault and Authentik can never drift apart. **Opt-in,
   default off**; needs `sksso.api_token` in sksso's vault (see skmesh's
   README "Prerequisites" for how to bootstrap that token once per
   cluster). Whether Authentik's API echoes a provider's `client_secret`
   back on a read is unconfirmed; the task always re-sends it on update,
   so the result is correct either way, just reported "updated" every run
   if it doesn't.
2. Set `OIDC_ISSUER` to that application's issuer URL, typically
   `https://<sso host>/application/o/<app slug>/`.
   MeshCentral itself fetches the discovery document, the token and the
   userinfo from that host when it starts and on every login: if the
   name does not resolve from inside the cluster, set `SSO_HOST_IP` (the
   edge's IP, rendered as an `extra_hosts` entry for the issuer host), and
   if the IdP sits behind a private CA, put that CA's PEM in
   `EXTRA_CA_CERT` (mounted read-only and loaded through
   `NODE_EXTRA_CA_CERTS`). Neither is needed with public DNS and a public
   certificate. If discovery fails at startup MeshCentral disables OIDC
   for the domain and logs `OIDC: Discovery failed`; fix the reachability
   and restart the server.
3. `OIDC_GROUP_CLAIM` (default `groups`) and `OIDC_ADMIN_GROUP` (default
   `skadmin`, the SKStacks admin-group convention) map an Authentik
   group onto MeshCentral's site-admin role via the id token's groups
   claim (Authentik's default `profile` scope mapping already includes
   it). `OIDC_NEW_ACCOUNTS` (default `true`) lets MeshCentral create a
   local account the first time each Authentik identity logs in
   (required for OIDC to work at all: MeshCentral cannot pre-create
   accounts for an external provider).
4. `LOCAL_LOGIN_HIDDEN: true` sets `showPasswordLogin: false`: the
   username/password form is hidden from the normal login page (only the
   OIDC button shows), but **local login stays reachable as break-glass**
   at `/login` regardless (MeshCentral has no documented way to fully
   disable it, and this role does not try to fake one). Create the first
   local admin account the usual MeshCentral way (the very first account
   created on an empty server becomes a site admin) before hiding the
   password form, or use `OIDC_ADMIN_GROUP` to grant admin through
   Authentik instead. `OIDC_AUTO_REDIRECT: true` additionally sets
   `unknownUserRootRedirect: auth-oidc`, sending an unauthenticated visitor
   straight into the OIDC flow instead of showing the login page first
   (`/login` still reaches the page directly).
5. `LOCAL_NEW_ACCOUNTS` (default `false`) is MeshCentral's own
   self-service local signup, independent of OIDC; left off so the only
   way in is OIDC or an admin-created local account.

`AUTH_MODE: local` (the default) renders no `authStrategies` block at all:
local accounts only, exactly like a hand-run MeshCentral. This is the
standalone path: no SSO in front, no Authentik objects, CrowdSec optional
(`TRAEFIK_MIDDLEWARES`). The first account created on the empty server
becomes the site admin.

**Contradictory configs are refused.** In `AUTH_MODE: local` the play fails
before touching anything, naming the offending keys, when the vault also
sets any of `AUTHENTIK_PROVISION: true`, a non-empty `SSO_MIDDLEWARE` or
`SSO_HOST_IP`,
`OIDC_AUTO_REDIRECT: true`, `LOCAL_LOGIN_HIDDEN: true` (it would hide the
only login there is), or a non-empty `OIDC_ISSUER` / `OIDC_CLIENT_ID` /
`OIDC_CLIENT_SECRET` (only the key name is ever printed). Those keys only
mean something in `AUTH_MODE: oidc`: switch the mode or remove them.
Empty or `false` values are fine. The agent router is identical in both
modes and never carries `SSO_MIDDLEWARE`.

### Traefik, agents and the reverse-proxy trap

MeshCentral agents hold a wss connection to the server and (by default)
pin a hash of the TLS certificate they see; a reverse proxy that
terminates TLS with its OWN certificate breaks that pinning unless
MeshCentral is told what the proxy's real certificate actually is. This
role follows the framework's standard shape (every other optional service
does the same): **Traefik terminates TLS** with its own Let's Encrypt
certificate (`tls.certresolver=main`) and forwards plain HTTP to `server`
on `INTERNAL_PORT` (default `4430`); `server`'s `config.json` is told:

- `settings.port`: `INTERNAL_PORT`, the plain-HTTP port it actually listens
  on inside the overlay network.
- `settings.aliasPort`: `443`, the port agents and the web UI are told the
  server is REALLY reachable on (the public port, not `INTERNAL_PORT`).
- `settings.tlsOffload` / `trustedProxy`: `TRUSTED_PROXY_CIDR` (default the
  framework's `cloud-public-<env>` overlay subnet: `172.16.200.0/24` prod,
  `.201.0/24` staging, `.202.0/24` dev), so MeshCentral trusts Traefik's
  `X-Forwarded-*` headers instead of treating every connection as
  originating from inside the overlay network.
- `domains[""].certUrl`: `https://<skrac host>/`, which MeshCentral fetches
  **on its own** to learn the certificate agents actually see (Traefik's
  Let's Encrypt certificate), which is what makes agent cert-hash pinning
  work at all when the server process and the public-facing TLS endpoint
  are not the same thing.

**Agent/installer paths never carry `SSO_MIDDLEWARE`.** A forward-auth
layer in front of `/agent.ashx` (the agent's control connection),
`/meshrelay.ashx` (relay sessions), `/meshagents` (the installer binary
download), `/meshsettings.ashx` or `/devicefile.ashx` would try to redirect
every agent's websocket upgrade and every unauthenticated installer
download into an Authentik login page, breaking all of them; none of
those callers carry a browser session. The compose template renders TWO
routers: `<router>-agent` (those paths plus `/health.ashx`, priority 100,
`AGENT_TRAEFIK_MIDDLEWARES`) and `<router>` (everything else, priority 5, above skfenceha's priority-1 catch-alls,
`TRAEFIK_MIDDLEWARES` plus the optional `SSO_MIDDLEWARE`). CrowdSec
(`crowdsec-bouncer@file`) is fine on agent paths by default
(`AGENT_TRAEFIK_MIDDLEWARES` defaults to the same chain as
`TRAEFIK_MIDDLEWARES`); if a CrowdSec bouncer check ever proves to disrupt
an agent's websocket upgrade in practice, set `AGENT_TRAEFIK_MIDDLEWARES`
independently (e.g. to `default@file`, dropping the bouncer there only).
`SSO_MIDDLEWARE` is never added to the agent router under any setting.

### Health checks

`server`: a Node one-liner (always present; the image's own entrypoint)
hitting the image's own unauthenticated `/health.ashx` on
`127.0.0.1:INTERNAL_PORT`, avoiding a dependency on `curl`/`wget` that a
slimmer base image might not ship. `HEALTH_CHECK_INTERVAL` /`_TIMEOUT` /
`_RETRIES` / `_START_PERIOD` (30s, 10s, 3, 30s). `backup` has its health
check disabled (the image's own would probe a server it does not run).
Traefik's service health check is `/health.ashx` too.

### Backups

The `backup` sidecar writes `BACKUP_PATH/skrac-<UTC timestamp>.tar.gz`
(root `0600`: it contains the agent root certificate and, in `oidc` mode,
the OIDC client secret): one archive per run, written to a temp file,
verified (gzip and tar read it back and it holds `./config.json`) and only
then renamed; archives older than `BACKUP_KEEP_DAYS` are pruned only after
a good one was written. `meshcentral-files` (user uploads) is NOT part of
that archive by default (it can be large); `BACKUP_INCLUDE_FILES: true`
writes it as a SECOND, separate `skrac-files-<timestamp>.tar.gz` archive
each run, so a slow files archive never delays or risks the small,
critical data one. First archive `BACKUP_START_DELAY` seconds after the
sidecar starts. A manual archive:
`docker exec $(docker ps -qf name=skrac-<env>_backup) /bin/sh /usr/local/bin/skrac-backup once`
on the node running it.

With skbackup on the storage host (tier 2 copy plus a freshness check):

```yaml
skbackup:
  copy:
    apps:
      - name: skrac-prod
        newest: [{dir: backup, keep: 2}]   # a run may still be writing the newest
        dump: backup                        # warn when no archive is fresher than dumps.max_age_hours
```

Keep `BACKUP_FREQUENCY` at most `skbackup.dumps.max_age_hours` (default 26
hours; the default 86400s fits).

**Restore:** stop the server (`docker service scale skrac-<env>_server=0`),
unpack the `skrac-*` archive into an empty `DATA_PATH`, redeploy. Losing
`DATA_PATH` without a recent archive means every agent must be
reinstalled (see "Re-enrolling an existing agent" below): the agent root
certificate that signs agent-to-server trust is gone with it.

### Upgrades

Read the upstream release notes first, take an archive
(`skrac-backup once`), then bump `MESHCENTRAL_IMAGE` and redeploy.
`v1/scripts/check_images.sh` keeps the framework default pullable.

### Resources

Sized for a small homelab node: server limit 1.0 CPU / 768M (reservation
128M), backup 0.25 CPU / 128M (16M). Knobs: `SERVER_CPUS`,
`SERVER_MEMORY_LIMIT`, `SERVER_MEMORY_RESERVATION`.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skrac/deploy_skrac-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

The play: checks the vault (fails on an unknown `AUTH_MODE`, on
`AUTH_MODE: oidc` without a full Authentik config, on `AUTH_MODE: local`
combined with SSO-only keys, an image without a digest, or a local data
path without `DATA_NODE`), creates the directories, writes `config.json`, installs the
backup script and the compose file, ensures the networks, deploys stack
`skrac-<env>`, and restarts `server` when the config changed (a bind mount
does not change the service spec by itself).

## Configuration (vault)

`skrac:` in `optional/group_vars/<env>/skrac-<env>-<domain dashed>-<cluster>_vault.yml`.

```yaml
skrac:
  CLUSTERNAME: cluster1                     # default: inventory cluster_name
  DOMAIN: example.com                       # default: inventory domain
  CLOUDFLARED: false                        # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                              # public host; default skrac[-dev|-staging].<base domain>
  MESHCENTRAL_IMAGE: ""                     # default below; must carry @sha256:
  TRAEFIK_MIDDLEWARES: ""                   # default default-no-crowdsec@file; UI/API router
  AGENT_TRAEFIK_MIDDLEWARES: ""             # default = TRAEFIK_MIDDLEWARES; agent router, NEVER gets SSO_MIDDLEWARE
  SSO_MIDDLEWARE: ""                        # e.g. authentik@file: EXTRA forward-auth on the UI router only
  TLS_OPTIONS: ""                           # default intermediate@file; "none" = no tls.options label
  INTERNAL_PORT: 4430                       # plain-HTTP port server listens on behind Traefik
  TRUSTED_PROXY_CIDR: ""                    # default per-env cloud-public-<env> subnet
  AUTH_MODE: local                          # local | oidc
  OIDC_ISSUER: ""                           # required if AUTH_MODE: oidc
  OIDC_CLIENT_ID: ""                        # required if AUTH_MODE: oidc
  OIDC_CLIENT_SECRET: ""                    # required (16+ chars) if AUTH_MODE: oidc; secret, no_log
  SSO_HOST_IP: ""                           # oidc only: pin the OIDC_ISSUER host to this IP (extra_hosts), e.g. the edge
  EXTRA_CA_CERT: ""                         # PEM of a private CA in front of the IdP (NODE_EXTRA_CA_CERTS); public, not a secret
  OIDC_GROUP_CLAIM: groups
  OIDC_ADMIN_GROUP: skadmin
  OIDC_NEW_ACCOUNTS: true
  OIDC_AUTO_REDIRECT: false                 # true: unauthenticated visitors skip straight to OIDC
  AUTHENTIK_PROVISION: false                # opt-in Authentik API automation, see "Sign-in, SSO"
  AUTHENTIK_APP_SLUG: skrac
  AUTHENTIK_APP_NAME: SKRac
  AUTHENTIK_AUTHORIZATION_FLOW: default-provider-authorization-implicit-consent
  AUTHENTIK_INVALIDATION_FLOW: default-provider-invalidation-flow
  AUTHENTIK_SIGNING_KEY: "authentik Self-signed Certificate"
  AUTHENTIK_VAULT_FILE: ""                  # default: sksso's vault file next to this one
  AUTHENTIK_INTERNAL_URL: ""                # default http://sksso-<env>_server:9000
  AUTHENTIK_NETWORK: ""                     # default sksso-<env>
  SSO_HOST: ""                              # default sso[-env].<base domain>
  PROVISIONER_IMAGE: ""                     # default python:3.13.15-alpine@sha256:...
  LOCAL_LOGIN_HIDDEN: false                 # true: showPasswordLogin false (break-glass /login still works)
  LOCAL_NEW_ACCOUNTS: false                 # MeshCentral's own local self-service signup
  TITLE: ""                                 # default "skrac"
  TITLE2: ""
  EXTRA_CONFIG: {}                          # arbitrary extra config.json keys, deep-merged in (settings/domains/etc)
  DATA_PATH: ""                             # default /var/data/skrac-<env>/data
  FILES_PATH: ""                            # default /var/data/skrac-<env>/files
  BACKUP_PATH: ""                           # default /var/data/skrac-<env>/backup
  DATA_NODE: ""                             # pin server + backup to this node (required with a local path)
  PLACEMENT_CONSTRAINTS: []                 # server + backup, default ["node.role == worker"]
  HEALTH_CHECK_INTERVAL: 30s
  HEALTH_CHECK_TIMEOUT: 10s
  HEALTH_CHECK_RETRIES: 3
  HEALTH_CHECK_START_PERIOD: 30s
  SERVER_CPUS: "1.00"
  SERVER_MEMORY_LIMIT: 768M
  SERVER_MEMORY_RESERVATION: 128M
  BACKUP_ENABLED: true
  BACKUP_FREQUENCY: 86400                   # seconds between archives
  BACKUP_KEEP_DAYS: 7
  BACKUP_START_DELAY: 60                    # seconds before the first archive
  BACKUP_INCLUDE_FILES: false               # also archive FILES_PATH, as a second, separate archive
```

Default image: `ghcr.io/ylianst/meshcentral:1.2.6@sha256:09c5abfcf0758f2a7ab9b43fa8acb838b92bba1498c1447fc8c09bb1d296b73e`
(multi-arch index digest; full literal in the playbooks). Networks:
override with the flat per-env `skrac_<env>_networks` list.

## Re-enrolling the existing meshagents

When skrac replaces an older MeshCentral server whose data is gone, that
server's agent root certificate no longer exists anywhere. The
`meshagent.service` agents left polling it therefore
**cannot be re-pointed with a config edit**: an existing MeshCentral agent
pins the OLD server's root certificate hash, and that hash check fails
closed against any other server, including a new skrac instance,
regardless of hostname. Each of those hosts needs the agent **reinstalled**
from the new skrac server's own "Add Agent" page (device group -> Add
Agent -> the OS-specific one-line installer, or the `.msh` + agent binary
pair for an offline install), which enrolls it against skrac's own,
freshly-generated agent root certificate. Uninstall the old
`meshagent.service` / binary under `/usr/local/mesh_services/meshagent/`
first on each host (it will otherwise keep running and polling a host that
answers 404) before installing the new one.

## Files on the manager

| Path | Mode |
|---|---|
| `/var/data/config/skrac-<env>/skrac.yml` (+ `docker-compose.yml` link) | `0644` |
| `/var/data/skrac-<env>/deploy` (+ `/var/data/deploy_skrac-<env>` link) | `0755` |
| `/var/data/skrac-<env>/bin/skrac-backup` | `0755` |
| `<DATA_PATH>/`, `<FILES_PATH>/` and `<BACKUP_PATH>/` | `0700` root |
| `<DATA_PATH>/config.json` | `0600` root |

## Networks

| Network | Subnet |
|---|---|
| `skrac-prod` / `-staging` / `-dev` | `172.16.238.0/24` / `172.16.239.0/24` / `172.16.245.0/24` |
| `cloud-public-<env>` | framework-shared Traefik network |

## Verification

```bash
docker service ls --filter name=skrac
curl -s https://<host>/health.ashx          # 200
```

Tests: `v1/tests/test_skrac_render.py` (the deploy through real Ansible:
defaults, every knob set, knob-off paths, the agent/UI router split and
that SSO_MIDDLEWARE never reaches the agent router, the reverse-proxy
config.json settings, and the missing-OIDC-secret / unpinned-image /
local-path-without-DATA_NODE failures) and `v1/tests/test_skrac_backup.py`
(the backup script for real: a good archive, the separate files archive,
a failed run that keeps the previous one, retention). A skstack06 stage
`skrac` is planned (not yet release-gated; see CHANGELOG) to deploy it on
the lab cluster and check convergence, the route through skfenceha, the
OIDC redirect against the lab Authentik, and a real agent connecting
through Traefik on 443.
