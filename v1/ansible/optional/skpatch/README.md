# skpatch

[linux-update-dashboard](https://github.com/TheDuffman85/linux-update-dashboard)
(AGPL-3.0) on the swarm: a self-hosted dashboard that checks and (if armed)
applies Linux package updates across a fleet of hosts over SSH. Opt-in:
nothing renders or changes for an instance that never runs
`deploy_skpatch-<env>.yml`.

## Upstream, license and image

- **License.** AGPL-3.0 (`github.com/TheDuffman85/linux-update-dashboard`).
  This role **references the upstream image and vendors nothing**.
- **Image.** `ghcr.io/theduffman85/linux-update-dashboard`, release
  `2026.10.9`, multi-arch (amd64, arm64), pinned by the manifest-list
  digest; the play refuses an image without `@sha256:`.
- **No phone-home by default.** The only outbound traffic the app makes on
  its own is the SSH connections an admin configures to the nodes it is
  pointed at, plus whatever notification channel (email, etc.) an admin
  wires up in Settings.

## Why this app gets more scrutiny than most

This is the highest-trust app this framework deploys. It is **designed** to
hold a credential that can SSH into every host it is pointed at and, if an
admin lets it, run privileged commands there. A compromise of this
container is a compromise of every host it has a key for. Every design
choice below exists to shrink that blast radius.

### Report-only by default, actions are an explicit opt-in

Upstream is **not** report-only out of the box: it can run package upgrades
and reboot hosts (it also ships a genuine read-only *user role* inside its
own RBAC, which is a different thing: a per-user UI permission, not a
system-level guarantee). This role's privilege model does not rely on the
app's own RBAC to make that promise. It relies on **what the SSH user on
each target node is allowed to run**:

- `skpatch.ALLOW_ACTIONS` (vault, default `false`) is metadata: by itself it
  grants nothing. The deploy playbook reports the posture but writes no
  privilege anywhere.
- The actual enforcement is `provision_skpatch_nodes.yml` (see below): it
  writes a tight, exact-command sudoers rule on each target node, and only
  adds the upgrade/reboot lines when that node's own `skpatch_allow_actions`
  (or, if unset, the vault's `skpatch.ALLOW_ACTIONS`) is `true`.
- **Hypervisors, the NAS, and anything else that must never run an upgrade
  or a reboot through this app** set `skpatch_allow_actions: false` in
  their own inventory group_vars. That host-level `false` always wins, even
  when the instance-wide `skpatch.ALLOW_ACTIONS` is `true`. Rebooting a
  hypervisor takes its whole cluster down with it; this is not a knob to
  get wrong.

So: deploy the dashboard with `ALLOW_ACTIONS: false` (the default) and
nothing on any node can be upgraded or rebooted through it, full stop,
regardless of what an admin clicks in the UI. Flip it on, per estate, only
once the access model for that estate has been reviewed and approved.

### Credentials and the SSH key

- The app's own database (`dashboard.db`, SQLite, under `/data`) holds
  every SSH credential it is given, AES-256-GCM-encrypted at rest with
  `LUDASH_ENCRYPTION_KEY` (vault secret, written only to the dashboard's
  0600 env file). Losing or changing that key makes every stored credential
  unreadable; they must be re-entered.
- The SSH keypair this role provisions is **one per estate**, generated
  fresh for this purpose (`ssh-keygen -t ed25519 -f skpatch_<estate>_ed25519
  -C skpatch@<estate>`), never a reused admin key (`ansible`,
  `holouser*`, `skuser*`, `root`, ...). The private half lives only in that
  estate's vault/secret store; the public half is `skpatch.SSH_PUBLIC_KEY`
  in the vault that `provision_skpatch_nodes.yml` reads.
- **No cross-estate reuse.** An instance only ever holds keys for the
  nodes in its own estate, reached over that estate's own LAN or tailnet.
  Never let one estate's skpatch instance hold another estate's keys, and
  never route one estate's dashboard through another estate's network.

## Design

### Service

| Service | Image | Replicas | What |
|---|---|---|---|
| `dashboard` | `linux-update-dashboard` | **1, always** | Node app over a single SQLite file, everything under `/data` (`dashboard.db`: credentials, schedules, activity history). Exactly one replica: it is the only writer of the database file. Updates are `stop-first`. |

Traefik (skfenceha, or skfence) routes `https://<host>/` to
`dashboard:3001` (health check `/`). Networks: `skpatch-<env>` and
`cloud-public-<env>`.

### Placement and data

- `DATA_PATH` (default `/var/data/skpatch-<env>/data`, NFS on a typical
  instance) is bind-mounted at `/data`. For local disk, set `DATA_PATH`
  outside `/var/data` and `DATA_NODE` to the node that holds it: the
  play refuses a local path without `DATA_NODE` (local data on a floating
  service is an empty, credential-less instance after the next
  reschedule). Same rule as sksso/skhub/skpayment/skgym/skhome.
- `PLACEMENT_CONSTRAINTS` defaults to `["node.role == worker"]`.

### Backups

Not automated by this role in v1. `dashboard.db` is a single SQLite file;
upstream documents no dedicated backup CLI the way HomeCenter (skhome)
does, so this role does not invent one. With skbackup on the storage host
(tier 2 copy of the app dir), add a plain directory copy entry, e.g.:

```yaml
skbackup:
  copy:
    apps:
      - name: skpatch-prod
        newest: [{dir: data, keep: 2}]
```

A copy taken while the app is writing is a normal SQLite consistency risk
(not specific to this role); stop the `dashboard` service first
(`docker service scale skpatch-<env>_dashboard=0`) for a guaranteed-clean
copy if that matters for a given instance.

### Sign-in and the admin group

The upstream app has its **own** accounts (password+bcrypt, TOTP,
passkeys/WebAuthn) *and* native OIDC support of its own. This role does
**not** wire the native OIDC path by default; it uses the same two-path
pattern as skhome/skgym instead, because an app that can SSH into every
node in the estate is exactly the case for defense in depth (two
independent logins, not one):

- **`sso` (default): Authentik forward auth in front of the WHOLE
  router.** `SSO_MIDDLEWARE` (default `authentik@file`) guards every path;
  there is no API consumer to carve out (unlike skgym's phone app).
  **Bind the Authentik application to the instance's admin group, not
  every Authentik user.** Users then sign in twice: Authentik, then the
  app's own login.
- **`local`: no SSO.** The app's own login only. For a private instance,
  add an IP allowlist to `TRAEFIK_MIDDLEWARES` as well.

Guard (the deploy refuses, naming the keys): an `AUTH_MODE` other than
`sso`/`local`; `AUTH_MODE: local` with `SSO_MIDDLEWARE` set; a forward-auth
middleware inside `TRAEFIK_MIDDLEWARES` (in `local` mode that is SSO under
another name; use `AUTH_MODE: sso` instead).

Password login at the app level cannot be fully disabled upstream (a
lockout guard: it stays available unless passkeys or SSO are configured),
so treat the first account created in the app as sensitive and keep the
Authentik binding narrow; this is the same shape as every other
forward-auth-fronted app in this framework.

### Traefik

`TRAEFIK_MIDDLEWARES` is the router's whole middleware chain, default
`default-no-error-pages@file` (skfenceha: rate limit, security headers,
gzip), not the usual `default@file`: the app's own JSON API errors
(credential validation, login) should not be replaced with an HTML page.
`TLS_OPTIONS` defaults to `intermediate@file`; `none` omits the label.

### Health checks

`dashboard`: `wget --spider -q http://127.0.0.1:3001/` (upstream documents
no dedicated health endpoint; this probes the app shell itself). Traefik's
service health check is `/`.

## Provisioning and least privilege (`provision_skpatch_nodes.yml`)

A separate, idempotent, reversible playbook, not a swarm deploy. It runs
against whatever inventory group holds the nodes an estate's skpatch
instance should be able to reach, and:

1. Creates a dedicated system account (`skpatch.PROVISION_USER`, default
   `skpatch-ro`) on each targeted host. Never reuses an existing admin
   account.
2. Installs exactly one authorized key: `skpatch.SSH_PUBLIC_KEY` (the
   public half of that estate's dedicated keypair), `exclusive: true` (no
   other key survives on that account).
3. Writes `/etc/sudoers.d/skpatch` from `src/skpatch/skpatch.sudoers.j2`:
   always the read-only lines (`apt list --upgradable`, a dist-upgrade dry
   run, the reboot-required check); the upgrade/reboot lines **only** when
   `skp_node_allow_actions` is true for that host.
4. `skpatch_provision_state: absent` (command line `-e`, not a vault key) removes the account, its home, its key and the
   sudoers file from every targeted host, cleanly.

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpatch/provision_skpatch_nodes.yml \
  -l <estate-inventory-group> \
  -e skpatch_provision_env=prod \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

**Per-host actions ceiling.** Set `skpatch_allow_actions: false` in the
group_vars of any host that must stay report-only no matter what: every
hypervisor, every NAS, and anything else where an upgrade or (especially)
a reboot run through this app would be unacceptable. That per-host
`false` always beats the vault's instance-wide `skpatch.ALLOW_ACTIONS`.
There is deliberately no mechanism in this role to override a host's own
`false` from the vault side: the host inventory is the one place that
knob can be set to off.

**Non-Debian hosts.** The shipped sudoers template covers apt
(Debian/Ubuntu) only, which is every host in both estates today. A host on
another package manager needs its own read (and, if armed, action) lines
added to the template by hand before it is provisioned; see upstream's own
sudoers-generator documentation for the dnf/pacman/apk equivalents.

## Deployment

```bash
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpatch/deploy_skpatch-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass
```

The play: checks the vault (fails on a missing `LUDASH_ENCRYPTION_KEY`, an
image without a digest, a local data path without `DATA_NODE`,
contradictory sign-in settings), creates the directories, installs the
env/compose files, ensures the networks, deploys stack `skpatch-<env>`,
and restarts `dashboard` when its env file changed.

Run `provision_skpatch_nodes.yml` against the target estate's nodes
**before or after** this deploy, in either order; the dashboard itself
only needs the credential entered in its own UI (Settings -> Systems) to
start reaching a node, which an admin does once the account exists there.

## Configuration (vault)

`skpatch:` in `optional/group_vars/<env>/skpatch-<env>-<domain dashed>-<cluster>_vault.yml`.

```yaml
skpatch:
  LUDASH_ENCRYPTION_KEY: "<openssl rand -base64 32>"  # required, secret, at least 32 characters
  CLUSTERNAME: cluster1                     # default: inventory cluster_name
  DOMAIN: example.com                       # default: inventory domain
  CLOUDFLARED: false                        # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                              # public host; default skpatch[-dev|-staging].<base domain>
  TRAEFIK_MIDDLEWARES: ""                   # default default-no-error-pages@file
  AUTH_MODE: sso                            # sso (Authentik forward auth, whole router) | local
  SSO_MIDDLEWARE: ""                        # sso mode: default authentik@file; must be empty in local mode
  TLS_OPTIONS: ""                           # default intermediate@file; "none" = no tls.options label
  TZ: UTC
  LUDASH_LOG_LEVEL: info
  extra_env: {}                             # any other upstream app variable, verbatim into skpatch.env
  IMAGE: ""                                 # default below; must carry @sha256:
  DATA_PATH: ""                             # default /var/data/skpatch-<env>/data
  DATA_NODE: ""                             # pin dashboard to this node (required with a local path)
  PLACEMENT_CONSTRAINTS: []                 # default ["node.role == worker"]
  HEALTH_CHECK_INTERVAL: 30s
  HEALTH_CHECK_TIMEOUT: 10s
  HEALTH_CHECK_RETRIES: 3
  DASHBOARD_CPUS: "0.50"
  DASHBOARD_MEMORY_LIMIT: 384M
  DASHBOARD_MEMORY_RESERVATION: 64M
  # Read by provision_skpatch_nodes.yml, not the swarm deploy:
  ALLOW_ACTIONS: false                      # instance-wide default; a host's own skpatch_allow_actions: false always wins
  PROVISION_USER: skpatch-ro                # the low-privilege account's name
  SSH_PUBLIC_KEY: ""                        # required by provision_skpatch_nodes.yml; one keypair per estate
```

Default image:
`ghcr.io/theduffman85/linux-update-dashboard:2026.10.9@sha256:1fb855a4d4b5eacc0ef3a9816064184492e9d068fb4da353c62871faeeeb1543`
(full digest in the playbooks). Networks: override with the flat per-env
`skpatch_<env>_networks` list.

## Files on the manager

| Path | Mode |
|---|---|
| `/var/data/config/skpatch-<env>/skpatch.env` | `0600` |
| `/var/data/config/skpatch-<env>/skpatch.sh.env` | `0640` |
| `/var/data/config/skpatch-<env>/skpatch.yml` (+ `docker-compose.yml` link) | `0644` |
| `/var/data/skpatch-<env>/deploy` (+ `/var/data/deploy_skpatch-<env>` link) | `0755` |
| `<DATA_PATH>/` | `0700` root (the image chowns `/data` on its own first start) |

On every provisioned node: `/etc/sudoers.d/skpatch` (`0440`), the
`skpatch-ro` account and its `~/.ssh/authorized_keys`.

## Networks

| Network | Subnet |
|---|---|
| `skpatch-prod` / `-staging` / `-dev` | `172.16.154.0/24` / `172.16.155.0/24` / `172.16.156.0/24` |
| `cloud-public-<env>` | framework-shared Traefik network |

## Gotchas

- This is the one app in the catalog whose compromise means *every node it
  can reach* is at risk. Keep `ALLOW_ACTIONS` off until the access model
  for that specific estate has been reviewed, and keep hypervisors/NAS
  permanently excluded via their own `skpatch_allow_actions: false`
  regardless.
- The dedicated SSH keypair and `LUDASH_ENCRYPTION_KEY` are both secrets
  that unlock every other credential the app holds: rotate them the same
  way any root-equivalent secret gets rotated, and never let them leave
  the estate's own vault.
- Native OIDC support exists upstream but is not wired by default; wiring
  it would replace, not add to, the forward-auth layer. If an instance
  ever wants that instead of the two-login pattern above, that is a
  deliberate, separate decision, not a vault default.

## Portal tile (Authentik)

Opt-in (`skpatch.AUTHENTIK_TILE: true`, default `false`): the next deploy creates or
updates a provider-less Authentik `core.Application` named `AUTHENTIK_TILE_NAME`
(default `SKPatch (Linux updates)`), launch URL `https://<the public host above>`,
icon `AUTHENTIK_TILE_ICON` (default `fa://fa-wrench`), bound to the group
`AUTHENTIK_TILE_GROUP` (default `authentik Admins`, which must already exist: this
app holds the keys to every node it reaches, so the default is deliberately the
admin group, not a broader one). This only adds a dashboard bookmark; it creates no
provider and changes nothing about `AUTH_MODE`/forward auth above. Needs
`sksso.api_token` in sksso's own vault (see `optional/sksso/README.md`). See
`shared/tasks/authentik_portal_tile.yml`.
