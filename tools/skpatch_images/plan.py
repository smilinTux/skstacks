#!/usr/bin/env python3
"""Print the image references skpatch should try to patch, as a JSON list.

Default: every image a v1 compose template pins (v1/scripts/list_images.py),
minus unresolved ones and our own builds (ghcr.io/smilintux/*, which are
fixed at source). An explicit newline-separated list (workflow_dispatch
input) replaces the default.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def template_images():
    out = subprocess.run([sys.executable, str(ROOT / "v1/scripts/list_images.py"), str(ROOT)],
                         capture_output=True, text=True, check=True).stdout
    refs = []
    for line in out.splitlines():
        _, ref = line.split("\t", 1)
        if "<unresolved>" in ref or ref.startswith("ghcr.io/smilintux/"):
            continue
        refs.append(ref)
    return sorted(set(refs))


def main():
    explicit = [r.strip() for r in (sys.argv[1] if len(sys.argv) > 1 else "").splitlines() if r.strip()]
    print(json.dumps(sorted(set(explicit)) if explicit else template_images()))


if __name__ == "__main__":
    main()
