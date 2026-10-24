# sksec-web (CrowdSec Web UI)

[crowdsec-web-ui](https://github.com/TheDuffman85/crowdsec-web-ui) on the
swarm: a browser dashboard for the cluster's existing `sksec-<env>` CrowdSec
instance (investigate alerts, manage decisions, watch runtime metrics).
Opt-in, like every optional role: nothing renders or changes for an instance
that never runs `deploy_sksec-web-<env>.yml`, and `sksec-web` deploys
**after** `core/sksec` (it talks to that stack's LAPI; it provides no
CrowdSec engine or bouncer of its own).

Named to match the main CrowdSec role (`core/sksec`, stack `sksec-<env>`)
with `-web` appended: `sksec-web`, stack `sksec-web-<env>`.

## Upstream, license and image

- **License.** AGPL-3.0 ([TheDuffman85/crowdsec-web-ui](https://github.com/TheDuffman85/crowdsec-web-ui)).
  This role **references the upstream image and vendors nothing**.
- **Image.** `ghcr.io/theduffman85/crowdsec-web-ui`, release `2026.10.10`,
  pinned by the manifest-list digest; the play refuses an image without
  `@sha256:`.
- **Its own, separate login.** The web UI has its own account system
  (password, optional TOTP, passkeys/WebAuthn, and its own OIDC/SSO with
  group-role mapping if an operator wires it up, which this role does not
  do). An admin setup runs on first start. This role puts Authentik forward
  auth in **front** of that login (same double-gate pattern as skhome and
  skgym): nobody without an Authentik account in the admin group ever
  reaches the dashboard's own login form.

## Why this is admin-grade, not just another optional app

The dashboard authenticates to CrowdSec's LAPI as a registered **machine**
(a "watcher" account, `cscli machines add`), the same credential class
CrowdSec agents use to talk to each other. A machine account can read every
alert and decision; CrowdSec's own `trusted_ips` on the LAPI side is what
additionally gates **deleting** a decision (see "Decision deletion" below).
Either way this is not a low-value secret: treat `sksec_web.LAPI_PASSWORD`
like any other credential that can see and (potentially) erase the
cluster's security posture. It is never the Traefik bouncer key
(`core/sksec`'s `CROWDSEC_BOUNCER_API_KEY` / `BOUNCER_KEY_traefik`): this
role registers its own, separate machine identity and never reads or
writes the bouncer's key.

Consequently:

- **`AUTH_MODE` defaults to `sso`.** This role must never be reachable from
  the internet without Authentik in front of it. See "Sign-in and SSO".
- **The portal tile is admin-only.** The instance's Authentik application
  for `sksec-web` is bound to the admin group only (the same group skhome's
  tile is bound to on that instance: `authentik Admins` or `skadmin`,
  whichever the instance actually uses -- check the live binding before
  copying it), never to every signed-in user. This role does not create
  Authentik objects itself (same as skhome/skgym): the instance operator
  registers the forward-auth Proxy provider, application and group binding
  once, by hand or via the instance's own blueprint/IaC, same convention as
  every other `AUTH_MODE: sso` role in this framework.
- **Decisions default to read-only** (see below): the credential this role
  mints cannot delete a decision out of the box, even though it can read
  everything.

## Talking to the cluster's CrowdSec LAPI

`sksec_web.LAPI_URL` defaults to `http://sksec-<env>_crowdsec:8080` -- the
same Swarm service-DNS convention `core/sksec`'s own README documents for
its bouncer (`CROWDSEC_AGENT_HOST`). Reaching it means attaching to the
**same** `sksec-<env>` overlay network `core/sksec` already created (this
role declares it `external: true`, with the identical name and subnet, so
the idempotent network-creation task is a no-op if `core/sksec` is already
up, and a clear failure if it is not deployed yet). No new overlay network
or subnet is allocated by this role.

`sksec_web.CROWDSEC_SERVICE` overrides the service name if an instance's
`core/sksec` stack is not named `sksec-<env>` (default
`sksec-<env>_crowdsec`).

### Machine registration (idempotent, by this role's own deploy)

After the `app` service is up, the deploy resolves the Swarm node running
`core/sksec`'s `crowdsec` task (same `docker service ps` approach as
`core/sksec`'s own `tasks/lapi_allowlist.yml`) and, inside that container:

1. Lists existing machines (`cscli machines list -o json`).
2. If `sksec_web.LAPI_USERNAME` (default `sksec-web`) is not already
   registered, adds it with `sksec_web.LAPI_PASSWORD` and `-f /dev/null`
   (never overwrite the CrowdSec agent's own local LAPI credentials file).
3. Verifies the machine now exists.

It never re-registers or re-keys an existing machine (same philosophy as
`core/sksec`'s bouncer key: a second deploy with a *different*
`LAPI_PASSWORD` in vault does **not** change what is already registered).
**To rotate:** change `sksec_web.LAPI_PASSWORD` in vault, then inside the
`core/sksec` `crowdsec` container run `cscli machines delete sksec-web`
(or your `LAPI_USERNAME`), then redeploy `sksec-web` so it re-registers
with the new password. The password is never printed: every task that
touches it is `no_log: true`, and the add command passes it to `cscli`
through a container-local environment variable rather than a literal
argument, to keep it off that container's process list and out of any
`-v`/`-vvv` ansible output.

### Decision deletion

CrowdSec's own `trusted_ips` (server-side, `/etc/crowdsec/config.yaml`
inside the `crowdsec` container) is what additionally permits a machine
account to delete decisions; without the web UI's container network address
in that list, `sksec-web`'s machine can read everything but a delete is
refused by the LAPI itself. **This role never edits `core/sksec`'s
`trusted_ips`** (that file belongs to the `core/sksec` role, and changing it
from here would be a cross-role, security-sensitive mutation outside this
role's scope). Default `sksec_web.READ_ONLY: true` additionally sets the
upstream image's own `PERMISSION_READ_ONLY=true` (an instance-wide read-only
mode in the dashboard itself: alerts and decisions are visible, nothing
can be changed or deleted from the UI), so a fresh install is safe-by-default
at **two** independent layers before any operator opts in to write access.

To grant delete capability: set `sksec_web.READ_ONLY: false` in vault (the
UI-side gate), and, as a separate manual step documented here rather than
automated, add this role's `sksec-web-<env>` network CIDR
(`sksec_web.NETWORK_SUBNET`, printed by the deploy) to `core/sksec`'s
`trusted_ips` and restart that stack's `crowdsec` service. Until both are
done, `READ_ONLY: false` alone only removes the UI-level guard; the LAPI
itself still refuses the delete.

## Sign-in and SSO

`AUTH_MODE` (same two-path pattern as skhome/skgym):

- **`sso` (default): Authentik forward auth in front.** `SSO_MIDDLEWARE`
  (default `authentik@file`) guards the whole router. Authentik needs a
  proxy provider in forward-auth mode covering the `sksec-web` host, bound
  to the embedded outpost, **with its application restricted to the
  cluster's admin group** (see "Why this is admin-grade" above). Users
  still sign in to the dashboard's own account system afterward (or its
  own OIDC, if an operator wires that up separately; out of scope here).
- **`local`: no SSO.** The dashboard's own accounts only. Refused unless
  `sksec_web.ALLOW_LOCAL_PUBLIC_RISK: true` is also set (naming the risk
  explicitly), because an admin-grade CrowdSec console with no forward auth
  in front of it must never be an accident.

Guard (the deploy refuses, naming the keys): an `AUTH_MODE` other than
`sso`/`local`; `AUTH_MODE: local` with `SSO_MIDDLEWARE` set; a forward-auth
middleware (`authentik`, or the configured `SSO_MIDDLEWARE` name) inside
`TRAEFIK_MIDDLEWARES` directly (in `local` mode that is SSO under another
name; in either mode, use `AUTH_MODE: sso` / `SSO_MIDDLEWARE` instead);
`AUTH_MODE: local` without `ALLOW_LOCAL_PUBLIC_RISK: true`.

## Design

### Service

| Service | Image | Replicas | What |
|---|---|---|---|
| `app` | `crowdsec-web-ui` | 1 | React/Vite + Node.js UI and API, SQLite (`better-sqlite3`) under `/app/data` for the UI's own session/auth state and a local alert/decision cache (CrowdSec's LAPI is the source of truth; cached alert history expires after `sksec_web.SYNC_LOOKBACK`, default `168h`/7 days -- the upstream default). Exactly one replica: it is the only writer of that SQLite file. |

Traefik (`skfenceha`/`skfence`) routes `https://<host>/` to `app:3000`
(health check `/api/health`, public and unauthenticated per upstream, same
endpoint the image's own `HEALTHCHECK` already probes). Networks:
`sksec-<env>` (to reach the LAPI) and `cloud-public-<env>` (Traefik).

No `skbackup` sidecar by default: the dashboard holds no data CrowdSec's
own LAPI does not already hold more durably. An instance that wants the
UI's own settings/session state backed up anyway can point `skbackup` at
`sksec_web.DATA_PATH` directly; this role does not wire that in.

### Placement and data

- `DATA_PATH` (default `/var/data/sksec-web-<env>/data`, NFS on a typical
  instance) is bind-mounted at `/app/data`. For local disk, set `DATA_PATH`
  outside `/var/data` and `DATA_NODE` to the node that holds it (same rule
  as skhome/skgym/sksso/skhub/skpayment: the play refuses a local path
  without `DATA_NODE`, since local data on a floating service is an empty
  instance after the next reschedule).
- `PLACEMENT_CONSTRAINTS` defaults to `["node.role == worker"]`.

### Secrets vs settings

| Key | What it is |
|---|---|
| `LAPI_PASSWORD` | **Required.** The CrowdSec machine password this role registers (see "Machine registration"). At least 24 characters; the deploy refuses a shorter or missing value. Never printed. |
| `SESSION_SECRET` | Optional. The dashboard's own session-signing secret (`AUTH_SECRET`). Upstream auto-generates one if unset, which means every container restart signs everyone out of the dashboard's own login (not the Authentik layer in front of it). Set a random 32+ character value to keep sessions stable across restarts/redeploys. |

### Traefik

`TRAEFIK_MIDDLEWARES` is the router's whole middleware chain, default
`default-no-error-pages@file` (skfenceha: rate limit, security headers,
gzip) **not** the usual `default@file`: the dashboard's own API routes
return JSON errors its UI needs to read, same reasoning as skhome.
`TLS_OPTIONS` defaults to `intermediate@file`; `none` omits the label.

### Health checks

`app`: `GET /api/health` (the same endpoint the upstream image's own
`HEALTHCHECK` already probes; this stack re-declares it on the framework's
schedule). Traefik's service health check uses the same path.

## Configuration (vault)

`sksec_web:` in
`optional/group_vars/<env>/sksec-web-<env>-<domain dashed>-<cluster>_vault.yml`.

```yaml
sksec_web:
  LAPI_PASSWORD: "..."                       # required, >= 24 chars, openssl rand -base64 32
  # LAPI_USERNAME: sksec-web
  # LAPI_URL: http://sksec-<env>_crowdsec:8080
  # CROWDSEC_SERVICE: sksec-<env>_crowdsec
  # CROWDSEC_WEB_UI_IMAGE: ghcr.io/theduffman85/crowdsec-web-ui:2026.10.10@sha256:f57568d27b2c7798155a638b9cb76302777a9dcf43d8026f344c17767589d694
  # READ_ONLY: true
  # SESSION_SECRET: "..."
  # SYNC_LOOKBACK: "168h"
  # AUTH_MODE: sso
  # SSO_MIDDLEWARE: authentik@file
  # ALLOW_LOCAL_PUBLIC_RISK: false
  # HOSTNAME: sksec-web.example.com
  # TRAEFIK_MIDDLEWARES: default-no-error-pages@file
  # TLS_OPTIONS: intermediate@file
  # DATA_PATH: /var/data/sksec-web-<env>/data
  # DATA_NODE: ""
  # PLACEMENT_CONSTRAINTS: ["node.role == worker"]
  # CPUS: "0.50"
  # MEMORY_LIMIT: "384M"
  # MEMORY_RESERVATION: "64M"
  # HEALTH_CHECK_INTERVAL: "30s"
  # HEALTH_CHECK_TIMEOUT: "5s"
  # HEALTH_CHECK_RETRIES: 3
  # HEALTH_CHECK_START_PERIOD: "15s"
  # TZ: UTC
  # extra_env: {}
```

## Deploying

```bash
ansible-playbook -i inventory/hosts v1/ansible/optional/sksec-web/deploy_sksec-web-prod.yml \
  -e target_manager_group=swarm_managers
```

Deploy `core/sksec` first. `sksec-web` attaches to its `sksec-<env>`
network and registers against its LAPI; it fails loudly (no Running
`crowdsec` task found, LAPI never answers) rather than deploying half-wired.

## Tests

`v1/tests/test_sksec_web_render.py` (real Ansible, `v1/tests/render/`):
defaults, every knob, the `AUTH_MODE` guards (missing SSO, contradictory
middleware, local without the explicit risk flag), the missing-`LAPI_PASSWORD`
and too-short-`LAPI_PASSWORD` refusals, the unpinned-image refusal, the data
path/node guard, and dev/staging/prod playbook parity.

## Portal tile (Authentik)

On by default (`sksec_web.AUTHENTIK_TILE`, default `true`; set to `false` to opt out): the next deploy creates
or updates a provider-less Authentik `core.Application` named `AUTHENTIK_TILE_NAME`
(default `SKSec Web (CrowdSec)`), launch URL `https://<the public host above>`, icon
`AUTHENTIK_TILE_ICON` (default `fa://fa-shield-halved`), bound to the group
`AUTHENTIK_TILE_GROUP` (default `authentik Admins`, which must already exist). This
only adds a dashboard bookmark; it creates no provider and changes nothing about
`AUTH_MODE`/forward auth above. Needs `sksso.api_token` in sksso's own vault (see
`optional/sksso/README.md`). See `shared/tasks/authentik_portal_tile.yml`.
