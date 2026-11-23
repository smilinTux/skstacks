# Changelog

All notable changes to the SKStacks framework. Tags: `skstacks-vMAJOR.MINOR.PATCH`.
Each entry says what an instance must do, if anything.


## skstacks-v2.26.32 - 2026-11-23

### skpods: new optional service for Solid pod hosting

- New role `v1/ansible/optional/skpods` (`deploy_skpods-{dev,staging,prod}.yml`): Solid pods at
  `https://pods[-<env>].<cluster>.<domain>`. One engine setting, `skpods.ENGINE`; only `nextcloud` is
  implemented (a standalone, slim Nextcloud with Postgres, Redis and cron, most default apps
  disabled, and the Solid app shipped inside an instance-built image pinned by digest).
- The deploy sets and checks `overwriteprotocol`, `overwritehost`, `overwrite.cli.url`,
  `trusted_domains` and `trusted_proxies` before it enables Solid (each pod's root ACL stores the
  absolute WebID). The router carries only the role's own headers middleware: no CORS, error pages
  or forward auth on any path.
- `--tags seed [-e skpods_reset=true]` creates or resets users from `skpods.SEED_USERS`, and mints
  Solid client credentials for users marked `client_credentials: true` into a 0600 file on the
  controller. Optional `skpods.REGISTRY_LOGIN` logs the manager in to a private registry.
- Networks: `skpods-prod` `172.16.184.0/24`, `skpods-staging` `172.16.185.0/24`, `skpods-dev`
  `172.16.186.0/24`. **Instance action**: none unless you deploy it; then build an engine image,
  add the `skpods` vault (README minimal snippet) and run the env playbook.

### skpayment: its own minimal runtime image

skpayment ran its gateway inside a general-purpose web UI image only because
that image happened to ship fastapi, uvicorn and httpx. The default is now
`smilintux/skpayment-runtime:py3.11-20261123`, built from the new
`image/Dockerfile`: Debian 12 slim Python 3.11 pinned by digest, Debian security
updates applied, exact package pins (the same versions as before except
starlette 1.3.1, anyio 4.14.2 and certifi 2026.10.18, the first releases that fix
open CVEs), no build tools. Trivy (HIGH and CRITICAL, fixed only): 11 critical
and 165 high before, none after. The gateway answers identically on both images
(`/health`, the unconfigured webhook and checkout paths). **Instance action:**
none, unless the instance overrides `skpayment.IMAGE`.

### skmem-pg: image rebuilt on a pinned Debian 12 base

`smilintux/skmem-pg` is rebuilt from `image/Dockerfile` on
`pgvector/pgvector:0.8.7-pg17-bookworm` pinned by digest, with the Debian
security updates applied, and the stack pins the dated tag
`pg17-bm25-age-20261123` by digest instead of the moving `pg17-bm25-age`. Trivy
(HIGH and CRITICAL, fixed only): 16 critical and 67 high before, 1 critical and
25 high after, all in the upstream `gosu` binary. The base stays on Debian 12
and PostgreSQL 17 so the existing data directory, its collation and the
pgvector, pg_search and AGE indexes are untouched (checked: a data directory
written by the old image starts under the new one and all three still answer).
**Instance action:** none; the next skmem-pg deploy rolls the new image.

sksso creates the domain-wide forward-auth catch-all behind `authentik@file` as code, closed to the admin group
and on by default. Instance action: an estate that made its catch-all by hand must adopt it or opt out BEFORE
it redeploys sksso (see below); everything else needs nothing.

### sksso: the `authentik@file` catch-all, created as code and bound to `skadmin` only

- `authentik@file` (the Traefik forwardAuth middleware `core/skfenceha` ships, attached by default by many
  roles) gates nothing without an Authentik provider that matches the request host, and nothing in the repo
  created one. A new estate had SSO-default apps that were unreachable, or open to every Authentik account
  when someone hand-made the provider without a policy binding (a live audit found exactly that). sksso now
  renders `blueprints/forward-auth-catchall.yaml`: ONE `forward_domain` Proxy Provider (`external_host` the
  sksso public URL, `cookie_domain` the cluster domain, `DOMAIN` with `CLOUDFLARED`), ONE Application, and ONE
  policy binding to the admin group (`skadmin`) and nothing else. There is no "any authenticated user" path:
  `FORWARD_AUTH_ADMIN_GROUP: skuser` is refused, and the attach step refuses a catch-all whose Application has
  any other enabled binding. The group `skadmin` is created if absent (existence only, membership untouched);
  on a new estate it starts empty, so nobody passes the catch-all until admin accounts are added to it.
- The provider is attached to `authentik Embedded Outpost` additively by a new post-deploy step
  (`tasks/forward_auth_attach.yml`, `src/sksso/forward_auth_attach.py`, `outpost.providers.add()` under
  `ak shell`, no API token needed). It is not a blueprint outpost entry on purpose: a blueprint saves an
  outpost's `providers` as a full replacement set and would detach every other provider on it. It waits for
  the worker to apply the blueprint (a read-only preflight, `src/sksso/forward_auth_preflight.py`, runs BEFORE
  the blueprint is rendered and refuses the deploy when a `forward_domain` provider under another name already
  covers the cookie domain, so the worker never creates a duplicate), and refuses (the deploy fails, nothing is attached, forward auth then
  denies) when the Application is bound to more than the admin group, when another `forward_domain` provider
  already serves the same cookie domain, or when the outpost is missing or not a proxy outpost.
- Knobs, all optional and documented in the README and the new `SOP.md`: `FORWARD_AUTH_ENABLED` (default
  `true`; `false` opts out and removes a stale blueprint file this role wrote), `FORWARD_AUTH_PROVIDER_NAME`
  and `FORWARD_AUTH_APP_SLUG` (the identifiers: set them to an existing hand-made provider and application to
  ADOPT them instead of creating a duplicate), `FORWARD_AUTH_APP_NAME`, `FORWARD_AUTH_SSO_HOST` (default
  `sso[-<env>].<base>`, `CAPAUTH_SSO_HOST` when set), `FORWARD_AUTH_COOKIE_DOMAIN` (default `<base>`),
  `FORWARD_AUTH_ADMIN_GROUP` (default `skadmin`) and `FORWARD_AUTH_OUTPOST`. Contradictory settings fail the
  play before anything is rendered: opting out while setting any other knob, an unreadable
  `FORWARD_AUTH_ENABLED` (it is never read as "off" by accident), a cookie domain the sksso host is not under,
  a malformed slug or host, the app slug equal to the skgit OIDC client id.
- The `/blueprints/custom` mount in `server` and `worker` is now on by default (it was on only when another
  app's blueprint was enabled), and the playbooks create `/var/data/config/sksso-<env>/blueprints` up front.
  `FORWARD_AUTH_ENABLED: false` renders `sksso.yml` as before. The blueprint omits `invalidation_flow` for an
  image tagged older than Authentik 2024.10 (the framework's default image is 2024.4.2), so it applies there too.
- `check-access.py` reports the catch-all itself (C7, no declarations needed): missing (no `forward_domain`
  provider with an application), unbound, bound to anything but the admin group (another group, a user, a
  policy), or attached to no outpost. `authentik Admins` on the catch-all stays a C2 warning. Tests:
  `v1/tests/test_sksso_forward_auth_catchall.py` (real Ansible render of the three playbooks, the attach
  script against a fake Authentik ORM) and `v1/tests/test_sksso_check_access.py`.
- **Instance action**: an estate with a hand-made catch-all must, BEFORE redeploying sksso, either set
  `FORWARD_AUTH_PROVIDER_NAME` and `FORWARD_AUTH_APP_SLUG` (and `FORWARD_AUTH_SSO_HOST` /
  `FORWARD_AUTH_COOKIE_DOMAIN` if its provider differs from the derived values) to the existing provider's
  name and the application's slug, or set `FORWARD_AUTH_ENABLED: false`. Then confirm its binding: the
  adopted Application must be bound to `skadmin` alone (remove stray bindings such as `authentik Admins`,
  `skuser` or an any-account policy in Authentik; `check-access.py --who-passes <slug>` shows who passes).
  Skipping this no longer creates a second catch-all beside yours: a read-only preflight (`ak shell` in the
  running server container, run before the blueprint is rendered) finds a `forward_domain` provider under
  another name on the same cookie domain and the deploy REFUSES, naming it and the settings to set; nothing is
  rendered or changed in Authentik and yours keeps serving. A first install (no running server) skips the
  preflight. New estates: add the admin accounts to `skadmin` after the first deploy.

### skfenceha: the ACME master label stays where it is

Every skfenceha deploy used to remove `traefik.acme.master` from all managers and put it on
whichever manager that run selected (the first one that answered). A deploy that selected another
manager moved traefik-acme with it, and skha, which renders its keepalived priorities from the same
label, kept preferring the old node until its next deploy. The label is now sticky: a single Ready
and Active holder keeps it; it moves only when no manager has it, several do, or the holder is down
or drained. New `skfenceha.ACME_MASTER_NODE` pins it to a named manager. **Instance action:** none
required. Check that the label and skha agree (README, "Which manager is the ACME master"); if they
do not, run the skha deploy once.

### Image bumps for open CVEs

Default image pins move to releases that clear findings from a Trivy scan. Every pin stays
`tag@sha256:<multi-arch index digest>`, read from the registry. Same major everywhere, no major
bumps.

- **Postgres, three cases.** (1) Alpine server images move within alpine: skbom and skflow
  `17.7-alpine` to `17.12-alpine`, skmesh `18.6-alpine` to `18.7-alpine`. (2) Images that only run
  a dump sidecar (`POSTGRES_BACKUP_IMAGE`) move to the `-bookworm` variant of the same major:
  skbom and skflow `17.7` to `17.12-bookworm`, skgit, skmesh and sksso `18.6` to `18.7-bookworm`.
  `-bookworm` is the same Debian base the old pins already used, so there is no glibc or
  collation change; the bare `17.12` and `18.7` tags moved to a newer Debian with a newer glibc
  and more unfixed HIGH findings. (3) Debian images that run a database server (skgit and sksso
  `postgres:18.6`, skmem-pg `postgres:17.11`) are **not changed**: a base OS change under an
  existing data directory can change text collation and corrupt indexes, and these pins scan as
  well or better than the replacements. Dump sidecars hold no data directory, so they are safe to
  move.
- skbom: Dependency-Track apiserver and frontend `4.14.6` to `4.14.7`.
- skgit: Forgejo `17.0.1` to `17.0.2` (tag only, as before).
- skpdf: Stirling-PDF `3.3.0` to `3.5.0`. Upstream removed the PostScript vector conversion
  endpoints (3.4.0), the PDF to .doc output and the .wpd, .wps, .lwp, .abw, .hwp and .xps inputs
  (3.5.0), and replaced LibreOffice with its own Office Convert engine in the image.
- skrac: MeshCentral `1.2.6` to `1.3.1` (a security release). Upstream no longer ships the default
  agent binaries inside the image: the server downloads and verifies them from pinned GitHub
  releases into `meshcentral-data/agentbuilds/` at startup (`settings.agentDownloads`), so the
  first start needs GitHub egress, or the files placed there by hand.
- skhub: Talk recording image `20260929_105435` to `20261119_161514`.
- skmon: Alertmanager `v0.34.2` to `v0.35.0`, and the CI-only promtool pin `v3.15.1` to `v3.16.0`.
  Loki `3.7.10` and Grafana `13.3.1` are not taken yet: the skmon pin table requires a release to
  be 14 days old, and they are younger.
- skhome: HomeCenter `2.3.1` to `3.3.0` (major). No env, entrypoint or backup interface change;
  the app applies its database migrations at start, and two of them drop columns, so a rollback
  means restoring a backup, not just redeploying the old image. Take a backup first.
- skgym: openGym api and web `1.4.1` to `1.4.5`.
- skpatch-hosts: linux-update-dashboard `2026.10.9` to `2026.11.6`. API tokens now carry granular
  permissions; the role still registers systems through its own short-lived session.
- sksec-web: crowdsec-web-ui `2026.10.10` to `2026.11.12`. Settings that `config.yaml` or
  `CONFIG_*` set now override and lock the matching UI settings; OIDC auto-login and back-channel
  logout are off by default.
- skorch: n8n `2.49.1` to `2.49.3`.
- skmesh: NetBird management, signal and relay `0.83.1` to `0.83.2`, dashboard `v2.97.0` to
  `v2.98.0`.
- skpages: Caddy `2.11.8-alpine` to `2.11.9-alpine`, now pinned by digest.
- skport: Portainer `2.45.4` now pinned by digest (same version).
- skflow: Windmill `1.847.0` to `1.882.0`; its database migrations run at start.
- skwrit: nextcloud-mcp-server `0.199.4` to `0.218.0`. The command and environment the role uses
  are unchanged; upstream now rejects tool calls with unknown arguments and renamed its client
  loggers (`nextcloud_client.*`).
- skstream: Kometa pinned to `kometateam/kometa:v2.5.5` by digest (was a digest with no tag).
- Left as is: skgraph FalkorDB (default is still v4.x) and skvector Qdrant (default is below
  1.19), which are not part of this bump.

**Instance action:** instances that override these images in their vault or vars keep their old
pin until they change it. Redeploy the affected roles to pick up the new defaults.

## skstacks-v2.26.31 - 2026-11-21

Patch on v2.26.30. skgit can sign users in through Authentik over OIDC, managed as code on both
ends (opt-in, off by default). skha keepalived restarts wait for each node to be ready instead of a
fixed pause, and a `vip_gate_checks` entry that names no health check is rejected. skreg no longer
recreates its own overlay network. The portal tile provisioner retries Authentik's "starting" 503.
`main` merges through a merge queue. The libvirt-cluster module can route test VMs' apt through a
cache, with retries (opt-in). skreg gets back the deployable role an unrelated merge had reverted. skfenceha certs-watch tasks no longer share one temp file. Portal tiles find an existing binding by checking it, not by trusting the Authentik list filter. The skpatch-hosts registrar no longer creates a duplicate system under a second address. skbom puts its web UI behind Authentik forward auth by default (`AUTH_MODE`), and skbom and skflow give a first Postgres init 300 s. Five Renovate image updates for gated apps. Instance action: redeploy skha (check `vip_gate_checks` first, see
below); everything else is opt-in or needs nothing.

### skbom: web UI behind Authentik forward auth by default (`AUTH_MODE`)

- New `skbom.AUTH_MODE` (`sso` default, or `local`) and `skbom.SSO_MIDDLEWARE` (default
  `authentik@file`), the same pattern as skpulse. Before this, the role assumed a domain-wide
  forward-auth catch-all, so on an estate without one Dependency-Track's own login page was public.
  Forward auth goes on the frontend (web UI) router only. The `-api` router keeps the plain chain:
  CI uploads SBOMs with an `X-Api-Key`, and the API server refuses unauthenticated calls itself.
  An unknown `AUTH_MODE`, or `SSO_MIDDLEWARE` with `local`, fails the play.
- **Instance action**: none for an instance that already lists its forward-auth middleware in
  `skbom.TRAEFIK_MIDDLEWARES` (it is not appended a second time). One that wants Dependency-Track's
  own login only sets `AUTH_MODE: local`. Redeploy skbom to apply.

### skbom and skflow: Postgres first-init start period 300 s (#501)

- The Postgres healthcheck `start_period` defaults to 300 s (was 30 s) for skbom and skflow, tunable
  with `POSTGRES_HEALTH_START_PERIOD`. A first `initdb` on NFS outlived 30 s plus 6 retries, swarm
  killed it half-way, and the next task skipped init on the non-empty data dir (no `pg_hba` host
  line, no app role). **Instance action**: none; a redeploy picks it up.

### skreg: the deployable role is back (#199, undone by #200)

- #200 (skflow, 2026-10-16) was built on a pre-#199 copy of the skreg role and reverted it whole, so
  since v2.26.16 the deploy script sourced a `skreg.sh.env` nothing rendered, ran
  `docker service rm` on a service that does not exist, and deployed the stack twice in a row. The
  playbook runs it with `/bin/bash`, so the errors were ignored and only the second deploy's exit
  counted; the release gate caught it losing a race (`update out of sequence`). #199 is restored
  (rendered `skreg.env` and `skreg.sh.env`, one `docker stack deploy --prune`, network checks, a
  `/v2/` wait, the healthcheck, the README) with the later changes kept: #492's network task and the
  pinned registry 3.1.2. The compose file is rendered `0600` (root only), as the secret-file-mode
  check expects. `v1/tests/test_skreg_deploy_role.py` pins it so it cannot silently go again.
- **Instance action**: redeploy skreg. Needs `skreg.REGISTRY_HTTP_SECRET` in the vault (the play
  refuses without it; the legacy `vault_skreg_registry_http_secret` is accepted).

### skfenceha: traefik-certs-watch tasks no longer share one temp file

- `traefik-certs-watch` runs on every manager against the same shared `/dynamic-out`, and its temp
  file was `certs-generated.yml.tmp.$$`, where `$$` is 1 in every container. One task's `mv` could
  take another's temp file away mid-write, and that task exited 1 now and then
  (`sha256sum: can't open .../certs-generated.yml.tmp.1`). The temp name now carries the container
  hostname. Tests: `v1/tests/test_skfenceha_certs_watch.py`.
- **Instance action**: none; the next skfenceha deploy picks it up. A leftover
  `certs-generated.yml.tmp.1` in the dynamic dir can be deleted.

### Portal tiles: bindings matched client-side, not by the list filter

- `ensure_binding` and `ensure_group_binding` (`v1/ansible/shared/src/authentik_portal_tile.py`)
  trusted the `/policies/bindings/?target=&group=` (or `&policy=`) filters. One Authentik release
  answered with the app's binding to ANOTHER group, so the wanted binding (for example `skadmin`)
  was reported "unchanged" and never created, and the postcondition then failed the deploy after
  the stack was already up. The answer is now filtered on target and group (or policy) in the
  script, across every page. Tests: `v1/tests/test_authentik_portal_tile.py` (a fake Authentik
  that ignores the filters).
- **Instance action**: none. An estate that added the missing admin bindings by hand keeps them;
  the next tile run reports them unchanged.

### skpatch-hosts: the registrar never registers a system twice (#503)

- Systems were matched only by `hostname:port`. A provisioning run without the estate's targets
  inventory (the one with `ansible_host`) saw each host under its ssh alias and created a second
  system with the same name, and schedules, which resolve systems by name, moved to the duplicate.
  Now a target whose name is already registered at another address is left alone with a
  `WARN <name>: already registered at ...` line, nothing is edited or deleted, and it is not reported
  as an orphan. The README also says that a schedule is stopped with `enabled: false` (removing it
  from the list leaves it as it was). Tests: `test_skpatch_hosts_register_systems_js.py`.
- **Instance action**: none. If a run prints the WARN line, pass the targets inventory (or set
  `skpatch_hosts_dashboard_address`); delete any duplicate an earlier run already made by hand.

### Dependency updates (Renovate, apps covered by the release gate)

- skpulse Uptime Kuma 2.5.12 (#496), skport Portainer 2.45.4 (#497), skgit Actions runner Docker
  29.10.0 dind/cli (#498), skorch n8n 2.49.1 (#499), skmon prometheus-pve-exporter 3.11.0 (#500).
  Digest pins kept where the old default had one. **Instance action**: redeploy a service to pick up
  its new default image, as usual; vault-pinned images are unchanged.

### libvirt-cluster: `apt_proxy` no longer sends https through the cache

- With `apt_proxy` set, cloud-init also writes `Acquire::https::Proxy "DIRECT"`. apt's https method falls
  back to the http proxy when no https proxy is set; apt-cacher-ng answers that CONNECT with 403, so the
  https Docker repo never loaded and Docker never installed. The README said https went direct; now it
  does. Unset `apt_proxy` still renders byte-identical cloud-init. **Instance action**: none, opt-in.

### skreg: overlay network task no longer recreates an existing network

- `deploy_skreg-{dev,staging,prod}.yml` stop passing `driver_options: {encrypted: "false"}`. An
  overlay created without that option read as changed, so `docker_network` tried to recreate a
  network the registry was attached to and the deploy failed ("network ... is in use by service").
  Unencrypted is the overlay default, so new networks are unchanged. Tests:
  `v1/tests/test_skreg_network_no_driver_options.py`. **Instance action**: none.

### libvirt-cluster: apt retries behind `apt_proxy`

- With `apt_proxy` set, cloud-init also writes `Acquire::Retries "5"`. A test cluster's four VMs filling an
  empty apt-cacher-ng at once got one 503 on a package and one 500 on the archive's `InRelease`, and the
  HWE kernel install failed on that VM. Unset `apt_proxy` still renders byte-identical cloud-init.
  **Instance action**: none, opt-in.

### skgit and sksso: Forgejo sign-in through Authentik OIDC, managed as code (opt-in)

- **`skgit.AUTH_MODE: oidc`** makes the skgit deploy create, or update in place, ONE Forgejo OAuth2
  auth source (provider `openidConnect`, discovery URL
  `https://<sso host>/application/o/<slug>/.well-known/openid-configuration`), named by
  `skgit.OIDC_SOURCE_NAME` (default `SKStack SSO`). It runs `forgejo admin auth list`, finds the
  source by name, then `update-oauth --id N` or `add-oauth`. It never deletes an auth source and
  never touches one of another name; a same-named source of another type stops the deploy. The
  client secret travels by environment variable name (in no `docker` argv, no output, `no_log`).
  Optional knobs: `OIDC_AUTO_REGISTRATION` and `OIDC_ACCOUNT_LINKING_BY_EMAIL` (both off by
  default, rendered into `app.ini` `[oauth2_client]` only in oidc mode), `OIDC_USERNAME_CLAIM`,
  `OIDC_CLIENT_ID`, `OIDC_SSO_HOST`, `OIDC_DISCOVERY_URL`. The default `AUTH_MODE: local` leaves
  every rendered file byte-identical.
- **`sksso.SKGIT_OIDC_ENABLED: true`** (default off) renders the Authentik blueprint
  `skgit-oidc.yaml` (OAuth2 provider + application, secret read with `!Env` from
  `sksso.SKGIT_OIDC_CLIENT_SECRET`, redirect URI
  `https://<skgit host>/user/oauth2/<source name, URL-encoded>/callback`). The application slug is
  skgit's portal tile slug, so there is one Application. `sksso.SKGIT_OIDC_AUTHENTICATION_FLOW:
  capauth-authentication` opts this one application into the CapAuth (PGP, passwordless) login.
- Both ends share one secret and one source name, kept in each role's own vault
  (`sksso.SKGIT_OIDC_CLIENT_SECRET` and `skgit.OIDC_CLIENT_SECRET`; `sksso.SKGIT_OIDC_SOURCE_NAME`
  and `skgit.OIDC_SOURCE_NAME`). Tests: `v1/tests/test_skgit_oidc_sksso.py`.
- **Instance action**: none unless you want Forgejo SSO. To enable: generate one secret
  (`openssl rand -hex 32`), set `sksso.SKGIT_OIDC_ENABLED: true` and `SKGIT_OIDC_CLIENT_SECRET`, deploy
  sksso, then set `skgit.AUTH_MODE: oidc` and `OIDC_CLIENT_SECRET` (same value) and deploy skgit. An
  instance that made the Forgejo auth source or the Authentik application by hand: use the same
  source name, and the deploy takes the objects over instead of duplicating them. See the two role
  READMEs.

### Tests: Ansible's `difference` filter in the secret-file render env

- `v1/tests/test_secret_files_not_world_readable.py` renders playbooks with plain Jinja, which has no
  `difference` filter; #490's skha `vip_gate_checks` validation uses it, so 4 tests failed on main. The
  test env now provides an Ansible-compatible `difference`. Test-only. **Instance action**: none.

### skha: restarts wait for a ready node; gate names validated

- The `restart keepalived` handler no longer sleeps a fixed 20 s. It waits, one manager at a time,
  until that node reports MASTER or BACKUP (`skha_notify.sh` now also writes `/run/skha.state`),
  and fails if the node is not ready within `skha.restart_settle_seconds`. The handler is one
  `run_once` task looping over the managers whose config changed (`src/keepalived/restart_keepalived.yml`,
  delegated to each in turn), so a failed node stops the rollout and no later manager restarts.
  (`throttle: 1` plus `any_errors_fatal` does not stop it: the linear strategy still runs the task
  on every host first.) That setting is now a
  maximum, not a fixed pause, and its default is derived from the health checks (30 s for the
  default Traefik check). A manager that cannot carry the VIP is reported instead of passed over.
  A node that settles in FAULT counts as ready only while another manager answers on the VIP: with
  Traefik on some managers only (plain skfence), the others are FAULT on their gate check by
  design, and the cluster is healthy. With the VIP answering nowhere, the restart still fails and
  the rollout stops.
- `skha.vip_gate_checks` is rejected when it is a mapping, and when it names a check that is not
  in `skha.health_checks`, including the implicit default `[traefik]` (such a gate never fails,
  so the VIP would never move on it). **Instance action** if your Traefik check has another
  name: set `vip_gate_checks` to it, or the deploy now stops.
- **Instance action**: none for the defaults. If you set `restart_settle_seconds` below the time
  your gate checks need to pass `rise` times, raise it or remove it.

### Portal tiles: retry Authentik's "starting" 503

- The portal tile provisioner (`v1/ansible/shared/src/authentik_portal_tile.py`) retries an HTTP 503
  `{"error":"authentik starting"}` up to 6 times (5 to 25 s backoff, about 75 s) instead of failing
  the deploy. Authentik's front server sends it before the request reaches the backend, so every
  method is safe to resend. Other errors still fail at once. A release gate deploy failed on one such
  503 with the server up for 22 minutes. **Instance action**: none.

### CI: merge queue on `main`; CONTRIBUTING.md

- `skred-scan.yml` (the required `scan` check) and `framework-tests.yml` run on `merge_group`, so
  the merge queue tests each queued merge commit once instead of a full rerun after every BEHIND
  rebase. Both skip the queue's own `gh-readonly-queue/*` branches on `push`, so nothing runs
  twice. New `CONTRIBUTING.md` (branches, tests, the CHANGELOG placement rule, the merge queue);
  AGENTS.md now says queue with `gh pr merge <N> --squash`, never `--admin`. **Instance action**:
  none.

### libvirt-cluster: optional `apt_proxy`

- **libvirt-cluster: optional `apt_proxy`.** Setting it (for example
  `http://apt-cache.example:3142`, an apt-cacher-ng pull-through cache) makes
  cloud-init fetch apt packages on every VM through that HTTP proxy, so repeated
  test-gate runs stop re-downloading the HWE kernel and distro packages. Only
  plain-HTTP sources are cached; the https Docker repo is fetched direct. Unset
  (the default) renders byte-identical cloud-init. See the module README for the
  dead-proxy behavior.
  **Instance action:** none, opt-in.

## skstacks-v2.26.30 - 2026-11-20

Patch on v2.26.29. skpeek, skseek and skpdf sit behind SSO by default. skgit gets bounded git pack
limits, a 7-day Actions artifact expiry and a Postgres login-rule guard for restored data dirs.
sksec-web keeps its data dir's owner on redeploy. skbackup can recover from a full pool.
Machine and API paths bypass the HTML error pages on every app. New read-only SSO conformance
check and a per-app SSO access resolver. The skpatch digest lists each day's vulnerabilities.
Immich defaults to v3.4.2. skflow and skbom get the Postgres major guard. Instance action: read the upgrade notes first (SSO
defaults), then redeploy skgit, sksec-web, skbackup, skpatch-hosts and the apps listed under
"Routers" below; set `SSO_MIDDLEWARE: none` before redeploying any of skpeek, skseek or skpdf
that must stay public.

### Upgrade notes

- **UPGRADE: skpeek, skseek, skpdf are now behind SSO forward auth by default; set
  `SSO_MIDDLEWARE: none` to keep one public (#474).** None of the three has a login of its own;
  on one production instance they were open to the internet and skseek's `GET /api/config`
  returned the model provider API keys. Each router appends `<svc>.SSO_MIDDLEWARE` (default
  `authentik@file`) to the end of its chain, including an instance override of
  `TRAEFIK_MIDDLEWARES`, and never adds it twice when the chain already names it (vaults that
  added `authentik@file` by hand render unchanged). sksso must be deployed (the middleware must
  exist), or Traefik drops the router and the app is unreachable. Empty or unset keeps the
  default (fail closed). Tests: `v1/tests/test_sso_default_skpeek_skseek_skpdf.py`. **Instance
  action**: none to get SSO; set `SSO_MIDDLEWARE: none` in the vault for an instance that must
  stay public, before redeploying.
- **UPGRADE: skfence/skfenceha `DEFAULT_SNI_STRICT` defaults to false since v2.26.29 (#465).**
  An instance coming from v2.26.28 or older that ran `sniStrict: true` on the `default` TLS
  option by hand should pin the value it wants in its vault before rolling out (false serves
  hosts without a matching certificate with the default certificate instead of refusing the
  handshake).

### skgit (#405, #469)

- **Actions artifacts expire after 7 days** (#405). `app.ini` `[actions]` renders
  `ARTIFACT_RETENTION_DAYS` from `skgit.ARTIFACT_RETENTION_DAYS` (default 7), and the env file
  carries the same value. Forgejo's default is 90 days; one e2e pipeline grew the data dir by
  over 20 GB in a week until a backup pool filled. Forgejo's daily `cleanup_actions` cron does
  the expiry. Tests: `v1/tests/test_skgit_artifact_retention.py`.
- **Bounded git pack limits in `app.ini` `[git.config]`** (#469): `pack.threads=1`,
  `pack.windowMemory=256m`, `pack.deltaCacheSize=64m`, `core.deltaBaseCacheLimit=64m`,
  `gc.auto=0`, so concurrent repacks no longer OOM-kill Forgejo. `skgit.GIT_CONFIG` (a dict)
  replaces a default or adds keys. A hand edit of the live `app.ini` does not survive a
  redeploy; this is the durable form. Tests: `v1/tests/test_skgit_git_pack_limits.py`.
- **Postgres network login rule guaranteed in an existing data dir** (#469, first opened as #471). The postgres
  entrypoint appends `host all all all scram-sha-256` to `pg_hba.conf` only when it creates the
  cluster, so a restored or hand-initialised data dir refused Forgejo. New
  `shared/tasks/postgres_hba_guard.yml` runs before the skgit stack deploy: a fresh dir is left
  alone, an existing `host all all all|0.0.0.0/0` rule is untouched, otherwise the scram rule is
  appended. Tests: `v1/tests/test_postgres_hba_guard.py`.
- **Instance action**: redeploy skgit. The first deploy restarts Forgejo once. After it, check
  that `app.ini` has the `[git.config]` section and `ARTIFACT_RETENTION_DAYS`. If Postgres was
  running when the guard appended the rule, reload it (`SELECT pg_reload_conf()`).

### sksec-web (#470)

- The data-dir tasks asserted `root:root 0700` on every run, so a redeploy took the dir back
  from the running app (`unable to open database file`) until a forced restart (#446). They now
  run only when the dir is missing; an existing dir's owner and mode are left alone.
- The config force-update after the deploy retries on Swarm's transient `update out of
  sequence` (5 x 3 s) (#464).
- Tests: `v1/tests/test_sksec_web_datadir_and_force_update.py`. **Instance action**: redeploy
  sksec-web.

### skbackup: a full pool can recover (#404)

- The retention prune runs for every app on success and on failure, so a full pool expires old
  copies again. `--delete-after` leaves the previous copy whole when a run runs out of space.
- Per-app timeout `copy.timeout_seconds` (default 7200, `0` off); a timed-out app is reported
  and the run continues. Capacity precheck `copy.precheck_capacity_pct` (default 85) notifies.
- `check` warns when a copy shrank by more than `alerts.copy_shrink_pct`, and
  `alerts.thin_volumes` watches thin LVs. The alert-host check keys an alert on the condition,
  digits stripped, so a persisting condition keeps one thread.
- Tests: `v1/tests/test_skbackup_full_pool.py`. **Instance action**: redeploy skbackup; keep
  reproducible bulk (CI artifacts, packages, caches) out of the copy (README).

### skpatch-hosts (#401, #408, #476)

- With `AUTH_MODE: oidc` the healthcheck reads `/api/auth/status` and fails unless
  `oidcEnabled` is true, so a task that started while the identity provider was unreachable
  (SSO silently off) is replaced. `start_period` defaults to 90 s in oidc mode. `sso` and
  `local` modes are unchanged. Tests: `v1/tests/test_skpatch_hosts_render.py`.
- README and SOP rewritten to the sk-standards doc standards (docs only, #408).
- **Daily vulnerability list in the Talk digest** (#476). The digest gains a "Vulnerabilities"
  section naming the security updates per host (from the dashboard API, GET only; the pinned
  dashboard stores no CVE ids), with the full list as a reply in the digest's thread. Optional
  image side: new `skpatch-image-vulns@` service and timer (06:15, before the 07:00 digest) list
  the swarm's images over `SKPW_SWARM_SSH` and scan each with a digest-pinned Trivy container
  (HIGH and CRITICAL, fix available); the digest reads the result from `SKPW_IMAGE_VULNS`.
- **Instance action**: redeploy skpatch-hosts where `AUTH_MODE: oidc`. For image CVEs, set
  `SKPW_SWARM_SSH` and `SKPW_IMAGE_VULNS` in the watch's env file and enable
  `skpatch-image-vulns@<estate>.timer` (needs local docker for the units' user); without them the
  digest carries the host lines only. No dashboard redeploy is needed.

### Machine and API traffic bypasses the error-pages middleware on every app (#410)

- **Fix: API errors were hidden behind the SKStacks error page.** The shared Traefik chains end in
  `error-pages@file`, which replaced any backend 400 and 402-599 with an HTML page and replayed the
  original method there (a failed POST read as a 405 or 501 from the error page). API clients, webhook
  senders, S3 and WebDAV clients, git, OIDC clients and probes never saw the app's real status or JSON
  error. It hid a real skgit merge failure for three days (skgit was fixed first). Now skorch, skport,
  skpdf, skseek, skpeek, skboard, skgallery, skflow, skaegis, skpayment, skhub, skbook, skstream and
  sksso have an `-api` router for their machine paths (derived chain without error pages, knob
  `<app>.API_MIDDLEWARES`); skpulse's public and metrics routers, skrac's agent router and skpayment's
  webhook router drop error pages from their default chain; skstor (S3), skvector and skwhoami are
  API-only and drop it from the default. See "API routers" in `docs/v1-service-catalog.md`. Needs the
  operator: nothing, unless the main chain is `default@file` with an IP allowlist or CrowdSec, in which
  case set `<app>.API_MIDDLEWARES` to keep them on the API router. skbook, skstream, sksso and skhub take
  the chain from the instance vault, so their API router follows whatever `router_middlewares` says.
- Tests: the guarded skseek probe in the skstack06 gate (anonymous `/api/config`, `/api/`, `/` must
  302 to sign-in or 401) covers the SSO chain on the `-api` routers. **Instance action**: redeploy
  the apps listed above.

### SSO access: conformance check and per-app resolver (#472, #475)

- `optional/sksso/src/sksso/check-access.py` (#472): a read-only conformance check of
  Authentik applications, bindings and groups (design #361 checks C1 to C10). GET only; the
  token comes from `AUTHENTIK_TOKEN` or stdin and is never printed. Exit 0 conforms, 1
  violation, 2 could not run.
- `shared/tasks/sso_access_resolve.yml` and the tile script (#475, design #361 phase 1): a
  role's `SSO_*` keys resolve into one access declaration, validated at render. The tile script
  creates missing groups and binds the declared mode, additive only, with a postcondition (at
  least one enabled binding); a new application whose bindings fail is deleted again. Roles
  that pass only `group:` keep admin-only access.
- **Instance action**: none required. Run `check-access.py` against the instance's Authentik
  to see its current state before the next tile run.

### skgallery: Immich v3.4.2 by default (#485)

- The default for server, microservices and machine-learning moves from v2.7.5 to v3.4.2 (security
  fixes). The Immich Postgres image stays `14-vectorchord0.4.3-pgvectors0.2.0`, as upstream's 3.4.2
  compose has it. **Instance action**: none for an instance that pins `IMMICH_VERSION`; one on the
  default upgrades on its next skgallery redeploy (read Immich's v3 upgrade notes first).

### skflow, skbom: Postgres major guard and PG18 data-mount layout (#484)

- All six deploy playbooks run `shared/tasks/postgres_major_guard.yml` before the stack deploy, as
  sksso, skgit, skmesh and skorch already do (skflow skips it for an external `DATABASE_URL`). The
  data mount follows the image: `/var/lib/postgresql` for 18 and newer, `/var/lib/postgresql/data`
  before, so an 18 image no longer lands on an anonymous volume. Defaults stay on 17.7 and render
  byte-identical. Each README gets a "17 to 18" dump-and-restore runbook. **Instance action**: none
  on the defaults; move to 18 only by the runbook, pinning both images.

### Docs: Postgres to 18 restore runbooks (skgit, skmesh, skorch, sksso, skflow, skbom)

- Step 4 of "dump and restore (16 to 18)" (and skflow and skbom's new "17 to 18") now stops the app and its backup sidecar again, drops the
  fresh application database and only then restores the full `pg_dumpall`. Step 3's deploy starts
  the app, which migrates the empty 18 cluster, so restoring on top collided with that schema, and
  the sidecar could dump the empty interim database. Found on a live sksso upgrade. **Instance
  action**: none; follow the updated runbook for a 16 to 18 move.

### Renovate and CI (#473, #477, #478, #482)

- Renovate groups minor, patch and digest bumps per service directory (the old
  `{{parentDir}}` group put everything in one branch). CI Python stays below 3.13,
  `dmacvicar/libvirt` below 0.9, skmon's Python runtime images on 3.13; no updates for the
  retired skgraph and skvector.
- Image bumps from the old batch, re-applied on main: data stores (mariadb, redis, postgres
  digests), netbird and coturn, the skmon stack and others (see #473).
- Majors capped per the 2026-11-20 migration plan (#482): mariadb below 13, postgres, pgvector and
  postgres-backup-local below 19, skgallery's postgres below 15 (the dump sidecar matches Immich's
  14), skflow, skbom and skmem-pg postgres below 18 (no major guard yet), mysql below 9, promtail
  below 3.
- CI: `actions/upload-artifact` v7, `actions/download-artifact` v8.
- **Instance action**: redeploy a service to pick up its bumped image as usual.

## skstacks-v2.26.29 - 2026-11-19

Patch on v2.26.28. Every Traefik router now uses the default TLS options, so a restarted service no
longer strands HTTP/2 clients on 421. skha's VIP fails over again (the ACME master is preferred,
not locked). Adds the SKPages role and a generated subnet registry. Instance action:
redeploy skfence or skfenceha (TLS options), then any service whose routers carried
`tls.options`; remove `TLS_OPTIONS`, `tls_options` or `TRAEFIK_TLS_OPTIONS` from vaults that set
`intermediate@file`; set `DEFAULT_SNI_STRICT: true` where the estate ran `sniStrict` before and a
certificate covers every router; redeploy skha on the whole manager group (see skha below).

### SKPages: agent-published HTML pages (new role, #461)

- **`optional/skpages`**: one Caddy container serving a read-only docroot (usually a Nextcloud
  group folder) at `pages.<cluster>.<domain>`, private behind `authentik@file`, with `/p/<token>/`
  public by link. Knobs for the middleware chains (`TRAEFIK_MIDDLEWARES`,
  `TRAEFIK_MIDDLEWARES_PUBLIC`), `CERTRESOLVER` and `AUTH`. The publishing CLI and skill are in
  smilinTux/skpages. **Instance action**: none unless you deploy it (README, Deploy); an instance
  that carried its own `optional/skpages` drops it and keeps its vault.
- **`v1/ansible/shared/networks`** is now generated from every playbook's default subnets by
  `v1/scripts/networks_registry.py`, with the free /24s listed at the bottom. CI fails when it is
  stale, and `test_unique_subnets.py` now sees every subnet format (it missed bare `- subnet:`
  lists and per-env maps before). **Instance action**: none.

### Traefik: no router names a TLS option (#463)

- A router with `tls.options=intermediate@file` made Traefik answer 421 Misdirected Request on
  every request of an HTTP/2 connection opened while that router was missing (a service restart),
  because its domain-fronting check compares option names. Browsers retry on 421; Forgejo runners
  do not, so they stayed stuck until restarted (one nor runner slot for about 16 hours on
  2026-11-18, 68,624 421s in the access log).
- skfence and skfenceha: `sniStrict` on the `default` TLS options is opt-in,
  `DEFAULT_SNI_STRICT` (default `false`). The first v2.26.29 candidate turned it on for every
  router, which refused the handshake for any host with no matching certificate (ACME still
  pending, or only Traefik's self-signed default cert); the skstack06 gate caught it. Routers
  that used `intermediate@file` lose its sniStrict unless the instance sets the knob.
  **Instance action**: set `DEFAULT_SNI_STRICT: true` only where a certificate covers every router.
- Hardcoded `tls.options=intermediate@file` labels are removed from skgit (4 routers), skboard,
  skorch, skpdf, skpeek, skport, skpulse, skseek, sksso, skstor, sksync and skvector. The
  `TLS_OPTIONS` knobs of skaegis, skbom, skflow, skgym, skhome, skpatch-hosts, skpayment, skrac and
  sksec-web default to `none`, and skgallery's `TRAEFIK_TLS_OPTIONS` renders only when set. An
  explicit value still works.
- Tests: baselines ignore the removed labels, render tests expect none by default, and a repo-wide
  guard fails on any template that hardcodes or defaults a router to a named option.

### skha: the VIP fails over again, no ACME-master lock by default (#466)

- **Every manager is `state BACKUP` with `nopreempt` and tracks the health checks.** The ACME
  master (label `traefik.acme.master`) used to be rendered `state MASTER`, priority 150, with no
  health checks, so the VIP could never leave it. On 2026-11-19 one estate's ACME-master-configured
  manager held the VIP while its Traefik worker could not start (NFS `/var/data` failed to
  mount after the host crashed), public 443 was refused, and the two healthy workers never got
  the VIP. skfenceha's ACME uses the Cloudflare DNS-01 challenge, so renewal never needed the VIP
  there. The ACME master is now only preferred: `skha.acme_master_priority`, default 150.
- **VIP gate checks** (`skha.vip_gate_checks`, default `[traefik]`) default to `require: true`
  and `weight: 0` and start failed (`init_fail`). A node with no running `traefik-worker` fails
  the check and goes to FAULT, and the VIP moves to a peer in about 10 s plus the master-down
  timer. A negative weight alone never moves the VIP under `nopreempt`.
- keepalived restarts on a redeploy go one manager at a time (`throttle: 1`) with a
  `skha.restart_settle_seconds` (default 20) wait each, so a config change never drops the VIP.
  `skha.vip_gate_checks` must be a list; the play fails on a string.
- `skha.lock_vip_to_acme_master: true` restores the old lock, for an estate whose ACME challenge
  needs inbound traffic. It has no failover.
- Tests: `v1/tests/test_skha_failover.py`, plus gate cases in
  `v1/tests/test_skha_health_check_require.py`. **Instance action**: redeploy skha on every
  manager (`deploy_skha-<env>.yml`, whole manager group). Before running it, check that every
  manager runs a `traefik-worker`. A manager without one goes to FAULT after the redeploy, which
  is the point, but if NO manager has one, no node holds the VIP. That includes a fresh estate
  where skha is deployed before skfenceha, and an estate that ran its ACME master without a
  worker on purpose. If your own `skha.health_checks` names the Traefik check something other
  than `traefik`, set `skha.vip_gate_checks` to that name.

## skstacks-v2.26.28 - 2026-11-18

Patch on v2.26.27. Fixes the Talk HPB on `TALK_TURN_EXTERNAL` estates and gives skfence the
timeout knobs skfenceha already has. Instance action: redeploy skhub; an skfence estate with Talk
internal signaling or large uploads sets `skfence.FORWARDING_TIMEOUTS`.

### skhub: the talk-hpb healthcheck in `TALK_TURN_EXTERNAL` mode (#458, #459)

- With `TALK_TURN_EXTERNAL: true` the default talk-hpb command runs no eturnal, but the service
  inherited aio-talk's image healthcheck, which also requires eturnal and the TURN port. It never
  passed, so Swarm stopped an otherwise healthy HPB about every 60 s and it stayed 0/1. The
  external mode now gets its own healthcheck (signaling on 8081 with `/api/v1/stats`, and Janus on
  8188). `TALK_HPB_HEALTHCHECK_TEST` replaces the test in any mode. Default renders are unchanged.

### skfence: `FORWARDING_TIMEOUTS` and `RESPONDING_TIMEOUTS` (#460)

- skfence hardcoded `responseHeaderTimeout: 30s`. Nextcloud Talk internal signaling long-polls
  for 30 s, so every idle poll got a 504 and Talk showed "Lost connection to signaling server";
  the same limit cut multi-GB skhub uploads. `skfence.FORWARDING_TIMEOUTS` (a map) replaces the
  forwarding timeouts and `skfence.RESPONDING_TIMEOUTS` (a map) sets the websecure entrypoint's
  responding timeouts, mirroring skfenceha's `WORKER_*` knobs. Unset, the render is byte-identical
  and the default (30s/10s/90s) is skfenceha's. See the skfence README for the values to set.

## skstacks-v2.26.27 - 2026-11-18

Cut from main. Gated on skstack06 (fresh, PROFILE=full). Read the upgrade notes before rolling:
skmon v2 replaces the skmon stack, skgit rolls once onto the code.forgejo.org registry, and the
first skpatch run on workers now goes through the swarm upgrade gate. An instance that runs the
Talk HPB next to the bundled skmesh coturn sets `skhub.TALK_TURN_EXTERNAL: true` with
`TURN_DOMAIN` and `turn_secret` pointing at that coturn.

### skmon v2: the stack starts (healthchecks, Grafana provisioning)

Found by the skstack06 tier-2 gate, the first live deploy of skmon v2 (#373):

- **VictoriaMetrics and vmalert never became healthy.** They listen on IPv4 only, and busybox
  `wget http://localhost:<port>` tries `::1` first and is refused, so Swarm stopped both tasks over
  and over. Every wget healthcheck now targets `127.0.0.1`.
- **Grafana refused to start.** The dashboards copy put `SOURCES.yml` (the boards' vendoring
  provenance) at the top of `provisioning/dashboards`, where Grafana parses every YAML file as a
  provider config. The deploy now removes it after the copy, including from a data node that
  already has it.
- **No agent was scraped on a host with UFW.** The Alloy agent runs on the host network, so its
  port (`AGENT_PORT`, 12345) is reached through INPUT, where a default-deny UFW (as the framework's
  swarm-firewall role sets it) dropped every scrape after `SKMON-<ENV>` had let it through. When
  UFW is active, the deploy now allows the agent port in UFW; `SKMON-<ENV>` still runs first and
  drops every source that is not an allowlisted agent address. Loki, Pyroscope and `/healthz` are
  Swarm-published ports and were never affected.

### skhub: the Talk HPB can use an external TURN (`TALK_TURN_EXTERNAL`)

- **`skhub.TALK_TURN_EXTERNAL: true` (with `enable_talk_hpb`)** keeps the HPB and hands TURN to the
  server at `TURN_DOMAIN`, typically the bundled skmesh coturn (#450), which binds 3478 on the
  managers' host network. talk-hpb then publishes no 3478 (the routing mesh would otherwise open it on
  every node) and, with the default image, runs without eturnal (`dinit ... nats-server janus
  signaling`). Janus and Talk use `TURN_DOMAIN:3478` with `turn_secret` (set it to the coturn's
  `TURN_SECRET`); `TALK_TURNS_PORT` (e.g. 5349) adds the TLS entry. The deploy refuses it together
  with the eturnal-only knobs (`TALK_TURN_TLS_443`, host publish mode, relay range or address).
  Unset, everything renders as before. Tests: `v1/tests/test_skhub_talk_turn_external.py`.
  **Instance action**: none unless you set it; an instance turning the HPB on next to the bundled
  coturn sets `TALK_TURN_EXTERNAL: true`, `TURN_DOMAIN` and `turn_secret`.

### skrac: agent watchdog for stale MeshCentral agent sessions

- skrac: agent watchdog (`deploy_skrac-agent-watchdog.yml`): a timer restarts a MeshCentral agent whose server session went stale (nothing received for 10 min, no connection, or a spinning main process), and agent units get `Restart=always` and a 20 s stop timeout. Local signals only, no credentials on the host. From an agent that sat offline while its TCP connection stayed ESTABLISHED.

### skgit: Forgejo pulls from code.forgejo.org (codeberg.org is NXDOMAIN)

- codeberg.org stopped resolving on 2026-11-18 (`.org` answers NXDOMAIN), so
  `codeberg.org/forgejo/forgejo:17.0.1` could not be pulled and the weekly
  check-images canary failed on every run. The skgit image now defaults to
  `code.forgejo.org/forgejo/forgejo`, Forgejo's own registry, which carries the
  same release tags (the runner already came from there).
- New `skgit.FORGEJO_IMAGE` sets the image repository (default
  `code.forgejo.org/forgejo/forgejo`); `skgit.FORGEJO_VERSION` still sets the tag.
  An instance that wants codeberg back sets `FORGEJO_IMAGE: codeberg.org/forgejo/forgejo`.
- Deploying this changes the image reference of running skgit stacks, so the
  next deploy rolls the forgejo service once (same version, new registry).

### skrac, skmesh: portal tiles open in a new tab

- **Both Authentik provisioners now set `open_in_new_tab`** on their application (knob
  `<role>.AUTHENTIK_TILE_OPEN_IN_NEW_TAB`, default `true`), matching the shared portal-tile
  helper's default. Before, they never set it, so a fresh create got Authentik's default (False)
  and these were the only two tiles opening in the same tab (found on an instance 2026-11-17).
  The deploy now also puts it back if it drifts. Tests: `v1/tests/test_authentik_tile_open_in_new_tab.py`.
- **Instance action:** none. An instance that wants the old behaviour sets the knob to `false`.
  On the next skrac or skmesh deploy the provisioner prints `application: updated (open_in_new_tab)`
  where the tile was still False.

### skmesh: the bundled coturn is reachable; skhub: Talk without the HPB

- **skmesh coturn (`DEPLOY_COTURN`) now works.** It used `network_mode: host`, which `docker stack
  deploy` ignores, and published no ports, so nothing listened on 3478/5349 and NetBird clients showed
  STUN and TURN Unavailable (#449). coturn now joins Swarm's predefined `host` network, reads a mounted
  `turnserver.conf` (the shared secret is no longer on the command line, and coturn no longer gets all of
  `skmesh.env`), denies private and special peer ranges, and runs as `nobody` after start. New knobs:
  `TURN_DOMAIN`, `COTURN_RELAY_IP`, `TURN_TLS` (5349 TLS/DTLS with the Traefik certificate skfence's
  dumper writes; a no-op router on the dashboard makes Traefik obtain it, and coturn restarts on
  renewal), `TURN_TLS_DUMP_DIR`, `TURN_ALLOWED_PEER_IPS`, `COTURN_PLACEMENT_CONSTRAINTS`, `COTURN_IMAGE`
  (default now pinned by digest). With `DEPLOY_COTURN`, `TURN_TIME_BASED_CREDENTIALS` defaults to true
  (coturn's `use-auth-secret` refuses static credentials). See skmesh README "Bundled coturn".
- **skhub `TALK_SIGNALING: internal`**: Talk on its built-in signaling with TURN/STUN from an external
  server (`TURN_DOMAIN`, `turn_secret`, optional `TALK_TURNS_PORT`, `TALK_STUN_SERVERS`); removes a stale
  `signaling_servers`. See skhub README "Talk without the HPB".
- **Instance action:** none unless `DEPLOY_COTURN` is true. Such an instance's coturn changes on the next
  skmesh deploy (it starts listening; add router forwards and firewall allows for 3478 tcp+udp, 5349 if
  `TURN_TLS`, and the relay range udp), and its peers switch to TURN REST credentials unless
  `TURN_TIME_BASED_CREDENTIALS: false` is set.

### skpatch-hosts: the swarm upgrade gate covers workers and repairs stale vxlan devices

- **Every swarm node gets the gate, workers too** (`provision_skpatch_hosts_nodes.yml` now detects a
  swarm member, `LocalNodeState` active, instead of a manager). On 2026-11-17 a worker's docker-ce
  upgrade left a stale overlay vxlan device and every task placed there was rejected with
  `error creating vxlan interface: file exists` for 11 hours.
- **After an engine upgrade, every node** waits until it is an active swarm member and dockerd's
  journal shows no such rejection since the upgrade. On a rejection it deletes only the `vx-*`
  devices (host netns and docker's overlay netns) of overlays with no running container on that node,
  never the ingress's, and checks again. A timeout sets the hold and fails the apt run (the dashboard's
  failed upgrade, alerted by `skpatch_watch`).
- **One hold for the whole swarm**: a file on shared storage (`SWARM_GATE_SHARED_DIR`, default
  `/var/data/skpatch-swarm-gate`) that workers and managers read, plus the manager label as before.
  A failed worker stops the next worker and every manager. `sk-swarm-upgrade-gate clear-hold` clears
  both.
- Tests: `v1/tests/test_skpatch_swarm_upgrade_gate.py`, which runs the script against a fake swarm
  (docker, ip, nsenter and journalctl stubs). **Instance action**: rerun the provisioning playbook
  with the workers in `-l` (and the dashboard node); the check-diff shows the three gate files new
  on each worker and the settings file gaining `SHARED_DIR` on each manager.

### skpatch-hosts: single-manager swarm holds docker engine upgrades

- The swarm upgrade gate refuses a `docker-ce`/`containerd.io` upgrade on a swarm with one manager
  unless an engine window is open on that node (label `sk.skpatch.engine-window=<epoch until>`) or
  `skpatch_hosts.SWARM_GATE_SINGLE_MANAGER_ENGINE: allow` is set. On one manager the upgrade restarts
  every service at once; an unattended one took a single-node estate down for 7 to 12 minutes
  (2026-11-17). Instance action: rerun `provision_skpatch_hosts_nodes.yml` (the same run as above) to install the
  new gate; a single-node estate that WANTS unattended engine upgrades sets the knob to `allow`.

### skhub: MariaDB auto-upgrade of an older datadir no longer crash-loops (#438)

- The `db` service reads the shared `skhub.env`, whose `MYSQL_HOST=db` is meant for the Nextcloud
  containers. On a datadir from an older MariaDB major, the image entrypoint's `mariadb-upgrade`
  (with `MARIADB_AUTO_UPGRADE=1`) honoured it and tried TCP to `db` while the temporary server
  listened on the socket only: "Can't connect to server on 'db'", "Upgrade failed", crash loop.
  The db wrapper now unsets `MYSQL_HOST` before `docker-entrypoint.sh`. skbook's identical wrapper
  gets the same line (its env has no `MYSQL_HOST`; the two stay in step). Found migrating a
  MariaDB 10.11 instance onto the 12.3 default. Test: `v1/tests/test_skhub_db_no_mysql_host.py`.

### sksso: instance-owned branding packs (`sksso.branding_dir`)

- New optional `sksso.branding_dir`: load the branding pack from the instance repo (absolute, or
  relative to `inventory_dir`, the same rule as skfence `EXTRA_DYNAMIC_FILES`) instead of the role's
  `branding/<pack>/`. `sksso.branding` still names the pack and its `media/public/<pack>/` dir and
  must now be a plain name. Default empty: unchanged behaviour for `skworld` and `nam`.

### skmesh: the postgres runtime directory owner and mode follow the image

- **skmesh: `/var/data/runtime/skmesh-<env>/postgres` gets the image's postgres uid as owner and
  group, and 0700 only when it is PGDATA** (prod and dev playbooks), the same hardening skgit got in
  #426.
  - **Owner**: 70 for the alpine images, 999 for the Debian ones. The deploys wrote 999 for every
    image while the alpine entrypoint chowns PGDATA to 70 on each start, so live hosts drifted
    from the framework (70:0700 live vs 999:0755 rendered on an estate running 16 alpine).
  - **Mode**: with a pinned postgres older than 18 the directory is PGDATA (mounted at
    `/var/lib/postgresql/data`) and is created 0700 instead of 0755. With postgres 18 and up (the
    role default) it is the parent of PGDATA (mounted at `/var/lib/postgresql`) and stays 0755: the
    alpine entrypoint runs as uid 70 and must traverse it, and a 999-owned 0700 parent stopped
    initdb (`mkdir: can't create directory '/var/lib/postgresql/18/': Permission denied`; the
    skstacks-v2.26.26 gate, which shipped without the first version of this change, #429, see
    #436).
  - The playbooks derive both from `skmesh.POSTGRES_IMAGE` with the same default and the same
    major parse as `skmesh.yml.j2`'s bind-mount target. Tests: `v1/tests/test_skmesh_pgdata_mode.py`.
    **Instance action**: none; the next skmesh deploy applies it. On an alpine estate its
    check-diff shows the owner and group going to 70, which is what the entrypoint already does.

### skhub: MariaDB buffer pool and memory limit are knobs

- **skhub: `DB_INNODB_BUFFER_POOL_SIZE` and `DB_MEMORY_LIMIT`.** The db service took MariaDB's 128M
  InnoDB buffer pool and a fixed 2G limit. On a hub with a large `oc_filecache` that cache misses
  constantly, so authenticated WebDAV requests read from disk (found on a production estate with an
  8 GB filecache table). Set `skhub.DB_INNODB_BUFFER_POOL_SIZE` (for example `1536M`) and, when it is
  over about 1G, raise `skhub.DB_MEMORY_LIMIT` (default `2G`) to leave room for connections. Unset
  keeps today's behaviour. Changing either restarts the db task on the next deploy. MariaDB can also
  resize the pool online with `SET GLOBAL innodb_buffer_pool_size`, which a restart reverts.

### skmon v2, phase 1: VictoriaMetrics, Alloy agent, Loki and Grafana on local disk

- **skmon: Prometheus, Promtail, node-exporter and cAdvisor are removed.** The stack is now
  `victoriametrics` (metrics, vmui at `vmui[-env].<cluster>.<domain>`), `loki`, `grafana` and
  `alertmanager`, all pinned by digest and placed on `DATA_NODE`. One Grafana Alloy agent per host
  (a host container, or a systemd unit on storage and hypervisor hosts) replaces the three global
  services: node metrics, container metrics, Docker and journal logs to Loki, and blackbox probes. The
  next deploy's `docker stack deploy --prune` removes the retired services. Needs the operator: set
  `skmon.DATA_NODE` (a swarm node, required) and `skmon.DATA_PATH` (local disk, default
  `/srv/local/skmon-<env>`, never under `/var/data`) and create nothing else by hand; the deploy makes
  the directories. Any `PROMETHEUS_*`, `NODE_EXPORTER_*`, `CADVISOR_*`, `PROMTAIL_*` or `JAEGER_*` vault
  key now fails the deploy with a message naming it: delete those keys. `skmon.replicas: 0` still
  parks everything, agents included, keeps `DATA_PATH`, and the deploy script no longer force-updates
  parked services.
- **skmon: default probes.** The blackbox probes default to one per Traefik router host found on the
  estate at deploy time (every `traefik.http.routers.<name>.rule` Host on every service), severity
  `critical`, pass on 2xx or 3xx, with the labels `probe`, `severity` and `forward_auth` (true when the
  router carries `skmon.FORWARD_AUTH_MIDDLEWARE`, default `authentik@file`). `skmon.PROBES` replaces
  discovery. An `authentik` health probe is added when the sksso host is known (`skmon.AUTHENTIK_HOST`,
  else derived from sksso's vault); without either it is left out and the deploy carries on.
  `skmon.HEALTHZ_ALLOW` lists the addresses allowed to reach the bridge `/healthz`.

### Upgrade notes (instances)

- **skmon v2: the first v2 deploy restarts every skmon service and replaces the stack.** Plan for a short
  gap in metrics, logs and alerting, and run the migration runbook in the skmon README ("Migration from
  skmon v1"). `docker stack deploy --prune` removes `prometheus`, `node-exporter`, `cadvisor`, `promtail`
  and `jaeger`; `/var/data` is untouched, and the old `/var/data/skmon-<env>` directories can be removed
  by hand after a week. Set `skmon.DATA_NODE` and `skmon.DATA_PATH` (a dedicated local disk, never shared
  storage). New required keys: `skmon.ESTATE`, `GRAFANA_OIDC_CLIENT_ID`, `GRAFANA_OIDC_CLIENT_SECRET`,
  `AUTHENTIK_HOST`, `HEALTHZ_ALLOW`, `SKALERT_SCRIPT_SRC`, `SKALERT_SCRIPT_SHA256`, `ALERT_ROUTING`,
  `ALERT_SECRETS` and `ALERT_OUT_OF_DOMAIN_CHANNEL` (or an explicit `ALERT_ACCEPT_IN_DOMAIN_ONLY` with
  its reason). `AUTHENTIK_TILE_GROUP` and `GRAFANA_BASIC_AUTH` are retired: Grafana signs in through OIDC,
  and vmui and Alertmanager use forward auth (`FORWARD_AUTH_MIDDLEWARE`, default `authentik@file`). On
  the Authentik applications skmon owns, `skadmin` is the only binding and any other is removed on every
  deploy. The retired `jaeger` and `prometheus` applications are removed only when `AUTHENTIK_PROVISION`
  is true; otherwise remove them by hand. Inventory hostnames of swarm hosts must equal their Swarm
  hostnames (the agent's `node` label). Finish with the outside Kuma monitor on `/healthz`: an estate
  is not done until it is green.

- **skmon: Jaeger removed** (carried from #368, closed as superseded by the skmon v2 design). Jaeger had
  no trace producers: its service, Traefik router, Prometheus job, deploy health wait, icon and portal
  tile entry are gone, and `JAEGER_*` knobs no longer exist. The next deploy's `docker stack deploy --prune`
  removes the service. Left for the operator: an existing Jaeger portal tile (slug `jaeger`,
  `jaeger-<env>` off prod) is not deleted automatically, so delete it once in the admin UI or with
  `ak shell`; `/var/data/skmon-<env>/jaeger/` is left on disk. The v2 skmon descriptor, service page and
  STACK-MAP now name Loki as the log store.

### New

- **`optional/skaegis`: continuous CVE detection for every host (upstream Wazuh 4.14.10).** Opt in. The
  indexer, manager and dashboard run pinned by digest on a node labelled `skaegis=true` (plus `DATA_NODE`
  for local disk); only the dashboard is on the edge (`traefik.swarm.*`, crowdsec via `default@file`). Login
  is OIDC against sksso, admin-only on `skadmin`, with the internal `admin` kept for break-glass. Needs a
  `skaegis-<env>-<domain>_vault.yml` (README "Vault contract") and, for OIDC,
  `sksso.SKAEGIS_OIDC_CLIENT_SECRET` plus a sksso redeploy (the blueprint makes the provider, the
  application and its `skadmin` binding). Agents: `provision_skaegis_agents.yml` (Debian and Ubuntu,
  pinned and held, `state=absent` to remove) and a Windows script delivered through skrac. It does not
  replace skpatch-hosts: skaegis detects, skpatch remediates.

## skstacks-v2.26.26 - 2026-11-17

Patch line on v2.26.25 (same base and release-line commits, no other changes from main).

### skpatch-hosts: every dashboard action works on an armed host; one onboarding path (#403, #406)

- **Selected-package upgrades (#403):** sudoers grants two root-owned wrappers by absolute path,
  `skpatch-apt-only-upgrade` and `skpatch-snap-refresh`, instead of an apt-get or snap wildcard
  (which would allow `-o APT::Update::Pre-Invoke::=<cmd>`). Each wrapper validates every name,
  clears the environment and ends option parsing with `--`.
- **Actions that failed on an armed host (#406):** `dpkg --configure -a` for the Repair banner;
  `env_keep += "DEBIAN_FRONTEND"` so debconf stays noninteractive; dpkg `force-confdef`/`force-confold`
  (`DPKG_KEEP_CONFFILES: false` opts out); the read-only Proxmox `pvesh get /cluster/tasks` check
  where pvesh exists; pacman and flatpak hosts (exact commands plus validated wrappers); Ubuntu
  phased updates; `EXTRA_HOSTS` for SMTP without hairpin NAT. No wildcard anywhere; the Proxmox and
  Arch variants pass `visudo -cf`.
- **One onboarding path:** one `provision_skpatch_hosts_nodes.yml` run also converges the dashboard's
  wrapper scripts and settings; hosts with an uncovered package manager are refused; maintenance
  pause (`skpatch_hosts_paused` or `/etc/skpatch-hosts.paused`); `--check --diff` no longer fails in
  registration.
- **Drift and compliance:** CI fails when the pinned image's own sudo command list changes without
  a sudoers decision (`v1/tests/fixtures/skpatch_hosts_sudo_commands.json`); a fleet compliance check
  (drift, coverage, dashboard, sudo denials) with a systemd timer template; alerts that resolve
  themselves; optional Talk routing; a DNS-server host must not run NetBird DNS.
- **Instance action:** rerun `provision_skpatch_hosts_nodes.yml` (`--check --diff` first). An estate
  that was already provisioned from the #406 branch sees no sudoers change.

### skpatch-hosts: swarm managers upgrade the Docker engine one at a time, for real

- **skpatch-hosts: new APT hook `sk-swarm-upgrade-gate` on swarm managers**, installed by
  `provision_skpatch_hosts_nodes.yml`. The dashboard's chains only wait for apt to exit, so three
  managers in one schedule restarted dockerd within about 90 seconds. On a batch containing
  `docker-ce` or `containerd.io` the hook now refuses while the swarm is held, waits a 600 s gap
  after the previous manager, requires all managers Reachable, and after the upgrade waits (900 s
  max) for this node Ready/Active, all managers Reachable and every service at desired replicas.
  A timeout sets the node label `sk.skpatch.hold`, which stops every later manager. Knobs
  `SWARM_GATE*`, opt-out `skpatch_hosts_swarm_gate: false`. README "Swarm upgrade gate". Tests:
  `v1/tests/test_skpatch_swarm_upgrade_gate.py` (runs the real script on a stub swarm and a
  virtual clock). **Instance action**: rerun `provision_skpatch_hosts_nodes.yml` for the managers.

### skhub: the Recognize model check asks Nextcloud where the app is

- **skhub: the "models already present" check uses `occ app:getpath recognize`** instead of a
  hardcoded `custom_apps/recognize/models` (dev, staging, prod). Where the image ships Recognize
  under `apps/`, the old check never saw the models, so every deploy ran
  `occ recognize:download-models` (over 1 GB, deleting the models first; an hour and more on a lab
  cluster). If `app:getpath` fails the old path is the fallback. Tests:
  `v1/tests/test_skhub_recognize_models_path.py` (runs the task's shell against a stub docker).
  **Instance action**: none.

### skpdf: Stirling-PDF updates stop-first

- **skpdf: `update_config.order` is `stop-first`, not `start-first`.** Stirling-PDF is one replica
  whose state is files on shared storage (`/configs`, pipeline, logs). start-first ran the new
  container next to the old one on the same files and needed two memory reservations at once, so
  an update could stall at 0/1. The router baselines for skpdf carry the same one-line change.
  Tests: `v1/tests/test_skpdf_update_order.py`. **Instance action**: none; the next skpdf deploy
  applies it (a few seconds of downtime per update instead of an overlap).

### skgit: the postgres runtime directory is created 0700

- **skgit: `/var/data/runtime/<app>-<env>/postgres` is created 0700, not 0755.** With a pinned postgres
  older than 18 that directory is PGDATA itself, and PostgreSQL refuses to start on a group- or
  world-readable data directory. Each deploy reset it to 0755 and only the image entrypoint's own
  chmod on the next container start put it back, so a start without that step would fail. With
  postgres 18+ (the default) it is the mount's parent, where 0700 is also fine. Tests:
  `v1/tests/test_skgit_pgdata_mode.py`. **Instance action**: none; the next skgit deploy applies it.

## skstacks-v2.26.25 - 2026-11-17

Patch line on v2.26.24 (same base and release-line commits, plus #431 only).

### sksec on single-node installs: skfence and skmesh can use the bouncer

- **skfence: `CROWDSEC_ENABLED` (default false).** Puts `crowdsec-bouncer@file` first in the
  `default@file` chain, the same switch skfenceha has, so a single-node estate running sksec can
  protect every router on `default@file`. **Instance action**: none by default. To use it, deploy
  sksec first, then set `skfence.CROWDSEC_ENABLED: true` and redeploy skfence.
- **skmesh: `TRAEFIK_MIDDLEWARES` (default empty).** Middlewares for every skmesh HTTP router
  (management API and gRPC, oauth2, signal, relay, dashboard); the routers had no middlewares label
  at all. Empty renders as before. **Instance action**: none by default. To put skmesh behind
  CrowdSec, set `skmesh.TRAEFIK_MIDDLEWARES: [crowdsec-bouncer@file]` after sksec is deployed and
  redeploy skmesh. Tests: `v1/tests/test_sksec_single_node_knobs.py`.

## skstacks-v2.26.24 - 2026-11-17

Patch line on v2.26.23 (same base and release-line commits, no other changes from main).

### skgit: API and git paths bypass error pages; forgejo runs under init

- **skgit: new `skgit-api` router** (priority 150) for `/api/` and the git smart-HTTP and LFS paths. Its
  chain is the main chain with `default@file` / `default-no-crowdsec@file` swapped for
  `default-no-error-pages@file`, so CrowdSec, the rate limit and the security headers stay, but API and git
  clients get Forgejo's own JSON errors instead of the HTML error page. The new `skgit.API_MIDDLEWARES` knob
  replaces the derived chain. The runner RPC router's default chain is now `default-no-error-pages@file`.
  The web UI keeps its error pages.
- **skgit: `init: true` on the forgejo service.** The entrypoint override made forgejo PID 1, and it never
  reaped reparented git children (one instance gathered about 5,300 zombies in 23 hours, toward the pids
  limit, after which forks fail and pushes stall). Docker's init now reaps them.
- **Instance action**: redeploy skgit. The forgejo task restarts once.

## skstacks-v2.26.23 - 2026-11-16

Patch line on v2.26.22 (same base and release-line commits, no other changes from main).

### skfence, skfenceha: Traefik v3.7.19

- **Traefik v3.7.18 to v3.7.19**, pinned by tag and index digest
  (`traefik:v3.7.19@sha256:aa942a00...`). A patch release with dependency bumps only: quic-go
  v0.65.0 (HTTP/3), klauspost/compress v1.20.4 (Compress middleware) and golang.org/x/crypto
  v0.59.0. No configuration change. Needs the operator: redeploy skfence or skfenceha (skfenceha
  rolls `traefik-worker` one task at a time, so expect a short edge blip). An instance that pins
  `skfence.TRAEFIK_IMAGE` or `skfenceha.TRAEFIK_IMAGE` keeps its own pin: move it.

## skstacks-v2.26.22 - 2026-11-15

Security-only line on v2.26.21 (same base, no other changes from main).

### skgit: registration and anonymous-read knobs (security)

- **skgit: `DISABLE_REGISTRATION` and `REQUIRE_SIGNIN_VIEW`.** Forgejo defaults to open
  self-registration and anonymous read of every public repo, and `app.ini.j2` had no `[service]`
  section, so an instance could only close them with a live edit that the next deploy reverted.
  Either key now renders a `[service]` section with that setting (lowercase boolean). Unset renders
  byte-identical to before (no `[service]` section). Tests: `v1/tests/test_skgit_service_section.py`.
  **Instance action**: none; an internet-facing skgit should set both to `true` and redeploy skgit.
- **skgit: the deploy's pre-admin wait probes `/api/healthz`.** With `REQUIRE_SIGNIN_VIEW: true`
  every anonymous `/api/v1` request answers 403, so the old wait on `/api/v1/version` never turned
  green and the deploy timed out after `first_install_wait_seconds`. `/api/healthz` stays public and
  only answers 200 once the database is reachable. Found by the v2.26.22 lab gate.

### Portal tiles and skfenceha dashboard

- **Portal tile icons are Font Awesome 5 names.** The Authentik portal only draws FA5 names, so the
  FA6-only defaults (`fa-shield-halved`, `fa-house`, `fa-magnifying-glass`, `fa-gears`, `fa-list-check`,
  `fa-circle-info`, `fa-gauge`, `fa-heart-pulse`, skmon's Jaeger `fa-diagram-project`) rendered blank.
  They now use FA5 equivalents; `v1/tests/test_portal_tile_icons_fa5.py` guards it.
- **The tile task keeps a tile's portal section and description when the role leaves them unset**
  (`AUTHENTIK_TILE_CATEGORY` empty no longer blanks a section set through the API).
- **skfenceha: `DASHBOARD_AUTH_MODE` (`sso` default, `basic`, `lan`).** In `sso` mode only the
  forward-auth middleware gates the Traefik dashboard and no `traefikAuth` basic-auth middleware or
  htpasswd is rendered. **Needs the operator**: an instance that relies on the dashboard's Basic auth
  sets `skfenceha.DASHBOARD_AUTH_MODE: basic` before redeploying skfenceha, or it becomes SSO only.
- **skfenceha refuses to render dynamic config while a subdirectory sits in `dynamic/` or
  `dynamic_acme/`.** Traefik reads them recursively and the first definition wins, so a leftover
  backup directory silently shadowed every later router or middleware change. Move such directories
  out before deploying.

## skstacks-v2.26.21 - 2026-11-14

### skfence, skfenceha: Traefik v3.7.18 (security)

- **Traefik v3.7.16 to v3.7.18**, pinned by tag and index digest
  (`traefik:v3.7.18@sha256:9c437ac4...`). v3.7.17 and v3.7.18 fix five high-severity advisories:
  GHSA-xv5x-242w-7hf7 (per-connection backend transport leak from unauthenticated Negotiate/NTLM
  headers), GHSA-ccgh-9g52-6vwg (ingress-nginx client certificate verification bypass),
  GHSA-cp28-5662-543f (`sanitizePath` left `/..;x/` dot-segments, a path-based authorization bypass),
  GHSA-p956-6gvv-mxqp (ForwardAuth NTLM bypass via Proxy-Authorization) and GHSA-x5pf-cqh3-2gv8
  (Compress middleware zstd memory-exhaustion DoS). Needs the operator: redeploy skfence or skfenceha
  (skfenceha rolls `traefik-worker` one task at a time, so expect a short edge blip). An instance that
  pins `skfence.TRAEFIK_IMAGE` or `skfenceha.TRAEFIK_IMAGE` keeps its own pin: move it. Upstream
  behaviour changes that can matter: BasicAuth now rejects passwords over 1024 bytes with 401; with
  `sanitizePath` on (the default) a `.`, `..` or empty segment carrying path parameters is resolved like
  a plain dot-segment; Go 1.27 drops the `tlsrsakex`, `tls10server` and `tls3des` GODEBUG settings
  (unused here) and rejects requests with more than 500 header values (431).

## skstacks-v2.26.20 - 2026-11-11

### Known issues (fixed in the next release)

- **skhub, MariaDB 10.11 to 12.3:** the first start's automatic upgrade fails with
  `Unknown server host 'db'` and the db restarts in a loop. The db reads the env file
  Nextcloud uses, which sets `MYSQL_HOST=db`, and the mariadb client honours it. Workaround:
  `docker service update --env-rm MYSQL_HOST <stack>_db`. The upgrade then completes, and a
  later deploy putting the variable back is harmless. A fresh 12.3 install is not affected.
- **skreg:** on an instance whose `skreg-<env>` network was created without
  `encrypted=false`, the deploy fails at "Ensure skreg-<env> overlay network exists" with
  `network ... is in use by service`, before anything changes. Stay on registry 2 until the
  next release, which drops that driver option.

### Upgrade notes (instances)

- **Portal tiles and OIDC app admins default to the `skadmin` group, not `authentik Admins`**: the
  `AUTHENTIK_TILE_GROUP` defaults (every role with a portal tile, plus the shared
  `authentik_portal_tile.yml`) and the `OIDC_ADMIN_GROUPS` defaults (sksec-web, skpatch-hosts) are now
  `skadmin`. `authentik Admins` stays Authentik's own administration group, and the automation API
  account still lives there. An instance that wants the old binding sets the knob explicitly.
  `skadmin` must already exist in Authentik. Tiles already bound to `authentik Admins` are not moved
  automatically, so move them once in the admin UI or with `ak shell`.

- **Renovate majors, wave 2 (data stores)**: back up each stack's data before deploying it. None of
  these can be rolled back by repinning alone once the new major has written its data.
  - **Postgres 18 for skgit, skmesh, sksso and skorch (server and dump sidecar; skorch: pgvector pg18
    and postgres-backup-local 18).** 18+ images mount the data directory at `/var/lib/postgresql`
    (PGDATA `/var/lib/postgresql/18/docker`); a pinned older image keeps `/var/lib/postgresql/data`.
    A new shared guard (`v1/ansible/shared/tasks/postgres_major_guard.yml`) reads `PG_VERSION` and
    refuses the deploy, changing nothing, when the data on disk is another major. **Instance action**
    for every existing skgit, skmesh, sksso or skorch: pin `POSTGRES_IMAGE` and
    `POSTGRES_BACKUP_IMAGE` to the 16 images to stay, or follow "Upgrading Postgres to 18" in the
    role README (dump, move the old directory aside, deploy fresh, restore, verify). skgallery
    (Immich's own image), skmem-pg and v2 skdata (no pg18 build yet) are not moved.
  - **MariaDB 10.11 to 12.3.3 LTS for skhub and skbook (not 13.0, a rolling release Nextcloud 35
    does not support).** Both env files now set `MARIADB_AUTO_UPGRADE=1`, so the first start upgrades
    the datadir in place (the image saves a system-tables backup first). Healthchecks use
    `mariadb-admin`. There is no downgrade: take a dump first. To stay, pin `skhub.MARIADB_IMAGE` or
    the new `skbook.MARIADB_IMAGE`.
    12.x keeps a `tc.log`. On a clean shutdown it overwrites the first magic byte with `A` and then
    deletes the file; on an NFS datadir that delete does not always land, and every later start aborts
    with "Bad magic header in tc log" (found twice by the v2.26.20 gate). The `db` services now start
    through a small wrapper that moves aside only a `tc.log` carrying that closed marker
    (`tc.log.closed-<time>` next to it) and then runs the normal entrypoint. A genuinely crashed log is
    left for MariaDB's own recovery. They also get `stop_grace_period: 120s` (knob
    `DB_STOP_GRACE_PERIOD`) for a slow shutdown on a busy datadir.
  - **Redis 8.10.3 for skgallery, skorch and sksso; valkey 9.1.3 for skpeek; registry 3.1.2 for
    skreg.** In place, no step: a newer server reads the older RDB/AOF, but the old major cannot read
    what the new one saves. New pin knobs `skorch.REDIS_IMAGE`, `sksso.REDIS_IMAGE`, `skreg_image`
    (skgallery `REDIS_VERSION` and skpeek `redis_image` already existed). Registry 3 drops the `oss`
    and `swift` drivers and reads its config from `/etc/distribution/config.yml`; the skreg template
    uses none of that.
  - **promtail 2.9.17 to 3.6.11 (skmon).** Matches Loki 3. Promtail is end of life upstream; a move
    to Grafana Alloy is future work.
  - **n8n stays on 2.x** (3.x is still an upstream pre-release). Immich postgres updates are
    disabled in Renovate (the image moves with `IMMICH_VERSION`).

- **skorch: the postgres healthcheck runs as the configured user**: it used `$POSTGRES_USER`, which
  docker stack deploy fills in from the deployer's shell (unset), so the probe ran with an empty
  user and database and the server logged `FATAL: role "-d" does not exist` every 10 s since
  v2.18.0. It now uses `$${POSTGRES_USER}`. A new test scans every `pg_isready` healthcheck for a
  deploy-time variable.

- **skmesh: NetBird 0.82.0 (dashboard v2.96.0), configurable `AUTHENTIK_SUB_MODE`, and the site
  root registered as a redirect URI**: the provisioner used to force `sub_mode: user_id`, which on a
  provider created as `hashed_user_id` turned the owner's next login into a new user without the
  owner role. `AUTHENTIK_SUB_MODE` (default `user_id`) keeps what the instance uses; unknown values
  fail before Authentik is touched. The dashboard's main login sends `https://<host>/` as its
  redirect URI, so it is now in the strict list (it was dropped by every redeploy, breaking SSO
  login). `AUTHENTIK_EXTRA_REDIRECT_URIS` adds more. **Instance action**: check the NetBird user ids
  (64 hex characters means `hashed_user_id`) and set `AUTHENTIK_SUB_MODE` to match before deploying.

- **skpatch-hosts: schedules, notification channels, upgrade mode and key rotation as code**:
  the registrar run of `provision_skpatch_hosts_nodes.yml` now also converges, by name and never
  deleting, `SCHEDULES` (or `skpatch_hosts_schedules`), `NOTIFICATIONS`, the APT `UPGRADE_MODE` per
  system, and with `SYNC_CREDENTIAL: true` rewrites the estate credential's key (rotation, SOP §5).
  Hosts can set `skpatch_hosts_dashboard_address`/`_port`. Nothing changes for an instance that sets
  none of these. Armed hosts (`ALLOW_ACTIONS`) gain one sudoers line, `snap refresh`: rerun the
  provisioning playbook to pick it up. README "Schedules and notifications as code" lists what the
  pinned app does with schedules (serial update chains, reboots that do not wait, the Proxmox guard,
  the Telegram poller) that an estate's schedule design has to respect.

## skstacks-v2.26.19 - 2026-11-10

### Upgrade notes (instances)

- **Renovate majors, wave 1 (internet-facing stacks)**: back up each stack before deploying it.
  - **skhub: Nextcloud default 31.0.14 to 35.0.2.** Nextcloud only upgrades one major at a time, so a new
    pre-deploy guard (`tasks/major_upgrade_guard.yml`) fails the play on a skipped major or a downgrade,
    and leaves the running stack untouched. An instance on 34 can take 35 directly. An instance on an
    older major pins `NEXTCLOUD_IMAGE` to the next major and steps up one deploy at a time (the README
    lists the 32, 33 and 34 digests). New `tasks/post_upgrade.yml` runs `occ upgrade` only when the DB
    still needs it, `app:update --all` after a major, and adds missing columns and primary keys. Each step
    is several minutes of maintenance mode. Rollback means restoring the backup and pinning the previous
    image.
  - **skhub: Talk TURN and STUN servers are written as typed, sensitive config again.** Talk on
    Nextcloud 29+ declares `turn_servers` and `stun_servers` as sensitive arrays, and occ silently
    refused the old untyped write ("Config value were not updated", exit 0). The task hid that error,
    so Talk ran without our TURN/STUN. The write now uses `--type=array --sensitive -n` when occ supports
    it, reads each key back, and fails the deploy on a refused write. After deploying, check that Talk
    calls connect behind NAT.
  - **skmail: docker-mailserver default to 17.0.0.** No template change was needed, and a test keeps the
    renamed 16 and 17 settings out. Coming from 16.x, the Rspamd Bayes data starts empty (state moves to
    Valkey). Back up `config` and `mailstate`. The restart can take up to the 1800s healthcheck window
    because of the per-mailbox ownership `chown`.
  - **skgit: Forgejo 15.0.9 to 17.0.1, runner 11.3.1 to 13.2.1, dind 27.5.1 to 29.9.3.** A direct 15 to
    17 jump is supported, and migrations run in order. Before deploying, back up and run
    `forgejo manager flush-queues`. After deploying, run `forgejo doctor check --all`. Runner 13 drops the
    old workflow commands (`set-output`, `set-env`, `add-path`) and `${{ gitea.* }}`. An existing dind
    volume keeps its image store.
  - **skorch: n8n stays on 2.x.** n8n 3.x is still an upstream pre-release, so its breaking-change notes
    are parked on #352 for later. The pinned-digest test no longer hardcodes the major version.
  - **Undeployed descriptors (v2 tree, skboard)**: authentik 2026.8, valkey 9, tempo 3, grafana 13,
    gitlab 19, garage 2, vikunja 2.8, terraform cloudflare 5 (resources migrated to
    `cloudflare_dns_record`) and vault 5. Nothing that deploys today uses these files. The upgrade path
    for each is in `v2/infra/tofu/README.md` and in PR #351. FalkorDB 6 was declined because skgraph is
    retired.

- **skpatch-hosts: host auto-registration works again, through a dashboard session instead of an
  API token**: linux-update-dashboard 2026.10.9 refuses API tokens for creating systems and
  credentials, so v2.26.17 refused registration outright. `provision_skpatch_hosts_nodes.yml` now
  runs `register_systems.js` inside the dashboard's own container, which signs a five-minute
  session for an existing dashboard admin with the app's own session key (works in `sso`, `local`
  and `oidc` modes; revoked by that user's "sign out everywhere"). New vault keys:
  `skpatch_hosts.REGISTER_AS` (the admin's username) plus `SSH_PRIVATE_KEY` (the registrar creates
  the SSH credential from it) or `CREDENTIAL_ID` (an existing one; `API_CREDENTIAL_ID` is still
  read). It creates missing systems and never edits or deletes one. **A vault that still sets
  `API_TOKEN` now fails** with these steps; remove it. `PROVISIONER_IMAGE` is no longer used by
  this role.

## skstacks-v2.26.18 - 2026-11-10

### Upgrade notes (instances)

- **skgallery: Postgres and Redis can live on local disk on one pinned node
  (`skgallery.DATA_NODE`, `DB_DATA_PATH`, `REDIS_DATA_PATH`)**: both were hardcoded under
  `/var/data/skgallery-<env>/` (NFS on a typical instance). On an instance (2026-11-09) NFS
  write stalls of 30 to 120 s froze the database and the bullmq job queue with it, so the
  worker lost its job locks and the API stopped answering. With a path outside `/var/data`
  the playbook creates it on `DATA_NODE` (owner 999; 0700 for the database) and pins
  `database` and `redis` there, and refuses to run without `DATA_NODE`. The NFS directories
  are left in place as the rollback copy. Unset renders exactly as before. **Instance action**:
  optional. To move: stop the stack's `database`, `redis` and Immich services, copy both
  directories to the new paths on `DATA_NODE` with ownership and modes kept, set the three
  keys, redeploy, verify, then rename the NFS copies. Tests: `v1/tests/test_skgallery_local_db.py`.
- **skfenceha: traefik-worker rolling restarts drain instead of dropping traffic**: the
  worker publishes host-mode ports, so start-first is impossible, and it stopped with Docker's
  default 10 s grace, the same as Traefik's own default drain: every restart SIGKILLed the
  worker (exit 137) mid-drain and the next node went down 10 s later. On an instance
  (2026-11-09) two restarts refused connections for about 4.5 minutes and failed a Windmill
  flow. Now `stop_grace_period` defaults to 45 s (`TRAEFIK_WORKER_STOP_GRACE`), the delay
  between nodes to 30 s (`TRAEFIK_WORKER_UPDATE_DELAY`, update and rollback), the deploy waits
  up to 900 s for the rolling restart (`WORKER_UPDATE_TIMEOUT`), and the new
  `WORKER_LIFECYCLE` map renders `entryPoints.websecure.transport.lifeCycle` (unset: nothing,
  as before). **Instance action**: recommended: set `WORKER_LIFECYCLE: {graceTimeOut: 30s}`
  (keep it below the stop grace) and redeploy skfenceha. Tests:
  `test_skfenceha_worker_timeouts.py`, `test_skfenceha_worker_resources.py`.
- **skgallery: optional split of Immich's background jobs out of the API
  (`skgallery.SPLIT_WORKERS`)**: Immich runs its API and its job workers (video
  transcoding, thumbnails, metadata) in one container under one CPU limit. With software
  transcoding the encode takes the whole limit, so the API stops reading upload bodies: large
  phone video uploads stall until the client gives up, and Traefik's 10 s health check marks
  the backend down (503s for every client). Seen on an instance on 2026-11-09 with a 9 GB
  iPhone video. With `SPLIT_WORKERS: true` immich-server runs only the API
  (`IMMICH_WORKERS_INCLUDE=api`) and a new `immich-microservices` service runs every other
  worker (`IMMICH_WORKERS_EXCLUDE=api`) under its own limits
  (`MICROSERVICES_CPU_LIMIT`/`_MEMORY_LIMIT`, defaults 4.0 / 4G;
  `MICROSERVICES_CPU_RESERVATION`/`_MEMORY_RESERVATION`, `MICROSERVICES_REPLICAS`). It shares
  the media mounts and the env file, has no router, and has the image's API healthcheck
  disabled. Unset, the stack renders exactly as before. **Instance action**: recommended for
  any instance that takes large video uploads: set `SPLIT_WORKERS: true` and redeploy (the
  API restarts once; do it when no large upload is in flight). Tests:
  `v1/tests/test_skgallery_split_workers.py`.
- **skgallery: the Traefik load-balancer health check can be turned off
  (`skgallery.TRAEFIK_HEALTHCHECK: false`)**: with a single immich-server replica the probe
  has nothing to fail over to. When the API stalled for 30 to 60 s on slow NFS writes
  (2026-11-09), every probe timeout marked the only backend down and Traefik answered every
  phone upload with an instant 503 (358 of them in two bursts), so the iOS app retried
  hundreds of files. Without the probe those requests wait out the stall. Swarm's container
  healthcheck still restarts a dead task; raise `HEALTH_CHECK_RETRIES` if stalls can run past
  its window. Unset keeps the probe, so the stack renders as before. **Instance action**:
  optional; recommended with one immich-server replica and shared storage. Tests:
  `v1/tests/test_skgallery_split_workers.py`.

## skstacks-v2.26.17 - 2026-11-08

### Upgrade notes (instances)

- **skhub: notify_push no longer installs an HTTP error page as its binary**: the manual
  fallback fetched the binary with `curl -sL` and checked it with `test -f`, so a GitHub 504
  saved a 92-byte HTML page as the executable and notify_push crash-looped on "exec format
  error", on every later redeploy too. It now retries, fails on HTTP errors, writes to a `.part`
  file, and checks the ELF header both before skipping the download and after it. Retries span
  about 3 minutes, since GitHub release downloads 504ed on and off for an hour that day. An instance
  already holding a bad file gets it replaced on the next deploy. Found by the v2.26.17 gate.
- **skpatch-hosts: node provisioning no longer fails when host registration is off**: with
  `skpatch_hosts.API_TOKEN` unset, every provision run failed with "'selected_manager' is
  undefined" because ansible-core 2.19+ templates `delegate_to` before `when`. Registration now
  lives in `tasks/register_systems.yml`, included only when it is configured, and picks the first
  answering manager from `swarm_managers` itself: the shared manager selection only kept managers
  inside the play's own hosts, and this play's hosts are the nodes being registered. Its delegated
  tasks now use the manager's own connection (ssh by default): the play runs `connection: local`,
  so they had been running on the controller instead of the manager.
  **Host auto-registration itself is refused for now**: the pinned linux-update-dashboard
  (2026.10.9) rejects every API-token request to create or edit a system (403 "API tokens cannot
  configure SSH connections"), so with `API_TOKEN`/`API_CREDENTIAL_ID` set the provision play fails
  with that explanation. Add nodes in the dashboard until a session-based redesign lands.
  **Instance action**: none.
- **Authentik 2026.2+: OAuth2 providers now set `grant_types`**: Authentik 2026.2 added the
  field and gives a provider created without it an empty list, so every sign-in fails with
  `invalid_request` ("Invalid grant_type for provider"). Providers that existed before the upgrade
  were migrated to every grant type, so only fresh installs broke (caught by the v2.26.17 gate's
  skmesh login). skmesh sets NetBird's code, refresh, client_credentials and device code grants;
  skrac and the sksec-web, skpatch-hosts and skflow blueprints set the code flow. A redeploy narrows
  a migrated provider from all seven grant types to the ones it uses. The skmesh and skrac
  provisioners also stop re-PATCHing `redirect_uris` on every deploy (2026.8 returns each entry with
  an added `redirect_uri_type`). CapAuth user links (`sksso.CAPAUTH_USERS`) are made with
  `ak shell` on Authentik 2026.2+, in the worker container on its node: 2026.8 made the API's
  source-connection `user` read-only, so creating a link over the API answered HTTP 500.
  **Instance action**: none.
- **skstream: cli_debrid pinned to v0.7.81, viewer watchlists as code**: the DUMB and
  cli_debrid configs are seeded once, so per-viewer watchlist sources and the cli_debrid version
  lived only on the media node (nor had six managed Plex Home sources, a friends RSS source and a
  hand-patched `plex_watchlist.py`). New `skstream-converge` runs on `MEDIA_NODE` every deploy
  and upserts only what skstream owns: `CLI_DEBRID_VERSION` (default `v0.7.81`, which carries
  upstream's managed Plex Home fix de5c411f; `''` = no pin), `PLEX_OTHER_WATCHLISTS` (one source
  per viewer with their own token) and `PLEX_FRIENDS_WATCHLIST` (owner's friends RSS feed, Plex
  Pass). A pin also turns off DUMB's standalone CLI Battery (cli_debrid v0.7.29+ runs it
  in-process and has no `cli_battery/main.py`), and points cli_debrid's data paths at
  `/data/cli_debrid` directly (DUMB reinstalls a pinned release on every start and lost its
  `/cli_debrid/data` symlink, so cli_debrid died with a PermissionError). Other sources and UI
  edits are never touched. A new pin restarts `skstream-dumb`; a
  sources-only change restarts cli_debrid alone (the rclone mount stays up). The play now refuses
  a `DUMB_EXTRA_BINDS` entry over `plex_watchlist.py` while a release is pinned.
  **Instance action**: an instance with that patch bind must drop it from the vault in the same
  deploy. The first deploy upgrades cli_debrid (nor: 0.7.23 to 0.7.81): back up
  `<LOCAL_ROOT>/dumb-data/cli_debrid` first. Move viewer tokens into `PLEX_OTHER_WATCHLISTS` and
  set `PLEX_FRIENDS_WATCHLIST: true` where friends should count; existing matching sources are
  adopted by username, not duplicated.
- **Portal tile skips cleanly when sksso is not deployed yet**: the default-on tile provisioner
  joins `sksso-<env>`, so any role deployed before sksso (a fresh cluster, the release lab:
  skvector in the v2.26.17 gate) failed with "network sksso-dev not found". The shared task now
  checks the network first and skips the tile with a warning; the tile appears on the role's next
  deploy after sksso. `portal_tile.require_sksso: true` fails instead. **Instance action**: none.
- **Docs: one agent instructions file, `AGENTS.md`**: the root `CLAUDE.md` shim is removed and
  `AGENTS.md` is reshaped to the sk-standards AGENT_INSTRUCTIONS_STANDARD (commands copied from
  CI). `v2/CLAUDE.md` and `v3/CLAUDE.md` become nested `AGENTS.md` files. Claude Code reads
  `AGENTS.md` natively and skips it when a `CLAUDE.md` sits next to it. **Instance action**: none.
- **Tests: skhealth cost line no longer flakes on a long suite**: the test ran the
  engine inside pytest and held pytest's own peak RSS to the unit's 128 MiB budget,
  which a 5000-test run crossed (129.4 MiB). It now checks the line reports the
  process's own peak. **Instance action**: none.
- **CI: superseded PR runs are cancelled, Renovate rebases only on conflict**: every
  test and scan workflow (not the release or deploy ones) now has a concurrency group
  per workflow and PR, so a new push to a PR cancels its older run; pushes to main
  still queue. `renovate.json` sets `rebaseWhen: conflicted`, so a merge to main no
  longer rebases every open Renovate PR (with strict up-to-date protection that
  queued about six workflows per PR on each merge). **Instance action**: none.
- **sksso: `skworld` branding pack with a working password-recovery flow**: setting
  `sksso.branding: skworld` now copies the SKWorld login look (logo, favicon, orange
  theme) and installs `apply-branding`, which sets the default brand, applies
  Authentik's stock `default-recovery-flow` when it is missing, and links it to the
  login page so "Forgot password?" appears. Recovery email uses sksso's global email
  settings. `tasks/branding.yml` no longer requires `templates/` or
  `nextcloud-theming.sh` in a pack (skipped when absent); the `nam` pack behaves as
  before. **Instance action**: none for an instance without `sksso.branding` or on
  `nam`. To adopt, set `branding: skworld` in the sksso vars, deploy sksso, then run
  `/var/data/sksso-<env>/apply-branding sksso-<env>` once on the node hosting the
  `_server` container.
- **Portal tiles on core roles find sksso's vault again**: skfence and skfenceha (core
  roles) looked for sksso's vault under `core/group_vars/<env>/`, but sksso is optional
  and its vault lives under `optional/group_vars/<env>/`, so their default-on portal tile
  task failed with "Could not find or access ... sksso-..._vault.yml". The shared
  `authentik_portal_tile.yml` now swaps the directory for core roles too. skmesh's tile
  no longer errors when its icon copy result is undefined. **Instance action**: none;
  `portal_tile.authentik_vault_file` still overrides the path.
- **skstream: Plex 1.43.5.11079 (from 1.43.4.10903)**: `PLEX_IMAGE`'s default moves to
  `plexinc/pms-docker:1.43.5.11079-b3b13e506` with its multi-arch digest (the version tag is
  now in the pin too, so the running version is readable from `docker service ls`).
  **Instance action**: none for an instance on the default; the next skstream deploy
  updates Plex. An instance pinning its own `PLEX_IMAGE` is unaffected.
- **skpatch role renamed to skpatch-hosts, plus opt-in host auto-registration**: the
  Ansible role at `v1/ansible/optional/skpatch/` moved to `v1/ansible/optional/skpatch-hosts/`
  (role dir, both deploy and provisioning playbook filenames, the `skpatch_hosts:` vault
  namespace, stack/network/router/default-host names, the `AUTHENTIK_TILE_SLUG` default,
  and sksso's companion OIDC vault keys `SKPATCH_HOSTS_OIDC_CLIENT_SECRET`/`_HOST`/
  `_CLIENT_ID`/`_APP_NAME`); the **skpatch program** name (`docs/skpatch-program.md`, the
  planned `skflow` `f/skpatch` flows) is unchanged -- this role is only the host-patching
  half of it. The portal tile's default name also changes to "SKPatch Hosts" (previously
  "SKPatch (Linux updates)"). **Instance action**: see
  `v1/ansible/optional/skpatch-hosts/SOP.md` "Migrating an existing skpatch instance to
  skpatch-hosts" for the full in-place recipe (rename the vault file and its top-level
  key, deploy the new stack alongside the still-running old one, reprovision under the
  new playbook name, verify, then remove the old stack -- `DATA_PATH` and the provisioned
  `skpatch-ro` account are kept, so no credential or SSH-access loss).

  **Also new on this role**: `provision_skpatch_hosts_nodes.yml`, on every `present` run,
  can now upsert the target group's nodes as dashboard systems through the upstream app's
  own API (`src/skpatch-hosts/systems_sync.py`), so the dashboard is never empty after a
  deploy. Opt-in (`skpatch_hosts.API_TOKEN` + `API_CREDENTIAL_ID`, both unset by default);
  idempotent by the upstream app's own `(hostname, port, username)` uniqueness (confirmed
  against the pinned `linux-update-dashboard` source, `system-service.ts`); a node no
  longer in the target group is reported, never deleted. Researched: the dashboard's API
  tokens are a separate auth path from session-cookie login, so `AUTH_MODE: oidc` with
  password login off does not block them -- no local-admin-password bootstrap is needed.
  What IS unavoidable: every token, regardless of permissions, is barred from
  `/api/credentials` (upstream's own token-scope list), and registering a system needs an
  existing `credentialId`; only a signed-in human can create that credential, through the
  UI, once per estate. See README.md "Automatic host registration" for that one-time
  bootstrap (mint a token with `systems:add`/`systems:edit`, add one SSH credential for
  the estate's existing keypair, put both values in the vault). Tests:
  `v1/tests/test_skpatch_hosts_render.py`, `test_skpatch_hosts_provision.py`,
  `test_skpatch_hosts_systems_sync.py`, `test_sksec_web_skpatch_hosts_oidc_sso.py`.

- **sksso on Authentik 2026.5 or newer**: the worker healthcheck only recognized `manage` and
  `celery` processes, so the worker introduced in 2026.5 (`ak worker`) was marked unhealthy and
  killed by Swarm every few minutes although it was working. It now uses the image's own
  `ak healthcheck`. Redeploy sksso before or with an upgrade to 2026.5+.
- **sksec-web / skpatch new `AUTH_MODE: oidc` (2026-11-06), via sksso as code**: both
  roles default to `AUTH_MODE: sso` and keep doing exactly what they did before
  (byte-identical render when `AUTH_MODE` is unset) -- this is additive, not a default
  change. The new third mode gives the dashboard its own OIDC login against sksso
  (Authentik), with no forward-auth middleware in front and Authentik group membership
  deciding the role (admin/read-only/deny), closing the "sign in to Authentik, then
  still hit the app's own first-run local-account screen" gap both roles' READMEs
  previously called "out of scope". Verified against the pinned upstream source
  (`crowdsec-web-ui` `server/config.ts`/`server/app-auth.ts`,
  `linux-update-dashboard` `server/auth/auth-config.ts`/`server/auth/oidc.ts`), not
  just docs. sksso renders the Authentik side as code (OAuth2 provider + application +
  a private `groups` scope mapping), the same blueprint mechanism skflow (Windmill) already
  uses, gated on a new `sksso.SKSEC_WEB_OIDC_CLIENT_SECRET` / `sksso.SKPATCH_OIDC_CLIENT_SECRET`
  (same value as each app's own `OIDC_CLIENT_SECRET`). The blueprint's Application slug
  defaults to the same router name each role's own portal tile (`AUTHENTIK_TILE_SLUG`)
  already defaults to, so the existing portal-tile provisioner
  (`shared/src/authentik_portal_tile.py`, its provider-preservation logic from #242)
  manages that ONE Authentik Application's tile + group binding instead of creating a
  second, provider-less one. **Instance action**: none to keep current behavior. To
  adopt `oidc` on an existing `sso`/`local` instance: (1) in sksso's vault, set
  `SKSEC_WEB_OIDC_CLIENT_SECRET` / `SKPATCH_OIDC_CLIENT_SECRET` (32+ chars) and
  redeploy sksso; (2) in that app's own vault, set `AUTH_MODE: oidc`, `OIDC_ISSUER`
  (`https://<sso host>/application/o/<client id>/`), and the SAME client secret in
  `OIDC_CLIENT_SECRET`, then redeploy that app. There is no teardown path yet: flipping
  back to `sso`/`local` and removing the secret from sksso's vault stops the blueprint
  from being re-applied, but does not delete the provider/application Authentik already
  has (same limitation skflow's blueprint has had all along); clean that up by hand if
  retiring OIDC for an app rather than redeploying it elsewhere. Tests:
  `v1/tests/test_sksec_web_render.py`, `v1/tests/test_skpatch_render.py`,
  `v1/tests/test_sksec_web_skpatch_oidc_sso.py`.
- **sksec-web config patcher runs with the controller's own Python**: `tasks/patch_app_config_yaml.yml` called a hardcoded `~/.skenv/bin/python3`, so a
  controller without that venv (or without ruamel.yaml in it) failed the deploy with
  `ModuleNotFoundError`. It now uses `ansible_playbook_python`, and the controller's
  packages are listed in the new `v1/ansible/requirements-controller.txt`; a missing
  ruamel.yaml fails with an install hint. `v1/tests/test_no_hardcoded_controller_venv.py`
  keeps any task from hardcoding a venv again. **Instance action**: install
  `v1/ansible/requirements-controller.txt` into the Python that runs `ansible-playbook`
  on each deploy controller (a no-op where ruamel.yaml 0.19.1 is already there).

- **skpatch Phase 1 (image pin audit, 2026-11-06), skgraph/skvector/sksync/skpdf/skorch
  defaults**: a fleet-wide audit of every image still running `:latest` on the nor and
  NAM estates found these five framework defaults were the real gaps (the rest of the
  audited list was already pinned, either as an existing framework default or an
  instance override; `docker service ls` just never shows the `@sha256` suffix, which
  is what made them look unpinned at a glance). `skgraph.FALKORDB_VERSION` (`v4.18.8`),
  `skvector.QDRANT_VERSION` (`v1.17.0`), and `sksync.SYNCTHING_VERSION` (`2.0.13`) kept
  a real version tag but no digest; all three now carry the digest for the version
  running today, a no-op at the next deploy. `skpdf.IMAGE_TAG`'s default was stale
  (`2.14.3`; `3.1.0` is what every instance pinning its own override is actually
  running) and `skorch_cfg.N8N_VERSION`'s default pinned an old digest with no tag
  (`n8nio/n8n@sha256:cae1f28d...`); both are bumped to the version running today
  (`3.1.0`, `2.40.7`) with its digest. **Instance action**: none; an instance pinning
  its own override is unaffected, one relying on the default picks up the version
  bump (skpdf, skorch) or just gains the digest (skgraph, skvector, sksync) on its
  next deploy. `v1/tests/test_no_latest_images.py` already covers all five generically
  (it walks every `v1/ansible/*/*/src/**/*.j2`), so no scope-list change was needed;
  confirmed the five new defaults also match `renovate.json`'s regex managers so
  Renovate can track them going forward.
- **skhub, Talk `turn_servers` 3478 entry**: the post-deploy task now writes `"schemes":"turn"`
  instead of `"turn,turns"` for `<TURN_DOMAIN>:3478`. aio-talk's eturnal never serves TLS on
  3478, so the `turns:<host>:3478` URLs it advertised could never connect (confirmed on nor and
  NAM, 2026-10-25: TLS handshake gets EOF) and only slowed ICE gathering on restricted networks.
  TURN over TLS is the separate `TALK_TURN_TLS_443` entry (`turns:<TURN_DOMAIN>:443`), unchanged.
  **Instance action**: none; the next skhub deploy rewrites the entry.

- **Authentik portal tiles (every role with `AUTHENTIK_TILE`, default on since #231)**:
  `authentik_portal_tile.py` PATCHed an existing Application with `provider: null` on every
  deploy. When the tile's slug is also a provider-backed app (for example skhub's slug used by
  Nextcloud `user_oidc`), that detached the provider, the OIDC discovery URL returned 404 and
  every SSO login through it failed ("Could not reach the OpenID Connect provider"). The tile
  now leaves an existing provider untouched and manages only the tile fields; new tiles are
  still created provider-less. **Instance action**: if a deploy since #231 already detached a
  provider, reattach it once in Authentik (Applications, the app, Provider); deploys no longer
  undo it.

- **skfenceha (HA Traefik edge) and skfence, `DUMPER_IMAGE`**: `traefik-certs-dumper`
  was pinned to `v2.10.0`, which panics (`unsupported key type: ''`) on `acme.json`
  written by Traefik 3.7+ (the account `KeyType` field was dropped). Bumped to
  `v2.11.4` by digest in both roles (tactically live on two estates since
  2026-10-24; this lands the framework pin). **Instance action**: none for the
  version bump itself (the default moves); an instance pinning its own
  `DUMPER_IMAGE`/`skfence.DUMPER_IMAGE` ahead of the framework is unaffected.
- **skfenceha only, new `traefik-certs-watch` sidecar**: `traefik-worker` reads
  `acme.json` once at container start and never again, so a cert renewed or
  newly issued by `traefik-acme` never reached a running worker without
  restarting every worker by hand (confirmed on the NAM estate, 2026-10-24: a
  worker cert expiring 2026-12-14 with nothing to renew it at the edge). Fixed
  by a new global-mode sidecar, one per manager node (same placement as
  `traefik-worker`, no Docker socket, no network), that turns
  `traefik-certs-dumper`'s already-dumped PEM output (mounted into
  `traefik-worker` since this role's first version, but never read by it)
  into a Traefik file-provider `tls.certificates` dynamic file
  (`certs-generated.yml`) that `traefik-worker`'s existing file provider
  already watches. Every node redundantly regenerates and writes the same
  file because the shared dynamic directory is NFS and a write from a
  *different* node never fires another node's `fsnotify` watch (the
  mechanism Traefik's file provider relies on, confirmed from its v3.7
  source: no polling fallback exists); each node's own write to that shared
  file is always a *local* write as far as that same node's own watch is
  concerned, so this sidesteps the limitation instead of needing to defeat
  it. **Instance action**: none to pick up the fix (redeploying skfenceha
  adds the sidecar automatically); `skfenceha.CERTS_WATCH_IMAGE` and
  `skfenceha.CERTS_WATCH_INTERVAL_SECONDS` (default 15) are available to
  override it, and `certs-generated.yml` is now a reserved dynamic file name
  (`EXTRA_DYNAMIC_FILES` on skfenceha cannot use it). See the skfenceha
  README, "Cert reload without a restart".
- **(all apps, test-only):** new render-time test guards against the class of bug #232/#234 covered: Traefik v3's swarm provider skips any service whose labels carry both the deprecated `traefik.docker.*` family and the current `traefik.swarm.*` family at once ("both Docker and Swarm labels are defined" -> "Skip container"), dropping its routers to the error-pages fallback with no other symptom. `test_no_service_emits_both_traefik_docker_and_swarm_labels` (`test_compose_templates_render.py`) renders every app template with an empty context and fails if any service's labels contain both prefixes; a template needing real fixture vars to render is skipped there and relies on its own app-specific test instead. No template in this release carries `traefik.docker.*` (confirmed by a fleet-wide search: zero occurrences), so this is a test-only addition - nothing renders differently. **Instance action:** none.
- **Secret-file-mode test harness (`v1/tests/test_secret_files_not_world_readable.py` +
  `test_secret_file_modes_survive_deploy.py`), no instance action**: this walker's own minimal
  Jinja2 environment (not Ansible's) had no `bool` filter and never seeded `cluster_name`/`domain`
  (real inventory host vars, same gap `env` was already worked around for), so the first `vars:`
  block anywhere in any deploy playbook to use `| bool` or reference `cluster_name`/`domain`
  directly aborted the whole walk with `UndefinedError`/`TemplateAssertionError`, hiding every
  playbook after it. The portal-tile roles' `vars:` blocks were the first to do either. Fixed:
  added a `bool` filter, seeded `cluster_name`/`domain` placeholders in both files' base context,
  and made the custom `Undefined` class silent on stringification and on Python-level `+`
  concatenation (`ChainableUndefined` only covers attribute/item access, not output or arithmetic)
  so a reference to a fact this harness never runs (no real `set_fact`) degrades to an empty
  string instead of aborting the walk. Confirmed the gap itself predates this PR, not something
  this PR invented: the 4 tests pass clean on an unmodified `origin/main` worktree (no `| bool` or
  bare `cluster_name`/`domain` reference inside any existing task's `vars:` block to trip it) and
  only started failing once this PR's portal-tile `vars:` blocks were the first to do either.

- **Render-test harness (`v1/tests/render/render_playbooks.py`), no instance action**: fixes a
  real crash the icon-shipping change below exposed, in test infrastructure only. A `copy: src:
  dest:` task whose source is binary (an icon PNG) was captured through
  `lookup('ansible.builtin.file', ...)`, which Ansible already decodes as UTF-8 with lossy
  escaping for anything that is not (a deprecated behavior removed outright in ansible-core
  2.23); writing that escaped text back out at `unpack()` time then raised `UnicodeEncodeError`
  on the lone surrogate codepoints, failing every render test for any role with a binary `copy:`
  source (`test_skpulse_render.py`, `test_skflow_render.py`, 16 failed + 15 errored). Fixed by
  base64-encoding every `copy:src` file capture (`BINARY_MARK`) and decoding it back to bytes in
  `unpack()`; a text file round-trips exactly as before, a binary one no longer crashes the
  harness. Does not touch a real deploy: `ansible-playbook` itself has always copied binary files
  correctly, this only affected the render harness's own capture-and-replay step.

- **Every role with a web UI** (listed in the Changes entry below): new `AUTHENTIK_TILE`, default
  `true` (on by default: an instance that never sets it gets a tile on its next deploy). Set it to
  `false` per role to opt out. When on, the deploy creates or updates a provider-less Authentik
  `core.Application` bound to a group (`AUTHENTIK_TILE_GROUP`, default `authentik Admins`), with a
  launch URL, icon and open-in-new-tab setting, so the service shows a dashboard tile on the
  Authentik portal. It never touches auth itself: all of these roles already sit behind the
  cluster's domain-wide Traefik ForwardAuth catch-all (or, for skrac/skmesh, provision their own
  OIDC login already), so this only fixes visibility, not access. Needs `sksso.api_token` in
  sksso's own vault (same automation token `optional/skrac` and `optional/skmesh` already use); an
  instance missing it gets a loud, named failure on its next deploy of any of these roles, not a
  silent skip, so upgrading means setting that token first if it is not already set. The only
  explicit opt-out today is `sksso` itself (it is the portal, a tile pointing at itself is
  meaningless). See each role's README "Portal tile (Authentik)" where one exists, or
  `docs/ROLE_AUTHORING_CHECKLIST.md` for the general mechanism.

### Changes

- v1: new optional role **skbom** (`v1/ansible/optional/skbom/`): OWASP Dependency-Track
  (Apache-2.0), the image-patching half of the skpatch program (`docs/skpatch-program.md`): an
  SBOM store matched continuously against NVD/GHSA/OSV, replacing the hand-kept CVE register
  the program's design doc describes retiring. Two upstream images, `dependencytrack/apiserver`
  and `dependencytrack/frontend`, release `4.14.6` (the latest 4.x stable release; v5 is a
  separate major line, see the README "Why 4.x, not 5.x"), both multi-arch and pinned by the
  manifest-list digest, resolved from the Docker Hub registry API and cross-checked by actually
  pulling both images. Its own Postgres uses the identical `postgres:17.7-alpine` digest
  `skflow` defaults to (the two share a registry layer on any node running both) plus a
  `pg_dumpall` sidecar copied from skflow's own dump loop. One host, two Traefik routers:
  `PathPrefix(`/api`)` to `apiserver`, everything else to `frontend`, each with its own
  `loadbalancer.server.port` label on its own service (not one service proxying to the other,
  unlike skgym). Default placement constraint is `node.labels.skbom == true`, not the bare
  `node.role == worker` most optional roles default to, because apiserver needs a node sized
  for its JVM heap (`EXTRA_JAVA_OPTIONS` default `-Xmx3g`); apiserver's own `/data` (a
  secret/keystore) shares Postgres's `DATA_NODE` pin since it is local state too. OIDC login
  against Authentik (Dependency-Track's own, not a forward-auth proxy) is off by default and
  validated all-or-nothing: `OIDC_ENABLED: true` needs both `OIDC_ISSUER` and `OIDC_CLIENT_ID`,
  or the deploy refuses. Health checks confirmed against the live images rather than assumed:
  apiserver's unauthenticated `GET /health` and frontend's `GET /`, both over `curl`/`wget`
  (both images ship a real HTTP client, unlike the thin image sksec-web was burned by). Tests:
  `v1/tests/test_skbom_render.py` (happy path, every knob set, the OIDC all-or-nothing guard,
  the three-path `DATA_NODE` guard, loud failures for a missing/short `DB_PASSWORD` and an
  unpinned image); keeps `v1/tests/test_compose_templates_render.py` (the `traefik.docker.*` /
  `traefik.swarm.*` lint from #235), `v1/tests/test_no_latest_images.py` and
  `v1/tests/test_unique_subnets.py` (new subnets `skbom-{prod,staging,dev}` =
  `172.16.{64,65,66}.0/24`) green. Catalog row + detail section in
  `docs/v1-service-catalog.md`; full 9-section SOP in
  `v1/ansible/optional/skbom/SOP.md`.
- Docs only, no instance action: new `docs/skpatch-program.md` documents the skpatch program as a
  whole (host patching via the existing `skpatch` role, plus the planned image-patching pipeline:
  Trivy SBOM scanning + Dependency-Track packaged as `skbom`, Renovate run as an `skflow`
  (Windmill) `f/skpatch` flow, and a Forgejo Actions runner on a dedicated build host), with the
  four standard architecture diagrams (system context, component view, data-flow with a crypto
  label on every hop, sequence), the cadence/policy table, and the release path (gate, tag the
  exact tested commit, forward-merge, deploy one estate then the other). `v1/ansible/optional/
  skpatch/README.md` is reworked to the `sk-standards` README-as-hub shape and a new
  `v1/ansible/optional/skpatch/SOP.md` carries the full 9-section operating procedure plus a
  mermaid diagram of the role itself, with a `docs-evidence` block (5 hermetic checks). Both
  `docs/v1-service-catalog.md` and `docs/ROLE_AUTHORING_CHECKLIST.md` now link to the new docs;
  the checklist gained a "Role docs (README + SOP)" section naming skpatch as the reference
  implementation of that shape. No role behavior, template, or vault default changed.

- v1: **skhub** the Talk post-deploy task writes `"schemes":"turn"` (was `"turn,turns"`) for the
  `<TURN_DOMAIN>:3478` `turn_servers` entry in all three deploy playbooks; eturnal in aio-talk has
  no TLS listener, so the `turns:` URLs on 3478 were dead. README "Talk HPB TURN" updated. Tests:
  `v1/tests/test_skhub_internal_ingress_and_stun.py` (3478 entry is `turn` only, with and without
  `TALK_TURN_TLS_443`). Verified live on nor: Talk advertising `turn:` udp/tcp 3478 plus `turns:` 443,
  skcall relay-only ICE matrix passes on all three.

- v1: new shared task `shared/tasks/authentik_portal_tile.yml` + `shared/src/authentik_portal_tile.py`:
  idempotently creates/updates a provider-less Authentik `core.Application` and its group policy
  binding (`order: 0`, no policy object), for a service whose auth is already handled by the
  cluster's domain-wide ForwardAuth catch-all and that only needs a portal bookmark. Root cause:
  every such tile on nor today (skmon, skfenceha, skport's visibility tile, alertmanager, prometheus,
  etc.) was hand-created through the Authentik UI/API with no declaration in code, so a newly
  deployed service (skpulse) renders and forward-auths correctly but never appears on a user's
  portal, and nothing catches that at review time. This mirrors the existing provider-creating
  pattern (`optional/skrac/src/skrac/authentik_provision.py`) minus the provider: nothing here
  creates a group (a typo in `AUTHENTIK_TILE_GROUP` fails loudly rather than shipping an invisible
  tile), and nothing here creates an Authentik `core.Application`'s provider field, which stays
  `null`. Also supports a role-shipped icon file (`portal_tile.icon_file`, a path under the role's
  own `src/config/<app>/`): the task copies it into sksso's media volume
  (`/var/data/sksso-<env>/media/public/application-icons/sk/<icon_name><ext>`, `icon_name`
  defaulting to `slug` but normally set explicitly so dev/staging/prod share one uploaded copy
  instead of writing three) and points `meta_icon` at it, falling back to `portal_tile.icon` (a
  `fa://` glyph) when no file is shipped or the copy fails. Authentik 2025.12+ has no API endpoint
  left to set an icon from a URL or upload (the old `set_icon` admin action now answers `405`), so
  a media-volume file is the only way to give a tile a real logo instead of a Font Awesome glyph.
  Tests: `v1/tests/test_authentik_portal_tile.py` (happy path: create on first run, then a true
  no-op on an identical second run; edge case: an update touches only the fields that actually
  changed, and an existing group binding is never duplicated; failure case: a group name Authentik
  does not have raises instead of silently producing an invisible tile, and an HTTP error from the
  API surfaces with its status code).

  **Real logos shipped**: 25 PNGs from `homarr-labs/dashboard-icons` (Apache-2.0; full license
  text and a per-file attribution table in `shared/third_party/dashboard-icons/`) for every wired
  service that has an upstream icon in that catalog, matching the names already uploaded
  by hand on both estates (`alertmanager`, `bookstack`, `forgejo`, `garage`, `grafana`, `immich`,
  `jaeger`, `lidarr`, `meshcentral`, `n8n`, `netbird`, `nextcloud`, `plex`, `portainer`,
  `prometheus`, `prowlarr`, `qdrant`, `radarr`, `searxng`, `sonarr`, `stirling-pdf`, `syncthing`,
  `traefik` (skfence and skfenceha both), `uptime-kuma`, `windmill`) so a redeploy writes to the
  same `application-icons/sk/<name>.png` path an instance may already have instead of orphaning
  it. The remaining wired roles (`skboard`, `skdash`, `skgym`, `skhome`, `skmail`, `skpatch`,
  `skpayment`, `sksec-web`, `skseek`, `skwhoami`) keep their `fa://fa-...` glyph: no checked
  upstream entry for that exact service.

  Wired into every role in `core/` and `optional/` that exposes a Traefik web UI, default
  `AUTHENTIK_TILE: true` (opt-out per role, not opt-in: a tile is a bookmark, gated by its group
  binding, so defaulting it on does not expose anything that was not already reachable):
  `skboard`, `skbook`, `skdash`, `skfence`, `skfenceha`, `skfetch` (one tile per *arr UI: sonarr,
  radarr, lidarr, prowlarr, each with its own icon), `skflow`, `skgallery`, `skgit`, `skgym`,
  `skhome`, `skhub`, `skmail`, `skmon` (one tile per sub-service: Grafana, Prometheus,
  Alertmanager, Jaeger, each with its own icon), `skorch`, `skpatch`, `skpayment`, `skpdf`,
  `skpeek`, `skport`, `skpulse`, `sksec-web`, `skseek`, `skstor`, `skstream`, `sksync`,
  `skvector`, `skwhoami` (one tile per instance: 01/02/03). `sksso` is the one explicit opt-out
  (it is the identity provider rendering the portal itself; see the `# portal-tile: opt-out`
  marker in its deploy playbook).

  `skrac` and `skmesh` already provision their own Authentik provider + application when their
  native OIDC login is turned on (`optional/skrac/tasks/authentik.yml`,
  `optional/skmesh/tasks/authentik.yml`): their deploy wiring is unchanged, but both
  `authentik_provision.py` scripts and both Ansible tasks now carry the same icon mechanism
  (new, optional `cfg["icon"]` → `meta_icon`; the task copies `src/config/<app>/icon.png` to
  sksso's media as `meshcentral.png` / `netbird.png` respectively, `AUTHENTIK_TILE_ICON_FILE` /
  `AUTHENTIK_TILE_ICON_NAME` override, `AUTHENTIK_TILE_ICON` fa:// fallback on a failed copy), so
  they get a real logo too, not just the generic-mechanism roles. Verified:
  `v1/tests/test_skmesh_auth_modes.py` (39 tests) and `v1/tests/test_skrac_render.py` reran clean.

- v1: new `v1/tests/test_portal_tile_lint.py`: scans every role under `core/` and `optional/` for a
  Traefik `Host(` router rule and asserts its `deploy_<app>-prod.yml` declares a tile (the shared
  mechanism above, or a role-owned one like skrac/skmesh's) or carries an explicit
  `# portal-tile: opt-out (<reason>)` marker. This is the enforcement for the upgrade note above:
  a future role that ships a web UI with neither fails this test, naming the role, instead of
  silently repeating the skpulse gap this release fixes. See `docs/ROLE_AUTHORING_CHECKLIST.md`
  (new file) "Portal tile (Authentik)" for what a role author does to pass it. Render-gate fixture:
  `v1/tests/render/vars/<role>.with.sksso.yml` added for every wired role (the fake `sksso.api_token`
  the render harness's real, non-mocked `include_vars` step needs now that the tile defaults on;
  modeled on the existing `skmesh.with.sksso.yml`), so the existing per-role render suites keep
  passing with no change to their own assertions.

- v1: `ldez/traefik-certs-dumper` bumped from `v2.10.0` to `v2.11.4` by digest in
  both `core/skfence/src/config/skfence/skfence.yml.j2` and
  `core/skfenceha/src/config/skfenceha/skfenceha.yml.j2`, each behind a new
  `DUMPER_IMAGE` override knob (same convention as the existing `TRAEFIK_IMAGE`
  knob, two independent vault keys since an instance runs only one of the two
  roles). v2.10.0 panics on the account-level ACME data Traefik 3.7+ writes to
  `acme.json` (the `KeyType` field was removed from the account struct);
  v2.11.4 parses it. Tests:
  `v1/tests/test_dumper_image_knob.py` (default is the fixed version on both
  roles, override replaces it, the two roles' knobs are independent, and
  `traefik-worker`'s existing `/certs:ro` mount -- previously unused -- is
  still there for `traefik-certs-watch` to rely on).

- v1 (skfenceha only): new `traefik-certs-watch` global-mode sidecar
  (`src/config/skfenceha/certs-watch.sh`, `busybox:1.36.1-musl` by digest,
  overridable via `CERTS_WATCH_IMAGE`/`CERTS_WATCH_INTERVAL_SECONDS`) closes
  the gap where a cert `traefik-acme` renews or newly issues never reaches a
  running `traefik-worker`, since the worker only ever reads `acme.json` once
  at startup. The sidecar polls the shared, read-only `/certs` directory
  `traefik-certs-dumper` already populates and -- only when the computed
  result changes -- atomically rewrites a `tls.certificates` file
  (`certs-generated.yml`) into the same shared `dynamic/` directory
  `traefik-worker`'s file provider already watches with `watch: true`. It
  deliberately runs on every manager node rather than once centrally: the
  shared dynamic directory is NFS, and NFS does not propagate a write's
  change notification to a different node's `fsnotify` watch (reproduced on
  the NAM estate, 2026-10-24, and confirmed as inherent to Traefik v3.7's
  file provider by reading its source: `watcher.Add()` is only ever called on
  the configured directory itself, with no polling fallback). Running the
  regenerator on every node instead means every node's own write is a local
  write to that same node's own watch, which always fires regardless of
  backing store -- proven end-to-end against a real, unmodified
  `traefik:v3.7.16` container (the same digest this framework pins): before,
  it served `CN=TRAEFIK DEFAULT CERT` for a test SNI with no matching
  dynamic cert; a cert was generated and the sidecar run once against the
  same shared directory the running container's file provider watches; the
  container's `StartedAt`/`RestartCount` were unchanged throughout, and a
  second TLS handshake for the same SNI then returned the injected
  `CN=test.example.com` cert, with zero restart. Also added
  `certs-generated.yml` to the dynamic-file reserved-name list (skfenceha
  only -- skfence has no such sidecar, since its single Traefik process is
  the same one doing the renewal, with no cross-process staleness to fix)
  and deploy.j2 tracking (`record_preexisting`/`preexisted`, so a fresh
  install does not force-restart it mid-first-boot, same as every other
  service in this stack). Tests: `v1/tests/test_skfenceha_certs_watch.py`
  (render shape: global mode, same placement as `traefik-worker`, pinned
  image + both override knobs, `/certs` read-only vs. `/dynamic-out`
  read-write, no Docker socket, no network, script is POSIX `/bin/sh` with
  no bashisms, script defaults match the compose environment) and
  `v1/tests/test_skfenceha_certs_watch_reserved_name.py` (reserved name
  present on skfenceha in all three envs, absent on skfence, and the
  `certs-watch.sh` deploy task exists with the right mode in all three envs).

## skstacks-v2.26.16 - 2026-11-06

### Upgrade notes (instances)

- **sksso:** none required. New optional `colocate_with_data_node` (default `false`); an instance that does not set it renders exactly as before.
- **skfence, skfenceha:** none required for the bump itself (patch-equivalent for our usage: no breaking change found in buffering, forwardAuth, errors, headers, plugins, swarm provider options or respondingTimeouts between v3.6 and v3.7 for our Docker/Swarm + file-provider setup). Traefik image moves from v3.6.2 to v3.7.16 (this month's security batch: CVE-2026-114635/114637/114639/114640/114642 and GHSA-hrpp-p4fc-wwf8, all fixed by v3.7.14-v3.7.16). An instance that pulls the image fresh on its next deploy picks up the patched build automatically; no vault change needed. New optional `TRAEFIK_IMAGE` (per role: `skfence.TRAEFIK_IMAGE` / `skfenceha.TRAEFIK_IMAGE`, the image had no override knob before this release) lets an instance pin a digest to stage a tactical version ahead of or instead of a full framework release; unset renders the new default above. skfenceha's `traefik-acme` and `traefik-worker` always move together under the one knob.
- **skhub, skbook:** none required; no default image changed. PHP2Shell (CVE-2026-109618, CVE-2026-109619) response: checked both framework defaults live by running them, not by tag name. No patched build exists yet for either (skhub's `nextcloud:31.0.14` default bundles PHP 8.3.30; skbook's `lscr.io/linuxserver/bookstack:v26.09-ls285` bundles PHP 8.5.10, and even `:latest` is only 8.5.11). A patched `nextcloud:34.0.5` exists (current major, same-day emergency rebuild) but is not made the skhub default: that would be a 3-major jump for any instance that never pinned its own `NEXTCLOUD_IMAGE`, and this framework's lab only ever does a fresh skhub install, never proves a version-to-version upgrade, so the jump is unproven and `renovate.json` deliberately never groups major bumps for the same reason. See the skhub and skbook READMEs for the live-verified PHP versions and the pin-forward recommendation for genuinely new installs. Prod (nor, NAM) is unaffected either way: both instances pin `NEXTCLOUD_IMAGE` explicitly in their own vaults and are being hot-patched there directly, independent of this framework default.

- **sksec:** none required. New optional `DATA_NODE`/`CROWDSEC_DB_PATH` pin the CrowdSec LAPI's SQLite db to local disk on one node (default stays the legacy NFS path; a local path without `DATA_NODE` is refused). See the README's "Moving the LAPI db off NFS" for the zero-downtime migration runbook before adopting it on an instance with an existing bouncer/ban history. With `TRAEFIK_HA_MODE` also set, `DATA_NODE` now pins the service to itself alone (the HA acme-master constraint is dropped, not ANDed in) -- see the placement-constraint fix entry below before enabling both on an HA instance.
- **sksso:** `AUTHENTIK_ERROR_REPORTING__ENABLED` now defaults to `false` (was `true`). This is a sovereign stack: Authentik no longer phones home to its upstream Sentry by default. An instance that wants the old behavior sets `sksso.error_reporting_enabled: "true"` explicitly.
- **sksec-web:** new optional role, opt-in; nothing an instance already deploys renders differently because of it. Deploy `core/sksec` first (this role attaches to its `sksec-<env>` network and registers against its LAPI).
- **skgit:** none required. When `skgit.TRAEFIK_MIDDLEWARES` puts `crowdsec-bouncer@file` on the main router, the skgit service also gets a `skgit[-env]-runner-rpc` Traefik router (priority 200) for `/api/actions/runner.v1.RunnerService/` that skips the CrowdSec forward-auth (`skgit.RUNNER_RPC_MIDDLEWARES`, default `default-no-crowdsec@file`). It keeps Forgejo runners working when the CrowdSec LAPI is slow and the bouncer fails closed with 403. Runners still authenticate with their tokens. Without that knob nothing renders differently. An instance that already added the same eight `skgit-runner-rpc` labels by hand sees no change on the next deploy.
- **skflow (Nextcloud integration):** none required. Optional runbook plus `tasks/nextcloud_integration.yml` for instances that run Nextcloud and want file events, Talk notifications and a Talk bot in Windmill; no deploy playbook includes it.
- **skflow, sksso (Windmill SSO):** none required. An instance whose Windmill login was set up by hand sets the same secret in both vaults to adopt it as code; the blueprint matches the existing provider, application and group by name, so nothing is duplicated.
- **skfenceha:** the `traefik-worker` CPU limit default rises from 0.40 to 2.00 CPUs (per manager, the service is global). The next skfenceha deploy restarts the workers one at a time (a brief connection reset per node). To keep the old cap set `skfenceha.TRAEFIK_WORKER_CPUS: "0.40"`. An instance that already ran `docker service update --limit-cpu 2` sees no change.
- **skpatch:** new optional role, opt-in; nothing an instance already deploys renders differently because of it. Ships with `ALLOW_ACTIONS: false` by default and a separate `provision_skpatch_nodes.yml` playbook that must be run (and approved per estate) before the account it creates can reach any node.
- **skwrit:** new optional role, opt-in; nothing an instance already deploys renders differently because of it.
- **skflow:** new optional role, opt-in; nothing an instance already deploys renders differently because of it. An instance that runs a hand-deployed Windmill under the stack name `skflow-<env>` follows the README's "Migrating a hand-deployed Windmill" before its first framework deploy.
- **skpulse:** sign-in now has two modes, `skpulse.AUTH_MODE: sso` (the new default: Authentik forward auth, `SSO_MIDDLEWARE` defaulting to `authentik@file`, on the dashboard/login/`/socket.io/` router only) and `local` (Kuma's own login, no forward auth, no Authentik objects). A skpulse vault that never set `AUTH_MODE` previously rendered one Traefik router with no forward auth at all; it now defaults to `sso` (fail closed), so an instance that wants to keep running with no SSO in front must set `AUTH_MODE: local` on its next deploy. Push monitors (`/api/push/`), public status pages (`/status`, `/status-page`, `/api/status-page/`) and badges (`/api/badge/`) now render on a separate, higher-priority router that never carries SSO in either mode (previously they shared the one router, so an instance that had hand-set `TRAEFIK_MIDDLEWARES` to include a forward-auth middleware had silently broken all three).
- **skpulse:** the `disableAuth` toggle (above) can now be re-run on its own, `--tags authmode` (vault load, `AUTH_MODE` resolve/assert/conflict checks and the toggle itself, nothing else), for the window between an instance's first deploy and whenever its admin account actually gets created through Kuma's own first-run UI. Found on a real instance (nor) stuck showing Kuma's own login behind Authentik 11 days after its admin account was created, because nothing had redeployed it since. **Instance action**: none to adopt; an instance in that exact state can run `--tags authmode` instead of waiting for (or forcing) a full redeploy.
- **skpulse:** a fresh install's deploy now completes Kuma's own first-run setup itself (database type sqlite, then the first admin account, `skadmin`, with a freshly generated password) instead of needing a manual click-through, even behind Authentik's forward-auth gate. The generated password lands in `~/.skstacks-generated/` on the deploying controller (0600, never printed, not committed); move it into the instance vault if anyone needs to sign in to Kuma directly. **Instance action**: none; a no-op for any instance whose database is already configured.
### Changes

- v1: **sksso** new optional `colocate_with_data_node` (default `false`): with `DATA_NODE` also set, adds `node.hostname == <DATA_NODE>` to `server` and `worker` as well as the existing `postgres`/`redis` pin. Root cause: on a multi-node Swarm estate where `server`/`worker` float independently of `DATA_NODE`, every OIDC token-endpoint request's DB/Redis calls cross the overlay network instead of staying on loopback. Measured on two production estates: colocated p50 roughly 0.5s; cross-node p50 1.26s / p95 1.57s / max 2.27s under light recurring load. This eats into a downstream OIDC client's HTTP timeout margin; skrac (MeshCentral)'s pinned `openid-client@5.7.1` defaults to 3.5s with no exposed way to raise it (see skrac README "OIDC login timeout margin"). Default stays `false`: no instance's render changes without opting in, and colocating does make `DATA_NODE` a shared failure domain for `server`/`worker` too (documented trade-off). READMEs: sksso "Colocating server + worker with the data node", skrac "OIDC login timeout margin". Tests: `v1/tests/test_sksso_placement_renders.py` (happy path: all four services pinned; edge case: opted in with `DATA_NODE` unset is a no-op; failure/regression case: `DATA_NODE` set alone, or with `colocate_with_data_node: false` explicitly, renders identically to today).
- v1: **sksec-web** fixes `tasks/register_lapi_machine.yml` undefined-variable crash under `ansible-playbook --check` and under a tool that renders this role's templates without running Ansible for real (skstack06's `tools/dry_render.py`, which drops every shell/command task outright rather than honoring `check_mode`): found live by that tool (`tools/dry_render.sh sksec-web`, run `20261023T192928Z-proof-traefik-v22616`), `'sksw_lapi_machines' is undefined` out of a `set_fact` that read `sksw_lapi_machines.stdout` directly. The three read-only lookups (node discovery, LAPI readiness, the existing-machines listing) are now `check_mode: false` (same convention as `shared/tasks/select_manager_node.yml`), so a real `--check --diff` reports live registration status instead of skipping them; the mutating "Register" task and the "Verify" task that depends on it stay check-mode-skippable on purpose. The listing is resolved through `| default({})` before ever touching `.stdout`, in its own `set_fact`, so the Jinja expression never dereferences a register that might not exist at all (not just be empty) -- the actual class of bug a stub-based render harness cannot catch, since it always supplies SOME register output. Tests: `v1/tests/test_sksec_web_register_lapi_machine_guard.py` (the check_mode markers, and the dry-render path reproduced by running only the tasks that survive dropping every shell/command task, with the registers those tasks would have set deliberately absent, through real Ansible: no crash, `sksw_machine_exists` resolves to `false`).
- v1: **skfence, skfenceha** Traefik `v3.6.2` to `v3.7.16@sha256:abb2b9c1b132b5b0be40d09c85bf8d3dab2a59fa0eb3aa955ab8be5efbb73de6` (this month's security batch: CVE-2026-114635, CVE-2026-114637, CVE-2026-114639, CVE-2026-114640, CVE-2026-114642, and GHSA-hrpp-p4fc-wwf8, fixed across v3.7.14-v3.7.16). Read the v3.6-to-v3.7 migration notes for every area that could touch our middlewares (buffering, forwardAuth, errors, headers, plugins, swarm provider options, respondingTimeouts): no breaking change applies to our Docker/Swarm + file-provider setup (the gated changes are h2c upgrade headers no longer forwarded [v3.7.13], a stricter rootless-target rejection [v3.7.13], CONNECT requests now always 501 [v3.7.15], none of which any of our routes rely on; `underscoreHeadersStrategy`/`aliasHeadersStrategy` is unused in this framework). One real but non-blocking finding: both skfenceha's `authentik` forwardAuth middleware and sksec's `crowdsec-bouncer` forwardAuth middleware set `trustForwardHeader: true`, deprecated since v3.6.14 in favor of entrypoint-level `forwardedHeaders.trustedIPs` (still supported, not removed, in v3.7.16). Deliberately NOT migrated in this release: the replacement needs per-cluster verification of which upstream IPs (Cloudflare, cloudflared) should be trusted at the entrypoint on nor and NAM, which is a real production-networking change independent of this version bump and deserves its own reviewed PR, not a bundled side effect. Tracked for a follow-up. Added `skfence.TRAEFIK_IMAGE` / `skfenceha.TRAEFIK_IMAGE` (the image had no override knob before this release, so an instance could not stage a tactical version ahead of a full framework release): unset renders this release's default; skfenceha's `traefik-acme` and `traefik-worker` always move together under the one knob. Updated: `docs/v1-service-catalog.md`, both roles' READMEs, `v1/tests/test_no_latest_images.py` example string. No render-fixture files reference the literal image string, so no golden-fixture update was needed. Tests: `v1/tests/test_traefik_image_knob.py` (default on both roles, override on each, the two knobs independent).

- v1: **sksec** CrowdSec LAPI db on local disk: new optional `sksec.DATA_NODE` (pins the `crowdsec` service to this node, combined with the existing HA-mode `traefik.acme.master` constraint when `TRAEFIK_HA_MODE` is also set) and `sksec.CROWDSEC_DB_PATH` (default unchanged: `/var/data/runtime/sksec-<env>/crowdsec-db` on NFS; a path outside `/var/data` is refused without `DATA_NODE`, same guard shape as sksso's `DATA_NODE`). With `DATA_NODE` set, the deploy also sets `db_config.use_wal: true` in CrowdSec's `config.yaml` (`tasks/enable_wal.yml`, idempotent, never applied on the NFS path: WAL is not safe on NFS). Root cause: `fbonalair/traefik-crowdsec-bouncer` queries the LAPI for every request and the db's SQLite writes serialize under load on NFS; once a lookup exceeds `CROWDSEC_TIMEOUT` the bouncer fails CLOSED (403) to every protected route (coord card `d6fa93ec`, nor incident 2026-10-21: 437/8900 external requests 403 in 30 minutes, CPU-raised as an interim hotfix in skstack01-prod#129). CrowdSec's own entrypoint re-registers the persisted bouncer key from `sksec.env` at every start and never re-keys an existing one, so a fresh/empty db still gets a working bouncer automatically; migrating the real db file (README "Migrating a running instance", a live `sqlite3 .backup` with no required outage) is still the right default so local bans and CAPI/console sync state are not lost. The config dir (machine credentials, hub data, `acquis.yaml`) is unaffected and stays on shared storage either way. Tests: `v1/tests/test_sksec_data_node_unset.py` (default skip, local dirs created on `DATA_NODE` with 0750, `delegate_to` renders without `DATA_NODE` defined), `v1/tests/test_sksec_crowdsec_db_path.py` (real-Ansible render: byte-identical with the knob unset, bind mount + placement constraints with it set, HA+DATA_NODE combined, local path without `DATA_NODE` refused).
- v1: **sksec** fixes the HA+`DATA_NODE` placement constraint introduced two entries above, found live on the skstack06 lab before it ever reached a real estate: the compose template ANDed `node.labels.traefik.acme.master == true` with `node.hostname == <DATA_NODE>`, requiring both on the SAME node. `skfenceha`'s manager selection (`shared/tasks/select_manager_node.yml`) is an ordered failover pick, not guaranteed to land the acme-master label on any fixed hostname, so the moment it landed on a manager other than `DATA_NODE` the `crowdsec` service became permanently unschedulable ("no suitable node"), with no self-heal. Traced why the constraint existed at all: CrowdSec reads Traefik's access logs from the shared `/var/data` mount the same way on every node (`acquis.yaml.j2` globs both `worker/` and `acme/` subdirectories, polling since NFS gives no inotify event for another node's writes) -- it never needed to run on the acme master specifically. That constraint was there only because, before `DATA_NODE` existed, pinning to the one label Swarm already guaranteed unique was a cheap way to land on a single stable node. `DATA_NODE` is a complete, more direct answer to the same need, so it now pins `node.hostname == DATA_NODE` alone; without `DATA_NODE`, HA mode keeps pinning to the acme-master label, unchanged. `tasks/lapi_allowlist.yml`'s wait-for-the-crowdsec-task loop now also recognizes a `docker service ps` "no suitable node" error and fails with the real cause (check `DATA_NODE` against the current acme-master node) instead of a bare "no Running task yet" timeout. README "Moving the LAPI db off NFS" updated. Tests: `v1/tests/test_sksec_crowdsec_db_path.py` (`test_data_node_wins_over_ha_mode_acme_master` replaces the old combined-constraint assertion; the DATA_NODE-unset HA-mode case is unchanged and still covered by `test_ha_mode_only_renders_the_legacy_single_constraint`), `v1/tests/test_sksec_lapi_allowlist.py` (`test_unschedulable_crowdsec_task_fails_with_a_clear_message`, against a docker test double that reports the real "no suitable node" error text).
- v1: **sksso** `AUTHENTIK_ERROR_REPORTING__ENABLED` defaults to `false` (was the upstream Authentik default of `true`): a sovereign stack should not phone error reports home to Authentik's Sentry by default. `sksso.error_reporting_enabled` still overrides either way. Tests: `v1/tests/test_sksso_error_reporting.py` (unset renders `false`, explicit `"true"`/`"false"` both honored).
- v1: new optional role **sksec-web** (`v1/ansible/optional/sksec-web`): [crowdsec-web-ui](https://github.com/TheDuffman85/crowdsec-web-ui) (AGPL-3.0), a browser dashboard for the cluster's existing `core/sksec` CrowdSec LAPI. Named to match the main role (`core/sksec`) with `-web` appended. One digest-pinned upstream image (`CROWDSEC_WEB_UI_IMAGE`, no default without `@sha256:`), service `app` (one replica, SQLite under `/app/data` for its own session/auth state and a local alert cache; CrowdSec's LAPI stays the source of truth). Attaches to the existing `sksec-<env>` overlay network (no new subnet) and, after deploy, idempotently registers its own CrowdSec machine credential (`cscli machines add`, `sksec_web.LAPI_PASSWORD`, 24+ chars, required, never printed or passed as a literal argv on the container's process list) against that LAPI, distinct from `core/sksec`'s own Traefik bouncer key. Admin-grade by design: `AUTH_MODE` defaults to `sso` (Authentik forward auth, same double-gate pattern as skhome/skgym) and refuses `local` mode without an explicit `ALLOW_LOCAL_PUBLIC_RISK: true`; decisions default to read-only (`sksec_web.READ_ONLY: true` sets the upstream image's own `PERMISSION_READ_ONLY`), and this role never edits `core/sksec`'s `trusted_ips` (the second, LAPI-side gate on decision deletion), documented instead as a manual opt-in. Env var names (`CROWDSEC_URL`/`CROWDSEC_USER`/`CROWDSEC_PASSWORD`/`PERMISSION_READ_ONLY`/`CROWDSEC_LOOKBACK_PERIOD`/`AUTH_SECRET`) verified against the upstream source, not its README (which documents an older, now-wrong naming scheme). `DATA_PATH` on local disk requires `DATA_NODE` (fail closed). Tests: `v1/tests/test_sksec_web_render.py` (real Ansible: defaults, every knob, the `AUTH_MODE`/risk-flag/contradictory-middleware guards, missing/short-`LAPI_PASSWORD` and unpinned-image refusals, dev/staging/prod playbook parity). skstack06 lab stage: see below.
- docs: `v1/docs/sso-user-management.md`, the operator runbook for people and passwords with skhub SSO enabled (add a person or household, claims and the `nextcloud_uid` username mapping, remove, reset, recovery links, MFA, app passwords and service accounts, break-glass, secret rotation, rollback, forward-auth lesson, portal tile sync caveat). Cross-links from the Nextcloud OIDC doc, the provisioning doc and the estate plan. No behavior change.
- docs: `v1/docs/sso-provisioning.md` (Authentik as source of truth for people, user_oidc just-in-time provisioning and group sync, add and deprovision runbooks) and `v1/scripts/sso-nextcloud-deprovision.py` (dry-run-by-default job that disables Nextcloud accounts whose Authentik user is inactive). skflow README: leave `SSO_MIDDLEWARE` empty when Windmill uses native SSO.
- docs: `v1/docs/sso-nextcloud-oidc.md` (Nextcloud SSO with Authentik via native `user_oidc`, existing-account mapping, verification and rollback; why forward auth must not front Nextcloud) and `v1/docs/sso-estate-plan.md` (per-service SSO pattern, bypass lists, groups, break-glass, SSO as code). No behavior change.
- docs: skflow README section "SSO and Nextcloud integration (overview)" (how SSO, the native trigger, the skhub webhook worker, the Talk bot and notifications fit together, service-account and break-glass notes) and the trigger-delay note updated for the webhook worker. No behavior change.
- docs: sksso README section "SSO for skhub and skflow" (application/provider table, the `nextcloud` scope mapping and `nc-*` groups, recovery and SMTP status, forward-auth leftovers, tile sync caveat). No behavior change.
- v1: **skfenceha** `traefik-worker` resources are variables: `TRAEFIK_WORKER_CPUS` (default `2.00`, was a hard-coded `0.40` that throttled large downloads to about 15 MB/s), `TRAEFIK_WORKER_MEMORY_LIMIT` (`384M`), `TRAEFIK_WORKER_CPUS_RESERVATION` (`0.08`), `TRAEFIK_WORKER_MEMORY_RESERVATION` (`96M`). Other services unchanged. Tests: `v1/tests/test_skfenceha_worker_resources.py` (defaults, every knob, empty values, other services untouched). README "Worker resources".
- v1: new optional role **skpatch** (`v1/ansible/optional/skpatch`): [linux-update-dashboard](https://github.com/TheDuffman85/linux-update-dashboard) (AGPL-3.0), a self-hosted dashboard that checks (and, if armed, applies) Linux package updates across a fleet over SSH. One digest-pinned upstream image (`IMAGE`, default pinned, refused without `@sha256:`), service `dashboard` (exactly one replica, `stop-first`, one SQLite file under `DATA_PATH` holding AES-256-GCM-encrypted SSH credentials). `AUTH_MODE: sso` (default, Authentik forward auth on the whole router, no API path carved out) or `local` (the app's own login only), same contradictory-config guards as skhome/skgym. This is the highest-trust app in the catalog (it is designed to hold a key that can SSH into every node it is pointed at), so its privilege model does not rely on the app's own RBAC: `ALLOW_ACTIONS` (vault, default `false`) is metadata only at the swarm-deploy layer and grants nothing by itself. The actual enforcement is a new, separate, idempotent and reversible playbook, `provision_skpatch_nodes.yml`: it creates a dedicated low-privilege account on each targeted node, installs exactly one estate-dedicated SSH key (never a reused admin key), and writes a tight exact-command sudoers rule, adding the upgrade/reboot lines only when that node's own `skpatch_allow_actions` (or, unset, the vault's `ALLOW_ACTIONS`) is true; a host's own `false` always beats the instance-wide knob, which is how hypervisors and a NAS stay permanently report-only regardless. `DATA_PATH` on local disk requires `DATA_NODE` (fail closed). New subnets `skpatch-prod/staging/dev` = `172.16.154/155/156.0/24`. Tests: `v1/tests/test_skpatch_render.py` (real Ansible: defaults, every knob, AUTH_MODE guards, missing-secret and unpinned-image refusals, dev/staging parity) and `v1/tests/test_skpatch_provision.py` (real Ansible, system-mutating tasks stubbed: the per-host actions ceiling always overriding the instance-wide knob, the public-key guard and format check, present/absent). Not yet release-gated by a skstack06 stage; no node provisioned or deployed by this change.
- v1: new optional role **skwrit** (`v1/ansible/optional/skwrit`): the worker of the skwrit matter engine as one digest-pinned service (`SKWRIT_IMAGE`, no default, refused without `@sha256:`) that skflow calls over an existing overlay, plus an optional digest-pinned `nextcloud-mcp`. Settings as `SKWRIT_*` variables; Swarm secrets and the state volume are external and referenced by name only (the play never touches a secret value; `deploy` refuses to start when one is missing); `STACK_NAME` and `WORKER_SERVICE` let an instance keep an existing service DNS name. Stack-local overlay subnets `skwrit-prod/staging/dev` = `172.16.249.0/24`, `172.16.253.0/25`, `172.16.253.128/25`. Tests: `v1/tests/test_skwrit_render.py` (real Ansible: defaults, every knob, naming, refusals, env playbook parity).
- docs: skhub README sections "Webhook worker" and "Single sign-on and user management" (what the worker does and measured effect, why forward auth must not front Nextcloud, claims, groups, app passwords, deprovisioning, break-glass). No behavior change.
- v1: new optional role **skflow** (`v1/ansible/optional/skflow`): [Windmill](https://github.com/windmill-labs/windmill) (AGPLv3), the ratified provider of the `skflow` capability, as a cluster's shared workflow engine. Services `server` (UI/API on 8000, healthcheck `/api/version`), `worker` (`WORKER_REPLICAS`, default 2, `WORKER_GROUP`), optional `worker-native` (`NATIVE_WORKER_REPLICAS`), and its own Postgres (`postgres:17.7-alpine`, stop-first, one replica) with a `pg_dumpall` dump sidecar (`postgres:17.7`, bash loop with temp-file rename and age-based retention, same contract as the other dump sidecars), or an external `DATABASE_URL` instead (then neither Postgres service renders). All images pinned by manifest-list digest; the play refuses one without `@sha256:`. Secrets: `DB_PASSWORD` (16+ chars, URL-encoded into `DATABASE_URL`) and optional `SUPERADMIN_SECRET` (32+ chars, Windmill's own), written only to 0600 env files. Traefik: one router with `TRAEFIK_MIDDLEWARES`; with `SSO_MIDDLEWARE` set, `SSO_EXEMPT_PREFIXES` (default `/api/`) get their own router without forward auth (webhooks and token clients) and `/outpost.goauthentik.io/` on the host is routed by label to sksso's Traefik service (`SSO_OUTPOST_SERVICE`, default `sksso[-env]-server@swarm`), so no hand-placed dynamic file is needed. `POSTGRES_PATH`/`DUMP_PATH` on local disk require `DATA_NODE` (fail closed). Tenants are separate stacks on the attachable `skflow-<env>` overlay. New subnets `skflow-prod/staging/dev` = `172.16.246/247/248.0/24`. Tests: `v1/tests/test_skflow_render.py` (real Ansible: defaults, every knob, external database, URL-encoded password, SSO router split and outpost route per env, empty exempt list, data node pinning, every refusal, image literal parity, env playbook parity) and `v1/tests/test_db_dump_sidecars.py` (the dump loop run for real). Not yet release-gated by a skstack06 stage.
- v1: **skflow + sksso** Windmill single sign-on as code (was hand-applied). sksso: new optional `SKFLOW_OIDC_CLIENT_SECRET` (+ `SKFLOW_HOST`, `SKFLOW_OIDC_CLIENT_ID`, `SKFLOW_OPERATORS_GROUP`, `SKFLOW_APP_NAME`) renders an Authentik blueprint (`blueprints/skflow-windmill.yaml.j2`: OAuth2 provider `skflow-windmill`, application, group `skflow-operators` and policy binding) into `/var/data/config/sksso-<env>/blueprints`, mounted at `/blueprints/custom` in server and worker; the secret is passed as the `SKFLOW_OIDC_CLIENT_SECRET` env var and read with `!Env`, never written into the blueprint. skflow: new optional `SSO_CLIENT_SECRET` (+ `SSO_HOST`, `SSO_CLIENT_ID`, `SSO_DISPLAY_NAME`, `SSO_WORKSPACE_ADMINS`, `PROVISIONER_IMAGE`) runs `src/skflow/sso_provision.py` after the deploy: idempotently sets the global setting `oauths` and adds the listed workspace admins through the Windmill API as the `SUPERADMIN_SECRET` superadmin (both secrets on stdin, `no_log`). Windmill CE allows 10 SSO users. Both off by default: nothing renders differently for an instance that sets neither. README: skflow "Single sign-on" and "Tenant private assets" (how a tenant provides licensed fonts and a border image reproducibly). Render harness: set-pass vars for both roles and Authentik blueprint tags (`!Env`, `!Find`, `!KeyOf`) accepted by the YAML check. Tests: `v1/tests/test_skflow_sso.py` (blueprint content and no secret in it, mounts and env only when enabled, playbook order, provisioner against a fake Windmill: create, idempotent, drift, missing workspace, wrong secret, no secret printed).
- v2: **skflow** descriptor gets a `deploy:` block (Windmill standalone, digest-pinned, `/api/version` healthcheck) and is now deploy-ready. Its secrets become `database_url` and `superadmin_secret`, which the renderer injects as the `DATABASE_URL` and `SUPERADMIN_SECRET` Windmill reads; the previous `windmill_superadmin_password` / `windmill_cookie_secret` placeholders (and the `n8n_*` names in the generated doc) mapped to no Windmill setting.
- v1: **skpulse** `AUTH_MODE: sso|local`. `sso` (default) puts `SSO_MIDDLEWARE` (default `authentik@file`) on the main router (priority 5, above skfenceha's priority-1 catch-all, same convention as skrac/skgym), which covers the dashboard, the login page and `/socket.io/` (not a separate path: it hangs off the same `Host` rule, so forward auth gates it like every other page). `local` is Kuma's own username/password login only. The deploy refuses a `local`-mode vault that also sets `SSO_MIDDLEWARE`, naming the key, never its value. Two more routers at priority 100, neither ever carrying `SSO_MIDDLEWARE`: `<router>-public` (always rendered) for push (`/api/push/`), public status pages (`/status`, `/status-page`, `/api/status-page/`, the status page's own SPA assets under `/assets`, `/upload` and its favicon/manifest/icon files) and badges (`/api/badge/`); `<router>-metrics` (opt-in, `METRICS_BYPASS_SSO`, default false) for `/metrics` only, which still carries Kuma's own HTTP Basic Auth (`apiAuth`) regardless. New `BYPASS_TRAEFIK_MIDDLEWARES` and `METRICS_TRAEFIK_MIDDLEWARES` knobs let the bypass/metrics routers carry a different chain than the main router's `TRAEFIK_MIDDLEWARES`. The play also keeps Kuma's own login (`disableAuth`, a row in Kuma's own database, not an env var) aligned with `AUTH_MODE`: `sso` sets it so Authentik is the only prompt, `local` always forces it back off (fail closed: a cluster that switches back from `sso` to `local` is never left with Kuma's own auth disabled and no forward auth either), gated on the first admin account already existing (never touches an instance mid first-run setup). README "Sign-in, SSO and the first admin". Tests: `v1/tests/test_skpulse_render.py` (real Ansible: the default sso render incl. the bypass router's exact rule and `/socket.io/` carrying no carve-out, the sso spellings, a custom `SSO_MIDDLEWARE`, local mode, the refusal and that it never prints the middleware value, the opt-in metrics router, dev/staging hosts and subnets). `test_router_middlewares_knob.py` no longer covers skpulse (its default rendering changed on purpose), the same way skgym/skrac never joined that generic test. Release-gated by the skstack06 stage `skpulseauth` (run after skfenceha+sksso exist; unauthenticated UI redirected to Authentik, a logged-in session loads the dashboard with the websocket connecting, a push URL answers with no SSO, a public status page loads with no SSO, local mode still works end to end). **Instance action**: see the upgrade note.

The skstack06 harness gains one stage for this release: `skpulseauth` (both auth modes, the bypass routes proven live: push, status page, badge, with the SSO router proven to cover the dashboard and the websocket).

## skstacks-v2.26.14 - 2026-10-21

### Upgrade notes (instances)

- **skhome:** new optional role, opt-in; nothing an instance already deploys renders differently because of it.

### Changes

- v1: new optional role **skhome** (`v1/ansible/optional/skhome`): [HomeCenter](https://github.com/yatzin/home-center) (MIT), a self-hosted home manager (properties, vehicles, equipment, warranties, receipts with OCR, costs, maintenance reminders, optional family health records, an optional AI assistant, all in one SQLite-backed container). One digest-pinned upstream image (`HOMECENTER_IMAGE`, no default without `@sha256:`), service `app` (exactly one replica, `stop-first`) and an optional `backup` sidecar (same digest, no extra pull, a different entrypoint so it never races the app's own migrate/seed start-up) that runs HomeCenter's own backup engine (`npx tsx scripts/backup.ts`, the same code behind Settings -> Data, never a raw copy of the live SQLite file) and verifies every run (`scripts/backup-verify.ts`) before trusting it, into `BACKUP_PATH`, a volume separate from `DATA_PATH`. Schedule: the same `backup_time`/`backup_tz` fixed-daily-time pattern as skhub/skbook, or `BACKUP_FREQUENCY`/`BACKUP_START_DELAY` when unset. `AUTH_MODE: sso` (default, Authentik forward auth on the whole router; health records are sensitive) or `local` (HomeCenter's own accounts only; it has no OIDC of its own either way), with the usual contradictory-config guards. `first_install_wait_seconds` (default `120`) is one knob for both the app healthcheck's `start_period` and the deploy script's own convergence-wait budget (the skhub #213 pattern). `DATA_PATH`/`BACKUP_PATH` on local disk require `DATA_NODE` (fail closed). The AI assistant has no environment variable of its own (upstream: 100% admin-configured in-app, key stored encrypted in the database): pointing it at the cluster's skgateway, if ever wanted, is a post-deploy admin step documented in the README, not a vault knob. New subnets `skhome-prod/staging/dev` = `172.16.175/176/177.0/24`. Tests: `v1/tests/test_skhome_render.py` (real Ansible: defaults, every knob, AUTH_MODE/backup_time/backup_tz guards, missing-secret and unpinned-image refusals, dev/staging parity) and `v1/tests/test_skhome_backup.py` (the backup script's own scheduling/verification logic against a faked `npx tsx`). skstack06 stage `skhome` planned (sso and local modes).

## skstacks-v2.26.13 - 2026-10-17

### Upgrade notes (instances)

- **skhub:** none required. Both new Talk federation keys default to unset/false, which never touches `federation_enabled` or `federation_allowed_groups` (byte-identical post-deploy task behavior). An instance that wants Talk federation to another Nextcloud sets `TALK_FEDERATION_ENABLED: true` (and usually `TALK_FEDERATION_ALLOWED_GROUPS`) on its next skhub deploy; both nor (skstack01) and NAM (namstack01) already have this hot-applied via `occ config:app:set` ahead of the framework catching up, so their next deploy should set the matching vault keys or the deploy will warn (no revert: the task is additive, it never clears an existing value).
- **skhub, skbook:** none required. `backup_time`/`backup_tz` (skhub) and `BACKUP_TIME`/`BACKUP_TZ` (skbook) are optional; unset, every `db-backup` env file and compose render stays byte-identical to v2.26.12 and the service keeps today's sleep-2m-then-sleep-`BACKUP_FREQUENCY` loop. Set them to move an instance to a fixed daily run time.
- **skhub:** none required to keep working; the nextcloud healthcheck `start_period` default moves from a fixed `300s` to `skhub.first_install_wait_seconds` (default `1800s`). An instance that already overrides `skhub.first_install_wait_seconds` keeps its own value for both the ansible wait and the healthcheck automatically; one that does not override it gets a more patient default on every deploy, not just first installs (harmless once Nextcloud is already healthy: `start_period` only matters before the first passing healthcheck).

### Changes

- v1: **skhub** `TALK_FEDERATION_ENABLED` (bool, default `false`) and `TALK_FEDERATION_ALLOWED_GROUPS` (list of local group ids, default `[]`): new "Configure Talk federation" post-deploy task sets Talk's (`spreed`) `federation_enabled` and, when the group list is non-empty, `federation_allowed_groups` via `occ config:app:set`. Needs `enable_talk_hpb`. Context: nor and NAM (two separate Nextcloud clusters) are being cross-federated over each side's public OCM endpoint (`/ocm-provider`, `/.well-known/ocm`; both already reachable in both directions, `cloud_federation_api`/`federatedfilesharing` already enabled on both) so users (including the `lumina` agent on nor) can Talk-chat and call across the two clusters without any network-level tunnel or route between them. Federation is chat + reactions + calls (via each side's own HPB/signaling backend, both already 2.1.1, well above the 2.0 minimum for federated calls) and does not cover federated Talk file sharing. Restricted to a `federation` group on each side rather than every user. README "Talk HPB TURN" section gains the two new rows. Tests: `v1/tests/test_skhub_talk_federation.py` (new: task exists, default/falsey values never run it, `enabled` writes `federation_enabled=yes`, empty vs non-empty `TALK_FEDERATION_ALLOWED_GROUPS`, needs `enable_talk_hpb`, playbooks still parse, README documents both keys). Verified live (hot `occ config:app:set`, ahead of this framework PR, per the task's own instruction for an appconfig-only change under `sk-lock`): both instances' `occ setupchecks` report Talk "Federation: success"; an end-to-end federated-invite test between two throwaway users (one per cluster, deleted after) exchanged a chat message in both directions and both sides' chat history showed both messages.
- v1: **skhub, skbook** `db-backup`: added an optional fixed-time daily schedule. Today's loop sleeps 2 minutes on container start, dumps, prunes, then sleeps `BACKUP_FREQUENCY` (default `1d`/`86400`) and repeats, so the actual run time drifts with whenever the container last started or restarted (Chef wants a predictable 03:00 America/New_York run for skhub). With `skhub.backup_time`/`skbook.BACKUP_TIME` set (24-hour `HH:MM`), the loop instead computes, every iteration, the next occurrence of that wall-clock time in `skhub.backup_tz`/`skbook.BACKUP_TZ` (default `UTC`, an IANA zone such as `America/New_York`) using `today HH:MM`, rolling to `tomorrow HH:MM` once that has already passed, logs `next backup at <time> <tz>`, sleeps exactly that long, then dumps and prunes; `BACKUP_FREQUENCY` is unused in this mode. Recomputing fresh every loop (rather than sleeping a fixed interval) is what keeps it correct across DST: a fall-back day's gap is 25 real hours and a spring-forward day's gap is 23, and GNU `date -d` with `TZ` set reads that zone's wall-clock calendar rather than a cached UTC offset, so it lands on the right one. Verified against the pinned `mariadb:10.11.19` image (and skbook's floating `mariadb:10.11`): both carry `tzdata` and GNU `date`. Validated at render time, after the vault loads: `backup_time`/`BACKUP_TIME` must match `HH:MM` 24-hour, and `backup_tz`/`BACKUP_TZ` must be a zone the control node's `/usr/share/zoneinfo` actually has; either refusal fails the deploy before it touches the stack. The `mariadb-dump` command, `--all-databases`/single-database target, `gzip` pipe and `BACKUP_NUM_KEEP` retention logic are all unchanged. README "db-backup" rows (skhub) and vault example (skbook). Tests: `v1/tests/test_db_backup_fixed_time.py` (new: unset byte-identical render for both services, schedule logic present when set, render-time validation wired after "Load vault file" in all six env playbooks, a shell-level GNU `date` check of the 25h/23h DST deltas).

### Changes (added to the v2.26.13 section, after the db-backup entry)

- v1: **skhub** `nextcloud` Docker healthcheck `start_period` now derives from the existing `skhub.first_install_wait_seconds` knob (default `1800s`, was a fixed `300s`), same pattern as `skmail.HEALTHCHECK_START_PERIOD`. Incident (skstack06 release gate, 2026-10-17/18): the ansible "Wait for the swarm node running the Nextcloud task" step already waits up to `first_install_wait_seconds` (1800s) for a slow first install, but the container's own healthcheck (`interval 120s * retries 3` after `start_period`, previously a fixed 300s => 660s total grace) is what Swarm's `restart_policy` actually watches; on a busy host that 660s window was not enough for the one-time entrypoint copy + first DB install to finish, Swarm killed the container as unhealthy (`exit 137`) and restarted it, resetting progress every time, so the ansible wait never saw a passing Running state even though it was patient enough to wait 1800s. Confirmed live on the skstack06 lab (host load 28-37 on 20 cores both times it recurred): `docker service ps` showed repeated `task: non-zero exit (137): dockerexec: unhealthy container` at ~9-11 minute intervals, and the container log always stopped at the same line ("Initializing nextcloud 31.0.14.1 ..."). `interval`/`timeout`/`retries` and the ansible wait knob are unchanged; only `start_period` now matches the same budget. Tests: `v1/tests/test_skhub_first_install_waits.py` (extended: default start_period, knob-driven, covers the ansible wait budget at several values, interval/timeout/retries/test unchanged).

## skstacks-v2.26.12 - 2026-10-16

### Upgrade notes (instances)

- **skhub, skbook:** none required. The `db-backup` service's `mariadb-dump` command gains flags only; no vault keys change and no instance action is needed. An instance picks this up on its next `skhub`/`skbook` deploy.

### Changes

- v1: **skhub, skbook** `db-backup` service: added `--single-transaction --quick --routines --events` to every `mariadb-dump` invocation. Incident (NAM, 2026-10-16 ~13:13-13:27Z): the daily `skhub-prod_db-backup` run took a per-database table lock for the whole dump (no `--single-transaction`), sat 13+ minutes reading `oc_filecache` into the pipe to `gzip` over NFS, queued 152 Nextcloud queries on "Waiting for table metadata lock", and hung Nextcloud + Talk (status.php timeouts, talk-hpb "context deadline exceeded", Android Talk clients failing) until the on-call killed the dump query by hand. `--single-transaction` dumps InnoDB tables from one consistent snapshot behind a brief metadata lock instead of a table lock held for the dump's whole duration; `--quick` streams rows instead of buffering the full result set in the dump process, bounding memory on large tables. `--routines --events` close a silent gap in the existing `--all-databases`/single-database dumps: stored procedures, functions and the event scheduler were not previously captured. Caveat: `--single-transaction` only guarantees a consistent snapshot for InnoDB tables; Nextcloud's own schema (`oc_*`) is InnoDB-only, so this is safe as deployed, but a MyISAM table present in any database at dump time would not be covered by the single transaction (same caveat `mariadb-dump` has always had; not new risk from this change). `--all-databases` (skhub), the target database argument (skbook), the `gzip` pipe, and the age/count retention logic are all unchanged. Every other optional stack was checked for the same pattern: all other database dump sidecars use `pg_dump` (skgallery, skmem-pg, sksso, skmesh, skgit), which already dumps a single database from one internally-consistent snapshot by design and needed no change. Tests: `v1/tests/test_db_backup_single_transaction.py` (new, render-gated; watched red before the fix, green after).

## skstacks-v2.26.11 - 2026-10-15

### Upgrade notes (instances)

- **skhub:** none. `COLLABORA_IMAGE` is a bump of the default only; an instance that already overrides it is unaffected, and one on the default picks up the new digest on its next skhub deploy with no vault change.

### Changes

- v1: **skhub** `COLLABORA_IMAGE` default bumped to upstream `26.04.5.1.1` (`collabora/code:26.04.5.1.1@sha256:bb8f3c9c5c9016045f25fccb5f0649be94469ebc7c7cc379f3576e470cccf33d`, multi-arch index digest, verified with `docker buildx imagetools inspect`), from `26.04.4.2.1`. Same `26.04` major: the shell-less healthcheck default (`COLLABORA_HEALTHCHECK_TEST`, `coolwsd --probe --use-env-vars`, inc-a2671db2) is unchanged and still correct, no shell/curl in this image either. Upstream notes for 26.04.4.2 to 26.04.5.1: disappearing slide preview thumbnails on mobile landscape fixed, Writer comment-scrolling improved, and the editor's own health check now verifies document-processing readiness rather than bare HTTP reachability (an internal change to the image, not to how `coolwsd --probe` is invoked here). No breaking changes or Docker image structure changes found in the upstream release notes. Tests: `v1/tests/test_skhub_component_images.py::test_collabora_bumped_to_26_04`, `v1/tests/test_skhub_collabora_healthcheck.py::test_default_healthcheck_is_the_26_04_native_probe_not_curl` (both updated to assert the new digest; render-gated).

## skstacks-v2.26.10 - 2026-10-15

### Upgrade notes (instances)

- **skhub:** none. The three new keys are optional; unset or false, every file renders byte-identical to v2.26.9 and the post-deploy Talk task runs the same commands.

### Changes

- v1: **skhub** `INTERNAL_INGRESS_IP`: pins the public skhub hostname (and `skhub[-env].<base domain>`, the name `talk-hpb.env` builds `NC_DOMAIN`/`TALK_HOST` from, when it differs) to an address of the cluster's own Traefik with `extra_hosts`, in `nextcloud`, `cron`, `talk-hpb` and `talk-recording` only. On an instance behind a CDN tunnel the HPB's backend requests and room pings to Nextcloud, and Nextcloud's calls to the HPB, used to leave the cluster and loop back through the tunnel (a production instance logged "connection reset by peer" / "incomplete OCS response" room-ping failures through its single cloudflared replica). URLs, TLS names and certificates are unchanged, so `verify: true` keeps working; aim it at a keepalived VIP that follows a healthy Traefik so it is not a single node. The play refuses a value that is not an IPv4 address, or is `0.x`, `127.x` or broadcast. README "Internal routing to the public hostname". Tests: `v1/tests/test_skhub_internal_ingress_and_stun.py` (default and empty byte-identical in all three envs, the four services pinned and nothing else changed, `SKHUB_HOSTNAME` plus the talk-hpb name, `CLOUDFLARED`, talk/recording off, with the TURN relay wrapper, every refusal). Release-gated by the skstack06 skhub stage check `skhub_internal_ingress` (knob on from a lab vault override: the name resolves to the pinned address inside nextcloud, cron and talk-hpb while DNS answers another address, and a signed HPB backend request from talk-hpb's network namespace is accepted by Nextcloud through it).
- v1: **skhub** `TALK_STUN_SERVERS`: a list of `host:port` strings the post-deploy Talk task writes to `spreed` `stun_servers` (so an instance can use its own eturnal for STUN, together with the existing `TURN_DOMAIN`, `TALK_TURN_PUBLISH_MODE: host`, relay range and `TALK_HPB_PLACEMENT_CONSTRAINTS` for TURN). Unset or empty, `stun_servers` is not touched. The play refuses a non-list, more than 10 entries, an entry that is not `host:port` (port 1-65535, no scheme, no whitespace), and the key without `enable_talk_hpb`. README "Talk HPB TURN" (the `TURN_DOMAIN` row now also says it sets the HPB's `TURN_DOMAIN`, which it has since v2.26.3). Tests: same file (unset/empty task unchanged, the JSON list written, signaling/TURN lines untouched, `no_log` kept, every refusal). Release-gated by the same lab check (the stored `stun_servers` read back through a masking filter, plus the existing TURN allocate on the cluster's own eturnal).
- v1: **skhub** `TALK_TURN_TLS_443`: `true` adds a Traefik TCP router on `websecure` (`HostSNI(TURN_DOMAIN)`, TLS terminated with certresolver `main`) to eturnal's TCP 3478 on talk-hpb, and appends `turns:<TURN_DOMAIN>:443` (tcp) to Talk's `turn_servers`, so clients behind a firewall that only allows 443 can still reach TURN. Default `false` renders byte-identical (no `traefik.tcp` label, one `turn_servers` entry). The play refuses it without `enable_talk_hpb` or without a `TURN_DOMAIN` that is a dotted DNS name (no port, nothing that could break out of the `HostSNI` rule). README "Talk HPB TURN". Tests: same file (six TCP labels on talk-hpb only, the two `turn_servers` entries, every refusal, real Ansible). Release-gated by the same lab check (a TLS TURN Allocate through the HA edge on 443).

## skstacks-v2.26.9 - 2026-10-14

### Upgrade notes (instances)

- **skhub:** a redeploy now refuses to replace the stack when a bind mount on a running skhub service is missing from the new render, and names each `service: target`. An instance that added mounts by hand (`docker service update --mount-add`, or an edit of the rendered `skhub.yml`) must move them into the new `skhub.EXTRA_MOUNTS` before its next skhub deploy, or the play stops; to drop them on purpose set `skhub.ALLOW_MOUNT_DROP: true` for that one deploy. Instances without hand-added mounts render exactly as before.

### Changes

- v1: **skhub** `EXTRA_MOUNTS`: a list of `{source, target, readonly?, propagation?}` host bind mounts rendered into both `nextcloud` and `cron` (long-syntax binds, appended after the fixed mounts), for Nextcloud "Local" external storages on host paths. Hand-added mounts were dropped silently by the next framework redeploy (it re-renders the compose file and replaces the stack), which broke three external storages on a production instance. The play validates the list in `pre_tasks`, before anything is written: a list of mappings, known keys only, absolute paths with no whitespace, colon, comma or trailing slash, unique targets, nothing on, under or above `/var/www/html` and the other fixed nextcloud/cron mount targets, `readonly` a boolean, `propagation` a valid bind propagation. Empty (the default) renders byte-identical to before. README "Extra bind mounts". Tests: `v1/tests/test_skhub_extra_mounts.py` (render, default unchanged, every refusal); the real-Ansible render gate's `skhub.set.yml` sets it.
- v1: **skhub** mount drift guard: before the compose file is rewritten, the play reads the running `skhub-<env>` services' bind mounts (`docker service inspect` of the mount fields only, never the spec Env) and `files/skhub_mount_drift.py` compares them with the same template rendered through Ansible's template lookup. A live bind mount whose target the new render of that service lacks stops the play, naming `service: target (source ...)`, unless `ALLOW_MOUNT_DROP: true` (then it still lists them). Runs under `--check` too (read-only), so a dry run shows it. A changed source for the same target is not a drop; services absent from the render are not judged; unreadable inspect output fails closed. Tests: the incident replayed (three mounts live, a render without them is refused for both services), a hand-added mount, the override, first deploy, ignored volume/tmpfs mounts, the guard's place before the compose write in all three env playbooks. Release-gated by the skstack06 skhub stage check `skhub_extra_mounts` (deploy with `EXTRA_MOUNTS` from a lab vault override, the mount readable inside nextcloud and cron, a redeploy without it refused by the guard, then dropped with `ALLOW_MOUNT_DROP`).

## skstacks-v2.26.8 - 2026-10-14

### Upgrade notes (instances)

- **skrac:** new optional role, opt-in; nothing an instance already deploys renders differently because of it.
- **skgym:** sign-in now has two modes, `skgym.AUTH_MODE: sso` (the new default: Authentik forward auth, `SSO_MIDDLEWARE` defaulting to `authentik@file`, on everything but `/api/`) and `none` (openGym accounts only). A skgym vault without `SSO_MIDDLEWARE` that relied on the v2.26.6 default of no forward auth must set `AUTH_MODE: none` to keep it. Sign-up is now `SIGNUP: auto` by default: open while openGym has no profile, invite-only from the first deploy after one exists. A vault with `INVITE_ONLY: false` keeps sign-up open forever: drop it to get the automatic close.

### Changes

- v1: new optional role **skrac** (`v1/ansible/optional/skrac`): [MeshCentral](https://github.com/Ylianst/MeshCentral) (Apache-2.0) remote access/management, built to replace a decommissioned hand-run MeshCentral instance (its data and agent root certificate gone) and the dead `meshagent.service` agents still polling it. One service, `server` (upstream image, release `1.2.6` published 2026-09-24, multi-arch, pinned by the manifest-list digest; exactly one replica, `stop-first` updates: it holds the NeDB database, every device/user record and the agent root certificate in one data dir), plus `backup` (same image, no extra pull: a tar.gz of the data dir every `BACKUP_FREQUENCY`, `meshcentral-files` as a second, separate archive only with `BACKUP_INCLUDE_FILES`). Sign-in is MeshCentral's own native OIDC against Authentik (`AUTH_MODE: oidc`; `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`, the one required secret, 16+ chars, the play fails loudly without it) with a group claim mapped to `skadmin` (the SKStacks admin-group convention) and local login kept reachable at `/login` for break-glass even with `LOCAL_LOGIN_HIDDEN` hiding the password form, or `AUTH_MODE: local` (default, standalone: MeshCentral local accounts, no SSO in front, no Authentik objects). `SSO_HOST_IP` (oidc only, an `extra_hosts` pin for the issuer host) and `EXTRA_CA_CERT` (a private CA for the IdP, via `NODE_EXTRA_CA_CERTS`) for clusters whose IdP name or certificate is private. Contradictory configs are refused with the offending key names: `AUTH_MODE: local` together with `AUTHENTIK_PROVISION: true`, `SSO_MIDDLEWARE`, `SSO_HOST_IP`, `OIDC_AUTO_REDIRECT`, `LOCAL_LOGIN_HIDDEN` or any `OIDC_ISSUER`/`OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET` value. An optional, opt-in, default-off `tasks/authentik.yml` (`AUTHENTIK_PROVISION: true`) creates/updates the Authentik provider and application through its API, modelled on skmesh's. Traefik terminates TLS as usual and the compose template renders TWO routers: the agent/installer paths (`/agent.ashx`, `/meshrelay.ashx`, `/meshagents`, `/meshsettings.ashx`, `/devicefile.ashx`, `/health.ashx`) on their own router that never carries `SSO_MIDDLEWARE` (a forward-auth layer there would break every agent's websocket upgrade and the unauthenticated installer download), and everything else on the normal router (priority 5, above skfenceha's priority-1 catch-alls) with the optional SSO forward-auth layer; `config.json`'s `tlsOffload`/`trustedProxy`/`aliasPort`/`certUrl` are set so agent connections and the agent's TLS cert-hash pinning both work through the edge (the classic MeshCentral-behind-a-reverse-proxy trap). `TRAEFIK_MIDDLEWARES`/`AGENT_TRAEFIK_MIDDLEWARES` (independent chains), `TLS_OPTIONS`, `DATA_PATH`/`FILES_PATH`/`BACKUP_PATH`/`DATA_NODE` with the skgym-style local-disk guard, `PLACEMENT_CONSTRAINTS`, `HEALTH_CHECK_*` and per-service resource knobs. New subnets `skrac-prod/staging/dev` = `172.16.238/239/245.0/24`. Tests: `v1/tests/test_skrac_render.py` (real Ansible: defaults, every knob, knob-off, the router split with SSO never reaching the agent router, the reverse-proxy config.json settings, missing/short OIDC secret, bad AUTH_MODE, every local-mode SSO conflict, unpinned image, local path without `DATA_NODE`) and `v1/tests/test_skrac_backup.py` (the backup script for real: a good archive, the separate files archive, a failed run, retention). Release-gated by the skstack06 stage `skrac` (both auth modes, each proven by a real login: local first account = site admin; oidc through the lab Authentik, the `skadmin` user lands as site admin and a user outside it does not; the agent route answers in both modes and behind `SSO_MIDDLEWARE`). **Instance action**: none; to use it, add a `skrac:` vault block and follow the README's "Sign-in, SSO and the first admin" and "Re-enrolling the existing meshagents" sections (an existing MeshCentral agent pins the OLD server's root certificate and cannot be re-pointed with a config edit: every agent must be reinstalled from the new server's own "Add Agent" page).
- v1: **skgym** `AUTH_MODE: sso|none` and `SIGNUP: auto|open|invite`. `sso` (default) puts `SSO_MIDDLEWARE` (default `authentik@file`) on the app shell router and keeps `/api/` outside it (the phone app and passkey ceremonies carry no SSO cookie); `none` renders the single host router. `SIGNUP: auto` (default) reads `<DATA_PATH>/db.json` on the data host (`slurp`, `no_log`; missing = no profile) and sets the api's `INVITE_ONLY` from the profile count: open on a fresh instance so the operator creates the first profile (the admin), invite-only on every deploy after that, so closing sign-up after the first admin is "rerun the deploy". `open`/`invite` force it; the older `INVITE_ONLY: true|false` maps to `invite`/`open`. The deploy refuses, naming the keys: an unknown `AUTH_MODE` or `SIGNUP`; `AUTH_MODE: none` with `SSO_MIDDLEWARE`; a forward-auth middleware inside `TRAEFIK_MIDDLEWARES` (it would guard `/api/` too); `SIGNUP` together with `INVITE_ONLY`; invite-only on an instance with no profile (nobody could sign up); a `db.json` that is not a JSON object under `auto`. The play prints the resolved mode and profile count. README "Sign-in, SSO and the first admin". Tests: `v1/tests/test_skgym_render.py` (real Ansible: both modes, the sso spellings, a custom middleware, auto open/closed on 0/1 profiles, the explicit and legacy modes, every refusal). Release-gated: skstack06 stage `skgym` runs both modes (open sign-up through skfenceha; Authentik redirect and a test user's login through the forward-auth outpost). **Instance action**: see the upgrade note.
- v1: **skgym** the default `TRAEFIK_MIDDLEWARES` moves from `default-no-crowdsec@file` to `default-no-error-pages@file`. skfenceha's `error-pages` middleware (inside `default@file` and `default-no-crowdsec@file`) replaced every 400 and 402-599 body with an HTML page, so openGym's JSON errors (403 `invite` once sign-up is closed, 409 `name-taken`, 400 password policy) never reached the app; found by the skstack06 skgym stage (`403` with an HTML body after closing sign-up). With sksec use `crowdsec-bouncer@file,default-no-error-pages@file`. Tests: `test_skgym_render.py` (default chain on both routers). **Instance action**: a vault that sets `TRAEFIK_MIDDLEWARES` to a chain with `error-pages` should switch to `default-no-error-pages@file`.

The skstack06 harness gains two stages for this release: `skrac` (MeshCentral in both auth modes, each proven by a real login, agent route and installer reachable) and `skgym` (both auth modes; sign-up open at the first deploy and closed by the rerun).

## skstacks-v2.26.7 - 2026-10-13

### Upgrade notes (instances)

- **sksec:** the deploy now refuses a `CROWDSEC_AGENT_HOST` without a port (a bare `crowdsec` made the bouncer dial the LAPI on 80 instead of 8080, fail every check and answer 403 on every protected router); leave it unset for `sksec-<env>_crowdsec:8080`, which an empty value now also renders. With skfenceha the next sksec deploy also writes the bouncer middleware (`sksec.yml`) into `dynamic_acme/`, so the ACME-master Traefik loads `crowdsec-bouncer@file` and its routers on the `default` chain stop being disabled (that chain names the bouncer once skfenceha has `CROWDSEC_ENABLED`). Behind a tunnel or CDN (cloudflared), see the sksec README "Behind a tunnel or CDN": skfenceha `TRUSTED_PROXIES` + sksec `BOUNCER_TRUSTED_PROXIES` + `ALLOWLIST_CIDRS` together give CrowdSec and the bouncer the real client address; one new knob, skfenceha `DROP_FORWARDED_HEADERS` (e.g. `[X-Forwarded-Host]`), which removes headers on the `websecure` entrypoint before any router: with `TRUSTED_PROXIES` behind Cloudflare a client-sent `X-Forwarded-Host` otherwise reaches the apps as trusted (Authentik then renders absolute URLs on the forged host). Nothing changes for an instance that sets none of them; `TRUSTED_PROXIES` and `DROP_FORWARDED_HEADERS` are Traefik static config, so restart the workers after setting them.
- **skpayment:** the payment site now sits behind CrowdSec and only the Stripe webhook is exempt. The role renders two routers: the site router (`Host`) with `MIDDLEWARES`, whose default moves from `default-no-crowdsec@file` to `default@file` (CrowdSec when skfenceha has `CROWDSEC_ENABLED`), and a new `skpayment[-env]-webhook` router for the exact path `/webhook/stripe` with `default-no-crowdsec@file` and priority 1000, so Stripe's callers are never banned. The exemption relies on the app's Stripe signature check (every app release verifies it when `STRIPE_WEBHOOK_SECRET` is set; v1.1.0 also refuses replays and refuses every webhook without a secret). The next deploy changes the live router labels. To keep the old single-router layout set `WEBHOOK_PATH: none` and `MIDDLEWARES: default-no-crowdsec@file`.

### Added

- v1: **skpayment** router knobs `WEBHOOK_PATH` (default `/webhook/stripe`, `none` = one router), `WEBHOOK_MIDDLEWARES` (default `default-no-crowdsec@file`), `WEBHOOK_PRIORITY` (default 1000), and an optional Stripe IP allowlist on the webhook router: `WEBHOOK_IP_ALLOWLIST` (default false), `WEBHOOK_SOURCE_RANGES` (default Stripe's published webhook IPs as of 2026-10-13), `WEBHOOK_IP_STRATEGY_DEPTH` (for a proxy in front of Traefik). `MIDDLEWARES` and the webhook knobs are validated (no stray characters in a label). Tests: `v1/tests/test_skpayment_render.py` (+17: both routers, priority order including a long hostname, every knob, non-prod names, invalid values, the IP list identical in every env playbook).

### Fixed

- v1: **sksec** `CROWDSEC_AGENT_HOST` validated as `<host>:<port>` before anything is written; an empty value renders the default instead of an empty host; the bouncer middleware reaches skfenceha's `dynamic_acme/` as well as `dynamic/` (skfence unchanged). README: "Behind a tunnel or CDN" recipe. Tests: `v1/tests/test_sksec_tunnel_real_ip.py` (24).
- v1: **skfenceha** `DROP_FORWARDED_HEADERS` (list of header names, validated): a `drop-forwarded-headers@file` entrypoint middleware on the worker's `websecure`; unset renders byte-identical. Tests: `v1/tests/test_skfenceha_drop_forwarded_headers.py` (11).

The skstack06 skpayment stage gains a check for this release: skfenceha's live Traefik API must show the site router on `default@file` and the webhook router on `Path(/webhook/stripe)` with `default-no-crowdsec@file` at priority 1000 above it, both enabled on one service, and a neighbouring path must land on the site router.

## skstacks-v2.26.6 - 2026-10-13

### Upgrade notes (instances)

- **skmesh:** the default images move from NetBird `0.79.0` to `0.80.0` (management, signal, relay) and the dashboard to `v2.94.0`, each pinned by tag and digest. The 0.80.0 release notes list no breaking change and no manual step; management's automatic schema migration adds an indexed `public_id` column to the policies, routes and network resources tables at start (additive). Take a database dump before the redeploy (the `postgres-db-backup` sidecar's latest, or `pg_dump`). Rolling back to 0.79.0 leaves the columns, which 0.79.0 ignores. An instance that pins `MANAGEMENT_IMAGE`/`SIGNAL_IMAGE`/`RELAY_IMAGE`/`DASHBOARD_IMAGE` in its vault is unaffected.
- **skpayment:** the default app pin moves to smilinTux/skpayments `v1.1.0` (`d3138a2`), so the next deploy installs new app code. The first release (`17b0080`, the previous default) ACCEPTED unsigned Stripe webhooks whenever `STRIPE_WEBHOOK_SECRET` was empty; v1.1.0 refuses them. An instance that overrides `APP_REF`/`APP_SHA256` back to `17b0080` can no longer leave the webhook secret empty (the deploy refuses).
- **skhealth:** a remote `smart_devices` path that does not exist (a wrong by-id name, a swapped disk) used to read as healthy; it now alerts "device not found". After the upgrade, check `skhealth check` on the primary: a stale device list shows up as a PROBLEM.
- **skgallery:** a redeploy no longer force-restarts the database (it restarts only when its image or config changed, through the stack deploy itself).
- **skhub with `smtp_enabled: true`:** the next deploy writes the vault `smtp_*` values into Nextcloud's `config.php` (they used to reach only a first install). On an instance whose mail already works, make the vault values match the live `config.php` first, or the deploy replaces them. `smtp_enabled` unset or false: no change.
- **skgym:** new optional role, opt-in; nothing an instance already deploys renders differently because of it.

### Changes

- v1: **skhub** talk-recording resources knob: `skhub.TALK_RECORDING_CPUS_LIMIT`, `TALK_RECORDING_CPUS_RESERVATION`, `TALK_RECORDING_MEMORY_LIMIT` and `TALK_RECORDING_MEMORY_RESERVATION` set the `talk-recording` swarm limits and reservations, defaulting to the values that were hardcoded (2.0 / 1.0 CPUs, 4G / 1G), so the default render is byte-identical to `skstacks-v2.26.5`. The recorder encodes with software ffmpeg: on an older-generation Xeon without AVX2 a 1080p recording at 2.0 CPUs records at about 2 fps, and a usable one needs about 4-6. README row. Tests: `v1/tests/test_skhub_talk_recording_resources.py`. **Instance action**: none (raise `TALK_RECORDING_CPUS_LIMIT` where recordings stutter). (#168)
- v1: **skhealth** a missing SMART device is a PROBLEM. smartctl answering "No such device"/"No such file", or exit bit 1 (device open failed) without a health line, is "device not found"; exit bit 0 (command line or sudo failure) without a health line is "smartctl could not read it"; the message quotes smartctl's reason line, not its banner. Before, a remote `smart_devices` path that did not exist read as healthy. New optional `skhealth.disks.devices` (local /dev paths a host must have, by-id names): a missing or dangling path alerts "device not found" without being queried; a present one the scan did not list is checked by its path. Unset, `skhealth.json` is byte-identical (the engine script itself changes). Tests: `v1/tests/test_skhealth_missing_device.py` (stub binaries record argv) and render tests. **Instance action**: see the upgrade note. (#169)
- v1: **skhealth** optional signed webhooks: `skhealth.alerts.webhook_hmac_secret` (root-only `secrets.env`, never printed; `skhealth status` shows `webhook_signed` as a boolean) adds `X-Webhook-Signature: <hex HMAC-SHA256 of the raw body>` and an idempotency `X-Request-ID` (`skhealth-<cluster>-<host>-<hash>` over the run's (kind, key) pairs and the saved state's last run: the retry of an undelivered change set repeats it, any later change set gets a new one) to every webhook POST, for receivers that reject unsigned posts (the Hermes webhook platform). Unset: no header, the request and the rendered files are unchanged. Telegram is never signed. The deploy refuses a secret without `webhook_url`. Tests: `v1/tests/test_skhealth_webhook_hmac.py` (a fake webhook that records headers) and render tests. **Instance action**: none; to send alerts to Hermes, set `webhook_url` to its route and `webhook_hmac_secret` to the route's secret. (#170)
- v1: **skmesh** NetBird `0.79.0` to `0.80.0` (management, signal, relay; the latest 0.80.x) and dashboard `v2.94.0` (the release paired with 0.80; `netbirdio/dashboard` publishes semver tags now, so the old untagged `main` digest pin goes), each `tag@sha256:<multi-arch index digest>`. From the 0.80.0 notes and the v0.79.0...v0.80.0 diff: no breaking change, no new or removed management/signal/relay flags, no new explicit migration; the store refactor is code-only; the automatic schema migration adds indexed `public_id` columns (policies, routes, network resources). Security fixes in 0.80 include refusing HTTP redirects on IdP clients and revoking local Dex sessions on password change. The default render changes by the four image lines only. README "Upgrading from 0.79.0"; `docs/v1-service-catalog.md`. Tests: `v1/tests/test_skmesh_turn_relay_modes.py::test_image_defaults`. **Instance action**: see the upgrade note. (#171)
- v1: **skpayment** "Stripe not configured" mode: `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` may both be empty or absent (one without the other is refused). **Security:** the pinned app accepted unsigned webhooks when the webhook secret was empty (anyone could post `checkout.session.completed` and be granted a tier); fixed in smilinTux/skpayments#2, tagged `v1.1.0`, which answers 503 on `/checkout` and `/portal` without a secret key, 503 to every webhook without a webhook secret, 400 to bad or replayed (older than 300 s) signatures, and adds `stripe_configured` to `/health`. The default `APP_REF`/`APP_SHA256` move to v1.1.0; the deploy refuses the empty-webhook-secret mode on the `17b0080` file hash. The default render changes only in the controller fetch script's pinned ref and hash. Tests: `v1/tests/test_skpayment_render.py` (+8), the app's own `tests/test_skpayment.py` (17). Coord card `1c2347e9`. **Instance action**: see the upgrade note. (#172)
- v1: **skgallery** the deploy wrapper force-updated every pre-existing service after the stack deploy, the database included: on every deploy it restarted postgres, killed an in-flight `postgres-db-backup` dump and dropped every Immich DB connection. The database has no bind-mounted config (its env, image, resources and healthcheck are spec fields, so `docker stack deploy` already restarts it exactly when one changed), so it leaves the forced loop; immich-server, machine-learning and redis are forced as before. skhub and sksso were checked: neither forces its database. The same pattern remains in skbook (`db`, `db-backup`), skgit (`postgres`, `db-backup`) and skorch (`postgres`, `postgres-backup`) and is not changed in this release. Tests: `v1/tests/test_skgallery_db_not_force_restarted.py`. **Instance action**: none. (#173)
- v1: **skhub** applies the vault mail settings to Nextcloud on every deploy. The role rendered `SMTP_*` into `skhub.env`, but the Nextcloud image reads them only on its first install, so an existing install had no `mail_*` keys in `config.php` and sent no mail (seen for months on a production instance). With `skhub.smtp_enabled: true` a post-deploy task (`tasks/mail_config.yml`) sets `mail_smtpmode`/`mail_sendmailmode` (`smtp`), `mail_smtphost`, `mail_smtpport` (integer), `mail_smtpsecure`, `mail_smtpauth` (true), `mail_smtpauthtype` (default `LOGIN`), `mail_smtpname`, `mail_smtppassword`, `mail_from_address` and `mail_domain` from the existing vault keys. The settings reach the container on stdin, never on argv; `files/nextcloud-mail-config.php` (run with `php -r` as `www-data`) compares them with `config.php` (the password by sha256) and only on a real difference writes a 0600 temp file, runs `occ config:import` from an argv array and overwrites and removes the file. The task is `no_log`, prints key names only and reports changed only when it wrote. The deploy validates the keys when enabled. Unset or false: nothing runs and the render is byte-identical. README "Mail (SMTP)". Tests: `v1/tests/test_skhub_mail_config.py` (the applier against a fake Nextcloud root with a real `php`; the task through real `ansible-playbook` with a fake `docker` recording argv: the password is never on argv or in the output, a rerun reports changed=0, disabled never calls docker). Release-gated: skstack06 `v226_check_skhub_mail` (lab mailpit SMTP, knob-on redeploy CHANGED, keys in `config.php`, a message delivered, identical redeploy UNCHANGED). **Instance action**: see the upgrade note. (#175)
- v1: new optional role **skgym** (`v1/ansible/optional/skgym`): [openGym](https://github.com/DuarteSantos8/openGym) (AGPL-3.0), a self-hosted gym and body-weight tracker with passkey sign-in. Three services in stack `skgym-<env>`: `api` (upstream `opengym-api`, exactly one replica, `stop-first` updates: it keeps its database in memory and in JSON files under `/data`), `web` (upstream `opengym-web`, nginx on `:8080` serving the app and proxying `/api/` to `api`, one origin for passkeys; `WEB_REPLICAS`) and `backup` (same image as `api`: a tar.gz of `/data` every `BACKUP_FREQUENCY` into `BACKUP_PATH`, written through a verified temp file, retention pruned only after a good archive; layout ready for skbackup's `copy.apps` with `dump: backup`). Images are the upstream releases referenced by digest (`1.4.1`, amd64/arm64); nothing is vendored or rebuilt, because the `web` image carries exercise media licensed to openGym only (README "Upstream, license and images"). The one secret, `SESSION_SECRET`, is required (the play fails loudly without it or under 32 characters) and written to `<DATA_PATH>/secret` (root 0600, `no_log`), so the vault is the source of truth for the cookie-signing key; every other upstream setting is a plain vault key with a default (`INVITE_ONLY`, `FIRST_USER_ADMIN`, `PASSWORD_LOGIN`, `ALLOW_GUEST`, audit and media limits, `COACH_DISABLED` default on, `extra_env`). Router host from the framework's base-domain expressions (`HOSTNAME`, `CLOUDFLARED`), `TRAEFIK_MIDDLEWARES` (default `default-no-crowdsec@file`), `TLS_OPTIONS`, and `SSO_MIDDLEWARE` (forward auth on everything except `/api/`, which the phone app and passkey ceremonies reach without an SSO cookie; openGym has no OIDC). `DATA_PATH`/`BACKUP_PATH`/`DATA_NODE` with the local-disk guard, `PLACEMENT_CONSTRAINTS`/`WEB_PLACEMENT_CONSTRAINTS`, `HEALTH_CHECK_*` and per-service resource knobs. New subnets `skgym-prod/staging/dev` = `172.16.165/166/167.0/24`. Tests: `v1/tests/test_skgym_render.py` (real Ansible: defaults, every knob, knob-off, missing/short secret, unpinned image, local path without `DATA_NODE`) and `v1/tests/test_skgym_backup.py` (the backup script for real). Release-gated: new skstack06 stage `skgym`. **Instance action**: none; to use it, add a `skgym:` vault block and follow the README's first-admin steps.

### Release gate

The skstack06 stages gain checks for this release: every skhealth webhook post validly HMAC-signed with a distinct request id; a missing `disks.devices` path reported "device not found" by the real engine and smartctl; an unsigned skpayment webhook refused (400) through skfenceha, and the Stripe-not-configured mode exercised inside the running container; a skgallery redeploy that keeps the database task and restarts immich-server; the skhub mail knob end to end against a lab mailpit SMTP; and the new skgym role (stage `skgym`). skmesh 0.80 runs through the existing skmesh stage (both auth modes, CapAuth login).

### Also in this tree, not release-gated

- v1: **skbackup** (restic engine replacing Duplicati, #109) is present in this tree from `skstacks-v2.26.0` onward but is not one of the skstack06 release-gate stages: it is covered by unit/render tests only (`v1/tests/test_skbackup_*`), never deployed in the skstack06 lab, and never ran through the fresh full `PROFILE=full` lane-1 run that gates this release. Do not deploy skbackup from this tag until it has been validated separately (a dedicated skstack06 stage or an instance pilot).
- v1: **skhealth** SMART health parsing, RAID, NFS, TLS and remote-host checks are covered by unit tests only (stub commands); the lab has no such hardware. The gate proves the deploy, the alert channels and their signatures, the primary election, the dedupe/recovery path and the missing-device report, not those checks on real devices.
- v1: **skhub** `TALK_RECORDING_*` resource knobs: the gate deploys talk-recording with the defaults only; set values are covered by render tests.
- v1: **skpayment** a real Stripe checkout, signed Stripe events and LiteLLM provisioning are not exercised in the lab (no Stripe account); they are covered by the app's unit tests.

## skstacks-v2.26.5 - 2026-10-11

### Upgrade notes (instances)

- **sksec:** the next sksec deploy creates (or converges) the CrowdSec LAPI allowlist `sksec-operator` from `sksec.ALLOWLIST_CIDRS`. An instance that already created an allowlist by hand under that name keeps it: the deploy adds what is missing and REMOVES every entry not in `ALLOWLIST_CIDRS`, so put every hand-added address into the vault list first (or set `LAPI_ALLOWLIST_NAME` to another name, or `LAPI_ALLOWLIST_ENABLED: false`). The image must be CrowdSec 1.6.8 or later (the default `v1.6.11` is).
- **skhealth, skpayment:** new optional roles, opt-in; nothing an instance already deploys renders differently because of them.

### Changes

- v1: **sksec** syncs `sksec.ALLOWLIST_CIDRS` into a CrowdSec LAPI centralized allowlist (CrowdSec 1.6.8+). The s02-enrich whitelist parser only drops LOCAL events, so community blocklist (CAPI), console blocklist and `cscli decisions add` decisions still reached the bouncer and could ban the operator's own ranges; LAPI applies an allowlist to every decision (a `cscli decisions add` on a listed address is refused). After the stack deploy all three playbooks include `tasks/lapi_allowlist.yml`: resolve the node running the crowdsec task with `docker service ps` (with `TRAEFIK_HA_MODE` that is the ACME master, often not the deploy node) and delegate there, wait up to `sksec.LAPI_WAIT_SECONDS` (default 300) for `cscli lapi status`, then make allowlist `sksec.LAPI_ALLOWLIST_NAME` (default `sksec-operator`) hold exactly `ALLOWLIST_CIDRS`: create it, add missing entries, remove unlisted ones, verify. An empty list empties it; other allowlists are never touched; the task reports changed only when it changed something; `cscli` runs from an argv, never a shell. `sksec.LAPI_ALLOWLIST_ENABLED: false` skips it. `ALLOWLIST_CIDRS` that is not a YAML list now fails the play. README "Allowlist" explains parser vs LAPI allowlist and how to check (`cscli allowlists check <ip>`) and unban (`cscli decisions delete --ip <ip>`). Tests: `v1/tests/test_sksec_lapi_allowlist.py` (the real task file through `ansible-playbook` against a docker test double modelled on `crowdsecurity/crowdsec:v1.6.11`: fresh, idempotent, drift, empty, rename, LAPI wait, LAPI never ready, no running task, non-list input; also checked by hand against a real v1.6.11 container). **Instance action**: see the upgrade note.
- v1: per-router Traefik middleware knobs for the ten optional services whose router chain was hardcoded to `default-no-crowdsec@file`: `skboard`, `skgit`, `skorch`, `skpdf`, `skpeek`, `skport`, `skpulse`, `skseek`, `skstor` and `skvector` read `<svc>.TRAEFIK_MIDDLEWARES`, and skvector's gRPC router `skvector.TRAEFIK_MIDDLEWARES_GRPC` (the `skgallery.TRAEFIK_MIDDLEWARES` convention). The value replaces the whole chain, so keep skpdf's `skpdf[-env]-upload` and skvector's `skvector[-env]-buffering` when overriding. This lets an instance put a router behind `crowdsec-bouncer@file` without hand-editing live labels (which the next deploy reverted). Unset or empty renders byte-identical to `skstacks-v2.26.4` (baselines rendered from the v2.26.4 templates). Documented in `docs/v1-service-catalog.md` ("Router middleware overrides") and the service READMEs; the render gate's set pass drives every knob. Tests: `v1/tests/test_router_middlewares_knob.py`. **Instance action**: none.
- v1: **sksec** bouncer knobs (the bouncer is the fail-closed forwardAuth every router behind `crowdsec-bouncer@file` depends on): `sksec.BOUNCER_REPLICAS` (default 1; more than 1 adds `max_replicas_per_node: 1`, so one node down leaves a bouncer answering); `sksec.BOUNCER_HEALTHCHECK` (default off) with `BOUNCER_HEALTH_CHECK_INTERVAL/_TIMEOUT/_RETRIES/_START_PERIOD`, an explicit healthcheck on the image's own `/healthchecker` (the image already ships a Docker HEALTHCHECK that applies when the knob is off; the knob makes it visible and tunable); `sksec.BOUNCER_TRUSTED_PROXIES` (default unset), rendered as `TRUSTED_PROXIES` into `sksec.env`: unset, the bouncer trusts every hop and takes the leftmost, client-controlled `X-Forwarded-For` entry, so a client could dodge a ban through a CDN-proxied hostname. The play refuses 0 replicas, a non-list, non-address entries, `0.0.0.0/0` and `::/0`. Unset knobs render `sksec.yml` and `sksec.env` byte-identical to baselines rendered from the templates before the change. README "Bouncer resilience" and "Client IP". Tests: `v1/tests/test_sksec_bouncer_knobs.py`. **Instance action**: none; an instance with CDN-proxied hostnames behind the bouncer should set `BOUNCER_TRUSTED_PROXIES`.
- v1: new optional role **skhealth** (`v1/ansible/optional/skhealth`): lightweight infrastructure health checks with alerts, for clusters where a metrics stack (skmon) is too heavy to keep running. One Python script (standard library only) on a systemd timer every 5 minutes on every swarm node: no daemon, no container, the unit capped at `CPUQuota=25%`/`MemoryMax=128M`, every external command by absolute path (a timer's minimal PATH). Checks, each switchable in the vault: swarm services below desired replicas, stuck at 0 or crash-looping; swarm nodes not Ready/Active or managers unreachable; filesystems over a threshold; hung NFS mounts (`timeout 5 stat`); load, MemAvailable and memory pressure; OOM kills since the last run; failed systemd units; local disk SMART health and NVMe critical warning/wear (installs smartmontools, key-gated); ZFS `zpool status -x` and md RAID; read-only checks of extra hosts (a NAS or Proxmox host) over SSH through a forced-command wrapper that permits only `zpool status -x`, `smartctl -H`/`-A` on listed devices and `pvesm status` (the role installs nothing on remote hosts; the README documents the `authorized_keys` line); TLS certificate expiry. The cluster-wide checks run on one primary (lowest-id Ready manager, or a named host). Alerts go to Telegram and/or a webhook on state change (after `confirm_runs`, default 2), re-alert every N hours, one RECOVERED message; each names the cluster and host; `skhealth --test`; secrets in a root-only 0600 env file and never printed. Every run prints its own CPU seconds and peak RSS on its summary line; measured on 2-vCPU lab nodes: about 0.4 s wall / 0.35 s CPU per run (1.6 s / 0.6 s on the primary), engine peak RSS about 22 MiB. `test_single_manager_deploy.py` gains a documented `# skstacks: every-node` exemption that may not touch `/var/data` or drive the swarm. Tests: `v1/tests/test_skhealth_checks.py`, `test_skhealth_alerts.py`, `test_skhealth_remote_wrapper.py`, `test_skhealth_render.py` (stub commands record their arguments; one injected failure gives exactly one alert and one recovery). Release-gated: new skstack06 stage `skhealth`. **Instance action**: none; to use it, add a `skhealth:` vault block and run `deploy_skhealth-<env>.yml`.
- v1: new optional role **skpayment** (`v1/ansible/optional/skpayment`): the SKPayment multi-app Stripe subscription gateway from its own app repo, so a hand-deployed gateway can be reproduced from the framework. Image pinned by digest (default: the Python base image digest the hand-deployed gateway runs; the play refuses an image without `@sha256:`); the app code is fetched on the controller at a pinned `APP_REF` and verified against `APP_SHA256` before it is copied (a mismatch or unknown ref stops the play); Stripe keys and `apps.json` (per-app LiteLLM master keys) from the vault into root 0600 files; `DATA_PATH`/`DATA_NODE` knobs with the skgraph-style local-disk guard; router host from the framework's base-domain expressions (`HOSTNAME` override, `CLOUDFLARED`); `TLS_OPTIONS` (default `intermediate@file`, sniStrict; `none` omits the label, since Traefik rejects `default@file`). **Flagged:** `MIDDLEWARES` defaults to `default-no-crowdsec@file`, as on the hand-deployed gateway; whether that was intentional is unknown (README "CrowdSec"). New subnets `skpayment-prod/staging/dev` = `172.16.235/236/237.0/24`. The render gate gets a test double for the controller checkout. Tests: `v1/tests/test_skpayment_render.py` (the fetch runs for real against a local git repo). Release-gated: new skstack06 stage `skpayment`. **Instance action**: none; to migrate a hand-deployed gateway, follow its README.

### Also in this tree, not release-gated

- v1: **skbackup** (restic engine replacing Duplicati, #109) is present in this tree from `skstacks-v2.26.0` onward but is not one of the skstack06 release-gate stages: it is covered by unit/render tests only (`v1/tests/test_skbackup_*`), never deployed in the skstack06 lab, and never ran through the fresh full `PROFILE=full` lane-1 run that gates this release. Do not deploy skbackup from this tag until it has been validated separately (a dedicated skstack06 stage or an instance pilot).
- v1: **skhealth** SMART, RAID, NFS, TLS and remote-host checks are covered by unit tests only (stub commands); the skstack06 lab has no such hardware, so its stage proves the deploy, the alert channels, the primary election and the dedupe/recovery path, not those checks on real devices.

## skstacks-v2.26.4 - 2026-10-09

### Upgrade notes (instances)

- **sksso with a database user other than `postgres`:** the next sksso deploy changes the `postgres` healthcheck to probe as the configured user and database, so the `postgres` service is updated (one restart of postgres). An instance with `sksso.postgres_user: postgres` renders exactly as before.

### Changes

- v1: **sksso** the `postgres` healthcheck ran `pg_isready -U postgres`. pg_isready only needs the server to answer, so it passed, but on an instance whose database user is not `postgres` every probe made the server log `FATAL: role "postgres" does not exist` (every 10 seconds, seen on a production instance). It now runs `pg_isready -U $POSTGRES_USER -d $POSTGRES_DB` from the container's own environment (`sksso.env`, never rendered into the compose file) whenever `sksso.postgres_user` is not `postgres`; with `postgres` it renders byte-identical to `skstacks-v2.26.3`. Tests: `v1/tests/test_sksso_pg_healthcheck_and_baseline.py`. **Instance action**: none; see the upgrade note.
- v1: **sksso** local-disk database (`DATA_NODE`, `POSTGRES_DATA_PATH`, `REDIS_DATA_PATH`, shipped in `skstacks-v2.23.0`): an empty `POSTGRES_DATA_PATH` or `REDIS_DATA_PATH` now means unset (the `/var/data/runtime` default), as for skgraph/skvector; before, an empty value rendered a bind with no source and stopped the play with a misleading "local disk without DATA_NODE" message. Unset or empty knobs render byte-identical to `skstacks-v2.26.3` (test against fixtures rendered from the v2.26.3 template). The README "Migrating a running instance" procedure is rewritten for moving a live instance off NFS (the trigger: an instance whose postgres on NFS twice crashed uncleanly and restart-looped on `lock file "postmaster.pid" is empty`): stop order, `rsync -aHAX --numeric-ids` on `DATA_NODE`, verify by regular-file count and byte sum (not `du --apparent-size`, which counts directory sizes differently on NFS and ext4), deploy, rename the old dirs to `*.STALE-moved-to-<node>-local-<date>` and never delete them. Tests: `v1/tests/test_sksso_pg_healthcheck_and_baseline.py`, `v1/tests/test_sksso_local_db.py`, `v1/tests/test_sksso_data_node_unset.py`. **Instance action**: none; to move, follow the README migration first.

### Also in this tree, not release-gated

- v1: **skbackup** (restic engine replacing Duplicati, #109) is present in this tree from `skstacks-v2.26.0` onward but is not one of the skstack06 release-gate stages: it is covered by unit/render tests only (`v1/tests/test_skbackup_*`), never deployed in the skstack06 lab, and never ran through the fresh full `PROFILE=full` lane-1 run that gates this release. Do not deploy skbackup from this tag until it has been validated separately (a dedicated skstack06 stage or an instance pilot).

## skstacks-v2.26.3 - 2026-10-08

### Upgrade notes (instances)

- **skhub:** the next skhub deploy changes five Nextcloud settings (Talk `signaling_servers`, notify_push setup and `base_endpoint`, whiteboard `collabBackendUrl`, richdocuments `wopi_url`) on every instance whose public hosts are not `skhub.`/`collabora.<CLUSTERNAME>.<DOMAIN>`: `CLOUDFLARED: true`, a dev or staging env, or a set `SKHUB_HOSTNAME`/`COLLABORA_HOSTNAME`. They move to the host the routers and `OVERWRITEHOST` already use, which fixes Talk calls, push, whiteboard and Collabora there. Nothing to set beforehand; a prod instance without `CLOUDFLARED` and without those keys renders exactly as before. If you hand-corrected any of these settings, the deploy now writes the same value.
- **skhub with `TURN_DOMAIN`:** `talk-hpb` now gets `TURN_DOMAIN` too. Before, only Nextcloud's `turn_servers` did, so every skhub deploy rebuilt `talk-hpb` without it and Janus fell back to `NC_DOMAIN` for TURN (on an instance whose `NC_DOMAIN` is proxied, ICE failed on every call until fixed by hand).

- **skhub Talk HPB image:** an instance that does not set `skhub.TALK_HPB_IMAGE` moves `talk-hpb` to the new default image on its next skhub deploy (see Changes). With `TALK_TURN_RELAY_IPV4` set, the relay wrapper then execs that image's dinit CMD. An instance that pins `TALK_HPB_IMAGE` to another image keeps it and the old supervisord default, unchanged; one that pins it to the new digest without `TALK_HPB_CMD` now gets dinit (before, supervisord, which crash-loops that image). The new image's Janus resolves `TALK_HOST` at startup and exits if it does not resolve (a real deployment's public host resolves).

- **skhub Talk recording:** `talk-recording` now runs exactly one replica (was `TALK_RECORDING_MAX_CONCURRENT`, default 2); an instance on the default goes from 2 replicas to 1 on its next skhub deploy. `TALK_RECORDING_MAX_CONCURRENT` is ignored (the deploy prints a warning when it is set): remove it.

### Changes

- v1: **skhub** `talk-recording` is fixed at one replica. Talk's `recording_servers` URL is the service VIP, so with several replicas the start and the stop of one recording round-robined to different replicas: the stop reported "Trying to stop unknown recording" and an orphaned recorder stayed in the call (seen on a production instance). One recording server runs concurrent recordings itself, and the aio-talk-recording config has no concurrency setting, so `TALK_RECORDING_MAX_CONCURRENT` no longer sets anything; the deploy warns when it is set. Tests: `v1/tests/test_skhub_recording_single_ech.py`, `v1/tests/test_skhub_recording_ai.py`. **Instance action**: see the upgrade note.
- v1: **skhub** the recording browser gets a locked Firefox policy (`talk-recording-firefox-policies.json`, rendered by the deploy and bind-mounted read-only at `/usr/lib/firefox/distribution/policies.json`) that turns off `network.dns.echconfig.enabled`, `network.dns.http3_echconfig.enabled` and `network.dns.native_https_query`. On an instance whose public skhub host is Cloudflare-proxied with an HTTPS DNS record carrying `ech=`, Firefox intermittently sent the ECH outer SNI (`cloudflare-ech.com`) to the instance's own Traefik, got the default certificate, and about 3 in 8 recordings failed with `InsecureCertificate`. Always on (harmless where no ECH record exists). Tests: `v1/tests/test_skhub_recording_single_ech.py`. **Instance action**: none.
- v1: **skhub** default `TALK_HPB_IMAGE` is now `ghcr.io/nextcloud-releases/aio-talk:latest@sha256:831a08f9772d188e65aee2fb59ef3785d3567a1fe2fb65f0e6fd2c17a16ffc87` (digest-pinned), was `aio-talk:20260122_105751`. That build's `/start.sh` ignores `TURN_DOMAIN` and writes no STUN/TURN server into `janus.jcfg`, so Janus offered only its private container address: a production instance whose deploy fell back to the default lost media on every call (ICE failed). The new image writes `turn_server = TURN_DOMAIN` (else `NC_DOMAIN`) into `/conf/janus.jcfg` and still writes the `  relay_ipv4_addr:` line into `/conf/eturnal.yml` that the TURN relay wrapper rewrites (checked against the image's `/start.sh`). It is dinit-based, so `TALK_HPB_CMD` now defaults to the CMD of the image that runs: `[dinit, --system, --container, nats-server, eturnal, janus, signaling]` for the default digest (unset, or `TALK_HPB_IMAGE` pinned to that digest), `[supervisord, -c, /supervisord.conf]` (the old default) for any other pinned image. Tests: `v1/tests/test_skhub_talk_hpb_image.py` (default digest-pinned, wrapper CMD per image, `TALK_HPB_CMD` wins), `v1/tests/test_skhub_talk_turn_modes.py`. **Instance action**: none; see the upgrade note.
- v1: **skhub** the five Nextcloud settings the post-deploy tasks write with a public URL now use the routers' own host expressions (`SKHUB_HOSTNAME`, default `skhub[-dev|-staging].<base domain>`; `COLLABORA_HOSTNAME`, default `collabora.<base domain>`; `<base domain>` honours `CLOUDFLARED`): `notify_push:setup` and notify_push `base_endpoint` (`https://<host>/push`), whiteboard `collabBackendUrl` (`wss://<host>/whiteboard`), Talk `spreed` `signaling_servers` (`https://<host>/standalone-signaling/`) and richdocuments `wopi_url`. They were hardcoded to `skhub.`/`collabora.<CLUSTERNAME>.<DOMAIN>`. A production instance on `CLOUDFLARED` had every skhub deploy point all five at names that did not resolve or reached an internal route with a self-signed certificate: Talk reported the HPB as "Unknown error" and calls could not start, until corrected by hand. For a prod instance without `CLOUDFLARED` and without the two keys all five lines render byte-identical to `skstacks-v2.26.2` (test against the v2.26.2 lines). README "Public hostnames". Tests: `v1/tests/test_skhub_public_hosts.py` (byte-identical where the old names were right; for every env, `CLOUDFLARED` and key combination the URLs equal the routers' `Host()` rules, `OVERWRITEHOST` and the Collabora code URL; secrets and `verify` unchanged). **Instance action**: none; see the upgrade note.
- v1: **skhub** `talk-hpb.env` renders `TURN_DOMAIN` when `skhub.TURN_DOMAIN` is set (#145 only fed it to Nextcloud's `turn_servers`); unset renders exactly as before. Tests: `v1/tests/test_skhub_public_hosts.py`. **Instance action**: none.

### Also in this tree, not release-gated

- v1: **skbackup** (restic engine replacing Duplicati, #109) is present in this tree from `skstacks-v2.26.0` onward but is not one of the skstack06 release-gate stages: it is covered by unit/render tests only (`v1/tests/test_skbackup_*`), never deployed in the skstack06 lab, and never ran through the fresh full `PROFILE=full` lane-1 run that gates this release. Do not deploy skbackup from this tag until it has been validated separately (a dedicated skstack06 stage or an instance pilot).

## skstacks-v2.26.2 - 2026-10-06

### Changes

- v1: **skgraph** (FalkorDB) and **skvector** (Qdrant) can keep their data on local disk on one pinned node, the same design as skhub's `DATA_NODE` in v2.26.0. `skgraph.DATA_NODE` / `skvector.DATA_NODE` add `node.hostname == <DATA_NODE>` to `falkordb` / `qdrant` (after skgraph's `node.role == worker` and after `skvector.placement_constraints`, not added twice when the instance already sets the identical constraint); `skgraph.DATA_PATH` and `skvector.STORAGE_PATH`/`SNAPSHOTS_PATH` move the bind mounts (defaults unchanged under `/var/data/<app>-<env>`), and skvector's `skvector-<env>-storage`/`-snapshots` named volumes follow the same paths. A path outside `/var/data` is created on `DATA_NODE` (not recursive, same owner/mode as the shared-storage copy: skgraph root:root 0755, skvector 1000:1000 0755; both images run as uid 0) and the old `/var/data` path is no longer created; a local path without `DATA_NODE` stops the play, since local data on a floating service would be an empty database after the next reschedule. New READMEs for both stacks document the keys and the "Migrating a running instance" steps (scale to 0, `rsync -aHAX --numeric-ids`, verify counts and sizes, deploy from the new pin, rename the old dir to `*.STALE-moved-to-<node>-local-<date>`, never delete it). Tests: `v1/tests/test_skgraph_skvector_local_data.py` (unset knobs render byte-identical to `skstacks-v2.26.1`, against fixtures rendered from the v2.26.1 templates; the guard and directory tasks run through real `ansible-playbook`); the render gate's new `skgraph.set.yml` / `skvector.set.yml` cover the set path. **Instance action**: none; unset knobs render exactly as before. To move, follow the README migration first.

### Also in this tree, not release-gated

- v1: **skbackup** (restic engine replacing Duplicati, #109) is present in this tree from `skstacks-v2.26.0` onward but is not one of the skstack06 release-gate stages: it is covered by unit/render tests only (`v1/tests/test_skbackup_*`), never deployed in the skstack06 lab, and never ran through the fresh full `PROFILE=full` lane-1 run that gates this release. Do not deploy skbackup from this tag until it has been validated separately (a dedicated skstack06 stage or an instance pilot).

## skstacks-v2.26.1 - 2026-10-05

### Changes

- v1: **skhub** `skhub.TURN_DOMAIN` overrides only the hostname Nextcloud's `turn_servers` config advertises (default unset renders `{{ CLUSTERNAME }}.{{ DOMAIN }}`, unchanged). For an instance whose own ingress cannot serve TURN and that instead relays Talk's TURN/STUN through another cluster's TURN server (a Chef-approved cross-cluster exception on the NAM client cluster, whose ISP blocks inbound 3478), this lets Nextcloud point at that other TURN host while `talk-hpb`'s own eturnal runs unused; `turn_secret` is set to the remote cluster's secret the same way as before. Port (3478) and `turn_secret` plumbing are unaffected. Tests: `v1/tests/test_skhub_turn_domain_override.py`. **Instance action**: none; unset renders exactly as before.

### Also in this tree, not release-gated

- v1: **skbackup** (restic engine replacing Duplicati, #109) is present in this tree from `skstacks-v2.26.0` onward but is not one of the skstack06 release-gate stages: it is covered by unit/render tests only (`v1/tests/test_skbackup_*`), never deployed in the skstack06 lab, and never ran through the fresh full `PROFILE=full` lane-1 run that gates this release. Do not deploy skbackup from this tag until it has been validated separately (a dedicated skstack06 stage or an instance pilot).

## skstacks-v2.26.0 - 2026-10-05

### Changes

- v1: **skhub** `db` (MariaDB) and `redis` can keep their data on local disk on one pinned node. `skhub.DATA_NODE` adds `node.hostname == <DATA_NODE>` to `db` and `redis` only (after `placement_constraints`; `db-backup`, `clamav`, `imaginary`, `collabora` and the app services keep floating); `skhub.DB_DATA_PATH`/`REDIS_DATA_PATH` move their data (defaults unchanged under `/var/data/runtime/skhub-<env>`). A path outside `/var/data` is created on `DATA_NODE` (not recursive, same owner/mode as the shared-storage copy) and the old `/var/data/runtime` path is no longer created; a local path without `DATA_NODE` stops the play, since local data on a floating service would be an empty database after the next reschedule. Built after a production instance found its NFS-backed `db`/`redis` adding measurable write latency to every `occ` capability check and Talk HPB backend call; the instance moved the live mounts by hand first and renamed the now-stale NFS copies so a redeploy from the old template fails loudly instead of silently starting MariaDB on stale data. README "Local database on a pinned node" has the migration steps and the separate-updates rule for swapping mounts. Tests: `v1/tests/test_skhub_local_db_redis.py`. **Instance action**: none; unset knobs render exactly as before. To move, follow the README migration first.
- v1: **skfenceha** the traefik-acme restart's `docker service scale` calls are now bounded (`--detach=true` plus a `timeout` backstop) with a convergence poll afterward, the same shape as the existing worker-role poll. Closes a gap `#140` left: the scale call had no bound of its own, so a Pending acme-master task (for example a manager low on reservable memory) hung the deploy indefinitely instead of failing within minutes with a clear message. Tests: `v1/tests/test_skfenceha_acme_restart_bounded.py`. **Instance action**: none.
- v1: new tool **skstacks-deploy** (`tools/skstacks-deploy`), a one-command v1 app deploy with automatic rollback: renders, deploys, probes the result (`v1/ansible/optional/<app>/probe.yml`, added for skbook, skfetch, skhub, sksso and skstream) and rolls back to the prior image/compose state on a failed probe instead of leaving a broken deploy live. **Instance action**: none; opt in by using the tool for a deploy.
- v1: new published package **skstacks-config-template** (Gap 1 of the config-template-and-safe-deploy design): `scripts/generate-config-template.sh` and `scripts/scrub-check.sh` turn a private instance config repo into a redacted, shareable template (`templates/config-repo/`), with a CI job (`config-template-drift.yml`) that fails if the template drifts from a fresh render or still carries vault ciphertext or a fleet-specific literal the estate denylist should have caught. **Instance action**: none; an operator who wants to publish their own config layout as a template runs the script.

## skstacks-v2.26.16 - 2026-11-06

- v1 skbackup: assemble recursive offsite snapshot sources on a private tmpfs with stable dataset-relative paths, including children whose live mountpoints lie outside their parents. Inspect frozen parents outside the backup tree, skip empty containers and preserve parent-owned files only where child directories already exist. Track all mounts for reverse-order trap cleanup, including partial failure and termination. Preserve restic host, tags and source paths so existing seed snapshots remain eligible parents. Tests cover custom mountpoints, a real seed-parent reuse and restore, file-holding parents, unsafe directories and cleanup. **Instance action**: redeploy the skbackup engine before resuming an affected offsite run.

- v1 skbackup: recursive offsite backups now create one atomic snapshot name across included filesystems, then mount every child read-only at stable paths and unmount in reverse. Unrestricted trees use ZFS recursion; exclusions and zvols use one explicit atomic filesystem list. Track capture ownership and keep only per-set last-success source references, preserving external snapshots and failed-run restore sources. Serialize restore tests with backup capture cleanup. Single-dataset Sanoid behavior and configuration remain unchanged.

- v1 skbackup: opt-in recursive dataset trees with rooted exclusions and explicit private/recovery dataset consent, per-dataset sanoid retention, stable read-only restic mount trees and dataset-specific restore provenance. Local copies freeze only each selected app's owning dataset. Add restic tuning and upload overrides, weekly subset checks, optional protected healthcheck pings, and non-destructive external snapshot provenance reporting. Existing single-dataset vaults render identically, verified against a pre-extension 17-file SHA256 fixture. No live dataset changes are performed by the tests.

### Upgrade notes (instances)

- **skbackup is now host-level (restic), the Duplicati skbackup is retired.** It was never deployed on an instance. Remove its keys from any skbackup vault (`settings_encryption_key`, `ui_password`, `passphrase`, `jobs`, `exclude`, `DUPLICATI_VERSION`, `source_path`, `source_read_only`, `CLI_ARGS`): the new deploy stops while one is present. `CLUSTERNAME`, `DOMAIN`, `ACME_ENABLED` and `networks` are unused and may stay. To adopt it, put the storage host in an inventory group (`storage_hosts` or `-e skbackup_hosts=`) and write the vault from `v1/ansible/optional/skbackup/README.md`.

### Changes

- v1: **skbackup** is rebuilt as a host-level app for the storage host that serves the swarm's data (a Proxmox or any Linux host exporting `/var/data` from a ZFS dataset), replacing the Duplicati swarm stack of v2.19.0 to v2.22.0, which ran on the cluster it protected, read live files over NFS and was never deployed. The engine is the storage-host setup proven on a production NAS, lifted with its behaviour kept and de-branded: `sync` (tier 2: dump hooks, then one atomic snapshot of the data dataset, then a throttled local rsync per app into per-app datasets on a second pool with daily/weekly/monthly history, `newest=DIR:N` for dump dirs, dump freshness flagged but never blocking), `restic` (tier 4: the newest sanoid snapshot mounted read-only at a STABLE path so restic reuses its parent, sets `kind: apps` (every copied app, same excludes) and `kind: paths` (big offsite-only data), upload limit, weekly `forget --prune`, B2 keys mapped to `AWS_*` for the S3 backend; tier 5 `restore-test`: one app plus random sample files restored from the repository and compared by sha256 with the exact snapshot they came from, with a report), `check` (tier 6: sanoid, copy, dump, hook, restic, pool and thin-pool staleness and health; `--notify` alerts through a log or any command, `check_repo` optionally asks the repository), `predeploy` (tier 1 helper: `pre-<purpose>` snapshots with age pruning; sanoid owns tier 1 retention) and `alert-host-check` (page from an alerting host over ssh). The playbook targets a storage-host group (`storage_hosts` or `-e skbackup_hosts=`), writes `sanoid.conf` BEFORE installing the sanoid package (it starts its timer on install; Ubuntu's ships no `/etc/sanoid`), and installs the engine, `/etc/<short_name>/` config (one config file plus `apps.conf`, `restic-sets.conf`, `hooks.conf`, restic credentials 0600) and one systemd service + timer per enabled tier. Defaults encode the production lessons: rsync `--bwlimit` 10000 KiB/s (40000 pushed NFS writes from a node to 45 s on a USB SSD pool), sync 05:30 after the dump sidecars, restic 06:30 with a 22 h runtime cap so a multi-night seed resumes. White-label: `skbackup.branding` (`product_name`, `short_name`, `tagline`, `alert_prefix`, `report_footer`, `contact`, `logo_url`, optional `credits`) plus `unit_prefix` and `host_label` drive the CLI banner and help, alert subjects and bodies, reports, unit names and descriptions, install, state, lock and restic paths and a restic tag, following `v2/skwire/branding.py`; the engine carries no brand string of its own. The README is the backup pattern doc (architecture, tiers, B2 setup, app selection, restore procedures per tier, white-labelling, lessons learned). The deploy fails closed on an incomplete vault and on the retired Duplicati keys. Tests: `v1/tests/test_skbackup_render.py` (the real playbook through Ansible, default and white-label, the records the engine reads, unit ordering, 18 fail-closed cases), `v1/tests/test_skbackup_whitelabel.py` (with a custom brand, no default brand string in any rendered file, install path, shell body, or anything the engine prints, alerts, reports or names on disk; the same scan must fire on the default brand), `v1/tests/test_skbackup_engine.py` (the real scripts on a scratch tree with zfs/zpool/mount test doubles, `v1/tests/fakezfs.py`, and a local restic repository: copy, excludes, newest-N, hooks, locking, pre-deploy pruning, byte-exact restore, parent reuse, restore test pass and mismatch, repository checks, staleness, notifier), `v1/tests/test_skbackup_shellcheck.py` (clean on shellcheck 0.9 and 0.11), `v1/tests/test_skbackup_duplicati_retired.py`; the render gate's `skbackup.example.yml` / `skbackup.set.yml` cover the default brand and every tier under a white-label brand. Generic gates: a documented `# skstacks: storage-host` exemption to the single-manager rule (never a manager group, never the swarm), `test_list_images` only expects images from services that ship a compose template, the env-template guard knows `quote`; CI installs restic. The v1 catalog, v2 docs and the skmem-pg dump comment no longer name Duplicati. **Instance action**: see the upgrade note above.

## skstacks-v2.25.1 - 2026-10-04

### Changes

- v1: **skhub** Collabora's healthcheck test is now an override knob (`COLLABORA_HEALTHCHECK_TEST`), matching the `COLLABORA_IMAGE` pattern (#137). The hardcoded `curl`-based test (in place since 2026-06-21) always failed on the 26.04.4.2.1 default bumped in v2.25.0: that image dropped its shell and `curl` (minimal, non-root image, no `/bin/sh`) and ships its own probe (`/usr/bin/coolwsd --probe --use-env-vars`). Every 26.04 deploy got marked unhealthy and killed by Swarm on a 2-6 minute cycle (confirmed live: coolwsd logs "Ready to accept connections" then an immediate SIGTERM, exit 0, not OOM/apparmor/seccomp; the `ERR Could not open proof RSA key` line some installs also see is unrelated and non-fatal, logged before the server is ready). The default test is now the 26.04 native probe; a pin of `COLLABORA_IMAGE` back to a pre-26.x, curl-capable build must pin the matching curl-based test alongside it (pre-26.x `coolwsd` does not support `--probe`). Root cause: inc-a2671db2. Tests: `v1/tests/test_skhub_collabora_healthcheck.py`. **Instance action**: an instance that had pinned `COLLABORA_IMAGE` to a pre-26.x build as a mitigation should also set `COLLABORA_HEALTHCHECK_TEST` to `["CMD", "curl", "-f", "http://localhost:9980/hosting/discovery"]`, or clear both overrides to pick up the fixed 26.04 default.

## skstacks-v2.25.0 - 2026-10-04

### Changes

- v1: **skfenceha** `error-pages` is now pinned to the same `node.labels.traefik.acme.master == true` label `traefik-acme` already uses, not just `node.role == manager`. The image is built once, by the deploy playbook, on the single manager selected for this run (the same node the playbook labels traefik.acme.master=true); with only the role constraint, Swarm was free to schedule the error-pages task on a different manager that never built the image, which rejected it ("failed to resolve reference ... not found") and rescheduled forever, hanging the deploy. The deploy script's convergence poll also had unbounded `docker service ls`/`inspect` calls and never failed the task on a non-converged worker; both now time out (`timeout 15`) and the script exits 1 instead of silently reporting "Deployment complete!". Found running the v2.25.0 release-gate test (skfenceha hung 5+ hours twice; same class hit NAM 2026-10-01). Tests: `v1/tests/test_skfenceha_error_pages_placement.py`. **Instance action**: none; the next skfenceha deploy picks up the fixed placement and the bounded wait.
- v1: **skhub** `TALK_HPB_CMD` sets the command the Talk TURN relay wrapper execs (with `TALK_TURN_RELAY_IPV4`). The wrapper hardcoded `supervisord -c /supervisord.conf`, the CMD of the default aio-talk image; newer aio-talk images (e.g. the signaling 2.1.1 build that `TALK_HPB_IMAGE` exists for) run dinit and have no supervisord, so the first deploy that combined `TALK_HPB_IMAGE` with the relay knobs left talk-hpb crash-looping with exit 127. Unset keeps supervisord. The wrapper now checks that the command exists and exits 127 with a message naming the knob. Tests: `v1/tests/test_skhub_talk_turn_modes.py`. **Instance action**: an instance that sets `TALK_HPB_IMAGE` to a dinit-based aio-talk and `TALK_TURN_RELAY_IPV4` must set `TALK_HPB_CMD` to that image's CMD before deploying skhub.
- v1: **skhub** component images bumped to the digests already running in production on the NAM cluster: `MARIADB_IMAGE` (mariadb 10.11.7 to 10.11.19, patch within the 10.11 LTS line), `REDIS_IMAGE` (8.4.0-alpine to 8.10.2-alpine, same 8.x major), `CLAMAV_IMAGE`, `COLLABORA_IMAGE`, `WHITEBOARD_IMAGE`, `IMAGINARY_IMAGE` (already-latest upstream, now pinned by digest); each gets its own IMAGE override knob, matching the existing `NEXTCLOUD_IMAGE`/`TALK_HPB_IMAGE` pattern (Nextcloud and talk-hpb are untouched, already current). Adds ClamAV memory tuning via a full `clamd.conf` bind-mount (`clamd.conf.j2`, new): `ConcurrentDatabaseReload no` by default (`CLAMD_CONCURRENT_DATABASE_RELOAD`), measured to keep a forced database reload within the idle RSS baseline instead of spiking about 94 percent; `CLAMD_STREAM_MAX_LENGTH_MB` (default 25) and `CLAMD_MAX_SCAN_SIZE_MB` join `MaxThreads` as deploy-resource-parameterized knobs, the same pattern redis already uses. A new `enable_files_antivirus`-gated occ task sets the `files_antivirus` app's `av_max_file_size`/`av_stream_max_length` from the same `CLAMD_STREAM_MAX_LENGTH_MB` knob so the two cannot drift apart (gated because NAM runs the clamav container without installing files_antivirus at all; the existing unconditional av_host/av_port/av_mode task is unchanged). Tests: `v1/tests/test_skhub_component_images.py`, `v1/tests/test_skhub_clamav_tuning.py`, `v1/tests/test_skhub_files_antivirus_tuning.py`. **Instance action**: none; an instance pinned to an older component image keeps rendering that pin until it sets the matching `*_IMAGE` key.
- v1: **skhub** Talk recording and local AI, both default off. `TALK_RECORDING_ENABLED` adds a `talk-recording` service (the official `ghcr.io/nextcloud-releases/aio-talk-recording` image, digest-pinned default) registered with Talk via `occ config:app:set spreed recording_servers`; it needs `TALK_RECORDING_NODE` (pinned, must differ from `APP_NODE` so recording CPU never competes with Nextcloud) and `TALK_RECORDING_SECRET` (vault, 32+ chars), and `TALK_RECORDING_MAX_CONCURRENT` (default 2) sets its replica count (one ffmpeg+headless-browser worker per replica). `AI_ENABLED` installs/enables `integration_openai` + `assistant` and points `integration_openai` at an OpenAI-compatible gateway (`AI_BASE_URL`, `AI_API_KEY` vault) with `AI_TEXT_MODEL`/`AI_STT_MODEL` (default `sk-default`/`sk-stt`) as model aliases only, never a literal cloud model id; image generation and text-to-speech are turned off (out of scope, and both default to hardcoded OpenAI model ids a local gateway does not serve). The deploy playbooks refuse either combination when enabled without its required keys. README "Talk recording and local AI". Tests: `v1/tests/test_skhub_recording_ai.py`. **Instance action**: none; an instance that wants recording and/or local AI sets the vault keys above and redeploys skhub.

## skstacks-v2.24.0 - 2026-10-01

### Changes

- v1: **skhub** can keep the Nextcloud code tree on local disk on one pinned node. `skhub.WEBROOT_LOCAL_PATH` (default empty: unchanged render) moves the `/var/www/html` and `custom_apps` binds of `nextcloud`, `cron` and `notify_push` from `/var/data` to `<path>/html` and `<path>/custom_apps`; `config`, `data` and `themes` stay shared. `skhub.APP_NODE` pins those three services with `node.hostname == <APP_NODE>`, and the play stops when a local path is set without it (or the path is relative, under `/var/data` or has a trailing slash). The deploy creates the local dirs on `APP_NODE` (not recursive; `delegate_to` falls back to the current host so an unset instance never templates a missing key, the sksso #125 lesson), refuses an unseeded local tree on an installed instance, and by default installs a nightly throttled local to `/var/data` sync timer on `APP_NODE` (`WEBROOT_NFS_SYNC`, `WEBROOT_NFS_SYNC_ON_CALENDAR`, `WEBROOT_NFS_SYNC_BWLIMIT`) that refuses to mirror an empty or unseeded tree or to write into an unmounted `/var/data`. On NFS every Nextcloud image bump made the entrypoint rsync about 700 MB of small files over NFS (35 to 75 minutes per hop) and `occ integrity:check-core` took 340 s. README "Local code tree on a pinned node" has the migration steps and the separate-updates rule for swapping mounts. Tests: `v1/tests/test_skhub_local_webroot.py` (render default, set and empty, only the three services change, guard cases, the delegated dir task through real `ansible-playbook` unset and set, StrictUndefined `delegate_to`, seed guard, the sync script against temp trees). **Instance action**: none; to move, follow the README migration before setting the keys.
- v1: **skhub** `TALK_HPB_IMAGE` sets the Talk HPB (aio-talk) image. The compose template hardcoded `ghcr.io/nextcloud-releases/aio-talk:20260122_105751`, so an instance that moved talk-hpb to a newer aio-talk (a newer signaling server, which a newer Talk app asks for in its setup checks) could not keep it: the next skhub deploy put talk-hpb back on the older image. The knob sets talk-hpb only; unset renders byte-identical. Pin a digest. Tests: `v1/tests/test_skhub_talk_hpb_image.py` (default, knob, byte-identical unset in every env, other services untouched, the TURN relay command kept). **Instance action**: none; an instance that already runs a newer aio-talk sets `skhub.TALK_HPB_IMAGE` to that image before its next skhub deploy.
- v1: **skstream** `DUMB_EXTRA_BINDS` adds bind mounts to the host-level DUMB container: a list of `{source, target, read_only}` (read-only by default), rendered as `-v source:target[:ro]` right after the `/data` bind. Built for a patched cli_debrid module (Other Plex Watchlist for managed Plex Home users, fixed upstream but not released yet) that a production instance binds over the module in the image; without the knob the next skstream deploy rewrote the unit and dropped the fix. The play checks every entry (mapping, absolute paths, no spaces or colons) before rendering. Unset renders the unit byte-identical. Tests: `v1/tests/test_skstream_dumb_extra_binds.py` (byte-identical unset per env, order, read-only default and opt-out, incomplete entries ignored, the playbook's own assert expressions against good and bad entries). **Instance action**: none; an instance that bound a file into DUMB by hand lists it here before its next skstream deploy.
- v1: **skfetch** can run slskd (Soulseek) and Soularr: `skfetch.SLSKD_ENABLED` (default false). slskd shares gluetun's network namespace (the same VPN tunnel and kill switch as qBittorrent) with state on local disk and downloads on the media dataset (`slskd-downloads`, seen by Lidarr as `/data/slskd-downloads`); Soularr reads Lidarr's wanted list and asks slskd for each album. `slskd.yml` and Soularr's `config.ini` are 0600 seeds written only if absent; the five secrets (`SLSKD_SOULSEEK_USERNAME`/`PASSWORD`, `SLSKD_WEB_USERNAME`/`PASSWORD`, `SLSKD_API_KEY`) are required when on and also go to `skfetch.env`. Images digest-pinned (`SLSKD_IMAGE`, `SOULARR_IMAGE`). A production instance added both by hand; without the knob the next skfetch deploy rewrote the compose file without them. Off renders the compose file and env file exactly as before. Tests: `v1/tests/test_skfetch_slskd.py`. **Instance action**: none; an instance that added slskd by hand sets the knob and the five secrets (the values it already runs) before its next skfetch deploy.
- v1: **sksso** server router hooks: `sksso.router_middlewares` (Traefik middlewares on the server router, default none) and `sksso.SERVER_BACKEND_SCHEME` (`http` on 9000, the default, or `https` on 9443; anything else stops the play). A production Authentik router carries a security-headers/error-pages middleware chain and an https backend; the template had no hook, so the first framework deploy of sksso would have dropped the headers from the login page. Unset renders byte-identical. Tests: `v1/tests/test_sksso_router_hooks.py`. **Instance action**: none; an instance moving an existing Authentik onto the framework sets both to what its running router uses.

## skstacks-v2.23.0 - 2026-10-01

### Changes

- v1: **sksso** deploys again with `DATA_NODE` unset (the default). The local database directory task added with the local-DB knobs below delegated to `{{ sksso.DATA_NODE }}`, and Ansible templates `delegate_to` for every loop item before it evaluates `when`, so every instance that did not opt in stopped at that task ("object of type 'dict' has no attribute 'DATA_NODE'") although it would have been skipped. `delegate_to` now falls back to the current host; the play's guard still requires `DATA_NODE` whenever a local path is set. Found by the skstack06 v2.23.0 release run (stage sksso); the Jinja-only tests rendered with a lenient undefined and never ran Ansible. Tests: `v1/tests/test_sksso_data_node_unset.py` (the playbook's own task through real `ansible-playbook`, default and pinned, plus a StrictUndefined render). **Instance action**: none.
- v1: new optional app **skstream** (Plex plus Real-Debrid). Plex (and optional Kometa) run as the swarm stack `skstream-<env>`, pinned to a media node (`skstream.MEDIA_NODE`, node label `skstream-media=true`); DUMB (zurg, rclone FUSE mount, cli_debrid, cli_battery) runs as the host-level unit `skstream-dumb[-<env>].service` on that node, because a swarm service cannot get `/dev/fuse`, `SYS_ADMIN` and an unconfined AppArmor profile. Plex sees the mount through an `rslave` bind at `/mnt/debrid`. Hardening from a production outage: the unit waits for swarm before joining the overlay, is `WantedBy=docker.service` with `StartLimitIntervalSec=0`, and cleans a stale FUSE mount on start and stop; the Plex SQLite databases, DUMB `/data` and `/log` are on local disk (on NFS Plex stalled on a busy database); seeds are written only if absent (`FORCE_SEED`); the zurg library hook asks Plex for partial scans and reads the token at runtime. Knobs include `CUTOFF_DATE` (how far back watchlisted shows are fetched) and `PLEX_EXTRA_BINDS` (read-only binds into Plex, for example a skfetch media dataset). Tests: `v1/tests/test_skstream_app.py`. **Instance action**: none unless you deploy it; see the skstream README.
- v1: **skstream** `PLEX_TOKEN` must be a dedicated account token from the plex.tv PIN link flow, never the server's `PlexOnlineToken`: PlexAPI clients identify as a controller, and plex.tv rewrote the server's device record with every watchlist poll until Plex apps stopped listing the server. New guard `skstream.PLEX_TOKEN_IS_SERVER_TOKEN` (default false) fails the play when true; the README covers minting and rotating the token. `skstream.CLI_DEBRID_HYBRID_MODE` (default true) sets cli_debrid `Scraping.hybrid_mode`, so older shows without a cached release take the best uncached one instead of being blacklisted. The README also notes Real-Debrid DMCA blocks (a cached torrent whose unrestrict returns 451). Tests: `v1/tests/test_skstream_app.py`. **Instance action**: none for a new install; an existing install that seeded the server token must rotate it as the README describes (seeds are only if absent).
- v1: **skstream** Plex uses `endpoint_mode: dnsrr` by default (`skstream.PLEX_ENDPOINT_MODE`, `dnsrr` or `vip`; anything else fails the play). After a full-cluster reboot a host-level container on the shared attachable overlay was given the same address as the Plex service VIP (the local daemon does not know which addresses swarm holds as VIPs), and calls to `plex` were black-holed for about 9 hours. With dnsrr the name resolves straight to the task. Plex publishes no ports and Traefik targets task addresses, so nothing else changes. Any swarm service reached by name from host-level containers on a shared overlay should use dnsrr (README notes in skstream and skfetch). Tests: `v1/tests/test_skstream_app.py`. **Instance action**: none; the next skstream deploy switches the Plex service to dnsrr (set `PLEX_ENDPOINT_MODE: vip` to keep the old mode).
- v1: new optional app **skfetch**: Sonarr, Radarr, Lidarr, Prowlarr and qBittorrent behind a VPN, plus optional FlareSolverr (`FLARESOLVERR_ENABLED`). It is a host-level docker compose project `skfetch-<env>` on `skfetch.MEDIA_NODE`, started by `skfetch[-<env>].service`, because qBittorrent runs in gluetun's network namespace (`network_mode: service:gluetun`) and gluetun needs `NET_ADMIN` and `/dev/net/tun`, neither of which swarm supports. VPN credentials live only in `skfetch.env` (0600); qBittorrent is never published and its incomplete downloads stay on the media dataset. Seeds (only if absent) set each app to External authentication with `AllowedHosts` including the public host, so a forward-auth proxy logs users in once. `wire.py` (stdlib only, idempotent) wires root folders, the download client and categories, Plex notifications with path mapping, Prowlarr app sync, FlareSolverr and public indexers. Optional Traefik file-provider routes per UI (`TRAEFIK_DYNAMIC_DIR`) with a middleware list knob. Also fixed: an all-digit API key loaded as an integer and crashed a length assert (required checks now cast with `| string`). Tests: `v1/tests/test_skfetch_app.py` (including a fake Servarr and Prowlarr API that checks a second wiring run creates nothing). **Instance action**: none unless you deploy it; see the skfetch README.
- v1: **sksso** postgres and redis can run on local disk on a pinned node. `sksso.DATA_NODE` pins postgres and redis only (server, worker, capauth and the backup sidecar keep floating); `sksso.POSTGRES_DATA_PATH` and `sksso.REDIS_DATA_PATH` move their data (defaults unchanged under `/var/data/runtime/<app>-<env>`). A path outside `/var/data` is created on `DATA_NODE` (owner 999, mode 0700, never recursive) and the old shared-storage path is no longer created; a local path without `DATA_NODE` stops the play, since local data on a floating service would be an empty database after the next reschedule. Built after an NFS hang on the worker running them took Authentik, and every forward-auth login, down. The README section "Local database on a pinned node" gives the migration steps and warns that `docker service update --mount-rm X --mount-add ...target=X` in one command drops both mounts, so postgres initializes an empty volume: do the two in separate updates or redeploy the stack. Tests: `v1/tests/test_sksso_local_db.py`. **Instance action**: none; unset knobs render exactly as before. To move, follow the README migration first.
- v1: **sksso** CapAuth login keeps the password login. The `capauth-authentication` flow now shows the normal username and password form **plus** a CapAuth button on one page (Authentik's identification stage with its inline password stage and the source, the standard pattern), then the default MFA validation stage and user login, so an application bound to it (skmesh `AUTHENTIK_AUTHENTICATION_FLOW`) accepts both PGP and password logins and the password path is no weaker than the default login. `sksso.CAPAUTH_PASSWORD_LOGIN: false` restores CapAuth only (the pilot behaviour); `CAPAUTH_PASSWORD_STAGE` and `CAPAUTH_MFA_STAGE` name the stages (defaults: Authentik's default blueprints; a missing stage fails the deploy before anything changes; `CAPAUTH_MFA_STAGE: ""` binds none). Switching is idempotent and removes the MFA binding when turned off. Tests: `v1/tests/test_sksso_capauth.py` (default on, off and back on, named and missing stages, string booleans, task wiring). **Instance action**: none; a CapAuth flow created by an earlier build gains the password form on the next sksso deploy (`identification stage: updated (password_stage, user_fields)`).
- v1: **sksso** `CAPAUTH_IMAGE` has a default now: the public CapAuth release `ghcr.io/smilintux/capauth:0.3.13@sha256:f2de737815f30a47706336250cfb257673c4861c0a790f28d09cd7403fb80ac7` (capauth v0.3.13 on ghcr.io, with the per-client `require_nonce` Authentik's OAuth source needs). A vault value still wins and must still be pinned by digest; `test_no_latest_images` no longer exempts sksso (no instance-only image left). Tests: `v1/tests/test_sksso_capauth.py` (the default, the vault override, digest guard). **Instance action**: none; drop `CAPAUTH_IMAGE` from the vault to use the release.
- v1: **skmesh** `AUTH_MODE: authentik` can bind a login flow to skmesh only: `skmesh.AUTHENTIK_AUTHENTICATION_FLOW` (a flow slug, default empty) becomes the skmesh provider's authentication flow, so a user who is not logged in to Authentik yet goes through that flow when opening skmesh while every other application keeps the brand's default login. Built for sksso's CapAuth login (`capauth-authentication`). Empty means the brand's default, and removing the key unbinds a bound flow on the next deploy (rollback). An unknown slug fails the deploy before the stack starts. Tests: `v1/tests/test_skmesh_auth_modes.py` (knob wiring, default unbound, bind then unbind, unknown slug). **Instance action**: none.
- v1: **sksso**'s worker healthcheck can fail again. It looked for any command line containing the substrings `celery` and `worker`, and its own command line (the `python3 -c` source and the `sh -c` wrapping it) contains both, so it always matched itself and passed whether or not a worker ran: a production worker on Authentik 2025.12.3 (dramatiq, no celery at all) reports `healthy`. The check now compares argv elements exactly: a process with a `worker` argument and `manage` (`python -m manage worker`: Authentik 2024.x runs celery inside it, 2025.10+ dramatiq) or a celery executable (older releases). Test: `v1/tests/test_sksso_worker_healthcheck.py` runs the rendered check against a fake `/proc` that includes the checker's own processes (no worker, `manage migrate` or celery `beat` alone now fail; both worker generations pass). **Instance action**: none; the next sksso deploy changes the worker's healthcheck (a post-deploy parity diff on it is this fix) and restarts the worker.
- v1: **sksso** can run a CapAuth login (PGP-signed) next to Authentik: `sksso.CAPAUTH_ENABLED` adds a `capauth` OIDC identity provider service (own host `capauth[-<env>].<base>`, one replica, state under `/var/data/sksso-<env>/capauth`), and the deploy registers it in stock Authentik (2025.2+) as an OAuth source with PKCE S256, matched by PGP fingerprint only and with no enrollment flow, plus a `capauth-authentication` flow (identification offering only that source, then user login) and fingerprint links for `sksso.CAPAUTH_USERS`. The flow is bound to nothing: an application opts in (skmesh: `AUTHENTIK_AUTHENTICATION_FLOW`), every other application keeps the password login. Idempotent (`tasks/capauth_provision.yml`, `src/sksso/capauth_provision.py`, via `sksso.api_token`); only `/oidc/`, `/capauth/v1/challenge` and `/capauth/v1/verify` are routed publicly (the admin API stays cluster-internal); a redeploy force-restarts `capauth` so a changed client registry takes effect, and turning it off removes the service (a stack deploy alone would leave it running). Also: the bind-mount source test now follows static `include_tasks`/`import_tasks` files, and `test_no_latest_images` allows exactly one instance-named image (`sksso.CAPAUTH_IMAGE`, no public pinned build yet) because the deploy refuses it without `@sha256:`. Tests: `v1/tests/test_sksso_capauth.py` (render, playbook wiring, vault guard, and the provisioner against a fake Authentik that validates like 2025.12). **Instance action**: none unless you enable it; see the sksso README "CapAuth login" (needs a digest-pinned CapAuth image with per-client `require_nonce`, DNS and a certificate for the CapAuth host, and `sksso.api_token`).

## skstacks-v2.22.0 - 2026-10-01

### Upgrade notes (instances)

Read before repinning an instance to `skstacks-v2.22.0`. Details are in the matching entry under Changes.

- **Restart policy (all long-running services).** Delete `skboard.RESTART_POLICY_MAX_ATTEMPTS`, `skboard.RESTART_POLICY_WINDOW`, `skdash.restart_policy_max_attempts` and `skdash.restart_policy_window` from the vault if set: the skboard and skdash playbooks now stop with a message while one is present. Every service listed in the restart-policy entry changes its live `RestartPolicy` to `condition: any` with no `max_attempts` on its next redeploy; a post-deploy live-parity diff on that field is this fix.
- **skmail.** If the running docker-mailserver has a hand-set healthcheck `start_period` (for example `docker service update --health-start-period 3600s`), set `skmail.HEALTHCHECK_START_PERIOD` to the same value before redeploying; otherwise the redeploy drops it to the `1800s` default. `skmail.MAIL_WEB_ENABLED: false` is optional (read the README section "The mail-web sidecar" first).
- **skmon.** An instance that keeps skmon parked must set `skmon.replicas: 0`; without it every redeploy brings the whole stack back at full size (the v2.21.0 behaviour). Behind the edge `traefikAuth` middleware set `skmon.GRAFANA_BASIC_AUTH: false`; on NFS-backed data set `skmon.GRAFANA_HEALTH_START_PERIOD: 5m`.
- **skhub Talk TURN.** Nothing changes unless you set the new keys. For callers outside the LAN: `skhub.TALK_TURN_PUBLISH_MODE: host`, `skhub.TALK_HPB_PLACEMENT_CONSTRAINTS` pinning talk-hpb to one node, `skhub.TALK_TURN_RELAY_MIN_PORT`/`MAX_PORT` (at most 200 ports), and `skhub.TALK_TURN_RELAY_IPV4` set to the public address. The router must forward 3478 tcp+udp and the relay range udp to **that pinned node's own address**, not a VIP or the ingress: host mode publishes only on the node running the task. Then redeploy (talk-hpb).
- **skhub Nextcloud image.** Nothing to do unless you upgrade Nextcloud: set `skhub.NEXTCLOUD_IMAGE` to the version you run, pinned by digest, and keep it set (an older image than the installed one crash-loops: Nextcloud does not downgrade).
- **sksec activation order.** The bouncer fails closed (CrowdSec down means 403 on every router behind it), so: (1) set `sksec.ALLOWLIST_CIDRS` to your own LAN, VPN, WAN and CI runner addresses; (2) deploy sksec and confirm it acquires the skfenceha access logs (`cscli metrics` shows lines read and parsed) and that a test ban on a documentation-range address takes effect; (3) canary `crowdsec-bouncer@file` on one smoke-test router with `skwhoami.MIDDLEWARES`; (4) restart Traefik on any node that has not loaded `dynamic/sksec.yml` (NFS gives no inotify event); (5) only then add the bouncer to real routers. A running sksec from an earlier version must be redeployed to acquire logs at all; clean up stale bouncer registrations as described in its entry.
- **skmesh.** Set `skmesh.AUTH_MODE` explicitly: `standalone` (NetBird's embedded IdP, the default) or `authentik` (sksso). A vault that sets `NETBIRD_AUTH_CLIENT_ID` without `AUTH_MODE` is refused, so an install never moves to a different user store silently. `authentik` needs `sksso.api_token` (registered once with sksso's `ensure-api-token.sh`) plus `skmesh.IDP_MANAGER_PASSWORD` (each `openssl rand -hex 32`) and `skmesh.SSO_HOST_IP`; the deploy creates the Authentik provider, application and IdP-manager account. Also set `NETBIRD_RELAY_ENDPOINT` to `rels://skmesh.<base>:443`, and to share an existing TURN server set `skmesh.TURN_URI`, `STUN_URIS` and `TURN_TIME_BASED_CREDENTIALS`.
- **Ownership and mode fixes are bounded.** No deploy runs `chown -R` / `chmod -R` over a data dir any more; nothing to set. skgit repository files keep their current mode unless `skgit.PERMS_FULL_WALK: true` is set for one deploy (plan for its I/O).
- **Database dump sidecars (skgallery, sksso, skmem-pg, skmesh).** Nothing to set: an existing `BACKUP_NUM_KEEP` / `backup_num_keep` now means days kept. To set it under the new name use `skgallery.BACKUP_KEEP_DAYS`, `sksso.backup_keep_days`, `skmem_pg_backup_keep_days` or `skmesh.BACKUP_KEEP_DAYS`. Redeploying the stack (or just its backup service) replaces the sidecar; the one file the old sidecar kept rewriting ages out after that many days.
- **skgit.** Redeploy once to close the data dir (`0750`) and the key files (`0600`), and to keep the Actions runner's `runner-config.yml` readable (`0644`). Consider rotating the SSH host keys and the JWT signing key that were world-readable.
- **skpdf.** Check the vault before repinning: `skpdf.AUTO_KEY` must be a UUID or unset, and the skpdf deploy now stops on anything else, such as the old default shape `skpdf-<env>`. Replace such a value with fresh `uuidgen` output (Stirling never used the old one: it generated a random key on every deploy instead). Then redeploy, delete any `extraConfigs/settings.yml.*.bak`, and rotate the admin password and OAuth2 client secret that sat world-readable.

### Changes

- v1: no long-running service carries a Swarm restart budget any more. `deploy.restart_policy.max_attempts` is lifetime state per task slot, not a crash-loop brake: once spent, Swarm never reschedules the slot. With a single-node placement pin, one outage of that node spends it, and the service sits at 0/N until someone forces an update. A production instance lost two services this way for about four months each: **skboard** (vikunja, 3 attempts) and **skmail**'s `mail-web` (3 attempts, pinned). `max_attempts` (and the `window` that only counts attempts) is removed from every service that had one, and each of them now restarts on any exit (`condition: any`): Swarm's healthcheck kill usually ends in a clean exit 0 (busybox httpd, gunicorn), which `on-failure` never restarts. Delays are unchanged. Changed: **sksec** `crowdsec`, `bouncer-traefik`; **skboard** `vikunja`; **skbook** `bookstack`; **skdash** `dashy`; **skform** `opentofu`; **skgallery** `immich-server`, `immich-machine-learning`, `redis`, `database`; **skgraph** `falkordb`; **skmail** `mail-web`, `skmail`; **skorch** `postgres`, `redis` (plus `gpg-signer`, already `any`); **skport** `portainer`; **skvector** `qdrant`; and the no-op `max_attempts: 0` dropped from **skdesk** `hbbs`/`hbbr` and **skseek** `skseek`. The retired knobs `skboard.RESTART_POLICY_MAX_ATTEMPTS` / `RESTART_POLICY_WINDOW` and `skdash.restart_policy_max_attempts` / `restart_policy_window` are gone, and the skboard/skdash playbooks now stop with a message if a vault still sets one; the condition defaults of `skboard.RESTART_POLICY_CONDITION`, `skdash.restart_policy_condition` and `skbook.restart_policy_condition` change from `on-failure` to `any`. Test: `v1/tests/test_restart_policy_never_gives_up.py` renders every compose template for every env and flag combination and fails on any `max_attempts`, backed by a raw scan of `v1/ansible` (a true one-shot job may be exempted in its `ONE_SHOT` map, with a reason; none is today). **Instance action**: delete the retired skboard/skdash keys from the vault if set. The next redeploy of each service above changes its live `RestartPolicy` (a post-deploy parity check will show it; classify it as this fix). A service already stuck at 0/N needs one `docker service update --force` or a redeploy.
- v1: deploys no longer walk whole data trees to reset ownership or modes. `chown -R` / `chmod -R` / ansible `recurse: yes` issue one SETATTR per entry on every run whether or not anything is wrong; on NFS that is metadata I/O proportional to the data. One skgit deploy spent 40+ minutes in `chown -R` + `chmod -R` over a 60G Forgejo data dir (repositories, LFS) where everything was already correct, and saturated NFS for every other service on it. Every such fix is now incremental (a `find` that selects only entries whose owner or mode is wrong, so a no-op deploy writes nothing) and bounded to the paths that need it. **skgit**: the top two levels of `data/` (the roots of the repository, LFS, attachment and package stores, never their contents), the key dirs (`ssh/`, `forgejo/ssh/`, `forgejo/jwt/`, `git/.ssh/`: now owner-only 0700, private keys 0600, public keys without group write or other bits) and `config/`. #89's intent is kept: `data/` is 0750, which keeps "other" out of everything below it, so repository files an older `chmod -R 755` left 0644/0755 are unreachable and deliberately left alone; the new `skgit.PERMS_FULL_WALK` (vault, default false) walks all of `data/` once, incrementally, for a tree copied in with foreign owners or to strip those bits. **skmesh**: management/signal/relay ownership and management modes are incremental (tens of files, still walked in full). **sksync**: `sync-data` top level only (Syncthing writes what it syncs as uid 1000), `syncthing_config` owner incremental, and the blanket dirs 755 / files 644 is gone: it made Syncthing's `key.pem` world-readable, and private keys are now put back to 0600; `deploy.j2` chowns its dirs non-recursively. **skbook**: `bookstack/` and `bookstack/config/` are created owned by PUID/PGID, not recursed (the container writes `/config` as PUID/PGID; the old recurse also made every upload 0755). **skboard**: `data/` and `data/files/` are created 1000:0, not recursed (Vikunja writes its db and attachments as 1000). **skmail**: the deploy's `chown -R 5000:5000 /var/mail` is now the domain and mailbox dirs only (DMS already chowns each mailbox recursively at every start), and `user-patches.sh`'s amavis fix is incremental and skips the quarantine (`virusmails`). skmon and skhub had no recursive walks. Tests: `v1/tests/test_no_unbounded_recursive_perms.py` (static: fails on any `chown -R` / `chmod -R` / `recurse: yes`, on a `find -exec chmod|chown` without an owner/mode predicate, and on an unbounded `find` walk not on its allowlist with a reason), `v1/tests/test_skgit_data_dir_modes.py` (runs the skgit task on a scratch tree: correct modes, bulk data and already-correct entries keep their ctime, a second run writes nothing, chown never reaches into the bulk stores). **Instance action**: none. skgit repository files keep whatever mode they have; set `skgit.PERMS_FULL_WALK: true` for one deploy only if you want them stripped of other bits (plan for the full walk's I/O), then unset it.
- v1: the pg dump sidecars of **skgallery**, **sksso**, **skmem-pg** and **skmesh** keep a history of good dumps and can no longer destroy one. Their entrypoint was `bash -c 'bash -s <<EOF ... EOF'` with an unquoted heredoc, so the outer shell expanded the backtick `date` once, when the container started: every loop wrote the same file name, `>` truncated the only copy before the dump ran (a failed or interrupted dump left an empty or partial file in place of the last good one), and the `xargs rm -- {}` retention errored on every loop. A production instance had one skgallery dump and one sksso dump, rewritten daily under a file name from the container start weeks earlier. Now one shell runs the script (list-form entrypoint, as skgit already does, every `$` escaped for compose), each run takes its own date-stamped name, the dump goes to a hidden temp file in the same directory and is renamed only when the dump exits 0 and is non-empty (`pipefail` for skmesh's `pg_dumpall | gzip`), leftover temp files are removed at start, and dumps older than `BACKUP_KEEP_DAYS` (new vault knobs, falling back to the old `NUM_KEEP` keys, default 7) are pruned only right after a good dump, so a run of failures never ages out the last good copy. SIGTERM now stops the sidecar at once instead of after the current sleep. skmesh gains `skmesh.BACKUP_FREQUENCY` (was hardcoded). skgit, skhub and skbook already took the date inside the loop and were not affected; skorch uses `postgres-backup-local`. Test first: `v1/tests/test_db_dump_sidecars.py` renders each template and runs its real entrypoint script with fake `pg_dump`/`pg_dumpall`/`date`/`sleep` (unique names, a failing dump leaves the good ones and no temp file, age pruning, no pruning on failure, the knob and its fallback) plus static guards against a heredoc, a backtick and an unescaped `$` (28 red before, 28 green after). **Instance action**: none; see the upgrade note.
- v1: **skport**'s socket-proxy detection no longer misfires, and fails loudly when there is no proxy. It piped `docker service ls` into `grep -q` under `set -o pipefail`: on a busy cluster grep exits at the match, `docker` takes SIGPIPE on its next write, the pipeline reports failure, and the `if` fell through, so with skfenceha deployed the task could answer `skfence` and skport's `DOCKER_HOST` pointed at a proxy that does not exist. When neither proxy existed it also answered `skfence`. The task now captures the service list once (a failing `docker service ls` stops the deploy), matches whole names with `grep -qx <<<"$services"`, and stops the deploy with a message naming both expected services when neither exists. The render gate's stub for `socket_proxy_detection` is now `skfenceha` (it rendered `tasks.-<env>_socket-proxy`). The same `| grep -q` under pipefail pattern is fixed everywhere else it occurs: `v2/harness/{k3d,swarm,vm}-deploy.sh`, `v2/platform/rke2/scripts/longhorn-preflight.sh` (remote dpkg checks) and `v2/secrets/openbao/bootstrap.sh`, where it was also a logic bug: `bao status` exits 2 when sealed, so an initialized but sealed server was read as uninitialized and the script went on to `bao operator init`. Tests: `v1/tests/test_skport_socket_proxy_detection.py` runs the task's own shell for each env against a fake `docker` that, like the real CLI, dies of SIGPIPE (the busy-cluster case picked `skfence` before the fix); `v1/tests/test_no_pipe_to_grep_q_under_pipefail.py` scans the whole repository (Ansible shell/command task bodies one task at a time, every other file as a whole) and fails on any `| grep -q`/`--quiet` where `pipefail` is set. **Instance action**: none. A skport deploy on a cluster with neither skfenceha nor skfence now fails instead of deploying a skport that cannot reach Docker.
- v1: **skgit** and **skmesh** playbooks load again. A lone apostrophe in a comment inside a free-form `shell: |` body ("Forgejo's", "NetBird's", added by the v2.22.0 data-dir mode fixes) made Ansible's argument splitter fail with "unbalanced jinja2 block or quotes", so `deploy_skgit-{dev,staging,prod}.yml` and `deploy_skmesh-prod.yml` failed before running a task (found by the skstack06 run: skgit stage). The render gate and unit tests passed because they read the YAML, not Ansible's task parser. New `v1/tests/test_shell_bodies_parse_in_ansible.py` runs `ansible.parsing.splitter.split_args` over every free-form shell/command body in every deploy playbook and task file (4 red before, all green). **Instance action**: none.
- v1: **skgit** no longer makes Forgejo's data dir world-readable. The post-deploy "Fix Forgejo data permissions and start runners" task ran `chmod -R 755` over `/var/data/skgit-<env>/data` on every deploy (#87 fixed the same shape for the config dir). That is Forgejo's `/data`: the image's OpenSSH host keys (`ssh/ssh_host_*_key`), the built-in SSH server key (`forgejo/ssh/gitea.rsa`), the OAuth2/JWT signing key (`forgejo/jwt/private.pem`) and every repository, all left `0755` on the shared `/var/data`. Forgejo runs as the container user `1000:1000` (`skgit.yml.j2` `user:`; the image's `git` user is uid/gid 1000), so it only needs owner access. The fix now sets owner rwX (existing execute bits such as git hooks are kept), group read, other nothing (dirs `0750`), then private key files under `ssh/`, `forgejo/ssh/`, `forgejo/jwt/` and `git/.ssh/` to `0600` (`*.pub` excepted) and `git/.ssh` to `0700`, the modes ssh-keygen and Forgejo create them with. The data dir itself is now created `0750`, so the create task and the post-deploy fix agree. Since the bounded-walk entry above (#103), the ownership and mode fix touches only the top two levels of `data/` and the key dirs, incrementally, never the whole tree. Test: `v1/tests/test_skgit_data_dir_modes.py`, which runs the task's own shell body against a scratch tree shaped like a Forgejo 15 `/data`. Note: uid 1000 is also the first local account on a typical node, which owns these files on the host whatever the mode; that is the container uid mapping, not something a mode can fix. **Instance action**: redeploy skgit: `data/` becomes `0750` (which closes everything below it to other accounts) and the key files `0600` (an earlier deploy left everything `0755`; see `skgit.PERMS_FULL_WALK` above to also strip the repository files). Treat the SSH host keys and the JWT signing key as exposed to every account that could read the shared filesystem, and consider rotating them (delete the key files and restart Forgejo to regenerate; SSH clients will see a host key change, and JWT-signed OAuth2 tokens are invalidated).
- v1: **skgit**'s `app.ini.j2` now sets `WORK_PATH` in the root section (before `[server]`), the only place Forgejo reads it (`modules/setting/path.go` reads the unnamed section). Under `[server]` the key was ignored: the effective work path came from the image wrapper's `GITEA_WORK_DIR` (`/data/gitea`, the same value), and `forgejo web` then wrote a root `WORK_PATH` back into `app.ini` on every start (`cmd/web.go`), so the file on disk never matched what the playbook rendered and a live-parity check always reported it. Behaviour is unchanged (same path). Test: `v1/tests/test_skgit_work_path_root_section.py`. Instance action: none.
- v1: **skgit** Actions runner no longer crash-loops after a deploy. The env-template loop renders every file `0640` root:root (v2.21.0 secret file modes), including `runner-config.yml`, which the runner compose project bind-mounts read-only at `/data/config.yaml` in the forgejo-runner container. That container runs as a non-root user, so it could not open its config (`open /data/config.yaml: permission denied`), restarted forever and every Actions job stayed queued. The loop now takes a per-item `mode` (default `0640`) and `runner-config.yml`, which holds no secret (capacity, timeouts, labels, the DinD address), is rendered `0644`. Test: `v1/tests/test_skgit_runner_config_readable.py`. **Instance action**: an instance deployed with v2.21.0 whose runner restart-loops can `chmod 0644` the file on the node (or redeploy with this fix); the runner recovers on its next restart.
- v1: **skhub** Talk HPB can serve TURN to callers outside the LAN. The talk-hpb service (aio-talk) runs eturnal on 3478, published through the Swarm ingress: eturnal saw the ingress SNAT address, so a STUN binding answered an internal `10.0.0.x` address and a TURN client's permission for its own public address was refused as a private peer, and eturnal advertised the container IP as its relay address with no relay port published, so a relayed candidate was unreachable from the internet (Talk still worked only because Janus, its one relay peer, is in the same container; any other TURN client, such as NetBird peers sharing the server, got nothing usable). New optional keys, unset = rendered byte-identical: `skhub.TALK_TURN_PUBLISH_MODE` (`ingress` default, or `host`: 3478 tcp+udp in host mode), `skhub.TALK_HPB_PLACEMENT_CONSTRAINTS` (talk-hpb's own placement, default `skhub.placement_constraints`; required with `host`, since host ports open only on the task's node), `skhub.TALK_TURN_RELAY_MIN_PORT`/`TALK_TURN_RELAY_MAX_PORT` (a bounded relay range, host mode only, at most 200 ports, passed to eturnal as `ETURNAL_RELAY_MIN_PORT`/`ETURNAL_RELAY_MAX_PORT` in `talk-hpb.env` and published udp in host mode) and `skhub.TALK_TURN_RELAY_IPV4` (the relay address eturnal advertises: aio-talk's `start.sh` always writes the container IP, so the service command rewrites that one line of `/conf/eturnal.yml` and then runs the image's own `supervisord` command, exiting with a message if the line is missing). The deploy playbooks (dev, staging, prod) assert the combination is coherent before rendering. README section "Talk HPB TURN" covers range sizing and the router forwards. Test first: `v1/tests/test_skhub_talk_turn_modes.py` (55 red, 10 default/guard green before; 65 green after); the render gate's skhub set pass now enables Talk with every key set. Proven against the pinned aio-talk image: relayed address = the configured address, relay port inside the range, peer-to-relay and relay-to-peer datagrams round-trip, healthcheck passes. **Instance action**: none unless you want outside TURN: set the keys, add router forwards (3478 tcp+udp and the relay range udp) to the pinned node, and redeploy talk-hpb.
- v1: **skhub** redeploys no longer re-download the Recognize models or loosen the Nextcloud data directory. The post-deploy task ran `occ recognize:download-models` on every deploy, and that command deletes the whole `custom_apps/recognize/models` directory before downloading the model archive (over 1 GB) and extracting it again (`DownloadModelsService::download()`): gigabytes of unthrottled delete and write traffic on the shared filesystem per deploy, a hard dependency on the internet, and, since the task is best-effort, a failed download silently left Recognize with no models. Recognize fetches its models itself on install and upgrade, so the task now downloads only when the models directory is missing or empty (a `find -maxdepth 1` check in the container; force a refresh by hand with the same `occ` command). The directory task also created and re-moded Nextcloud's data directory (`/var/data/skhub-<env>/data`, mounted as `/var/www/html/data`) `0755` on every deploy, while Nextcloud requires `0770`; each redeploy made the list of user directories readable to any account on the shared filesystem until Nextcloud's own check restored `0770`. It is now `0770`. Test first: `v1/tests/test_skhub_redeploy_safety.py` runs the task's own shell against a fake `docker` (models present: no download; missing or empty: one download) and checks the data dir mode (9 red, 6 green before; 15 green after). **Instance action**: none. A running instance keeps its models; its data dir stays `0770`.
- v1: **skhub** gains `skhub.NEXTCLOUD_IMAGE` (default `nextcloud:31.0.14`, unchanged), the image for `nextcloud`, `cron` and `notify_push`. The compose template hardcoded the Nextcloud image three times, so an instance could not move to a newer Nextcloud without a framework release, and an instance that had already upgraded would be put back on the older image by its next deploy: Nextcloud upgrades are one-way, the image's entrypoint refuses to start on a downgrade ("downgrading is not supported"), and the service crash-loops. The three services share `/var/www/html`, so one knob sets all three. Unset: rendered byte-identical. Test first: `v1/tests/test_skhub_nextcloud_image.py` (5 red, 4 green before; 9 green after); the render gate's skhub set pass sets it. **Instance action**: none. An instance that upgrades Nextcloud (one major at a time) must set it to the running version and keep it set.
- v1: **skmail** gains `skmail.HEALTHCHECK_START_PERIOD` (default `1800s`, was a hardcoded `60s`) for the docker-mailserver healthcheck's `start_period`. At every start DMS `chown -R`s each account's mailbox before postfix listens; on NFS that is a round trip per file, and one production start with 22 mailboxes took ~17 minutes under NFS load. The old window (60s plus 3 x 30s retries) made Swarm kill the task as unhealthy and restart it into the same chown: a kill loop and a mail outage. 1800s is the observed worst case with ~75% headroom and still replaces a container that never comes up after ~32 minutes; it does not delay a normal start (probe failures inside `start_period` are not counted and the first passing probe marks the task healthy at once). Probe interval, timeout and retries are unchanged. DMS 16.0.1 has no env option to skip or limit the startup ownership fix (the per-mailbox `chown -R` in `_create_accounts` is unconditional for FILE provisioning), so none is exposed; the README section "Healthcheck and slow starts" explains how to size the window. Test: `v1/tests/test_skmail_healthcheck_start_period.py`. **Instance action**: an instance that already runs a hand-set `start_period` (for example 3600s via `docker service update --health-start-period`) should set `skmail.HEALTHCHECK_START_PERIOD` to that value, so the next redeploy keeps it instead of dropping to the default; others need nothing.
- v1: **skmail**'s `mail-web` sidecar is optional: `skmail.MAIL_WEB_ENABLED` (default `true`, unchanged). mail-web is a `traefik/whoami` container whose router makes Traefik's ACME resolver obtain a certificate for the mail hostname on an instance that has none. It is not needed for renewal: Traefik renews every certificate stored in `acme.json` on its own timer whether or not a router references it (`renewCertificates`, `pkg/provider/acme/provider.go`, v3.6.2), and a router whose domain a stored certificate already covers (a wildcard included) never obtains one (`getUncheckedDomains`). A production instance whose docker-mailserver reads a wildcard (`SSL_DOMAIN: "*.<domain>"`) had no certificate for the mail hostname in `acme.json` at all, only the wildcard, renewed in the same batch as every other stored certificate; its mail-web did nothing for TLS and published each visitor's request headers and client IP. `false` drops the service and its router from the compose file, and `deploy` removes a leftover `skmail-<env>_mail-web` (`docker stack deploy` does not remove services that left the file). `cert_setup.sh`'s troubleshooting text no longer points at a disabled mail-web. The README section "The mail-web sidecar" explains when it is safe to drop and the one case neither setting covers (a lost `acme.json` needs a router that names the wildcard). Test: `v1/tests/test_skmail_mail_web_optional.py`. **Instance action**: none by default. To drop mail-web, confirm the certificate docker-mailserver uses is in `acme.json`, set `skmail.MAIL_WEB_ENABLED: false`, and redeploy skmail (the removal is part of that deploy).
- v1: **skmesh** gets two auth modes, `skmesh.AUTH_MODE: standalone | authentik` (default `standalone`), and in `authentik` mode the deploy creates the Authentik side itself. Before, `management.json` pointed at an Authentik application nothing created, and NetBird 0.79 management exits at startup when its issuer's discovery document 404s, so skmesh could not come up on its own; it also had an empty `AuthAudience`, no IdP manager, no PKCE/device flow for the CLI, a dashboard asking for a nonexistent `api` scope, prod hostnames in every env, and routers naming their network with `traefik.docker.network`, which the swarm provider ignores. `standalone` = NetBird's embedded IdP (Dex) at `https://<skmesh host>/oauth2` (routed to management; owner created through the dashboard's setup page). `authentik` = sksso: `HttpConfig` on the application issuer, Authentik `IdpManagerConfig` (service account + app password), PKCE for the CLI, optional `DEVICE_AUTH_FLOW`; `tasks/authentik.yml` runs before the stack deploy and, with `sksso.api_token` from sksso's vault, creates or updates (idempotent, drift-correcting) the public OAuth2 provider, the application and a least-privilege IdP-manager account (role: `authentik_core.view_user` only), from a throwaway container on `sksso-<env>` (secrets on stdin, `no_log`; Authentik 2024.4 and 2024.8+ APIs). New sksso helper `src/sksso/ensure-api-token.sh` registers `sksso.api_token` once per cluster. Also: `EXTRA_CA_CERT` (CA for management's HTTPS to a privately signed sksso), env-aware hostnames (`skmesh-<env>`, `sso-<env>`), new `deploy_skmesh-dev.yml` (skmesh-dev 172.16.244.0/24), `management.json` 0600, `traefik.swarm.network` on every routed service. Render gate: `vars/<svc>.with.<other>.yml` stages another service's fake vault. Tests: `v1/tests/test_skmesh_auth_modes.py` (both modes' render, the vault assert, provisioning order, the provisioner against a fake Authentik API). **Instance action**: set `skmesh.AUTH_MODE` explicitly if your vault has `NETBIRD_AUTH_CLIENT_ID` (the deploy refuses to guess). For `authentik`: add `skmesh.IDP_MANAGER_PASSWORD` (`openssl rand -hex 32`) and `sksso.api_token` (`openssl rand -hex 32`), and register the token once with `ensure-api-token.sh` (sksso README). `standalone` needs nothing new.
- v1: **skmesh** (NetBird) can use a shared TURN server, reaches its own relay, pins its images, and no longer makes the management data dir world-readable. (1) TURN: `management.json` hardcoded `turn:turn.<base>:3478` with static credentials and `TimeBasedCredentials: false`, so a cluster that already runs one shared-secret TURN server (coturn `use-auth-secret`, eturnal `secret`) had to run a second one. New optional `skmesh.TURN_URI`, `skmesh.STUN_URIS` and `skmesh.TURN_TIME_BASED_CREDENTIALS` (management then mints HMAC-SHA1 time-limited credentials from `TURN_SECRET`; `TURN_USER`/`TURN_PASSWORD` become optional). Unset, `management.json` renders byte-identical. (2) Relay: the relay service had no Traefik route and sat only on the internal network, and `skmesh.env` stripped the scheme from `NETBIRD_RELAY_ENDPOINT` into `NB_EXPOSED_ADDRESS`, so the relay announced plain `rel://` for a TLS-terminated `:443`: peers could not reach it. It now listens on `:33080`, joins `cloud-public-<env>`, is routed at `PathPrefix(/relay)` on the skmesh host (the dashboard router excludes `/relay`), and keeps the `rels://` scheme. (3) Optional `MANAGEMENT_IMAGE`, `SIGNAL_IMAGE`, `RELAY_IMAGE`, `POSTGRES_IMAGE`, `POSTGRES_BACKUP_IMAGE`; defaults unchanged. (4) The post-deploy task ran `chmod -R 755` over `/var/data/skmesh-<env>/management` (NetBird's `/var/lib/netbird`), the same class as the skgit data dir below; management runs as root, so the dir is now created `0700` and fixed to `u+rwX,go-rwx`, incrementally (only entries whose mode is wrong are touched; see the bounded-walk entry above). Instances: set `NETBIRD_RELAY_ENDPOINT` to `rels://skmesh.<base>:443`; a redeploy re-modes an existing management dir. Test first: `v1/tests/test_skmesh_turn_relay_modes.py` (13 red, 3 default-guard green before; 16 green after).
- v1: **skmon** gains `skmon.replicas` (default 1) and `skmon.GRAFANA_PLUGINS`. Before, an operator who scaled the stack to 0 to save resources got it back at full size on the next deploy, and the Grafana plugin list was hardcoded. `replicas` sets every replicated service (prometheus, grafana, loki, alertmanager, jaeger); at 0 the global services (promtail, cadvisor, node-exporter) keep `mode: global` (swarm cannot change a service mode in place) and get a placement constraint no node satisfies, so they run 0 tasks and scaling back up is a plain redeploy with the var removed. At 0 the deploy script also skips its per-service 1/1 health waits (about 15 minutes of polling otherwise). `GRAFANA_PLUGINS` is a list of plugin ids rendered into `GF_INSTALL_PLUGINS` (`[]` omits the line); the default is the old list. Grafana 13 installs but refuses to load the Angular plugins in that default (`grafana-simple-json-datasource`, `grafana-piechart-panel`, `grafana-worldmap-panel`: "angular plugins are not supported"); startup is not affected and no dashboard shipped here uses them. Core `piechart` and `geomap` panels and `marcusolsson-json-datasource` replace them. With both vars unset every render is byte-identical to before. Test: `v1/tests/test_skmon_replicas_and_plugins.py`; render gate `vars/skmon.set.yml` sets both. **Instance action**: none. Optional: set `skmon.GRAFANA_PLUGINS` to drop the three Angular plugins.
- v1: **skmon** gains `skmon.GRAFANA_BASIC_AUTH` (default true) and `skmon.GRAFANA_HEALTH_START_PERIOD` (default `60s`). Every skmon hostname sits behind the edge `traefikAuth@file` basic-auth middleware, which forwards the browser's `Authorization` header upstream. Grafana tries that header as a Grafana login on every request; when the edge user name equals the Grafana admin name (both are commonly `admin`) each request is a failed admin login, and after a handful Grafana's brute-force protection blocks the admin login entirely ("too many consecutive incorrect login attempts"). `GRAFANA_BASIC_AUTH: false` renders `GF_AUTH_BASIC_ENABLED=false`, so Grafana ignores the header and the login form and session work normally through the edge. Separately, Grafana 13's first boot on a slow shared filesystem (about 720 SQLite migrations plus plugin signature checks) took over two minutes before it listened, longer than `start_period: 60s` plus three 30s retries, so swarm killed each attempt as unhealthy; `GRAFANA_HEALTH_START_PERIOD` sets the grafana healthcheck `start_period`. Both unset: renders byte-identical. Tests: `v1/tests/test_skmon_replicas_and_plugins.py`. **Instance action**: behind `traefikAuth`, set `skmon.GRAFANA_BASIC_AUTH: false`; on NFS-backed data, set `skmon.GRAFANA_HEALTH_START_PERIOD: 5m`.
- v1: **skpdf** secrets no longer live in Stirling-PDF's `settings.yml`. The pinned image (`frooodle/s-pdf:2.14.3`, `scripts/init-without-ocr.sh`) runs `chown -R` and `chmod -R 755` over `/configs` (the `extraConfigs/` bind mount) on every container start, so the 0600 the playbook wrote (v2.21.0, #84) never survived and the initial admin password, the OAuth2 client secret and `AutomaticallyGenerated.key` were readable by any account on the shared `/var/data` (the v2.21.0 known limit). Stirling binds settings with Spring Boot relaxed binding and loads `settings.yml` as `spring.config.additional-location`, so an environment variable overrides the file; the three values now go only to `skpdf.env` (the stack `env_file`, now 0600 root:root, was 0640, under `/var/data/config`, which the image never touches) as `SECURITY_INITIALLOGIN_PASSWORD`, `SECURITY_OAUTH2_CLIENTSECRET` and `AUTOMATICALLYGENERATED_KEY`, and `settings.yml` renders none of them. `AUTOMATICALLYGENERATED_KEY` is emitted only when `skpdf.AUTO_KEY` is set, and the deploy now refuses an `AUTO_KEY` that is not a UUID: Stirling (`InitialSetup.initSecretKey`) silently swapped the old default (`skpdf-<env>`, not a UUID) for a random key and wrote that into `settings.yml`, so the rendered key was never used and a new one was generated on every deploy. Stirling's own state in `/configs` has no env override (the H2 user database with password hashes and per-user API keys, and the generated key when `AUTO_KEY` is unset), so `/var/data/skpdf-<env>`, the host directory above the mount, is now 0750 root:root: the container never re-modes it (dockerd resolves bind mounts as root) and other host accounts can no longer traverse into `extraConfigs/`. Test: `v1/tests/test_skpdf_secrets_not_in_settings_yml.py`; render gate gains `vars/skpdf.set.yml` (login, OAuth2 and `AUTO_KEY` set). **Instance action**: if `skpdf.AUTO_KEY` is set to a non-UUID value, replace it with `uuidgen` output or remove it. Redeploy skpdf so `settings.yml` is rewritten without them, and delete any `extraConfigs/settings.yml.*.bak` (Stirling's own backups of an earlier file keep the old values). Treat the admin password and OAuth2 client secret that sat world-readable as exposed and rotate them.
- v1: **sksec** now works against the framework's own skfenceha. Log acquisition read nothing: with skfenceha the crowdsec service mounted `<log_dir>/acme-node` and `<log_dir>/worker-base` (which the playbook created empty) and `acquis.yaml` globbed `node-*/traefik-access.log` there, but skfenceha writes `<log_dir>/{worker,acme}/access-<node>.log`. It now mounts the whole log dir read-only and acquires those files, with `poll_without_inotify: true` (the logs are on the shared filesystem and other nodes' writes raise no inotify event on the CrowdSec node), labelled `type: traefik` for the stock `crowdsecurity/traefik-logs` parser (the `traefik-v3` label pointed at a custom parser the playbook never deployed; that file is removed). The bouncer key never reached the bouncer when the vault left `CROWDSEC_BOUNCER_API_KEY` unset: `sksec.env` rendered the empty vault value instead of the generated, persisted key; it now renders the computed key. The bouncer was registered by a `cscli bouncers add --key <key>` task that ran before the stack deploy (a first install found no container and skipped it), only looked on the deploy node, and put the key on a process command line; CrowdSec now registers bouncer `traefik` itself at start from `BOUNCER_KEY_traefik` in `sksec.env`. With `TRAEFIK_HA_MODE` the playbook removed `traefik.acme.master` from every manager and re-added it on its own deploy node, which could move skfenceha's ACME master and made Swarm stop skfenceha's constrained tasks in between; sksec now only reads the label. New `sksec.ALLOWLIST_CIDRS` renders a CrowdSec whitelist parser for the operator's own ranges, and `sksec.CROWDSEC_IMAGE` / `sksec.BOUNCER_IMAGE` let an instance pin digests (defaults unchanged). The README documents that the bouncer fails closed and that `dynamic/sksec.yml`, written from one node onto the shared filesystem, may not be loaded by other nodes' Traefik until they restart. Test: `v1/tests/test_sksec_acquisition_and_bouncer.py`. **Instance action**: set `sksec.ALLOWLIST_CIDRS`; a running sksec must be redeployed to acquire logs. Its existing bouncer keeps its registration (the env-var path never re-keys one); if it was registered with a different key than the one now in `sksec.env`, run `cscli bouncers delete traefik` (and the old `bouncer-traefik`) and restart crowdsec. Remove the stray empty `acme-node`/`worker-base` dirs under the Traefik log dir.
- v1: **sksec** first install with a generated bouncer key works. With `CROWDSEC_BOUNCER_API_KEY` unset the deploy generates `secret/bouncer.key`, but the task that reads it back was gated on the existence check taken before the generation, so on a fresh install the new key was never read, the computed key was empty and every first deploy stopped with "CROWDSEC_BOUNCER_API_KEY could not be determined" (found by the skstack06 v2.22.0 run; a vault that sets the key was not affected). The read now runs whenever the vault key is unset. Test: `v1/tests/test_sksec_first_install_bouncer_key.py` runs the playbook's own key tasks through real Ansible for dev, staging and prod (first install, redeploy keeps the key, vault key wins; 6 red before, 12 green after). **Instance action**: none.
- v1: **skwhoami** gains `skwhoami.MIDDLEWARES`, a map of service name (`whoami01`..`whoami03`) to that router's Traefik middlewares string, so one smoke-test router can canary a new edge middleware (such as sksec's fail-closed `crowdsec-bouncer@file`) through the vault instead of a hand edit of live labels. Unset routers keep `default-no-crowdsec@file`. Test: `v1/tests/test_skwhoami_middlewares_hook.py`. Instance action: none; unset renders exactly as before.

## skstacks-v2.21.0 - 2026-09-27

- v1: **skfence** and **skfenceha** wildcard catch-all routers (`wildcard-domain`, `wildcard-cluster`) now actually match. They rendered ``HostRegexp(`{subdomain:.+}.<domain>`)``, Traefik v2 placeholder syntax; the framework pins `traefik:v3.6.2` with no `defaultRuleSyntax`/`ruleSyntax`, so v3 compiled it as a plain regexp that never matched a real host, and unknown subdomains got Traefik's own bare 404. The rules are now anchored v3 regexps with the domain's dots escaped (``HostRegexp(`^[a-z0-9-]+(\.[a-z0-9-]+)*\.example\.test$`)``). Their `priority` changes from `0` to `1`: Traefik reads `0` as "use the rule length", and the new regexps are longer than a typical service `Host()` rule, so at `0` the catch-all would have outranked real service routers. Verified against `traefik:v3.6.2`: an unknown subdomain went from 404 to the error-pages backend, a label-style `Host()` service router still wins, and a host under a different domain still gets 404. **Instance action**: after the next skfence/skfenceha redeploy the catch-alls start matching, so requests for unknown subdomains of the domain and cluster now reach the catch-all target (error-pages) instead of Traefik's 404. If anything relied on that 404 (monitoring probes, a router with an explicit `priority: 1` or lower that overlaps these hosts), adjust it before redeploying.
- v1: **skfence** and **skfenceha** gain `skfence.EXTRA_DYNAMIC_FILES` / `skfenceha.EXTRA_DYNAMIC_FILES`: an optional list of `{src, dest[, acme]}` entries for instance-owned Traefik dynamic config (for example a hypervisor web UI that is not a Swarm service), rendered into `dynamic/` right after the fixed dynamic files in the dev, staging and prod playbooks. `src` is absolute or relative to the instance's inventory directory (`inventory_dir`); `dest` must be a plain file name ending `.yml`/`.yaml` that does not shadow a framework file (`routers.yml`, `services.yml`, `middlewares.yml`, `tls.yml`, `sksec.yml`); `acme: true` (skfenceha only) also renders into `dynamic_acme/`. Every entry is validated before anything is written. skfenceha now has its own README, which documents the option (as does the skfence README). Instance action: none; default (unset or empty) renders nothing.
- v1: **skfence** and **skfenceha** Traefik log rotation now actually rotates, and only what it safely can. In a production deploy the rendered `/etc/logrotate.d/<app>-<env>` rotated nothing for months and the ingress node's access log grew to tens of GB (~1.2 GB/day): the path was a glob over the shared log dir (an instance copy was one level too deep and matched nothing, hidden by `missingok`); at the framework's depth it also caught Traefik's main log, which Traefik v3 rotates itself and does not reopen on USR1, so an external rename strands its write fd on a deleted inode; and the postrotate never reached Traefik (skfenceha's `pkill -USR1 -f "traefik"` cannot match the container's PID 1 `traefik --configFile=...` and would also signal unrelated processes such as traefik-certs-dumper; skfence read a `/var/run/traefik.pid` that does not exist). Each node's file now lists only that node's ACCESS log(s), exactly as Traefik's `filePath` names them (skfenceha: `<log_dir>/{worker,acme}/access-<hostname>.log`; skfence: `<log_dir>/<node>/access.log`), and the postrotate sends `docker kill -s USR1` only to this node's Traefik containers, found by `label=com.docker.swarm.service.name=<app>-<env>_traefik[-worker|-acme]` (none on the node is not an error; containers are never restarted). Settings follow the validated production config: `su root root`, `daily` plus `maxsize`, `dateext` with `dateformat -%Y%m%d-%s` (several size rotations a day never collide), `compress` + `delaycompress`, `missingok`, `notifempty`, `create 0644 root root`, `sharedscripts`. New knobs `<svc>.LOGROTATE_MAXSIZE` (default `200M`, was a hardcoded `100M`) and `<svc>.LOGROTATE_ROTATE` (default 7 in prod, 3 in dev/staging, as before). skfenceha now installs logrotate and a per-node file on every host of `target_manager_group` (traefik-worker is global over the managers), named by each node's `hostname`, the same name Swarm hands `entrypoint.sh`; skfence keeps installing on the deploy node. **Instance action**: redeploy skfence/skfenceha to replace the broken `/etc/logrotate.d/<app>-<env>` (skfenceha: on every manager in `target_manager_group`; a manager outside it that runs traefik-worker gets no file). Before the first nightly run, check the current access log sizes under `/var/data/logs/<app>-<env>/traefik/`: an access log that has grown for months is renamed whole by the next nightly run and gzipped single-threaded by the run after, so a very large one is better moved aside, reopened with `docker kill -s USR1 <traefik container>` and compressed by hand first. From then on logrotate deletes archives older than the rotate count. Rotated main logs (`traefik*.log`) are Traefik's own and are left alone.
- v1: a deploy never deletes an existing overlay network any more. The network setup in nine `deploy.j2` scripts (skboard, skbook, skgraph, skorch, skpdf, skpeek, skseek, sksso, skvector) and in the shared `create_networks.yml` task (used by skfence, skfenceha, sksec and most optional services) ran `docker network rm` and recreated any network whose live subnet differed from the configured or default one. Shared networks such as `cloud-public-<env>` and `cloud-socket-proxy-<env>` carry other stacks' services, so deploying one service could delete the network another one depends on (it failed only if something was attached at that instant), and deleting a service's own network cut it off mid-deploy. `create_networks.yml` was worse: it parsed `{{.IPAM.Config}}` as JSON, never read a subnet, and so tried to remove every existing network on every run, matching ones included, swallowing the error when the network was in use. Now an existing network with the configured subnet is reused, a missing one is created as before, and an existing one with a different subnet stops the deploy with an error naming the network, both subnets and the vault key that feeds it (`<svc>.networks` or `<svc>_<env>_networks`, with any `-` in the app name read as `_`, so **skmem-pg** names `skmem_pg.networks`, the key its playbooks read). A static check requires every `create_networks.yml` caller to feed `app_networks` from one of the two keys the error can name. `NETWORK_RECREATE_ON_MISMATCH=true` (env for `deploy.j2`, or the vault/`-e` variable `network_recreate_on_mismatch`) opts in to recreating the service's own `<svc>-<env>` network only; shared `cloud-*` networks are never recreated. skorch, skpdf and skseek `deploy.j2` now read `app_networks` (their playbooks' `<svc>_<env>_networks`) instead of hardcoded subnets, with the same defaults. A failed network create in skboard, skbook, skgraph, skpeek and skvector now fails the deploy instead of "continuing anyway". Tests: `v1/tests/test_networks_never_deleted.py` covers the nine `deploy.j2` scripts, `create_networks.yml`, and **skfence** / **skfenceha**: their `deploy.j2` must not create or remove networks, every env playbook must route its networks through `create_networks.yml`, and a subnet mismatch on their shared `cloud-*` networks stops the deploy without deleting anything even with `network_recreate_on_mismatch=true`, naming `skfence_<env>_networks` / `skfenceha_<env>_networks`. **Instance action**: a deploy now fails instead of recreating a network whose live subnet differs from the configured one. Before redeploying, set the network subnets in the vault to the live values (`docker network inspect <name> --format '{{range .IPAM.Config}}{{.Subnet}}{{end}}'`),.
- v1: secret-bearing files are no longer written world-readable. PR #71 fixed `skmail.env`; the same class remained in 11 more services, rendered `0644` onto the shared filesystem (or a node's `/etc`), so any account on any node mounting the share could read them. Now `0600` root:root unless noted: env files **skpeek.env** (its `extra_env` may carry `SEARXNG_SECRET_KEY` or any other secret), **sksso.env** (Authentik secret key, DB and SMTP passwords, GeoIP license key) and **sksso-backup.env** (DB password), read by the docker daemon via compose `env_file`; the compose files of **skmem-pg**, **skmesh**, **skorch**, **skreg** and **skdesk**, which inline a DB password, TURN secret, GPG signer API secret, registry HTTP secret or the RustDesk key, read only by the root-run deploy script; **skha** `/etc/keepalived/keepalived.conf` (VRRP `auth_pass`, read by keepalived as root); **skgit** `app.ini` (Forgejo secret key, internal/JWT/metrics tokens, DB and mail passwords), still owned `1000:1000`, the container's user; **skpdf** `extraConfigs/settings.yml` (OAuth2 client secret, initial admin password, API key), now owned `1000:1000`, the Stirling-PDF runtime user (`PUID` default), which the container's entrypoint also chowns `/configs` to; **skmon** `prometheus/prometheus.yml` (scrape `bearer_token`s) is `0640` `root:65534`, since Prometheus runs as `nobody` (65534). New `v1/tests/test_secret_files_not_world_readable.py` walks every v1 deploy playbook and the task files it imports, resolves each `template` task's src, dest and mode through its loop, and fails when a file whose dest is `*.env`, or whose template interpolates a secret-looking variable (PASSWORD, SECRET, TOKEN, `*_KEY`, `extra_env`, ...), is rendered with any "other" permission bit or with no mode. Proven non-secret env files stay as they are and are allow-listed with a reason: `skmail.sh.env` (CLUSTERNAME/DOMAIN/INSTANCE/APP_ENV/HOSTNAME for the helper scripts), `skpulse.env` (base URL, CLUSTERNAME, DOMAIN, PORT) and `skwhoami.env` (DOMAIN, CLUSTERNAME, APP_ENV); the allow-list only waives the `*.env` rule, so one of them fails again the moment it interpolates a secret. Known limit: **skpdf**'s Stirling-PDF image entrypoint runs `chown -R` and `chmod -R 755` over `/configs` on every container start, so on a running node `extraConfigs/settings.yml` is 0755 whatever mode the playbook writes; until that is addressed, treat its secrets as readable by any account that can reach the shared filesystem. **Instance action**: redeploy the affected services (skdesk, skgit, skha, skmem-pg, skmesh, skmon, skorch, skpdf, skpeek, skreg, sksso) so the modes are rewritten, and check instance-side copies or overrides of these files for the same modes. Treat every secret those files held while world-readable on the shared filesystem as exposed, and rotate it.
- v1: secret-bearing files now keep their mode after the deploy that writes them. **skreg**'s `docker-compose.yaml` symlink task set `mode: 0755`, and Ansible's `file` module follows a link, so every deploy re-moded `skreg.yml` (registry HTTP secret) to 0755 right after the template wrote it 0600. **skgit**'s post-deploy permission fix ran `chmod -R 755` over the config directory, re-moding `app.ini` (secret key, tokens, DB and mail passwords) to 0755. The symlink task no longer sets a mode, and skgit now chmods only the config directory itself. New `v1/tests/test_secret_file_modes_survive_deploy.py` walks every deploy playbook in task order and fails when a later task re-modes a secret-bearing file (a mode on a followed link task, a `file` mode granting other read, a shell `chmod` on the file, or `chmod -R` on a parent directory). Found by the skstack06 release test, which checks the modes on a running node. **Instance action**: redeploy skreg and skgit, or `chmod 0600` the two files by hand.
- v1: **skpeek** gains `skpeek.SETTINGS_YML`: an instance-owned SearXNG `settings.yml` (a raw YAML string), written verbatim in place of the framework template, which only exposed `debug`, `instance_name`, `default_theme` and `secret_key` and so could not express a hand-written minimal file (no `use_default_settings`, its own engine list, outgoing timeouts, resolvers, `redis.url`). The value must parse as a YAML mapping; invalid YAML, a scalar or list, or a YAML map instead of a string stops the deploy before `settings.yml` is written, with an error naming the key that never prints the value. Tag it `!unsafe` in the vault so Ansible does not template `{{QUERY}}`-style content. Unset or empty, `settings.yml` renders from the template byte-identical to before. `settings.yml` is now written `0640` (was `0644`) on both paths, since it carries `server.secret_key`; the playbook writes it `root:root`, and the SearXNG entrypoint (which starts as root) re-owns `/etc/searxng` to its `searxng` user (977:977 in the pinned image) on every start, so on a running node the file is `0640 977:977`: readable by root and the container user only. The rendering moved from the config loop into `skpeek/tasks/settings_yml.yml`, imported by all three env playbooks; skpeek gains a README. Instance action: none, unless you want a custom `settings.yml`; the value can hold `server.secret_key`, so keep it in the vault.
- v1: **skpulse** `discover-traefik-frontends.sh` now parses. `extract_fqdn`'s sed expression had an unescaped backtick inside double quotes (`[^`]*`), so bash read the rest of the file as an unterminated command substitution (`bash -n`: "unexpected EOF while looking for matching ``'") and the script died when it reached the function. Test: `v1/tests/test_shell_scripts_parse.py` (every shipped `.sh` must pass `bash -n`). **Instance action**: none; the next skpulse redeploy copies the fixed script.
- CI: new `ansible-render` job in `framework-tests.yml` renders every `v1/ansible/*/*/deploy_<svc>-<env>.yml` through real Ansible (ansible-core 2.21.3, pinned) instead of plain jinja2, with public fake example vars (`v1/tests/render/vars/<svc>.example.yml`, plus a second pass with `<svc>.set.yml` on top that drives the optional hooks down their set path). Rendered output is checked against the Compose Specification schema and the swarm checks, YAML/JSON/TOML parsing, `bash -n` on rendered shell (task bodies included), leftover Jinja, `None` env values, `\n` inside single-quoted `php -r` strings, and, where a service README documents its vault keys, that every key the example needs is documented. Catches Ansible-only bugs such as an `is mapping` test on an absent key, which the plain-jinja2 suite passes. Run locally: `python v1/tests/render/render_playbooks.py [--service NAME] [--env dev] [--pass base] [--keep DIR]`. **Instance action**: none. A new service, or a new required vault key, needs its example vars file updated or this job fails.

## skstacks-v2.20.0 - 2026-09-27

- v1: **skgit**'s admin bootstrap now accepts the vault keys its playbook header documents, `skgit.ADMIN_PASSWORD` / `skgit.ADMIN_EMAIL` (and `ADMIN_USERNAME`), as well as the lowercase `admin_password` / `admin_email` / `admin_username` (lowercase wins). Before, only the lowercase keys worked, so an instance following the documented uppercase names never got its admin user. Instance action: none.
- v1: **skgit**, **skstor** and **skgallery** first-install waits now fit a slow first boot (same pattern as skhub). skgit: Forgejo's healthcheck has `start_period: 300s`, but "Wait for Forgejo container to start and fix permissions" polled 30 x 2s (and, as `{1..30}` under the shell module's `/bin/sh`, really only once) and then ran the chown and `start-runners.sh` anyway, and "Create admin user" polled the API 60 x 2s and then ran `forgejo admin user create ... || true` under `ignore_errors` and `no_log`, so a slow first boot skipped the requested admin user without a trace. A new "Wait for the swarm node running the Forgejo task" (fails the play, reports task states on stderr) and "Wait for the Forgejo API" derive their retries from one guarded knob, `skgit.first_install_wait_seconds` (default 1800), announced up front; permissions and runners run only after it. The admin bootstrap now runs on the node that holds the Forgejo task, passes the credentials via the environment, uses `--must-change-password=false`, treats an existing user as success, and when `skgit.admin_enabled` is true a failed creation fails the play with Forgejo's reason (password redacted). `skgit.admin_enabled` without `skgit.admin_password` and `skgit.admin_email` is now refused up front instead of silently skipped. skstor: "Wait for Garage service to report 1/1 replicas" (was 12 x 5s) derives its retries from `skstor.first_install_wait_seconds` (default 600), announced up front, with the replica count on stderr each attempt. skgit and skgallery `deploy.j2`: the replica health poll (was 36 x 5s) now runs for `HEALTH_WAIT_SECONDS` (env var, default 900); it stays warning-only. Instance action: an instance with `skgit.admin_enabled: true` must also set `skgit.admin_password` and `skgit.admin_email`; otherwise none. Set the knobs to shorten or extend the waits.
- v1: **skhub** no longer fails a slow first install. Nextcloud has a Docker healthcheck, so Swarm reports its task Running only once it is healthy, after the entrypoint copy and the first DB install; on a memory-pressured host that took ~9 minutes while "Wait for the swarm node running the Nextcloud task" gave up after 5 (60 x 5s) and failed the play although Nextcloud finished installing. That wait and "Wait for Nextcloud to finish its first-run install" (was 60 x 10s) now derive their retries from one guarded knob, `skhub.first_install_wait_seconds` (default 1800), in dev, staging and prod. A debug task announces the budget before waiting, and each failed attempt reports the current task states (e.g. `Starting 4 minutes ago`) on stderr, visible with `-vvv` and in the final failure. Instance action: none; set the knob to shorten or extend the wait.
- v1: **skfenceha** gains a second batch of optional live-parity hooks, all defaulting to the previous render (byte-identical when unset). Dynamic middlewares: `skfenceha.SECURITY_HEADERS` (map of Traefik `headers` options that replaces `default-security-headers`' own options entirely, no per-key merge; the `CORS_ALLOWED_ORIGINS` block still follows) and `skfenceha.DASHBOARD_AUTH_REALM` (default `TraefikDashboard`; empty omits `realm`). Routers: `skfenceha.ROUTER_CERT_RESOLVER` (default `main`) sets the resolver on every router except `dashboard` when ACME is on; empty renders `tls: {}` so Traefik serves a stored certificate matching the SNI without asking the resolver. ACME master `traefik.yml`: `skfenceha.ACME_DOCKER_PROVIDER_ENABLED` (default `false`) adds the same `providers.docker` block the worker already has. Worker `traefik-worker.yml`: `skfenceha.WORKER_LOG` (map, renders a static `log:` block; `entrypoint.sh` still injects `filePath` and replaces `level` with `LOG_LEVEL`), `skfenceha.ACCESS_LOG_FILTERS` (map, `accessLog.filters`), `skfenceha.ACCESS_LOG_FIELDS` (map that replaces the whole `accessLog.fields` block, `ACCESS_LOG_HEADERS` included), `skfenceha.WORKER_GLOBAL` (map replacing `global`; `{}` omits the block) and `skfenceha.WORKER_TLS` (map, default none; INERT, parity-only: Traefik has no static `tls:` section and silently ignores the block, so it only reproduces a live file byte for byte. Real TLS options/stores go in the dynamic tls config, `dynamic-tls.yml.j2` rendered as `tls.yml`). The dashboard `basicAuth` users entry is `skfenceha.DASHBOARD_AUTH_HASH` rendered verbatim (no `$$` escaping), so a hash that differs from a live deploy is a vault value to update, not a template issue. Instance action: none; to match an existing deploy, set these to its live values.
- v1: **skmail** no longer stores the SMTP relay password world-readable. `user-patches.sh` inlined `RELAY_USER` and `RELAY_PASSWORD` and was written 0755 on the shared `/var/data`, and `skmail.env` (the container env_file, which carries `RELAY_PASSWORD`) was written 0644. The script now reads `RELAY_HOST`/`RELAY_PORT`/`RELAY_USER`/`RELAY_PASSWORD` from the container environment (docker-mailserver passes env_file vars in) and skips relay setup with a warning when they are unset; it holds no secret and stays 0755. `skmail.env` is now rendered 0600 owned by root in dev, staging and prod (`skmail.sh.env`, no secrets, stays 0644). The SASL map is still written only when the domain line is absent, now under umask 077, and still chmod 600. Instance action: none; a redeploy rewrites both files. Treat previously rendered copies as exposed and rotate the relay password if the share was readable by others.
- v1: deploys no longer force-restart a service on its **first install**. 23 `deploy.j2` scripts (all that force-update except skboard, fixed separately in v2.19.1) ran `docker service update --force` right after `docker stack deploy`, which on a fresh install killed the first containers in the middle of first-boot init (schema migrations, initial setup, initdb); an interrupted Vikunja SQLite migration left a corrupt db that failed every later start, and Forgejo, Authentik, n8n, BookStack, Nextcloud, Immich and the bundled databases were exposed the same way. Each script now records which of those services already exist before the stack deploy and force-updates only them afterwards, printing `First install of <svc>: skipping forced restart` for new ones. Redeploys still restart to pick up changed bind-mounted config; timeouts, `|| true` and health polls are unchanged. Instance action: none.
- ci: **skred** baseline gains an any-commit entry for the made-up sentinel password in `v1/tests/test_skgit_first_install_waits.py` (asserts the skgit admin password never reaches output; not a credential), so the scan passes on the skgit first-install tests. Instance action: none.

## skstacks-v2.19.1 - 2026-09-27

- v1: **skhub**'s Redis post-deploy step wrote Nextcloud's `config.php` with a literal backslash-n (PHP single quotes), breaking the file ("syntax error, unexpected variable $CONFIG") the first time it had to write. It now uses `PHP_EOL`. Instance action: none if skhub was never deployed with v2.19.0; if it was and Nextcloud reports that parse error, replace the literal `\n` after `<?php` and after the final `;` in `config.php` with real newlines, then redeploy.
- v1: **skboard** fresh installs no longer corrupt the Vikunja SQLite db. The deploy ran `docker service update --force` on `<stack>_vikunja` ~5s after `docker stack deploy`, which on a first install killed the first Vikunja task mid-way through its initial migrations (slow on NFS) and left a half-migrated `vikunja.db`, so every later start failed with `Migration failed: migration 20200515172220 failed: no such column: done_at_unix` (or `table tasks_dg_tmp already exists`). `deploy.j2` now checks whether the vikunja service exists before `docker stack deploy` and only forces the update on a redeploy (where it still picks up changed bind-mounted config); on a first install it is skipped. Instance action: an instance whose first skboard deploy failed with "Migration failed" must remove its half-created `data/vikunja.db` (fresh instance, no data) and redeploy; others need nothing.

## skstacks-v2.19.0 - 2026-09-26

- v1: **skhub** post-deploy steps, including the notify_push install, now run on the node hosting Nextcloud. Instances pin skhub to workers (`skhub.placement_constraints`), but the container lookup and every `docker exec` ran on the play's manager, found no container there, and were silently skipped (`ignore_errors` plus a `length > 0` guard), so notify_push was never installed and its service failed forever with `exec .../custom_apps/notify_push/bin/x86_64/notify_push: no such file or directory`. The playbooks now resolve the node running `<app>-<env>_nextcloud` via `docker service ps` (polled up to 5 minutes, the same approach as skstor's garage bootstrap), `delegate_to` it for the container lookup and all `docker exec` steps, and wait up to 10 minutes for Nextcloud's first-run install before any `occ` call. The container lookup, Redis config/wait and notify_push steps now fail the play instead of being skipped; optional app and tuning steps (Collabora, Talk, recognize, ClamAV, maintenance) stay best-effort. Instance action: none; a redeploy installs notify_push where it was missing.
- v1: **skmail**'s `user-patches.sh` only waits for `/etc/dms/tls` certificates and rewrites Dovecot's cert paths when `skmail.SSL_TYPE` is `manual` (the traefik-certs-dumper bind). With `letsencrypt` (default) or `self-signed` it no longer blocks startup for up to 300s, which outlasted the healthcheck and got the container killed as unhealthy in a loop. Instance action: none.
- v1: **skmail** fresh installs no longer restart-loop on "You need at least one mail account to start Dovecot". New optional vault keys `skmail.BOOTSTRAP_ACCOUNT` and `skmail.BOOTSTRAP_PASSWORD` (first install only): when both are set and `docker-data/dms/config/postfix-accounts.cf` does not exist, the deploy playbooks write it with one `<account>|{SHA512-CRYPT}<hash>` line (hash from `openssl passwd -6 -stdin` on the target, password on stdin only, tasks `no_log`, root:root 0640), before the stack deploy. An existing `postfix-accounts.cf` is never touched. `add_user` is fixed: it ran `docker exec skmail-<env>_skmail`, the Swarm service name, which is not a container, so it always failed; it now resolves the local task container (`name=skmail-<env>_skmail.`), says which node hosts the task when it is not local, and reads the password from stdin instead of argv. **Behaviour change**: `add_user <email> <password>` is now `add_user <email>` with the password on stdin. Instance action: for a new instance, set both keys; existing instances need nothing.
- v1: **skboard** no longer bind-mounts its data dir over `/app/vikunja` (the image's install dir): that hid the Vikunja binary, so a fresh deploy failed every task with `exec /app/vikunja/vikunja: no such file`. The data dir is now mounted at `/db` (`VIKUNJA_DATABASE_PATH=/db/vikunja.db`, the same `vikunja.db` file) and `data/files` at `/app/vikunja/files`. Instance action: none; an existing data dir keeps its layout, and a stale Vikunja binary copied into it is simply ignored, so image upgrades now take effect.
- v1: **skbook** and **skhub** deploys now create every bind-mount directory their compose files use. Docker Swarm never creates a bind source, so a fresh cluster rejected `skbook-<env>_bookstack` ("bind source path does not exist: /var/data/skbook-<env>/bookstack/config"; the playbooks only created its parent) and could reject skhub's `imaginary` (`/var/data/skhub-<env>/html/data`, which otherwise only appeared as a side effect of Nextcloud's own nested mount on that node). `bookstack/config` gets the same PUID/PGID ownership (default 1000) as `bookstack`; `html/data` is `www-data` like `html`. A new generic test (`v1/tests/test_bind_mount_sources_created.py`) checks every `/var/data` bind source (short and long volume syntax, plus local-driver bind `device:`) in every v1 compose template against the directories, rendered files and `mkdir -p` paths the dev/staging/prod playbook and `deploy.j2` create. No instance action needed; existing instances already have these directories.
- v1: deploys no longer hang forever on an unschedulable service. Every `docker service update --force --detach=false` in the 21 `deploy.j2` scripts is now wrapped in `timeout "${SERVICE_UPDATE_TIMEOUT:-300}"` (new env var, default 300s). `--detach=false` has no timeout of its own, so a task stuck Pending (for example "no suitable node (insufficient resources)") blocked the script and the Ansible play indefinitely. Each call keeps its `|| true`, so the replica health poll that follows still runs and reports the stuck service unhealthy (with its recent logs) instead of the deploy hanging.
- v1: **skfenceha** gains optional live-parity hooks on the worker-role Traefik, all defaulting to the previous render (byte-identical when unset): `skfenceha.WORKER_RESPONDING_TIMEOUTS` (map, rendered verbatim as `entryPoints.websecure.transport.respondingTimeouts`; unset renders no transport block), `skfenceha.WORKER_FORWARDING_TIMEOUTS` (map that replaces `serversTransport.forwardingTimeouts` entirely, no per-key merge; default `responseHeaderTimeout: 30s`/`dialTimeout: 10s`/`idleConnTimeout: 90s`) and `skfenceha.GZIP_COMPRESS` (map that replaces the `gzip` middleware's `compress` options in the dynamic middlewares; `{}` means Traefik's own defaults; default the existing `includedContentTypes` list plus `minResponseBodyBytes: 1024`). The 30s response-header default cuts off multi-GB skhub (Nextcloud) uploads during chunk assembly. Instance action: to match an existing deploy, set these to its live values (for example: responding `readTimeout: 7200s`, `writeTimeout: 7200s`, `idleTimeout: 1800s`; forwarding `responseHeaderTimeout: 7200s`, `dialTimeout: 30s`, `idleConnTimeout: 1800s`; `GZIP_COMPRESS: {}`).
- v1: **skhub** gains `skhub.REDIS_RESOURCES_LIMITS_MEMORY` / `skhub.REDIS_RESOURCES_RESERVATIONS_MEMORY` (skdash/skmail `RESOURCES_*` naming, `REDIS_` prefixed; defaults `256M`/`64M`, unchanged) for the redis service, and `skhub.TALK_HPB_TZ` for talk-hpb's `TZ` (defaults to `skhub.TZ`, then `UTC`, so unset is unchanged), letting Nextcloud and talk-hpb run different time zones. Instance action: to match an existing deploy, set these to its live values (for example: redis `1.5G`/`512M`, `TALK_HPB_TZ: UTC` alongside `TZ: America/New_York`).
- v1: **skbook** gains `skbook.placement_constraints` (list, rendered with `| tojson` like skorch/skgit), applied to bookstack, db and db-backup, which each hardcoded `node.role == worker`. Default `["node.role == worker"]` keeps the current render. The pin is not load-bearing where `/var/data` (including `/var/data/runtime`, home of the db volume's bind device) is one NFS export mounted on every node, as on skstack01; the template comment that called that path "a local bind on the manager, not NFS" was wrong and is corrected. Instance action: to match an existing deploy with no constraint (skstack01 prod runs unconstrained, tasks on managers), set `skbook.placement_constraints: []`.
- v1: **skmail** gains optional live-parity hooks, all defaulting to the previously hardcoded values so an unset instance renders unchanged: `skmail.RESOURCES_LIMITS_CPUS`/`RESOURCES_LIMITS_MEMORY`/`RESOURCES_RESERVATIONS_CPUS`/`RESOURCES_RESERVATIONS_MEMORY` on the DMS service (skdash's naming; defaults `1.5`/`3G`/`0.5`/`1G`; an empty reservation value is omitted, both empty drops the reservations block), `skmail.SSL_DOMAIN` (renders `SSL_DOMAIN=` in skmail.env only when set), `skmail.TLS_DOMAINS` (list of domain strings or `{main, sans}` maps, rendered as `tls.domains[N].main`/`.sans` labels on the `mail-web-<env>` router; default none), `skmail.MAIL_WEB_IMAGE` (default `traefik/whoami:v1.12.0`) and `skmail.MAIL_WEB_PLACEMENT_CONSTRAINTS` (default `["node.role == manager"]`; the DMS service's own placement is unaffected). Instance action: to match an existing deploy, set these to its live values (for example: limits `1`/`1G` with both reservations `""`, its `SSL_DOMAIN` and TLS domain label, `traefik/whoami:v1.11.0`, and its hostname pin plus `node.role == manager`).
- v1: **skhub**'s `skhub.env` now renders one variable per line. Under Ansible's trim_blocks, whitespace control glued `DEFAULT_PHONE_REGION` and `NEXTCLOUD_CUSTOM_APPS` onto one line (`DEFAULT_PHONE_REGION=USNEXTCLOUD_CUSTOM_APPS=...`: a wrong phone region, and `NEXTCLOUD_CUSTOM_APPS` never set), and in `storage_backend: s3`/`skstor` mode glued `OBJECTSTORE_S3_BUCKET` and `OBJECTSTORE_S3_HOST` together (`OBJECTSTORE_S3_BUCKET=skhubOBJECTSTORE_S3_HOST=...`). New `skhub.placement_constraints` (list, default `[]`, unchanged behaviour) applies to every skhub service; live prod runs `node.role == worker`. Instance action: redeploy skhub so the env file is rewritten; an s3/skstor instance should check Nextcloud's objectstore bucket/host afterwards, and set `skhub.placement_constraints: ["node.role == worker"]` to match prod. A new generic test renders every v1 `*.env.j2` and fails on any line holding two `KEY=` assignments; no other template was affected.
- v2 infra/tofu: **libvirt-cluster** module gains `disable_auto_updates` (bool, default `false`; unchanged default renders byte-identical cloud-init). When set true, cloud-init writes apt periodic-update/download/unattended-upgrade config to "0", disables Unattended-Upgrade::Automatic-Reboot, points needrestart at list-only mode (`$nrconf{restart} = 'l'`), and disables+masks apt-daily.timer, apt-daily-upgrade.timer, apt-daily.service, apt-daily-upgrade.service, unattended-upgrades.service and motd-news.timer, plus holds snapd refresh if snapd is present; all of it lands via runcmd, which cloud-init always finishes before the packages block below installs the HWE kernel, so none of it races that install. Root cause: an skstack06 manager rebooted itself unattended ~15 min after boot mid test run. For short-lived test clusters, set `disable_auto_updates = true` on the module call; it changes nothing for any cluster that leaves it unset.
- v1: **skfenceha**'s worker-role Traefik config now renders a static `accessLog.fields.headers` block, driven by the new `skfenceha.ACCESS_LOG_HEADERS` instance var (`{defaultMode: keep|drop|redact, names: {<Header>: keep|drop|redact, ...}}`). **Behaviour change**: unset, the framework default now redacts `Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key` and `Proxy-Authorization` in access logs (`defaultMode: keep` for everything else) instead of relying entirely on Traefik's own built-in default. This is a safe improvement for every instance; set `ACCESS_LOG_HEADERS` explicitly only to change the set of redacted headers.
- v1: **skfence** and **skfenceha** gain `skfence.DASHBOARD_HOST` / `skfenceha.DASHBOARD_HOST`, an optional literal-hostname override for the dashboard/API router group (`dashboard`, `dashboard-redirect-http`, `dashboard-redirect-https`) only; every other router (`catch-all`, both wildcards) is unaffected and keeps building the normal `<name>[-env].<cluster>.<domain>` hostname. Default (unset) is unchanged.
- v1: **skha** health check entries (`skha.health_checks`) gain a per-entry `require: true` mode: when a check's `container_pattern` is absent, the check now FAILS (exits non-zero) instead of silently skipping, for checks whose container is expected to always be present on that node. Default (`require` unset/`false`) is unchanged: absent-container checks still skip.
- v1: image-pin test hardened to also reject variant-only tags (`redis:alpine`) and major-only tags (`registry:2`, `postgres:16`) unless digest-pinned, not just the literal `latest`/`lts`/`stable`/`release`/`edge`/`main` it already caught. Every image the strengthened test now catches is pinned: where skstack01 prod runs the same image:tag, to the exact version resolved from prod's running digest; elsewhere, to the current stable release. No instance action needed unless an instance vault overrode one of these images with its own floating tag, in which case pin it to a real version. sksync's image is now an instance-overridable var (`sksync.SYNCTHING_VERSION`) like every other service; the pinned value is unchanged.
- v1: skboard, skdesk, skorch, skpeek, skport, skpulse, skseek, sksync and skwhoami now fail closed on `CLUSTERNAME`/`DOMAIN` instead of falling back to a literal placeholder (`cluster1`/`skstack01`, `example.com`) when neither the instance override nor the inventory host var is set. Falling back to the inventory host vars (`cluster_name`/`domain`) still works unchanged; only the literal default is gone. Instance action: any of these nine services deployed without `CLUSTERNAME`/`DOMAIN` set (directly or via inventory) will now fail to deploy instead of silently routing under `<service>.cluster1.example.com` (or `.skstack01.` for skport/skwhoami) - set the vars before the next deploy.
- docs: added the `v1/ansible/core/skfence/README.md` skfenceha's own comments already pointed at; aligned skport/skwhoami's README deploy command with the framework convention (`envs/<env>/inventory.ini` + `target_manager_group`, not `v1/ansible/shared/hosts` + `-e env=`); added a skstor README deploy example; clarified skgallery's `IMMICH_TELEMETRY_INCLUDE` comment (it selects which categories Immich's own local metrics endpoint exposes, gated by `IMMICH_METRICS` which defaults off, not phone-home telemetry, so the default of `all` is unchanged); corrected two CHANGELOG entries above (skbackup is v1 optional, not core; skfence has no `RATE_LIMIT_ENABLED` toggle at all, not one that defaults off).
- docs: v1 service catalog (`docs/v1-service-catalog.md`) covering all 30 published v1 services (tier, upstream version, dependencies, exposed ports/hostnames, storage, required vault vars, opt-in flags, deploy command, health check, gotchas); v1/README.md now links it instead of carrying its own stale, partial service list.
- v1 core: **skfenceha** (HA Traefik edge: docker-socket-proxy per manager, worker-tier Traefik, dynamic middlewares/TLS, error pages) published as the HA alternative to skfence. `skfenceha.ACME_ENABLED` defaults to `false` like skfence (self-signed cert); set `true` for real Let's Encrypt certs via Cloudflare DNS-01. `skfenceha.RATE_LIMIT_ENABLED` defaults to `true` (skfence has no such toggle; its rate limiting is unconditional); CrowdSec bouncer support is opt-in (`skfenceha.CROWDSEC_ENABLED`, default `false`, expects an external bouncer/LAPI reachable as `crowdsec-bouncer@file`). Each tier's `cloud-edge-<env>`/`cloud-socket-proxy-<env>` overlay subnets are pinned to a distinct `172.16.x.0/24` (`cloud-public-<env>` is the existing shared network); an instance whose cluster already uses one of those ranges overrides them via `skfenceha_<env>_networks`, the same override pattern skfence itself uses. Also carries forward the v2.18.0 fix for skfence routers rendering `tls: {}` when ACME is off.
- v1: **skdesk** (RustDesk relay: hbbs/hbbr) and **skseek** (Perplexica) published as optional services. skdesk runs with host networking (RustDesk's relay protocol needs the real host ports, and `docker stack deploy` cannot apply `network_mode` the way `docker run` does), so it is pinned to a single node via placement constraint and restarts unconditionally. skseek depends on **skpeek** (SearXNG) already being deployed in the same environment; its overlay subnet must match skpeek's own `deploy_skpeek-<env>.yml`, not a fresh allocation.
- v1: **skmail** (docker-mailserver + Rspamd + ClamAV + Fail2ban) published as an optional service. `skmail.DMS_VERSION` defaults to `16.0.1` (an instance vault var, not a literal in the compose template), so mail server upgrades are opt-in per instance.
- v1: **skbook** (BookStack + MariaDB) published as an optional, single-manager-deploy service. SSO/OIDC is off by default (`skbook.ENABLE_SSO`, then `skbook.OIDC_CLIENT_ID`/`OIDC_CLIENT_SECRET`/`OIDC_ISSUER`, no framework defaults); MariaDB creds are `skbook.DB_USERNAME`/`skbook.DB_PASSWORD` (BookStack's actual `.env` keys, not the `DB_USER`/`DB_PASS` pair an earlier pass assumed).
- v1: **skboard** (Vikunja, sqlite, single-manager deploy) published as an optional service. Its data dir is chowned (recursively) to uid 1000, the uid the Vikunja image runs as, and the compose file carries no healthcheck (the image ships no shell, wget or curl, so any exec healthcheck crash-loops it).
- v1: **sksso** gains `sksso.csrf_extra_origins` (list, default `[]`) for extra Authentik CSRF trusted origins beyond the instance's own domain, needed when Authentik is reached through more than one hostname.
- v1: **skdesk** compose template's image line now reads from the namespaced `skdesk.IMAGE_TAG` instead of a bare unnamespaced var, so a per-instance override is honoured instead of silently ignored on deploy.
- v1: all remaining floating `:latest` image refs (~22, across skdash, skfence, skfenceha, skform, skgraph, skhub, skmail, skmon, skorch, skpdf, skpeek, skpulse, sksync and skvector) are now pinned to prod-matched versions, or prod's exact digest where no matching tag exists, with one exception: skmail's mail-web cert-trigger image is pinned to `traefik/whoami:v1.12.0` (current stable), NOT prod-matched, since skstack01 prod runs `v1.11.0` (set `skmail.MAIL_WEB_IMAGE` to match); `v1/tests/test_no_latest_images.py` asserts every remote image carries an explicit tag or digest and rejects moving tags (`latest`, `lts`, `stable`, `release`, `edge`, `main`).
- v1: **skport** (Portainer CE, pinned to `2.45.1`) and **skwhoami** (traefik/whoami, pinned to `v1.12.0`) published as new optional services. skport reaches the Swarm API via the existing socket-proxy service (skfence or skfenceha), never a direct docker.sock mount, and bakes no admin password (first-run setup is interactive). Both need only `CLUSTERNAME`/`DOMAIN` in their vault.
- v1 core: **sksec** (CrowdSec agent + traefik bouncer, pinned to `v1.6.11`/`0.5.0`) and v1 optional **skmesh** (Netbird management/signal/relay/dashboard/coturn + Postgres, ported from the NAM/skstack01 estate) published. `skmesh.DASHBOARD_IMAGE` is an instance override for the dashboard image, defaulting to netbird's dashboard pinned by digest.
- v1 optional: **skbackup** (Duplicati-based framework backup service, image pinned by digest) published. Jobs are rendered from vault (`job.json.j2`) but Duplicati has no first-class job-import API, so each rendered job is imported once via the web UI; `skbackup.settings_encryption_key` and `skbackup.ui_password` have no literal defaults (the upstream image's own `changeme` fallback is never used).
- v1: **skorch**'s redis started as `redis-server --requirepass <password>`, putting the password in `docker service inspect` ContainerSpec.Args and /proc/<pid>/cmdline for anyone with node or API read access (confirmed exposed this way on skstack01 prod). The password now comes from a mounted `redis.conf` (new `redis.conf.j2`, rendered by each deploy playbook in its own task, owned `999:999` mode `0640`: the redis image drops to uid 999 before starting redis-server, so a `root:root` file is unreadable and the service sits at 0/1) instead of an argv flag; redis starts as `redis-server /usr/local/etc/redis/redis.conf`. n8n's own Redis connection is unaffected (it already read `REDIS_PASSWORD` from `skorch.env`, never argv).
- v2: **skcache** (Valkey) had the same pattern: `valkey-server --requirepass <password>` on `command:`, and `valkey-cli -a <password>` again on its healthcheck. The password now flows in only as `VALKEY_PASSWORD`/`REDISCLI_AUTH` environment variables; a small entrypoint script writes it into a config file (`/usr/local/etc/valkey/valkey.conf`) before exec'ing `valkey-server` against it, and the healthcheck drops `-a` entirely since valkey-cli 8.x reads `REDISCLI_AUTH` itself.
- test: `v1/tests/test_no_secrets_in_compose_argv.py` and `v2/platform/swarm/tests/test_no_secrets_in_stack_argv.py` render every compose/stack template with a sentinel standing in for every password/secret/token-looking var and fail if it ever reaches a service's `command`, `entrypoint`, or `healthcheck.test`, guarding against this class of leak on any current or future service.
- v1: every `deploy.j2` health check (skfence, skfenceha, sksec, skbackup, skboard, skbook, skdash, skform, skgallery, skgit, skgraph, skhub, skmail, skmesh, skmon, skorch, skpdf, skpeek, skport, skpulse, skseek, sksso, sksync, skvector, skwhoami) now polls `docker service ls` for replica convergence (up to 36 polls at 5s, ~3 min) instead of checking once ~10s after `docker stack deploy`, the same poll skstor got in v2.18.0. A redeploy that restarts a task no longer fails or warns spuriously while it shows 0/1. Each script's behaviour on final failure is unchanged (skfence/skfenceha still warn rather than fail). No instance action.
- v1: **skorch** gains `skorch.N8N_VERSION` (a full image ref, default the n8n digest already shipped), `skorch.extra_env` (dict appended verbatim to `skorch.env`, e.g. `N8N_MCP_SERVER_*`) and `skorch.placement_constraints` (list, applied to every skorch service; the default `[]` renders `constraints: []`, i.e. no `node.role` or other constraint, which is what skorch has always had). Its Traefik Host rule now prefers `skorch.CLUSTERNAME`/`skorch.DOMAIN` over the inventory `cluster_name`/`domain` (v2.18.0 had it the other way round, unlike `skorch.env` and every other service). Instance action: if an instance sets both and they differ, the skorch hostname changes to the `skorch.*` values on next deploy; otherwise none, and set the new vars only to change the n8n image, add env or pin placement.
- v1: **sksso** gains `sksso.IMAGE` (overrides the server and worker image, default `ghcr.io/goauthentik/server:<authentik_version>`) and `sksso.extra_env` (dict appended verbatim to `sksso.env`), for instances running a custom Authentik build or env the framework has no key for. Defaults unchanged.
- v1: **skpeek**'s `settings.yml` `debug`, `instance_name` and `ui.default_theme` now come from `skpeek.DEBUG` / `skpeek.INSTANCE_NAME` / `skpeek.THEME` (defaults unchanged), and `skpeek.extra_env` (dict) is appended to `skpeek.env` for anything the SearXNG entrypoint reads directly.
- v1: **skseek** gains `skseek.CONFIG_TOML_PATH` (default the previous hardcoded `/var/data/config/skseek-<env>/config.toml`), used for both the rendered file and the compose mount so they cannot diverge.
- v1: **skvector**, **skpdf**, **skgit** (forgejo, postgres, db-backup) and **sksync** gain `<svc>.placement_constraints` (list, default `[]` = no constraint; sksync's was hardcoded `[]`). **sksync** also gains `sksync.SYNC_DATA_MOUNT` (container target of `sync-data`, default `/var/syncthing`, unchanged) and `sksync.SYNC_DATA_EXTRA_MOUNT` (unset by default; when set, a third host dir `syncthing_data` is created and mounted there). Defaults reproduce current behaviour.
- v1: **skvector** and **skgraph** dev/staging playbooks now read the network override from `<svc>.networks` with `driver`/`attachable`, exactly like their prod playbooks, instead of `<svc>_dev_networks` / `<svc>_staging_networks`. **Behaviour change** for dev/staging only: an instance that set `skvector_dev_networks`, `skvector_staging_networks`, `skgraph_dev_networks` or `skgraph_staging_networks` must move that value to `skvector.networks` / `skgraph.networks` in the matching env vault, or it is now ignored.
- v1: **skform** fails closed with a named message when `CLUSTERNAME`, `DOMAIN`, `APP_ENV`, `TOFU_LOG_LEVEL`, `STATE_BACKEND_TYPE` or `STATE_BACKEND_PATH` is unset (previously a generic Jinja undefined error mid-render), and `skform.env`'s `TOFU_VERSION` defaults to `1.10.7` like the compose file. Instance action: none if the vault already deploys.
- v1: **skpeek**, **skport**, **skwhoami** and **skpulse** default `target_manager_group` to `swarm_managers` like every other select-one-manager service, instead of failing on an unset var. **skdash** and **skgallery** now fall back to the inventory `cluster_name`/`domain` for `CLUSTERNAME`/`DOMAIN` everywhere, like the other services.
- v1: **skmail** gains `skmail.SSL_TYPE` (`letsencrypt` default, byte-identical render; `manual` mounts `skmail.SSL_CERT_PATH`/`SSL_KEY_PATH` read-only; `self-signed` has the deploy playbook generate a CA and leaf cert for `HOSTNAME`). The Traefik `acme.json` preflight now runs only for `letsencrypt`. `skmail.HOSTNAME` defaults to `mail.<DOMAIN>` everywhere, including the DKIM steps in the deploy playbooks and script. No instance action; set `SSL_TYPE` only for an instance without ACME.
- v1: **sksso**'s deploy playbooks own `/var/data/sksso-<env>/media` as `sksso.authentik_uid` (default `1000`, the uid the Authentik image runs as), mode `0755`, so Authentik can create `media/public` on first migration. This shipped in v2.18.0 without a CHANGELOG entry. Instance action: none on a fresh deploy; an instance whose Authentik image runs as a different uid sets `sksso.authentik_uid`.
- v1: **skhub**'s whiteboard image is pinned to `v1.5.4` (what prod runs) instead of the moving `stable` tag (now `v2.0.0`). Instance action: an instance that wants whiteboard 2.x must set it explicitly.
- v1: **skgit**'s admin-bootstrap task was named "(DEV/STAGING ONLY)" in all three playbooks, but it is gated only by `skgit.admin_enabled` + `admin_password` + `admin_email`, in prod too. Renamed to say so; behaviour unchanged. Instance action: make sure a prod vault does not set `skgit.admin_enabled` unless a prod admin account is wanted.
- ci: **skred** baseline entries of the form `*:file:rule:line` now match a finding at any commit, not only the commit they were recorded at; the gpg-signer test fixture token is baselined (a fake value, not a credential).
- docs: `docs/testing.md` (framework test gates and how an instance validates a release before adopting it), `docs/dependency-updates.md` plus `renovate.json` (Renovate regex managers for the pinned v1/v2 images; not enabled by this repo), the vault var naming convention and its legacy exceptions in `docs/v1-service-catalog.md`, and the top-level README no longer calls v1 private and frozen.

Instance action: skorch and skcache were both exposing their Redis/Valkey password in process argv and `docker service inspect` output, readable by anyone with node or Docker API access; **rotate `skorch.REDIS_PASSWORD` and skcache's `VALKEY_PASSWORD` on next redeploy**, the old values must be treated as already disclosed. skfenceha is a drop-in HA alternative to skfence (do not run both on the same cluster); set `ACME_ENABLED: true` for real certs and `CROWDSEC_ENABLED: true` only if the estate runs a CrowdSec bouncer, and review each tier's `cloud-edge-<env>`/`cloud-socket-proxy-<env>` subnet against any existing cluster's own `172.16.x.0/24` allocations before first deploy. skfenceha's access-log header redaction default changed (see above); no action needed unless a stricter or different header set is wanted, in which case set `skfenceha.ACCESS_LOG_HEADERS` explicitly. skdesk, skseek, skmail, skbook and skboard are all new optional services, each needing its own vault (`v1/group_vars/<env>/<svc>-<env>-<domain-dashed>-<cluster>_vault.yml`) per its README; skseek additionally requires skpeek already deployed in the same environment. skmail defaults to DMS 16.0.1; set `skmail.DMS_VERSION` to pin elsewhere. skbook needs `skbook.DB_USERNAME`/`skbook.DB_PASSWORD` and, only if using SSO, the `OIDC_*` vars from its README. sksso's `csrf_extra_origins` defaults to empty (current behavior unchanged); set it only when Authentik is reached through more than one origin. skdesk IMAGE_TAG override is now honoured; instances that already set `skdesk.IMAGE_TAG` will pick it up on next deploy instead of the framework default. All images are now pinned to an explicit tag or digest and a test rejects any floating/moving tag going forward; no instance action needed unless an instance vault set its own `:latest`/`lts`/`stable`/`release`/`edge`/`main` override, in which case pin it to a real version. skport and skwhoami are new optional services, each needing its own vault (`v1/group_vars/<env>/<svc>-<env>-<domain-dashed>-<cluster>_vault.yml`) with `CLUSTERNAME`/`DOMAIN`; skport additionally requires skfence or skfenceha already deployed for its socket-proxy. sksec and skmesh are new services, each needing their own vault per README; skmesh's Postgres creds and Netbird setup key have no literal defaults. Set `skmesh.DASHBOARD_IMAGE` only to override the pinned dashboard digest. skbackup is a new service needing `skbackup.settings_encryption_key` (min 8 alphanumeric chars) and `skbackup.ui_password` in its vault (both fail closed, no default); after first deploy, import each rendered job into Duplicati via its web UI (it is not auto-imported). Instances overriding skvector or skgraph networks in dev/staging must move `<svc>_dev_networks`/`<svc>_staging_networks` to `<svc>.networks` (see above).

## skstacks-v2.18.0 - 2026-09-26

- v1: **skstor** meta/data now bind-mount onto `/var/data/skstor-<env>/{meta,data}`, the shared filesystem every other v1 service already requires, instead of a named Docker volume that stranded data on whichever manager the replicas=1 Garage service last ran on. `garage.toml.j2` gains `skstor.db_engine` (default `sqlite`, was a hardcoded `lmdb`; Garage's own docs call LMDB "prone to database corruption after an unclean shutdown", which a Swarm reschedule is), and `skstor.meta_dir`/`skstor.data_dir` (default the bind-mount paths above; override to a node-local path plus a placement constraint to trade shared storage for LMDB's speed).
- v1: **skhub** gains `skhub.storage_backend: skstor` as a one-line shortcut for S3 against this cluster's own in-cluster skstor Garage service (host/port/region default to the in-cluster service, only `skhub.s3_bucket_name`/`s3_access_key`/`s3_secret_key` are required), and joins the `skstor-<env>` overlay network only when that backend is selected. Default remains `local` (NFS-backed `/var/data/skhub-<env>/data`, unchanged); the existing free-form `skhub.storage_backend: s3` mode is unchanged.
- v1: **skorch** (n8n + pgvector Postgres + Redis + postgres-backup) published. Its optional gpg-signer sidecar (`skorch.enable_gpg_signer`, default `false`) is a REST API for detached PGP signing with pre-provisioned keys; image defaults to `ghcr.io/smilintux/skstacks-gpg-signer`, built by CI on `skstacks-v*` tags and `workflow_dispatch` (`.github/workflows/gpg-signer-image.yml`). No literal default for `skorch.GPG_SIGNER_API_SECRET` (required only when the sidecar is enabled); its `/import-key` endpoint is gated behind `GPG_SIGNER_ALLOW_KEY_IMPORT` (default off).
- v1: **skgit**'s Forgejo `/metrics` now fails closed. `FORGEJO__metrics__ENABLED` (skgit.env.j2) and `[metrics] ENABLED` (app.ini.j2, the file Forgejo actually reads on this framework since its entrypoint bypasses the image's environment-to-ini step) both default to `false` and only turn on once `skgit.METRICS_TOKEN` is set, or an instance explicitly sets `skgit.METRICS_ENABLED: true` (each deploy playbook now asserts a token exists before allowing that). Previously `ENABLED` defaulted to `true` unconditionally, so an instance that never set a token served /metrics with no auth at all; verified live on a real instance (200 without auth).
- v1: **skorch**'s `set_fact: skorch: "{{ skorch_defaults | combine(skorch) }}"` named the merged fact after the very variable it read. Ansible's extra-vars (`-e @vars.yml`) outrank a play's own `set_fact`, so a deploy run with `-e` silently discarded the merge and every skorch template saw the bare extra-vars dict, undefined `POSTGRES_DB`/`POSTGRES_USER` included. The merged fact is renamed to `skorch_cfg`, and skorch.env.j2 / postgres.env.j2 / postgres-backup.env.j2 / skorch.sh.env.j2 / skorch.yml.j2 now read `skorch_cfg.*` exclusively, so extra-vars can no longer shadow the merge no matter which key is missing. Also: `GENERIC_TIMEZONE` default changed from the oddly specific `Europe/Rome` to `UTC`.
- v1: **skgallery** and **skhub** deploy playbooks install the Docker SDK with apt (`python3-docker`) instead of system `pip`, which PEP 668 refuses on Ubuntu 24.04 (the deploy failed before touching the stack). skstor does the same from the start.

Instance action: skstor is new (Garage, data on `/var/data/skstor-<env>`, sqlite metadata, one-time layout bootstrap in deploy); skhub's default storage is unchanged (local), `storage_backend: skstor` is opt-in and first-install only; skorch's gpg-signer is opt-in (`skorch.enable_gpg_signer`, needs trustee keys in `GNUPGHOME` and `GPG_SIGNER_API_SECRET`; the old literal default secret is gone, it fails closed). If you scrape Forgejo metrics, set `skgit.METRICS_TOKEN` in the vault (a redeploy is required so the ansible-rendered app.ini picks it up) and make Prometheus send `Authorization: Bearer <token>`; skmon's own `EXTRA_SCRAPE_CONFIGS` vault entry for the `forgejo` job needs its `bearer_token` updated to match whenever the skgit token changes, or that scrape starts failing with 401. skorch: no vault changes required; if an instance's vault or extra-vars relied on the old (buggy) shadowing behavior to inject `POSTGRES_DB`/`POSTGRES_USER` some other way, remove that workaround, the combine now always applies. If an instance intentionally set `skorch.GENERIC_TIMEZONE` to keep the old `Europe/Rome` default, set it explicitly in the vault; unset instances now get `UTC`.

## skstacks-v2.17.0 - 2026-09-26

- v1 core: **skfence** (Traefik + docker-socket-proxy + certs-dumper + error pages), the first core-tier service. `skfence.ACME_ENABLED` defaults to `false` (Traefik self-signed cert, works with no public DNS); `true` enables Let's Encrypt via Cloudflare DNS-01 with no literal secret defaults.
- v1 core: **skha** (keepalived VRRP VIP failover). A documented exception to the single-selected-manager rule: it runs per host, marked `# skstacks: per-host` and scoped to core by a test.
- v1: **skdash** (Dashy) with generic Traefik discovery driven by `skdash.discovery.*` instance vars, replacing the estate-specific discovery script.
- v1: **skhub** (Nextcloud + MariaDB + Redis + ClamAV + notify_push + whiteboard + imaginary + db backup). Collabora and Talk HPB are feature flags, default off (they need public DNS + TLS). `skhub.CLUSTERNAME` / `skhub.DOMAIN` are required.
- v1: **skgallery** (Immich + pgvector Postgres + Redis) and **skgit** (Forgejo + Postgres, opt-in Actions runners via Docker-in-Docker).
- v1: **sksso** (Authentik server/worker/postgres/redis).
- v1 tests: no two services share a subnet (`test_unique_subnets.py`, cloud-public-* exempt). sksso moves to 172.16.104/105/106 (prod/staging/dev) and sksync-staging to 172.16.107.
- CI: scheduled image-availability canary (`v1/scripts/list_images.py`, `v1/scripts/check_images.sh`). A Docker Hub `toomanyrequests` is reported as RATE-LIMITED (inconclusive), not a failure.
- v1: **skhub** router now supports `skhub.router_middlewares` (list, default `[]`, appended after the framework's own `skhub,skhub-dav` middlewares on the `skhub-secure` router) and `skhub.tls_options` (string, default unset -> no label).
- v1: **skdash** router gets the same `skdash.router_middlewares` (default `[]`) and `skdash.tls_options` (default unset) hooks on its secure router. Its compose template also gains `restart_policy` (`on-failure`, 10s delay, 5 attempts) and `rollback_config` (parallelism 1, 10s delay), resource limits/reservations moved to `skdash.RESOURCES_LIMITS_CPUS`/`RESOURCES_LIMITS_MEMORY`/`RESOURCES_RESERVATIONS_CPUS`/`RESOURCES_RESERVATIONS_MEMORY` (defaults unchanged: 0.5/512M limits, 0.1/128M reservations), and a sticky-cookie `sameSite` hook via `skdash.sticky_samesite` (default unset -> no label, current framework behavior).
- v1: **skhub** and **skdash** both gain `router_middlewares_pre` (list, default `[]`), prepended before the built-in middlewares on the secure router; `router_middlewares` keeps appending after them. Needed for middleware chains where order matters (e.g. a request-buffering middleware that must run before the app's own headers/rewrite middlewares).
- v1: **skdash** `restart_policy` and `rollback_config` gain more instance-overridable fields: `skdash.restart_policy_condition` (default `on-failure`), `skdash.restart_policy_max_attempts` and `skdash.restart_policy_window` (defaults `5`/`120s`, set to an empty string to omit the key entirely), and opt-in `skdash.rollback_failure_action` / `skdash.rollback_monitor` / `skdash.rollback_max_failure_ratio` (all unset by default, matching current framework behavior of no extra rollback fields).
- v1: **skgit**'s `start-runners.sh` fails closed (skips runner startup, by design) when `SKGIT_DIND_NODE` is unset -- but the dev/staging/prod deploy playbooks called it from a `shell` task without ever setting that variable, so runners silently never started on ANY node regardless of the instance's vault. Each playbook's post-deploy task now passes `SKGIT_DIND_NODE: "{{ skgit.DIND_NODE | default('') }}"` through the task's `environment`.

Instance action: none required for skhub/skdash; all new hooks default to current framework behavior. Set the new vars only where an instance needs a middleware, TLS options, resource sizing, cookie policy, or restart/rollback semantics the defaults do not already provide. For skgit, set `skgit.DIND_NODE` to the single node that should run DIND + runners (empty/unset keeps the existing fail-safe skip).

Instance action: a new service needs its vault (`<tier>/group_vars/<env>/<svc>-<env>-<domain-dashed>-<cluster>_vault.yml`) with the required vars listed in its README. **sksso** and **sksync-staging** instances already deployed on the old subnets must recreate those networks (or pin the old subnet via the playbook's network list in their own fork) before redeploying.

## skstacks-v2.16.0 - 2026-09-26

- v1: **sksync** (Syncthing), **skpulse** (Uptime Kuma) and **skpeek**
  (SearXNG + valkey) published.
- tofu libvirt-cluster: `registry_mirrors` writes Docker's daemon.json (e.g.
  `mirror.gcr.io`), so test clusters behind one public IP stop hitting Docker
  Hub's anonymous pull limit.
- sksync configures its firewall from the one selected manager (it ran on
  every manager); skpeek's SearXNG `secret_key` is required (`skpeek.SECRET_KEY`,
  it had a literal default).

Instance action: set `sksync.*` (incl. SYNCWALLET, ENCRYPTION_TOKEN, UUID),
`skpulse.*` and `skpeek.SECRET_KEY`; shared `/var/data` as for the other v1
services.

## skstacks-v2.15.0 - 2026-09-26

- v1: **skmon** (Prometheus, Grafana, Loki, Promtail, cAdvisor, node-exporter,
  Alertmanager, Jaeger) published. Scrape extra services with
  `skmon.EXTRA_SCRAPE_TARGETS` (static) or `skmon.EXTRA_SCRAPE_CONFIGS`
  (complete scrape_config entries, e.g. DNS service discovery).
- v1: **skform** (OpenTofu CLI container) published. Its image is
  `ghcr.io/opentofu/opentofu` (the Docker Hub name never existed) and it idles
  with `sleep infinity` as the entrypoint.
- v1: fixes found by the end-to-end test: skmon's deploy script only found its
  files in prod; skform's compose was invalid YAML with `SKSTOR_ENABLED` false.
- v1: new tests: deploy scripts look where playbooks write; compose templates
  render to valid YAML the way Ansible renders them.

Instance action: skmon needs `GRAFANA_ADMIN_PASSWORD`, `GRAFANA_SECRET_KEY`
and the other `skmon.*` keys listed in its playbooks; move bespoke Prometheus
jobs into `EXTRA_SCRAPE_CONFIGS`. skform needs its `skform.*` keys.

## skstacks-v2.14.0 - 2026-09-25

- v1: **skmem-pg** (Postgres 17 + pgvector + pg_search + Apache AGE) published,
  with dev and staging playbooks authored to match prod. Its init SQL is a
  template: set `skmem_pg_default_agent` / `skmem_pg_graph` (defaults
  `default` / `agent_knowledge`).
- v1: **skreg** (Docker Registry v2) published. It now deploys from one
  selected manager like every other service (it ran on every manager at once
  and raced itself on the shared `/var/data`).
- v1: secrets fail closed. No secret-looking variable may fall back to a
  literal default (new test). `skreg`'s `vault_skreg_registry_http_secret`
  and `skpdf`'s `SECURITY_INITIAL_PASSWORD` (when login is enabled) are now
  required.
- v1: tests require every deploy playbook to select one manager.
- skred: the denylist no longer reads a git worktree's `.git` pointer file
  (it broke the pre-push hook in worktrees).

Instance action: set `skmem_pg_password`, `vault_skreg_registry_http_secret`,
and `skpdf.SECURITY_INITIAL_PASSWORD` if skpdf login is enabled; shared
`/var/data` as for the other v1 services.

## skstacks-v2.13.0 - 2026-09-25

- v1: third published v1 service, **skpdf** (Stirling-PDF), with dev and
  staging playbooks brought to prod parity (dropped the self-referencing
  vault-select vars).

Instance action: if you adopt v1 skpdf, set its vault values including
`CLUSTERNAME` and `DOMAIN` (both now required, no estate default), and
share `/var/data` across all nodes the same way skgraph and skvector
require.

## skstacks-v2.12.0 - 2026-09-25

- v1: second published v1 service, **skvector** (Qdrant), with dev and
  staging playbooks brought to prod parity (dropped the removed-`ipam`
  docker_network task, added the control script symlink task).

Instance action: if you adopt v1 skvector, set its vault values including
`CLUSTERNAME` and `DOMAIN` (both now required, no estate default), and
share `/var/data` across all nodes the same way skgraph requires.

## skstacks-v2.11.0 - 2026-09-25

- tofu: `v2/infra/tofu/modules/libvirt-cluster` (local KVM VMs, Ubuntu 24.04
  with the HWE kernel, Docker; inventory in the swarm platform's layout) and
  `examples/libvirt-swarm`.
- v1: first published v1 service, **skgraph** (FalkorDB), with the shared
  tasks it uses. `select_vault_file.yml` honours `skstacks_vault_dir` so an
  instance keeps its own vaults.

- swarm platform: `deploy.yml` found none of its roles (fixed with a
  `playbooks/roles` link) and its plays are now tagged `bootstrap`, `ha`,
  `traefik`; `--tags bootstrap` builds just the swarm.
- k3d platform: `scripts/create.sh` and `clusters/ci.yaml` were rejected by
  k3d v5 (`--name`, `kubeAPI` under `options`); both fixed.
- CI: `framework tests` workflow runs the v1, swarm, k3d and tofu suites.

Instance action: none unless you adopt v1 skgraph; then set
`skstacks_vault_dir`, share `/var/data` across all nodes, and see
`v1/README.md`. If you ran the swarm `deploy.yml` with your own role path
workaround, you can drop it.

## skstacks-v2.10.0 - 2026-09-24

- skred.denylist: a non-UTF-8 denylist exits 2; unreadable files are
  findings; FIFOs and sockets are skipped instead of hanging; UTF-16 text is
  searched.
- skred gate: exits 2 when every target is out of scope (nothing scanned)
  instead of passing.
- skmesh: cloudflared, netbird and newt Deployments run with a read-only root
  filesystem, no privilege escalation, minimal capabilities and the
  RuntimeDefault seccomp profile.
- INSTANCE-MODEL guide: status section brought up to date; clone steps
  re-disable framework push.

Instance action: netbird and newt now mount emptyDirs for their state
paths; if you patched these manifests, re-apply your changes on top.

## skstacks-v2.9.0 - 2026-09-24

First public release of the v2 framework.

- Framework + instance model: see `v2/docs/INSTANCE-MODEL.md`.
- skred scope defaults are loopback-only; declare yours with
  `SKRED_SCOPE_DOMAINS` / `SKRED_SCOPE_CIDRS`.
- `skred.denylist` estate gate in CI.

Instance action: none (first release). Pin `framework/` to this tag.
