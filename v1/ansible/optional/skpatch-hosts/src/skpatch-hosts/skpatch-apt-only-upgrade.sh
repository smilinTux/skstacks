#!/bin/sh
# Managed by SKStacks optional/skpatch-hosts (provision_skpatch_hosts_nodes.yml).
# Do not edit by hand; rerun the playbook instead.
# Installed root:root 0755 as /usr/local/sbin/skpatch-apt-only-upgrade.
#
# Why this exists: the dashboard's "upgrade selected packages" action runs
#   apt-get -o DPkg::Lock::Timeout=60 install --only-upgrade -y <pkg> [<pkg>...]
# and the package names vary per call, so a sudoers line for it would need a
# wildcard. A wildcard on apt-get lets the caller append options such as
# `-o APT::Update::Pre-Invoke::=<cmd>` and run anything as root. So sudoers
# grants THIS script (by absolute path, no wildcard) and the script does the
# argument checking sudoers cannot do:
#   - at least one argument;
#   - every argument must match ^[a-z0-9][a-z0-9+.:-]*$ (a Debian package name
#     with an optional ":arch"), so nothing starting with "-" and no "=", "/",
#     "*", "~", "^", "?", whitespace, quotes or shell metacharacters passes;
#   - the whole call is rejected, before anything runs, if ANY argument fails.
# It then execs one fixed command line. Nothing from the caller's environment
# is passed on (env -i), and "--" ends option parsing so a name can never be
# read as an option even if the check above were ever loosened.
#
# A name that looks like a regex (it contains "." or "+", both legal in
# Debian names such as libsdl1.2 or g++-13) is resolved by apt as an exact
# name first. In the worst case it selects a few more INSTALLED packages that
# have a newer candidate, because --only-upgrade never installs anything new.
# That is the same effect the dashboard itself can already have, never a
# privilege boundary.
#
# Tests copy this file and substitute APT_GET below with a stub; the installed
# copy has a fixed absolute path and takes no override from argv or the
# environment, so it cannot be redirected under sudo.
set -eu

LC_ALL=C
export LC_ALL

APT_GET=/usr/bin/apt-get
ENV_BIN=/usr/bin/env

die() {
    printf 'skpatch-apt-only-upgrade: %s\n' "$1" >&2
    exit 2
}

[ "$#" -ge 1 ] || die "usage: skpatch-apt-only-upgrade <package> [<package>...]"

# Under LC_ALL=C the bracket ranges are plain ASCII. The first pattern rejects
# the empty string and any name that does not start with [a-z0-9]; the second
# rejects any character outside [a-z0-9+.:-] (a newline included).
for pkg in "$@"; do
    case $pkg in
        '' | [!a-z0-9]* | *[!a-z0-9+.:-]*)
            die "refusing invalid package name: '$(printf '%s' "$pkg" | LC_ALL=C tr -c 'a-zA-Z0-9+.:_-' '?')'"
            ;;
    esac
done

exec "$ENV_BIN" -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    LC_ALL=C \
    DEBIAN_FRONTEND=noninteractive \
    "$APT_GET" -o DPkg::Lock::Timeout=60 install --only-upgrade -y -- "$@"
