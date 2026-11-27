#!/bin/sh
# skpatch-hosts NetBird keep-up. Installed only where the host var
# skpatch_hosts_netbird_keepup is true, run every 5 min by
# skpatch-netbird-keepup.timer (provision_skpatch_hosts_nodes.yml).
#
# Why: a NetBird client that gets a 403 from its management server (a proxy
# catch-all while the backend is down) maps it to PermissionDenied, leaves its
# retry loop for good and sits in NeedsLogin until someone runs `netbird up` by
# hand. On a setup-key peer that `netbird up` needs no input, so this does it.
# On an SSO-enrolled peer it would only print a login URL: keep the var off.
#
# Acts only when netbird is installed, the daemon status is NeedsLogin or Idle,
# and the active profile does not set "DisableAutoConnect": true. A plain
# `netbird down` (Idle) is undone within 5 min; to keep a peer down,
# run `netbird up --disable-auto-connect` before `netbird down`, or turn the
# host var off.
# Output goes to the journal. Always exits 0.
# SKP_NETBIRD_ROOT prefixes the config paths (tests only).
set -u
root=${SKP_NETBIRD_ROOT:-}
dir="$root/var/lib/netbird"

command -v netbird >/dev/null 2>&1 || exit 0

state=$(netbird status --json 2>/dev/null | sed -n 's/.*"daemonStatus"[[:space:]]*:[[:space:]]*"\([A-Za-z]*\)".*/\1/p' | head -n 1)
case "$state" in
  NeedsLogin | Idle) ;;
  *) exit 0 ;;
esac

# The active profile: /var/lib/netbird/active_profile.json names it ("name",
# default "default"); default is default.json (older clients:
# /etc/netbird/config.json), any other one <name>.json in the shared
# directory or a pre-migration per-user one.
name=$(sed -n 's/.*"name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$dir/active_profile.json" 2>/dev/null | head -n 1)
[ -n "$name" ] || name=default
profile=""
if [ "$name" = default ]; then
  for f in "$dir/default.json" "$root/etc/netbird/config.json"; do
    [ -f "$f" ] && { profile=$f; break; }
  done
else
  for f in "$dir/$name.json" "$dir"/*/"$name.json"; do
    [ -f "$f" ] && { profile=$f; break; }
  done
fi
if [ -z "$profile" ]; then
  echo "skpatch-netbird-keepup: daemon is $state but no config for profile '$name'; not touching it"
  exit 0
fi
if grep -Eq '"DisableAutoConnect"[[:space:]]*:[[:space:]]*true' "$profile" 2>/dev/null; then
  exit 0
fi

echo "skpatch-netbird-keepup: daemon is $state (profile '$name'), running netbird up"
timeout 60 netbird up --no-browser 2>&1
echo "skpatch-netbird-keepup: netbird up exited $?"
exit 0
