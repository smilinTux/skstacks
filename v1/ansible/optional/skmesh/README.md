# skmesh (Netbird)

Self-hosted WireGuard mesh VPN (Netbird: management, signal, relay,
dashboard, Postgres) with optional bundled coturn, fronted by Traefik and
authenticating against this cluster's Authentik SSO. An optional service,
prod-only (no dev/staging variant is provided upstream).

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

## SSO integration

`skmesh` expects an Authentik OIDC application named `skmesh` on this
cluster's `sksso` instance (`AuthIssuer`/`OIDCConfigEndpoint` in
`management.json.j2` point at `https://sso.<cluster>.<domain>/application/o/skmesh/`).
Because the management container reaches `sksso` by an `extra_hosts` entry
rather than public DNS, `skmesh.SSO_HOST_IP` (required) must be the IP where
that container can reach the SSO service.

If `sksso` enforces CSRF-trusted origins, add this stack's `skmesh.*`
hostname to `sksso.csrf_extra_origins` in sksso's vault so login redirects
are not rejected.

## Coturn

Set `skmesh.DEPLOY_COTURN: true` to run coturn as part of this stack;
leave it `false` (default) if the cluster already runs a shared TURN
server (e.g. eturnal) that Netbird's `TURNConfig` should point at instead.

## Required instance vars (vault)

`skmesh-prod_vault.yml` (see the framework README's "Instance contract" for
the lookup path):

```yaml
skmesh:
  # REQUIRED - no framework default, these are estate-specific
  CLUSTERNAME: "<your-cluster-name>"
  DOMAIN: "<your-domain>"
  NETBIRD_AUTH_CLIENT_ID: ""     # Authentik OIDC client ID for skmesh
  NETBIRD_DATASTORE_ENC_KEY: "CHANGE_ME"   # openssl rand -base64 32
  NETBIRD_RELAY_AUTH_SECRET: "CHANGE_ME"   # openssl rand -base64 32
  POSTGRES_PASSWORD: "CHANGE_ME"           # openssl rand -base64 32
  TURN_PASSWORD: "CHANGE_ME"               # openssl rand -base64 32
  TURN_SECRET: "CHANGE_ME"                 # openssl rand -base64 32
  TURN_USER: ""
  COTURN_EXTERNAL_IP: ""
  NETBIRD_RELAY_ENDPOINT: ""     # e.g. rels://skmesh.yourdomain.com:443
  SSO_HOST_IP: ""                # IP where the management container reaches sksso

  # optional
  CLOUDFLARED: false             # true: BASE_DOMAIN = DOMAIN (no cluster prefix)
  DEPLOY_COTURN: false           # true to bundle coturn in this stack
  DASHBOARD_IMAGE: ""            # pin your own dashboard image; default: upstream netbirdio/dashboard (see "Dashboard image" above)
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
```

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skmesh/deploy_skmesh-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```
