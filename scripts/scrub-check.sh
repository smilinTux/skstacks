#!/usr/bin/env bash
#
# scrub-check.sh: fail if any file under the given path(s) contains what
# looks like a real private IP address, as opposed to a placeholder. Guards
# published, public content (the config-template source at
# templates/config-repo/) against accidentally leaking real infrastructure
# topology.
#
# This deliberately does NOT also hardcode the fleet's real hostnames or
# domains the way smilinTux/skgateway's scrub-check.sh does: this framework
# repo already runs that check privately, at the estate-denylist gate in
# .github/workflows/skred-scan.yml (`python -m skred.denylist
# "$GITHUB_WORKSPACE" ...`), which scans the whole workspace -- including
# templates/config-repo/ and this script -- on every push and same-repo PR,
# with its exact pattern list kept in the ESTATE_DENYLIST secret so it can
# never leak by being read out of a public script. A public script that
# re-encoded those same identifiers as literal text would itself be a
# finding against that gate. Private IP ranges carry no such risk: they
# identify no estate (RFC 1918 is used by every private network everywhere),
# so checking for them here is safe to keep public, which also means it
# still runs on a fork PR, where the ESTATE_DENYLIST secret is unavailable.
#
# Usage:
#   scripts/scrub-check.sh <path>...
#
# Exits 1 and prints every match to stderr if anything is found, 0 otherwise.
set -euo pipefail

[ $# -ge 1 ] || { echo "usage: scrub-check.sh <path>..." >&2; exit 2; }

# Private IP ranges that could point at real infrastructure:
# 10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12.
PRIVATE_IP_RE='\b(10(\.[0-9]{1,3}){3}|192\.168(\.[0-9]{1,3}){2}|172\.(1[6-9]|2[0-9]|3[01])(\.[0-9]{1,3}){2})\b'

# Allowed "documentation" addresses: RFC 5737 TEST-NET-1/2/3 (not actually
# private, but harmless to allow-list here too) plus the two most common
# generic placeholder addresses that show up in illustrative examples
# everywhere and identify no real host.
DOC_IP_ALLOW_RE='^(192\.0\.2\.[0-9]{1,3}|198\.51\.100\.[0-9]{1,3}|203\.0\.113\.[0-9]{1,3}|192\.168\.1\.1|10\.0\.0\.1)$'

found=0

check_file() {
  local f="$1"

  while IFS=: read -r lineno rest; do
    [ -n "$lineno" ] || continue
    local ip
    ip="$(printf '%s\n' "$rest" | grep -oE "$PRIVATE_IP_RE" | head -n1)"
    [ -n "$ip" ] || continue
    if [[ "$ip" =~ $DOC_IP_ALLOW_RE ]]; then
      continue
    fi
    echo "SCRUB: $f:$lineno: possible real private IP ($ip): $rest" >&2
    found=1
  done < <(grep -nE "$PRIVATE_IP_RE" "$f" 2>/dev/null || true)
}

while IFS= read -r -d '' f; do
  check_file "$f"
done < <(find "$@" -type f -print0)

exit "$found"
