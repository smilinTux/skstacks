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
  Run by the systemd user timer on noroc2027.

## Config

- `config.yaml` - clusters, hosts, ssh jump settings, the Talk room names,
  state/cache file paths. No secrets live here.
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
~/.skenv/bin/python3 v1/scripts/image_monitor/check_fleet_images.py --config v1/scripts/image_monitor/config.yaml
```

`--dry-run` prints the digest without posting to Talk or writing GTD/state.
