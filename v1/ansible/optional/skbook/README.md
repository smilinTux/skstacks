# skbook (BookStack wiki)

BookStack (the LinuxServer.io image) plus its supporting MariaDB database and
a `db-backup` sidecar, on Docker Swarm. Modeled structurally on
[skmem-pg](../skmem-pg) (app + db + backup) and the Traefik router-hook
convention from [skhub](../skhub)/[skdash](../skdash).

## Architecture

- Services: `bookstack` (app), `db` (MariaDB), `db-backup` (mysqldump sidecar)
- Networks: `skbook-<env>` (internal, app to db to backup), `cloud-public-<env>`
  (external, shared with Traefik - see the framework README's "Instance
  contract")
- Bind mounts:
  - `/var/data/skbook-<env>/config:/config` (BookStack app data)
  - `/var/data/runtime/skbook-<env>/db:/var/lib/mysql` (MariaDB data, a local
    bind on the manager, not NFS, so MariaDB never does file locking over
    a network filesystem)
  - `/var/data/skbook-<env>/database-dump:/dump` (backup sidecar output)

## Overlay network subnets

| Network | Env | Subnet |
|---|---|---|
| `skbook-dev` | dev | `172.16.220.0/24` |
| `skbook-staging` | staging | `172.16.221.0/24` |
| `skbook-prod` | prod | `172.16.153.0/24` |
| `cloud-public-dev` | dev | `172.16.202.0/24` (shared) |
| `cloud-public-staging` | staging | `172.16.201.0/24` (shared) |
| `cloud-public-prod` | prod | `172.16.200.0/24` (shared) |

## Deploy race safety

Every `deploy_skbook-<env>.yml` selects a single manager
(`hosts: selected_manager_group`, via `select_manager_node.yml`) for all real
work, in dev, staging and prod alike. A play targeting the whole manager
group would run `docker stack deploy` from every manager in parallel on
first creation, racing on the shared `/var/data` tree.

## Required instance vars (vault)

`skbook-<env>_vault.yml` (see the framework README's "Instance contract" for
the lookup path):

```yaml
skbook:
  # REQUIRED - no framework default, these are all estate-specific
  MYSQL_ROOT_PASSWORD: "..."
  DB_DATABASE: bookstack
  DB_USER: bookstack
  DB_PASSWORD: "..."
  APP_KEY: "base64:..."            # Laravel app key - generate once, then pin it
  CLUSTERNAME: "<your-cluster-name>"
  DOMAIN: "<your-domain>"

  # optional
  PUID: 1000                        # default: 1000
  PGID: 1000                        # default: 1000
  TZ: UTC                            # default: UTC
  APP_URL: "https://wiki.example.com"  # default: derived host rule below
  DB_PORT: 3306                      # default: 3306
  BOOKSTACK_VERSION: v26.09-ls285    # default shown
  EXPORT_PAGE_SIZE: letter           # default: letter
  BACKUP_FREQUENCY: 86400            # default: 86400 (seconds)
  BACKUP_NUM_KEEP: 7                 # default: 7
  replicas: 1                        # default: 1

  # mail (SMTP) - all optional, only rendered if MAIL_HOST is set
  MAIL_HOST: "smtp.example.com"
  MAIL_DRIVER: smtp                  # default: smtp
  MAIL_PORT: 587                     # default: 587
  MAIL_USERNAME: "..."
  MAIL_PASSWORD: "..."
  MAIL_ENCRYPTION: tls               # default: tls
  MAIL_FROM: "bookstack@example.com" # default: bookstack@<cluster>.<domain>

  # SSO / OIDC - OFF by default
  ENABLE_SSO: false                  # default: false
  OIDC_NAME: SSO                     # default: SSO
  OIDC_CLIENT_ID: "..."              # required if ENABLE_SSO is true
  OIDC_CLIENT_SECRET: "..."          # required if ENABLE_SSO is true
  OIDC_ISSUER: "https://sso.example.com"  # required if ENABLE_SSO is true

  # Traefik router hooks - never hardcoded, empty/unset by default
  router_middlewares: []             # appended after the built-in router
  router_middlewares_pre: []         # prepended before the built-in router
  tls_options: ""                    # e.g. "generic-tls@file"
  sticky_samesite: ""                # e.g. "strict"
```

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skbook/deploy_skbook-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

## Verifying health

```bash
curl -sf https://skbook[-<env>].<your-domain>/
```

or, from the swarm manager: `docker service logs <stack>_bookstack` /
`docker service ps <stack>_bookstack` where `<stack>` is `skbook-<env>`.
