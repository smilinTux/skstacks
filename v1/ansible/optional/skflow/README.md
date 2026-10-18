# skflow

[Windmill](https://github.com/windmill-labs/windmill) on the swarm: the
cluster's shared workflow engine (scripts and flows in Python, TypeScript,
Bash and more, schedules, webhooks, approval steps that suspend a flow
until a person or an event resumes it, retries and run history). Opt-in:
nothing renders or changes for an instance that never runs
`deploy_skflow-<env>.yml`.

This is the ratified provider for the `skflow` capability (see
`v2/docs/services/skflow.md`).

## Upstream, license and image

- **License.** Windmill's community edition is AGPLv3. Nothing is
  vendored or rebuilt: the role runs the unmodified upstream image.
- **Image.** `ghcr.io/windmill-labs/windmill`, release **`1.837.0`**,
  multi-arch, pinned by the manifest-list digest. Postgres is
  `postgres:17.7-alpine`, and the dump sidecar uses `postgres:17.7` (the
  Debian variant, because its dump loop needs bash), both by digest. The
  play refuses any of them without `@sha256:`.
- **Phone-home.** Not audited for this role. After the first login,
  review the instance settings (Superadmin settings) for any usage
  reporting and turn it off if the instance must stay quiet.

## Design

### A shared engine, tenants outside

skflow is infrastructure, like skvector or sksso: one Windmill per cluster
environment that many workloads use. A tenant (a document pipeline, an
agent, a Nextcloud webhook source) deploys **its own stack** and talks to
skflow over the network: it calls the Windmill API with a workspace token,
or Windmill flows call the tenant's service. Never add a tenant's containers
to `skflow.yml`: a second tenant would inherit them, and redeploying one
tenant would roll the engine for all of them.

To give a tenant stack access, attach it to the `skflow-<env>` overlay (it
is `attachable`) and use `http://skflow-<env>_server:8000`, or go through the
public host. Keep one Windmill **workspace** per tenant so tokens, variables,
resources and schedules stay separate.

### Tenant private assets (fonts, images)

A tenant that needs files which must not live in a public repository (licensed
fonts, a bordered-certificate image) must not depend on someone copying them
onto a node. This repository has no tenant role for them. The pattern the
current document-pipeline tenant uses, which any tenant should follow:

- **Build-time assets** (fonts) are copied into the tenant's private image
  from a private directory named by an environment variable at build time
  (the tenant's `build-worker-context` script reads `FWE_FONT_DIR`), each file
  checked against a SHA-256 pinned in the tenant's repository. A missing or
  changed file fails the build rather than substituting another one. The
  image goes to the cluster registry and is pinned by digest in the tenant's
  stack file, so a fresh deploy gets the fonts by pulling the image.
- **Run-time assets** (the border PNG at `/state/assets/borders/`) live on the
  tenant's state volume. They are not in the image today, which is the
  reproducibility gap: on a fresh volume the file is absent and the renders
  that need it fail or skip. Provide it with a Swarm config (or secret) the
  tenant stack mounts read-only, or bake it into the same private image, and
  keep the pinned SHA-256 check. Until the tenant stack does that, restoring
  its state volume from backup restores the file; a brand-new volume needs it
  placed once from the private asset store.
- Never commit the assets, their hashes' source files, or the private paths
  to this repository. Tests that need them must be run where the private
  directory exists; without it, rendering tests error or skip, so treat a
  green run without the assets as incomplete.

### Services

| Service | Replicas | Purpose |
|---|---|---|
| `server` | `SERVER_REPLICAS` (1) | UI and API on port 8000, behind Traefik |
| `worker` | `WORKER_REPLICAS` (2) | Runs jobs (worker group `WORKER_GROUP`, default `default`) |
| `worker-native` | `NATIVE_WORKER_REPLICAS` (0) | Optional, native jobs only (`NATIVE_MODE`), worker group `native` |
| `postgres` | 1 | Windmill's database (absent when `DATABASE_URL` is set) |
| `postgres-db-backup` | 1 | `pg_dumpall` archives for skbackup (absent when `DATABASE_URL` is set) |

All Windmill services read one env file (`skflow.env`); only `MODE` and the
worker group differ.

### Placement and data

| Path | What |
|---|---|
| `/var/data/runtime/skflow-<env>/postgres` | Postgres data (`POSTGRES_PATH`) |
| `/var/data/skflow-<env>/database-dump` | Dumps written by `postgres-db-backup` (`DUMP_PATH`) |
| `/var/data/runtime/skflow-<env>/logs` | Job logs too large for the database; server and every worker share it |
| `/var/data/runtime/skflow-<env>/worker-cache` | Dependency cache of the workers (disposable) |

`/var/data` is shared storage on every node, so any service can land on any
node that matches `PLACEMENT_CONSTRAINTS` (default `node.role == worker`).
To keep Postgres on a node's local disk instead, set `POSTGRES_PATH` and
`DUMP_PATH` to local paths **and** `DATA_NODE` to that node: the play refuses
a local path without `DATA_NODE` (Swarm would otherwise reschedule Postgres
onto an empty directory and Windmill would start on a fresh database). With
`DATA_NODE` set, only `postgres` and `postgres-db-backup` are pinned there.

### Secrets vs settings

The vault carries two secrets: `DB_PASSWORD` (required, 16+ characters,
unless `DATABASE_URL` is set) and `SUPERADMIN_SECRET` (optional, 32+
characters). The env files that contain them are written `0600 root:root`
and read only by the Docker daemon. Nothing secret goes on a command line.

`SUPERADMIN_SECRET` is Windmill's own `SUPERADMIN_SECRET`: a bearer token
that acts as the virtual superadmin, meant for automation that bootstraps
workspaces. Leave it unset unless you need it, and rotate it like any other
admin credential.

### First login

Windmill creates the default account `admin@windmill.dev` / `changeme` on a
fresh database. Log in right after the first deploy, create your own
superadmin account, then delete or re-password the default one. Until then,
anyone who can reach the host can log in, which is one reason to deploy with
`SSO_MIDDLEWARE` set the first time.

### Traefik, SSO and the API

One router serves the host with `TRAEFIK_MIDDLEWARES` (default
`default-no-crowdsec@file`). `SSO_MIDDLEWARE` (for example
`authentik@file`) adds forward auth in front of Windmill's own login. When it
is set the template also renders:

- `skflow[-env]-api`: the paths in `SSO_EXEMPT_PREFIXES` (default
  `["/api/"]`) on their own router **without** forward auth. Windmill
  authenticates those requests itself (workspace tokens, webhook tokens, the
  UI's session cookie); behind forward auth every webhook and token client
  would get the SSO login page instead. Set it to `[]` to put everything
  behind SSO (webhooks from outside then stop working).
- `skflow[-env]-outpost`: `/outpost.goauthentik.io/` on this host, routed to
  sksso's own Traefik service (`SSO_OUTPOST_SERVICE`, default
  `sksso[-env]-server@swarm`) so the forward-auth login round trip completes.
  This replaces any hand-placed Traefik dynamic file for the same purpose.

The Authentik side needs a Proxy provider (forward auth, single
application) for the skflow host, added to the embedded outpost, as for
every other `authentik@file` service (see `optional/sksso/README.md`).

### SSO and Nextcloud integration (overview)

How the pieces fit when skflow runs next to skhub (Nextcloud) and sksso (Authentik). Each step has its own
detailed subsection in this README; this is the map. People and password procedures are in
[`v1/docs/sso-user-management.md`](../../../docs/sso-user-management.md).

1. **People sign in with SSO, not forward auth.** Windmill's own OAuth login talks to Authentik
   (sksso blueprint plus the `oauths` setting from `tasks/sso_provision.yml`, see "Single sign-on" below).
   Leave `SSO_MIDDLEWARE` empty. Forward auth in front of Windmill gives a second login, and it gates paths
   that Windmill's editor websocket and every machine client need. Forward auth is only correct for an app
   with no login of its own, with an explicit bypass list (`/api/`, webhooks, websockets, health); see
   `sso-estate-plan.md`.
2. **Who may sign in.** Members of the Authentik group `skflow-operators`. A Windmill account is created on
   the first login, matched by email, so an operator's Authentik email must equal the email Windmill knows
   (workspace admins and workflow owners are matched the same way). Add or remove a person by group
   membership in Authentik; removing them stops new logins, and a Windmill user or token that already exists
   must be removed in Windmill as well (workspace settings, Users). CE allows 10 SSO users in total.
3. **Windmill talks to Nextcloud with a service account.** The OAuth client for the native trigger is created
   in Nextcloud (`occ oauth2:add-client`) and the integration is connected in the workspace as a dedicated
   service account. That account is a local Nextcloud user with an app password; it has no Authentik user and
   no SSO. Webhook management is delegated to a service-account group (`NEXTCLOUD_WEBHOOK_GROUP`) so it needs
   no Nextcloud admin rights.
4. **Native trigger.** A Windmill native trigger on a Nextcloud event (for example `NodeWrittenEvent` with a
   path filter) registers a `webhook_listeners` webhook. Nextcloud queues each delivery as a background job.
   Run skhub's `webhook-worker` so deliveries leave within seconds instead of waiting for cron
   (measured: about 4 seconds on a live instance versus up to 5 minutes). Harden the registered webhook with
   `tokenNeeded: {}` unless the flow needs the user tokens.
5. **Talk bot and notifications.** Windmill posts into a Talk room through a Talk bot, and Talk can call a
   Windmill script back (slash commands, replies). The bot secret and the room token are Windmill secret
   variables; the bot webhook URL carries a Windmill token scoped to that single script. This is the
   notification channel for human approvals and flow results: a flow sends a message to the room, a person
   answers in Talk or in Windmill (signed in through SSO).
6. **Failure handling.** Workspace error handlers are enterprise only; give flows a `failure_module` that
   posts to the same Talk room.
7. **Break-glass.** The Windmill superadmin (`SUPERADMIN_SECRET`) works without SSO. Keep it in the vault and
   test it after SSO changes. If Authentik is down, SSO users cannot sign in but running schedules, triggers
   and API tokens keep working.

### Single sign-on (Authentik OAuth2 login)

Forward auth (above) gates the page; this is the other half, signing people
in to Windmill itself with their Authentik account (Windmill's OAuth login
button). It is code, not a click path, in two halves that share one secret:

1. **Authentik side, sksso.** Set `sksso.SKFLOW_OIDC_CLIENT_SECRET` and
   redeploy sksso. It renders an Authentik blueprint (provider, application,
   group binding) that Authentik's worker applies; see
   `optional/sksso/README.md` "skflow (Windmill) login".
2. **Windmill side, this role.** Set `skflow.SSO_CLIENT_SECRET` (the same
   value) and `skflow.SUPERADMIN_SECRET`. After the stack is up, the play
   (`tasks/sso_provision.yml`) runs `src/skflow/sso_provision.py` in a
   throwaway container on the `skflow-<env>` overlay and, as the virtual
   superadmin that `SUPERADMIN_SECRET` gives:
   - writes the global setting `oauths` (`POST /api/settings/global/oauths`,
     only when it differs from what `GET` returns): one provider `authentik`
     with `id` = the client id, `secret`, `display_name` (default
     `SKWorld SSO`), `connect_config` (scopes `openid offline_access`) and
     `login_config` (scopes `openid offline_access email profile`), with
     `https://<SSO host>/application/o/{authorize,token,userinfo}/`;
   - for every workspace in `SSO_WORKSPACE_ADMINS`, adds each listed email as
     an admin (`POST /api/w/<ws>/workspaces/add_user`
     `{email, is_admin: true, operator: false}`) or promotes a member who is
     not one yet. A workspace that does not exist is an error, so deploy the
     tenant that creates it first and run the play again; the `oauths`
     setting is already applied by then.

Both secrets go to the container on stdin; the task is `no_log`, and the
provisioner prints status lines only. Re-running changes nothing.

**Limit.** Windmill community edition accepts at most **10 SSO users** in
total (the 11th login is refused by Windmill). Keep the Authentik group
`skflow-operators` (`sksso.SKFLOW_OPERATORS_GROUP`) at or under that, or move
to the enterprise edition.

The user logging in must be in that Authentik group, or the application is
not shown to them (the blueprint binds the application to the group). A
user's Windmill account is created on first SSO login, matched by email.

### Nextcloud integration by default (optional)

When the instance also runs Nextcloud (SKHub), Windmill can take file events from it, post into a Talk room and talk
back through a Talk bot. Proven on the NAM estate with Windmill CE 1.837.0 and Nextcloud 34 (hammerTime
`deploy/fwe/windmill/README.md` has the working scripts). Nothing here is on by default; the pieces are:

1. **Nextcloud apps.** `webhook_listeners`, `oauth2`, `spreed` (Talk) and, for the event list in the Windmill UI,
   `integration_windmill` enabled. `tasks/nextcloud_integration.yml` does this part, delegated to the Nextcloud node.
2. **Pretty URLs.** `occ config:system:set htaccess.RewriteBase --value=/` then `occ maintenance:update:htaccess`.
   Without it `/apps/oauth2/api/v1/token` answers 405 and the Windmill connect step fails.
3. **OAuth client (Nextcloud) and workspace integration (Windmill).** `occ oauth2:add-client "Windmill skflow <ws>"
   "https://<skflow host>/workspace_settings?tab=native_triggers&service=nextcloud"`, then in the workspace
   (Workspace settings, Native triggers, Nextcloud, Configure OAuth) or by API
   `POST /api/w/<ws>/native_triggers/integrations/nextcloud/create` with client id and secret, the Nextcloud base URL and
   that redirect URI, then connect. The client secret goes to the vault, not to the repo.
4. **Connect as a service account, not an admin.** Managing webhooks is an administrator setting, but it can be
   delegated: `occ admin-delegation:add 'OCA\WebhookListeners\Settings\Admin' <service account group>`. The task file
   does this when `skflow.NEXTCLOUD_WEBHOOK_GROUP` is set. Connect the integration as that account.
5. **Triggers.** Create native triggers on the events you need (for example `NodeWrittenEvent` with an `eventFilter`
   regex on `event.node.path`). Windmill CE supports them; they run from Nextcloud's background job. On skhub with the
   `webhook-worker` service (see the skhub README, "Webhook worker") the delay is a few seconds; without it the delay is
   the Nextcloud cron period (5 minutes). Windmill registers webhooks with `tokenNeeded: owner, trigger`, which puts two short-lived
   Nextcloud user tokens into every job's args. Re-save the webhook with `tokenNeeded: {}` (see `harden_trigger.py` in
   hammerTime) unless the flow needs them.
6. **Talk bot.** `occ talk:bot:install "<Bot>" <secret 40+ chars> <webhook url> --feature webhook --feature response`,
   `occ talk:room:create ...`, `occ talk:bot:setup <bot id> <room token>`. The webhook URL targets a Windmill script with
   `?raw=true&include_header=x-nextcloud-talk-signature,x-nextcloud-talk-random&token=<token>`; use a Windmill token whose
   only scope is `jobs:run:scripts:<path>`. Keep the bot secret and room token in Windmill secret variables.

Workspace-level error handlers are an enterprise feature; give flows a `failure_module` instead.

### Health checks

- `server`: `curl http://127.0.0.1:8000/api/version` (unauthenticated);
  Traefik's load balancer probes the same path.
- `postgres`: `pg_isready`.
- Workers have no HTTP listener; Swarm restarts them when the process exits.

The deploy script waits up to `HEALTH_WAIT_SECONDS` (environment, default
600) for `server` to converge: the first boot runs Windmill's migrations
first. A slow start prints a warning, never a failed deploy.

### Job isolation (nsjail)

`DISABLE_NSJAIL` defaults to `true`, as in Windmill's own compose file:
jobs run as processes inside the worker container, without nsjail's
per-job sandbox. Anyone who can write a script in a workspace can therefore
read what the worker container can read (its env file, which includes
`DATABASE_URL`, and the shared cache). Treat workspace authors as trusted
operators, keep tenants in separate workspaces, and do not give untrusted
users script-author rights. Setting `DISABLE_NSJAIL: false` enables nsjail,
which needs the worker to run with extra privileges that a Swarm service
cannot be granted; verify it on a test cluster before relying on it.

### Backups

`postgres-db-backup` runs `pg_dumpall` every `BACKUP_FREQUENCY` (seconds,
default 86400) into `DUMP_PATH`, writing to a temp file renamed only on
success, and prunes dumps older than `BACKUP_KEEP_DAYS` (default 7) only
after a good dump. `pg_dumpall` (not `pg_dump`) because Windmill grants its
row-level security policies to cluster-level roles that a single-database
dump does not carry. Point skbackup at `/var/data/skflow-<env>/database-dump`
(the default path is already under `/var/data`, which skbackup covers);
never back up the live Postgres directory as files.

With an external `DATABASE_URL`, backing that database up is the external
database's job.

### Upgrades

Bump `WINDMILL_IMAGE` (tag and digest together). Windmill migrates its
schema on server start, so read the upstream release notes and take a fresh
dump first. Postgres major upgrades need `pg_dumpall` and a restore into a
new data directory; never point a newer major at the old data directory.

## Deployment

```bash
ansible-playbook -i <inventory> \
  framework/v1/ansible/optional/skflow/deploy_skflow-<env>.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file <vault password file>
```

Then open `https://skflow[-env].<base domain>/` and do the first login
steps above.

## Configuration (vault)

`skflow:` in `optional/group_vars/<env>/skflow-<env>-<domain dashed>-<cluster>_vault.yml`
(or `skflow-<env>_vault.yml`).

```yaml
skflow:
  # REQUIRED (no default)
  DB_PASSWORD: ""                           # 16+ chars; Postgres password (unless DATABASE_URL is set). Secret.

  # optional
  CLUSTERNAME: cluster1                     # default: inventory cluster_name
  DOMAIN: example.com                       # default: inventory domain
  CLOUDFLARED: false                        # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                              # public host; default skflow[-dev|-staging].<base domain>
  WINDMILL_IMAGE: ""                        # default ghcr.io/windmill-labs/windmill:1.837.0@sha256:...; must carry @sha256:
  POSTGRES_IMAGE: ""                        # default postgres:17.7-alpine@sha256:...; must carry @sha256:
  POSTGRES_BACKUP_IMAGE: ""                 # dump sidecar, default postgres:17.7@sha256:... (Debian, needs bash); must carry @sha256:
  DATABASE_URL: ""                          # external Postgres instead of the stack's own; then no postgres/backup services. Secret.
  SUPERADMIN_SECRET: ""                     # Windmill SUPERADMIN_SECRET bearer token, 32+ chars when set. Secret.
  COOKIE_DOMAIN: ""                         # Windmill COOKIE_DOMAIN; unset = the browser's origin
  CREATE_WORKSPACE_REQUIRE_SUPERADMIN: true # only superadmins create workspaces
  DISABLE_NSJAIL: true                      # see "Job isolation (nsjail)"
  JSON_LOGS: false                          # Windmill JSON_FMT
  LOG_LEVEL: info                           # RUST_LOG
  LOG_MAX_SIZE: 10m                         # json-file log rotation
  LOG_MAX_FILES: 3
  TZ: UTC
  SSO_CLIENT_SECRET: ""                     # Windmill OAuth login via Authentik; 32+ chars, same as sksso.SKFLOW_OIDC_CLIENT_SECRET. Needs SUPERADMIN_SECRET. Secret.
  SSO_HOST: ""                              # Authentik host; default sso[-env].<base domain>
  SSO_CLIENT_ID: skflow-windmill            # must equal sksso.SKFLOW_OIDC_CLIENT_ID
  SSO_DISPLAY_NAME: SKWorld SSO             # label of the login button
  SSO_WORKSPACE_ADMINS: {}                  # {workspace: [email, ...]} added as workspace admins; the workspace must exist
  PROVISIONER_IMAGE: ""                     # python:3-alpine pinned by digest, runs sso_provision.py
  EXTRA_ENV: {}                             # extra Windmill env vars (map), written to skflow.env
  PLACEMENT_CONSTRAINTS: ["node.role == worker"]
  DATA_NODE: ""                             # required when POSTGRES_PATH/DUMP_PATH are outside /var/data
  POSTGRES_PATH: ""                         # default /var/data/runtime/skflow-<env>/postgres
  DUMP_PATH: ""                             # default /var/data/skflow-<env>/database-dump
  TRAEFIK_MIDDLEWARES: ""                   # default default-no-crowdsec@file
  SSO_MIDDLEWARE: ""                        # e.g. authentik@file; forward auth on the UI router
  SSO_EXEMPT_PREFIXES: ["/api/"]            # paths kept OUT of forward auth when SSO_MIDDLEWARE is set
  SSO_OUTPOST_SERVICE: ""                   # default sksso[-env]-server@swarm
  TLS_OPTIONS: ""                           # default intermediate@file; "none" = no tls.options label
  SERVER_REPLICAS: 1
  SERVER_CPUS: "1.00"
  SERVER_MEMORY_LIMIT: 2048M
  SERVER_MEMORY_RESERVATION: 256M
  WORKER_REPLICAS: 2
  WORKER_GROUP: default
  WORKER_CPUS: "2.00"
  WORKER_MEMORY_LIMIT: 2048M
  WORKER_MEMORY_RESERVATION: 256M
  NATIVE_WORKER_REPLICAS: 0                 # >0 adds worker-native
  NATIVE_WORKER_CPUS: "1.00"
  NATIVE_WORKER_MEMORY_LIMIT: 1024M
  POSTGRES_CPUS: "1.00"
  POSTGRES_MEMORY_LIMIT: 2048M
  POSTGRES_MEMORY_RESERVATION: 256M
  HEALTH_CHECK_INTERVAL: 30s                # server healthcheck
  HEALTH_CHECK_TIMEOUT: 10s
  HEALTH_CHECK_RETRIES: 3
  HEALTH_CHECK_START_PERIOD: 60s
  BACKUP_FREQUENCY: 86400                   # seconds between dumps
  BACKUP_KEEP_DAYS: 7                       # BACKUP_NUM_KEEP is accepted as a fallback
```

Network override (rarely needed): `skflow_<env>_networks`, a list of
`{name, subnet, driver, attachable}` like the default below.

## Networks

| Network | Subnet |
|---|---|
| `skflow-prod` | `172.16.246.0/24` |
| `skflow-staging` | `172.16.247.0/24` |
| `skflow-dev` | `172.16.248.0/24` |

Plus the shared `cloud-public-<env>` for Traefik.

## Migrating a hand-deployed Windmill

For a Windmill that was started by hand with `docker stack deploy` under the
same stack name (named volumes on one node, hand-made Swarm secrets, a
Traefik dynamic file for the outpost route):

1. **Export what lives in Windmill.** With the `wmill` CLI, per workspace:
   `wmill workspace add <ws> <ws> https://<host>/` then `wmill sync pull`
   into a git directory, and commit it. This captures scripts, flows, apps,
   schedules, variables and resources (secret values only if you ask for
   them). Keep it as the restore of last resort.
2. **Dump the database.** On the node running the old Postgres task:
   `docker exec <old postgres container> pg_dumpall -U <user> | gzip > skflow-pre-migration.sql.gz`.
   Check it is not empty.
3. **Stop the old stack.** `docker stack rm <old stack>`, wait for its
   tasks to go. Keep its named volumes until the migration is verified.
4. **Prepare the new data.** Set the vault (`DB_PASSWORD` etc.), run the
   playbook once so it creates the directories and the network, then scale
   everything but Postgres to 0
   (`docker service scale skflow-<env>_server=0 skflow-<env>_worker=0`).
5. **Restore.** Into the new, empty Postgres:
   `gunzip -c skflow-pre-migration.sql.gz | docker exec -i <new postgres container> psql -U windmill -d postgres`.
   Role or database "already exists" notices for `windmill` are expected.
   If the old database user was not `windmill`, rename the roles or set
   ownership so `windmill` owns the `windmill` database.
6. **Rerun the playbook.** It scales the services back to their configured
   replicas. Log in and check workspaces, flows, schedules and resources.
7. **Remove the hand-made pieces.** Delete the Traefik dynamic file that
   routed `/outpost.goauthentik.io/` for this host (the outpost router is
   now a label, when `SSO_MIDDLEWARE` is set), the old Swarm secrets
   (`docker secret ls`), and finally the old named volumes.
8. **Tenant stacks** that ran inside the old stack move to their own stack
   and reach skflow over `skflow-<env>` or the public host.

## Files on the manager

| Path | What |
|---|---|
| `/var/data/config/skflow-<env>/skflow.yml` | Rendered compose file (`docker-compose.yml` symlinks to it) |
| `/var/data/config/skflow-<env>/skflow.env` | Windmill env, 0600 |
| `/var/data/config/skflow-<env>/postgres.env` | Postgres env, 0600 |
| `/var/data/config/skflow-<env>/postgres-backup.env` | Dump sidecar env, 0600 |
| `/var/data/skflow-<env>/deploy` | Deploy script (`/var/data/deploy_skflow-<env>` symlinks to it) |

## Verification

```bash
docker service ls --filter name=skflow-<env>
curl -fsS https://skflow[-env].<base domain>/api/version
ls -l /var/data/skflow-<env>/database-dump/   # a dump_*.sql.gz after the first BACKUP_FREQUENCY
```
