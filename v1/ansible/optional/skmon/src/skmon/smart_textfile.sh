#!/usr/bin/env bash
# Write SMART health and temperature as node_exporter textfile metrics.
# usage: smart_textfile.sh <out_dir>      (writes <out_dir>/smart.prom)
#   node_smart_healthy{device}              1 when `smartctl -H` reports PASSED, else 0
#   node_smart_temperature_celsius{device}  the drive temperature, when smartctl reports one
# Needs only bash, coreutils and smartctl. Without smartctl it writes nothing, says so on
# stderr and exits 0, so a timer on a host without smartmontools is not a failed unit.
# The file is written to a temp file in <out_dir> and renamed, so the collector never
# reads a half-written file.
set -u

out_dir="${1:-}"
if [ -z "$out_dir" ]; then
  echo "usage: smart_textfile.sh <out_dir>" >&2
  exit 2
fi
if ! command -v smartctl >/dev/null 2>&1; then
  echo "smart_textfile: smartctl not found, writing nothing" >&2
  exit 0
fi
mkdir -p "$out_dir"
tmp="$(mktemp "$out_dir/.smart.prom.XXXXXX")"
trap 'rm -f "$tmp"' EXIT

{
  echo "# HELP node_smart_healthy 1 when the drive's SMART overall health self-assessment is PASSED."
  echo "# TYPE node_smart_healthy gauge"
  echo "# HELP node_smart_temperature_celsius Drive temperature reported by SMART."
  echo "# TYPE node_smart_temperature_celsius gauge"
} >"$tmp"

health=""
temps=""
while read -r dev _; do
  [ -n "$dev" ] || continue
  name="${dev##*/}"
  # smartctl exits non-zero for a failing disk and for plain warnings, so its status is not used.
  if smartctl -H "$dev" 2>/dev/null | grep -q 'PASSED\|OK$'; then ok=1; else ok=0; fi
  health="${health}node_smart_healthy{device=\"${name}\"} ${ok}"$'\n'
  # ATA attribute table: id 194 (or 190), the raw value is the 10th column. NVMe: "Temperature: 35 Celsius".
  t="$(smartctl -A "$dev" 2>/dev/null | awk '
      $1 == 194 || $1 == 190 { print $10; exit }
      /^Temperature:/ { print $2; exit }
      /^Current Drive Temperature:/ { print $4; exit }')"
  case "$t" in
    ''|*[!0-9]*) ;;
    *) temps="${temps}node_smart_temperature_celsius{device=\"${name}\"} ${t}"$'\n' ;;
  esac
done < <(smartctl --scan 2>/dev/null)

printf '%s%s' "$health" "$temps" >>"$tmp"
chmod 0644 "$tmp"
mv "$tmp" "$out_dir/smart.prom"
trap - EXIT
