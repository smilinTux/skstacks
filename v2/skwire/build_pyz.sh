#!/usr/bin/env bash
# Build skwire.pyz — a single-file, zero-dep executable that runs on Linux AND macOS
# (anywhere with Python 3.10+). `chmod +x skwire.pyz && ./skwire.pyz`  — no install.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"          # the skwire/ package dir
OUT="${1:-$HERE/dist/skwire.pyz}"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# stage a clean copy of the package (no tests / caches / build cruft)
mkdir -p "$STAGE/skwire"
( cd "$HERE" && tar --exclude='tests' --exclude='__pycache__' --exclude='.pytest_cache' \
    --exclude='dist' --exclude='examples' --exclude='*.pyz' -cf - . ) | tar -xf - -C "$STAGE/skwire"

mkdir -p "$(dirname "$OUT")"
python3 -m zipapp "$STAGE" -m "skwire.cli:main" -p "/usr/bin/env python3" -o "$OUT"
chmod +x "$OUT"
echo "✓ built $OUT  ($(du -h "$OUT" | cut -f1))"
echo "  run it:  $OUT scan   |   $OUT serve   |   $OUT model setup"
