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

# Start SKGit CI/CD Runners with Docker-in-Docker
# Based on Forgejo official documentation:
# https://forgejo.org/docs/latest/admin/actions/docker-access/

set -e

# ============================================================================
# IMPORTANT: DIND must run on exactly ONE node.
# The dind-data volume uses local storage (root FS) and must not be
# replicated; only one DIND instance should provide Docker-in-Docker for
# CI/CD. This is a per-instance choice, so it is REQUIRED (no default) -
# a published framework must fail closed rather than assume a hostname
# that happens to be right for one deployment.
# ============================================================================
CURRENT_HOSTNAME=$(hostname)
ALLOWED_HOSTNAME="${SKGIT_DIND_NODE:-}"

if [ -z "$ALLOWED_HOSTNAME" ]; then
  echo "SKGIT_DIND_NODE is not set - skipping runner startup on $CURRENT_HOSTNAME."
  echo "Set SKGIT_DIND_NODE to the single node that should run DIND + runners,"
  echo "for example: SKGIT_DIND_NODE=$CURRENT_HOSTNAME $0"
  exit 0
fi

if [ "$CURRENT_HOSTNAME" != "$ALLOWED_HOSTNAME" ]; then
  echo "Skipping runner startup on $CURRENT_HOSTNAME"
  echo "   Runners are configured to run ONLY on $ALLOWED_HOSTNAME"
  echo "   Current node: $CURRENT_HOSTNAME"
  echo ""
  echo "   This prevents multiple DIND instances from running simultaneously."
  exit 0
fi

echo "Running on designated node: $CURRENT_HOSTNAME"

# Resolve the directory where this script is located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Determine stack name from directory name (e.g., skgit-prod)
STACK_NAME=$(basename "$SCRIPT_DIR")

# Construct path to config file
# Script is in /var/data/skgit-<env>/
# Config is in /var/data/config/skgit-<env>/skgit-runners.yml
COMPOSE_FILE="$SCRIPT_DIR/../config/$STACK_NAME/skgit-runners.yml"
CONFIG_FILE="$SCRIPT_DIR/../config/$STACK_NAME/runner-config.yml"

echo "=== Starting SKGit Runners with DIND ==="
echo "Script directory: $SCRIPT_DIR"
echo "Stack name: $STACK_NAME"
echo "Compose file: $COMPOSE_FILE"
echo "Runner config: $CONFIG_FILE"

if [ ! -f "$COMPOSE_FILE" ]; then
  echo "Error: Compose file not found at $COMPOSE_FILE"
  exit 1
fi

if [ ! -f "$CONFIG_FILE" ]; then
  echo "Warning: Runner config not found at $CONFIG_FILE"
  echo "Runner may use default configuration"
fi

# Determine env file location
ENV_FILE="$SCRIPT_DIR/../config/$STACK_NAME/.env"
if [ -f "$ENV_FILE" ]; then
  echo "Loading .env from $ENV_FILE..."
  set -a
  source "$ENV_FILE"
  set +a
fi

# Ensure runner data directory exists with correct permissions
RUNNER_DATA="/var/data/runtime/$STACK_NAME/runner-data"
if [ ! -d "$RUNNER_DATA" ]; then
  echo "Creating runner data directory: $RUNNER_DATA"
  mkdir -p "$RUNNER_DATA"
  chown 1000:1000 "$RUNNER_DATA"
fi

# Ensure DIND data directory exists
DIND_DATA="/var/data/runtime/$STACK_NAME/dind-data"
if [ ! -d "$DIND_DATA" ]; then
  echo "Creating DIND data directory: $DIND_DATA"
  mkdir -p "$DIND_DATA"
fi

# Stop any existing runners first (clean slate)
echo "Stopping any existing runners..."
docker compose -p "$STACK_NAME-runners" -f "$COMPOSE_FILE" down --remove-orphans 2>/dev/null || true

# Start runners with DIND
echo "Starting DIND and Runner containers..."
docker compose -p "$STACK_NAME-runners" -f "$COMPOSE_FILE" up -d --remove-orphans

# Wait for DIND to be healthy
echo "Waiting for DIND to be healthy..."
for i in {1..30}; do
  if docker exec "skgit-${STACK_NAME##*-}-dind" docker info >/dev/null 2>&1; then
    echo "DIND is healthy."
    break
  fi
  echo "Waiting for DIND... (attempt $i/30)"
  sleep 2
done

echo ""
echo "=== Container Status ==="
docker compose -p "$STACK_NAME-runners" -f "$COMPOSE_FILE" ps

echo ""
echo "=== Runner Logs (last 20 lines) ==="
docker compose -p "$STACK_NAME-runners" -f "$COMPOSE_FILE" logs --tail=20 forgejo-runner

echo ""
echo "SKGit runners started successfully with DIND."
echo ""
echo "To view logs: docker compose -p $STACK_NAME-runners -f $COMPOSE_FILE logs -f"
echo "To stop: $SCRIPT_DIR/stop-runners.sh"
