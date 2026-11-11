# SSO app access provisioning: per-app groups, a baseline bundle, and fail-closed access

Status: design proposal (docs only, no role or template code changes in this PR).
Normative rules: `standards/APPLICATION_ACCESS_SSO_STANDARD.md` in the sk-standards repo (PROPOSED,
sk-standards PR #70). This document is the SKStacks implementation design for that standard and
follows its names exactly: global groups `skadmin` and `skuser`, per-app groups `app-<app>-admins` and
`app-<app>-users`, modes `admin-only` (the default), `app-groups` (with the `in_baseline` flag, variable
`SSO_BASELINE`), `any-account` and `public`, conformance checks C1 to C10, and test accounts
`sktest-user` and `sktest-admin`. Where this text and the standard disagree, the standard wins.
Related here: `v1/docs/sso-estate-plan.md` (patterns per service), `v1/ansible/optional/sksso/README.md`
("Application access and group model"), open PR #360 (portal tile and OIDC admin defaults move from
`authentik Admins` to `skadmin`).

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
   app that should be reachable by `app-<app>-users` but sits behind the catch-all is denied (or, with an
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
example `skpulse:`), all optional. It is the SKStacks spelling of the standard's `access:` block
(standard section 6):

```yaml
skpulse:
  SSO_ACCESS: admin-only      # admin-only | app-groups | any-account | public   (default admin-only)
  SSO_BASELINE: false         # the standard's in_baseline: true also binds skuser (app-groups only)
  SSO_PROVIDER: own           # catch-all | own. Default: catch-all for admin-only, own for every other mode
  SSO_ADMINS_GROUP: ""        # default "app-<slug>-admins"
  SSO_USERS_GROUP: ""         # default "app-<slug>-users"
  SSO_EXEMPTION:              # required for any-account and public
    reason: ""
    owner: ""
    approved: ""              # date
    review_by: ""             # date, at most 12 months after approved
```

Global group names (`skadmin`, `skuser`) are sksso-level settings (`sksso.ADMIN_GROUP`,
`sksso.USER_GROUP`, defaults per the standard), not per-role knobs, so no role can quietly bind a
different "global" group.

Modes and the binding set each one produces, always with policy engine mode `any` (`G` = `skadmin`,
`A` = `app-<app>-admins`, `U` = `app-<app>-users`, `S` = `skuser`):

| `SSO_ACCESS` | `SSO_BASELINE` | Bound | Meaning |
|---|---|---|---|
| `admin-only` (default) | must be false | `G` | Only estate administrators. The only mode the catch-all serves. |
| `app-groups` | false (default) | `G`, `A`, `U` | Admins, app admins, app users. |
| `app-groups` | true | `G`, `A`, `U`, `S` | Same, plus the baseline bundle. Admin role is still mapped only from `G` and `A`. |
| `any-account` | n/a | the named policy `sso-allow-any-account` | Any authenticated account. Reviewed opt-out, needs an exemption record. |
| `public` | n/a | the named policy `sso-allow-public` | No IdP gate. Reviewed opt-out, needs an exemption record. |

There is no "every estate user" mode. That is `app-groups` with `SSO_BASELINE: true`, so an account
outside `skuser` (a community account, a service account) is excluded. `any-account` is the opposite,
deliberate widening, for apps that do their own authorization.

The two groups `A` and `U` are created for EVERY app, including `admin-only` ones, so provisioning is
ready (standard section 8.1). In `admin-only` mode they have no effect; members of them are an
ineffective grant (check C9).

Compatibility: an explicit `AUTHENTIK_TILE_GROUP` keeps working for one release. It is bound additively and
logs a deprecation line, so an instance that pins `authentik Admins` today loses nothing, but the standard
treats that binding as a defect (check C2), so the conformance check reports it until migration step 4.
`SSO_PROVIDER: catch-all` on any mode other than `admin-only` is rejected at render. The roles' defaults for `AUTHENTIK_TILE_GROUP` become empty, and the standard's rule applies: the tile is bound to the same
groups as the gate, so `AUTHENTIK_TILE_GROUP` is no longer a value for every mode.

### 4.3 The estate baseline list

The baseline set has two sources, and the estate wins:

1. The role's `SSO_BASELINE` (default false).
2. `sksso.BASELINE_APPS` in sksso's vault, a list of application slugs. When the key is defined (even
   as an empty list) it replaces every role's `SSO_BASELINE`: an app is baseline if and only if its slug
   is in the list. When undefined, role values apply.

sksso's vault is the right home because the tile task already loads it (for `sksso.api_token`) on every
run, so no new lookup is needed, and one estate-wide list is reviewable in one place. A role that wants
to be in the bundle by default sets `SSO_BASELINE: true` in its role defaults file; the framework itself
ships none of them true (the default bundle is empty until an estate chooses). Every baseline app
is listed in each access review (standard section 8.4).

### 4.4 How skuser is bound: directly, not as a parent group

Two ways to make `skuser` members reach a baseline app were considered.

- **Parent group**: make `skuser` the parent of each `app-<app>-users`.
- **Direct binding**: bind `skuser` to each baseline app, next to `app-<app>-users`.

Direct binding is chosen, for these reasons.

1. Authentik groups have a single `parent`. A baseline app needs `skuser` members to be members of
   `app-<app>-users`. That requires `skuser` to be a child of every `app-<app>-users` group, which a single
   parent field cannot express for N apps. The opposite direction (`skuser` as the parent) is the one
   expressible, but in Authentik a child group's members inherit the parent group's membership and
   roles, so it would make every `app-<app>-users` member a `skuser` member, which silently grants the
   whole bundle to a person added for one app. That breaks "per-app only" provisioning. (This inheritance
   direction is from the product's group documentation as understood by the author and MUST be
   re-verified on the pinned Authentik version in phase 0; the design does not depend on it, which is
   the point of choosing direct bindings.)
2. For the same reason, `app-<app>-admins` must never have `skadmin` as its parent: that would promote an
   app admin to a global admin. The design therefore sets no parents. `skadmin` reaches every app by
   being bound on every app, not by hierarchy.
3. Direct bindings evaluate as plain "user is a member of this group" checks, are idempotent to
   create (look up by app and group, as the script does today), are visible in the admin UI's
   application "Policy / Group / User bindings" tab, and are trivial to audit: the conformance
   check reads bindings only.
4. Offboarding is predictable. Removing a person from `skuser` removes baseline access and leaves any
   explicit `app-<app>-users` access intact.

The standard (section 3.3) reaches the same conclusion. Parents may be used for cosmetic organisation but
never carry access; no knob in this design uses a parent for access. The standard's check C6 tests the
actual outcome rather than the model, which covers the release-dependent inheritance behaviour.

### 4.5 Group creation, bindings and the fail-closed rules in `authentik_portal_tile.py`

The script input changes from `group: <name>` to a resolved access object (the Ansible side computes it
once, see 4.8):

```json
{"slug": "skpulse", "name": "...", "launch_url": "...",
 "access": {"mode": "app-groups", "in_baseline": true,
            "admin_global": "skadmin", "user_global": "skuser",
            "admins": "app-skpulse-admins", "users": "app-skpulse-users",
            "extra": [], "legacy": ["authentik Admins"],
            "bindings": "additive", "provider": "none"}}
```

New behaviour, in this order. Any error stops before the next step:

1. **Validate**: mode in the allowed set; the group names match `^[A-Za-z0-9][A-Za-z0-9 _.-]*$`; the
   desired group set is non-empty unless the mode is `any-account` or `public`; `any-account` and `public` carry a valid, unexpired exemption (review date within 12 months).
2. **Ensure groups** (`ensure_group`), for every app regardless of mode,: look up by exact name, create when missing (`is_superuser: false`,
   no roles, no parent, an `attributes: {"sk-managed": true}` marker, never any members). The global
   groups are created on demand too, so a fresh instance does not need a manual step. Creating an
   override-named group is allowed; a typo therefore yields an empty group, which is fail-closed (only
   `skadmin` gets in) rather than the old hard failure. The script prints `group: created <name>` so a
   typo is visible in the deploy log, and the conformance check lists empty `sk-managed` groups.
3. **Ensure application**: as today. When the resolved provider is `own` (any mode except `admin-only`), ensure a forward-auth
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
   any mode, `any-account` and `public` included, which have their explicit policy binding) or, outside `any-account` and `public`, if the
   global admin group is not bound.

Existing instances start with `bindings: additive` (release N), so nothing is removed on upgrade. The
migration section moves them to `authoritative`.

Blueprint-owned applications (skflow, and skhub's OIDC application) keep their own binding. Their roles
declare `bindings: additive` permanently and an `adopt` list naming the blueprint's group, so the two
writers never fight; the tile script adds only the global admin group and, if configured, the baseline
group on top. Their blueprint group names move to the per-app names (`app-skflow-users`,
`app-skhub-users`) in phase 3, by the rename migration in section 5.1; there is no legacy exception.

### 4.6 The `any-account` and `public` modes

An application with zero bindings must always mean a bug (check C1). So the two widening modes do not
mean "no binding"; each binds one shared, named expression policy that the script creates idempotently
(body `return True`): `sso-allow-any-account` for `any-account`, `sso-allow-public` for `public`. Apps in
either mode are listable by that policy and reviewable. Each needs an exemption record (reason, owner,
approval date, review date within 12 months); the render fails without one, and the conformance check
(C6) fails on an expired one. Approval is an ITIL change (standard section 5).

Roles that serve both a gated UI and public paths (status pages, webhooks) keep separate routers with the
bypass pattern from `sso-estate-plan.md`; `public` is for a whole app that is intentionally open. A render
check refuses `public` on a router that still carries a forward-auth middleware default unless the
exemption record is present, and public-by-design federation endpoints stay outside this model entirely
(standard section 5).

### 4.7 The catch-all, and who actually gates a request

Because only an application behind a provider gates anything (section 2.1), the design separates the
two layers:

- **Visibility layer** (every app): the tile's bindings, driven by `SSO_ACCESS` as above.
- **Enforcement layer** (forward-auth apps): the application of the provider that matches the request
  host.

For a forward-auth app the enforcement layer needs the right provider:

- `admin-only`: the catch-all is sufficient once its application is bound to the global admin group
  alone. The tile and the catch-all agree.
- Every other mode (`app-groups` baseline or not, `any-account`, `public`): the catch-all admits admins only,
  so a tile would promise access the gate refuses. Decision: any app that is not `admin-only` gets its own
  provider by default (`SSO_PROVIDER: own`); the catch-all serves `admin-only` apps only. The script
  ensures a per-host forward-auth single-application provider on the embedded outpost whose application
  carries the same bindings as the tile (one Application, both jobs). A render-time assertion fails the
  deploy if a role routes through `authentik@file` with a mode other than `admin-only` and
  `SSO_PROVIDER: catch-all` (standard check C7). Phase 0 must still confirm, against the pinned Authentik
  version, that the embedded outpost prefers a host-specific single-application provider over the domain
  provider for the same host; if it does not, the catch-all must be narrowed to the hosts it serves.
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
| `OIDC_ADMIN_GROUPS` (sksec-web, skpatch-hosts) | `<global admin>,app-<app>-admins` as CSV (`skadmin,app-skpulse-admins` shape) |
| `OIDC_READ_ONLY_GROUPS` (sksec-web, skpatch-hosts) | `app-<app>-users`, plus `skuser` when the app is baseline; empty in `admin-only` mode |
| `OIDC_ADMIN_GROUP` (skrac, accepts a list in its template) | the same admin list as above |
| `OIDC_UNMATCHED_ROLE` | stays `deny` and is not overridable to a permissive value |
| skflow `skflow-operators` | renamed to `app-skflow-users` (Windmill's regular operators; admins map from `skadmin` and `app-skflow-admins`). The blueprint's group name changes with it. Windmill's 10 SSO user limit is unchanged. |
| skhub `skhub-users` | renamed to `app-skhub-users` (admins from `skadmin` and `app-skhub-admins`). Nextcloud-side handling is in section 5.1. |

Admin mapping never includes `skuser` and never `authentik Admins` (check C2); because `skadmin` is bound directly, it is listed in the admin mapping explicitly or an estate administrator would be a plain user inside the app; a read-only role is never granted to someone the Authentik
binding would have refused (the derived lists are subsets of the bound groups, and a render-time
assertion enforces `oidc groups subset of bound groups` unless the operator overrides both).

### 4.10 Test identities: a closed loop for per-stack testing

Decision: test identities bypass interactive MFA, scoped to members of one dedicated group, `sktest`, and
nothing else. They still go through the application access policy, so a probe tests the real gate. A fixed
set per estate (account names are variables, defaults shown; the standard's section 8.5 defines the first two):

| Account | Variable | Standing groups | Role in the loop |
|---|---|---|---|
| `sktest-admin` | `sksso.TEST_ADMIN_NAME` | `skadmin` | Proves allowance everywhere the admin group is meant to reach. |
| `sktest-user` | `sksso.TEST_USER_NAME` | `skuser` | Proves the baseline: allowed on baseline apps, refused elsewhere and on admin UIs. |
| `sktest-nogroup` | `sksso.TEST_NOGROUP_NAME` | none | Proves nobody-by-default: refused on every app that is not `any-account` or `public`. |
| `sktest-appadmin` | `sksso.TEST_APPADMIN_NAME` | none (temporary `app-<app>-admins` only) | Proves the per-app admin path and its isolation from other apps. |
| `sktest-appuser` | `sksso.TEST_APPUSER_NAME` | none (temporary `app-<app>-users` only) | Proves the per-app user path. |
| `sksvc-test` | `sksso.TEST_SVC_NAME` | none | Service account with an API token: the machine-client identity. Proves a token holder with no group is refused by gates and has no admin API rights. |

All six are members of the `sktest` group and of nothing that grants access. `sktest` itself is bound to no
application, is never a gate and is never an admin mapping input.

**Credentials.** Per-estate vault keys in sksso's vault (the same file that holds `sksso.api_token`):
`sksso.TEST_ADMIN_PASSWORD`, `sksso.TEST_USER_PASSWORD`, `sksso.TEST_NOGROUP_PASSWORD`,
`sksso.TEST_APPADMIN_PASSWORD`, `sksso.TEST_APPUSER_PASSWORD` and `sksso.TEST_SVC_API_TOKEN` (32+ chars each),
each generated per estate and rotated on the schedule of other privileged credentials. Nothing goes in a
repository, a unit file or an echoed CI variable.

**Scoped MFA bypass (sksso baseline blueprint plan).** `sksso-access-baseline.yaml.j2` grows, besides the
global groups and the two named policies:

1. the group `sktest` (existence only, no members written by the blueprint);
2. a policy binding on the authenticator-validation stage binding of the estate's authentication flow, group
   `sktest`, `negate: true`, so for a member the stage binding fails its policy and the stage is skipped and
   for everyone else it runs unchanged. It touches only that one stage binding. It is not a flow-wide
   exemption, it does not alter any application binding, and the account still needs a valid password and
   must pass the target application's bindings.

The blueprint is applied only when the adopting estate opts in (`sksso.TEST_IDENTITIES_ENABLED: true`), so an
estate without a probe has no bypass. The conformance check gets one extra rule, X1: `sktest` has members
other than the six fixed accounts, a fixed account holds a standing group other than its table row, or
`sktest` is bound to any application.

**Provisioning script.** `ensure-test-identities.sh` (same pattern as `ensure-api-token.sh`, secrets on
stdin, idempotent) creates or adopts the six accounts, sets or rotates their credentials, places them in
`sktest` and asserts the standing memberships in the table. It fails if an account holds any other group
(except a temporary `app-*` membership, which the next script manages).

**Membership tooling (temporary app membership).** `sktest-appadmin` and `sktest-appuser` hold no standing
group. For one test run, a wrapper adds them to the target stack's groups and always removes them:

- `optional/sksso/src/sksso/test-membership.py` (phase 2). A library function `with_test_membership(app)`,
  a context manager that adds `sktest-appadmin` to `app-<app>-admins` and `sktest-appuser` to
  `app-<app>-users`, yields, and removes both in `finally` (also on SIGINT and SIGTERM). A CLI form,
  `test-membership.py --app <slug> -- <command>`, runs the command inside the same guarantee; its exit code
  is the command's, and a failed removal exits non-zero and loud.
- `optional/sksso/tasks/test_membership.yml` (phase 2), the Ansible form for deploy-time probes: a `block`
  that adds the memberships, runs the probe tasks, and an `always` section that removes them, so an
  interrupted or failed play still cleans up.
- A leak sweep, `test-membership.py --sweep`, removes both accounts from every `app-*` group, and exits 1 if
  it found any (a leak). It runs at harness start, nightly and as check X1's data source. Because an app
  in `admin-only` mode with members is an ineffective grant (C9), a leaked test membership also surfaces
  there.
- Membership changes use the sksso automation token (as `ensure-api-token.sh` provisions). `sksvc-test` has
  no groups and therefore no such rights by design.

**Headless login.** The harness logs in through the flow executor with the vault credentials, as a browser
would: `GET /api/v3/flows/executor/<authentication flow slug>/`, then `POST` the identification stage
(username), then the password stage, following `component` hints until the flow answers with a redirect.
The resulting session cookie is then used against the app host (forward-auth apps: the outpost start and
callback redirects are followed; OIDC apps: the authorization code flow is followed headless). Because the
MFA stage is skipped only for `sktest` members, the same code path is the one a real user takes minus the
second factor. `sksvc-test` authenticates with its token as a bearer credential instead.

**Per-stack probe recipe.** For a target stack `<app>`, run inside `with_test_membership(<app>)`. The
expectations that depend on the declared mode are in section 7; the four always-on assertions are:

| # | Account | Request | Expected |
|---|---|---|---|
| 1 | `sktest-admin` | the app, and its admin UI | reaches both (the control that makes every refusal below meaningful) |
| 2 | `sktest-appuser` | the app | reaches it as a user (2xx), and for OIDC apps holds no admin role |
| 3 | `sktest-nogroup` | the app | refused (no 2xx content from the app) |
| 4 | `sktest-appuser` | the app's admin UI | refused |

Extra assertions run in the same loop: `sktest-appadmin` reaches the app's admin UI and is refused on a
different app's admin UI (isolation); `sktest-user` is allowed on baseline apps and refused on non-baseline
ones; `sksvc-test` with a bearer token is refused by the gates and its API token gets 403 on the group and
application admin endpoints. A refusal only counts when assertion 1 passed on the same app in the same run.
Because this loop needs only the stack under test plus sksso, it runs per stack in CI, in a RAD or dev
cycle on a laptop-sized instance, and on every deploy.

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
`skuser` (baseline) or to `app-<app>-users` groups (a la carte); both are first-class and neither implies the other, run the gate tests (7).

Step 3, authoritative (release N+1 or later, per instance, one app at a time first): set
`bindings: authoritative` (or the sksso-wide `sksso.BINDINGS_AUTHORITATIVE: true`). The first run with
`--check` prints the bindings that would be removed (typically `authentik Admins`); apply when the plan is
only legacy groups. `authentik Admins` remains Authentik's administration group and the automation
service account stays in it.

### 5.1 Renaming `skhub-users` and `skflow-operators` (fail forward)

Decision: no legacy exception and no long-lived dual naming. Rename in place, per app, in a maintenance
window, and carry on forward. Authentik bindings reference a group by id, so renaming the group keeps the
application bindings (and the blueprint-created binding) intact.

For `skhub-users` to `app-skhub-users`:

1. **Rename the IdP group in place** (admin UI or `ak shell`: change `Group.name`; do not create a new group
   and copy members). Bindings follow by id. Also create `app-skhub-admins` if it does not exist.
2. **Update the role vars and blueprint** that name it: `SSO_USERS_GROUP` (drop any override so the derived
   default `app-skhub-users` applies), the skhub vault, and the documentation text. Redeploy skhub so the
   tile and bindings reconcile.
3. **Update Nextcloud's OIDC group mapping.** Authentik's `nextcloud` scope mapping emits only `nc-*` groups
   as `nextcloud_groups`, so a pure gate group is not synced and nothing in Nextcloud changes. If an estate
   has widened the mapping or `user_oidc` group provisioning to include the gate group, then Nextcloud holds
   a group literally named `skhub-users` (a Nextcloud group id cannot be renamed, shares and group folders
   reference it). Two ways, pick one per estate:
   - **Alias (no share work):** keep the Nextcloud-side group id and have the scope mapping translate
     `app-skhub-users` to `skhub-users` for Nextcloud only. The IdP name is clean; the legacy id stays inside
     Nextcloud. Recorded as an explicit Nextcloud-side name, not an IdP exception.
   - **Migrate:** let provisioning create `app-skhub-users` in Nextcloud, re-share each share and group folder
     from `skhub-users` to the new group (`occ` and the sharing API), verify, then remove the old Nextcloud
     group.
   Then check `occ user_oidc:provider` for the group claim and mapping settings, and confirm a real login
   keeps exactly one account and the expected groups.
4. **Probe** (section 4.10 recipe) and run the conformance check: no binding or role var names the old group.

For `skflow-operators` to `app-skflow-users`: rename the group in place; change the blueprint
(`sksso_skflow_group`) and its role var to the new name so the blueprint's `!Find`/identifier matches the
renamed group instead of creating a second one, and redeploy sksso then skflow. Windmill maps operators by
group in its `oauths` setting, so update the group name there if it is listed (the skflow play writes it).
Re-run the probe. Members need no change.

Rollback: rename the group back and revert the role vars; nothing is deleted. Do the blueprint and the
rename in the same window, because a blueprint that still names the old group will recreate it.

Rollback of the earlier steps: re-add a binding in the admin UI or set `bindings: additive` and redeploy; removing the
catch-all binding restores the old open behaviour (not recommended). Nothing in step 1 deletes anything.

## 6. Conformance check (spec)

`v1/ansible/optional/sksso/src/sksso/check-access.py`, stdlib only, same transport as the tile script
(API token on stdin, run in the throwaway container). Read-only. Output one line per finding, exit 1 on
any violation.

This is the SKStacks implementation of the standard's `sso-access-check` (standard section 9.1): same
exit codes (0 conforms, 1 at least one violation, one line per violation `<slug> <check> <detail>`, 2 the
check could not run, including zero applications returned or an unreachable API; a check that saw nothing
never reports clean) and the same check ids. Pagination is followed to the end.

6.1 Checks. The standard's ids are authoritative; the table maps them to what this design adds:

| Id | Fails when (standard) | SKStacks notes |
|---|---|---|
| C1 | zero enabled bindings | The audit's headline failure. Covers the catch-all, provider apps, and the skrac and skmesh applications. |
| C2 | any binding, tile group or OIDC role map names `authentik Admins` | Reports the legacy bindings that additive mode leaves behind; warning until migration step 4 (`--strict` makes it an error). Also scans `OIDC_ADMIN_GROUPS` and friends as rendered. |
| C3 | binding to a single user | |
| C4 | only binding disabled, or a negated gate binding | |
| C5 | multi-group gate with engine mode `all` | The tile script always writes `any`. |
| C6 | bindings differ from what the declared mode derives, or `any-account`/`public` lacks a valid exemption | Includes: `skadmin` missing; `skuser` bound on a non-baseline app or missing on a baseline one (my earlier NO-ADMIN and BASELINE-DRIFT); `skuser` in an admin role map; an app admin group inside `skuser`. |
| C7 | app not `admin-only` but on the catch-all provider | My earlier NO-PROVIDER. Reads provider and outpost membership. |
| C8 | tile groups differ from gate groups | |
| C9 | `app-*` group has members while the app is `admin-only`, or an app has no declared `access` block | Ineffective grants. Also reports empty `sk-managed` groups as info, which flags typos in group name overrides (my earlier EMPTY-GROUP). |
| C10 | a required group is missing, or an `app-*` group exists for no declared app | Includes `skadmin`, `skuser`. |

Overlap summary: UNBOUND is C1, LEGACY is C2, NO-ADMIN and BASELINE-DRIFT and SKUSER-ADMIN and the
public/exemption review are parts of C6, NO-PROVIDER is C7, EMPTY-GROUP is C9 (info) and C10.

6.2 Inputs: Authentik base URL and a read-only token, `--declarations <path>` (slug to mode, in_baseline and
exemption, as rendered by the roles; the standard's input), `--strict`. Ships with a negative control
(scratch IdP or recorded response with an unbound app, proving exit 1), per the standard's reference to the
testing standard; a checker never seen to fail is not evidence.

6.3 It is also the pre-flight for step 1 of the migration: with `--who-passes <slug>` it prints, for one
application, the users that would pass its current bindings, so the catch-all change is a reviewed list.

## 7. Gate harness tests

Two layers, both runnable on an instance after any deploy and in CI against a throwaway Authentik.

The harness is the SKStacks implementation of the standard's probe (section 9.3). Expectations derive from the
declared mode, with the user-level account being skuser-only:

| Mode | `sktest-admin` | `sktest-user` |
|---|---|---|
| `admin-only` | reaches | refused |
| `app-groups`, not baseline | reaches | refused |
| `app-groups`, baseline | reaches, as admin | reaches, as a user, NOT admin |
| `any-account` | reaches | reaches |
| `public` | reaches | reaches without login |

A refusal only counts when `sktest-admin` reached the same app in the same run (a down app must not pass).
"Reached" is a 2xx from the app itself; "refused" is anything else (401, 403, redirect to login or denied).
The extra probe the standard allows (temporary `app-<app>-users` membership for `sktest-user` during the gate,
removed after) covers the a la carte path.

Layer A, policy check without a browser: for every app and both test accounts, call
`GET /api/v3/core/applications/<slug>/check_access/?for_user=<pk>` (superuser token) and compare with the table.
It also proves an admin-UI refusal for `sktest-user` on every `admin-only` app (skfenceha dashboard, hypervisor
console, remote-control tool, update dashboard, and the catch-all).

Layer B, live forward-auth probe through the edge for representative hosts: one `admin-only` tool, one baseline
app, one non-baseline `app-groups` app with its own provider, one native OIDC app. It authenticates through the
IdP's own flow. It also asserts the OIDC role result for the two OIDC apps: admin role for `sktest-admin`, a user
or no role for `sktest-user`, never admin.

Framework unit tests (no network, same style as `v1/tests/test_authentik_portal_tile.py`, mocked API):
group creation is idempotent (second run says unchanged); every mode produces the table in 4.2; an
application never ends with zero bindings, including when group creation fails; prune refuses an empty or
adminless result; `skuser` is never bound on a non-baseline app and never appears in an admin mapping;
`SSO_BASELINE: true` on `admin-only` is rejected; `any-account`/`public` without an exemption, or with a review date over 12 months out, fail the render; `AUTHENTIK_TILE_GROUP` stays additive; a render test
asserts `OIDC_*_GROUPS` defaults are a subset of the bound groups and that forward-auth plus a non-admin
mode with `SSO_PROVIDER: catch-all` fails the render. A lint test (extending `test_portal_tile_lint.py`) fails
a role that has a tile but no `SSO_ACCESS` handling.

## 8. Implementation plan

Phases are independent PRs. Sizes are rough (S under a day, M a day or two, L several days).

| Phase | Content | Files | Size |
|---|---|---|---|
| 0 | Verify on the pinned Authentik: (a) group parent inheritance direction (the standard already forbids parents carrying access; this only confirms why), (b) host-specific single provider beats the domain provider on the embedded outpost, (c) `check_access` with `for_user`. Write results into this doc. | this document only | S |
| 1 | Resolver and tile script: `sso_access_resolve.yml`, `ensure_group`, multi-binding, `sso-allow-public`, postcondition assert, additive default, tests. Roles unchanged except through shared code and a compatibility shim for `AUTHENTIK_TILE_GROUP`. | `shared/tasks/authentik_portal_tile.yml`, `shared/tasks/sso_access_resolve.yml`, `shared/src/authentik_portal_tile.py`, `v1/tests/test_authentik_portal_tile.py`, `v1/tests/test_portal_tile_lint.py` | M |
| 2 | sksso baseline: global groups blueprint, catch-all ensure step, `BASELINE_APPS`, `sktest` group and scoped MFA bypass in the blueprint, test identities and membership tooling, `ensure-api-token.sh` untouched. | `optional/sksso/src/config/sksso/blueprints/sksso-access-baseline.yaml.j2`, `optional/sksso/tasks/test_membership.yml`, `optional/sksso/src/sksso/ensure-access-baseline.py`, `.../ensure-test-identities.sh`, `.../test-membership.py` (context manager, CLI, sweep), sksso deploy files and README | M |
| 3 | Roles adopt the block: 29 tile roles x 3 envs pass `access:` instead of `group:`; OIDC defaults in sksec-web, skpatch-hosts, skrac; `skrac` and `skmesh` provisioners write bindings; blueprints stop owning bindings that the tile script now manages (skflow keeps `adopt`); `skhub-users` and `skflow-operators` are renamed per 5.1. Mostly mechanical, generated edit. | `optional/*/deploy_*-{dev,staging,prod}.yml`, `core/skfence*/deploy_*`, `optional/skrac/src/skrac/authentik_provision.py`, `optional/skmesh/src/skmesh/authentik_provision.py`, role READMEs and SOPs, `docs/ROLE_AUTHORING_CHECKLIST.md` | L |
| 4 | Conformance check script and its unit tests, wired into the sksso deploy as a non-fatal report. | `optional/sksso/src/sksso/check-access.py`, `v1/tests/test_sksso_check_access.py` | M |
| 5 | Gate harness: headless flow-executor login, per-stack probe recipe (4.10), layers A and B, `with_test_membership` wiring, CI job against a throwaway Authentik. | `v1/tests/gate/` (new, including `login.py` and `probe_stack.py`), CI workflow | M |
| 6 | Authoritative mode and pruning, default flipped in a later release with upgrade notes; optional adoption of the catch-all provider into a blueprint. | tile script, sksso blueprint, CHANGELOG | M |

Docs to update in phase 3: `v1/docs/sso-estate-plan.md` ("Groups and roles" already proposes
`app-<app>-users`, `app-<app>-admins`; align names with the standard), `v1/docs/sso-user-management.md`
("Claims and groups", "Add a person"), skfetch README (catch-all warning), sksec-web, skpatch-hosts,
skpulse READMEs.

## 9. Risks and open questions

- **Lockout when the catch-all is bound to `skadmin`.** Anyone relying on the open catch-all today (the
  community users it wrongly admits, but also legitimate non-admin operators) is cut off. Mitigation:
  `--who-passes` review, additive rollout, break-glass session, apply per environment.
- **Provider precedence on the embedded outpost** (4.7) is unverified. If wrong, per-app forward-auth
  providers do nothing and the design falls back to narrowing the catch-all by host.
- **Group parent semantics** (4.4): avoided by design and forbidden as an access path by the standard.
  Phase 0 confirms the inheritance direction only to document why.
- **Blueprint versus script ownership** of the same binding (skflow, skhub, the two OIDC blueprints): two
  writers can flap. The `adopt` list and additive mode keep them apart; the conformance check reports
  drift.
- **Auto-created groups hide typos.** A mistyped override creates an empty group; it is fail-closed but
  silent. The deploy log line and `EMPTY-GROUP` mitigate; a stricter `SSO_GROUPS_CREATE: false` mode can
  restore the old hard failure.
- **Test identities in production.** They are low-privilege but real accounts. Keep credentials in the
  vault, disable them between runs where the estate prefers, and exclude them from user-facing lists. The MFA bypass is
  scoped to the `sktest` group on one stage binding and is opt-in per estate; the risk is a real person being
  added to `sktest`, which check X1 reports. Leaked temporary memberships are caught by the sweep.
- **Standard alignment.** Names and modes follow `standards/APPLICATION_ACCESS_SSO_STANDARD.md`, which is still
  PROPOSED (sk-standards PR #70). If it changes before merge, the names here are variables, so the change is
  data, not code. `skhub-users` and `skflow-operators` are renamed in place (section 5.1), with no legacy exception.
- **Scope of "fail-closed" for apps with their own login.** For an app whose router has no forward auth,
  `SSO_ACCESS` governs portal visibility only; the app's own login still decides entry. The design does not
  claim more.
