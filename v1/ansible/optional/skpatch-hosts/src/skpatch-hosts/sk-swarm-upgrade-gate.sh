#!/bin/sh
# sk-swarm-upgrade-gate: APT hook that keeps Docker engine upgrades on swarm
# nodes safe. Installed by skstacks skpatch-hosts
# (provision_skpatch_hosts_nodes.yml) on every swarm member, wired as:
#   DPkg::Pre-Install-Pkgs { "/usr/local/sbin/sk-swarm-upgrade-gate pre"; };
#   DPkg::Post-Invoke      { "/usr/local/sbin/sk-swarm-upgrade-gate post"; };
#
# Why: the skpatch-hosts dashboard runs a schedule's upgrades one host after
# another, but it only waits for apt to exit. A docker-ce or containerd.io
# upgrade restarts dockerd, and the swarm needs minutes to reschedule that
# node's tasks, so three managers in one schedule restarted dockerd within
# about 90 seconds of each other. And on a worker, a restart once left a stale
# overlay vxlan device behind, so every task the swarm placed there was
# rejected ("error creating vxlan interface: file exists") for 11 hours.
#
# It acts ONLY when the batch apt is about to install contains docker-ce or
# containerd.io, and only on a swarm member. Every other upgrade passes
# straight through.
#
# The hold. A failed post sets it; while it is set, no swarm node upgrades its
# engine. It is a file on the swarm's shared storage (SHARED_DIR, default
# /var/data/skpatch-swarm-gate/hold), which every node, worker or manager, can
# read, and on a manager also the node label sk.skpatch.hold. Clear both once
# the swarm is healthy: sk-swarm-upgrade-gate clear-hold (on a manager).
#
#   pre  (before dpkg installs the batch), every swarm member:
#     1. refuse while the hold is set (shared file, or any node's label)
#   and on a manager also:
#     1a. on a swarm with ONE manager, refuse unless an engine window is open
#        (node label sk.skpatch.engine-window=<epoch until>, set by hand for
#        the window, e.g. docker node update --label-add
#        sk.skpatch.engine-window=$(( $(date +%s) + 7200 )) <node>), or
#        SINGLE_MANAGER_ENGINE=allow is set in the knobs file. There is no
#        other manager to keep the swarm up, so an engine upgrade there is a
#        full outage of every service and must never happen unattended
#        (an instance, 2026-11-17: an unattended upgrade took SSO, Nextcloud
#        and remote access down for 7 to 12 minutes)
#     2. wait until GAP_S has passed since the last manager finished its
#        docker upgrade (label sk.skpatch.docker-upgraded-at, epoch seconds)
#     3. wait (at most PRE_TIMEOUT_S) while another manager carries a fresh
#        sk.skpatch.docker-upgrading label (overlapping schedules, a manual
#        run); it is set when an upgrade is let through and removed by post,
#        and ignored once older than BUSY_STALE_S (3600)
#     4. wait (same bound) until every manager is Ready and Leader/Reachable
#        and this node is Ready/Active; refuse on timeout
#     5. record which services are already short of their desired replicas
#        (a pre-existing failure is reported, not blamed on this upgrade)
#   post (after dpkg, only if pre let a docker upgrade through):
#     wait MIN_SETTLE_S, then (at most POST_TIMEOUT_S) until, on two polls in
#     a row: dockerd answers and this node is an active swarm member; no task
#     was rejected here with "vxlan interface: file exists" since the upgrade
#     (read from dockerd's journal, so it works on a worker too); and on a
#     manager every manager is Ready and Leader/Reachable and every replicated
#     or global service not in the pre baseline is at its desired replicas.
#     A vxlan rejection is repaired: each vx-<vni>-<net> device (host netns
#     and docker's overlay netns) whose overlay has no running container on
#     this node, and that is not the ingress overlay, is deleted; docker
#     recreates it on the next task. Then the checks start over.
#     Success on a manager labels it sk.skpatch.docker-upgraded-at=<now>.
#     Timeout sets the hold and fails, so apt reports an error (the dashboard
#     records a failed upgrade and skpatch_watch alerts) and every later
#     node's pre refuses.
#
# A refusing pre makes apt abort the whole run (nothing is installed); the
# dashboard records the upgrade as failed and the host is retried next window.
#
# Knobs (/etc/default/sk-swarm-upgrade-gate, written by the role):
#   GAP_S=600 PRE_TIMEOUT_S=900 POST_TIMEOUT_S=900 MIN_SETTLE_S=60 POLL_S=15
#   SHARED_DIR=/var/data/skpatch-swarm-gate
#   SINGLE_MANAGER_ENGINE=hold (default) | allow
# Bypass for one manual run (logged): SK_SWARM_GATE_SKIP=1 apt-get ...
# Check without side effects: echo <deb path> | sk-swarm-upgrade-gate pre --dry-run
#                             sk-swarm-upgrade-gate post --dry-run
set -u

MODE=${1:-}
DRY=0
[ "${2:-}" = "--dry-run" ] && DRY=1

CONF=${SK_SWARM_GATE_CONF:-/etc/default/sk-swarm-upgrade-gate}
# shellcheck disable=SC1090
[ -r "$CONF" ] && . "$CONF"
GAP_S=${GAP_S:-600}
PRE_TIMEOUT_S=${PRE_TIMEOUT_S:-900}
POST_TIMEOUT_S=${POST_TIMEOUT_S:-900}
MIN_SETTLE_S=${MIN_SETTLE_S:-60}
POLL_S=${POLL_S:-15}
SINGLE_MANAGER_ENGINE=${SINGLE_MANAGER_ENGINE:-hold}
STATE_DIR=${STATE_DIR:-/run/sk-swarm-upgrade-gate}
SHARED_DIR=${SHARED_DIR:-/var/data/skpatch-swarm-gate}
NETNS_DIR=${NETNS_DIR:-/run/docker/netns}
DOCKER=${DOCKER:-docker}
JOURNALCTL=${JOURNALCTL:-journalctl}
IP=${IP:-ip}
NSENTER=${NSENTER:-nsenter}

LABEL_HOLD=sk.skpatch.hold
LABEL_DONE=sk.skpatch.docker-upgraded-at
LABEL_BUSY=sk.skpatch.docker-upgrading
LABEL_WINDOW=sk.skpatch.engine-window
BUSY_STALE_S=${BUSY_STALE_S:-3600}
ENGINE_RE='(^|/)(docker-ce|containerd\.io)_[^/]*\.deb$'
VXLAN_ERR='error creating vxlan interface: file exists'

log() {
    logger -t sk-swarm-upgrade-gate -- "$*" 2>/dev/null || true
    echo "sk-swarm-upgrade-gate: $*" >&2
}
now() { date +%s; }
pause() { [ "$DRY" = 1 ] || sleep "$1"; }

is_manager() { [ "$($DOCKER info --format '{{.Swarm.ControlAvailable}}' 2>/dev/null)" = "true" ]; }
is_member() { [ "$($DOCKER info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null)" = "active" ]; }
self_id() { $DOCKER info --format '{{.Swarm.NodeID}}' 2>/dev/null; }

# Value of a node label on every node, one per line ("" when unset). Managers only.
label_values() {
    for n in $($DOCKER node ls -q 2>/dev/null); do
        v=$($DOCKER node inspect --format "{{index .Spec.Labels \"$1\"}}" "$n" 2>/dev/null)
        [ "$v" = "<no value>" ] && v=""
        printf '%s %s\n' "$n" "$v"
    done
}

# Why the swarm is held, or nothing. The shared file works on every node;
# the labels are only readable on a manager.
hold_reason() {
    [ -s "$SHARED_DIR/hold" ] && echo "$SHARED_DIR/hold: $(head -n 1 "$SHARED_DIR/hold")"
    if [ "$1" = manager ]; then
        held=$(label_values "$LABEL_HOLD" | awk '$2 != ""')
        [ -n "$held" ] && echo "$LABEL_HOLD on: $(echo "$held" | tr '\n' ' ')"
    fi
}

set_hold() {
    stamp="$(date -u +%Y%m%dT%H%M%SZ)-$(hostname)"
    if [ "$1" = manager ]; then
        $DOCKER node update --label-add "$LABEL_HOLD=$stamp" "$(self_id)" >/dev/null 2>&1 \
            || log "could not set $LABEL_HOLD on this node"
    fi
    { mkdir -p "$SHARED_DIR" && echo "$stamp $2" > "$SHARED_DIR/hold"; } 2>/dev/null \
        || log "could not write $SHARED_DIR/hold (no shared storage?): only this node's label holds the swarm"
    echo "$stamp"
}

# Prints why the nodes are not healthy; prints nothing when they are. Managers only.
nodes_problem() {
    out=$($DOCKER node ls --format '{{.Self}} {{.Hostname}} {{.Status}} {{.Availability}} {{.ManagerStatus}}' 2>/dev/null) \
        || { echo "docker node ls failed"; return; }
    [ -n "$out" ] || { echo "docker node ls returned nothing"; return; }
    echo "$out" | while read -r self host status avail mstatus; do
        if [ "$self" = "true" ] && { [ "$status" != "Ready" ] || [ "$avail" != "Active" ]; }; then
            echo "this node $host is $status/$avail"
        fi
        if [ -n "${mstatus:-}" ]; then
            case "$mstatus" in
                Leader|Reachable) [ "$status" = "Ready" ] || echo "manager $host is $status" ;;
                *) echo "manager $host is $mstatus" ;;
            esac
        fi
    done
}

# What a worker can see of itself: its swarm state and the swarm error.
self_problem() {
    state=$($DOCKER info --format '{{.Swarm.LocalNodeState}}' 2>/dev/null)
    [ "$state" = "active" ] || echo "this node's swarm state is ${state:-unknown}"
    err=$($DOCKER info --format '{{.Swarm.Error}}' 2>/dev/null)
    [ -n "$err" ] && echo "swarm error: $err"
}

# Number of managers in the swarm (0 when docker node ls fails).
manager_count() {
    $DOCKER node ls --format '{{.Self}} {{.Hostname}} {{.Status}} {{.Availability}} {{.ManagerStatus}}' 2>/dev/null \
        | awk 'NF >= 5 {n++} END {print n + 0}'
}

# Names of replicated/global services below their desired replicas. Managers only.
unconverged() {
    $DOCKER service ls --format '{{.Name}} {{.Mode}} {{.Replicas}}' 2>/dev/null \
    | while read -r name mode reps _rest; do
        case "$mode" in replicated|global) ;; *) continue ;; esac
        run=${reps%%/*}
        want=${reps#*/}
        want=${want%% *}
        [ "$run" = "$want" ] || echo "$name"
    done
}

# How many tasks dockerd rejected for a stale vxlan device since epoch $1.
vxlan_rejections() {
    $JOURNALCTL -u docker --since "@$1" --no-pager -o cat 2>/dev/null | grep -c "$VXLAN_ERR"
}

# "<netns file or -> <device>" for every vx-* device: host netns, then docker's.
vx_devices() {
    $IP -o link show type vxlan 2>/dev/null | awk -F': ' '{print $2}' | sed 's/@.*//' \
        | grep '^vx-' | sed 's/^/- /'
    for ns in "$NETNS_DIR"/*; do
        [ -e "$ns" ] || continue
        $NSENTER --net="$ns" $IP -o link show type vxlan 2>/dev/null | awk -F': ' '{print $2}' \
            | sed 's/@.*//' | grep '^vx-' | sed "s#^#$ns #"
    done
}

# Full IDs of the overlays a device must not be taken from: every network a
# running container here is on, and the ingress overlay (routing mesh).
kept_networks() {
    for c in $($DOCKER ps -q 2>/dev/null); do
        $DOCKER inspect --format '{{range .NetworkSettings.Networks}}{{.NetworkID}} {{end}}' "$c" 2>/dev/null
    done | tr ' ' '\n'
    for n in $($DOCKER network ls -q --no-trunc --filter driver=overlay 2>/dev/null); do
        [ "$($DOCKER network inspect --format '{{.Ingress}}' "$n" 2>/dev/null)" = "true" ] && echo "$n"
    done
}

# Delete one vx device: $1 is "-" (host netns) or docker's netns file.
del_dev() {
    if [ "$1" = - ]; then $IP link delete "$2"; else $NSENTER --net="$1" $IP link delete "$2"; fi >/dev/null 2>&1
}

# Delete vx-<vni>-<net> devices whose overlay has no running container here.
repair_vxlan() {
    keep=$(kept_networks | grep . | sort -u)
    devs=$(vx_devices)
    stale=0
    while read -r ns dev; do
        [ -n "${dev:-}" ] || continue
        net=${dev##*-}
        where=""
        [ "$ns" = - ] || where=" in $ns"
        if echo "$keep" | grep -q "^$net"; then
            log "keeping $dev$where: overlay $net has running containers here (or is the ingress)"
            continue
        fi
        stale=$((stale + 1))
        if [ "$DRY" = 1 ]; then
            log "dry run: would delete stale $dev$where (overlay $net, no running containers here)"
        elif del_dev "$ns" "$dev"; then
            log "deleted stale $dev$where (overlay $net, no running containers here); docker recreates it"
        else
            log "could not delete $dev$where"
        fi
    done <<EOT
$devs
EOT
    [ "$stale" -gt 0 ] || log "no stale vx-* device found (every one belongs to an overlay in use here)"
}

clear_busy() { $DOCKER node update --label-rm "$LABEL_BUSY" "$(self_id)" >/dev/null 2>&1 || true; }

pre() {
    debs=$(cat)
    if [ "${SK_SWARM_GATE_SKIP:-0}" = 1 ]; then
        log "SK_SWARM_GATE_SKIP=1: gate bypassed by hand"
        exit 0
    fi
    echo "$debs" | grep -Eq "$ENGINE_RE" || exit 0
    if is_manager; then role=manager; elif is_member; then role=worker; else exit 0; fi
    engine=$(echo "$debs" | grep -E "$ENGINE_RE" | sed 's#.*/##' | tr '\n' ' ')

    if [ "$role" = manager ] && [ "$(manager_count)" -le 1 ] && [ "$SINGLE_MANAGER_ENGINE" != allow ]; then
        until_s=$(label_values "$LABEL_WINDOW" | awk -v me="$(self_id)" '$1 == me && $2 ~ /^[0-9]+$/ {print $2}')
        if [ -z "$until_s" ] || [ "$(now)" -ge "$until_s" ]; then
            log "REFUSED docker engine upgrade ($engine): single-manager swarm, restarting dockerd stops every service. Open a window first: docker node update --label-add $LABEL_WINDOW=<epoch until> <node> (or SK_SWARM_GATE_SKIP=1 for one manual run)"
            exit 1
        fi
        log "single-manager swarm: engine window open until $until_s"
    fi

    held=$(hold_reason "$role")
    if [ -n "$held" ]; then
        log "REFUSED docker engine upgrade ($engine): swarm is held ($(echo "$held" | tr '\n' ';')). Clear it once the swarm is healthy: sk-swarm-upgrade-gate clear-hold (on a manager)"
        exit 1
    fi

    base=""
    if [ "$role" = manager ]; then
        last=$(label_values "$LABEL_DONE" | awk '$2 ~ /^[0-9]+$/ {print $2}' | sort -n | tail -n 1)
        if [ -n "$last" ]; then
            since=$(( $(now) - last ))
            if [ "$since" -lt "$GAP_S" ]; then
                wait_s=$(( GAP_S - since ))
                log "last manager docker upgrade finished ${since}s ago: waiting ${wait_s}s for the ${GAP_S}s gap"
                pause "$wait_s"
            fi
        fi

        deadline=$(( $(now) + PRE_TIMEOUT_S ))
        me=$(self_id)
        while :; do
            busy=$(label_values "$LABEL_BUSY" | awk -v me="$me" -v t="$(now)" -v stale="$BUSY_STALE_S" \
                '$1 != me && $2 ~ /^[0-9]+$/ && t - $2 < stale {print $1}')
            [ -z "$busy" ] && break
            if [ "$DRY" = 1 ] || [ "$(now)" -ge "$deadline" ]; then
                log "REFUSED docker engine upgrade ($engine): another manager is still upgrading ($LABEL_BUSY on: $(echo "$busy" | tr '\n' ' '))"
                exit 1
            fi
            pause "$POLL_S"
        done

        while :; do
            problem=$(nodes_problem)
            [ -z "$problem" ] && break
            if [ "$DRY" = 1 ] || [ "$(now)" -ge "$deadline" ]; then
                log "REFUSED docker engine upgrade ($engine): swarm not healthy after ${PRE_TIMEOUT_S}s: $(echo "$problem" | tr '\n' ';')"
                exit 1
            fi
            pause "$POLL_S"
        done

        base=$(unconverged)
        [ -n "$base" ] && log "services already below desired replicas before the upgrade (not blamed on it): $(echo "$base" | tr '\n' ' ')"
    fi
    if [ "$DRY" = 1 ]; then
        log "dry run: would let the docker engine upgrade through ($engine, $role)"
        exit 0
    fi
    mkdir -p "$STATE_DIR"
    echo "$base" > "$STATE_DIR/baseline"
    echo "$(now) $role" > "$STATE_DIR/pending"
    if [ "$role" = manager ]; then
        $DOCKER node update --label-add "$LABEL_BUSY=$(now)" "$me" >/dev/null 2>&1 \
            || log "could not set $LABEL_BUSY on this node"
    fi
    log "letting the docker engine upgrade through ($engine, $role); post will wait for the swarm"
    exit 0
}

post() {
    if [ "$DRY" = 0 ]; then
        [ -e "$STATE_DIR/pending" ] || exit 0
        since=""
        role=""
        read -r since role _ < "$STATE_DIR/pending" || true
        rm -f "$STATE_DIR/pending"
        case "$since" in ''|*[!0-9]*) since=$(now) ;; esac
        [ "${role:-}" = worker ] || role=manager
    else
        since=$(( $(now) - BUSY_STALE_S ))
        if is_manager; then role=manager; else role=worker; fi
    fi
    base=$(cat "$STATE_DIR/baseline" 2>/dev/null || true)
    pause "$MIN_SETTLE_S"
    deadline=$(( $(now) + POST_TIMEOUT_S ))
    good=0
    problem=""
    while :; do
        if $DOCKER info >/dev/null 2>&1; then
            if [ "$role" = manager ]; then
                problem=$(nodes_problem)
                short=""
                for s in $(unconverged); do
                    echo "$base" | grep -qx "$s" || short="$short $s"
                done
                [ -n "$short" ] && problem="${problem:+$problem; }services below desired replicas:$short"
            else
                problem=$(self_problem)
            fi
            vx=$(vxlan_rejections "$since")
            if [ "${vx:-0}" -gt 0 ]; then
                problem="${problem:+$problem; }$vx task(s) rejected here with \"$VXLAN_ERR\" (stale overlay vxlan device)"
                log "$vx task(s) rejected with \"$VXLAN_ERR\": removing stale vx-* devices of overlays with no running containers here"
                repair_vxlan
                since=$(now)
            fi
        else
            problem="dockerd not answering"
        fi
        if [ -z "$problem" ]; then
            good=$((good + 1))
            [ "$good" -ge 2 ] && break
        else
            good=0
        fi
        if [ "$DRY" = 1 ]; then
            if [ -z "$problem" ]; then
                log "dry run: swarm converged now ($role)"
                exit 0
            fi
            log "dry run: not converged now ($role): $problem"
            exit 1
        fi
        if [ "$(now)" -ge "$deadline" ]; then
            stamp=$(set_hold "$role" "docker engine upgrade did not converge: $(echo "$problem" | tr '\n' ';')")
            [ "$role" = manager ] && clear_busy
            log "FAILED: swarm not back ${POST_TIMEOUT_S}s after the docker engine upgrade on $(hostname) ($role): $(echo "$problem" | tr '\n' ';'). Hold $stamp set: no swarm node upgrades docker until it is cleared (sk-swarm-upgrade-gate clear-hold on a manager)"
            exit 1
        fi
        pause "$POLL_S"
    done
    if [ "$role" = manager ]; then
        $DOCKER node update --label-add "$LABEL_DONE=$(now)" "$(self_id)" >/dev/null 2>&1 \
            || log "could not set $LABEL_DONE on this node"
        clear_busy
        log "swarm converged after the docker engine upgrade; next manager may go after ${GAP_S}s"
    else
        log "worker back in the swarm after the docker engine upgrade, no stale vxlan rejections"
    fi
    exit 0
}

clear_hold() {
    if [ -e "$SHARED_DIR/hold" ]; then
        log "clearing $SHARED_DIR/hold: $(head -n 1 "$SHARED_DIR/hold")"
        rm -f "$SHARED_DIR/hold"
    fi
    if is_manager; then
        label_values "$LABEL_HOLD" | awk '$2 != "" {print $1}' | while read -r n; do
            $DOCKER node update --label-rm "$LABEL_HOLD" "$n" >/dev/null 2>&1 \
                && log "cleared $LABEL_HOLD on $n" || log "could not clear $LABEL_HOLD on $n"
        done
    elif is_member; then
        log "not a manager: node labels $LABEL_HOLD (if any) must be cleared on a manager"
    fi
    exit 0
}

case "$MODE" in
    pre) pre ;;
    post) post ;;
    clear-hold) clear_hold ;;
    *) echo "usage: $0 pre|post [--dry-run] | clear-hold" >&2; exit 2 ;;
esac
