#!/bin/bash
# Make Nextcloud (SKHub) match the NAM brand. occ only, no stack redeploy.
# Usage: nextcloud-theming.sh [stack] [sso-media-dir]
# Run on the swarm node that hosts the <stack>_nextcloud container; the media
# dir is the NFS path the role filled (shared, so every node sees it).
# Rollback: occ theming:config <key> "" for name, slogan, color, logo, logoheader, favicon, background.
set -euo pipefail
STACK="${1:-skhub-prod}"
SRC="${2:-/var/data/sksso-prod/media/public/nam}"
C=$(docker ps -q -f "name=${STACK}_nextcloud" | head -1)
[ -n "$C" ] || { echo "no ${STACK}_nextcloud container on this node" >&2; exit 1; }
for f in nam-leaf-white.svg nam-mark.svg nam-background.jpg; do
  docker cp "$SRC/$f" "$C:/tmp/$f"
done
occ() { docker exec -u www-data "$C" php occ "$@"; }
occ theming:config name "NAM - Native Asset Management"
occ theming:config slogan "Asset sovereignty, digitally empowered."
occ theming:config color "#00B4A0"
occ theming:config logo /tmp/nam-leaf-white.svg
occ theming:config logoheader /tmp/nam-leaf-white.svg
occ theming:config favicon /tmp/nam-mark.svg
occ theming:config background /tmp/nam-background.jpg
