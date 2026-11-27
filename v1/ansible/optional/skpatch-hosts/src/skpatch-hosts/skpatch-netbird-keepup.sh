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
# If `netbird up` prints a URL (a login or device-code prompt: the peer really
# needs SSO), it logs one warning without the URL and skips runs for 6 hours
# (BACKOFF_S) instead of dropping a fresh login URL into the journal every 5 min.
# Output goes to the journal. Always exits 0.
# SKP_NETBIRD_ROOT prefixes the config and state paths (tests only).
set -u
root=${SKP_NETBIRD_ROOT:-}
dir="$root/var/lib/netbird"
backoff="$root/var/lib/skpatch-netbird-keepup/backoff-until"
BACKOFF_S=21600

command -v netbird >/dev/null 2>&1 || exit 0

now=$(date +%s)
until_s=$(cat "$backoff" 2>/dev/null || echo 0)
case "$until_s" in '' | *[!0-9]*) until_s=0 ;; esac
[ "$now" -lt "$until_s" ] && exit 0

# netbird 0.67 and later: "daemonStatus" in --json (one compact line). Older
# clients have no such key and, in NeedsLogin, print "Daemon status: X" as
# plain text even with --json; plain `netbird status` is the last resort.
parse() {
  sed -n -e 's/.*"daemonStatus"[[:space:]]*:[[:space:]]*"\([A-Za-z]*\)".*/\1/p' \
    -e 's/^Daemon status: \([A-Za-z]*\).*/\1/p' | head -n 1
}
state=$(timeout 20 netbird status --json 2>/dev/null | parse)
[ -n "$state" ] || state=$(timeout 20 netbird status 2>/dev/null | parse)
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
out=$(timeout 60 netbird up --no-browser 2>&1)
rc=$?
if printf '%s\n' "$out" | grep -Eqi 'https?://'; then
  mkdir -p "${backoff%/*}" && echo $((now + BACKOFF_S)) > "$backoff"
  echo "skpatch-netbird-keepup: WARNING netbird up asks for an interactive login (URL not logged): this peer needs SSO, not a setup key. Skipping runs for $((BACKOFF_S / 3600)) h; turn skpatch_hosts_netbird_keepup off for it"
  exit 0
fi
[ -n "$out" ] && printf '%s\n' "$out"
echo "skpatch-netbird-keepup: netbird up exited $rc"
exit 0
