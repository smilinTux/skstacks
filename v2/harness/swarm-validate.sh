#!/usr/bin/env bash
# skstacks deploy harness (Swarm side) — validate skrender's compose output with the
# real Docker engine. `docker compose config` parses + schema-checks each rendered
# stack (services, networks, volumes, deploy keys). Complements k3d-validate.sh.
#
# Usage:  bash swarm-validate.sh [/path/to/v2]
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
V2="${1:-$(cd "$HERE/.." && pwd)}"
PY="${PYTHON:-python3}"
# auto-discover every descriptor that declares a deploy: block
mapfile -t SERVICES < <(cd "$V2" && "$PY" -c "import glob,yaml; [print(f.replace('/app.yaml','')) for f in sorted(glob.glob('*/*/app.yaml')) if (yaml.safe_load(open(f)) or {}).get('deploy')]")

log(){ printf '\033[1;36m[harness]\033[0m %s\n' "$*"; }
ok(){  printf '  \033[1;32m✓\033[0m %s\n' "$*"; }
bad(){ printf '  \033[1;31m✗\033[0m %s\n' "$*"; }

fails=0
for s in "${SERVICES[@]}"; do
  name="$(basename "$s")"
  log "render + validate (swarm): $name"
  if ! PYTHONPATH="$V2" "$PY" -m skrender.cli "$V2/$s" --platform swarm > "/tmp/${name}-swarm.yml" 2>/tmp/${name}-swerr; then
    bad "$name: skrender failed: $(cat /tmp/${name}-swerr)"; fails=$((fails+1)); continue
  fi
  # `docker compose config` validates structure; -q = quiet, non-zero on error.
  if docker compose -f "/tmp/${name}-swarm.yml" config -q >/tmp/${name}-cfg 2>&1; then
    img="$(PYTHONPATH="$V2" "$PY" -c "import yaml;d=yaml.safe_load(open('/tmp/${name}-swarm.yml'));print(next(iter(d['services'].values()))['image'])")"
    ok "$name: valid compose (image=$img)"
  else
    bad "$name: docker rejected the compose:"; sed 's/^/      /' /tmp/${name}-cfg; fails=$((fails+1))
  fi
done

echo
[ "$fails" -eq 0 ] && log "✅ all ${#SERVICES[@]} swarm stacks valid (real Docker engine)." || { log "❌ $fails failed."; exit 1; }
