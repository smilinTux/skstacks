# skpatch-hosts

**Deploys [linux-update-dashboard](https://github.com/TheDuffman85/linux-update-dashboard) (AGPL-3.0) on the swarm: a self-hosted dashboard that checks, and if armed applies, Linux package updates across a fleet of hosts over SSH.** This is the host-patching half of the wider [skpatch program](../../../../docs/skpatch-program.md); the image-patching half (SBOM scanning, CVE tracking, Renovate) is a separate, mostly-planned pipeline described there. **Maturity tier:** T0, Classical (see "Honest claims"). **Version phase:** framework `v1` tree (this repo's own [Versions table](../../../../README.md#versions): "Published, one service at a time", not the generic ecosystem `VERSION_LIFECYCLE` "Legacy" phase).

Opt-in: nothing renders or changes for an instance that never runs
`deploy_skpatch-hosts-<env>.yml`.

## Quickstart

```bash
# 1. Deploy the dashboard (report-only by default: ALLOW_ACTIONS: false)
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpatch-hosts/deploy_skpatch-hosts-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass

# 2. Give it something to patch: provision one estate's nodes (separate playbook)
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpatch-hosts/provision_skpatch_hosts_nodes.yml \
  -l <estate-inventory-group> -e skpatch_hosts_provision_env=prod \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

Full detail (build, test, deploy, configuration, API, troubleshooting)
is in **[SOP.md](./SOP.md)**.

## Honest claims (read this)

- This role **generates and stores classical key material**: one
  dedicated SSH **ed25519** keypair per estate (`ssh-keygen`, standard
  OpenSSH, no custom crypto) and an at-rest **AES-256-GCM** encrypted
  credential store inside the upstream app, keyed by
  `LUDASH_ENCRYPTION_KEY`. Both are standard, off-the-shelf primitives;
  this role implements neither a cipher nor a key-exchange protocol of
  its own.
- It makes **no post-quantum or hybrid-KEM claim anywhere**, and should
  not: everything here is classical (SSH ed25519, TLS for the dashboard's
  own HTTPS). It is not a "crypto component" under
  [`SK_REPO_DOC_STANDARD`](https://github.com/smilinTux/sk-standards/blob/main/standards/SK_REPO_DOC_STANDARD.md)
  in the sense that matters there (it does not implement, negotiate, or
  expose a cryptographic protocol to other systems); it operates one.
  No separate `CRYPTO_SPEC.md` is carried for that reason; this paragraph
  is the compliance statement.
- `ALLOW_ACTIONS` in the vault is **metadata only**: flipping it grants
  nothing by itself. The real enforcement is a sudoers allowlist written
  by `provision_skpatch_hosts_nodes.yml` on each target node, gated by that
  node's **own** `skpatch_hosts_allow_actions` (see SOP §6).
- **One SSH keypair per estate, never shared, never a reused admin key.**
  A compromise of this container is a compromise of every host it holds
  a key for; see SOP §1 "Why this app gets more scrutiny than most".

## How this fits the program

`skpatch-hosts` the role does hosts; `skpatch` the program also covers
container images, via `skbom` (Dependency-Track plus Trivy SBOM scanning)
and Renovate, both run as `skflow` (Windmill) flows and gated through the
same release path as everything else. See
**[`docs/skpatch-program.md`](../../../../docs/skpatch-program.md)** for
the cadence/policy table, the architecture diagrams, and what today's
state actually is (most of the image half is still planned; only the
host-patching role below is live).

## Start here

- **What this is, in two sentences.** A dashboard that holds one SSH
  credential per estate and, only if explicitly armed per node, can run
  package upgrades and reboots across that estate's hosts. By default it
  only reports what is out of date.
- **Key entry points:**
  - [`deploy_skpatch-hosts-<env>.yml`](./) deploys the dashboard stack.
  - [`provision_skpatch_hosts_nodes.yml`](./provision_skpatch_hosts_nodes.yml) is
    idempotent and reversible: it creates the low-privilege account,
    installs the estate's keypair, and writes the sudoers allowlist on
    each target.
  - [`src/skpatch-hosts/skpatch-hosts.sudoers.j2`](./src/skpatch-hosts/skpatch-hosts.sudoers.j2)
    is the exact-command allowlist; this is where "report-only" is
    actually enforced, not in the app.
  - [`SOP.md`](./SOP.md) has the full operating procedure (build, test,
    deploy, configuration reference, troubleshooting).
- **The data it owns.** `dashboard.db` (SQLite, under `/data`): every SSH
  credential this instance holds, AES-256-GCM-encrypted at rest.
- **External dependencies & integration points.** Traefik (skfence or
  skfenceha) for ingress; Authentik (sksso) for the default SSO-gated
  sign-in; SSH to whatever nodes `provision_skpatch_hosts_nodes.yml` has been
  run against.
- **Maturity tier.** T0, Classical. See "Honest claims" above.

## Configuration knobs (vault)

The full reference (every key, every default) is SOP §6. The ones that
matter most when reading this role for the first time:

| Key | Default | What it controls |
|---|---|---|
| `LUDASH_ENCRYPTION_KEY` | required, no default | AES-256-GCM key for the credential store. Losing it makes every stored credential unreadable. |
| `AUTH_MODE` | `sso` | `sso` puts Authentik forward auth in front of the whole router (default); `local` uses the app's own login only; `oidc` is the dashboard's own OIDC login against sksso, no forward auth. See "Sign-in and the admin group". |
| `ALLOW_ACTIONS` | `false` | Instance-wide **metadata**. A node only gets upgrade/reboot sudoers lines when its own `skpatch_hosts_allow_actions` (or, if unset, this) is `true`. |
| `SSH_PUBLIC_KEY` | required by provisioning | Public half of this estate's dedicated keypair. |
| `PROVISION_USER` | `skpatch-ro` | The low-privilege account `provision_skpatch_hosts_nodes.yml` creates on each target node. |
| `DATA_NODE` | `""` | Required if `DATA_PATH` is local disk, not shared storage; pins the single dashboard replica there. |
| `CLUSTERNAME` | inventory `cluster_name` | First label of the dashboard host (`skpatch-hosts.<CLUSTERNAME>.<DOMAIN>`) and the stack name suffix. |
| `DOMAIN` | inventory `domain` | Zone the dashboard host lives under; with `CLOUDFLARED: true` the host is `skpatch-hosts.<DOMAIN>` instead. |

Full vault contract (every key the deploy and the provisioning playbook
read; SOP §6 carries the same block):

```yaml
skpatch-hosts:
  LUDASH_ENCRYPTION_KEY: "<openssl rand -base64 32>"  # required, secret, >= 32 chars
  CLUSTERNAME: cluster1                     # default: inventory cluster_name
  DOMAIN: example.com                       # default: inventory domain
  CLOUDFLARED: false                        # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                              # public host; default skpatch-hosts[-dev|-staging].<base domain>
  TRAEFIK_MIDDLEWARES: ""                   # default default-no-error-pages@file
  AUTH_MODE: sso                            # sso (Authentik forward auth, whole router) | local | oidc
  SSO_MIDDLEWARE: ""                        # sso mode: default authentik@file; must be empty in local/oidc mode
  OIDC_ISSUER: ""                           # oidc mode: required, e.g. https://<sso host>/application/o/<client id>/
  OIDC_CLIENT_ID: ""                        # oidc mode: default the router name (e.g. skpatch-hosts)
  OIDC_CLIENT_SECRET: ""                    # oidc mode: required, >= 32 chars, same value as sksso's SKPATCH_HOSTS_OIDC_CLIENT_SECRET
  OIDC_SCOPE: "openid profile email groups"
  OIDC_GROUPS_CLAIM: groups
  OIDC_ADMIN_GROUPS: "skadmin"     # CSV of Authentik group names
  OIDC_READ_ONLY_GROUPS: ""                 # CSV, default none
  OIDC_UNMATCHED_ROLE: deny                 # deny | admin | read-only
  OIDC_END_SESSION: true
  TLS_OPTIONS: ""                           # default intermediate@file; "none" = no tls.options label
  TZ: UTC
  LUDASH_LOG_LEVEL: info
  extra_env: {}                             # any other upstream app variable, verbatim into skpatch-hosts.env
  IMAGE: ""                                 # default: the pinned linux-update-dashboard digest in defaults/main.yml; must carry @sha256:
  DATA_PATH: ""                             # default /var/data/skpatch-hosts-<env>/data
  DATA_NODE: ""                             # pin dashboard to this node (required with a local path)
  PLACEMENT_CONSTRAINTS: []                 # default ["node.role == worker"]
  HEALTH_CHECK_INTERVAL: 30s
  HEALTH_CHECK_TIMEOUT: 10s
  HEALTH_CHECK_RETRIES: 3
  HEALTH_CHECK_START_PERIOD: 60s            # default 30s, or 90s with AUTH_MODE oidc (see "OIDC healthcheck")
  DASHBOARD_CPUS: "0.50"
  DASHBOARD_MEMORY_LIMIT: 384M
  DASHBOARD_MEMORY_RESERVATION: 64M
  AUTHENTIK_TILE: true                      # false opts out of the portal tile
  AUTHENTIK_TILE_NAME: SKPatch Hosts        # tile title; slug/description/icon/group/open-in-new-tab below
  AUTHENTIK_TILE_SLUG: skpatch-hosts
  AUTHENTIK_TILE_DESCRIPTION: "Linux update dashboard"
  AUTHENTIK_TILE_ICON: "fa://fa-wrench"
  AUTHENTIK_TILE_GROUP: "skadmin"  # must already exist
  AUTHENTIK_TILE_OPEN_IN_NEW_TAB: true
  # Read by provision_skpatch_hosts_nodes.yml, not the swarm deploy:
  ALLOW_ACTIONS: false                      # instance-wide default; a host's own skpatch_hosts_allow_actions: false always wins
  PROVISION_USER: skpatch-ro                # the low-privilege account's name
  PROVISION_UID: 5201                       # its fixed uid and gid; never a system uid (containers share the host's uid space)
  SSH_PUBLIC_KEY: ""                        # required by provision_skpatch_hosts_nodes.yml; one keypair per estate
  # Host auto-registration (see "Automatic host registration" below); REGISTER_AS plus a credential, else skipped
  REGISTER_AS: ""                           # an existing dashboard admin's username (local or OIDC)
  SSH_PRIVATE_KEY: ""                       # private half of SSH_PUBLIC_KEY; the registrar creates the credential from it
  CREDENTIAL_ID: 0                          # or an existing sshKey credential's id (takes precedence)
  # Dashboard configuration as code (README "Schedules and notifications as code"); all optional
  SYNC_CREDENTIAL: false                    # true: rewrite the credential's key from SSH_PRIVATE_KEY (key rotation)
  UPGRADE_MODE: ""                          # upgrade | full-upgrade: APT default for registered systems; "" leaves it
  NOTIFICATIONS: []                         # [{name, type, enabled, notify_on, systems, config}], secrets allowed
  SCHEDULES: []                             # [{name, type, cron, enabled, systems, only_if_required, cache_hours, notifications}]
```

**Per-host actions ceiling:** set `skpatch_hosts_allow_actions: false` in the
group_vars of every hypervisor, NAS, or any host that must never be
upgraded or rebooted through this app. That per-host `false` always
wins over the vault's instance-wide `ALLOW_ACTIONS: true`.

## Sign-in and the admin group

`AUTH_MODE`:

- **`sso` (default): Authentik forward auth on the whole router.**
  `SSO_MIDDLEWARE` (default `authentik@file`). Authentik needs a proxy
  provider in forward-auth mode covering the `skpatch-hosts` host, bound to the
  embedded outpost, with its application restricted to the admin group
  (this app holds the keys to every node it reaches). Users still sign in
  to the dashboard's own account system afterward (a separate first-run
  local account, unless `oidc` below is used instead).
- **`local`: the app's own login only** (password/TOTP/passkey), no
  `SSO_MIDDLEWARE` allowed.
- **`oidc`: the dashboard's own OIDC login against sksso (Authentik), no
  forward auth.** One sign-in, no separate first-run account, Authentik
  group membership decides the role. Verified against the pinned
  `linux-update-dashboard` source (`server/auth/auth-config.ts`,
  `server/auth/oidc.ts`, `server/routes/auth.ts`):
  - Required: `OIDC_ISSUER` (the Authentik application's issuer URL, e.g.
    `https://<sso host>/application/o/<client id>/`; `issuer_mode:
    per_provider` on sksso's blueprint, see below) and `OIDC_CLIENT_SECRET`
    (32+ chars, **the same value** as sksso's `SKPATCH_HOSTS_OIDC_CLIENT_SECRET`).
  - `OIDC_CLIENT_ID` defaults to the router name (e.g. `skpatch-hosts`), the same
    slug `AUTHENTIK_TILE_SLUG` already defaults to above: sksso's blueprint
    (below) and this role's own portal tile then manage the SAME Authentik
    Application (see `shared/src/authentik_portal_tile.py` "keeping its
    existing provider"), not two.
  - `OIDC_SCOPE` (default `openid profile email groups`), `OIDC_GROUPS_CLAIM`
    (default `groups`), `OIDC_ADMIN_GROUPS` (default `skadmin`,
    CSV of Authentik group names), `OIDC_READ_ONLY_GROUPS` (CSV, default
    none), `OIDC_UNMATCHED_ROLE` (`deny` default, or `admin`/`read-only`
    for a group that matched neither list).
  - `OIDC_END_SESSION` (default `true`): also end the Authentik session on
    sign-out.
  - This role also sets `LUDASH_PASSWORD_LOGIN_ENABLED=false`, closing the
    separate first-run local-account path this mode exists to remove.
  - **Verified (`auth-config.ts`): every `LUDASH_OIDC_*` /
    `LUDASH_PASSWORD_LOGIN_ENABLED` value here is *pinned*.** It always
    wins over anything saved in the dashboard's own Settings page and is
    never written back to its database, so a vault change here cannot
    drift the way it can on sksec-web's equivalent (see
    `../sksec-web/README.md` "Sign-in and SSO" for that caveat); removing
    the env var restores whatever was last saved there, if anything.

Guard (the deploy refuses, naming the keys): an `AUTH_MODE` other than
`sso`/`local`/`oidc`; `SSO_MIDDLEWARE` set with `AUTH_MODE: local` or
`oidc`; a forward-auth middleware (`authentik`, or the configured
`SSO_MIDDLEWARE` name) inside `TRAEFIK_MIDDLEWARES` directly (SSO under
another name; use `AUTH_MODE: sso` / `SSO_MIDDLEWARE` instead);
`AUTH_MODE: oidc` without `OIDC_ISSUER` or a 32+ char `OIDC_CLIENT_SECRET`,
or an `OIDC_UNMATCHED_ROLE` other than `deny`/`admin`/`read-only`.

The Authentik side (the OAuth2 provider + application, as code) is
rendered by **sksso**, not this role: see `../sksso/README.md`
"sksec-web / skpatch-hosts (OIDC) login".

### OIDC healthcheck

With `AUTH_MODE: oidc` the app runs OIDC discovery once, at startup, and
never retries. If sksso is unreachable at that moment the task stays up with
SSO login silently off, and only a forced service update brings it back. So
in oidc mode the swarm healthcheck reads `/api/auth/status` (with `node`,
the image has no curl or wget) and fails unless it reports
`"oidcEnabled":true`. Swarm then replaces the task and discovery runs again;
while sksso stays down the task is replaced every few minutes, which is the
intended signal. The start period defaults to 90s in this mode so a normal
start, discovery included, never counts as a failure. Other modes keep the
plain `/` probe.

## Automatic host registration

`provision_skpatch_hosts_nodes.yml`, on every `present` run, adds one
dashboard system per node in its own target group that the dashboard does
not have yet, so the dashboard is never empty after a deploy. Opt-in:
nothing touches the dashboard unless `REGISTER_AS` and an SSH credential
(`SSH_PRIVATE_KEY` or `CREDENTIAL_ID`) are set; unset, the playbook says it
is skipping registration and nodes are added by hand, as before.

```yaml
skpatch_hosts:
  REGISTER_AS: ""         # username of an existing dashboard ADMIN (local or OIDC); unset skips registration
  SSH_PRIVATE_KEY: ""     # private half of SSH_PUBLIC_KEY; creates the credential "skpatch-hosts <PROVISION_USER>"
  CREDENTIAL_ID: 0        # or: an sshKey credential already in the dashboard (takes precedence)
```

**How it signs in.** The pinned dashboard (2026.10.9) refuses every
API-token request that creates a system or a credential (403 "API tokens
cannot configure SSH connections"), password login is off in `AUTH_MODE:
oidc`, and Authentik forward auth fronts it in `sso` mode. So the
registrar (`src/skpatch-hosts/register_systems.js`) runs **inside the
dashboard's own container**, on whichever node swarm placed it, and talks to
the app on `127.0.0.1` with a five-minute `ludash_session` it signs with the
app's own session key (`LUDASH_SECRET_KEY`, else the `.secret_key` file next
to the database) for the `REGISTER_AS` account. It carries that user's
current `session_version`, so "sign out everywhere" in the dashboard still
revokes it, and it is refused for a read-only or unknown account. This
grants nothing new: whoever can `docker exec` into that container can
already read the key, the database and the encryption key. Every system it
creates shows up in the dashboard as added by that account.

**One-time setup per estate.** Sign in to the dashboard once (first-run
local admin, or OIDC through sksso) so the account exists, then put its
username in `REGISTER_AS` and the estate key's private half in
`SSH_PRIVATE_KEY`. The next `present` run creates the credential and the
systems. Rotating the key: replace the credential in the dashboard (or
delete it and let the next run recreate it); the registrar never rewrites
an existing credential.

**Creates, never edits, never deletes.** The match key is hostname and
port (the app's own uniqueness rule is hostname, port and username, and
every system here shares the one credential). A node already registered is
left exactly as an operator configured it, because `PUT /api/systems/:id`
replaces the whole connection config. Hosts removed from the target
inventory are reported, never deleted.

**Host-key verification is off** for every system this creates
(`hostKeyVerificationEnabled: false`): these are the operator's own
already-provisioned fleet nodes, reached with the keypair
`provision_skpatch_hosts_nodes.yml` just installed. Turn it on by hand per
system if an estate wants it.

**Migrating from `API_TOKEN`.** Token registration never worked against
this dashboard version. A vault that still sets `API_TOKEN` makes the
playbook fail with what to set instead; remove it and set `REGISTER_AS`.
`API_CREDENTIAL_ID` is still read, as `CREDENTIAL_ID`.

## Schedules and notifications as code

The same registrar run (README "Automatic host registration", so
`REGISTER_AS` and a credential must be set) also converges the dashboard's
own configuration, so a redeploy or a fresh `dashboard.db` reproduces it:

| Input | Where | What it does |
|---|---|---|
| `SCHEDULES` | vault, or `skpatch_hosts_schedules` (e.g. `-e @<file>.yml`, which wins) | Refresh / update / autoremove / reboot / notification_digest schedules. `systems` is a list of dashboard system **names**, or absent for all. |
| `NOTIFICATIONS` | vault (channel configs carry SMTP passwords, tokens) | Notification channels; a digest channel is one a `notification_digest` schedule lists in `notifications`. |
| `UPGRADE_MODE` / `skpatch_hosts_upgrade_mode` | vault / host var | APT default upgrade mode of each registered system. |
| `SYNC_CREDENTIAL` | vault | Rewrites the estate credential's key from `SSH_PRIVATE_KEY` (key rotation, SOP §5). |
| `skpatch_hosts_dashboard_address` / `_port` | host var | The address the dashboard container dials, when the controller's (an ssh alias, a tailnet name) does not resolve there. |

Matched by name: missing is created, different is updated, anything the
input does not name is reported and left alone. Nothing is ever deleted.
Notification channels are rewritten on every run (the API only returns their
secrets masked, so they cannot be compared). A system whose package manager
is not detected yet (registered this run, first check pending) gets its
upgrade mode on the next run.

**What the pinned app (2026.10.9) actually does, which the schedule design
has to respect** (read from `server/services/scheduler.ts`):

- An **update** schedule refreshes its systems in parallel, then runs
  upgrade-all on them **one at a time, in the order of `systems`**. One
  schedule is therefore a strict serial chain; two schedules whose windows
  overlap run in parallel.
- Upgrade-all uses each system's APT mode. There is no "full-upgrade"
  schedule type, and plain `apt-get upgrade` keeps back anything that needs
  a new package, which on Ubuntu includes every new kernel ABI. Set
  `UPGRADE_MODE: full-upgrade` if kernels and dependency-changing security
  fixes should land on the schedule rather than by hand.
- A **reboot** schedule sends `reboot` to each system and moves on without
  waiting for it to come back. Never put two swarm managers (or every worker)
  in one reboot schedule: give each its own, staggered.
- Upgrades run under `nohup setsid` on the host and survive the dashboard
  container restarting; the schedule itself does not. Put the node that runs
  the dashboard last in its window.
- A Proxmox reboot is blocked by the app's own backup guard unless sudoers
  grants `pvesh get /cluster/tasks`, which this role does not (sudoers
  template). Hypervisors are rebooted from Proxmox, not from here.
- An enabled **Telegram** channel starts a `getUpdates` long-poll worker and
  rewrites that bot's command menu, whatever `commandsEnabled` says
  (`server/services/telegram-bot.ts` `sync()`). Never point it at a bot
  another service already uses; give it its own bot, or use email.
- Docker engine packages (`docker-ce`, `containerd.io`) upgrade like any
  other package and restart `dockerd`, moving that node's tasks. This role
  holds nothing (`apt-mark hold` is not in the sudoers and is not used);
  the safety comes from ordering: swarm nodes are upgraded in serial chains,
  managers in their own chain after the workers, so at most one `dockerd`
  restarts at a time and manager quorum is never at risk.

Example (estate specifics, host names and times, belong in the estate's own
prod repo):

```yaml
skpatch_hosts_schedules:
  - {name: Default refresh, type: refresh, cron: "15 */6 * * *", cache_hours: 6}
  - {name: Nightly workers, type: update, cron: "0 2 * * *", systems: [wkr-1, wkr-2]}
  - {name: Nightly managers, type: update, cron: "0 3 * * *", systems: [mgr-1, mgr-2, mgr-3]}
  - {name: Reboot mgr-1, type: reboot, cron: "0 4 * * 2", only_if_required: true, systems: [mgr-1]}
  - {name: Reboot mgr-2, type: reboot, cron: "30 4 * * 2", only_if_required: true, systems: [mgr-2]}
  - {name: Daily digest, type: notification_digest, cron: "0 7 * * *", notifications: [Patch digest]}
```

## Related projects / See also

- ⬆️ **Depends on:** [skfence / skfenceha](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skfence), Traefik ingress every router here assumes; [sksso](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#sksso), Authentik, for the default `AUTH_MODE: sso`.
- ⬇️ **Part of:** [the skpatch program](../../../../docs/skpatch-program.md): this role is the host-patching half; the image-patching half is `skbom` plus `skflow` below.
- ↔️ **Sibling:** [skbom](../../../../docs/skpatch-program.md#2-components), Dependency-Track, the image-side CVE register this program's other half feeds.
- ↔️ **Sibling:** [skflow](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skflow), Windmill, which runs the program's `f/skpatch`/`f/skrelease`/`f/ops` automation flows.
- ↔️ **Sibling:** [sksec-web](../sksec-web/), another SSO-gated single-purpose ops dashboard with the same read-only-by-default, actions-as-explicit-opt-in shape.
- ↔️ **Sibling:** [skgit](https://github.com/smilinTux/skstacks/blob/main/docs/v1-service-catalog.md#skgit), the repo of record the program's Renovate PRs and release gate target.
- 📐 **Standards:** [sk-standards](https://github.com/smilinTux/sk-standards), the doc/SOP, architecture & data-flow, and cryptography standards this README and its SOP follow.

*License: this role is Apache-2.0 (the framework's own license). The
upstream application it deploys, `linux-update-dashboard`, is AGPL-3.0
and is referenced by image reference only: nothing from it is vendored.*
