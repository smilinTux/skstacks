#!/bin/bash
# skrac agent watchdog (deploy_skrac-agent-watchdog.yml): restart a MeshCentral
# agent whose server session went stale. Uses local signals only, so it needs
# no MeshCentral credentials on the host:
#   - stale session: the agent's newest server connection has received nothing
#     for STALE_S seconds (the server and agent exchange keepalives about every
#     2 minutes, so a half-open connection that still shows ESTABLISHED is
#     caught here);
#   - no session: no established connection to the server port for
#     NOCONN_CHECKS consecutive runs;
#   - spin: the agent's main process (children, such as a desktop session,
#     excluded) above SPIN_PCT percent CPU for SPIN_CHECKS consecutive runs.
# An agent younger than MIN_UPTIME_S is left alone (it may still be connecting).
set -u
STALE_S=${STALE_S:-600}
NOCONN_CHECKS=${NOCONN_CHECKS:-2}
SPIN_PCT=${SPIN_PCT:-80}
SPIN_CHECKS=${SPIN_CHECKS:-2}
MIN_UPTIME_S=${MIN_UPTIME_S:-300}
SERVER_PORT=${SERVER_PORT:-443}
STATE_DIR=${STATE_DIR:-/run/skrac-agent-watchdog}
PROC=${PROC:-/proc}
UNITS=${UNITS:-$(systemctl list-units --type=service --state=running --no-legend --plain 'meshagent*' | awk '{print $1}')}

umask 077
mkdir -p "$STATE_DIR"
log() { logger -t skrac-agent-watchdog -- "$*"; echo "$*"; }
hz=$(getconf CLK_TCK)

for u in $UNITS; do
  pid=$(systemctl show -p MainPID --value "$u")
  [ "${pid:-0}" -gt 0 ] 2>/dev/null || continue
  now=$(date +%s)
  started=$(systemctl show -p ActiveEnterTimestampMonotonic --value "$u")
  up=$(( $(awk '{print int($1)}' "$PROC/uptime") - ${started:-0} / 1000000 ))
  sf="$STATE_DIR/$(printf %s "$u" | tr -c 'A-Za-z0-9._-' '_').state"

  lastpid='' noconn=0 spin=0 lastticks='' lasttime=''
  # shellcheck disable=SC1090  # our own state file, root-only directory
  [ -f "$sf" ] && . "$sf"
  if [ "$lastpid" != "$pid" ]; then noconn=0; spin=0; lastticks=''; lasttime=''; fi

  # Milliseconds since the newest of the main process's server connections
  # last received data (ss -i: "lastrcv:<ms>" on the line after the socket).
  rcv=$(ss -tnpi state established "( dport = :$SERVER_PORT )" 2>/dev/null \
    | awk -v p="pid=$pid," 'index($0, p) { f = 1; next }
        f { if (match($0, /lastrcv:[0-9]+/)) print substr($0, RSTART + 8, RLENGTH - 8); f = 0 }' \
    | sort -n | head -1)

  reason=''
  if [ -z "$rcv" ]; then
    noconn=$((noconn + 1))
    [ "$noconn" -ge "$NOCONN_CHECKS" ] && reason="no server connection for $noconn checks"
  else
    noconn=0
    [ $((rcv / 1000)) -ge "$STALE_S" ] && reason="nothing received from the server for $((rcv / 1000)) s"
  fi

  ticks=$(awk '{print $14 + $15}' "$PROC/$pid/stat" 2>/dev/null)
  if [ -n "$ticks" ] && [ -n "$lastticks" ] && [ $((now - lasttime)) -gt 0 ]; then
    pct=$(( (ticks - lastticks) * 100 / hz / (now - lasttime) ))
    if [ "$pct" -ge "$SPIN_PCT" ]; then spin=$((spin + 1)); else spin=0; fi
    [ -z "$reason" ] && [ "$spin" -ge "$SPIN_CHECKS" ] && reason="main process at ${pct}% CPU for $spin checks"
  fi

  if [ -n "$reason" ] && [ "$up" -ge "$MIN_UPTIME_S" ]; then
    log "$u (pid $pid, up ${up}s): $reason; restarting"
    systemctl restart "$u"
    rm -f "$sf"
    continue
  fi
  printf 'lastpid=%s\nnoconn=%s\nspin=%s\nlastticks=%s\nlasttime=%s\n' "$pid" "$noconn" "$spin" "$ticks" "$now" > "$sf"
done
