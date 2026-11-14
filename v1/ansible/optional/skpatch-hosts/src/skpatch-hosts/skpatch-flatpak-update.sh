#!/bin/sh
# Managed by SKStacks optional/skpatch-hosts (provision_skpatch_hosts_nodes.yml).
# Do not edit by hand; rerun the playbook instead.
# Installed root:root 0755 as /usr/local/sbin/skpatch-flatpak-update.
#
# Why this exists: the dashboard's "upgrade selected" action for Flatpak runs
#   flatpak update -y <id> [<id>...]
# with ids that vary per call. A sudoers wildcard on flatpak lets the caller
# add other subcommands' options or a different verb entirely (install from
# any remote, run, override) as root. So sudoers grants THIS script (absolute
# path, no wildcard) and the script:
#   - needs at least one argument;
#   - accepts only application ids matching ^[A-Za-z0-9][A-Za-z0-9._-]*$
#     (reverse-DNS ids such as org.mozilla.firefox), so nothing starting with
#     "-" and no "/" (full refs), "=", whitespace or shell metacharacters;
#   - rejects the whole call before anything runs if any argument fails;
#   - execs one fixed command line (`update`, which never installs anything
#     new) with a fixed environment and "--" before the ids.
#
# Tests copy this file and substitute FLATPAK below with a stub; the installed
# copy has a fixed absolute path and takes no override from argv or the
# environment, so it cannot be redirected under sudo.
set -eu

LC_ALL=C
export LC_ALL

FLATPAK=/usr/bin/flatpak
ENV_BIN=/usr/bin/env

die() {
    printf 'skpatch-flatpak-update: %s\n' "$1" >&2
    exit 2
}

[ "$#" -ge 1 ] || die "usage: skpatch-flatpak-update <app-id> [<app-id>...]"

for id in "$@"; do
    case $id in
        '' | [!A-Za-z0-9]* | *[!A-Za-z0-9._-]*)
            die "refusing invalid flatpak id: '$(printf '%s' "$id" | LC_ALL=C tr -c 'a-zA-Z0-9._-' '?')'"
            ;;
    esac
done

exec "$ENV_BIN" -i \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    LC_ALL=C \
    "$FLATPAK" update -y --noninteractive -- "$@"
