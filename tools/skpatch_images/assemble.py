#!/usr/bin/env python3
"""Merge patch_one.sh results into skpatch-patched-images.json and print a
Markdown summary (for $GITHUB_STEP_SUMMARY).

  assemble.py <results-dir> <out.json>
"""
import json
import sys
from pathlib import Path


def main():
    rows = []
    for p in sorted(Path(sys.argv[1]).rglob("*.json")):
        r = json.loads(p.read_text())
        for k in ("before", "after"):
            if isinstance(r.get(k), str):
                r[k] = json.loads(r[k])
        rows.append(r)
    mapping = {r["ref"]: {"image": r["image"], "before": r["before"], "after": r["after"]}
               for r in rows if r.get("status") == "patched"}
    Path(sys.argv[2]).write_text(json.dumps(dict(sorted(mapping.items())), indent=2) + "\n")
    by = {}
    for r in rows:
        by.setdefault(r["status"], []).append(r)
    print("## skpatch patched images\n")
    print(" | ".join(f"{s}: {len(v)}" for s, v in sorted(by.items())) + "\n")
    print("| upstream | critical | high | patched critical | patched high |\n|---|---|---|---|---|")
    for r in sorted(by.get("patched", []), key=lambda r: -(r["before"]["critical"] * 100 + r["before"]["high"])):
        print(f'| `{r["ref"].split("@")[0]}` | {r["before"]["critical"]} | {r["before"]["high"]} | {r["after"]["critical"]} | {r["after"]["high"]} |')
    bad = [r for r in rows if r["status"] not in ("patched", "nothing-to-patch")]
    if bad:
        print("\n### Not patched\n")
        for r in bad:
            print(f'- `{r["ref"]}`: {r["status"]}')


if __name__ == "__main__":
    main()
