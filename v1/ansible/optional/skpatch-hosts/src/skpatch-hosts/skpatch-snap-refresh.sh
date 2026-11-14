#!/bin/sh
# Managed by SKStacks optional/skpatch-hosts (provision_skpatch_hosts_nodes.yml).
# Do not edit by hand; rerun the playbook instead.
# Installed root:root 0755 as /usr/local/sbin/skpatch-snap-refresh.
#
# Why this exists: the dashboard's "upgrade selected packages" action for
# snaps runs `snap refresh <name> [<name>...]`. Names vary per call, so a
# sudoers line would need a wildcard, and a wildcard on snap would let the
# caller pass any flag. Sudoers therefore grants THIS script (absolute path,
# no wildcard) and the script checks every argument itself:
#   - at least one argument;
#   - every argument must match ^[a-z0-9][a-z0-9-]*$. Snap names are lowercase
#     letters, digits and hyphens (snapd: 2 to 40 characters, no leading or
#     trailing hyphen, no double hyphen, at least one letter). This regex is
#     deliberately a little looser than snapd's own rule (it is a guard
#     against option-shaped and shell-shaped input, not a name validator:
#     snapd rejects a malformed but regex-passing name itself) and a little
#     stricter in one place: parallel-install instance names (name_key) are
#     refused, because "_" is not allowed.
#   - the whole call is rejected, before anything runs, if ANY argument fails.
# Then it execs `snap refresh -- <names>`. snapd's CLI parser (go-flags)
# honours "--": verified on snap 2.78, where `snap refresh -x` fails with
# "unknown flag `x'" while `snap refresh -- -x` is read as a snap name. So the
# "--" is a second guard on top of the regex. The environment is cleared
# (env -i) and PATH is fixed.
#
# Tests copy this file and substitute SNAP below with a stub; the installed
# copy has a fixed absolute path and takes no override from argv or the
# environment, so it cannot be redirected under sudo.
set -eu

LC_ALL=C
export LC_ALL

SNAP=/usr/bin/snap
ENV_BIN=/usr/bin/env

die() {
    printf 'skpatch-snap-refresh: %s\n' "$1" >&2
    exit 2
}

[ "$#" -ge 1 ] || die "usage: skpatch-snap-refresh <snap> [<snap>...]"

for name in "$@"; do
    case $name in
        '' | [!a-z0-9]* | *[!a-z0-9-]*)
            die "refusing invalid snap name: '$(printf '%s' "$name" | LC_ALL=C tr -c 'a-zA-Z0-9+.:_-' '?')'"
            ;;
    esac
done

exec "$ENV_BIN" -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    LC_ALL=C \
    "$SNAP" refresh -- "$@"
