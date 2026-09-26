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
  backup_num_keep: 7               # default: 7
  backup_frequency: 1d              # default: 1d
  placement_use_worker_constraint: true   # default true; set false if the
                                           # cluster has no worker nodes
  placement_exclude_nodes: []      # optional list of node hostnames to exclude

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
  email_from: "..."

  # GeoIP - optional, only rendered if both geoip vars below are set
  geoip_account_id: "..."
  geoip_license_key: "..."
```

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
