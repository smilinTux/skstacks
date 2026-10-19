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
  - `/var/data/runtime/sksso-<env>/postgres:/var/lib/postgresql/data`
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
  `authentik@file` (defined wherever your Traefik middleware config lives) to
  that service's router middlewares. Point the middleware's address at
  `http://sksso-<env>_server:9000/outpost.goauthentik.io/auth/traefik` and
  configure an Authentik Proxy Outpost of type "Traefik" to match.
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
| Who may sign in | group `skhub-users` | group `skflow-operators` |
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
| `skhub-users` | may sign in to Nextcloud (bound to the `skhub` application) |
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
