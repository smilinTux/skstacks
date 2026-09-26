#!/bin/bash
# Copyright (C) 2025 S&K Holding QT (Quantum Technologies)
#
# Script to automatically discover Traefik frontend URLs from Docker Swarm services
# and add them as monitors to Uptime-Kuma
#
# Usage:
#   ./discover-traefik-frontends.sh [--env prod|staging|dev] [--dry-run] [--api-token TOKEN] [--base-url URL]

set -e

# Default values
ENV="${ENV:-prod}"
DRY_RUN=false
API_TOKEN="${UPTIME_KUMA_API_TOKEN:-}"
BASE_URL="${UPTIME_KUMA_BASE_URL:-http://localhost:3001}"
SKIP_EXISTING=true

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --env)
            ENV="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --api-token)
            API_TOKEN="$2"
            shift 2
            ;;
        --base-url)
            BASE_URL="$2"
            shift 2
            ;;
        --skip-existing)
            SKIP_EXISTING=true
            shift
            ;;
        --no-skip-existing)
            SKIP_EXISTING=false
            shift
            ;;
        *)
            echo -e "${RED}Unknown option: $1${NC}"
            echo "Usage: $0 [--env prod|staging|dev] [--dry-run] [--api-token TOKEN] [--base-url URL]"
            exit 1
            ;;
    esac
done

echo -e "${GREEN}🔍 Discovering Traefik frontend URLs for environment: ${ENV}${NC}"
echo -e "${BLUE}Base URL: ${BASE_URL}${NC}"
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}⚠️  DRY RUN MODE - No changes will be made${NC}"
fi

# Check if jq is available
if ! command -v jq &> /dev/null; then
    echo -e "${RED}❌ Error: jq is required but not installed${NC}"
    echo "Install with: apt-get install jq"
    exit 1
fi

# Function to extract FQDN from Host rule
extract_fqdn() {
    local rule="$1"
    # Extract content between Host(` and `)
    echo "$rule" | sed -n "s/.*Host(\`\([^`]*\)\`).*/\1/p"
}

# Function to determine if HTTPS should be used
is_https() {
    local entrypoint="$1"
    # Check if entrypoint contains 'secure' or 'websecure'
    if [[ "$entrypoint" == *"secure"* ]] || [[ "$entrypoint" == *"websecure"* ]]; then
        return 0
    fi
    return 1
}

# Function to get existing monitors from Uptime-Kuma
get_existing_monitors() {
    if [ -z "$API_TOKEN" ]; then
        echo -e "${YELLOW}⚠️  No API token provided, skipping existing monitor check${NC}"
        return
    fi
    
    local response=$(curl -s -X GET \
        -H "Authorization: Bearer ${API_TOKEN}" \
        "${BASE_URL}/api/monitors" 2>/dev/null || echo "[]")
    
    if [ "$response" != "[]" ] && echo "$response" | jq -e . >/dev/null 2>&1; then
        echo "$response" | jq -r '.[] | "\(.name)|\(.url)"'
    fi
}

# Function to add monitor to Uptime-Kuma
add_monitor() {
    local name="$1"
    local url="$2"
    local monitor_type="${3:-http}"
    
    if [ "$DRY_RUN" = true ]; then
        echo -e "${YELLOW}  [DRY RUN] Would add monitor: ${name} -> ${url} (${monitor_type})${NC}"
        return 0
    fi
    
    if [ -z "$API_TOKEN" ]; then
        echo -e "${RED}  ❌ Error: API token required to add monitors${NC}"
        return 1
    fi
    
    # Check if monitor already exists
    if [ "$SKIP_EXISTING" = true ]; then
        local existing=$(get_existing_monitors | grep -c "^${name}|" || true)
        if [ "$existing" -gt 0 ]; then
            echo -e "${YELLOW}  ⏭️  Skipping existing monitor: ${name}${NC}"
            return 0
        fi
    fi
    
    # Create monitor payload
    local payload=$(jq -n \
        --arg name "$name" \
        --arg url "$url" \
        --arg type "$monitor_type" \
        '{
            name: $name,
            url: $url,
            type: $type,
            interval: 60,
            retryInterval: 60,
            maxretries: 3,
            upsidedown: false,
            notificationIDList: []
        }')
    
    local response=$(curl -s -w "\n%{http_code}" -X POST \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer ${API_TOKEN}" \
        -d "$payload" \
        "${BASE_URL}/api/monitors" 2>/dev/null || echo "ERROR")
    
    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | head -n-1)
    
    if [ "$http_code" = "200" ] || [ "$http_code" = "201" ]; then
        echo -e "${GREEN}  ✅ Added monitor: ${name} -> ${url}${NC}"
        return 0
    else
        echo -e "${RED}  ❌ Failed to add monitor: ${name} (HTTP ${http_code})${NC}"
        echo -e "${RED}     Response: ${body}${NC}"
        return 1
    fi
}

# Discover services with Traefik labels
echo -e "${BLUE}📋 Discovering Docker Swarm services with Traefik labels...${NC}"

# Get all services
services=$(docker service ls --format "{{.Name}}" 2>/dev/null || echo "")

if [ -z "$services" ]; then
    echo -e "${RED}❌ Error: Could not list Docker services. Are you running this on a Swarm manager?${NC}"
    exit 1
fi

discovered_count=0
added_count=0
skipped_count=0

# Process each service
while IFS= read -r service_name; do
    # Filter by environment if specified
    if [ "$ENV" != "prod" ]; then
        if [[ ! "$service_name" == *"-${ENV}"* ]] && [[ ! "$service_name" == *"_${ENV}_"* ]]; then
            continue
        fi
    else
        # For prod, exclude staging and dev services
        if [[ "$service_name" == *"-staging"* ]] || [[ "$service_name" == *"-dev"* ]] || \
           [[ "$service_name" == *"_staging_"* ]] || [[ "$service_name" == *"_dev_"* ]]; then
            continue
        fi
    fi
    
    # Get service labels
    labels=$(docker service inspect "$service_name" --format '{{json .Spec.Labels}}' 2>/dev/null || echo "{}")
    
    # Check if Traefik is enabled
    traefik_enabled=$(echo "$labels" | jq -r '.["traefik.enable"] // empty')
    if [ "$traefik_enabled" != "true" ]; then
        continue
    fi
    
    # Extract all router rules
    router_rules=$(echo "$labels" | jq -r 'to_entries | map(select(.key | startswith("traefik.http.routers.") and (.key | contains(".rule")))) | .[].value' 2>/dev/null || echo "")
    
    if [ -z "$router_rules" ]; then
        continue
    fi
    
    # Process each router rule
    while IFS= read -r rule; do
        if [ -z "$rule" ]; then
            continue
        fi
        
        # Extract FQDN from Host rule
        fqdn=$(extract_fqdn "$rule")
        if [ -z "$fqdn" ]; then
            continue
        fi
        
        # Get entrypoint to determine HTTP vs HTTPS
        router_name=$(echo "$labels" | jq -r 'to_entries | map(select(.value == "'"$rule"'")) | .[0].key' | sed 's/\.rule$//' 2>/dev/null || echo "")
        if [ -n "$router_name" ]; then
            entrypoint=$(echo "$labels" | jq -r '.["'${router_name}.entrypoints'"] // empty' 2>/dev/null || echo "")
        else
            entrypoint=""
        fi
        
        # Determine protocol
        if is_https "$entrypoint"; then
            url="https://${fqdn}"
            monitor_type="http"
        else
            url="http://${fqdn}"
            monitor_type="http"
        fi
        
        # Create monitor name from service and FQDN
        monitor_name="${service_name}: ${fqdn}"
        
        discovered_count=$((discovered_count + 1))
        echo -e "${BLUE}  📍 Discovered: ${monitor_name} -> ${url}${NC}"
        
        # Add monitor
        if add_monitor "$monitor_name" "$url" "$monitor_type"; then
            added_count=$((added_count + 1))
        else
            skipped_count=$((skipped_count + 1))
        fi
        
    done <<< "$router_rules"
    
done <<< "$services"

echo ""
echo -e "${GREEN}✨ Discovery complete!${NC}"
echo -e "${BLUE}   Discovered: ${discovered_count} frontends${NC}"
echo -e "${GREEN}   Added: ${added_count} monitors${NC}"
if [ $skipped_count -gt 0 ]; then
    echo -e "${YELLOW}   Skipped: ${skipped_count} monitors${NC}"
fi

if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}⚠️  This was a dry run. Run without --dry-run to actually add monitors.${NC}"
fi


