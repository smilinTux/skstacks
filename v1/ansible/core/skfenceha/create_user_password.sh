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
#
# Operator utility: generates the pre-hashed dashboard credential skfenceha
# expects in the vault (skfenceha.DASHBOARD_AUTH_HASH). Run this OUT OF BAND
# (not by the playbook - the playbook never mutates the vault) and paste the
# resulting htpasswd: line's value into the instance vault yourself.
#
# Requires htpasswd (apache2-utils / httpd-tools).

set -e

command -v htpasswd >/dev/null 2>&1 || {
  echo "htpasswd not found. Install apache2-utils (Debian/Ubuntu) or httpd-tools (RHEL/Fedora)." >&2
  exit 1
}

USERNAME="${1:-admin$(tr -dc 'a-z0-9' </dev/urandom | head -c 4)}"
PASSWORD="${2:-$(tr -dc 'A-Za-z0-9' </dev/urandom | head -c 16)}"

HASHED_PASSWORD=$(htpasswd -nbBC 10 "$USERNAME" "$PASSWORD" | cut -d: -f2)

echo "username:$USERNAME"
echo "password:$PASSWORD"
echo "htpasswd:${USERNAME}:${HASHED_PASSWORD}"
echo
echo "Set in the instance vault: vault_skfenceha_dashboard_auth_hash: \"${USERNAME}:${HASHED_PASSWORD}\""
