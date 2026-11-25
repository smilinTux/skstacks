#!/bin/bash

# Copyright (C) 2025 S&K Holding QT (Quantum Technologies)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Move the Swarm node labels listed in /etc/keepalived/skha_vip_labels (one
# per line, from skha.vip_node_labels) onto THIS node, then remove them from
# every other node. Run by skha_notify.sh when this node becomes MASTER, and
# by the deploy on the node that holds the VIP.
#
# A service constrained to such a label (skmail: node.labels.mail-vip ==
# true) then follows the VIP: Swarm rejects its task on the node that lost
# the label and starts it here. That matters for host-published ports, which
# only answer on the node running the task.
#
# Label added here first, removed elsewhere second, so there is never a
# moment when no node carries it. Needs a manager (docker node update);
# on a non-manager it logs and does nothing.

LABELS_FILE=/etc/keepalived/skha_vip_labels
log() { echo "$*"; logger -t skha-vip-labels "$*"; echo "$(date '+%Y-%m-%d %H:%M:%S') - $*" >> /var/log/keepalived-notify.log; }

[ -s "$LABELS_FILE" ] || exit 0
command -v docker >/dev/null 2>&1 || { log "docker not found, labels not moved"; exit 0; }

if [ "$(timeout 20 docker info --format '{{.Swarm.ControlAvailable}}' 2>/dev/null)" != "true" ]; then
    log "not a reachable swarm manager, labels not moved"
    exit 0
fi
self=$(timeout 20 docker info --format '{{.Swarm.NodeID}}')

rc=0
while IFS= read -r label; do
    label=${label%%#*}; label=${label//[[:space:]]/}
    [ -n "$label" ] || continue
    if ! timeout 30 docker node update --label-add "${label}=true" "$self" >/dev/null; then
        log "could not add ${label} to this node"; rc=1; continue
    fi
    for n in $(timeout 30 docker node ls -q --filter "node.label=${label}"); do
        [ "$n" = "$self" ] && continue
        if timeout 30 docker node update --label-rm "$label" "$n" >/dev/null; then
            log "moved ${label} from node ${n} to this node"
        else
            log "could not remove ${label} from node ${n}"; rc=1
        fi
    done
done < "$LABELS_FILE"
exit "$rc"
