#!/bin/sh
# Managed by SKStacks optional/skpatch-hosts (provision_skpatch_hosts_nodes.yml).
# Do not edit by hand; rerun the playbook instead.
# Installed root:root 0755 as /usr/local/sbin/skpatch-pacman-upgrade.
#
# Why this exists: the dashboard's "upgrade selected packages" action on an
# Arch-family host runs
#   pacman -S --noconfirm <pkg> [<pkg>...]
# with names that vary per call. A sudoers wildcard on pacman lets the caller
# add options (--config, --dbpath, --hookdir, -U <file>) and run anything as
# root, and plain `pacman -S <name>` also INSTALLS a package that is not there
# yet. So sudoers grants THIS script (absolute path, no wildcard) and the
# script:
#   - needs at least one argument;
#   - accepts only names matching ^[a-z0-9@_+][a-z0-9@._+-]*$ (pacman's own
#     package-name alphabet), so nothing starting with "-" and no "/", "=",
#     whitespace, quotes or shell metacharacters;
#   - refuses any name that is not ALREADY installed (pacman -Q), so it only
#     ever upgrades, the same contract as apt's --only-upgrade;
#   - rejects the whole call before anything runs if any argument fails;
#   - execs one fixed command line with a fixed environment and "--" before
#     the names.
#
# Tests copy this file and substitute PACMAN below with a stub; the installed
# copy has a fixed absolute path and takes no override from argv or the
# environment, so it cannot be redirected under sudo.
set -eu

LC_ALL=C
export LC_ALL

PACMAN=/usr/bin/pacman
ENV_BIN=/usr/bin/env

die() {
    printf 'skpatch-pacman-upgrade: %s\n' "$1" >&2
    exit 2
}

[ "$#" -ge 1 ] || die "usage: skpatch-pacman-upgrade <package> [<package>...]"

for pkg in "$@"; do
    case $pkg in
        '' | [!a-z0-9@_+]* | *[!a-z0-9@._+-]*)
            die "refusing invalid package name: '$(printf '%s' "$pkg" | LC_ALL=C tr -c 'a-zA-Z0-9@._+-' '?')'"
            ;;
    esac
done

for pkg in "$@"; do
    "$ENV_BIN" -i PATH=/usr/sbin:/usr/bin:/sbin:/bin LC_ALL=C "$PACMAN" -Q -- "$pkg" >/dev/null 2>&1 \
        || die "refusing a package that is not installed: '$pkg' (this only upgrades)"
done

exec "$ENV_BIN" -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    LC_ALL=C \
    "$PACMAN" -S --noconfirm -- "$@"
