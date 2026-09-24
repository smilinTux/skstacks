"""The mantra applied to the stack: load-bearing services declare HA in their descriptor."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_V2 = Path(__file__).resolve().parents[1]

# These are the load-bearing services — "if you need one, get two."
CRITICAL = [
    "core/skvault", "core/sksso", "core/capauth",
    "compute/skdata", "compute/skcache", "compute/skobject",
    "comms/skbus", "cloud/skfence", "cloud/skdns",
]


@pytest.mark.parametrize("port", CRITICAL)
def test_critical_service_is_marked_ha(port):
    doc = yaml.safe_load((_V2 / port / "app.yaml").read_text())
    assert doc.get("ha") is True, f"{port} is load-bearing — mark it ha: true"
    assert int(doc.get("min_replicas", 1)) >= 2


def test_openbao_secret_server_is_3node_raft():
    # the secret server must never be a single point of failure
    doc = yaml.safe_load((_V2 / "core/skvault" / "app.yaml").read_text())
    assert doc["ha"] is True and int(doc["min_replicas"]) >= 3
