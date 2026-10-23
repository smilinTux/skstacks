"""Minimal dotted-integer version comparison for the vuln register.

Deliberately not a semver library: image tags and advisory fixed-version
strings in this register are plain dotted-integer runs ("3.7.14",
"10.11.19", "1.847.0", "v26.09.2-ls292"). Anything that doesn't parse that
way compares as unknown, never guessed.
"""
from __future__ import annotations

import re

_DEFAULT_PATTERN = r"\d+(?:\.\d+){1,3}"


def extract(text: str | None, pattern: str | None = None) -> str | None:
    """Pull a dotted-integer version out of `text`.

    `pattern` is an optional regex; if it has a capture group, group(1) is
    returned, otherwise the whole match. Falls back to the first run of
    2-4 dot-separated integers found anywhere in `text`.
    """
    if not text:
        return None
    m = re.search(pattern or _DEFAULT_PATTERN, text)
    if not m:
        return None
    return m.group(1) if m.groups() else m.group(0)


def _parts(v: str | None) -> tuple[int, ...] | None:
    if not v:
        return None
    try:
        return tuple(int(p) for p in v.split("."))
    except ValueError:
        return None


def at_least(version: str | None, floor: str | None) -> bool | None:
    """True if `version` >= `floor`, False if below, None if either side
    doesn't parse as dotted integers."""
    a, b = _parts(version), _parts(floor)
    if a is None or b is None:
        return None
    return a >= b
