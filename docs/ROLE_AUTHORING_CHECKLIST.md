# Role-authoring checklist

A short list of things a new `v1/ansible/{core,optional}/<app>` role needs,
beyond "it deploys and the Traefik route works". Each item exists because a
real role shipped without it.

## Portal tile (Authentik)

If the role renders a Traefik router with a `Host(...)` rule (a web UI,
whether a Docker Swarm label or, for skfence/skfenceha, a file-provider
dynamic router), it needs one of:

1. **The shared mechanism** (almost every role): include
   `../../shared/tasks/authentik_portal_tile.yml` from the role's
   `tasks:` with a `portal_tile:` vars block (slug, name, launch_url,
   group, icon, open_in_new_tab, description). See
   `v1/ansible/optional/skpulse/deploy_skpulse-prod.yml` for the simple
   single-host case, `v1/ansible/optional/skmon/deploy_skmon-prod.yml`
   for a role with several sub-services (one tile per `loop:` item).
   Default `AUTHENTIK_TILE` to `true` (on by default; a vault that never
   sets it still gets a tile) unless the role has a specific reason not
   to (document it next to the default if so). Default the group to
   `authentik Admins`; an instance loosens that per its own vault if the
   service is meant for a broader audience (not the framework's call to
   make per-instance).

2. **A role-owned mechanism**, when the service needs its own OAuth2/OIDC
   login rather than just a bookmark (see `optional/skrac/tasks/authentik.yml`
   and `optional/skmesh/tasks/authentik.yml`): this also satisfies the
   lint below, nothing further needed.

3. **An explicit opt-out**, when a tile genuinely makes no sense (today:
   only `sksso` itself, since it IS the portal). Add a comment containing
   the literal marker `portal-tile: opt-out` and a reason to the role's
   `deploy_<app>-prod.yml`.

**Icons**: pass `icon: "fa://fa-..."` (a Font Awesome glyph, free, no
asset to manage) as the default. If the role wants to ship the service's
real logo, pass `icon_file: "icon.png"` pointing at a file under
`src/config/<app>/` in the role itself; the shared task copies it into
sksso's own media volume and points `meta_icon` at it. Authentik 2025.12+
answers `405` on the old "set icon from URL" admin action, so an uploaded
file referenced by `meta_icon` is the only way to get a real logo, not an
API call.

**Enforced by `v1/tests/test_portal_tile_lint.py`**: it scans every role
under `core/` and `optional/` for a `Host(` rule, then checks the role's
`deploy_<app>-prod.yml` for one of the three options above. A role with a
web UI and none of them fails the suite, naming the role.

## Role docs (README + SOP)

Per [`sk-standards`](https://github.com/smilinTux/sk-standards)'
[`SK_REPO_DOC_STANDARD`](https://github.com/smilinTux/sk-standards/blob/main/standards/SK_REPO_DOC_STANDARD.md),
a role's README is the hub (purpose, quickstart, "Start here", config
knobs, "Related projects / See also") and its `SOP.md` carries the full
operating procedure in the standard's 9-section order, with at least one
mermaid architecture diagram of the role itself. **Reference
implementation:** `v1/ansible/optional/skpatch-hosts/`
([README](../v1/ansible/optional/skpatch-hosts/README.md) ·
[SOP](../v1/ansible/optional/skpatch-hosts/SOP.md)): a role with a nontrivial
privilege model (an SSH credential per estate, a gated sudoers
allowlist) is exactly the case where splitting "what and why" (README)
from "how, in full" (SOP) earns its keep. Not yet backfilled onto every
existing role; new or substantially reworked roles should follow this
shape going forward.

## Everything else

(Not yet written up here; see individual role READMEs and
`docs/v1-service-catalog.md` for the broader deploy-playbook conventions
-- vault selection, network registration, image pinning by digest, and
the dev/staging/prod playbook triad.)
