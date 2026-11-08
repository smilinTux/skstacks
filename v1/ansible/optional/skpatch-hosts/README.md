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
  OIDC_ADMIN_GROUPS: "authentik Admins"     # CSV of Authentik group names
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
  HEALTH_CHECK_START_PERIOD: 60s
  DASHBOARD_CPUS: "0.50"
  DASHBOARD_MEMORY_LIMIT: 384M
  DASHBOARD_MEMORY_RESERVATION: 64M
  AUTHENTIK_TILE: true                      # false opts out of the portal tile
  AUTHENTIK_TILE_NAME: SKPatch Hosts        # tile title; slug/description/icon/group/open-in-new-tab below
  AUTHENTIK_TILE_SLUG: skpatch-hosts
  AUTHENTIK_TILE_DESCRIPTION: "Linux update dashboard"
  AUTHENTIK_TILE_ICON: "fa://fa-wrench"
  AUTHENTIK_TILE_GROUP: "authentik Admins"  # must already exist
  AUTHENTIK_TILE_OPEN_IN_NEW_TAB: true
  # Read by provision_skpatch_hosts_nodes.yml, not the swarm deploy:
  ALLOW_ACTIONS: false                      # instance-wide default; a host's own skpatch_hosts_allow_actions: false always wins
  PROVISION_USER: skpatch-ro                # the low-privilege account's name
  PROVISION_UID: 5201                       # its fixed uid and gid; never a system uid (containers share the host's uid space)
  SSH_PUBLIC_KEY: ""                        # required by provision_skpatch_hosts_nodes.yml; one keypair per estate
  # Host auto-registration (see "Automatic host registration" below); both required, else skipped
  API_TOKEN: ""                             # dashboard API token (systems:add, systems:edit), minted once by hand
  API_CREDENTIAL_ID: 0                       # the SSH credential's numeric id, created once by hand
  PROVISIONER_IMAGE: ""                      # default: the pinned python:3.13-alpine digest
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
    (default `groups`), `OIDC_ADMIN_GROUPS` (default `authentik Admins`,
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

## Automatic host registration

> **Not supported with the pinned dashboard (2026.10.9).** Its API refuses every API-token request
> to create or edit a system (`POST`/`PUT /api/systems` answer 403 "API tokens cannot configure SSH
> connections"), so a token-based sync cannot work. With `API_TOKEN`/`API_CREDENTIAL_ID` set,
> `provision_skpatch_hosts_nodes.yml` now fails with that explanation instead of half-registering.
> Add nodes in the dashboard for now. A redesign on a signed-in dashboard session is tracked.

`provision_skpatch_hosts_nodes.yml`, on every `present` run, upserts one
dashboard system per node in its own target group, so the dashboard is
never empty after a deploy. Opt-in: nothing calls the dashboard's API
unless both `API_TOKEN` and `API_CREDENTIAL_ID` are set; unset, the
playbook reports that it is skipping registration and nodes can be added
by hand instead, exactly as before this feature existed.

**Why a one-time bootstrap is unavoidable.** Researched against the
pinned upstream source (`API.md`, `server/routes/api-tokens.ts`,
`server/routes/credentials.ts`): the dashboard's API tokens are a real,
separate auth path from session-cookie login (`AUTH_MODE: oidc` and
`LUDASH_PASSWORD_LOGIN_ENABLED: false` do not block a Bearer token call),
so a token minted once covers every later deploy regardless of sign-in
mode -- **no local-admin-password bootstrap is needed.** But every token,
with or without permissions, is explicitly barred from `/api/credentials`
(the upstream token-scope list), and `POST /api/systems` requires an
existing `credentialId`. Nothing upstream lets a token create that
credential; only a signed-in human can, through the UI. That one
human step is the entire bootstrap, done once per estate:

1. Sign in to the dashboard (as the first-run local admin before `oidc`
   is turned on, or, once it is, via OIDC through sksso -- either works,
   this step does not care which).
2. **Settings → API Tokens:** create a token named for this purpose,
   expiry `never`, permissions `systems:add` and `systems:edit`. Copy the
   token (shown once) into the vault as `API_TOKEN`.
3. **Add one credential** (Systems → add a system → new credential, or
   Settings → Credentials if the version running exposes it there): kind
   SSH key, username `skpatch_hosts.PROVISION_USER` (default `skpatch-ro`),
   private key the **same estate keypair** whose public half is already
   in `SSH_PUBLIC_KEY` and installed on every node by
   `provision_skpatch_hosts_nodes.yml`. Note its numeric credential id
   (visible in the UI, or in the id the dashboard assigns when you add
   the very first system by hand with it) and put it in the vault as
   `API_CREDENTIAL_ID`.

Redeploy or rerun `provision_skpatch_hosts_nodes.yml` after both are set;
every subsequent `present` run keeps the dashboard's system list in sync
with that run's target group with no further manual step.

**Identity and idempotency.** The upstream app's own uniqueness
constraint is `(hostname, port, username)`, confirmed from the pinned
source (`system-service.ts`, `DuplicateSystemConnectionError`); every
system this registers shares the one `API_CREDENTIAL_ID`, so hostname and
port are this role's own match key. A name-only change updates the
existing system in place. **Hosts removed from the target inventory are
reported, never deleted** -- their history and settings stay in the
dashboard until an operator removes them by hand.

**Host-key verification is off** for every system this creates
(`hostKeyVerificationEnabled: false`): these are the operator's own
already-provisioned fleet nodes, reached with the same keypair
`provision_skpatch_hosts_nodes.yml` just installed, not an arbitrary host
a human is adding one at a time through the UI (where strict pinning
belongs). Turn it on by hand per system afterward if a given estate wants
it.

```yaml
skpatch_hosts:
  API_TOKEN: ""                             # dashboard API token (systems:add, systems:edit); unset skips registration
  API_CREDENTIAL_ID: 0                       # the SSH credential's numeric id (see bootstrap above); unset skips registration
  PROVISIONER_IMAGE: ""                      # default: the pinned python:3.13-alpine digest shared/src/authentik_portal_tile.py also uses
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
