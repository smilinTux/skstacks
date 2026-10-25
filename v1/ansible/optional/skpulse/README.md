# skpulse

[Uptime Kuma](https://github.com/louislam/uptime-kuma) on the swarm:
monitor probing, push-based heartbeats, public status pages and badges.
Opt-in: nothing renders or changes for an instance that never runs
`deploy_skpulse-<env>.yml`.

## Design

One service (`skpulse`) holds everything (SQLite database, uploaded
status-page images) under one bind-mounted data directory
(`/var/data/skpulse-<env>/uptime-kuma`). Traefik (skfenceha, or skfence)
renders THREE routers on the same host and the same backend service (see
"Sign-in, SSO and the first admin" below for why):

| Router | Priority | Carries SSO? | What |
|---|---|---|---|
| `<router>-public` | 100 | never | push (`/api/push/`), public status pages (`/status`, `/status-page`, `/api/status-page/`), badges (`/api/badge/`), and the static SPA assets a status page needs to render (`/assets`, `/upload`, favicon/manifest/icons) |
| `<router>-metrics` | 100 | never (opt-in, `METRICS_BYPASS_SSO`) | `/metrics` only |
| `<router>` | 5 | `AUTH_MODE: sso` only | everything else: the dashboard, the login page, `/socket.io/` (not a separate path; it hangs off the same `Host` rule) |

Priority 5 is above skfenceha's priority-1 catch-all (a tie there loses to
it, same trap skrac hit; see its README), below the two bypass routers
above.

## Sign-in, SSO and the first admin

Uptime Kuma has no native OIDC client: `AUTH_MODE: sso` (the **default**)
puts Authentik forward auth (`SSO_MIDDLEWARE`, default `authentik@file`)
in front of the `<router>` router above. `AUTH_MODE: local` is Kuma's own
username/password login only, no forward auth, no Authentik objects. The
default is `sso` so a vault that never sets `AUTH_MODE` does not
accidentally expose a no-auth Kuma to the internet (fail closed).

**Why three routers, not one.** Uptime Kuma serves push monitors, public
status pages and badges from callers that carry no browser session:
external servers heart-beating in on a schedule, anonymous visitors of a
status page, `<img>`/`<iframe>` embeds of a badge on another site. A
forward-auth redirect in front of any of those just breaks it. The status
page is Kuma's own single-page app, so it needs its OWN static JS/CSS
bundle and icon/manifest files too, or the page loads an empty shell that
then tries (and fails, unauthenticated) to fetch its own code. The
`<router>-public` router carries `TRAEFIK_MIDDLEWARES` (or
`BYPASS_TRAEFIK_MIDDLEWARES` if set independently) but never
`SSO_MIDDLEWARE`, in either `AUTH_MODE`.

**Kuma's own login is disabled only when SSO is on.** `disableAuth` is a
setting inside Kuma's own database (`setting` table, key `disableAuth`),
not an environment variable: there is no way to seed it before first boot.
The play writes it directly (the same class of direct app-state write as
skrac's `config.json`) via `docker exec ... sqlite3 ... INSERT ... ON
CONFLICT ...`, idempotent, and only after the first admin account already
exists (a fresh, not-yet-set-up instance is never touched). `AUTH_MODE:
sso` sets `disableAuth` true (Authentik already gates the host, so Kuma's
own password prompt would just be a second, redundant login). `AUTH_MODE:
local` ALWAYS forces `disableAuth` back to false: fail closed, so a
cluster that used to run `sso` and switched to `local` can never end up
with Kuma's own auth disabled and no forward auth in front either.

**Contradictory configs are refused.** In `AUTH_MODE: local` the play fails
before touching anything when the vault also sets a non-empty
`SSO_MIDDLEWARE` (only the key name is ever printed). That key only means
something in `AUTH_MODE: sso`: switch the mode or remove it.

### Vault keys

| Key | Default | Notes |
|---|---|---|
| `AUTH_MODE` | `sso` | `sso` or `local` |
| `SSO_MIDDLEWARE` | `authentik@file` (in `sso` mode) | the forward-auth middleware on the main router |
| `TRAEFIK_MIDDLEWARES` | `default-no-crowdsec@file` | the main router's base chain (SSO_MIDDLEWARE, if any, is appended) |
| `BYPASS_TRAEFIK_MIDDLEWARES` | same as `TRAEFIK_MIDDLEWARES` | the `-public` router's chain; never carries SSO regardless |
| `METRICS_BYPASS_SSO` | `false` | `true` adds the `-metrics` router (no SSO, but Kuma's own HTTP Basic Auth on `/metrics` still applies) |
| `METRICS_TRAEFIK_MIDDLEWARES` | same as the bypass chain | the `-metrics` router's chain, independent of the other two |
| `HOSTNAME` | `skpulse[-env].<CLUSTERNAME>.<DOMAIN>` | a full override, e.g. a bare host with no cluster prefix |
| `CLUSTERNAME` / `DOMAIN` | the play's `cluster_name` / `domain` | public host (ignored when `HOSTNAME` is set) |

## Authentik setup (manual, one app)

Create a **Proxy Provider** (forward auth, external host mode) in
Authentik for `https://<skpulse host>/`, and an **application** using it,
then set the result as skfenceha's `authentik@file` middleware (or point
`SSO_MIDDLEWARE` at a differently-named one already provisioned for this
host). Add the admin group (e.g. `skadmin`) to the application's policy so
only admins reach the dashboard.

## Deploy

`deploy_skpulse-<env>.yml`, standard invocation.

**Health check**: `docker service ls --filter name=skpulse-<env>_skpulse`
shows `1/1`.

**First-run setup is automatic as of this release.** A fresh instance's
deploy itself completes Kuma's own setup (POST /setup-database, then the
"setup" socket event to create the first admin, username `skadmin`, a
freshly generated password) -- no manual click-through, even behind
Authentik's forward-auth gate. The generated password lands in
`~/.skstacks-generated/<app>-<env>-<host>-admin-password` on whichever
controller ran the deploy (0600, outside any git tree, never printed);
move it into the instance vault if anyone needs to sign in to Kuma
directly (AUTH_MODE: sso does not need it day to day). An instance
deployed before this release, or one whose admin got created by hand
some other way, is unaffected: the entry-page check is the gate, and it
is a no-op once a database already exists.

**Older gotcha, now only relevant pre-upgrade**: the first admin account
used to be created only through Kuma's own first-run web UI; complete it before
relying on `AUTH_MODE: sso` to auto-login (Kuma auto-logs in the single
admin account once `disableAuth` is true, so create that account, THEN let
the next deploy flip the setting). That "next deploy" does not have to be
a full deploy: `--tags authmode` runs only the vault load, the AUTH_MODE
resolve/assert/conflict checks and the `disableAuth` toggle itself (found
2026-10-24, nor: an instance that had its admin account created after its
last deploy was stuck showing Kuma's own login behind Authentik until the
next full redeploy; `--tags authmode` is the standalone fix for exactly
that window, no redeploy, no other service touched). If the admin
credential used for an out-of-band fix ever gets stored for reuse, record
the real username (`SELECT username FROM user;`), not a guess: a typo'd
username in a credential store looks identical to a wrong password until
you check Kuma's own `user` table. A separate `discover-traefik-frontends.sh`
helper (not run by the playbook) can auto-register every Traefik-fronted
service as a monitor via the Uptime Kuma API, given an
`UPTIME_KUMA_API_TOKEN`.

## Portal tile (Authentik)

On by default (`skpulse.AUTHENTIK_TILE`, default `true`; set to `false` to opt out): the next deploy creates or
updates a provider-less Authentik `core.Application` named `AUTHENTIK_TILE_NAME`
(default `SKPulse (Uptime Kuma)`), launch URL `https://<the public host above>`,
icon `AUTHENTIK_TILE_ICON` (default `fa://fa-heart-pulse`), bound to the group
`AUTHENTIK_TILE_GROUP` (default `authentik Admins`, which must already exist).
This only adds a dashboard bookmark; it creates no provider and changes nothing
about `AUTH_MODE`/forward auth above. Needs `sksso.api_token` in sksso's own vault
(see `optional/sksso/README.md`). See `shared/tasks/authentik_portal_tile.yml`.
