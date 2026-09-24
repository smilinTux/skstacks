#!/usr/bin/env bash
# skwire one-line installer (Linux / macOS).  curl -fsSL <url>/install.sh | sh
# Strategy: prefer a native binary from the latest GitHub release; else the universal
# skwire.pyz (needs only Python 3.10+); else pip from a local checkout / PyPI.
set -euo pipefail
REPO="${SKWIRE_REPO:-smilinTux/skstacks}"
BIN_DIR="${SKWIRE_BIN:-$HOME/.local/bin}"
mkdir -p "$BIN_DIR"

os="$(uname -s)"; arch="$(uname -m)"
case "$os-$arch" in
  Linux-x86_64)  asset="skwire-linux-x64" ;;
  Darwin-arm64)  asset="skwire-macos-arm64" ;;
  Darwin-x86_64) asset="skwire-macos-x64" ;;
  *)             asset="" ;;
esac

dl() { curl -fsSL "$1" -o "$2"; }
latest="https://github.com/$REPO/releases/latest/download"

# 1) native binary (no Python needed)
if [ -n "$asset" ] && dl "$latest/$asset" "$BIN_DIR/skwire" 2>/dev/null; then
  chmod +x "$BIN_DIR/skwire"
  echo "✓ installed native binary → $BIN_DIR/skwire"
# 2) universal single-file zipapp (needs Python 3.10+)
elif command -v python3 >/dev/null 2>&1 && dl "$latest/skwire.pyz" "$BIN_DIR/skwire.pyz" 2>/dev/null; then
  printf '#!/usr/bin/env bash\nexec python3 "%s/skwire.pyz" "$@"\n' "$BIN_DIR" > "$BIN_DIR/skwire"
  chmod +x "$BIN_DIR/skwire" "$BIN_DIR/skwire.pyz"
  echo "✓ installed skwire.pyz (zero-dep, runs on system Python) → $BIN_DIR/skwire"
# 3) pip from a local checkout or PyPI
elif command -v python3 >/dev/null 2>&1; then
  if [ -f pyproject.toml ] && grep -q 'name = "skwire"' pyproject.toml 2>/dev/null; then
    python3 -m pip install --user .
  else
    python3 -m pip install --user skwire
  fi
  echo "✓ installed via pip"
else
  echo "Need either a supported OS release asset or Python 3.10+. See the README." >&2
  exit 1
fi

case ":$PATH:" in *":$BIN_DIR:"*) ;; *) echo "→ add to PATH:  export PATH=\"$BIN_DIR:\$PATH\"" ;; esac
echo "Try:  skwire            # opens the chat-style setup"
echo "      skwire scan       # just look at this box"
