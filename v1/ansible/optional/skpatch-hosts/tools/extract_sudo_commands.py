#!/usr/bin/env python3
"""Regenerate the fixture of every sudo command the PINNED dashboard image runs.

Usage (from the repo root, needs docker and network for the pull):

    python3 v1/ansible/optional/skpatch-hosts/tools/extract_sudo_commands.py
    python3 .../extract_sudo_commands.py --image <ref>   # try a candidate tag

It runs the image's OWN server code, not a scrape of it: a copy of
/app/dist/server/index.js cut just before the server starts, with
dump_sudo_commands.mjs appended, calls the app's buildCommandReference() (the
same function behind its "sudoers preview") once per package manager the app
knows. The container gets --network none, a throwaway encryption key and a
database in /tmp, so nothing leaves it and nothing persists.

The output (v1/tests/fixtures/skpatch_hosts_sudo_commands.json) is keyed to
the image reference. test_skpatch_hosts_sudo_coverage.py fails when the
pinned image (skp_default_image in the deploy playbooks) is not the one the
fixture came from, so a Renovate bump fails CI until someone reruns this,
reviews the diff and updates src/skpatch-hosts/sudo-coverage.yml and the
sudoers template to match.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROLE = Path(__file__).resolve().parents[1]
REPO_V1 = ROLE.parents[2]
FIXTURE = REPO_V1 / "tests" / "fixtures" / "skpatch_hosts_sudo_commands.json"
DUMP_JS = Path(__file__).with_name("dump_sudo_commands.mjs")
MARK = "@@SKP-JSON@@"

# Cut the bundle where it starts the scheduler and the HTTP server; everything
# above it only defines functions and opens the (throwaway) database.
IN_CONTAINER = r"""
set -eu
B=/app/dist/server/index.js
O=/app/dist/server/skp-dump.mjs
n=$(grep -n '^logger.info("Starting update scheduler");' "$B" | cut -d: -f1)
[ -n "$n" ] || { echo "startup marker not found in $B: update the cut point" >&2; exit 3; }
head -n $((n - 1)) "$B" > "$O"
cat >> "$O"
mkdir -p /tmp/skp
cd /tmp/skp
LUDASH_ENCRYPTION_KEY=$(node -e 'console.log(require("crypto").randomBytes(32).toString("base64"))')
export LUDASH_ENCRYPTION_KEY LUDASH_DB_PATH=/tmp/skp/dashboard.db DATA_DIR=/tmp/skp
node "$O"
"""


def pinned_image() -> str:
    """The image the prod deploy playbook pins (skp_default_image)."""
    text = (ROLE / "deploy_skpatch-hosts-prod.yml").read_text()
    m = re.search(r'^\s*skp_default_image:\s*"([^"]+)"', text, re.M)
    if not m:
        sys.exit("skp_default_image not found in deploy_skpatch-hosts-prod.yml")
    return m.group(1)


def extract(image: str) -> dict:
    """Run the app's buildCommandReference() inside `image` and return its output."""
    proc = subprocess.run(
        ["docker", "run", "--rm", "-i", "--network", "none", "--entrypoint", "sh", image, "-c", IN_CONTAINER],
        input=DUMP_JS.read_text(), capture_output=True, text=True, timeout=300,
    )
    line = next((l for l in proc.stdout.splitlines() if l.startswith(MARK)), None)
    if proc.returncode != 0 or line is None:
        sys.exit(f"extraction failed (rc {proc.returncode}):\n{proc.stderr[-2000:]}")
    return json.loads(line[len(MARK):])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--image", help="image reference (default: the pinned skp_default_image)")
    ap.add_argument("--out", type=Path, default=FIXTURE)
    args = ap.parse_args()
    image = args.image or pinned_image()
    subprocess.run(["docker", "pull", "-q", image], check=True, stdout=subprocess.DEVNULL)
    data = extract(image)
    doc = {
        "image": image,
        "generator": "v1/ansible/optional/skpatch-hosts/tools/extract_sudo_commands.py",
        **data,
    }
    args.out.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n")
    print(f"wrote {args.out} ({sum(len(v['rows']) for v in data['managers'].values())} manager rows, "
          f"{len(data['common'])} common, {len(data['warnings'])} warnings) for {image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
