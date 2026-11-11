# SSO app access provisioning: per-app groups, a baseline bundle, and fail-closed access

Status: design proposal (docs only, no role or template code changes in this PR).
Related: `v1/docs/sso-estate-plan.md` (patterns per service), `v1/ansible/optional/sksso/README.md`
("Application access and group model"), open PR #360 (portal tile and OIDC admin defaults move from
`authentik Admins` to `skadmin`). A companion rules document in the standards repo defines the
normative group names and access modes; where this design uses working names (`<app>-admins`,
`<app>-users`), the standard decides the final form and the names are one variable away.

## 1. Why this exists

A live audit of one instance (2026-11-10) found four problems that all trace to the same cause: the
framework creates Authentik portal tiles and OIDC applications, but it has no model of who may use
each app, and where it has no opinion Authentik's own default is "admit everyone who can log in".

- Tiles were bound to `authentik Admins`, so a person in `skadmin` saw 11 of 39 applications.
- The domain-wide forward-auth catch-all application had no policy binding at all. With zero bindings
  Authentik admits every authenticated user, so roughly 27 community file-sharing users could pass
  forward auth for admin tools.
- Several provider-backed applications (a hypervisor login, a remote-control login) had no bindings.
- There was no `skuser` group and no user-level test account, so no test could ever prove a denial.

The fix is not another hand-edit. It is one declared access model per app, applied by the same code
that creates the tile, with a conformance check that fails when an app has no binding.

## 2. Current state (read from the code)

### 2.1 The shared tile mechanism

`v1/ansible/shared/tasks/authentik_portal_tile.yml` is included from a role's tasks with a
`portal_tile:` dict (`enabled`, `slug`, `name`, `launch_url`, `group`, `icon`, `icon_file`,
`icon_name`, `open_in_new_tab`, `description`, `category`, plus plumbing knobs `authentik_network`,
`authentik_internal_url`, `sso_host`, `base_domain`, `authentik_vault_file`, `provisioner_image`,
`require_sksso`). It skips with a warning when sksso's overlay network is missing, loads sksso's
vault for `sksso.api_token`, stages `shared/src/authentik_portal_tile.py` and runs it in a throwaway
container on sksso's network. The script receives one JSON document on stdin.

`v1/ansible/shared/src/authentik_portal_tile.py` does exactly two things:

1. `ensure_application`: create or patch a `core.Application` (`provider: null`, launch URL, icon,
   description, category, `policy_engine_mode: any`). If the slug already has a provider (an OIDC
   login created by a blueprint), the provider is kept and only tile fields are managed.
2. `ensure_group_binding`: look up ONE group by name, fail loudly when it does not exist ("this script
   never creates groups"), and create a policy binding (`order 0`) if no binding for that app and
   that group exists. It never removes a binding.

Consequences that matter for this design:

- **One group per app, and it must pre-exist.** There is no per-app admin/user split, and no code
  anywhere creates `skadmin`, `skuser`, `skhub-users` or any other group (the skflow blueprint
  creates `skflow-operators`; the rest are hand-made).
- **Bindings are additive.** After PR #360 changes the default to `skadmin`, an app that already has a
  `authentik Admins` binding gets a second binding, and keeps the first. Nothing migrates it.
- **The tile is a bookmark, not a gate.** A provider-less application is never consulted by forward
  auth. Its binding only controls portal visibility. The only thing that gates a request is the
  application behind the matched forward-auth or OIDC provider.
- **Zero bindings is not representable as an error.** The script cannot tell "intentionally open" from
  "nobody bound it", and neither can anything else.

### 2.2 Who calls it

29 role playbooks include the shared task (`core/skfence`, `core/skfenceha` and 27 under `optional/`),
each in three env variants. Every one defaults `AUTHENTIK_TILE: true`, `AUTHENTIK_TILE_SLUG` to the
role's router name, and `AUTHENTIK_TILE_GROUP` to `authentik Admins` on main (`skadmin` once #360
lands). `sksso` itself carries the `portal-tile: opt-out` marker. `skrac` and `skmesh` use their own
provider scripts (`tasks/authentik.yml`) and do not use the shared task.

What actually gates each app is a separate question from the tile, and it differs by role:

| Role | Tile kind today | Tile group knob (default on main) | Gate the role itself enforces | OIDC role-mapping knobs |
|---|---|---|---|---|
| skfence | link (no provider) | `AUTHENTIK_TILE_GROUP` (`authentik Admins`) | dashboard: own gate, no forward-auth default | none |
| skfenceha | link | `AUTHENTIK_TILE_GROUP` (`authentik Admins`) | dashboard: `DASHBOARD_SSO_MIDDLEWARE` (opt-in), else basic auth, else LAN allowlist | none |
| skboard | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list; authentik not in default | none |
| skbom | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list; authentik not in default | none |
| skbook | link | `AUTHENTIK_TILE_GROUP` | `router_middlewares_pre` / `router_middlewares` (instance opt-in) | none |
| skdash | link | `AUTHENTIK_TILE_GROUP` | `router_middlewares_pre` / `router_middlewares` (instance opt-in) | none |
| skfetch | link, one per sub-service (loop) | `AUTHENTIK_TILE_GROUP`, one value for all | forward auth ON by default (`TRAEFIK_MIDDLEWARES` default includes `authentik@file`) | none |
| skflow | link (own slug, separate from the OIDC app) | `AUTHENTIK_TILE_GROUP` | native OIDC via sksso blueprint, group `skflow-operators` (blueprint-owned binding); `SSO_MIDDLEWARE` empty by default | none (Windmill reads its own `oauths` setting) |
| skgallery | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list | none |
| skgit | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list; local Forgejo accounts | none (admin user name only) |
| skgym | link | `AUTHENTIK_TILE_GROUP` | `AUTH_MODE: sso` default, `SSO_MIDDLEWARE` default `authentik@file` | none |
| skhome | link | `AUTHENTIK_TILE_GROUP` | `AUTH_MODE: sso` default, `SSO_MIDDLEWARE` default `authentik@file` | none |
| skhub | link on the provider-backed `skhub` app (provider kept) | `AUTHENTIK_TILE_GROUP` | native OIDC, group `skhub-users`; forward auth forbidden by design | none (`nc-*` provisioning groups are a separate mechanism) |
| skmail | link | `AUTHENTIK_TILE_GROUP` | app login; no middleware knob | none |
| skmon | link, one per sub-service (loop) | `AUTHENTIK_TILE_GROUP`, one value for all | hard-coded router middlewares | none |
| skorch | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list | none |
| skpatch-hosts | link, or provider-backed when `AUTH_MODE: oidc` (blueprint adds the provider, tile keeps it) | `AUTHENTIK_TILE_GROUP` | `AUTH_MODE: sso` default (`authentik@file`), `oidc` and `local` selectable | `OIDC_ADMIN_GROUPS` (`authentik Admins` on main), `OIDC_READ_ONLY_GROUPS` (empty), `OIDC_GROUPS_CLAIM`, `OIDC_UNMATCHED_ROLE` (`deny`) |
| skpayment | link | `AUTHENTIK_TILE_GROUP` | `MIDDLEWARES`, `WEBHOOK_MIDDLEWARES` | none |
| skpdf | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list | none |
| skpeek | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list | none |
| skport | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list | none |
| skpulse | link | `AUTHENTIK_TILE_GROUP` | `AUTH_MODE: sso` default (`authentik@file`) | none |
| sksec-web | link, or provider-backed when `AUTH_MODE: oidc` | `AUTHENTIK_TILE_GROUP` | `AUTH_MODE: sso` default (`authentik@file`), `oidc`, `local` | `OIDC_ADMIN_GROUPS` (`authentik Admins` on main), `OIDC_READ_ONLY_GROUPS` (empty), `OIDC_GROUPS_CLAIM`, `OIDC_UNMATCHED_ROLE` (`deny`) |
| skseek | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list | none |
| skstor | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES` list | none |
| skstream | link | `AUTHENTIK_TILE_GROUP` | `router_middlewares_pre` / `router_middlewares` (instance opt-in) | none |
| sksync | link | `AUTHENTIK_TILE_GROUP` | none exposed | none |
| skvector | link | `AUTHENTIK_TILE_GROUP` | `TRAEFIK_MIDDLEWARES`, `TRAEFIK_MIDDLEWARES_GRPC` | none |
| skwhoami | link, one per sub-service (loop) | `AUTHENTIK_TILE_GROUP` | `MIDDLEWARES` | none |
| skrac | own provider script, OAuth2 provider + application, no binding written | none (no tile task) | `AUTH_MODE: local` default, `oidc` optional | `OIDC_ADMIN_GROUP` (`skadmin`, singular), `OIDC_GROUP_CLAIM` |
| skmesh | own provider script, public OAuth2 client + application, no binding written | none (no tile task) | `AUTH_MODE: standalone` default, `authentik` optional | none (first login becomes owner) |
| sksso | opt-out (it is the portal) | none | itself | none |

Authentik side: `sksso` ships three blueprints (`skflow-windmill`, `sksec-web-oidc`,
`skpatch-hosts-oidc`). Only the skflow blueprint writes a group (`skflow-operators`) and a binding.
`ensure-api-token.sh` creates the `skstacks-automation` service account in `authentik Admins`. The
forward-auth middleware `authentik@file` (skfenceha and skfence dynamic middlewares) points at
`sksso-<env>_server:9000/outpost.goauthentik.io/auth/traefik`, that is the embedded outpost.

How a router opts in to forward auth, by mechanism:

- Role-owned default: `SSO_MIDDLEWARE` (default `authentik@file` when `AUTH_MODE: sso`) in skgym,
  skhome, skpatch-hosts, skpulse, sksec-web; `TRAEFIK_MIDDLEWARES` default in skfetch.
- Instance opt-in: `router_middlewares_pre` and `router_middlewares` in skbook, skdash, skhub, skstream;
  `TRAEFIK_MIDDLEWARES` lists elsewhere.
- Dashboard: `DASHBOARD_SSO_MIDDLEWARE` in skfenceha.

`authentik@file` appears as a role default in exactly those six roles. Most role comments say a service
"already sits behind the cluster's domain-wide ForwardAuth catch-all"; that describes how an instance
routes it, not something the role enforces.

### 2.3 The catch-all

No code in this repository creates the domain-wide forward-auth provider or its application. It is a
hand-made Authentik object (a domain-level proxy provider on the embedded outpost) on each instance.
Whichever router carries `authentik@file` and has no provider of its own is authorised against that one
application's bindings. The skfetch README already warns that "a domain-level catch-all provider admits
every Authentik user". With zero bindings this is what the audit found.

## 3. Gaps

1. No per-app group model: one group per tile, no admin/user split, nothing creates the groups.
2. Fail-open by omission: Authentik treats an application with no bindings as open. The catch-all, and the
   `skrac` and `skmesh` applications (their scripts write no binding), can be in that state.
3. The tile script cannot remove bindings, so a default change (PR #360) or a mistake is never
   reconciled; apps accumulate bindings.
4. The tile cannot gate anything: provider-less applications are not consulted by forward auth. An
   app that should be reachable by `<app>-users` but sits behind the catch-all is denied (or, with an
   open catch-all, admitted to everyone), regardless of its tile.
5. OIDC role mapping knobs are independent strings (`OIDC_ADMIN_GROUPS`, `OIDC_ADMIN_GROUP`) and can
   disagree with the Authentik binding of the same app. skrac and the two blueprints even use different
   spellings and defaults.
6. No `skuser` group, no baseline bundle, no test identities, and no check that lists unbound apps.
7. The tile task's header comment says a missing group "fails loudly". That is a safety property worth
   keeping in spirit (never ship a tile nobody can see) while removing the pre-creation burden.

## 4. Target design

### 4.1 Principles

- Fail-closed: an app is admin-only unless its role says otherwise. An application never has zero
  bindings. "Open" is an explicit mode that writes an explicit binding.
- `skadmin` reaches every app. It is always in the binding set except for `public`.
- `skuser` is a baseline bundle and never grants admin. A baseline app admits `skuser`; a non-baseline
  app never does.
- One declaration per app; tile visibility, forward-auth authorisation and OIDC role mapping are all
  derived from it, so they cannot drift.

### 4.2 The per-app variable block

Every role that has a tile gets the same block in its vault dict (names use the role's own dict, for
example `skpulse:`), all optional:

```yaml
skpulse:
  SSO_ACCESS: admin-only      # admin-only | app-groups | all-users | public   (default admin-only)
  SSO_BASELINE: false         # true: skuser also admitted; default false
  SSO_ADMINS_GROUP: ""        # default "<slug>-admins"
  SSO_USERS_GROUP: ""         # default "<slug>-users"
  SSO_ADMIN_GLOBAL_GROUP: skadmin   # estate-wide, normally left alone
  SSO_USER_GLOBAL_GROUP: skuser
```

Modes and the binding set each one produces (`G` = the global admin group, `A` = admins group,
`U` = users group, `S` = the global user group):

| `SSO_ACCESS` | `SSO_BASELINE` | Bound groups | Meaning |
|---|---|---|---|
| `admin-only` | false (default) | `G` | Only global admins. Per-app groups are not created. |
| `admin-only` | true | rejected at render | A baseline app admits users by definition. |
| `app-groups` | false | `G`, `A`, `U` | Global admins, app admins, app users. |
| `app-groups` | true | `G`, `A`, `U`, `S` | Same, and everyone in the baseline bundle. |
| `all-users` | (implied true) | `G`, `S` | Every member of the baseline bundle, no per-app groups. |
| `public` | n/a | one binding to the shared always-allow policy | Explicitly open. See 4.6. |

`all-users` means "everyone in `skuser`", not "every authenticated Authentik user". Authentik can hold
accounts that belong to other applications (community file-sharing users, service accounts); none of
them are in `skuser`.

Compatibility: an explicit `AUTHENTIK_TILE_GROUP` keeps working. It is treated as one extra bound group
(additive) and logs a deprecation line, so an instance that deliberately pins `authentik Admins` today
loses nothing. The roles' defaults for it become empty.

### 4.3 The estate baseline list

The baseline set has two sources, and the estate wins:

1. The role's `SSO_BASELINE` (default false).
2. `sksso.BASELINE_APPS` in sksso's vault, a list of application slugs. When the key is defined (even
   as an empty list) it replaces every role's `SSO_BASELINE`: an app is baseline if and only if its slug
   is in the list. When undefined, role values apply.

sksso's vault is the right home because the tile task already loads it (for `sksso.api_token`) on every
run, so no new lookup is needed, and one estate-wide list is reviewable in one place. A role that wants
to be in the bundle by default sets `SSO_BASELINE: true` in its role defaults file; the framework itself
ships none of them true (the default bundle is empty until an estate chooses).

### 4.4 How skuser is bound: directly, not as a parent group

Two ways to make `skuser` members reach a baseline app were considered.

- **Parent group**: make `skuser` the parent of each `<app>-users`.
- **Direct binding**: bind `skuser` to each baseline app, next to `<app>-users`.

Direct binding is chosen, for these reasons.

1. Authentik groups have a single `parent`. A baseline app needs `skuser` members to be members of
   `<app>-users`. That requires `skuser` to be a child of every `<app>-users` group, which a single
   parent field cannot express for N apps. The opposite direction (`skuser` as the parent) is the one
   expressible, but in Authentik a child group's members inherit the parent group's membership and
   roles, so it would make every `<app>-users` member a `skuser` member, which silently grants the
   whole bundle to a person added for one app. That breaks "per-app only" provisioning. (This inheritance
   direction is from the product's group documentation as understood by the author and MUST be
   re-verified on the pinned Authentik version in phase 0; the design does not depend on it, which is
   the point of choosing direct bindings.)
2. For the same reason, `<app>-admins` must never have `skadmin` as its parent: that would promote an
   app admin to a global admin. The design therefore sets no parents. `skadmin` reaches every app by
   being bound on every app, not by hierarchy.
3. Direct bindings evaluate as plain "user is a member of this group" checks, are idempotent to
   create (look up by app and group, as the script does today), are visible in the admin UI's
   application "Policy / Group / User bindings" tab, and are trivial to audit: the conformance
   check reads bindings only.
4. Offboarding is predictable. Removing a person from `skuser` removes baseline access and leaves any
   explicit `<app>-users` access intact.

A parent link may still be added later as a documentation aid, behind an off-by-default knob, once the
phase 0 test shows which way inheritance flows on the pinned version. It is not part of any access path.

### 4.5 Group creation, bindings and the fail-closed rules in `authentik_portal_tile.py`

The script input changes from `group: <name>` to a resolved access object (the Ansible side computes it
once, see 4.8):

```json
{"slug": "skpulse", "name": "...", "launch_url": "...",
 "access": {"mode": "app-groups", "baseline": true,
            "admin_global": "skadmin", "user_global": "skuser",
            "admins": "skpulse-admins", "users": "skpulse-users",
            "extra": [], "legacy": ["authentik Admins"],
            "bindings": "additive", "provider": "none"}}
```

New behaviour, in this order. Any error stops before the next step:

1. **Validate**: mode in the allowed set; the group names match `^[A-Za-z0-9][A-Za-z0-9 _.-]*$`; the
   desired group set is non-empty unless the mode is `public`.
2. **Ensure groups** (`ensure_group`): look up by exact name, create when missing (`is_superuser: false`,
   no roles, no parent, an `attributes: {"sk-managed": true}` marker, never any members). The global
   groups are created on demand too, so a fresh instance does not need a manual step. Creating an
   override-named group is allowed; a typo therefore yields an empty group, which is fail-closed (only
   `skadmin` gets in) rather than the old hard failure. The script prints `group: created <name>` so a
   typo is visible in the deploy log, and the conformance check lists empty `sk-managed` groups.
3. **Ensure application**: as today. When the role declares `provider: proxy`, ensure a forward-auth
   single-application provider (see 4.7) and attach it to the embedded outpost, keeping the existing
   "keep a provider that already exists" behaviour for blueprint-owned OIDC providers.
4. **Add bindings** for every desired group (create when absent, `order` in a reserved band 10 to 19 so
   they are recognisable). Add before remove.
5. **Prune** (only when `bindings: authoritative`): delete group bindings on this application whose
   group is not in the desired set and not in an `adopt` list. Pruning refuses to run if the post-prune
   set would be empty or would no longer contain the global admin group, and it refuses on an
   application that carries user-level or policy bindings it does not understand unless told to
   (`prune_foreign: true`). Dry run (`--check`) prints the plan and changes nothing.
6. **Postcondition assert**: re-read the application's bindings; fail the run if the count is zero (for
   any mode, `public` included, which has its explicit policy binding) or, outside `public`, if the
   global admin group is not bound.

Existing instances start with `bindings: additive` (release N), so nothing is removed on upgrade. The
migration section moves them to `authoritative`.

Blueprint-owned applications (skflow, and skhub's OIDC application) keep their own binding. Their roles
declare `bindings: additive` permanently and an `adopt` list naming the blueprint's group, so the two
writers never fight; the tile script adds only the global admin group and, if configured, the baseline
group on top.

### 4.6 The `public` mode

An application with zero bindings must always mean a bug. So `public` does not mean "no binding"; it
binds one shared, named expression policy (`sk-public-allow`, body `return True`) that the script
creates idempotently. A conformance check can then treat any application with zero bindings as a
violation without a per-app allowlist, and `public` apps are listable and reviewable (`bindings` whose
policy is `sk-public-allow`). Roles that serve both a gated UI and public paths (status pages, webhooks)
should keep using separate routers with the bypass pattern from `sso-estate-plan.md`; `public` is for an
entire app that is intentionally open to any authenticated session on the portal. A render-time check
refuses `public` together with a forward-auth middleware default unless `SSO_PUBLIC_ACK: true` is set.

### 4.7 The catch-all, and who actually gates a request

Because only an application behind a provider gates anything (section 2.1), the design separates the
two layers:

- **Visibility layer** (every app): the tile's bindings, driven by `SSO_ACCESS` as above.
- **Enforcement layer** (forward-auth apps): the application of the provider that matches the request
  host.

For a forward-auth app the enforcement layer needs the right provider:

- `admin-only`: the catch-all is sufficient once its application is bound to the global admin group
  alone. The tile and the catch-all agree.
- `app-groups`, `all-users`, baseline: the catch-all would deny those users (it admits admins only) and
  therefore the tile would promise access the gate refuses. The role must declare `provider: proxy`; the
  script then ensures a per-host forward-auth single-application provider on the embedded outpost whose
  application carries the same bindings as the tile (one Application, both jobs). A render-time assertion
  fails the deploy if `SSO_ACCESS` is not `admin-only` for a role that routes through `authentik@file` and
  does not declare a provider. Phase 0 must confirm, against the pinned Authentik version, that the
  embedded outpost prefers a host-specific single-application provider over the domain provider for the
  same host; if it does not, the catch-all must be narrowed to the hosts it should serve instead.
- Native OIDC apps (skhub, skflow, skpatch-hosts and sksec-web in `oidc` mode, skrac, skmesh): the
  application behind the OIDC provider is the gate. The role (or its blueprint) must write at least the
  global admin binding; the tile script then reconciles the rest. This closes the gap in skrac and skmesh,
  whose provisioners write no binding today.

The catch-all itself is handled in sksso:

1. A new blueprint `sksso-access-baseline.yaml.j2` (always rendered once the feature is on) creates the
   global groups `skadmin` and `skuser` (authoritative for existence, never for membership).
2. `sksso.CATCHALL_APP_SLUG` names the existing hand-made catch-all application. A small ensure step
   (`ensure-access-baseline.py`, run from the sksso deploy or once by hand) binds it to `skadmin` only and
   prints what it changed. Unset, the step warns loudly that the catch-all is unmanaged and exits non-zero
   only under `sksso.REQUIRE_CATCHALL_BINDING: true`.
3. A later phase can adopt the catch-all provider and application into a blueprint (domain-level forward
   auth, adopted by name) so a rebuilt Authentik has it, bound to `skadmin`, from the start.

### 4.8 Resolving the declaration once: `sso_access_resolve.yml`

A new `v1/ansible/shared/tasks/sso_access_resolve.yml` takes the role's `SSO_*` keys and the sksso vault
and sets one fact, `sso_access`, with the mode, baseline flag, the group names, the bound list, and
derived strings:

- `sso_access.bound_groups` (list) for the tile script,
- `sso_access.oidc_admin_csv` and `sso_access.oidc_user_csv` for OIDC role mapping,
- `sso_access.forward_auth_ok` for the render-time assertions in 4.7.

Roles include it early (before env templates render), because OIDC knobs are rendered into env files.
`portal_tile` then carries `access: "{{ sso_access }}"` instead of `group:`.

### 4.9 OIDC role mapping defaults to the per-app groups

Every OIDC role-mapping knob keeps its name and its explicit-override behaviour, and gets a derived
default:

| Knob | New default |
|---|---|
| `OIDC_ADMIN_GROUPS` (sksec-web, skpatch-hosts) | `<global admin>,<app>-admins` as CSV (`skadmin,skpulse-admins` shape) |
| `OIDC_READ_ONLY_GROUPS` (sksec-web, skpatch-hosts) | `<app>-users`, plus `skuser` when the app is baseline; empty in `admin-only` mode |
| `OIDC_ADMIN_GROUP` (skrac, accepts a list in its template) | the same admin list as above |
| `OIDC_UNMATCHED_ROLE` | stays `deny` and is not overridable to a permissive value |
| skflow `skflow-operators` | stays the blueprint's group; mapped as `SSO_USERS_GROUP` for this app so the model is uniform |
| skhub `skhub-users` | the existing name already matches `<app>-users`; it stays `SSO_USERS_GROUP` |

Admin mapping never includes `skuser`; a read-only role is never granted to someone the Authentik
binding would have refused (the derived lists are subsets of the bound groups, and a render-time
assertion enforces `oidc groups subset of bound groups` unless the operator overrides both).

### 4.10 Test identities

Two fixture accounts, created by an idempotent script (`ensure-test-identities.sh`, in the same pattern as
`ensure-api-token.sh`, secrets on stdin) and kept out of production use:

- `sk-test-user`: member of `skuser` only. Never in `skadmin` or any `<app>-*` group.
- `sk-test-admin`: member of `skadmin` only.

Both are internal, non-superuser accounts with random vault-held credentials, in a group that is also
excluded from any interactive MFA stage for the harness's own login flow (an open question, see 9).

## 5. Migration for existing instances

Instances today have: tiles bound to `authentik Admins` (or, after PR #360, an extra `skadmin`
binding); a hand-made catch-all that may have zero bindings; provider apps with no bindings; no
`skuser`.

Step 0, read only. Run the conformance check (6) and keep its output. It lists every application with
zero bindings and every application bound to `authentik Admins`.

Step 1, additive (framework release N, safe on upgrade):

1. sksso deploy creates `skadmin` and `skuser` if missing (membership untouched; existing `skadmin`
   members are kept).
2. Bind the catch-all application to `skadmin` only. This is the one step that can lock people out:
   before it, take the 5 minute check in 6.3 (list who relies on forward-auth admin tools and confirm
   they are in `skadmin`), and keep a break-glass admin session open while applying.
3. Redeploy each role. Each app gets the global admin binding plus whatever `SSO_ACCESS` declares.
   `authentik Admins` bindings stay (`bindings: additive`).
4. Apps with no bindings now have at least the global admin binding.

Step 2, curate: decide the baseline list (`sksso.BASELINE_APPS`) and per-app modes, add people to
`skuser` or to `<app>-users` groups, run the gate tests (7).

Step 3, authoritative (release N+1 or later, per instance, one app at a time first): set
`bindings: authoritative` (or the sksso-wide `sksso.BINDINGS_AUTHORITATIVE: true`). The first run with
`--check` prints the bindings that would be removed (typically `authentik Admins`); apply when the plan is
only legacy groups. `authentik Admins` remains Authentik's administration group and the automation
service account stays in it.

Rollback: re-add a binding in the admin UI or set `bindings: additive` and redeploy; removing the
catch-all binding restores the old open behaviour (not recommended). Nothing in step 1 deletes anything.

## 6. Conformance check (spec)

`v1/ansible/optional/sksso/src/sksso/check-access.py`, stdlib only, same transport as the tile script
(API token on stdin, run in the throwaway container). Read-only. Output one line per finding, exit 1 on
any violation.

6.1 Rules, in order of severity:

1. `UNBOUND`: an application with zero bindings (the audit's failure). Applies to every application
   including the catch-all and provider apps.
2. `NO-ADMIN`: an application not in `public` mode whose bindings do not include the global admin group.
3. `LEGACY`: an application bound to `authentik Admins` (or any group in `--legacy`). Warning until
   authoritative mode, error after (`--strict`).
4. `PUBLIC`: lists applications bound to `sk-public-allow` for review (info).
5. `SKUSER-ADMIN`: any application whose admin-capable role mapping (OIDC admin groups) includes `skuser`,
   and any `<app>-admins` group that is a member of the baseline bundle.
6. `BASELINE-DRIFT`: an application bound to `skuser` that is not in `sksso.BASELINE_APPS`, or the reverse.
7. `EMPTY-GROUP`: a `sk-managed` group with no members (info; flags typos).
8. `NO-PROVIDER`: a forward-auth application that routes through `authentik@file` with a binding set wider
   than admin-only but no host-specific provider (reads provider and outpost membership).

6.2 Inputs: Authentik base URL and token, optional `--expect expect.json` (slug to mode and baseline, as
rendered by the roles), `--legacy "authentik Admins"`, `--strict`.

6.3 It is also the pre-flight for step 1 of the migration: with `--who-passes <slug>` it prints, for one
application, the users that would pass its current bindings, so the catch-all change is a reviewed list.

## 7. Gate harness tests

Two layers, both runnable on an instance after any deploy and in CI against a throwaway Authentik.

Layer A, policy check without a browser. For every application and each test identity, call
`GET /api/v3/core/applications/<slug>/check_access/?for_user=<pk>` (superuser token required).

| Identity | Expectation |
|---|---|
| `sk-test-admin` | pass on every application, `public` ones included |
| `sk-test-user` | pass on `all-users` and baseline applications; fail on `admin-only`, `app-groups` apps it has no group for, and on the forward-auth catch-all and every admin UI (skfenceha dashboard, hypervisor, remote-control, update dashboard) |

Layer B, live forward-auth probe through the edge for a handful of representative hosts: one
`admin-only` forward-auth tool, one baseline app, one `app-groups` app with its own provider, one native
OIDC app. Log in through the flow executor API as each identity, then `GET` the app host and assert:
anonymous gets a redirect to the login; `sk-test-user` gets 403 on `admin-only`, success on baseline;
`sk-test-admin` succeeds everywhere. The probe also asserts the OIDC role result for the two OIDC apps
(admin role for `sk-test-admin`; no role or read-only for `sk-test-user`; never admin).

Framework unit tests (no network, same style as `v1/tests/test_authentik_portal_tile.py`, mocked API):
group creation is idempotent (second run says unchanged); every mode produces the table in 4.2; an
application never ends with zero bindings, including when group creation fails; prune refuses an empty or
adminless result; `skuser` is never bound on a non-baseline app and never appears in an admin mapping;
`all-users` with `SSO_BASELINE: false` is rejected; `AUTHENTIK_TILE_GROUP` stays additive; a render test
asserts `OIDC_*_GROUPS` defaults are a subset of the bound groups and that forward-auth plus a non-admin
mode without `provider: proxy` fails the render. A lint test (extending `test_portal_tile_lint.py`) fails
a role that has a tile but no `SSO_ACCESS` handling.

## 8. Implementation plan

Phases are independent PRs. Sizes are rough (S under a day, M a day or two, L several days).

| Phase | Content | Files | Size |
|---|---|---|---|
| 0 | Verify on the pinned Authentik: (a) group parent inheritance direction, (b) host-specific single provider beats the domain provider on the embedded outpost, (c) `check_access` with `for_user`. Write results into this doc. | this document only | S |
| 1 | Resolver and tile script: `sso_access_resolve.yml`, `ensure_group`, multi-binding, `sk-public-allow`, postcondition assert, additive default, tests. Roles unchanged except through shared code and a compatibility shim for `AUTHENTIK_TILE_GROUP`. | `shared/tasks/authentik_portal_tile.yml`, `shared/tasks/sso_access_resolve.yml`, `shared/src/authentik_portal_tile.py`, `v1/tests/test_authentik_portal_tile.py`, `v1/tests/test_portal_tile_lint.py` | M |
| 2 | sksso baseline: global groups blueprint, catch-all ensure step, `BASELINE_APPS`, test identities script, `ensure-api-token.sh` untouched. | `optional/sksso/src/config/sksso/blueprints/sksso-access-baseline.yaml.j2`, `optional/sksso/tasks/`, `optional/sksso/src/sksso/ensure-access-baseline.py`, `.../ensure-test-identities.sh`, sksso deploy files and README | M |
| 3 | Roles adopt the block: 29 tile roles x 3 envs pass `access:` instead of `group:`; OIDC defaults in sksec-web, skpatch-hosts, skrac; `skrac` and `skmesh` provisioners write bindings; blueprints stop owning bindings that the tile script now manages (skflow keeps `adopt`). Mostly mechanical, generated edit. | `optional/*/deploy_*-{dev,staging,prod}.yml`, `core/skfence*/deploy_*`, `optional/skrac/src/skrac/authentik_provision.py`, `optional/skmesh/src/skmesh/authentik_provision.py`, role READMEs and SOPs, `docs/ROLE_AUTHORING_CHECKLIST.md` | L |
| 4 | Conformance check script and its unit tests, wired into the sksso deploy as a non-fatal report. | `optional/sksso/src/sksso/check-access.py`, `v1/tests/test_sksso_check_access.py` | M |
| 5 | Gate harness layers A and B and the CI job against a throwaway Authentik. | `v1/tests/gate/` (new), CI workflow | M |
| 6 | Authoritative mode and pruning, default flipped in a later release with upgrade notes; optional adoption of the catch-all provider into a blueprint. | tile script, sksso blueprint, CHANGELOG | M |

Docs to update in phase 3: `v1/docs/sso-estate-plan.md` ("Groups and roles" already proposes
`<app>-users`, `<app>-admins`; align names with the standard), `v1/docs/sso-user-management.md`
("Claims and groups", "Add a person"), skfetch README (catch-all warning), sksec-web, skpatch-hosts,
skpulse READMEs.

## 9. Risks and open questions

- **Lockout when the catch-all is bound to `skadmin`.** Anyone relying on the open catch-all today (the
  community users it wrongly admits, but also legitimate non-admin operators) is cut off. Mitigation:
  `--who-passes` review, additive rollout, break-glass session, apply per environment.
- **Provider precedence on the embedded outpost** (4.7) is unverified. If wrong, per-app forward-auth
  providers do nothing and the design falls back to narrowing the catch-all by host.
- **Group parent semantics** (4.4): avoided by design, but must be verified before anyone adds a parent
  link later.
- **Blueprint versus script ownership** of the same binding (skflow, skhub, the two OIDC blueprints): two
  writers can flap. The `adopt` list and additive mode keep them apart; the conformance check reports
  drift.
- **Auto-created groups hide typos.** A mistyped override creates an empty group; it is fail-closed but
  silent. The deploy log line and `EMPTY-GROUP` mitigate; a stricter `SSO_GROUPS_CREATE: false` mode can
  restore the old hard failure.
- **Test identities in production.** They are low-privilege but real accounts. Keep credentials in the
  vault, disable them between runs where the estate prefers, and exclude them from user-facing lists. MFA
  handling for the harness login flow needs a decision (dedicated flow or a policy exemption).
- **Standard alignment.** Group names, the mode names and the `public` mechanism follow the companion
  standard once it merges; this design keeps them behind variables so the rename is data, not code.
- **Scope of "fail-closed" for apps with their own login.** For an app whose router has no forward auth,
  `SSO_ACCESS` governs portal visibility only; the app's own login still decides entry. The design does not
  claim more.
