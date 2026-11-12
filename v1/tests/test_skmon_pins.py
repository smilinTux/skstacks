"""The skmon pin table: exact versions, digests, release age, and the compose defaults match it."""
import re
from datetime import date, timedelta

import yaml

from image_pins import template_default
from skmon_support import CFG

PINS = yaml.safe_load((CFG / "pins.yml").read_text())

# Compose image knob -> pin key. Extended as later tasks add services.
COMPOSE_KNOBS = {
    "VICTORIAMETRICS_IMAGE": "victoriametrics",
    "GRAFANA_IMAGE": "grafana",
    "LOKI_IMAGE": "loki",
    "ALERTMANAGER_IMAGE": "alertmanager",
}


def test_pin_table_has_every_key():
    assert set(PINS["images"]) == {
        "victoriametrics", "vmalert", "vmalert_tool", "alertmanager", "alloy", "loki",
        "grafana", "pyroscope", "pve_exporter", "python", "python_slim", "socket_proxy"}
    for k, p in PINS["images"].items():
        assert p["repo"] and p["source"].startswith("https://"), k


def test_every_pin_is_exact_and_digested():
    for k, p in PINS["images"].items():
        assert re.fullmatch(r"v?\d+\.\d+\.\d+([.-][\w.-]+)?", p["tag"]), k
        assert re.fullmatch(r"sha256:[0-9a-f]{64}", p["digest"]), k


def test_every_pin_is_at_least_14_days_old():
    for k, p in {**PINS["images"], **PINS["binaries"]}.items():
        assert date.fromisoformat(p["released"]) <= date.today() - timedelta(days=14), k


def test_compose_defaults_equal_pin_table():
    txt = (CFG / "skmon.yml.j2").read_text()
    for knob, key in COMPOSE_KNOBS.items():
        p = PINS["images"][key]
        assert template_default(txt, knob) == f"{p['repo']}:{p['tag']}@{p['digest']}", knob


def test_alloy_binary_matches_container_version():
    assert PINS["binaries"]["alloy"]["version"] == PINS["images"]["alloy"]["tag"]


def test_alloy_binary_checksums_are_sha256():
    b = PINS["binaries"]["alloy"]
    for arch in ("amd64", "arm64"):
        assert re.fullmatch(r"[0-9a-f]{64}", b[f"sha256_linux_{arch}"]), arch


def test_vmalert_tool_tracks_vmalert():
    i = PINS["images"]
    assert i["vmalert_tool"]["tag"] == i["vmalert"]["tag"]
    assert i["python_slim"]["tag"].replace("-slim", "") == i["python"]["tag"]
