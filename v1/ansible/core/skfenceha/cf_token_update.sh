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
# Operator utility: mints a scoped Cloudflare API token (DNS:Edit, all
# zones) from a Global API Key, for use as
# vault_skfenceha_cloudflare_dns_token. Run out of band; this script never
# touches the vault, it only prints the new token so you can paste it in.
#
# Requires CF_EMAIL and CF_GLOBAL_API_KEY in the environment.

set -e

if [[ -z "${CF_EMAIL:-}" || -z "${CF_GLOBAL_API_KEY:-}" ]]; then
  echo "CF_EMAIL and CF_GLOBAL_API_KEY environment variables must be set." >&2
  exit 1
fi

TOKEN_NAME="skfenceha-token-$(date +%s)"
API_URL="https://api.cloudflare.com/client/v4/user/api_tokens"

# DNS:Edit permission group id. Cloudflare's permission_groups ids are
# opaque and worth re-verifying before first use:
#   curl -s -H "X-Auth-Email: $CF_EMAIL" -H "X-Auth-Key: $CF_GLOBAL_API_KEY" \
#     https://api.cloudflare.com/client/v4/user/tokens/permission_groups \
#     | grep -B2 '"DNS Write"'
DNS_EDIT_PERMISSION_GROUP_ID="4755a26eedb94da69e1066d98aa820be"

TOKEN_PAYLOAD=$(cat <<EOF
{
  "name": "${TOKEN_NAME}",
  "policies": [
    {
      "effect": "allow",
      "resources": {"com.cloudflare.api.account.zone.*": "*"},
      "permission_groups": [
        {"id": "${DNS_EDIT_PERMISSION_GROUP_ID}"}
      ]
    }
  ]
}
EOF
)

RESPONSE=$(curl -s -X POST "$API_URL" \
    -H "X-Auth-Email: $CF_EMAIL" \
    -H "X-Auth-Key: $CF_GLOBAL_API_KEY" \
    -H "Content-Type: application/json" \
    --data "$TOKEN_PAYLOAD")

if ! echo "$RESPONSE" | grep -q '"success":true'; then
  echo "Failed to create API token: $RESPONSE" >&2
  exit 1
fi

NEW_TOKEN=$(echo "$RESPONSE" | grep -o '"value":"[^"]*"' | head -1 | cut -d '"' -f4)

echo "New scoped Cloudflare DNS:Edit token minted: ${TOKEN_NAME}"
echo "Set in the instance vault: vault_skfenceha_cloudflare_dns_token: \"${NEW_TOKEN}\""
