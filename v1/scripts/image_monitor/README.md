# Fleet image + runtime security monitor

Daily, unattended check of every running Swarm service image on the nor and NAM
clusters: is a newer digest or tag available upstream, and (for PHP-embedding
images) is the runtime itself below a configured security floor.

Built in response to the PHP2Shell RCE (CVE-2026-109618/109619). Goal: the next
one becomes a same-day patch, not a surprise.

## Pieces

- `inventory.py` - SSH into each cluster's Swarm managers and list every
  running service's image reference (`docker service ls` + `docker service
  inspect` for the pinned digest).
- `registry.py` - For each image, ask upstream: Docker Hub's REST API (tag
  metadata + a tag list sorted by `last_updated`) for Hub images, the generic
  OCI distribution v2 API with an anonymous bearer token for GHCR / lscr.io /
  quay.io. Caches lookups on disk and backs off on HTTP 429.
- `php_floor.py` - Loads `floors.yaml`, compares a probed runtime version
  against the configured minimum-safe floor.
- `php_probe.py` - For images that embed PHP, finds the running container on
  its node (`docker ps`) and runs `php -r 'echo PHP_VERSION;'` inside it.
- `state.py` - Last-seen-digest state file so a rerun only reports a change
  once.
- `notify.py` - Posts the daily digest to Nextcloud Talk by calling the
  existing Windmill script `f/ops/notify` (see
  `v1/scripts/windmill-ops/notify.py` and `ALERT-POLICY.md`, from PR #215).
  Below-floor findings go to room "NAM Urgent"; everything else ("a change
  happened") goes to "NAM Admin". No Talk room, bot or route is invented here;
  this only reuses the one PR #215 already wired up. Also upserts each
  actionable finding into the unified GTD
  (`skos.gtd_ingest.upsert`, source `image-monitor`).
- `check_fleet_images.py` - The CLI entry point that wires the above together.
  Meant to run from a systemd user timer on whichever control node has ssh
  access to both clusters' Swarm managers.

## Config

- `config.example.yaml` - a template: clusters, hosts, ssh jump settings, the
  Talk room names, state/cache file paths. No secrets go here, but real
  hostnames don't either: copy it somewhere private (e.g.
  `~/.skcapstone/image-monitor/config.yaml`), fill in your own hosts, and
  pass that path to `--config`. Cluster topology is operational data, not
  framework code, and this is a public repo.
- `floors.yaml` - the runtime security floor table. Seeded with PHP from the
  PHP2Shell advisory (8.5.12 / 8.4.27 / 8.3.36 / 8.2.35). See "Adding a floor"
  below.
- Talk notify credentials (a Windmill API token scoped to
  `jobs:run:scripts:f/ops/notify` in workspace `fwe`, plus the base URL) are
  read from `~/.skcapstone/secrets/image-monitor-notify.env`
  (`WM_NOTIFY_BASE_URL`, `WM_NOTIFY_WORKSPACE`, `WM_NOTIFY_TOKEN`), 0600, never
  committed. This file does not exist until an operator (Chef) creates the
  token in Windmill and drops it there; until then `notify.py` logs a clear
  warning and skips the Talk post (GTD capture and the local digest file
  still happen).

## Adding a new runtime floor

Edit `floors.yaml`. Each runtime is a regex list of image references it
applies to, the probe command run inside the container, and a
`{major.minor: minimum_patch}` table. Example, adding OpenSSL:

```yaml
openssl:
  images:
    - "nextcloud"
  probe: ["openssl", "version"]
  parse: 'OpenSSL (\d+\.\d+\.\d+)'
  min_versions:
    "3.0": 16
```

No code change needed; `php_floor.py` is runtime-name-agnostic.

## Running by hand

```
~/.skenv/bin/python3 v1/scripts/image_monitor/check_fleet_images.py --config ~/.skcapstone/image-monitor/config.yaml
```

`--dry-run` prints the digest without posting to Talk or writing GTD/state.

## Vulnerability register (check_fleet_vulns.py)

A second, sibling tool that answers a different question than the floor
check above: not just "is PHP below the floor" but "which of the CVEs
Chef is tracking right now apply to which running service, and is it
patched". Built in response to PHP2Shell plus the same week's eMudhra CA
compromise and a batch of MariaDB/Traefik/Redis/Docker/kernel/glibc
advisories.

- `vulns.yml` - the register itself: public, generic vulnerability data
  (same spirit as `floors.yaml`, no cluster topology or real inventory
  data). Each entry is one CVE or a batch of CVEs from the same
  disclosure, with a `check` block describing how to decide
  affected/not-affected automatically, or `manual_findings` recording a
  dated, by-hand verification when no automated check applies. See the
  file's own header comment for the four `check.type` shapes.
  - The PHP2Shell entry does **not** carry its own copy of the fix
    version numbers - it points at `floors.yaml`'s `php` block via
    `check.floors_ref: php`, so the floor table and the register can
    never drift apart. Bump `floors.yaml`, the register follows.
- `versions.py` - minimal dotted-integer version parsing/comparison, used
  only by the `min_fixed_version` check type (a single floor compared
  against the whole version, for software where only the newest line or
  two ever got a fix - e.g. Traefik 3.6.x, which isn't itself patched, vs.
  the 3.7.14 fix).
- `vuln_register.py` - the matcher. Every per-line floor comparison
  (`runtime_floor`, `line_floor`) reuses the existing, tested
  `php_floor.evaluate` - either against `floors.yaml` directly or against
  an ad-hoc one-entry floors dict built from the `vulns.yml` entry itself.
  No new per-line comparison code; `versions.py` only covers the simpler
  total-order case.
- `exposure.py` - classifies a service's Traefik labels into exactly three
  buckets: `internet-behind-sso-gate` (a forward-auth middleware like
  `authentik@file` sits in front - unauthenticated requests never reach
  the app), `internet-unauthenticated` (a public router exists but no
  forward-auth middleware does - true whether the app has its own native
  login or only CrowdSec, since either way an unauthenticated request
  reaches app code before any login check), or `internal-only` (no public
  router at all).
- `render_register.py` - renders the matched findings into a static HTML
  page: totals, a severity x exposure matrix, and the full per-row table
  grouped by vulnerability.
- `check_fleet_vulns.py` - the CLI entry point. Shares `--config` with
  `check_fleet_images.py` (the same `clusters` block); run both from the
  same systemd timer, this one right after the other, since both need the
  same ssh reachability and inventory shape - this tool does not create
  a second timer.

### Config additions

Three more keys on the same private `config.yaml` (see
`config.example.yaml`):

```yaml
vulns_file: "vulns.yml"
register_html: "~/.skcapstone/image-monitor/vuln-register.html"
register_json: "~/.skcapstone/image-monitor/vuln-register.json"
```

`register_html`/`register_json` are written outside the repo like
`state_file`/`cache_file` - the rendered register necessarily contains
real cluster/service/version data. GTD capture reuses `notify.py`'s
`gtd_upsert_finding`, now generalised with its own `source`/`source_ref`:
`check_fleet_vulns.py` passes `source="vuln-register"`,
`source_ref="<vuln-id>/<cluster>/<service>"` (distinct from
`check_fleet_images.py`'s own `"image-monitor"` / `"<cluster>/<service>/
<image>"` convention, so the two tools' findings never collide on the
same GTD key).

### Running by hand

```
~/.skenv/bin/python3 v1/scripts/image_monitor/check_fleet_vulns.py --config ~/.skcapstone/image-monitor/config.yaml
```

`--dry-run` prints the counts without writing the HTML/JSON outputs or
GTD items.

### Known gaps (tracked as `manual_findings`, not automated yet)

- Host-level entries (Linux kernel, glibc, Docker Engine) record a
  point-in-time hand verification; there's no automated host-kernel/libc
  probe module yet, and Ubuntu's own kernel/glibc build numbering doesn't
  map directly to upstream CVE-fixed versions anyway (needs a USN lookup,
  not a version-string compare).
- The eMudhra CA-compromise entry is informational by design: it's a
  trust-store question (which Go stdlib / OS CA bundle version is baked
  into Traefik/CrowdSec/NetBird's build), not a bug in any app we run.
- Nextcloud's `richdocumentscode` check is a hand-verified `occ app:list`
  read today, not a wired-in probe; a future pass could add an
  `occ_app_version` check type alongside the four above.
