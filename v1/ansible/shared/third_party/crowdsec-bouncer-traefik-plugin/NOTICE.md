# Third-party: maxlerebourg/crowdsec-bouncer-traefik-plugin

The sksec CrowdSec bouncer runs INSIDE Traefik as a local plugin
(`experimental.localPlugins`), replacing the old forwardAuth container
`fbonalair/traefik-crowdsec-bouncer` (no upstream release since 2022). This
directory holds the plugin source that skfence and skfenceha mount into
Traefik, so Traefik never downloads plugin code at start (offline-safe).

- Project: [maxlerebourg/crowdsec-bouncer-traefik-plugin](https://github.com/maxlerebourg/crowdsec-bouncer-traefik-plugin)
  (listed on the CrowdSec hub as the Traefik bouncer plugin)
- Version: `v1.8.2` (released 2026-11-14), tag commit
  `b0431e2e530cadd629583288909b462c8a6e9669`
- Release tarball `archive/refs/tags/v1.8.2.tar.gz`, sha256
  `8553b722bcd15f0c08bbf5aa1485660686b32033469b1f7c262b02358f3538cf`
- Upstream tests and examples use Traefik v3.7.17; the framework pins
  v3.7.19 (same minor, local plugin API unchanged).

## What is vendored

`src/` is the runtime subset of the module, files unchanged except one line: `LICENSE`,
`.traefik.yml` (the manifest Traefik reads; the example `CrowdsecLapiKey` in its
`testData` is blanked to `""` by the vendor script, because secret scanners flag it;
Traefik ignores `testData` for local plugins), `go.mod`, `go.sum`, the non-test `*.go` files
and `vendor/` (Traefik's yaegi interpreter resolves imports from it, offline).
Tests, examples, docs and CI files are left out. `SHA256SUMS` lists every
file in `src/`; the deploy verifies it on the controller and again on the
node (`shared/tasks/traefik_crowdsec_plugin.yml`), and
`v1/tests/test_sksec_traefik_plugin.py` verifies it in CI.

To bump: `tools/vendor_crowdsec_traefik_plugin.sh <tag> <tarball-sha256>`,
then update `traefik_crowdsec_plugin_version` in the shared task, this file
and the CHANGELOG.

## Why vendored, not fetched at deploy time

A deploy-time fetch needs internet on the controller or the nodes at deploy
time and makes a GitHub outage a deploy blocker. Vendored source is reviewed
in the PR that bumps it, pinned by checksum, and deploys the same bytes on
every estate. It is about 260 KB.

## Licenses

- The plugin: Apache License 2.0 (`src/LICENSE`).
- `src/vendor/github.com/maxlerebourg/simpleredis`: Apache License 2.0
  (its own `LICENSE`).
- `src/vendor/github.com/leprosus/golang-ttl-map`: GNU GPL v3 (its own
  `License.md`). Shipped as unmodified source with its license, which GPLv3
  section 13 allows alongside this AGPL-3.0 framework.

No file was modified. This NOTICE is the attribution Apache-2.0 section 4(d)
asks a redistributor to carry.
