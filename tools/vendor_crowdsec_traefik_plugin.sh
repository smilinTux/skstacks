#!/usr/bin/env bash
# Vendor (or bump) the CrowdSec bouncer Traefik plugin that skfence/skfenceha
# load as a Traefik LOCAL plugin (experimental.localPlugins), so Traefik never
# downloads plugin code from the internet at start.
#
# Usage: tools/vendor_crowdsec_traefik_plugin.sh <tag> <tarball-sha256>
#   e.g. tools/vendor_crowdsec_traefik_plugin.sh v1.8.2 8553b722...38cf
#
# What it does:
#   1. downloads the GitHub release tarball of <tag> and refuses to go on
#      unless its sha256 equals <tarball-sha256> (take it from a second,
#      independent download or from the release review, never from this run);
#   2. replaces v1/ansible/shared/third_party/crowdsec-bouncer-traefik-plugin/src
#      with the RUNTIME subset of the module: LICENSE, .traefik.yml (the
#      manifest Traefik reads), go.mod/go.sum, the non-test *.go files and
#      vendor/ (yaegi resolves imports from it, offline). Tests, examples,
#      docs and CI files are left out;
#   3. writes SHA256SUMS over that tree. The deploy (shared task
#      traefik_crowdsec_plugin.yml) checks it on the controller and again on
#      the node, and v1/tests/test_sksec_traefik_plugin.py checks it in CI;
#   4. prints the values to update in the shared task and NOTICE.md
#      (version, tag commit, tarball sha256).
set -euo pipefail

TAG="${1:?usage: $0 <tag> <tarball-sha256>}"
WANT_SHA="${2:?usage: $0 <tag> <tarball-sha256>}"
REPO="maxlerebourg/crowdsec-bouncer-traefik-plugin"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/v1/ansible/shared/third_party/crowdsec-bouncer-traefik-plugin"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl -fsSL --retry 3 -o "$work/plugin.tar.gz" \
  "https://github.com/${REPO}/archive/refs/tags/${TAG}.tar.gz"
got="$(sha256sum "$work/plugin.tar.gz" | cut -d' ' -f1)"
if [ "$got" != "$WANT_SHA" ]; then
  echo "FATAL: tarball sha256 $got != expected $WANT_SHA" >&2
  exit 1
fi

mkdir "$work/x"
tar -xzf "$work/plugin.tar.gz" -C "$work/x"
up="$(find "$work/x" -mindepth 1 -maxdepth 1 -type d | head -n1)"

grep -q "pluginVersion = \"${TAG}\"" "$up/version.go" || {
  echo "FATAL: version.go in the tarball does not say ${TAG}" >&2
  exit 1
}

rm -rf "$DEST/src"
mkdir -p "$DEST/src"
(
  cd "$up"
  {
    printf '%s\n' LICENSE .traefik.yml go.mod go.sum
    find . -name '*.go' ! -name '*_test.go' ! -path './tests/*' ! -path './examples/*' ! -path './vendor/*' | sed 's|^\./||'
    find vendor -type f
  } | sort -u | while read -r f; do
    mkdir -p "$DEST/src/$(dirname "$f")"
    cp -p "$f" "$DEST/src/$f"
  done
)
# Upstream's .traefik.yml testData carries an example CrowdsecLapiKey. It is not a
# credential, but secret scanners (gitleaks in the skred gate) flag it on every ref,
# so it is blanked. Traefik does not use testData when loading a local plugin.
sed -i -E 's/^([[:space:]]*CrowdsecLapiKey:).*/\1 ""/' "$DEST/src/.traefik.yml"
(cd "$DEST" && find src -type f | LC_ALL=C sort | xargs sha256sum > SHA256SUMS)

commit="$(curl -fsSL "https://api.github.com/repos/${REPO}/commits/${TAG}" | sed -n 's/^  "sha": "\([0-9a-f]*\)".*/\1/p' | head -n1)"
echo "Vendored ${REPO} ${TAG} into ${DEST#"$ROOT"/}"
echo "  tag commit:      ${commit:-<look it up>}"
echo "  tarball sha256:  ${got}"
echo "  files:           $(wc -l < "$DEST/SHA256SUMS")"
echo "Now update traefik_crowdsec_plugin_version in v1/ansible/shared/tasks/traefik_crowdsec_plugin.yml,"
echo "NOTICE.md next to the vendored tree, and the CHANGELOG."
