"""skred denylist: fail if a tree holds estate-specific values or vault ciphertext.

The patterns are private (they would disclose the estate if committed), so they
arrive at run time, e.g. from a CI secret. Output names file, line and pattern
NUMBER only, never the pattern or the matched text: CI logs of a public repo
are public.

    python -m skred.denylist ROOT [--denylist FILE]
    exit 0 clean, 1 findings, 2 unusable denylist or nothing scanned

Matching is per line: a value split across two lines is not found.
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import sys

VAULT_HEADER = b"$ANSIBLE_VAULT"
SKIP_DIRS = {".git", "node_modules"}


class DenylistError(Exception):
    """The denylist cannot be used; the message never contains a pattern."""


def load_patterns(path: str) -> list:
    """One regex per line; blank lines and `#` comments ignored. Case-insensitive."""
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
    except OSError as e:
        raise DenylistError(f"denylist unreadable: {e.strerror}") from None
    except UnicodeDecodeError:
        raise DenylistError("denylist is not UTF-8") from None
    patterns = []
    for ln in (ln for ln in lines if ln and not ln.startswith("#")):
        try:
            patterns.append(re.compile(ln, re.IGNORECASE))
        except re.error:
            raise DenylistError(f"pattern {len(patterns) + 1} is not a valid regex") from None
    if not patterns:
        raise DenylistError("denylist is empty: refusing to pass")
    return patterns


def scan(root: str, patterns: list) -> tuple:
    """Return (hits, files scanned). A hit is (relative path, line, what).
    Symlinks and non-regular files (FIFOs, sockets) are skipped; binaries are
    counted but not searched; a file that cannot be read is a finding."""
    hits, scanned = [], 0
    for dirpath, dirnames, files in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in files:
            path = os.path.join(dirpath, name)
            if not stat.S_ISREG(os.lstat(path).st_mode):
                continue  # symlink, FIFO, socket, device
            rel = os.path.relpath(path, root)
            scanned += 1
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                hits.append((rel, 0, "unreadable"))  # fail closed: it could hold anything
                continue
            if data.startswith(VAULT_HEADER):
                hits.append((rel, 1, "vault"))
                continue
            if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
                text = data.decode("utf-16", "replace")
            elif b"\0" in data[:8192]:
                continue  # binary
            else:
                text = data.decode("utf-8", "replace")
            for n, line in enumerate(text.splitlines(), 1):
                for i, rx in enumerate(patterns, 1):
                    if rx.search(line):
                        hits.append((rel, n, f"pattern {i}"))
    return hits, scanned


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="skred-denylist")
    ap.add_argument("root")
    ap.add_argument("--denylist", help="file of private regexes, one per line")
    args = ap.parse_args(argv)
    try:
        patterns = load_patterns(args.denylist) if args.denylist else []
    except DenylistError as e:
        print(e, file=sys.stderr)
        return 2
    if not os.path.isdir(args.root):
        print("scan root is not a directory: refusing to pass", file=sys.stderr)
        return 2
    hits, scanned = scan(args.root, patterns)
    if not scanned:
        print("0 files scanned: refusing to pass", file=sys.stderr)
        return 2
    for rel, n, what in hits:
        print(f"{rel}:{n}: {what}")
    print(f"{scanned} files scanned, {len(hits)} finding(s)")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
