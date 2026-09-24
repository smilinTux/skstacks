#!/usr/bin/env bash
# did-setup.sh — One-shot DID setup for an RKE2 node
#
# Generates all three DID document tiers from the local CapAuth profile and writes them
# to the standard locations.  Optionally configures Tailscale Serve so that
# ~/.skcomm/well-known/did.json is reachable at:
#   https://<hostname>.ts.net/.well-known/did.json
#
# Environment variables (all optional):
#   SKWORLD_HOSTNAME   Short Tailscale hostname (default: $(hostname -s))
#   SKWORLD_TAILNET    Tailnet magic-DNS suffix (default: autodetected via tailscale CLI)
#   CAPAUTH_BASE_DIR   CapAuth root (default: ~/.capauth)
#   SKCOMM_BASE_URL    Base URL of running SKComm API (default: http://127.0.0.1:9384)
#   SETUP_TAILSCALE_SERVE  Set to "true" to run tailscale serve (default: "false")
#
# Usage:
#   bash did-setup.sh
#   SETUP_TAILSCALE_SERVE=true bash did-setup.sh

set -euo pipefail

SKWORLD_HOSTNAME="${SKWORLD_HOSTNAME:-$(hostname -s)}"
SKWORLD_TAILNET="${SKWORLD_TAILNET:-}"
SKCOMM_BASE_URL="${SKCOMM_BASE_URL:-http://127.0.0.1:9384}"
CAPAUTH_BASE_DIR="${CAPAUTH_BASE_DIR:-$HOME/.capauth}"
SETUP_TAILSCALE_SERVE="${SETUP_TAILSCALE_SERVE:-false}"

SKCAPSTONE_HOME="${SKCAPSTONE_HOME:-$HOME/.skcapstone}"
SKCOMM_HOME="${SKCOMM_HOME:-$HOME/.skcomm}"

echo "=== Sovereign DID Setup ==="
echo "  Hostname:     $SKWORLD_HOSTNAME"
echo "  CapAuth dir:  $CAPAUTH_BASE_DIR"
echo "  SKComm URL:   $SKCOMM_BASE_URL"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Verify CapAuth profile exists
# ---------------------------------------------------------------------------
if [[ ! -f "$CAPAUTH_BASE_DIR/identity/profile.json" ]]; then
    echo "ERROR: No CapAuth profile found at $CAPAUTH_BASE_DIR"
    echo "       Run 'capauth init' first."
    exit 1
fi

echo "[1/4] CapAuth profile found."

# ---------------------------------------------------------------------------
# Step 2: Auto-detect Tailscale tailnet if not set
# ---------------------------------------------------------------------------
if [[ -z "$SKWORLD_TAILNET" ]] && command -v tailscale &>/dev/null; then
    SKWORLD_TAILNET=$(tailscale status --json 2>/dev/null \
        | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('MagicDNSSuffix',''))" 2>/dev/null || true)
fi

if [[ -n "$SKWORLD_TAILNET" ]]; then
    TS_FQDN="${SKWORLD_HOSTNAME}.${SKWORLD_TAILNET}"
    echo "[2/4] Tailscale tailnet: $SKWORLD_TAILNET  (FQDN: $TS_FQDN)"
else
    TS_FQDN=""
    echo "[2/4] Tailscale not detected — Tier 2 will fall back to did:key."
fi

# ---------------------------------------------------------------------------
# Step 3: Generate DID documents via Python
# ---------------------------------------------------------------------------
echo "[3/4] Generating DID documents..."

python3 - <<PYEOF
import sys, os, json
from pathlib import Path

# Inject env so capauth.did picks up the right directories.
os.environ.setdefault("SKCAPSTONE_HOME", "$SKCAPSTONE_HOME")
os.environ.setdefault("SKCOMM_HOME", "$SKCOMM_HOME")
os.environ["SKWORLD_HOSTNAME"] = "$SKWORLD_HOSTNAME"
os.environ["SKWORLD_TAILNET"] = "$SKWORLD_TAILNET"

try:
    from capauth.did import DIDDocumentGenerator, DIDTier
    from pathlib import Path as P

    base = P("$CAPAUTH_BASE_DIR")
    gen = DIDDocumentGenerator.from_profile(base_dir=base)
    docs = gen.generate_all(
        tailnet_hostname="$SKWORLD_HOSTNAME",
        tailnet_name="$SKWORLD_TAILNET",
    )

    # Write Tier 2 → ~/.skcomm/well-known/did.json
    wk = P("$SKCOMM_HOME") / "well-known"
    wk.mkdir(parents=True, exist_ok=True)
    (wk / "did.json").write_text(json.dumps(docs[DIDTier.WEB_MESH], indent=2))
    print(f"  Wrote {wk}/did.json")

    # Write all tiers → ~/.skcapstone/did/
    did_dir = P("$SKCAPSTONE_HOME") / "did"
    did_dir.mkdir(parents=True, exist_ok=True)
    (did_dir / "key.json").write_text(json.dumps(docs[DIDTier.KEY], indent=2))
    (did_dir / "public.json").write_text(json.dumps(docs[DIDTier.WEB_PUBLIC], indent=2))
    (did_dir / "did_key.txt").write_text(gen._ctx.did_key_id)
    print(f"  Wrote {did_dir}/key.json")
    print(f"  Wrote {did_dir}/public.json")
    print(f"  Wrote {did_dir}/did_key.txt")

    print(f"\n  did:key = {gen._ctx.did_key_id[:80]}...")
    print(f"  fingerprint = {gen._ctx.fingerprint}")

except ImportError as e:
    print(f"ERROR: capauth.did not importable: {e}", file=sys.stderr)
    print("       Install: pip install -e /path/to/capauth", file=sys.stderr)
    sys.exit(1)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
PYEOF

echo ""

# ---------------------------------------------------------------------------
# Step 4: Optionally configure Tailscale Serve
# ---------------------------------------------------------------------------
if [[ "$SETUP_TAILSCALE_SERVE" == "true" ]]; then
    if ! command -v tailscale &>/dev/null; then
        echo "[4/4] SETUP_TAILSCALE_SERVE=true but tailscale not found — skipping."
    else
        echo "[4/4] Configuring Tailscale Serve → https://443 → $SKCOMM_BASE_URL"
        tailscale serve --https=443 "$SKCOMM_BASE_URL" || {
            echo "      WARNING: tailscale serve failed (may already be configured)."
        }
        echo "      Tailscale Serve active."
        if [[ -n "$TS_FQDN" ]]; then
            echo "      DID endpoint: https://${TS_FQDN}/.well-known/did.json"
        fi
    fi
else
    echo "[4/4] Tailscale Serve not configured (set SETUP_TAILSCALE_SERVE=true to enable)."
    if [[ -n "$TS_FQDN" ]]; then
        echo "      To enable manually: tailscale serve --https=443 $SKCOMM_BASE_URL"
        echo "      Then: curl https://${TS_FQDN}/.well-known/did.json"
    fi
fi

echo ""
echo "=== DID setup complete ==="
echo ""
echo "Local verification:"
echo "  cat $SKCAPSTONE_HOME/did/did_key.txt"
echo "  curl -s $SKCOMM_BASE_URL/.well-known/did.json | python3 -m json.tool"
echo "  curl -s $SKCOMM_BASE_URL/api/v1/did/key"
