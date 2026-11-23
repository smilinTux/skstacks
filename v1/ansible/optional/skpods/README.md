# SKPods

Solid pod hosting for an SKStacks cluster. Every user gets a
[Solid](https://solidproject.org/) pod (a WebID plus personal storage) at
`https://pods[-<env>].<cluster>.<domain>`, and apps read and write it over
plain Solid HTTP with Solid-OIDC and DPoP, with the user's consent.

The service is defined by one setting, `skpods.ENGINE`:

| Engine | Status | What runs |
|---|---|---|
| `nextcloud` | implemented (default) | A standalone, slim Nextcloud whose only added app is Solid, plus Postgres, Redis and the Nextcloud cron. No Talk, Office, notify_push or other extras, and most default apps disabled. |
| `rust` | planned | Not implemented: the deploy refuses any engine other than `nextcloud`. A new engine adds `tasks/engine_<name>.yml` and `tasks/seed_<name>.yml` with the same contract (serve Solid at the public host, seed users the same way). |

Start here: [SOP.md](./SOP.md) has the full operating procedure (deploy,
seed and reset, verification, troubleshooting).

## Quickstart

1. Build and push an engine image (see [Engine image](#engine-image)) and
   note its digest.
2. Add an `skpods` block to the instance vault (minimal snippet below).
3. Deploy:

```bash
ansible-playbook -i envs/dev/inventory.ini \
  framework/v1/ansible/optional/skpods/deploy_skpods-dev.yml \
  -e target_manager_group=swarm_managers \
  -e skstacks_vault_dir=$(pwd)/v1/ansible \
  --vault-password-file ~/.vault_pass_env/.<instance>_dev_vault_pass
```

4. Check discovery: `curl -s https://pods-dev.<cluster>.<domain>/.well-known/openid-configuration`
   must show `"issuer": "https://pods-dev.<cluster>.<domain>"`.

Minimal vault snippet (`optional/group_vars/<env>/skpods-<env>-<domain>-<cluster>_vault.yml`):

```yaml
skpods:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.com"
  NEXTCLOUD_IMAGE: "registry.example.com/skpods/skpods-nextcloud:35.0.3-solid-<sha>@sha256:<digest>"
  ADMIN_PASSWORD: "<16+ characters>"
  DB_PASSWORD: "<16+ characters>"
```

## Engine image

The `nextcloud` engine does not use the Nextcloud app store. The instance
builds a thin derived image: the official `nextcloud:<version>-apache` image
(pinned by digest) plus the Solid app source the instance chooses, with its
composer `vendor/` tree, under `/usr/src/skpods-apps/solid`. A
`before-starting` entrypoint hook in that image copies the app into
`custom_apps/solid` on every start, so a new image digest always ships its
own app code, and finishes an app upgrade (`occ upgrade`) if one is pending.

The role only pins that image by digest (`NEXTCLOUD_IMAGE`). Where the image
is built and which Solid app revision it carries is the instance's choice.
Rollback is pinning the previous digest and redeploying. The deploy turns
the Nextcloud app store off (`appstoreenabled=false`) before it enables
anything, because `occ app:enable` of an app that is not on disk yet
downloads the store build. It waits until `custom_apps/solid` is exactly the
image's copy, enables it, and checks again; it fails closed otherwise (and
when the image has no `/usr/src/skpods-apps/solid`). An engine image should
also ship `appstoreenabled => false` in a config snippet, so the store is off
from the very first start.

## Configuration (vault)

| Key | Default | Meaning |
|---|---|---|
| `CLUSTERNAME`, `DOMAIN` | inventory `cluster_name`, `domain` | Build the hostname. |
| `ENGINE` | `nextcloud` | Engine selector (`SKPODS_ENGINE`). Only `nextcloud` is implemented. |
| `HOST_PREFIX` | `pods` | Hostname is `<HOST_PREFIX>[-<env>].<cluster>.<domain>`. |
| `HOSTNAME` | unset | Full hostname override. **Permanent once pods exist** (SOP "Hostname is permanent"). |
| `NEXTCLOUD_IMAGE` | required | Engine image, `repo:tag@sha256:digest`. |
| `ADMIN_USER` | `skpods-admin` | Nextcloud install admin. |
| `ADMIN_PASSWORD` | required | Install admin password (16+ characters). |
| `DB_PASSWORD` | required | Postgres password (16+ characters). |
| `DB_USER`, `DB_NAME` | `nextcloud` | Postgres role and database. |
| `POSTGRES_IMAGE`, `REDIS_IMAGE` | pinned digests | Override the datastore images (keep them pinned). |
| `REGISTRY_LOGIN` | unset | `{registry, username, password}`: `docker login` on the manager before the stack deploy (`--with-registry-auth` hands it to the node that pulls). Needed when the engine image is private. Use a read-only pull token. |
| `INGRESS_NETWORK` | `cloud-public-<env>` | The Traefik overlay the Nextcloud service joins. A dev stack on a cluster whose Traefik only joins `cloud-public-prod` sets `cloud-public-prod`, and lists it in `skpods_<env>_networks` too. |
| `skpods_<env>_networks` (top-level) | `skpods-<env>` + `cloud-public-<env>` | Overlay networks and subnets (`172.16.184/185/186.0/24` for prod/staging/dev). |
| `TRUSTED_PROXIES` | subnet of `INGRESS_NETWORK` | Nextcloud `trusted_proxies` (space separated). |
| `MIDDLEWARES` | `skpods[-<env>]-headers` | The router's whole middleware chain. See [Traefik](#traefik) before changing it. |
| `TLS_OPTIONS` | unset | Router `tls.options`. |
| `DATA_NODE` | unset | Pin all services to this node (required when a path below is node-local). |
| `APP_PATH` | `/var/data/skpods-<env>` | Nextcloud `html/`, `config/`, `data/` (pods live in each user's Files under `solid/`). |
| `POSTGRES_PATH`, `REDIS_PATH` | `/var/data/runtime/skpods-<env>/{postgres,redis}` | Datastore paths. |
| `PLACEMENT_CONSTRAINTS` | `[]` | Extra swarm constraints for every service. |
| `DISABLE_APPS` | built-in list | Default Nextcloud apps to disable (best effort; always-enabled apps are skipped). |
| `DEFAULT_PHONE_REGION`, `MAINTENANCE_WINDOW_START`, `TZ` | `US`, `1`, `UTC` | Nextcloud housekeeping. |
| `PHP_MEMORY_LIMIT`, `PHP_UPLOAD_LIMIT` | `512M`, `1G` | PHP limits. |
| `NEXTCLOUD_MEMORY_LIMIT`, `POSTGRES_MEMORY_LIMIT`, `REDIS_MEMORY_LIMIT`, `CRON_MEMORY_LIMIT` | `1536M`, `512M`, `128M`, `512M` | Container limits. |
| `first_install_wait_seconds` | `1200` | How long the play waits for the first install. |
| `SEED_USERS` | `[]` | Test or demo users; see [Seed and reset](#seed-and-reset). |
| `CREDENTIALS_OUT` | unset | Controller path for minted client credentials (JSON, 0600). |
| `AUTHENTIK_TILE`, `AUTHENTIK_TILE_*` | `true` | Portal tile (a bookmark only; it never puts Authentik in front of the service). |

## Traefik

Solid clients are browser apps on other origins and headless agents, so the
router is deliberately plain:

- The default middleware chain is only the role's own headers middleware
  (HSTS, `nosniff`). It sets **no** `Access-Control-*` header: the Solid app
  answers CORS and `OPTIONS` itself on `/apps/solid/*` and `/.well-known/*`,
  and a proxy-level CORS header would override it.
- No error-pages middleware: it would replace Solid's `401`/`403` bodies and
  headers (`WWW-Authenticate`).
- No forward auth (Authentik or other) on any path: Solid clients call
  `/apps/solid/*` and `/.well-known/*` cross-origin without an SSO session,
  and DPoP requests must not be redirected.
- No carddav/caldav `.well-known` redirect, so `/.well-known/openid-configuration`
  and `/.well-known/solid` reach Nextcloud unchanged.
- `Authorization` and `DPoP` request headers pass through (Traefik's default).
- Rate limiting: none by default. Solid apps poll and probe ACLs (many `401`,
  `403` and `404` answers), so an IP-reputation or rate-limit middleware can
  lock real clients out; add one through `MIDDLEWARES` only with limits that
  allow that traffic.

## Seed and reset

`SEED_USERS` lists users to create on every deploy (existing users keep
their pods; their password and display name are set again):

```yaml
skpods:
  CREDENTIALS_OUT: "/root/skpods-dev-credentials.json"   # controller path, 0600
  SEED_USERS:
    - { id: alice, display_name: "Alice", password: "<12+ chars>" }
    - { id: robot, password: "<12+ chars>", client_credentials: true }
    - { id: keeper, password: "<12+ chars>", resettable: false }
```

- `--tags seed` runs only the seed (plus the lookups it needs).
- `--tags seed -e skpods_reset=true` deletes and recreates every user with
  `resettable: true` (the default), and with it their pod data. Users with
  `resettable: false` are never touched unless named:
  `-e '{"skpods_reset_users": ["keeper"]}'`.
- `client_credentials: true` mints, for a newly created or reset user, a
  Nextcloud app password and Solid client credentials bound to the user's
  WebID (`POST /apps/solid/client-credentials`). They are written only to
  `CREDENTIALS_OUT` (or `-e skpods_credentials_out=...`) on the controller,
  never printed. `-e skpods_mint_credentials=true` mints a fresh pair for
  existing users too.

## Networks

| Network | Purpose |
|---|---|
| `skpods-<env>` | Service-local overlay: `172.16.184.0/24` (prod), `172.16.185.0/24` (staging), `172.16.186.0/24` (dev) |
| `INGRESS_NETWORK` | Traefik ingress (default `cloud-public-<env>`) |

## Storage

Everything is under `/var/data` by default (shared storage): the code tree,
config and Files (`APP_PATH`), Postgres and Redis (`/var/data/runtime`).
A user's pod is the `solid/` folder in their Files, so it is backed up and
inspectable as ordinary files.

## Related projects / See also

- [SOP.md](./SOP.md): deploy, verify, reset, troubleshoot.
- [`skhub`](../skhub/README.md): the full Nextcloud role (file sync and
  collaboration). skpods is a separate, slim Nextcloud and shares nothing
  with it.
- [Solid Protocol](https://solidproject.org/TR/protocol),
  [Solid-OIDC](https://solidproject.org/TR/oidc),
  [WAC](https://solidproject.org/TR/wac).
- [docs/v1-service-catalog.md](../../../../docs/v1-service-catalog.md#skpods).
