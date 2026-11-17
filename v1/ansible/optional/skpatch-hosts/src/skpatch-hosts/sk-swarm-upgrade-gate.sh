#!/bin/sh
# sk-swarm-upgrade-gate: APT hook that makes Docker engine upgrades on swarm
# managers truly one at a time. Installed by skstacks skpatch-hosts
# (provision_skpatch_hosts_nodes.yml) on swarm managers only, wired as:
#   DPkg::Pre-Install-Pkgs { "/usr/local/sbin/sk-swarm-upgrade-gate pre"; };
#   DPkg::Post-Invoke      { "/usr/local/sbin/sk-swarm-upgrade-gate post"; };
#
# Why: the skpatch-hosts dashboard runs a schedule's upgrades one host after
# another, but it only waits for apt to exit. A docker-ce or containerd.io
# upgrade restarts dockerd, and the swarm needs minutes to reschedule that
# node's tasks, so three managers in one schedule restarted dockerd within
# about 90 seconds of each other. This hook holds the next manager back.
#
# It acts ONLY when the batch apt is about to install contains docker-ce or
# containerd.io, and only on a swarm manager. Every other upgrade passes
# straight through.
#
#   pre  (before dpkg installs the batch):
#     1. refuse if any node carries the label sk.skpatch.hold (set by a failed
#        post below; clear it by hand once the swarm is healthy:
#        docker node update --label-rm sk.skpatch.hold <node>)
#     2. wait until GAP_S has passed since the last manager finished its
#        docker upgrade (label sk.skpatch.docker-upgraded-at, epoch seconds)
#     3. wait (at most PRE_TIMEOUT_S) until every manager is Ready and
#        Leader/Reachable and this node is Ready/Active; refuse on timeout
#     4. record which services are already short of their desired replicas
#        (a pre-existing failure is reported, not blamed on this upgrade)
#   post (after dpkg, only if pre let a docker upgrade through):
#     wait MIN_SETTLE_S, then (at most POST_TIMEOUT_S) until this node is
#     Ready/Active, every manager Ready and Leader/Reachable, and every
#     replicated or global service not in the pre baseline is at its desired
#     replicas, on two polls in a row. Success labels this node
#     sk.skpatch.docker-upgraded-at=<now>. Timeout labels it sk.skpatch.hold
#     and fails, so apt reports an error and every later manager's pre refuses.
#
# A refusing pre makes apt abort the whole run (nothing is installed); the
# dashboard records the upgrade as failed and the host is retried next window.
#
# Knobs (/etc/default/sk-swarm-upgrade-gate, written by the role):
#   GAP_S=600 PRE_TIMEOUT_S=900 POST_TIMEOUT_S=900 MIN_SETTLE_S=60 POLL_S=15
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
STATE_DIR=${STATE_DIR:-/run/sk-swarm-upgrade-gate}
DOCKER=${DOCKER:-docker}

LABEL_HOLD=sk.skpatch.hold
LABEL_DONE=sk.skpatch.docker-upgraded-at
ENGINE_RE='(^|/)(docker-ce|containerd\.io)_[^/]*\.deb$'

log() {
    logger -t sk-swarm-upgrade-gate -- "$*" 2>/dev/null || true
    echo "sk-swarm-upgrade-gate: $*" >&2
}
now() { date +%s; }
pause() { [ "$DRY" = 1 ] || sleep "$1"; }

is_manager() { [ "$($DOCKER info --format '{{.Swarm.ControlAvailable}}' 2>/dev/null)" = "true" ]; }
self_id() { $DOCKER info --format '{{.Swarm.NodeID}}' 2>/dev/null; }

# Value of a node label on every node, one per line ("" when unset).
label_values() {
    for n in $($DOCKER node ls -q 2>/dev/null); do
        v=$($DOCKER node inspect --format "{{index .Spec.Labels \"$1\"}}" "$n" 2>/dev/null)
        [ "$v" = "<no value>" ] && v=""
        printf '%s %s\n' "$n" "$v"
    done
}

# Prints why the nodes are not healthy; prints nothing when they are.
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

# Names of replicated/global services below their desired replicas.
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

pre() {
    debs=$(cat)
    if [ "${SK_SWARM_GATE_SKIP:-0}" = 1 ]; then
        log "SK_SWARM_GATE_SKIP=1: gate bypassed by hand"
        exit 0
    fi
    echo "$debs" | grep -Eq "$ENGINE_RE" || exit 0
    is_manager || exit 0
    engine=$(echo "$debs" | grep -E "$ENGINE_RE" | sed 's#.*/##' | tr '\n' ' ')

    held=$(label_values "$LABEL_HOLD" | awk '$2 != ""')
    if [ -n "$held" ]; then
        log "REFUSED docker engine upgrade ($engine): swarm is held ($LABEL_HOLD on: $(echo "$held" | tr '\n' ' ')). Clear it once the swarm is healthy: docker node update --label-rm $LABEL_HOLD <node>"
        exit 1
    fi

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
    if [ "$DRY" = 1 ]; then
        log "dry run: would let the docker engine upgrade through ($engine)"
        exit 0
    fi
    mkdir -p "$STATE_DIR"
    echo "$base" > "$STATE_DIR/baseline"
    : > "$STATE_DIR/pending"
    log "letting the docker engine upgrade through ($engine); post will wait for the swarm"
    exit 0
}

post() {
    if [ "$DRY" = 0 ]; then
        [ -e "$STATE_DIR/pending" ] || exit 0
        rm -f "$STATE_DIR/pending"
    fi
    base=$(cat "$STATE_DIR/baseline" 2>/dev/null || true)
    pause "$MIN_SETTLE_S"
    deadline=$(( $(now) + POST_TIMEOUT_S ))
    good=0
    problem=""
    while :; do
        if $DOCKER info >/dev/null 2>&1; then
            problem=$(nodes_problem)
            short=""
            for s in $(unconverged); do
                echo "$base" | grep -qx "$s" || short="$short $s"
            done
            [ -n "$short" ] && problem="${problem:+$problem; }services below desired replicas:$short"
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
                log "dry run: swarm converged now"
                exit 0
            fi
            log "dry run: not converged now: $problem"
            exit 1
        fi
        if [ "$(now)" -ge "$deadline" ]; then
            stamp="$(date -u +%Y%m%dT%H%M%SZ)-$(hostname)"
            $DOCKER node update --label-add "$LABEL_HOLD=$stamp" "$(self_id)" >/dev/null 2>&1 \
                || log "could not set $LABEL_HOLD on this node"
            log "FAILED: swarm not back ${POST_TIMEOUT_S}s after the docker engine upgrade: $(echo "$problem" | tr '\n' ';'). Set $LABEL_HOLD=$stamp: no further manager upgrades docker until it is cleared"
            exit 1
        fi
        pause "$POLL_S"
    done
    $DOCKER node update --label-add "$LABEL_DONE=$(now)" "$(self_id)" >/dev/null 2>&1 \
        || log "could not set $LABEL_DONE on this node"
    log "swarm converged after the docker engine upgrade; next manager may go after ${GAP_S}s"
    exit 0
}

case "$MODE" in
    pre) pre ;;
    post) post ;;
    *) echo "usage: $0 pre|post [--dry-run]" >&2; exit 2 ;;
esac
