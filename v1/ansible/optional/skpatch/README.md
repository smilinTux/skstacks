# skpatch

**Deploys [linux-update-dashboard](https://github.com/TheDuffman85/linux-update-dashboard) (AGPL-3.0) on the swarm: a self-hosted dashboard that checks, and if armed applies, Linux package updates across a fleet of hosts over SSH.** This is the host-patching half of the wider [skpatch program](../../../../docs/skpatch-program.md); the image-patching half (SBOM scanning, CVE tracking, Renovate) is a separate, mostly-planned pipeline described there. **Maturity tier:** T0, Classical (see "Honest claims"). **Version phase:** framework `v1` tree (this repo's own [Versions table](../../../../README.md#versions): "Published, one service at a time", not the generic ecosystem `VERSION_LIFECYCLE` "Legacy" phase).

Opt-in: nothing renders or changes for an instance that never runs
`deploy_skpatch-<env>.yml`.

## Quickstart

```bash
# 1. Deploy the dashboard (report-only by default: ALLOW_ACTIONS: false)
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpatch/deploy_skpatch-prod.yml \
  -e target_manager_group=swarm_managers \
  --vault-password-file ~/.vault_pass_env/.<instance>_prod_vault_pass

# 2. Give it something to patch: provision one estate's nodes (separate playbook)
ansible-playbook -i envs/prod/inventory.ini \
  framework/v1/ansible/optional/skpatch/provision_skpatch_nodes.yml \
  -l <estate-inventory-group> -e skpatch_provision_env=prod \
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
  by `provision_skpatch_nodes.yml` on each target node, gated by that
  node's **own** `skpatch_allow_actions` (see SOP §6).
- **One SSH keypair per estate, never shared, never a reused admin key.**
  A compromise of this container is a compromise of every host it holds
  a key for; see SOP §1 "Why this app gets more scrutiny than most".

## How this fits the program

`skpatch` the role does hosts; `skpatch` the program also covers
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
  - [`deploy_skpatch-<env>.yml`](./) deploys the dashboard stack.
  - [`provision_skpatch_nodes.yml`](./provision_skpatch_nodes.yml) is
    idempotent and reversible: it creates the low-privilege account,
    installs the estate's keypair, and writes the sudoers allowlist on
    each target.
  - [`src/skpatch/skpatch.sudoers.j2`](./src/skpatch/skpatch.sudoers.j2)
    is the exact-command allowlist; this is where "report-only" is
    actually enforced, not in the app.
  - [`SOP.md`](./SOP.md) has the full operating procedure (build, test,
    deploy, configuration reference, troubleshooting).
- **The data it owns.** `dashboard.db` (SQLite, under `/data`): every SSH
  credential this instance holds, AES-256-GCM-encrypted at rest.
- **External dependencies & integration points.** Traefik (skfence or
  skfenceha) for ingress; Authentik (sksso) for the default SSO-gated
  sign-in; SSH to whatever nodes `provision_skpatch_nodes.yml` has been
  run against.
- **Maturity tier.** T0, Classical. See "Honest claims" above.

## Configuration knobs (vault)

The full reference (every key, every default) is SOP §6. The ones that
matter most when reading this role for the first time:

| Key | Default | What it controls |
|---|---|---|
| `LUDASH_ENCRYPTION_KEY` | required, no default | AES-256-GCM key for the credential store. Losing it makes every stored credential unreadable. |
| `AUTH_MODE` | `sso` | `sso` puts Authentik forward auth in front of the whole router (default); `local` uses the app's own login only; `oidc` is the dashboard's own OIDC login against sksso, no forward auth. See "Sign-in and the admin group". |
| `ALLOW_ACTIONS` | `false` | Instance-wide **metadata**. A node only gets upgrade/reboot sudoers lines when its own `skpatch_allow_actions` (or, if unset, this) is `true`. |
| `SSH_PUBLIC_KEY` | required by provisioning | Public half of this estate's dedicated keypair. |
| `PROVISION_USER` | `skpatch-ro` | The low-privilege account `provision_skpatch_nodes.yml` creates on each target node. |
| `DATA_NODE` | `""` | Required if `DATA_PATH` is local disk, not shared storage; pins the single dashboard replica there. |
| `CLUSTERNAME` | inventory `cluster_name` | First label of the dashboard host (`skpatch.<CLUSTERNAME>.<DOMAIN>`) and the stack name suffix. |
| `DOMAIN` | inventory `domain` | Zone the dashboard host lives under; with `CLOUDFLARED: true` the host is `skpatch.<DOMAIN>` instead. |

Full vault contract (every key the deploy and the provisioning playbook
read; SOP §6 carries the same block):

```yaml
skpatch:
  LUDASH_ENCRYPTION_KEY: "<openssl rand -base64 32>"  # required, secret, >= 32 chars
  CLUSTERNAME: cluster1                     # default: inventory cluster_name
  DOMAIN: example.com                       # default: inventory domain
  CLOUDFLARED: false                        # true: default host is under DOMAIN, not CLUSTERNAME.DOMAIN
  HOSTNAME: ""                              # public host; default skpatch[-dev|-staging].<base domain>
  TRAEFIK_MIDDLEWARES: ""                   # default default-no-error-pages@file
  AUTH_MODE: sso                            # sso (Authentik forward auth, whole router) | local | oidc
  SSO_MIDDLEWARE: ""                        # sso mode: default authentik@file; must be empty in local/oidc mode
  OIDC_ISSUER: ""                           # oidc mode: required, e.g. https://<sso host>/application/o/<client id>/
  OIDC_CLIENT_ID: ""                        # oidc mode: default the router name (e.g. skpatch)
  OIDC_CLIENT_SECRET: ""                    # oidc mode: required, >= 32 chars, same value as sksso's SKPATCH_OIDC_CLIENT_SECRET
  OIDC_SCOPE: "openid profile email groups"
  OIDC_GROUPS_CLAIM: groups
  OIDC_ADMIN_GROUPS: "authentik Admins"     # CSV of Authentik group names
  OIDC_READ_ONLY_GROUPS: ""                 # CSV, default none
  OIDC_UNMATCHED_ROLE: deny                 # deny | admin | read-only
  OIDC_END_SESSION: true
  TLS_OPTIONS: ""                           # default intermediate@file; "none" = no tls.options label
  TZ: UTC
  LUDASH_LOG_LEVEL: info
  extra_env: {}                             # any other upstream app variable, verbatim into skpatch.env
  IMAGE: ""                                 # default: the pinned linux-update-dashboard digest in defaults/main.yml; must carry @sha256:
  DATA_PATH: ""                             # default /var/data/skpatch-<env>/data
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
  AUTHENTIK_TILE_NAME: SKPatch              # tile title; slug/description/icon/group/open-in-new-tab below
  AUTHENTIK_TILE_SLUG: skpatch
  AUTHENTIK_TILE_DESCRIPTION: "Linux update dashboard"
  AUTHENTIK_TILE_ICON: "fa://fa-wrench"
  AUTHENTIK_TILE_GROUP: "authentik Admins"  # must already exist
  AUTHENTIK_TILE_OPEN_IN_NEW_TAB: true
  # Read by provision_skpatch_nodes.yml, not the swarm deploy:
  ALLOW_ACTIONS: false                      # instance-wide default; a host's own skpatch_allow_actions: false always wins
  PROVISION_USER: skpatch-ro                # the low-privilege account's name
  PROVISION_UID: 5201                       # its fixed uid and gid; never a system uid (containers share the host's uid space)
  SSH_PUBLIC_KEY: ""                        # required by provision_skpatch_nodes.yml; one keypair per estate
```

**Per-host actions ceiling:** set `skpatch_allow_actions: false` in the
group_vars of every hypervisor, NAS, or any host that must never be
upgraded or rebooted through this app. That per-host `false` always
wins over the vault's instance-wide `ALLOW_ACTIONS: true`.

## Sign-in and the admin group

`AUTH_MODE`:

- **`sso` (default): Authentik forward auth on the whole router.**
  `SSO_MIDDLEWARE` (default `authentik@file`). Authentik needs a proxy
  provider in forward-auth mode covering the `skpatch` host, bound to the
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
    (32+ chars, **the same value** as sksso's `SKPATCH_OIDC_CLIENT_SECRET`).
  - `OIDC_CLIENT_ID` defaults to the router name (e.g. `skpatch`), the same
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
"sksec-web / skpatch (OIDC) login".

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
