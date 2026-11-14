#!/bin/sh
# Example alert hook for skpatch_compliance.py. Copy it, point it at your own
# alert command, and name the copy in SKPC_HOOK / --alert-hook.
#
# Called once when an issue opens and once when it clears (the tool keeps the
# de-duplication state itself), as:
#   <hook> raise   <host|issue> warn <message>
#   <hook> resolve <host|issue> info <message>
# The key is stable per host and issue, so an alert system that threads or
# de-duplicates by key (here: sk-alert's -g group) shows one item per problem.
set -eu
action=$1 key=$2 level=$3 msg=$4
group="skpatch/$(printf '%s' "$key" | tr '|:' '//')"
case "$action" in
  raise)   exec sk-alert -l "$level" -g "$group" --title "skpatch: $key" "$msg" ;;
  resolve) exec sk-alert -g "$group" --resolve "$msg" ;;
  *)       echo "unknown action: $action" >&2; exit 2 ;;
esac
