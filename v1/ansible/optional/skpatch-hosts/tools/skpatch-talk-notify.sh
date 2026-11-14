#!/bin/sh
# skpatch-hosts alert hook: post to a Nextcloud Talk room.
#
# The hook interface of tools/skpatch_compliance.py and tools/skpatch_watch.py:
#   skpatch-talk-notify.sh raise|resolve|post <key> <level> <message>
# raise and resolve are called once per issue (the tools de-duplicate by
# host|issue, so a problem that stays open is never posted twice); post is a
# plain message (the daily summary).
#
# Configuration (environment, normally the tool's EnvironmentFile):
#   SKP_TALK_URL            the Nextcloud base URL (https://cloud.example.org)
#   SKP_TALK_ROOM           the room token
#   SKP_TALK_USER           the account that posts (a room participant)
#   SKP_TALK_PASSWORD_FILE  file holding that account's app password (0600);
#                           a dedicated app password, never the login one
#   SKP_TALK_MENTION        optional user id to @mention on raise (so the
#                           person is notified in a room set to "mentions")
# The password goes to curl on stdin (-K -), never on the command line.
set -eu
action=${1:-}
key=${2:-}
level=${3:-info}
msg=${4:-}

case "$action" in
  raise)
    case "$level" in
      crit|critical|error) icon="🔴" ;;
      *) icon="🟠" ;;
    esac
    text="$icon skpatch: $key
$msg"
    [ -n "${SKP_TALK_MENTION:-}" ] && text="$text

@\"$SKP_TALK_MENTION\""
    ;;
  resolve) text="✅ skpatch resolved: $key
$msg" ;;
  post) text=$msg ;;
  *) echo "usage: $0 raise|resolve|post <key> <level> <message>" >&2; exit 2 ;;
esac

: "${SKP_TALK_URL:?}" "${SKP_TALK_ROOM:?}" "${SKP_TALK_USER:?}" "${SKP_TALK_PASSWORD_FILE:?}"
pw=$(cat "$SKP_TALK_PASSWORD_FILE")
url="${SKP_TALK_URL%/}/ocs/v2.php/apps/spreed/api/v1/chat/$SKP_TALK_ROOM"

code=$(printf 'user = "%s:%s"\n' "$SKP_TALK_USER" "$pw" | curl -sS -K - -o /dev/null -w '%{http_code}' \
  --max-time 20 --retry 2 \
  -H 'OCS-APIRequest: true' -H 'Accept: application/json' \
  --data-urlencode "message=$text" "$url") || code=000
case "$code" in
  2??) exit 0 ;;
  *) echo "Talk post failed: HTTP $code" >&2; exit 1 ;;
esac
