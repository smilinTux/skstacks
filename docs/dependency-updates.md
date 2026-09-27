# Dependency updates (image pins)

`v1/tests/test_no_latest_images.py` requires every remote container image a
published v1 service pulls to be pinned: an explicit non-floating tag, or a
`@sha256:...` digest. A pin only stays true the day it is written; upstream
keeps shipping new versions underneath it. `renovate.json` at the repo root
is the automation that keeps those pins current without anyone hand-editing
version strings, and without ever silently re-introducing a floating tag.

## What Renovate watches

Renovate does not understand this repo's file shapes out of the box: v1's
compose files are Jinja templates (`*.yml.j2`, some with the image ref split
across a literal prefix and a `{{ var | default('...') }}`), and several v2
manifests (`app.yaml`) are not compose files at all. `renovate.json` defines
two `customManagers` (regex-based) instead of relying on Renovate's built-in
`docker-compose` manager (which is explicitly disabled, so nothing runs it
twice):

- **v1** (`v1/ansible/**/*.j2`): matches a literal `image: repo:tag`, a bare
  `image: repo@sha256:...` digest reference, and both Jinja shapes - a
  literal repo with the tag (and optional digest) inside
  `default('...')`, and the whole reference (repo, optional tag, optional
  digest) inside `default('...')` with nothing on the `image:` line itself.
  The framework's own locally-built `-error-pages` sidecars (never pulled
  from a registry) fall out of the pattern automatically, the same way
  `test_no_latest_images.py` exempts them.
- **v2** (`v2/**/*.yaml`, `v2/**/*.yml`, and the two `*.yml.j2` compose
  templates under `v2/cloud/skfence` and `v2/apps/skfence`): matches the
  same literal `image:`/digest-only shapes. v2 has no Jinja `default()`
  image pins today.

Verified against `v1/scripts/list_images.py` (the same script the pin test
uses) on `integration/v2.19.0` at commit `cfc1c54`:

| Scope | Inventoried | Detected by Renovate | Gap |
|---|---|---|---|
| v1 (test-enforced) | 67 remote images (+2 locally-built, correctly exempt) | 67 | none |
| v2 (opportunistic, not test-enforced) | ~48 literal image refs across `app.yaml`/compose/swarm-compose files | 46 raw occurrences (all unique images) | 2 known gaps, below |

Known v2 gaps (neither blocks the v1 pin test; both are pre-existing in the
framework, not introduced by this change):
- `v2/apps/skfence/docker-compose.yml.j2` uses a bash-style
  `image: registry.skfence.io/skfence:${VERSION:-latest}` default, which
  isn't a container image Renovate can resolve a next version for (there is
  no registry to check against - it is a local app image built out of band).
  Converting it to a real pinned tag is a separate, unrelated fix.
- Three Helm values files (`v2/secrets/openbao/k8s-helmchart.yaml`,
  `v2/core/sksso/k8s-helmchart.yaml`, `v2/compute/skmon/k8s-helmchart.yaml`)
  split the image across `repository:`/`tag:` keys instead of one `image:`
  line. A follow-up `customManager` can cover that shape if it becomes worth
  automating; it was left out here to avoid a regex that spans two lines and
  could misattribute an unrelated `tag:` key.

Two things Renovate covers **without any custom config**, because the repo
already uses standard file shapes for them:
- **GitHub Actions** (`.github/workflows/*.yml`): the `uses: action@vN`
  pins are picked up by Renovate's built-in `github-actions` manager.
- **Dockerfiles** (`coturn/Dockerfile`, the two `error-pages` images, the
  `skmem-pg` image, the `gpg-signer` image): picked up by Renovate's
  built-in `dockerfile` manager. This is a bonus catch: `coturn/Dockerfile`
  still says `FROM coturn/coturn:latest` (that image isn't a "published v1
  service" per the pin test, so it was out of that test's scope, but it is
  exactly the same failure mode).

OpenTofu (`v2/infra/tofu/**`) has no container image references to pin - the
`image =` fields there are cloud-provider VM/AMI names (Hetzner, Proxmox),
and `registry_mirrors` is a list of mirror URLs, not a versioned image. v3
is a single `CLAUDE.md` placeholder with nothing to pin.

## Grouping and review

- **Minor/patch/digest** bumps for images in the same service directory
  (`{{parentDir}}` - e.g. everything under `skmon/` or `sksso/`) land as one
  grouped PR.
- **Major** bumps are never grouped - each gets its own PR, so a major never
  hides inside an unrelated minor bump.
- **`dependencyDashboardApproval`** holds major bumps for
  docker-mailserver, postgres, Authentik (`ghcr.io/goauthentik/server`),
  Forgejo, and Nextcloud behind a manual click on the Dependency Dashboard
  issue - these are exactly the images where an unattended major swap risks
  a data migration, a schema break, or (as already happened once with
  docker-mailserver on `:latest`, see `test_skmail_dms_version_pinned.py`)
  running three majors behind without anyone noticing.
- Everything else (minor/patch, or majors not on that list) opens as a
  normal PR for review.

## How an update actually ships

1. Renovate opens a PR against `main` (or updates the Dependency Dashboard
   issue for a held-back major, waiting for a checkbox).
2. CI runs on the PR: `framework-tests.yml` (renders every template through
   real Jinja and asserts, including the pin test itself, so a bad bump that
   somehow reintroduced a floating tag would fail loudly) and
   `image-canary.yml` (confirms the new image reference is still anonymously
   pullable - catches a typo'd tag or a registry that stopped serving it).
3. Before merging anything that isn't a trivial patch, run the instance
   pilot (`skstack06`, the second-instance test harness) against the
   affected service(s) to prove the new image actually starts and passes
   its own health checks, not just that the template renders.
4. Merge to `main`, then cut the framework's next tagged release the normal
   way.

## Turning it on

This PR only adds `renovate.json` - nothing runs against it yet. To
activate:

- **Hosted (simplest):** install the Renovate GitHub App
  (github.com/apps/renovate) on `smilinTux/skstacks` and select the repo.
  It will open the Dependency Dashboard issue and start proposing PRs on
  the configured weekly schedule. This is the only step that touches repo
  settings/installed apps, which is why this PR does not do it.
- **Self-hosted:** run `renovate` (the same CLI used to validate this
  config) against the repo with a token that has PR-creation rights, on a
  cron matching the `schedule` in `renovate.json`. Useful if the org wants
  updates to land through the same automation as everything else instead of
  a third-party GitHub App.

Either way, nothing merges by itself: every PR (and every held-back major on
the dashboard) still goes through the same review and CI gates as a
hand-written change.
