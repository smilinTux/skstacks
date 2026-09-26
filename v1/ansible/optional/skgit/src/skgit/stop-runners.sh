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

# Stop SKGit CI/CD Runners and DIND

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STACK_NAME=$(basename "$SCRIPT_DIR")
COMPOSE_FILE="$SCRIPT_DIR/../config/$STACK_NAME/skgit-runners.yml"

echo "=== Stopping SKGit Runners and DIND ==="
echo "Compose file: $COMPOSE_FILE"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "Warning: Compose file not found at $COMPOSE_FILE"
  echo "Attempting to stop containers by project name..."
  docker compose -p "$STACK_NAME-runners" down --remove-orphans 2>/dev/null || true

  echo "Attempting to stop by container name pattern..."
  docker ps --filter "name=skgit-.*-runner" --format "{{.ID}}" | xargs -r docker stop 2>/dev/null || true
  docker ps --filter "name=skgit-.*-dind" --format "{{.ID}}" | xargs -r docker stop 2>/dev/null || true

  echo "Cleanup complete."
  exit 0
fi

echo "Stopping runners and DIND with docker compose..."
docker compose -p "$STACK_NAME-runners" -f "$COMPOSE_FILE" down --remove-orphans

echo "SKGit runners and DIND stopped."
