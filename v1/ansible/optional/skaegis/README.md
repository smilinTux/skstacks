# skaegis

**Deploys continuous CVE detection on the swarm: an unmodified upstream [Wazuh](https://github.com/wazuh/wazuh-docker) 4.14.10 stack (GPL-2.0) of indexer, manager, dashboard and a backup sidecar, every image pinned by digest, with an OpenID login against sksso.** skaegis is the Detect step of the suite: Defend (sksec), Detect (skaegis), Remediate ([skpatch](../../../../docs/skpatch-program.md)). **Maturity tier:** T0, Classical (standard TLS and the upstream product's own security plugin; no hybrid-KEM or post-quantum claim). **Version phase:** framework `v1` tree (this repo's [Versions table](../../../../README.md#versions): "Published, one service at a time").

Opt-in: nothing renders or changes for an instance that never runs
`deploy_skaegis-<env>.yml`.

## What it does, and what it does not

- It inventories the packages on every enrolled host and matches them against **known, published CVEs** (the manager's vulnerability detector, with its feed refreshed on `FEED_UPDATE_INTERVAL`). It shows the findings in the dashboard.
- It does **not** patch anything. It does not touch an enrolled host except to run the agent that reports inventory. Fixing is the Remediate half of the suite ([skpatch-hosts](../skpatch-hosts/README.md) for Linux package updates, the skpatch program's image pipeline for container images).
- The wider remediation loop drafts a fix as a pull request for a human to review. **Nothing is auto-merged.**
- It does not detect unknown vulnerabilities (zero-days), and a host with no agent is invisible to it.
- It runs unmodified upstream software. This role pins, configures and wires it; no Wazuh code is vendored here.

## What is deployed

| Service | Image (Wazuh 4.14.10, pinned by manifest-list digest) | Role | Reachable from |
|---|---|---|---|
| `indexer` | `wazuh/wazuh-indexer` | OpenSearch: stores inventory, alerts and CVE findings; validates OIDC tokens | `skaegis-<env>` network only |
| `manager` | `wazuh/wazuh-manager` | Agent enrolment (1515), agent events (1514), vulnerability detector, API (55000, internal) | agents on 1514/1515 (published); services on `skaegis-<env>` |
| `dashboard` | `wazuh/wazuh-dashboard` | The only service on the edge: Traefik router, OpenID login | `skaegis-<env>` and `cloud-public-<env>` |
| `backup` | the manager image, entrypoint `skaegis-backup` | Daily manager tarball plus indexer snapshots | `skaegis-<env>` network only |

Internal TLS between the services comes from the upstream `wazuh-certs-generator`, run once on the first deploy (`tasks/certs.yml`). Traefik terminates the public TLS and talks plain HTTP to the dashboard on 5601 over the overlay network.

## Quickstart

```bash
# 0. sksso first (OIDC mode, the default): set sksso.SKAEGIS_OIDC_CLIENT_SECRET,
#    deploy sksso, so the skaegis provider and the skadmin gate exist.

# 1. Label the node that will hold the stack.
docker node update --label-add skaegis=true <node>

# 2. Deploy the stack.
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skaegis/deploy_skaegis-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass

# 3. Enrol Linux hosts (Debian/Ubuntu), limited to the hosts you choose.
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skaegis/provision_skaegis_agents.yml \
  -l <host-or-group> -e skaegis_agents_provision_env=prod \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Then open `https://skaegis.<cluster>.<domain>/` and sign in with a member of the `skadmin` group. The full procedure, diagrams and troubleshooting are in **[SOP.md](./SOP.md)**.

## Start here

- [`deploy_skaegis-prod.yml`](./deploy_skaegis-prod.yml) (and the `-staging` and `-dev` twins): every default, every fail-closed assert and the restart logic live here.
- [`provision_skaegis_agents.yml`](./provision_skaegis_agents.yml): the Linux agent playbook (install, enrol, hold, remove).
- [`tasks/certs.yml`](./tasks/certs.yml): the one-time internal certificate issue.
- [`src/config/skaegis/`](./src/config/skaegis/): the compose template, the per-service env files and the indexer, dashboard and manager configuration.
- [`src/skaegis/`](./src/skaegis/): the deploy script, the indexer entrypoint (applies the security config at every start), the backup script and the Windows agent script.

## Vault contract

The deploy reads one block, `skaegis:`, from the instance vault. The file name is the framework's, resolved by `v1/ansible/shared/tasks/select_vault_file.yml`: `optional/group_vars/<env>/skaegis-<env>-<domain-with-dashes>-<cluster>_vault.yml` (for example `skaegis-prod-example-test-render_vault.yml`), falling back to the legacy `skaegis-<env>_vault.yml`. With `skstacks_vault_dir` set, the same layout is read from that directory.

**Required secrets.** The deploy fails before it changes anything if one is missing or malformed (it checks length and shape, and never prints a value).

| Key | Generate | Rule |
|---|---|---|
| `INDEXER_PASSWORD` | `openssl rand -base64 24 \| tr -d '/+=\n'` | 16+ characters; no whitespace, quote, backslash, dollar or backtick. Password of the indexer `admin` user (the manager's filebeat login and the break-glass login). |
| `DASHBOARD_PASSWORD` | `openssl rand -base64 24 \| tr -d '/+=\n'` | Same rule. Password of the internal `kibanaserver` account the dashboard server uses. |
| `API_PASSWORD` | `echo "Aa1.$(openssl rand -hex 16)"` | Wazuh API policy: 12 to 64 characters with upper, lower, digit and symbol, plus the same forbidden characters. If it fails the policy the manager refuses to create the `wazuh-wui` API user and stops. |
| `ENROLMENT_PASSWORD` | `openssl rand -base64 24 \| tr -d '/+=\n'` | 16+ characters. Written to the manager's `authd.pass`; every agent must present it. Also used by the agent playbook and the Windows script. |
| `CLUSTER_KEY` | `openssl rand -hex 16` | Exactly 32 letters or digits. Rendered into the manager's `ossec.conf` (single node, clustering is disabled, but the field is required). |
| `OIDC_ISSUER` | the sksso issuer URL | `AUTH_MODE: oidc` only. `https://<sso host>/application/o/skaegis/` (the trailing slash matters; discovery is `<issuer>/.well-known/openid-configuration`). |
| `OIDC_CLIENT_SECRET` | `openssl rand -hex 32` | `AUTH_MODE: oidc` only. 32+ URL-safe characters, **the same value as `sksso.SKAEGIS_OIDC_CLIENT_SECRET`**. |

Every other key is optional. Defaults are the playbook's own.

| Key | Default | Meaning |
|---|---|---|
| `AUTH_MODE` | `oidc` | `oidc` (OpenID against sksso, basic login kept for break-glass) or `local` (internal users only; refuses `OIDC_ISSUER`, `OIDC_CLIENT_SECRET` and `SSO_HOST_IP`). |
| `ACCESS_MODE` | `admin-only` | The only accepted value. The gate and the in-app admin role are the group `skadmin` (APPLICATION_ACCESS_SSO_STANDARD). Other modes are refused until the Wazuh API RBAC mapping for non-admins exists. |
| `CLUSTERNAME`, `DOMAIN` | inventory `cluster_name`, `domain` | Base domain: `<cluster>.<domain>`. |
| `CLOUDFLARED` | `false` | When true the base domain is `DOMAIN` alone (no cluster label). |
| `HOSTNAME` | `skaegis[-<env>].<base domain>` | Public host name of the dashboard. |
| `INDEXER_IMAGE`, `MANAGER_IMAGE`, `DASHBOARD_IMAGE` | Wazuh 4.14.10 digests | Override only with another `@sha256:` pinned reference. A floating tag is refused. |
| `CERTS_GENERATOR_IMAGE` | `wazuh/wazuh-certs-generator:0.0.5` by digest | The certificate tool. Must be digest-pinned. |
| `CERT_TOOL_VERSION` | `4.14` | Passed to the generator as `CERT_TOOL_VERSION`. |
| `DATA_NODE` | empty | Hostname of the node holding local data. Required when any data path is outside `/var/data`. Also pins the indexer, manager and backup to that node and sets `vm.max_map_count` there. |
| `DATA_PATH` | `/var/data/skaegis-<env>/data` | Root for `manager/` and `certs/` (and the default parent of the indexer data). |
| `INDEXER_DATA_PATH` | `<DATA_PATH>/indexer` | The indexer's data. Local disk, never NFS (see Placement). |
| `BACKUP_PATH` | `/var/data/skaegis-<env>/backup` | Manager tarballs, plus `snapshots/` for the indexer snapshot repository. |
| `PLACEMENT_CONSTRAINTS` | `["node.role == worker", "node.labels.skaegis == true"]` | Swarm placement for indexer, manager and backup (plus the `DATA_NODE` hostname when set). Non-empty list. |
| `DASHBOARD_PLACEMENT_CONSTRAINTS` | `PLACEMENT_CONSTRAINTS` | Placement for the dashboard only. |
| `INDEXER_HEAP` | `2g` | JVM heap, `-Xms` and `-Xmx`, like `2g` or `1536m`. Keep it at or below half the memory limit. |
| `INDEXER_CPUS`, `INDEXER_MEMORY_LIMIT`, `INDEXER_MEMORY_RESERVATION` | `2.00`, `4G`, `3G` | Indexer resources. |
| `MANAGER_CPUS`, `MANAGER_MEMORY_LIMIT`, `MANAGER_MEMORY_RESERVATION` | `2.00`, `2G`, `1G` | Manager resources. |
| `DASHBOARD_CPUS`, `DASHBOARD_MEMORY_LIMIT`, `DASHBOARD_MEMORY_RESERVATION` | `1.00`, `1G`, `512M` | Dashboard resources. |
| `INDEXER_START_PERIOD`, `MANAGER_START_PERIOD`, `DASHBOARD_START_PERIOD` | `180s`, `300s`, `180s` | Health-check start periods. |
| `HEALTH_CHECK_INTERVAL`, `HEALTH_CHECK_TIMEOUT`, `HEALTH_CHECK_RETRIES` | `30s`, `10s`, `5` | Health-check timing for all three services. |
| `FEED_UPDATE_INTERVAL` | `60m` | How often the manager refreshes the vulnerability feed. |
| `APPLY_SECURITY_CONFIG` | `true` | The indexer entrypoint runs `securityadmin.sh` at every start so the repo's config is the truth. Set false only to stage changes by hand. |
| `SESSION_TTL_MS` | `3600000` | Dashboard session and cookie lifetime (one hour). |
| `AGENT_PUBLISH` | `true` | Publish the manager's 1514 and 1515. False for a stack with no remote agents. |
| `AGENT_PUBLISH_MODE` | `ingress` | `ingress` or `host`. |
| `AGENT_PORT`, `ENROLMENT_PORT` | `1514`, `1515` | Published ports (the container side is fixed at 1514 and 1515). |
| `AGENT_MANAGER_ADDRESS` | none | Address of a swarm node the agents reach on the two ports. Required by the agent playbook, never a public edge. Not read by the deploy. |
| `AGENT_VERSION` | `4.14.10-1` | Pinned Linux agent package version (`4.x.y-n`). Keep it equal to the manager's version. |
| `AGENT_GROUP` | `default` | Default Wazuh agent group for enrolment (a per-host `skaegis_agent_group` wins). |
| `OIDC_CLIENT_ID` | `skaegis` (`skaegis-<env>` outside prod) | Must equal sksso's provider client id. |
| `OIDC_SCOPE` | `openid profile email groups` | Requested scopes. |
| `OIDC_GROUPS_CLAIM` | `groups` | Claim carrying the group list (the indexer's `roles_key`). |
| `OIDC_SUBJECT_CLAIM` | `preferred_username` | Claim used as the user name. |
| `OIDC_LOGOUT_URL` | `<issuer>/end-session/` | Where the dashboard sends the browser on sign-out. |
| `SSO_HOST_IP` | empty | Maps the issuer host to this IP inside the indexer and dashboard (`extra_hosts`), for a lab where the issuer name does not resolve from the swarm. |
| `EXTRA_CA_CERT` | empty | PEM of a private CA that signs the issuer's certificate; mounted into the indexer and dashboard as a trusted CA. |
| `TRAEFIK_MIDDLEWARES` | `default@file` | Router middleware chain (includes the CrowdSec bouncer). |
| `SSO_MIDDLEWARE` | empty | Optional forward-auth layer on top of the dashboard's own login. |
| `TLS_OPTIONS` | `intermediate@file` | Traefik TLS options, or `none`. |
| `BACKUP_ENABLED` | `true` | Deploy the backup sidecar. |
| `BACKUP_FREQUENCY`, `BACKUP_KEEP_DAYS`, `BACKUP_START_DELAY` | `86400`, `7`, `300` | Seconds between runs, retention in days, delay before the first run. |
| `BACKUP_SNAPSHOTS` | `true` | Also take an indexer snapshot each run (the manager tarball is always taken). |
| `AUTHENTIK_TILE` | `true` | Create or update the portal tile (the same application sksso's blueprint creates). |
| `AUTHENTIK_TILE_SLUG`, `AUTHENTIK_TILE_NAME` | `OIDC_CLIENT_ID`, `skaegis` | Tile slug (must match the OAuth2 application) and name. |
| `AUTHENTIK_TILE_GROUP` | `skadmin` | The gate group. Never "authentik Admins". |
| `AUTHENTIK_TILE_ICON`, `AUTHENTIK_TILE_DESCRIPTION`, `AUTHENTIK_TILE_CATEGORY`, `AUTHENTIK_TILE_OPEN_IN_NEW_TAB` | `fa://fa-shield-halved`, a one-line text, `Security`, `true` | Tile presentation. |

Minimal vault:

```yaml
skaegis:
  CLUSTERNAME: "<cluster>"
  DOMAIN: "example.test"
  INDEXER_PASSWORD: "<openssl rand -base64 24 | tr -d '/+=\n'>"
  DASHBOARD_PASSWORD: "<openssl rand -base64 24 | tr -d '/+=\n'>"
  API_PASSWORD: "Aa1.<openssl rand -hex 16>"
  ENROLMENT_PASSWORD: "<openssl rand -base64 24 | tr -d '/+=\n'>"
  CLUSTER_KEY: "<openssl rand -hex 16>"
  OIDC_ISSUER: "https://sso.example.test/application/o/skaegis/"
  OIDC_CLIENT_SECRET: "<same as sksso.SKAEGIS_OIDC_CLIENT_SECRET>"
  DATA_NODE: "<node>"
  AGENT_MANAGER_ADDRESS: "192.0.2.10"
```

## Placement

The indexer, manager and backup are stateful and share one placement rule; the dashboard follows it unless `DASHBOARD_PLACEMENT_CONSTRAINTS` says otherwise.

```bash
docker node update --label-add skaegis=true <node>
```

The default constraints are `node.role == worker` and `node.labels.skaegis == true`, so the labelled node must be a **worker**. Override `PLACEMENT_CONSTRAINTS` to use a manager.

- **Local disk for the indexer.** The default paths sit under `/var/data`, which on a typical swarm is shared storage. **Never put the indexer's data on NFS**: OpenSearch needs real file locking and low latency. Put `INDEXER_DATA_PATH` (and, to keep the manager state and certificates beside it, `DATA_PATH`) on local disk of one node and set `DATA_NODE` to that node. Any path outside `/var/data` without a `DATA_NODE` fails the deploy: without the pin swarm could reschedule the stack onto an empty directory (every alert and CVE state gone, `client.keys` lost, so every agent must re-enrol).
- **`vm.max_map_count` 262144** on the indexer node. With `DATA_NODE` set the play writes `/etc/sysctl.d/90-skaegis.conf` there. With label-only placement it cannot know the node: set it yourself (`sysctl -w vm.max_map_count=262144`, and persist it in `/etc/sysctl.d/`).
- Data directories are owned by uid 1000 (indexer data, certificates, snapshots) or root (manager state, backup); the play sets this explicitly and never recursively.

**Sizing** for a comfortable production instance: about 9 to 11 GB RAM reserved and 4 to 5 vCPU, and 150 GB of local disk. The defaults (indexer 4G limit with 3G reserved and a 2g heap, manager 2G, dashboard 1G, backup 128M) fit a small fleet; raise `INDEXER_MEMORY_LIMIT`, `INDEXER_HEAP` (at most half the limit) and `INDEXER_CPUS` as the number of agents and retained alerts grow. A single-node indexer has no replica, so the snapshots from the backup sidecar are the only copy of the alert history other than the disk.

## Sign-in, SSO and break-glass

skaegis is **admin-only**: it shows every enrolled host's weaknesses.

- **Order.** Run `sksso` first with `SKAEGIS_OIDC_CLIENT_SECRET` set. Its `skaegis` blueprint creates the OAuth2 provider (redirect URI `https://<skaegis host>/auth/openid/login`), the application, and the policy binding of the application to the group `skadmin`. Then deploy skaegis with the same secret and `OIDC_ISSUER`. See the sksso README, section "skaegis (OIDC) login". The other order converges on the next sksso deploy.
- **Gate and role.** The group `skadmin` gates the application in Authentik, and inside the dashboard the indexer maps the `groups` claim to the backend role `skadmin`, which the roles mapping binds to `all_access` and the Wazuh admin role. A login that maps to no role gets nothing. "authentik Admins" is never mapped.
- **Break-glass.** The indexer's internal `admin` user (password `INDEXER_PASSWORD`) keeps working through the dashboard's basic login, which stays enabled next to OpenID. It is also the account the manager's filebeat uses. Store that password **off-cluster in the estate secret store**, not only in the vault the SSO outage would also block you from reading.
- **`AUTH_MODE: local`** disables OpenID: internal users only. The vault must then not set `OIDC_ISSUER`, `OIDC_CLIENT_SECRET` or `SSO_HOST_IP`.
- A private CA or an issuer name that does not resolve from the swarm: `EXTRA_CA_CERT` and `SSO_HOST_IP` (see SOP troubleshooting).

## Enrol hosts

**Linux (Debian and Ubuntu).** `provision_skaegis_agents.yml` adds Wazuh's apt repository (signing key checked by fingerprint), installs `wazuh-agent` at `AGENT_VERSION` pointed at `AGENT_MANAGER_ADDRESS` (ports `AGENT_PORT` and `ENROLMENT_PORT`), writes the enrolment password (root and the `wazuh` group only), enrols, and `apt-mark hold`s the package so [skpatch-hosts](../skpatch-hosts/README.md) never moves the agent: a version bump is a deliberate change here.

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skaegis/provision_skaegis_agents.yml \
  -l <host-or-group> -e skaegis_agents_provision_env=prod \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

- **Choose hosts with `-l`.** The playbook defaults to every host in the inventory.
- **Opt a host out** with `skaegis_agent_enabled: false` in its host or group vars (it is then skipped, state present or absent).
- **Group** the host with `skaegis_agent_group: <name>` (default: `AGENT_GROUP`).
- **Remove** with `-e skaegis_agents_provision_state=absent` (then delete the record on the manager, SOP "Remove an agent").
- `AGENT_MANAGER_ADDRESS` is the address of a swarm node the hosts reach on 1514 and 1515, for example `192.0.2.10`. Never point agents at a public edge.

**Windows.** The role ships `src/skaegis/windows/install-wazuh-agent.ps1`, delivered and run through the skrac (MeshCentral) agent the host already has, so no new inbound path or credential is needed. The enrolment password is uploaded as a one-line file and never appears on a command line. See SOP "Windows agents" for the exact `meshctrl` commands.

## Related projects / See also

- ⬆️ **Depends on:** [sksso](../sksso/README.md), Authentik: the OpenID provider and the `skadmin` gate (section "skaegis (OIDC) login"); [skfenceha](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skfenceha) (or skfence), the Traefik edge in front of the dashboard.
- ↔️ **Sibling:** [skrac](../skrac/README.md), the remote-access rail that delivers the Windows agent script (and the closest design sibling: self-hosted upstream app, own OIDC login, backup sidecar, `DATA_NODE`).
- ➡️ **Remediate:** [skpatch-hosts](../skpatch-hosts/README.md), Linux package updates, and the [skpatch program](../../../../docs/skpatch-program.md) for the image side; the findings skaegis reports feed that loop.
- ↔️ **Sibling:** [sksec](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#sksec), the Defend step of the suite.
- 🛡️ **Product:** [smilinTux/skaegis](https://github.com/smilinTux/skaegis), the suite framing and the honest claims.
- 📐 **Standards:** [sk-standards](https://github.com/smilinTux/sk-standards), the doc/SOP and application-access (SSO) standards this README and SOP follow.

*License: this role is Apache-2.0 (the framework's own license). The upstream Wazuh software it deploys is GPL-2.0 and is referenced by image reference only: nothing from it is vendored.*

## API router (raw status codes for machine traffic)

The router chain usually ends in `error-pages@file`, which replaces any backend 4xx/5xx with an HTML page and
replays the original method there, so API clients never see the app's own status or JSON error. `skaegis[-env]-api` carries the
machine paths (`/api/` (the SSO middleware stays on it)) through the same chain with each error-pages-bearing chain swapped for its
no-error-pages form; anything else in the chain (CrowdSec, SSO, custom middlewares) is kept. Vault key
`API_MIDDLEWARES` replaces the derived chain whole. `default-no-error-pages@file` has neither `default@file`'s
optional IP allowlist nor CrowdSec: set `API_MIDDLEWARES` if the main chain relies on them. See "API routers" in
`docs/v1-service-catalog.md`.
