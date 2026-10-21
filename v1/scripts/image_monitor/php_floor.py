"""Runtime security floor table: is a probed version below the configured minimum.

Reads floors.yaml (see that file for the shape) and is itself runtime-name
agnostic: adding OpenSSL/Node/Python floors later needs no code change here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass
class FloorCheck:
    runtime: str
    version: str | None          # probed version string, e.g. "8.5.10"; None if unparsable/unavailable
    floor: int | None            # the configured minimum patch for this version's major.minor
    below_floor: bool
    note: str = ""               # e.g. "no floor configured for 8.6", "probe failed: ..."


def load_floors(path: str | Path) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping of runtime -> config")
    return data


def runtimes_for_image(floors: dict[str, Any], image_repo: str) -> list[str]:
    """Which runtime names (keys of floors.yaml) apply to this image repo.

    Match is a case-insensitive substring check against each runtime's
    "images" list, mirroring how an operator would eyeball it ("this image
    is nextcloud, so check php").
    """
    repo_lower = image_repo.lower()
    out = []
    for name, cfg in floors.items():
        for pattern in cfg.get("images", []):
            if pattern.lower() in repo_lower:
                out.append(name)
                break
    return out


def _parse_major_minor(version: str) -> tuple[str, int] | None:
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if not m:
        return None
    major, minor, patch = m.groups()
    return f"{major}.{minor}", int(patch)


def evaluate(runtime: str, floors: dict[str, Any], probed_version: str | None,
            probe_error: str | None = None) -> FloorCheck:
    """Compare a probed version string against this runtime's floor table."""
    if probe_error:
        return FloorCheck(runtime=runtime, version=None, floor=None, below_floor=False,
                          note=f"probe failed: {probe_error}")

    cfg = floors.get(runtime, {})
    parse_re = cfg.get("parse")
    version = probed_version
    if version and parse_re:
        m = re.search(parse_re, version)
        version = m.group(1) if m else None

    if not version:
        return FloorCheck(runtime=runtime, version=probed_version, floor=None, below_floor=False,
                          note="could not parse a version from the probe output")

    parsed = _parse_major_minor(version)
    if not parsed:
        return FloorCheck(runtime=runtime, version=version, floor=None, below_floor=False,
                          note="version string not in major.minor.patch form")
    line, patch = parsed
    min_versions = cfg.get("min_versions", {})
    floor = min_versions.get(line)
    if floor is None:
        return FloorCheck(runtime=runtime, version=version, floor=None, below_floor=False,
                          note=f"no floor configured for {line}.x")
    return FloorCheck(runtime=runtime, version=version, floor=floor, below_floor=patch < floor)
