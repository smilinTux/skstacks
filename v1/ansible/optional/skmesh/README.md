# skmesh (Netbird)

Self-hosted WireGuard mesh VPN (Netbird: management, signal, relay,
dashboard, Postgres) with optional bundled coturn, fronted by Traefik. Users
log in either with NetBird's own embedded IdP (`AUTH_MODE: standalone`,
default) or through this cluster's Authentik SSO (`AUTH_MODE: authentik`).
An optional service with `dev` and `prod` playbooks (the dev one exists for
test clusters; hostnames get the usual `-dev` suffix).

## Dashboard image

`skmesh.DASHBOARD_IMAGE` (optional) lets an instance pin its own dashboard
image instead of the framework default. This exists because an instance
already running a private, branded dashboard fork (for example
`ghcr.io/smilintux/skmesh`) would otherwise have that image silently
swapped out from under it on the next deploy from this framework - the
default is deliberately not the only option.

Default (unset): the upstream `netbirdio/dashboard` image, pinned by
digest (see `src/config/skmesh/skmesh.yml.j2`), since that image publishes
no semver release tags, only `main` and per-commit `sha-*` tags - a digest
is the only reproducible pin available as a framework default. This is not
pullable from a private fork's own registry, so an instance that depends
on one (see "Instance notes" below) must set `DASHBOARD_IMAGE` explicitly;
this framework does not ship or publish that fork's image.

## Authentication (`AUTH_MODE`)

`skmesh.AUTH_MODE` picks where users log in. Default: `standalone`.

| | `standalone` (default) | `authentik` |
|---|---|---|
| IdP | NetBird's embedded IdP (Dex), served by management at `https://<skmesh host>/oauth2` | the cluster's sksso (Authentik), application `AUTHENTIK_APP_SLUG` (default `skmesh`) |
| users | local email/password users kept by NetBird | Authentik users (password today, whatever Authentik's flows allow later) |
| first login | the dashboard's setup page creates the owner (`POST /api/setup`, open until an owner exists: do it right after the first deploy) | the first Authentik user to log in becomes the owner |
| needs | nothing else | sksso deployed, `sksso.api_token` registered (sksso README), `IDP_MANAGER_PASSWORD`, `SSO_HOST_IP` |
| CLI login | PKCE via `http://localhost:53000/` (Dex `netbird-cli` client) | PKCE via `http://localhost:53000`; device code flow with `DEVICE_AUTH_FLOW: true` |

Why `standalone` is the default: it is the only mode that works with no
other stack and no one-time setup, which is what a framework default should
be (and it is NetBird's own default since v0.62). `authentik` is a
deliberate opt-in because it needs sksso and its automation token. An
instance whose vault predates `AUTH_MODE` and carries
`NETBIRD_AUTH_CLIENT_ID` (it was Authentik-wired) must set `AUTH_MODE`
explicitly: the deploy refuses to guess, so it never silently moves an
Authentik install onto a different user store.

**`authentik` mode is automated.** Before the stack starts (NetBird
management exits at startup if its issuer's discovery document is missing),
the deploy runs `tasks/authentik.yml`: from a throwaway container on
`sksso-<env>`'s overlay network it calls the Authentik API with
`sksso.api_token` (read from sksso's vault) and creates or updates, all
idempotent and drift-correcting:

- OAuth2/OpenID provider `<AUTHENTIK_APP_SLUG>`: public client
  `NETBIRD_AUTH_CLIENT_ID` (default `skmesh`), PKCE, `sub` = the user's
  numeric id (NetBird's Authentik IdP manager looks users up by it),
  redirect URIs `https://<skmesh host>/nb-auth`, `/nb-silent-auth` and
  `http://localhost:53000`, scopes openid, email, profile, offline_access
  and goauthentik.io/api, the default self-signed signing key;
- application `<AUTHENTIK_APP_SLUG>` with that provider;
- service account `IDP_MANAGER_USERNAME` (default `skmesh-idp-manager`) in a
  group whose role grants only `authentik_core.view_user`, with an
  app-password token whose key is `IDP_MANAGER_PASSWORD`;
- with `DEVICE_AUTH_FLOW: true`, the default brand's device code flow, only
  if the brand has none (never overwritten).

Every secret goes in on stdin; the task is `no_log` and the provisioner
prints status lines only. Management reaches `sso<-env>.<base>` through an
`extra_hosts` entry to `SSO_HOST_IP` (the edge), and the dashboard's
`AUTH_*` settings are rendered from the same values as `management.json`.
If sksso's certificate is signed by a private CA, give management that CA
with `EXTRA_CA_CERT` (PEM); it is mounted into `/etc/ssl/certs/`.

If `sksso` enforces CSRF-trusted origins, add this stack's `skmesh.*`
hostname to `sksso.csrf_extra_origins` in sksso's vault so login redirects
are not rejected.

## TURN / STUN

Set `skmesh.DEPLOY_COTURN: true` to run coturn as part of this stack;
leave it `false` (default) if the cluster already runs a shared TURN
server (e.g. eturnal, or another app's coturn) that Netbird's `TURNConfig`
should point at instead.

To use a shared TURN server that authenticates with a shared secret
(coturn `use-auth-secret` + `static-auth-secret`, eturnal `secret`, the
"TURN REST API" scheme that Nextcloud Talk also uses):

```yaml
skmesh:
  TURN_URI: "turn:<turn-host>:3478"          # default: turn:turn.<base>:3478
  STUN_URIS: ["stun:<stun-host>:3478"]       # default: [stun:turn.<base>:3478]
  TURN_TIME_BASED_CREDENTIALS: true          # default: false (static TURN_USER/TURN_PASSWORD)
  TURN_SECRET: "<that server's shared secret>"
```

Management then hands each peer HMAC-SHA1, time-limited TURN credentials
(`CredentialsTTL` 12h) derived from `TURN_SECRET`; `TURN_USER` and
`TURN_PASSWORD` are unused and may be omitted. The other app's credentials
are untouched: both derive from the same secret. Point `STUN_URIS` at a
STUN server that sees the client's real source address: a TURN server
published through Docker Swarm's ingress routing mesh answers STUN with the
ingress SNAT address, which is useless as a server-reflexive candidate.

## Relay

The NetBird relay listens on `:33080` inside the stack and is routed by
Traefik at `PathPrefix(/relay)` on the `skmesh` host (the WebSocket path
NetBird clients dial). Set `NETBIRD_RELAY_ENDPOINT` to
`rels://skmesh.<base>:443` (keep the `rels://` scheme: TLS ends at Traefik).
Peers whose relay support is on use the relay, not TURN relay candidates,
for traffic that cannot go peer to peer.

## Images

`MANAGEMENT_IMAGE`, `SIGNAL_IMAGE`, `RELAY_IMAGE`, `POSTGRES_IMAGE` and
`POSTGRES_BACKUP_IMAGE` (optional) override the framework defaults, e.g. to
pin `tag@sha256:digest`.

## Required instance vars (vault)

`skmesh-prod_vault.yml` (see the framework README's "Instance contract" for
the lookup path):

```yaml
skmesh:
  # REQUIRED - no framework default, these are estate-specific
  CLUSTERNAME: "<your-cluster-name>"
  DOMAIN: "<your-domain>"
  NETBIRD_DATASTORE_ENC_KEY: "CHANGE_ME"   # openssl rand -base64 32
  NETBIRD_RELAY_AUTH_SECRET: "CHANGE_ME"   # openssl rand -base64 32
  POSTGRES_PASSWORD: "CHANGE_ME"           # openssl rand -base64 32
  TURN_PASSWORD: "CHANGE_ME"               # openssl rand -base64 32 (unused with TURN_TIME_BASED_CREDENTIALS)
  TURN_SECRET: "CHANGE_ME"                 # openssl rand -base64 32, or the shared TURN server's secret
  TURN_USER: ""                            # unused with TURN_TIME_BASED_CREDENTIALS
  COTURN_EXTERNAL_IP: ""
  NETBIRD_RELAY_ENDPOINT: ""     # e.g. rels://skmesh.yourdomain.com:443

  # optional
  AUTH_MODE: standalone          # standalone (default) | authentik, see "Authentication"
  # authentik mode (required there, unused in standalone)
  IDP_MANAGER_PASSWORD: ""       # openssl rand -hex 32: the IdP manager's app password
  SSO_HOST_IP: ""                # IP where the management container reaches sksso (the edge)
  # authentik mode, optional
  NETBIRD_AUTH_CLIENT_ID: skmesh # OIDC client id (public client)
  AUTHENTIK_APP_SLUG: skmesh     # application/provider slug; issuer https://sso<-env>.<base>/application/o/<slug>/
  AUTHENTIK_APP_NAME: SKMesh
  IDP_MANAGER_USERNAME: skmesh-idp-manager
  DEVICE_AUTH_FLOW: false        # true: device code flow for headless `netbird up` (sets the brand's flow if unset)
  SSO_HOST: ""                   # default sso<-env>.<base>
  EXTRA_CA_CERT: ""              # PEM of a private CA in front of sksso
  AUTHENTIK_INTERNAL_URL: ""     # default http://sksso-<env>_server:9000
  AUTHENTIK_NETWORK: ""          # default sksso-<env>
  AUTHENTIK_VAULT_FILE: ""       # default: sksso's vault beside this one
  AUTHENTIK_AUTHORIZATION_FLOW: default-provider-authorization-implicit-consent
  AUTHENTIK_INVALIDATION_FLOW: default-provider-invalidation-flow
  AUTHENTIK_SIGNING_KEY: "authentik Self-signed Certificate"
  PROVISIONER_IMAGE: ""          # default python:3.13.15-alpine pinned by digest
  CLOUDFLARED: false             # true: BASE_DOMAIN = DOMAIN (no cluster prefix)
  DEPLOY_COTURN: false           # true to bundle coturn in this stack
  DASHBOARD_IMAGE: ""            # pin your own dashboard image; default: upstream netbirdio/dashboard (see "Dashboard image" above)
  TURN_URI: ""                   # default turn:turn.<base>:3478 (see "TURN / STUN")
  STUN_URIS: []                  # default [stun:turn.<base>:3478]
  TURN_TIME_BASED_CREDENTIALS: false
  MANAGEMENT_IMAGE: ""           # SIGNAL_IMAGE, RELAY_IMAGE, POSTGRES_IMAGE, POSTGRES_BACKUP_IMAGE likewise
  TURN_MAX_PORT: "65535"
  TURN_MIN_PORT: "49152"
  TURN_REALM: "skmesh."
  TURN_SERVER_NAME: "inventory_hostname"
  TZ: "UTC"

# optional - override this cluster's skmesh overlay network (default shown
# is this framework's reserved range; must stay unique per
# v1/tests/test_unique_subnets.py)
skmesh_prod_networks:
  - name: "skmesh-prod"
    subnet: "172.16.243.0/24"
  - name: "cloud-public-prod"
    subnet: "172.16.200.0/24"
# dev: skmesh_dev_networks, default skmesh-dev 172.16.244.0/24 + cloud-public-dev
```

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skmesh/deploy_skmesh-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```
