"""
Guards the RATIFIED adapter defaults (stack-validation 2026-06-11) so port
descriptors can't silently regress to superseded tech.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_V2 = Path(__file__).resolve().parents[1]


def _provider(rel):
    return yaml.safe_load((_V2 / rel / "app.yaml").read_text())["provider"]


@pytest.mark.parametrize("appyaml", sorted(_V2.rglob("*/app.yaml")), ids=lambda p: str(p.relative_to(_V2)))
def test_every_app_descriptor_parses(appyaml):
    # unquoted colons in provider: values silently break these — guard against it
    doc = yaml.safe_load(appyaml.read_text())
    assert isinstance(doc, dict) and "provider" in doc


@pytest.mark.parametrize("port,must_contain,must_not_start", [
    ("comms/skchat",  "Tuwunel",  "Matrix Synapse"),   # ratified switch
    ("compute/skflow", "Windmill", "n8n"),              # ratified switch
    ("comms/skbus",   "NATS",     None),
    ("comms/skvoice", "LiveKit",  None),
    ("compute/skobject", "Garage", "MinIO"),
    ("compute/skcache", "Valkey", "Redis"),
])
def test_ratified_provider_default(port, must_contain, must_not_start):
    prov = _provider(port)
    assert must_contain in prov, f"{port}: expected {must_contain} in provider"
    if must_not_start:
        assert not prov.startswith(must_not_start), f"{port}: must not default to {must_not_start}"
