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

# Keepalived notification script (SKHA)
# Logs VRRP state transitions for monitoring/debugging.

STATE=$1
TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')
HOSTNAME=$(hostname)

logger -t keepalived "VRRP state changed to ${STATE} on ${HOSTNAME} at ${TIMESTAMP}"
echo "${TIMESTAMP} - ${HOSTNAME} - VRRP state changed to ${STATE}" >> /var/log/keepalived-notify.log

exit 0
