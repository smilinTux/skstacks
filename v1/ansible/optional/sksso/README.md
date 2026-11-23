# SKSSO (Authentik SSO)

Authentik SSO stack: `server`, `worker`, `postgres`, `postgres-db-backup` and
`redis`, fronted by Traefik. Authentik is an identity provider (IdP): it
issues OIDC tokens and/or sits in front of other services as a forward-auth
gate.

## Architecture

- Services: `postgres`, `postgres-db-backup`, `redis`, `server`, `worker`
- Networks: `sksso-<env>` (internal), `cloud-public-<env>` (external, shared
  with Traefik - see the framework README's "Instance contract")
- Bind mounts:
  - `/var/data/runtime/sksso-<env>/postgres:/var/lib/postgresql` (Postgres 18+; `:/var/lib/postgresql/data` when `POSTGRES_IMAGE` pins an older major)
  - `/var/data/runtime/sksso-<env>/redis:/data`
  - `/var/data/sksso-<env>/media:/media`
  - `/var/data/sksso-<env>/certs:/certs`
  - `/var/data/sksso-<env>/custom-templates:/templates`
  - `/var/data/sksso-<env>/database-dump:/dump` (backup sidecar)

## Traefik integration

- Routes: `Host(\`sso[-<env>].<domain>\`)`, entrypoint `websecure`, TLS via
  `main` cert resolver.
- Backend port: 9000 (Authentik's own HTTP listener; Traefik does the TLS).
- Sticky session cookie: `authentik-session`.
- **Forward-auth**: for services with no native SSO support, add
  `authentik@file` (shipped by `core/skfenceha`, in
  `dynamic-middlewares.yml.j2`) to that service's router middlewares. Point
  the middleware's address at
  `http://sksso-<env>_server:9000/outpost.goauthentik.io/auth/traefik`. The
  Authentik side, the domain-wide `forward_domain` Proxy Provider, its
  Application (bound to `skadmin` only) and its attachment to the embedded
  outpost, is created by this role as code; see "Forward-auth catch-all
  (authentik@file)" below.
  A service that needs its own login policy (not the domain-wide
  catch-all) gets its own forward_single Proxy Provider; see
  `optional/skpages/README.md` (Deploy step 3) for a worked example,
  including the `ak shell` redirect-URI fix.
- **Native OIDC**: for apps that speak OIDC themselves, do **not** add the
  forward-auth middleware to that route - it would intercept the OIDC
  callback (`?code=...&state=...`) the app is waiting for. Create an OAuth2
  provider + Application per app in Authentik instead; the app redirects to
  `/application/o/<app>/authorize/` directly.

## Required instance vars (vault)

`sksso-<env>_vault.yml` (see the framework README's "Instance contract" for
the lookup path):

```yaml
sksso:
  # REQUIRED - no framework default, these are all estate-specific
  postgres_user: "authentik"
  postgres_password: "..."
  authentik_secret_key: "..."     # Django session/crypto signing key
  CLUSTERNAME: "<your-cluster-name>"
  DOMAIN: "<your-domain>"

  # optional
  postgres_db: authentik           # default: authentik
  authentik_version: "2024.4.2"    # default shown - pin explicitly for prod
  INSTANCE: primary                # default: primary
  CLOUDFLARED: false                # true: BASE_DOMAIN = DOMAIN (no cluster prefix)
  TZ: UTC
  log_level: ...                   # default: warning(prod)/info(staging)/debug(dev)
  backup_keep_days: 7              # days of pg dumps kept (default: backup_num_keep, else 7)
  backup_frequency: 1d              # default: 1d
  placement_use_worker_constraint: true   # default true; set false if the
                                           # cluster has no worker nodes
  placement_exclude_nodes: []      # optional list of node hostnames to exclude

  # postgres + redis data (see "Local database on a pinned node")
  DATA_NODE: ""                    # node hostname; pins postgres + redis there
  POSTGRES_DATA_PATH: /var/data/runtime/sksso-<env>/postgres   # default
  REDIS_DATA_PATH: /var/data/runtime/sksso-<env>/redis         # default
  colocate_with_data_node: false   # also pin server + worker to DATA_NODE
                                    # (see "Local database on a pinned node")

  # forward-auth catch-all behind authentik@file (see "Forward-auth catch-all
  # (authentik@file)"); ON by default, every knob below is optional
  FORWARD_AUTH_ENABLED: true                      # false: you manage the catch-all by hand
  FORWARD_AUTH_PROVIDER_NAME: "SKStacks forward auth (domain)"   # set to an existing provider's name to ADOPT it
  FORWARD_AUTH_APP_SLUG: skstacks-forward-auth    # set to an existing application's slug to ADOPT it
  FORWARD_AUTH_APP_NAME: "SKStacks forward auth (admin-only apps)"
  FORWARD_AUTH_SSO_HOST: sso.example.org          # default sso[-<env>].<base> (CAPAUTH_SSO_HOST, when set, before that)
  FORWARD_AUTH_COOKIE_DOMAIN: example.org         # default <base>: the cluster domain (DOMAIN with CLOUDFLARED)
  FORWARD_AUTH_ADMIN_GROUP: skadmin               # default skadmin; the ONLY group bound
  FORWARD_AUTH_OUTPOST: "authentik Embedded Outpost"   # default shown

  # replicas (all default to 1)
  server_replicas: 1
  worker_replicas: 1
  postgres_replicas: 1
  redis_replicas: 1
  backup_replicas: 1

  # email (SMTP) - all optional, only rendered if email_host is set
  email_host: "..."
  email_port: 465
  email_username: "..."
  email_password: "..."
  email_use_tls: false
  email_use_ssl: true
  email_from: "..."                # may carry a display name: "NAM - Native Asset Management <sender@example.com>"
                                   # (Gmail only accepts the authenticated account or a verified alias as the address)

  # branding pack under branding/ (optional, default off); see "Branding"
  branding: nam

  # GeoIP - optional, only rendered if both geoip vars below are set
  geoip_account_id: "..."
  geoip_license_key: "..."

  # Authentik automation API token - optional; read by the deploys of other
  # services that register themselves with Authentik (skmesh AUTH_MODE:
  # authentik). Not rendered into sksso itself; see "Automation API token".
  api_token: "..."                 # openssl rand -hex 32
```

## Branding

Two packs ship: `nam` (below) and `skworld`. An instance can bring its own (see
"Instance packs" below). A pack is optional parts only: the
media dir is required, `templates/` and `nextcloud-theming.sh` are copied only when
the pack has them.

### `branding: skworld`

`sksso.branding: skworld` copies `branding/skworld/media/` (logo, favicon, orange
`skworld.css`, `apply_branding.py`) to `/var/data/sksso-<env>/media/public/skworld/`
and installs `apply-branding` next to `deploy`. Run `apply-branding [stack]` once
after the stack is up (idempotent). It:

- sets the default brand: title (env `SKWORLD_BRAND_TITLE`, default "SKWorld"),
  logo, favicon and custom CSS (orange accent, and the "Powered by authentik"
  footer hidden);
- sets the login flow's heading (env `SKWORLD_FLOW_TITLE`, default "Welcome to the
  Kingdom");
- makes sure the stock password-recovery flow exists: when no flow with slug
  `default-recovery-flow` is present it applies Authentik's own blueprint
  `example/flows-recovery-email-verification.yaml` in-process (an existing flow is
  never overwritten), and sets it as the brand's recovery flow;
- points the identification stage(s) of the brand's login flow at that flow, so the
  login page shows "Forgot password?".

Recovery email is sent through sksso's global email settings
(`AUTHENTIK_EMAIL__*` in `sksso.env`, `sksso.email_from`); the skworld pack ships no
email templates. Rollback: clear the brand's logo, favicon, custom CSS and recovery
flow in the admin UI, and clear `recovery_flow` on the identification stage.

### `branding: nam`

`sksso.branding: nam` copies the NAM brand pack (`branding/nam/`) to shared
storage on every deploy: logo, favicon, flow background and `nam.css` to
`/var/data/sksso-<env>/media/public/nam/`, and the branded email templates
(`nam_invite.html` for invitations and enrollment, `nam_recovery.html` for
password recovery, each with a `.txt` twin) to `custom-templates/`. The role
also installs two helpers next to `deploy`:

- `apply-branding [stack]` writes the brand into Authentik (idempotent, run
  once after the stack is up): default brand title, logo, favicon, flow
  background, custom CSS, dark UI theme, the two email stages' template and
  subject, and the "Welcome to authentik!" flow titles. It never touches
  providers, flow logic or policies. Rollback: set the brand fields back to
  `/static/dist/assets/...` defaults and the stages to `email/password_reset.html`.
- `nextcloud-theming [stack] [media-dir]` runs `occ theming:config` (name,
  slogan, color, logo, favicon, background) on the node hosting
  `skhub-<env>_nextcloud`. No redeploy.

The email sender display name comes from `sksso.email_from`; changing it needs
a rolling update of `_server` and `_worker` (env change). The look is the
NAMhub palette (teal `#00B4A0` on deep navy `#0D1A24`, leaf mark) restored from
`namstacks/ansible/deployment/namhub-exapps/src/namhub-theme-notes.md`.

### Instance packs (`branding_dir`)

A pack that belongs to one instance (its own logo, colours, wording) lives in
that instance's repo, not here. Point the role at it with `sksso.branding_dir`
and keep `sksso.branding` as the pack's name:

```yaml
sksso:
  branding: acme                 # names media/public/acme/
  branding_dir: ../../branding/sksso/acme   # relative to inventory_dir, or absolute
```

The directory has the same layout as a shipped pack: `media/` (required),
`apply-branding.sh` (required), and optional `templates/` and
`nextcloud-theming.sh`. A relative `branding_dir` is resolved against the
instance's inventory dir (`inventory_dir`, e.g. `envs/prod/`), never against the
framework checkout, the same rule as skfence `EXTRA_DYNAMIC_FILES`.
`sksso.branding` must be a plain name (letters, digits, `.`, `_`, `-`). Empty
`branding_dir` (the default) keeps today's behaviour: `branding/<pack>/` in this
role.

## Automation API token

Deploys that create their own Authentik objects (today: skmesh in
`AUTH_MODE: authentik`, which creates its OAuth2 provider, application and
an IdP-manager service account) call the Authentik API with
`sksso.api_token` from this vault. Register that token in Authentik once
per cluster, idempotently, with `src/sksso/ensure-api-token.sh` on a node
that runs an sksso worker (or server) task, token on stdin:

```bash
scp framework/v1/ansible/optional/sksso/src/sksso/ensure-api-token.sh <node>:/tmp/
ansible-vault view --vault-password-file <pass> <sksso vault> \
  | python3 -c 'import sys,yaml; print(yaml.safe_load(sys.stdin)["sksso"]["api_token"])' \
  | ssh <node> "sudo bash /tmp/ensure-api-token.sh <env>"
```

It creates (or keeps) the service account `skstacks-automation` in the
`authentik Admins` group and the non-expiring API token
`skstacks-automation-api` with exactly the vault's key (re-running after a
rotation updates the key). The token never appears on a command line or in
the output.

## CapAuth login (optional)

`sksso.CAPAUTH_ENABLED: true` adds a **CapAuth OIDC identity provider** (PGP-signed login, from the capauth project) to this stack and registers it in Authentik as an OAuth source, with its own login flow. It needs **stock** Authentik 2025.2 or newer (OAuth sources have PKCE from 2025.2, and CapAuth refuses a code flow without PKCE S256); no custom Authentik image.

What the deploy does:

- runs a `capauth` service (one replica: its key registry, OIDC state and RS256 signing key are SQLite/files under `/var/data/sksso-<env>/capauth`), routed by Traefik at `capauth[-<env>].<base>` (`sksso.CAPAUTH_HOST` to override) for `/oidc/` (login page, OIDC endpoints), `/capauth/v1/challenge` and `/capauth/v1/verify` (key enrollment) only. The admin API, the phone-signer (bunker) and the other CapAuth endpoints are not routed. That host needs DNS and a certificate like `sso[-<env>]`. Authentik's server calls the token and userinfo endpoints on the stack network.
- renders `capauth.env` and `capauth-oidc-clients.json` (both `0600`): one client, `authentik`, whose only redirect URI is `https://sso[-<env>].<base>/source/oauth/callback/<source slug>/` (`sksso.CAPAUTH_SSO_HOST` if users reach Authentik on another host).
- after the stack is up (`tasks/capauth_provision.yml`, idempotent, via `sksso.api_token`): an OAuth source `CapAuth` (`capauth`), the flow `capauth-authentication` (title "Sign in with CapAuth"): one login form with the normal username and password fields **and** a CapAuth button (Authentik's identification stage with its inline password stage `default-authentication-password` and the source), then the MFA validation stage `default-authentication-mfa-validation` (so the password path is no weaker than the default login), then a user login stage. `sksso.CAPAUTH_PASSWORD_LOGIN: false` makes it CapAuth only (the button alone, no password field); switching back and forth is idempotent. Users are matched by the OIDC `sub` (the PGP fingerprint) only and the source has **no enrollment flow**: a CapAuth key that is not linked to an Authentik user is refused. Each `sksso.CAPAUTH_USERS` entry links a fingerprint to an existing Authentik user (never created).

The flow is not bound to anything. An application opts in by using it as its provider's authentication flow (skmesh: `skmesh.AUTHENTIK_AUTHENTICATION_FLOW: capauth-authentication`). Every other application keeps the brand's default password login.

```yaml
sksso:
  api_token: "..."                    # required (see "Automation API token")
  CAPAUTH_ENABLED: true
  CAPAUTH_CLIENT_SECRET: "..."        # required, openssl rand -hex 32 (CapAuth <-> Authentik)
  CAPAUTH_ADMIN_TOKEN: "..."          # required, openssl rand -hex 32 (approves keys)
  CAPAUTH_USERS:                      # optional: fingerprint -> existing Authentik user
    - {username: alice, fingerprint: 0123456789ABCDEF0123456789ABCDEF01234567}
  # optional
  CAPAUTH_IMAGE: "<registry>/capauth:<version>@sha256:<digest>"   # default: the pinned public release below
  CAPAUTH_HOST: capauth.example.org   # default capauth[-<env>].<base>
  CAPAUTH_SSO_HOST: sso.example.org   # default sso[-<env>].<base>
  CAPAUTH_REQUIRE_APPROVAL: true      # default true: new keys wait for an admin
  CAPAUTH_SOURCE_SLUG: capauth
  CAPAUTH_FLOW_SLUG: capauth-authentication
  CAPAUTH_SOURCE_AUTHENTICATION_FLOW: default-source-authentication
  CAPAUTH_PASSWORD_LOGIN: true        # default true: password form + CapAuth button; false = CapAuth only
  CAPAUTH_PASSWORD_STAGE: ""          # default default-authentication-password (must exist)
  CAPAUTH_MFA_STAGE: default-authentication-mfa-validation   # "" = no MFA stage on the password path
```

`CAPAUTH_IMAGE` defaults to the public CapAuth release `ghcr.io/smilintux/capauth:0.3.13@sha256:f2de737815f30a47706336250cfb257673c4861c0a790f28d09cd7403fb80ac7` (capauth v0.3.13: per-client `require_nonce`, which Authentik's source needs because it sends no OIDC nonce). An instance may set its own (for example a mirror in its own registry), and the deploy refuses a value without `@sha256:`.

Enrolling a user (once per key):

1. The user proves the key to CapAuth: `POST https://<capauth host>/capauth/v1/challenge` with the fingerprint, sign the returned canonical `CAPAUTH_NONCE_V1` payload, then `POST /capauth/v1/verify` with the fingerprint, nonce, signature and the ASCII-armored **public** key. With approval required this answers `403 enrollment_pending`.
2. An admin approves it from inside the cluster (the admin API is not routed publicly), on a manager, token on stdin:

   ```bash
   ansible-vault view --vault-password-file <pass> <sksso vault> \
     | python3 -c 'import sys,yaml; print("Authorization: Bearer " + yaml.safe_load(sys.stdin)["sksso"]["CAPAUTH_ADMIN_TOKEN"])' \
     | ssh <manager> 'docker run --rm -i --network sksso-<env> curlimages/curl:8.10.1 -fsS -H @- \
         -H "Content-Type: application/json" -d "{\"fingerprint\": \"<FP>\"}" \
         http://sksso-<env>_capauth:8420/capauth/v1/keys/approve'
   ```
3. Add `{username, fingerprint}` to `sksso.CAPAUTH_USERS` and redeploy sksso (links the fingerprint to the Authentik user).

Authentik keeps one CapAuth link per user: changing a user's fingerprint in `CAPAUTH_USERS` (key rotation) updates that link on the next deploy. A fingerprint already linked to a different user is refused; remove that link in the admin UI first.

Login: the application sends the user to the CapAuth flow. Either the user types username and password as usual (then MFA if they have it), or clicks "CapAuth", signs the challenge on CapAuth's page (copy the challenge, `capauth sign-challenge`, paste the signature), and returns to Authentik logged in as the linked user.

Rollback: unbind the flow in every application that uses it first (skmesh: remove `AUTHENTIK_AUTHENTICATION_FLOW` and redeploy it), then set `CAPAUTH_ENABLED: false` and redeploy sksso: the deploy removes the `capauth` service (a stack deploy alone would leave it running). The Authentik objects (source, stages, flow, links) stay until deleted in the admin UI; the source is useless without the service, and nothing is bound to the flow.

## Local database on a pinned node

By default postgres and redis keep their data under `/var/data/runtime`
(shared storage, NFS on a typical instance) and float across workers. An
NFS hang on the node running them then takes Authentik down, and with it
every forward-auth login. To keep them on local disk on one node:

```yaml
sksso:
  DATA_NODE: <node hostname>               # also an inventory host
  POSTGRES_DATA_PATH: /var/lib/sksso/postgres
  REDIS_DATA_PATH: /var/lib/sksso/redis
```

- `DATA_NODE` adds `node.hostname == <DATA_NODE>` to postgres and redis
  only (the worker constraint stays unless `placement_use_worker_constraint`
  is false). The backup sidecar, server and worker keep floating.
- A path outside `/var/data` is local: the deploy creates it on `DATA_NODE`
  (owner 999, mode 0700, never recursive) and no longer creates the old
  `/var/data/runtime` path. The play fails if a path is local but
  `DATA_NODE` is empty: local data on a floating service is an empty
  database after the next reschedule.
- Local data is not on shared storage: back it up with the node, and keep
  the nightly dump (`database-dump/`) on shared storage as it is.

### Colocating server + worker with the data node

`server` and `worker` keep floating across worker nodes by default, which
means every OIDC token-endpoint request (`/application/o/token/`) makes its
Postgres and Redis calls across the overlay network to wherever `DATA_NODE`
landed. Measured on two real multi-node Swarm estates: a token exchange with
server/worker/postgres/redis all on one node (loopback) ran at roughly 0.5s;
the same exchange with server/worker on a different node than postgres/redis
ran at p50 1.26s, p95 1.57s, max 2.27s under a light recurring load, purely
from the added round trips. That matters when a downstream OIDC client has a
short HTTP timeout: skrac (MeshCentral) pins `openid-client@5.7.1`, whose
default request timeout is 3.5s and which MeshCentral exposes no config.json
knob to raise (see skrac README "OIDC login timeout margin").

Set `colocate_with_data_node: true` with `DATA_NODE` set to also add
`node.hostname == <DATA_NODE>` to `server` and `worker`:

```yaml
sksso:
  DATA_NODE: <node hostname>
  colocate_with_data_node: true
```

- Default is `false` (today's floating behavior; no instance's render
  changes without opting in).
- Ignored when `DATA_NODE` is empty (nothing to colocate with).
- Trade-off: `DATA_NODE` going down now also takes `server` and `worker`
  with it, same as it already does for `postgres`/`redis`. If that single
  node is not acceptable as a shared failure domain, leave this off and
  raise the downstream client's timeout instead.

### Migrating a running instance

Moving a live database off NFS is a maintenance window (Authentik, and every
forward-auth login, is down while postgres is stopped). `<old>` is
`/var/data/runtime/sksso-<env>`, `<new>` the local paths you set; run the
copy and checks on `DATA_NODE`, which mounts both.

1. Take a fresh dump off shared storage first:
   `docker exec <postgres container> pg_dump -Fc -U <user> <db> > pre-move.dump`.
2. Scale to 0 in this order and wait until no task of them is running
   (`docker service ps --filter desired-state=running`):
   `docker service scale sksso-<env>_server=0 sksso-<env>_worker=0 sksso-<env>_postgres-db-backup=0`,
   then `docker service scale sksso-<env>_postgres=0 sksso-<env>_redis=0`.
   A clean postgres stop matters: a postgres killed mid-write on NFS is how
   an instance ends up restart-looping on `lock file "postmaster.pid" is empty`.
3. On `DATA_NODE`, copy each data dir with ownership, ACLs, xattrs and hard
   links preserved, by number: `rsync -aHAX --numeric-ids <old>/postgres/ /var/lib/sksso/postgres/`
   (same for `redis/`). The deploy later sets only the top dir to 999:999 0700
   (postgres and redis both run as uid 999 in their images; never recursive).
4. Verify by REGULAR FILES on both sides, count and byte sum:
   `find <dir> -type f -printf '%s\n' | awk '{n++; s+=$1} END {print n, s}'`.
   Both numbers must match. Do not compare `du -s --apparent-size`: it adds
   directory sizes, which differ between NFS and ext4 even for an exact copy.
5. Set `DATA_NODE`, `POSTGRES_DATA_PATH` and `REDIS_DATA_PATH` in the vault
   and deploy the stack from the template. Check: the postgres task runs on
   `DATA_NODE` with the new bind source (`docker service inspect`), its log
   shows no `initdb`, and an Authentik login works.
6. Rename the old dirs, never delete them: `mv <old>/postgres <old>/postgres.STALE-moved-to-<node>-local-<date>`
   (same for `redis`). Then put a guard file at each old path
   (`touch <old>/postgres`), so a deploy from an old template fails loudly
   instead of starting an empty database. The STALE copies stay until a
   separate, deliberate cleanup.

**WARNING:** do not switch a mount by hand with one
`docker service update --mount-rm <target> --mount-add type=bind,src=...,target=<target>`.
`--mount-rm` is applied last, so it removes BOTH the old and the new mount
for that target, and postgres starts `initdb` on an empty anonymous volume.
Do the `--mount-rm` and `--mount-add` in separate updates, or (better)
redeploy the stack from the template.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/sksso/deploy_sksso-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

## Verifying health

```bash
curl -sf https://sso[-<env>].<your-domain>/-/health/live/
```

or, from the swarm manager: `docker service logs <stack>_server` /
`docker service ps <stack>_server` where `<stack>` is `sksso-<env>`.

## Server router middlewares and backend scheme

- `sksso.router_middlewares` (list, default `[]`): Traefik middlewares on the server router, for example a file-provider chain with security headers and error pages (`["default@file"]`). Empty renders no `middlewares` label, as before.
- `sksso.SERVER_BACKEND_SCHEME` (`http` default, or `https`): how Traefik reaches the Authentik server, `http` on 9000 or `https` on 9443 (Authentik's own certificate). Anything else stops the play.

An instance moving an existing Authentik onto the framework sets both to what its running router uses, so the first deploy does not drop headers from the login page.

## Extra CSRF origins

`sksso.csrf_extra_origins` (list, default `[]`): extra `https://` origins appended to `AUTHENTIK_CSRF__TRUSTED_ORIGINS`, for another host that POSTs to Authentik (for example a mesh console).

## user_settings.py override

`sksso.user_settings_py` (string, default empty): the content of an Authentik `user_settings.py` (Django settings override, e.g. `TENANT_APPS`). When set, the deploy writes it to `/var/data/config/sksso-<env>/custom/user_settings.py` and mounts it read-only at `/data/user_settings.py` on server and worker. Empty = no override and no mount.

`sksso.authentik_uid` (default `1000`, the uid the Authentik image runs as): owner of `/var/data/sksso-<env>/media`, which Authentik writes to (it creates `media/public` on first migration).

## skflow (Windmill) login

`sksso.SKFLOW_OIDC_CLIENT_SECRET` (32+ chars, `openssl rand -hex 32`) gives
skflow (Windmill) its OAuth2 login as code. Unset, nothing changes. Set, the
deploy:

- renders `/var/data/config/sksso-<env>/blueprints/skflow-windmill.yaml` (no
  secret inside) and mounts the directory at `/blueprints/custom` (read only)
  in `server` and `worker`; Authentik's worker applies it and re-applies it on
  change;
- puts the secret in `sksso.env` (0600) as `SKFLOW_OIDC_CLIENT_SECRET`, which
  the blueprint reads with `!Env`.

The blueprint creates or adopts (matched by name or slug, so an object made by
hand is taken over, not duplicated):

| Object | Value |
|---|---|
| OAuth2 provider `skflow-windmill` | confidential, client id `skflow-windmill`, strict redirect `https://<skflow host>/user/login_callback/authentik`, authorization flow `default-provider-authorization-implicit-consent`, invalidation flow `default-provider-invalidation-flow`, signing key `authentik Self-signed Certificate`, subject mode `hashed_user_id`, scopes openid, email, profile, offline_access |
| Application `skflow-windmill` | name `Windmill (skflow)`, launch URL `https://<skflow host>/user/login` |
| Group `skflow-operators` | members are managed in Authentik, not by the blueprint |
| Policy binding | application bound to the group `skflow-operators`, so only its members see and may use it |

```yaml
sksso:
  SKFLOW_OIDC_CLIENT_SECRET: "..."   # same value as skflow.SSO_CLIENT_SECRET
  SKFLOW_HOST: skflow.example.org     # default skflow[-<env>].<base>
  SKFLOW_OIDC_CLIENT_ID: skflow-windmill
  SKFLOW_OPERATORS_GROUP: skflow-operators
  SKFLOW_APP_NAME: "Windmill (skflow)"
```

The Windmill half (the `oauths` setting and workspace admins) is applied by the
skflow play, see `optional/skflow/README.md` "Single sign-on". To check a
blueprint without applying it: `docker exec -i <server container> sh -c 'cat >
/tmp/x.py; ak shell < /tmp/x.py'` with a script that calls
`Importer.from_string(yaml).validate()` (it runs in a transaction that is
rolled back).

## sksec-web / skpatch-hosts (OIDC) login

`sksso.SKSEC_WEB_OIDC_CLIENT_SECRET` / `sksso.SKPATCH_HOSTS_OIDC_CLIENT_SECRET`
(32+ chars, `openssl rand -hex 32`) give `sksec-web` (crowdsec-web-ui) /
`skpatch-hosts` (linux-update-dashboard) their own OIDC login as code, when that
role's own `AUTH_MODE: oidc` is set (see `optional/sksec-web/README.md` /
`optional/skpatch-hosts/README.md` "Sign-in..."). Unset, nothing changes. Set,
the deploy renders `/var/data/config/sksso-<env>/blueprints/<app>-oidc.yaml`
(no secret inside) and mounts the blueprints directory at
`/blueprints/custom` (read only) in `server` and `worker`, same mechanism
as skflow above; the secret lands in `sksso.env` (0600) as
`<APP>_OIDC_CLIENT_SECRET`, which the blueprint reads with `!Env`.

Unlike skflow (Windmill), neither app needs a provisioner script on its own
side: both read their OIDC config from plain environment variables at
startup (that role's own deploy playbook renders them), verified against
the pinned upstream source. No Windmill-style "set a global API setting"
step exists or is needed.

The blueprint creates or adopts (matched by name or slug, so an object made
by hand is taken over, not duplicated):

| Object | Value |
|---|---|
| OAuth2 provider `<app>` | confidential, client id `<app>` (default; `<APP>_OIDC_CLIENT_ID` overrides), strict redirect `https://<app host>/api/auth/oidc/callback`, authorization flow `default-provider-authorization-implicit-consent`, invalidation flow `default-provider-invalidation-flow`, signing key `authentik Self-signed Certificate`, subject mode `hashed_user_id`, issuer mode `per_provider`, scopes openid, email, profile, and a private `groups` scope mapping (`request.user.ak_groups.all()`) |
| Application `<app>` | name `<APP>_APP_NAME` (default `SKSec Web (CrowdSec)` / `SKPatch Hosts`), launch URL `https://<app host>/` |

**No policy binding here, unlike skflow's blueprint above.** The
Application slug (`<app>`, i.e. the plain router name: `sksec-web` or
`skpatch-hosts`, same for both envs other than prod) is the SAME slug each
role's own portal tile defaults `AUTHENTIK_TILE_SLUG` to, so
`shared/tasks/authentik_portal_tile.yml` (run from that role's own deploy,
after this blueprint) finds this exact Application and manages its tile
fields and **group binding** itself, keeping this provider in place rather
than nulling it (`shared/src/authentik_portal_tile.py` "keeping its
existing provider", #242) -- one Authentik Application per app, not two.
Who may sign in is therefore that role's own `AUTHENTIK_TILE_GROUP`
(default `authentik Admins`), not a group this blueprint owns.

```yaml
sksso:
  SKSEC_WEB_OIDC_CLIENT_SECRET: "..."  # same value as sksec_web.OIDC_CLIENT_SECRET
  SKSEC_WEB_HOST: sksec-web.example.org  # default sksec-web[-<env>].<base>
  SKSEC_WEB_OIDC_CLIENT_ID: sksec-web
  SKSEC_WEB_APP_NAME: "SKSec Web (CrowdSec)"
  SKPATCH_HOSTS_OIDC_CLIENT_SECRET: "..."     # same value as skpatch_hosts.OIDC_CLIENT_SECRET
  SKPATCH_HOSTS_HOST: skpatch-hosts.example.org       # default skpatch-hosts[-<env>].<base>
  SKPATCH_HOSTS_OIDC_CLIENT_ID: skpatch-hosts
  SKPATCH_HOSTS_APP_NAME: "SKPatch Hosts"
```

Run order across the two roles' playbooks: deploy (or redeploy) `sksso`
with the relevant `<APP>_OIDC_CLIENT_SECRET` set, THEN deploy that app with
its own matching `AUTH_MODE: oidc` / `OIDC_CLIENT_SECRET`. Running the
app's deploy first is not an error: its portal tile task creates a
provider-less Application at that slug, and the next `sksso` deploy's
blueprint apply sets the provider onto that SAME object (Authentik
blueprints are authoritative for the fields they declare), so the two
converge regardless of order. There is no teardown path: turning `AUTH_MODE`
back to `sso`/`local` and removing the `sksso` vault key stops this
blueprint from being rendered or re-applied, but the provider/application it
already created in Authentik is not deleted (same limitation as skflow's
blueprint above); clean it up by hand if the app is being retired, not just
switched back to forward auth.


## Forward-auth catch-all (authentik@file)

`core/skfenceha` ships the `authentik@file` Traefik middleware and most roles attach it by default, but
a middleware alone gates nothing: the outpost needs a provider that matches the request host. This role
creates it as code, so a fresh estate has a working, closed SSO gate on the first deploy instead of apps
that are unreachable (no provider) or open to every Authentik account (a hand-made provider without a
policy binding, which Authentik treats as "admit everyone").

**On by default.** Every sksso deploy renders
`/var/data/config/sksso-<env>/blueprints/forward-auth-catchall.yaml` (no secret inside), mounts the
blueprints directory at `/blueprints/custom` (read only) in `server` and `worker`, and after the stack
deploy attaches the provider to the embedded outpost. The blueprint creates or adopts, matched by name,
slug or group:

| Object | Value |
|---|---|
| Group | `FORWARD_AUTH_ADMIN_GROUP` (default `skadmin`), existence only: no attributes, so the membership and flags of an existing group are never touched |
| Proxy Provider | named `FORWARD_AUTH_PROVIDER_NAME`, mode `forward_domain`, `external_host` `https://<sso host>`, `cookie_domain` the estate domain, authorization flow `default-provider-authorization-implicit-consent`, invalidation flow `default-provider-invalidation-flow` (only on Authentik 2024.10 and later, where the field exists) |
| Application | slug `FORWARD_AUTH_APP_SLUG`, name `FORWARD_AUTH_APP_NAME`, hidden from the user portal (infrastructure, not a tile), provider the one above |
| Policy binding | the Application bound to the admin group **only**, enabled, not negated |

**Closed by construction.** The only binding the role ever writes is to the admin group. There is no
setting that binds "any authenticated user": the deploy refuses `FORWARD_AUTH_ADMIN_GROUP: skuser` (the
every-user baseline group), and the attach step refuses to attach a catch-all whose Application has any
other enabled binding. The catch-all serves `admin-only` apps; an app that needs wider access gets its own
provider (design, section 4.7). On a new estate `skadmin` starts with no members, so nobody passes the
catch-all until you add the admin accounts to it: that is the fail-closed state, not a fault.

**The outpost attach is additive and is not in the blueprint.** An Authentik outpost's `providers` is a
many-to-many field that the blueprint importer saves as a full replacement set (and blueprints have no
merge mode for it), so an outpost entry listing this provider would silently detach every provider already
on the outpost, per-app `forward_single` providers included. Instead `tasks/forward_auth_attach.yml` runs
`src/sksso/forward_auth_attach.py` under `ak shell` in a running worker container (the way the CapAuth user
linker does, no API token needed, works on a brand-new cluster) and calls `outpost.providers.add()`, which
keeps what is there and does nothing when the provider is already attached. It waits (up to 5 minutes) for
the worker to apply the blueprint. It refuses, fails the deploy and attaches nothing when: the Application
is bound to anything but the admin group; another `forward_domain` provider already serves the same cookie
domain (two catch-alls for one domain are ambiguous); or the outpost does not exist or is not a proxy
outpost. An unattached provider is the safe state: the outpost cannot match the request, so forward auth
denies it.

**A hand-made catch-all is refused, not duplicated.** The blueprint matches the provider by name, and the
worker applies a rendered blueprint on its own, so an estate that already has a `forward_domain` provider
under another name and redeploys without `FORWARD_AUTH_PROVIDER_NAME` / `FORWARD_AUTH_APP_SLUG` would get a
second provider and application before the attach step could object. So a read-only preflight runs first,
before the blueprint is rendered: `src/sksso/forward_auth_preflight.py` under `ak shell` in the running server
container (no API token). When a `forward_domain` provider under another name covers the same cookie domain
(or a parent or sub domain of it), the deploy fails naming that provider and its application slug and the
settings to change (set the two names to adopt it, or `FORWARD_AUTH_ENABLED: false`); nothing is rendered,
nothing is edited in Authentik, and a blueprint rendered by an earlier run is removed from the mount. With no
running server container (a first install) the preflight is skipped.

**Knobs** (all optional, all in the sksso vault; the deploy refuses unusable values):

| Knob | Default | Meaning |
|---|---|---|
| `FORWARD_AUTH_ENABLED` | `true` | `false` opts out entirely: nothing is rendered or attached, a stale blueprint file this role wrote is removed, and nothing already in Authentik is deleted. Anything but a true/false spelling is refused (an unknown value would otherwise silently opt out). |
| `FORWARD_AUTH_PROVIDER_NAME` | `SKStacks forward auth (domain)` | The Proxy Provider's name, its identifier. Set it to an existing provider's name to adopt it. |
| `FORWARD_AUTH_APP_SLUG` | `skstacks-forward-auth` | The Application's slug, its identifier. Set it to an existing application's slug to adopt it. Not the skgit OIDC client id. |
| `FORWARD_AUTH_APP_NAME` | `SKStacks forward auth (admin-only apps)` | The Application's display name. |
| `FORWARD_AUTH_SSO_HOST` | `sso[-<env>].<base>` (`CAPAUTH_SSO_HOST` when set) | The host users reach Authentik on; the provider's `external_host` is `https://<it>`. |
| `FORWARD_AUTH_COOKIE_DOMAIN` | `<base>`: `<CLUSTERNAME>.<DOMAIN>`, or `<DOMAIN>` with `CLOUDFLARED` | The provider's cookie domain. Must be a multi-label domain that the SSO host sits under (a browser drops any other cookie), and must cover the hosts of every app behind `authentik@file`. Widen it (for example to `<DOMAIN>`) when apps live outside the cluster domain. |
| `FORWARD_AUTH_ADMIN_GROUP` | `skadmin` | The one group the Application is bound to. Never `skuser`. |
| `FORWARD_AUTH_OUTPOST` | `authentik Embedded Outpost` | The outpost the provider is attached to. |

**Contradictory settings fail the play** before anything is rendered: `FORWARD_AUTH_ENABLED: false` with any
other `FORWARD_AUTH_*` knob set (you cannot adopt what you opted out of), an unreadable
`FORWARD_AUTH_ENABLED`, a cookie domain the SSO host is not under, `FORWARD_AUTH_ADMIN_GROUP: skuser`, a
malformed slug or host, or `FORWARD_AUTH_APP_SLUG` equal to the skgit OIDC client id while
`SKGIT_OIDC_ENABLED` is on.

**Existing estates with a hand-made catch-all: adopt or opt out BEFORE redeploying sksso.** Look up the
provider's name and the application's slug in the Authentik admin UI, set `FORWARD_AUTH_PROVIDER_NAME` and
`FORWARD_AUTH_APP_SLUG` to them (and `FORWARD_AUTH_SSO_HOST`, `FORWARD_AUTH_COOKIE_DOMAIN` if the object
differs from the derived values: adoption sets mode, `external_host` and `cookie_domain` to the resolved
values), or set `FORWARD_AUTH_ENABLED: false`. Without either, the deploy creates a second catch-all next to
yours; the attach step refuses it (twin provider) and fails the deploy, and yours keeps serving, but the
play ends in error. Adoption never changes the Application's policy engine mode and never removes a
binding: after the first deploy, confirm the adopted Application is bound to the admin group alone
(`--who-passes <app slug>` below lists who passes it; the attach step refuses to attach otherwise, and the
check below reports C7 for it).

**Verify.** `check-access.py` (below) reports the catch-all missing, unbound, bound to anything but the admin
group, or attached to no outpost, all as C7. Run it after the first deploy of an estate.

**Rollback / opt out.** Set `FORWARD_AUTH_ENABLED: false` and redeploy: the blueprint file is removed and
nothing is attached again. The provider, application and binding already in Authentik stay (blueprint
removal never deletes objects); delete them by hand if you also want them gone, and detach the provider
from the outpost first, or requests behind `authentik@file` keep being gated by it.

## skgit (Forgejo) OIDC login

`sksso.SKGIT_OIDC_ENABLED: true` plus `sksso.SKGIT_OIDC_CLIENT_SECRET` (32+ chars,
`openssl rand -hex 32`) give `skgit` (Forgejo) a sign-in through Authentik as code,
when skgit's own `AUTH_MODE: oidc` is set (see `optional/skgit/README.md` "Sign-in
through Authentik (OIDC)", which creates the matching Forgejo auth source). Off by
default: with the flag unset or false, every rendered file is byte-identical to a
release without this feature (a secret alone changes nothing). On, the deploy renders
`/var/data/config/sksso-<env>/blueprints/skgit-oidc.yaml` (no secret inside) and mounts
the blueprints directory at `/blueprints/custom` (read only) in `server` and `worker`,
the same mechanism as the sections above. The secret lands in `sksso.env` (0600) as
`SKGIT_OIDC_CLIENT_SECRET`, which the blueprint reads with `!Env`.

**Which vault holds the secret.** Both ends hold the same value, one in each role's vault:
`sksso.SKGIT_OIDC_CLIENT_SECRET` in the sksso vault and `skgit.OIDC_CLIENT_SECRET` in the
skgit vault. Generate it once and put it in both.

The blueprint creates or adopts (matched by name or slug, so an object made by hand is
taken over, not duplicated):

| Object | Value |
|---|---|
| OAuth2 provider `skgit` | confidential, client id `skgit` (`SKGIT_OIDC_CLIENT_ID` overrides), strict redirect `https://<skgit host>/user/oauth2/<source name, URL-encoded>/callback`, authorization flow `default-provider-authorization-implicit-consent`, invalidation flow `default-provider-invalidation-flow`, signing key `authentik Self-signed Certificate`, subject mode `hashed_user_id`, issuer mode `per_provider`, scopes openid, email, profile, grant types authorization code and refresh token |
| Application `skgit` | name `SKGIT_APP_NAME` (default `SKGit (Forgejo)`), launch URL `https://<skgit host>/` |

**One source name, in both vaults.** Forgejo's callback path carries the auth source name,
so the redirect URI is built from it. It comes from one knob on each side that must hold
the same value: `sksso.SKGIT_OIDC_SOURCE_NAME` here and `skgit.OIDC_SOURCE_NAME` there
(default `SKStack SSO` on both, which renders `.../user/oauth2/SKStack%20SSO/callback`).
The name may hold letters, digits, spaces, dots, dashes and underscores; the deploy
refuses anything else.

**No policy binding here, as with sksec-web.** The Application slug (`skgit`) is the same
slug skgit's portal tile defaults `AUTHENTIK_TILE_SLUG` to, so
`shared/tasks/authentik_portal_tile.yml` (run from skgit's own deploy) finds this exact
Application, keeps this provider and manages the tile and its **group binding**: one
Authentik Application, not two. Who may sign in is therefore skgit's
`AUTHENTIK_TILE_GROUP` (default `skadmin`); widen it for everyone who should use SSO.

**CapAuth (PGP, passwordless).** `SKGIT_OIDC_AUTHENTICATION_FLOW: capauth-authentication`
binds the flow from "CapAuth login (optional)" above to this one application, the way
`skmesh.AUTHENTIK_AUTHENTICATION_FLOW` does for skmesh. It needs `CAPAUTH_ENABLED: true`
(the flow must exist). Unset (the default), the provider renders `authentication_flow:
null`, the brand's default login; removing the knob unbinds the flow again.

```yaml
sksso:
  SKGIT_OIDC_ENABLED: true                       # a YAML boolean, default false
  SKGIT_OIDC_CLIENT_SECRET: "..."                # same value as skgit.OIDC_CLIENT_SECRET
  SKGIT_OIDC_SOURCE_NAME: "SKStack SSO"          # same value as skgit.OIDC_SOURCE_NAME
  SKGIT_HOST: git.example.org                    # default git[-<env>].<base>
  SKGIT_OIDC_CLIENT_ID: skgit                    # default skgit (the portal tile slug)
  SKGIT_APP_NAME: "SKGit (Forgejo)"
  SKGIT_OIDC_AUTHENTICATION_FLOW: ""             # or capauth-authentication
```

`SKGIT_HOST` defaults to Forgejo's own default host (`git`, `git-<env>` outside prod, on
the cluster base domain, or the bare domain with `CLOUDFLARED`). Set it when skgit
overrides `SUBDOMAIN_PREFIX`. Set `SKGIT_OIDC_CLIENT_ID` when skgit overrides
`AUTHENTIK_TILE_SLUG` or `OIDC_CLIENT_ID`.

Run order: deploy (or redeploy) `sksso` with the flag and secret set, THEN deploy `skgit`
with its own `AUTH_MODE: oidc` (Forgejo reads Authentik's discovery document when it
registers the auth source, so the Application must exist and answer first; if it does
not, skgit's deploy stops with the Forgejo error and a redeploy after sksso fixes it).
Running skgit first does no harm to sksso: the portal tile creates a provider-less
Application that the next blueprint apply gives its provider. There is no teardown path: turning the flag off
stops the blueprint from being rendered or re-applied, but the provider and application
it created stay in Authentik (same limitation as the blueprints above); delete them by
hand when retiring the sign-in.


## skai (Open WebUI) OIDC login

`sksso.SKAI_OIDC_ENABLED: true` plus `sksso.SKAI_OIDC_CLIENT_SECRET` (32+ chars,
`openssl rand -hex 32`) give `skai` (Open WebUI) its sign-in through Authentik as code. skai
defaults to `AUTH_MODE: oidc`, so this is what an estate running skai turns on (see
`optional/skai/README.md` "Sign-in (Authentik OIDC)"). The mechanism is the skgit one above:
off by default, and with the flag unset every rendered file is byte-identical to a release
without it; on, the deploy renders `/var/data/config/sksso-<env>/blueprints/skai-oidc.yaml`
(no secret inside), mounts the blueprints directory at `/blueprints/custom` in `server` and
`worker`, and puts the secret in `sksso.env` (0600) as `SKAI_OIDC_CLIENT_SECRET`, read with
`!Env`.

**Which vault holds the secret.** `sksso.SKAI_OIDC_CLIENT_SECRET` in the sksso vault and
`skai.OIDC_CLIENT_SECRET` in the skai vault, the same value.

| Object | Value |
|---|---|
| OAuth2 provider `skai` | confidential, client id `skai` (`SKAI_OIDC_CLIENT_ID` overrides, same value as `skai.OIDC_CLIENT_ID`), strict redirects `https://<skai host>/oauth/oidc/login/callback` and the legacy `/oauth/oidc/callback`, the same flows, signing key, subject and issuer modes and grant types as skgit, scopes openid, email, profile (profile carries the `groups` claim skai maps to admin) |
| Application `skai` | name `SKAI_APP_NAME` (default `SKAI`), launch URL `https://<skai host>/` |

| Group `skai-users` | `SKAI_USERS_GROUP` overrides (same value as `skai.USERS_GROUP`); members = `SKAI_USERS` when set (the list IS the membership, re-applied by the worker), otherwise left alone |
| Bindings | the Application to the global admin group (`ADMIN_GROUP`, default `skadmin`, order 0) and to `skai-users` (order 10), policy engine mode `any` |

**Members only.** Only members of the two bound groups can sign in to skai (access mode
app-groups); the gate holds even with skai's portal tile off. skai's tile task (run from
skai's deploy) uses the same slug and groups, keeps this provider and finds these bindings.
Who becomes an Open WebUI admin is `skai.ADMIN_GROUP`, matched against the `groups` claim on
every login. Members are instance vars: the framework sets no member. A username in
`SKAI_USERS` that does not exist in Authentik makes the blueprint apply fail (shown in the
Authentik admin UI under System, Blueprints); create the user first.

```yaml
sksso:
  SKAI_OIDC_ENABLED: true                        # a YAML boolean, default false
  SKAI_OIDC_CLIENT_SECRET: "..."                 # same value as skai.OIDC_CLIENT_SECRET
  SKAI_HOST: ai.example.org                      # default skai[-<env>].<base>; same as skai.HOSTNAME
  SKAI_OIDC_CLIENT_ID: skai                      # default skai (the portal tile slug)
  SKAI_APP_NAME: "SKAI"
  SKAI_OIDC_AUTHENTICATION_FLOW: ""              # or capauth-authentication
  SKAI_USERS_GROUP: skai-users                   # default skai-users; same as skai.USERS_GROUP
  SKAI_USERS: []                                 # Authentik usernames; set = the whole membership
```

Run order: deploy (or redeploy) `sksso` with the flag and secret set, THEN deploy `skai`.

## skaegis (OIDC) login

`sksso.SKAEGIS_OIDC_CLIENT_SECRET` (32+ chars, `openssl rand -hex 32`) gives
skaegis (the Wazuh dashboard, `optional/skaegis`) its OpenID login as code.
`AUTH_MODE: oidc` is the skaegis default, so set this before the first skaegis
deploy. Unset, nothing changes. Set, the deploy renders
`/var/data/config/sksso-<env>/blueprints/skaegis-oidc.yaml` (no secret
inside), mounts the blueprints directory at `/blueprints/custom` (read only)
in `server` and `worker`, and puts the secret in `sksso.env` (0600) as
`SKAEGIS_OIDC_CLIENT_SECRET`, which the blueprint reads with `!Env`.

The blueprint creates or adopts:

| Object | Value |
|---|---|
| Group `skadmin` | adopted if it exists, created empty if not; members are managed in Authentik |
| OAuth2 provider `skaegis` | confidential, client id `skaegis` (`skaegis-<env>` outside prod; `SKAEGIS_OIDC_CLIENT_ID` overrides), strict redirect `https://<skaegis host>/auth/openid/login`, implicit-consent authorization flow, default invalidation flow, signing key `authentik Self-signed Certificate`, issuer mode `per_provider`, grant types authorization_code and refresh_token, scopes openid, email, profile and a private `groups` scope mapping |
| Application `skaegis` | name `SKAEGIS_APP_NAME` (default `skaegis`), launch URL `https://<skaegis host>/`, policy engine mode `any` |
| Policy binding | the application bound to `skadmin` (order 0, enabled, not negated) |

**Unlike the sksec-web and skpatch-hosts blueprints, this one owns the
binding.** skaegis is `admin-only` under APPLICATION_ACCESS_SSO_STANDARD and
sees every enrolled host's weaknesses, so the gate must stay closed even if
an instance switches its portal tile off. skaegis's own tile task uses the
same slug and the same group, finds this binding and reports it unchanged:
one Application, one binding. Inside the dashboard the indexer maps the
`groups` claim to its admin roles from `skadmin` only; `authentik Admins` is
never mapped.

```yaml
sksso:
  SKAEGIS_OIDC_CLIENT_SECRET: "..."   # same value as skaegis.OIDC_CLIENT_SECRET
  SKAEGIS_HOST: skaegis.example.org   # default skaegis[-<env>].<base>
  SKAEGIS_OIDC_CLIENT_ID: skaegis
  SKAEGIS_APP_NAME: "skaegis"
```

Run order: deploy `sksso` with the secret set, then deploy skaegis with the
matching `skaegis.OIDC_CLIENT_SECRET` and `skaegis.OIDC_ISSUER`
(`https://<sso host>/application/o/skaegis/`). The other order also
converges on the next sksso deploy, as for skpatch-hosts above.

## Application access and group model

Design (not yet implemented): [`docs/design/sso-app-access-provisioning.md`](../../../../docs/design/sso-app-access-provisioning.md).
Read this section for what is true today, and the design for where it is going.

**What the framework does today.**

- Every role with a portal tile binds it to ONE Authentik group, `AUTHENTIK_TILE_GROUP`. The default is
  `authentik Admins` on a release without PR #360 and `skadmin` with it. The group must already exist;
  nothing in the framework creates `skadmin`, `skuser` or any per-app group.
- The tile script only adds bindings. It never removes one, so changing the default does not move an
  existing tile off `authentik Admins`; it adds the new binding next to the old one.
- A provider-less tile is a bookmark. It controls portal visibility only. Access to a forward-auth
  service is decided by the application behind the matched provider, which for most services is the
  domain-wide catch-all. That catch-all is created and bound to `skadmin` by this role (see "Forward-auth
  catch-all (authentik@file)"); an estate that opted out or adopted a hand-made one is responsible for its binding.
- Authentik admits every authenticated user to an application that has NO policy bindings. An unbound
  catch-all therefore lets any user of the instance (including accounts that exist for other services)
  through forward auth. `skrac` and `skmesh` write no binding for their applications either. Run the
  access conformance check below on each instance: every application must have at least one binding.
- `OIDC_ADMIN_GROUPS` (sksec-web, skpatch-hosts) and `OIDC_ADMIN_GROUP` (skrac) are independent strings and
  are not derived from the Authentik binding.

**Target model (design).** Follows `standards/APPLICATION_ACCESS_SSO_STANDARD.md` in the sk-standards repo
(PROPOSED, sk-standards PR #70).

- Global groups `skadmin` (reaches every app) and `skuser` (an optional baseline bundle; never grants admin).
  Per-app groups `app-<app>-admins` and `app-<app>-users`. `authentik Admins` is reserved for administering
  Authentik itself and is never bound to an application.
- One block per app: `SSO_ACCESS: admin-only | app-groups | any-account | public` (default `admin-only`,
  fail-closed) and `SSO_BASELINE: true | false` (the standard's `in_baseline`, `app-groups` only, default
  false). `any-account` and `public` are reviewed opt-outs that need an exemption record. An estate list
  `sksso.BASELINE_APPS` overrides the per-role baseline flags.
- Two first-class ways to provision a person: add them to `skuser` for the baseline apps, or to individual
  `app-<app>-users` groups. Neither implies the other.
- `skuser` and `skadmin` are bound directly on each app; groups are never parents of one another for access,
  because Authentik allows one parent per group and inheritance direction is release dependent.
- The tile script would create the groups idempotently, bind them, and refuse to leave an application with
  zero bindings; the catch-all would be bound to `skadmin` only; OIDC role mapping defaults would derive
  from the per-app groups; a conformance check (the standard's C1 to C10) would list unbound applications and
  applications still bound to `authentik Admins`; a gate harness would prove the `sktest-user` account
  (`skuser` only) is denied non-baseline and admin tools and the `sktest-admin` account (`skadmin` only) is
  allowed.
- Any app that is not `admin-only` gets its own provider; the catch-all serves `admin-only` apps only.
  `skhub-users` and `skflow-operators` are renamed in place to `app-skhub-users` and `app-skflow-users`.
- Test identities (`sktest-admin`, `sktest-user`, `sktest-nogroup`, `sktest-appadmin`, `sktest-appuser`, and the
  service account `sksvc-test`) skip interactive MFA only as members of the `sktest` group, and still pass
  through the application access policy. Credentials are per-estate `sksso.TEST_*` vault keys.
- Existing instances migrate in additive steps first, and only later prune `authentik Admins` bindings.

### Access conformance check

`src/sksso/check-access.py` is the read-only conformance check (the standard's checks C1 to C10, design
section 6). Python stdlib only, GET requests only, the token on stdin (or `AUTHENTIK_TOKEN`), never on the
command line. Run it by hand from a framework checkout, against the instance's public SSO URL:

```bash
ansible-vault view --vault-password-file <pass> <sksso vault> \
  | python3 -c 'import sys,yaml; print(yaml.safe_load(sys.stdin)["sksso"]["api_token"])' \
  | python3 framework/v1/ansible/optional/sksso/src/sksso/check-access.py --url https://sso.<domain>
```

Output is one line per finding, `<slug> <check> <detail>` (a `warning:` or `info:` detail is not a
violation), then `#` summary lines. Exit 0 conforms, 1 at least one violation, 2 the check could not run
(including zero applications returned). `--strict` makes C2 (`authentik Admins` bindings) a violation instead
of a warning. `--declarations <file.json>` adds the checks that need each app's declared mode; without it
those are listed on a `# skipped:` line, not reported clean. `--who-passes <slug>` prints the usernames (no
emails) that the app's current bindings admit, the pre-flight review before binding the catch-all to
`skadmin`. The catch-all itself is checked without any declarations (C7): the check reports it missing (no
`forward_domain` provider at all), unbound, bound to anything other than the admin group (a group, a user,
a policy, a negated binding), or attached to no outpost, so a hand-made or adopted catch-all is held to the
same rule as the one this role creates. `sksso.ACCESS_CHECK: true` also runs it after every sksso deploy as a report that never fails the
deploy (`tasks/access_check.yml`, tag `access-check`).

## SSO for skhub and skflow

Authentik (this role) is the identity provider for two apps in the estate. Both use native OAuth2/OIDC, so
neither sits behind forward auth. Operator procedures are in
[`v1/docs/sso-user-management.md`](../../../docs/sso-user-management.md); the model is in
[`sso-provisioning.md`](../../../docs/sso-provisioning.md) and
[`sso-nextcloud-oidc.md`](../../../docs/sso-nextcloud-oidc.md).

| | skhub (Nextcloud) | skflow (Windmill) |
|---|---|---|
| Authentik application / provider | `skhub` / `skhub-nextcloud` (confidential, RS256, subject mode `user_id`) | `skflow-windmill` / `skflow-windmill` (this role's blueprint) |
| Strict redirect URI | `https://<skhub host>/apps/user_oidc/code` | `https://<skflow host>/user/login_callback/authentik` |
| Who may sign in | group `app-skhub-users` | group `skflow-operators` |
| Scopes | `openid email profile nextcloud` | `openid offline_access email profile` |
| Client side config | Nextcloud app `user_oidc` provider (`occ user_oidc:provider`) | Windmill global setting `oauths` (written by the skflow play) |
| Shipped as code today | blueprint documented, not yet rendered by this role | yes, `skflow-windmill.yaml.j2` |

### The `nextcloud` scope mapping

One scope mapping (scope name `nextcloud`) feeds Nextcloud two claims:

- `nextcloud_uid`: the user attribute `nextcloud_uid` if set, otherwise the Authentik username, and that value
  is written back to the attribute (pinned), so renaming the Authentik user later cannot create a second
  Nextcloud account. For a person whose existing Nextcloud uid differs from the Authentik username, set the
  attribute before their first login.
- `nextcloud_groups`: the names of the person's groups that start with `nc-`, prefix removed. Authentik
  `nc-hh-<family>` becomes Nextcloud `hh-<family>`.

The full expression is in `sso-provisioning.md`. Treat it as code: keep it in a blueprint so a rebuilt
Authentik gets it back.

### Groups

| Group | Meaning |
|---|---|
| `app-skhub-users` | may sign in to Nextcloud (bound to the `skhub` application) |
| `skflow-operators` | may sign in to Windmill (bound by the blueprint; Windmill CE limit: 10 SSO users) |
| `nc-<name>` | provisioning group: members get Nextcloud group `<name>` (only names matching the provider's whitelist regex are synced, for example `^(ops-team|hh-.+)$`) |
| `skadmin`, `skeditor`, `skreadonly` | portal tiers; independent of SSO |

### Recovery and SMTP

Passwords, MFA and recovery live here only. Self-service "forgot password" needs working SMTP
(`AUTHENTIK_EMAIL__HOST` and friends in `sksso.env`) and a brand recovery flow, for example the shipped
example blueprint `flows-recovery-email-verification.yaml`. An instance without SMTP must not enable the
login-page link (it would error); operators issue one-time recovery links with
`authentik.recovery.lib.create_recovery_token` instead (runbook: "Issue a recovery link"). Check the SMTP
status of the instance before promising self-service reset to anyone.

### Forward-auth leftovers and portal tiles

- Forward auth is not used for skhub or skflow. After switching skflow to native SSO (empty
  `skflow.SSO_MIDDLEWARE`), the old forward-auth Proxy application and provider for Windmill are unused:
  delete them and detach the provider from the embedded outpost after the next skflow deploy, or the portal
  shows a second, redundant tile.
- If you run a tile-sync script over a `tiles.yml`, the `skhub` tile must be declared provider-backed with its
  groups. A plain link tile with no provider detaches the OAuth provider and removes group bindings on the
  next run. Applications and groups not listed in `tiles.yml` (the Windmill application, `nc-*` groups) must be
  left untouched by the script; do not add them there.
- Rotating a client secret: runbook section "Rotate a client secret".

## Upgrading Postgres to 18

Framework default: `postgres:18.6` for the server and `postgres:18.7-bookworm` for the dump sidecar (both pinned `tag@sha256:digest`). Earlier releases ran Postgres 16.
**Postgres 18 cannot open a Postgres 16 data directory, and an upgrade is never automatic.**

What the framework does for you:

- **Fail-closed guard.** Before the stack deploy, `shared/tasks/postgres_major_guard.yml` reads
  `PG_VERSION` under `<sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>` and stops the play when the data on disk is another major than the
  image to deploy. The running stack is left untouched. Fresh installs (no `PG_VERSION`) pass.
- **New data layout.** 18+ images keep the cluster in `/var/lib/postgresql/<major>/docker` and declare
  `VOLUME /var/lib/postgresql`. For an 18+ image the compose file therefore mounts the data
  directory at `/var/lib/postgresql` (a fresh install persists at `<sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>/18/docker`). A
  pinned older image keeps the old mount target `/var/lib/postgresql/data`. Never mount
  `/var/lib/postgresql/data` under an 18 image: the real data would land on an anonymous volume.
  The guard is also what stops an 18 image from initialising an empty database beside old files.
- **Keep your current major.** Pin both images in the vault and redeploy; nothing else changes:

  ```yaml
  sksso:
    POSTGRES_IMAGE: "postgres:16.15@sha256:<digest>"
    POSTGRES_BACKUP_IMAGE: "postgres:16.15@sha256:<digest>"   # the dump sidecar should match the server major
  ```

Upstream support: Authentik's 2026.8 release notes include "fix docker compose pg volume path for compatibility with pg18", i.e. upstream's own compose moved to 18. The framework default `authentik_version` (2024.4.2) predates that: an instance still on a much older Authentik should pin `POSTGRES_IMAGE` to its current major until Authentik itself is upgraded.

PG18 defaults worth knowing (no action needed): `initdb` enables data checksums by default (a
dump-and-restore creates a checksummed cluster, a small CPU cost); `io_method` defaults to `worker`
(asynchronous I/O through background workers, no `io_uring` unless you set it, and no memory limit
change is needed); `md5` password hashes still work but log a deprecation warning, re-set the
password to move to SCRAM. `PGTEMP` in the compose file is not a Postgres setting and is ignored.

### Runbook: dump and restore (16 to 18)

Plan for a short outage (every login that goes through Authentik is down from step 2 to step 6). All commands run on the swarm
manager; `<env>` is your environment, `<stack>` is `sksso-<env>`.

1. **Dump from the running 16.** Stop the writers first so the dump is consistent, then dump the
   whole cluster with the old server's own tools:

   ```bash
   docker service scale <stack>_server=0 <stack>_worker=0
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec "$C" pg_dumpall -U <sksso.postgres_user> > /var/data/sksso-<env>/database-dump/pre-pg18.sql
   ls -l /var/data/sksso-<env>/database-dump/pre-pg18.sql
   tail -n 2 /var/data/sksso-<env>/database-dump/pre-pg18.sql   # must end "PostgreSQL database cluster dump complete"
   ```

   The file holds password hashes: keep it `0600` and delete it once the upgrade is verified.
2. **Stop the stack and move the old data aside** (this is your rollback copy, do not delete it yet):

   ```bash
   docker stack rm <stack>
   mv <sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres> <sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>.pg16
   mkdir -p <sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres> && chown 999:999 <sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>
   ```

3. **Pin nothing and deploy.** With the default image, run the usual deploy for `sksso`. The guard
   sees an empty directory (fresh install) and passes; the 18 image initialises `<sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>/18/docker`
   from the credentials in the stack's `postgres.env`/vault. Wait for the `postgres` service to report healthy.
4. **Stop the app again, drop the fresh database, restore.** Step 3 started Authentik's server and worker too, and it ran its
   migrations on the empty 18 cluster, so a restore on top collides with that schema (not only the
   bootstrap role). The backup sidecar would also dump the empty interim database if it fired now.
   Stop both, drop the fresh database, then restore the whole dump:

   ```bash
   docker service scale <stack>_server=0 <stack>_worker=0 <stack>_postgres-db-backup=0
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec "$C" psql -U <sksso.postgres_user> -d postgres \
     -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '<sksso.postgres_db, default authentik>' AND pid <> pg_backend_pid()" \
     -c 'DROP DATABASE "<sksso.postgres_db, default authentik>"'
   ```

   Then restore into the new cluster:

   ```bash
   C=$(docker ps -q -f name=<stack>_postgres)
   docker exec -i "$C" psql -U <sksso.postgres_user> -d postgres < /var/data/sksso-<env>/database-dump/pre-pg18.sql 2> /tmp/restore.err
   grep -v "already exists" /tmp/restore.err | head    # anything else here needs a look
   ```

   An `already exists` error for the bootstrap role is expected: `initdb` created it. The dump
   re-creates the database itself, so an error about its tables means the drop above did not run.
   With `DATA_NODE` set the directory lives on that node: run the `mv`/`mkdir` there (as root, mode `0700` for a local path).
5. **Verify:** `docker exec "$C" psql -U <sksso.postgres_user> -c "SHOW server_version" -c "SHOW data_checksums"`, then bring the
   writers back (`docker service scale <stack>_server=<n> <stack>_worker=<n> <stack>_postgres-db-backup=1`) and check the every login that goes through Authentik health endpoint and a real login or request.
6. **Clean up** after a few days of good operation: remove `<sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>.pg16` and the dump file.

**Rollback.** The old directory is untouched. `docker stack rm <stack>`, move `<sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>` aside,
move `<sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>.pg16` back to `<sksso.POSTGRES_DATA_PATH, default /var/data/runtime/sksso-<env>/postgres>`, pin `sksso.POSTGRES_IMAGE` and `sksso.POSTGRES_BACKUP_IMAGE` to the 16 image
(see above), redeploy. Data written after the upgrade is not carried back; restore it from a dump if needed.

Order matters: if the deploy runs before step 2 the guard stops it with the message that names this
section. That is the intended behaviour, not a fault.

## API router (raw status codes for machine traffic)

The router chain usually ends in `error-pages@file`, which replaces any backend 4xx/5xx with an HTML page and
replays the original method there, so API clients never see the app's own status or JSON error. `sksso[-env]-server-api` carries the
machine paths (`/application/o/` (OIDC), `/api/v3/`, `/-/` (health, metrics), `/.well-known/` (rendered when `router_middlewares` is set)) through the same chain with each error-pages-bearing chain swapped for its
no-error-pages form; anything else in the chain (CrowdSec, SSO, custom middlewares) is kept. Vault key
`API_MIDDLEWARES` replaces the derived chain whole. `default-no-error-pages@file` has neither `default@file`'s
optional IP allowlist nor CrowdSec: set `API_MIDDLEWARES` if the main chain relies on them. See "API routers" in
`docs/v1-service-catalog.md`.
