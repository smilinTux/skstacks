#!/usr/bin/env python3
"""Fail when a change adds CHANGELOG.md lines under a release that is already tagged.

A PR's release notes belong in the topmost section (`## Unreleased` or the
release being cut), and that section must not name a release that is already
tagged: after a tag, new notes start a fresh `## Unreleased` on top. Twice in v2.26.30, notes landed under an older heading
(#474 under v2.26.29, #410 under v2.26.27): that heading's tag never shipped
them, and the gate reads the topmost heading. Deleting or moving old lines is
fine (a release-notes PR does that); only additions are checked.

Usage: changelog_placement.py <base-ref> [file]   (exit 1 on a misplaced line)
"""
import re
import subprocess
import sys

HEADING = re.compile(r"^## (\S+)")
HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(base, path):
    """New-file line numbers of every added line, from `git diff base...HEAD`."""
    diff = subprocess.run(
        ["git", "diff", "--unified=0", f"{base}...HEAD", "--", path],
        check=True, capture_output=True, text=True,
    ).stdout
    out, line = [], 0
    for row in diff.splitlines():
        m = HUNK.match(row)
        if m:
            line = int(m.group(1))
        elif row.startswith("+") and not row.startswith("+++"):
            out.append(line)
            line += 1
    return out


def tag_exists(name):
    return subprocess.run(["git", "rev-parse", "-q", "--verify", f"refs/tags/{name}"],
                          capture_output=True).returncode == 0


def misplaced(text, added, tagged=tag_exists):
    """(line, heading) for each added non-blank line outside an untagged top section."""
    lines = text.splitlines()
    section = {}  # line number -> index of the `## ` section it sits in (0 = top)
    idx, heading, names = -1, None, {}
    for n, row in enumerate(lines, 1):
        m = HEADING.match(row)
        if m:
            idx += 1
            heading = row[3:].strip()
            names[idx] = heading
        section[n] = idx
    top_tagged = 0 in names and tagged(names[0].split()[0])
    bad = []
    for n in added:
        sec = section.get(n, -1)
        if n <= len(lines) and lines[n - 1].strip() and (sec > 0 or (sec == 0 and top_tagged)):
            bad.append((n, names[section[n]]))
    return bad


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    base, path = argv[1], (argv[2] if len(argv) > 2 else "CHANGELOG.md")
    text = open(path, encoding="utf-8").read()
    bad = misplaced(text, added_lines(base, path))
    for n, heading in bad[:20]:
        print(f"{path}:{n}: added under '## {heading}', which is not an untagged top section")
    if bad:
        print(f"{len(bad)} added line(s) below the top section. Put new notes in the topmost "
              "untagged '## ' section (Unreleased or the release being cut).", file=sys.stderr)
        return 1
    print(f"{path}: every added line is in the top section")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
