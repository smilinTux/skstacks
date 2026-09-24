"""Guard tests for the NATS JetStream (skbus) stacks — clustered, no inline secrets."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_COMMS = Path(__file__).resolve().parents[1]
_SWARM = _COMMS.parents[1] / "platform" / "swarm" / "stacks" / "skbus"


def test_swarm_nats_clustered_jetstream():
    doc = yaml.safe_load((_SWARM / "docker-compose.yml").read_text())
    svc = doc["services"]["nats"]
    assert svc["image"].startswith("nats:")
    assert svc["deploy"]["replicas"] >= 3                       # HA cluster
    assert svc["deploy"]["placement"]["max_replicas_per_node"] == 1


def test_swarm_config_has_jetstream_and_cluster():
    conf = (_SWARM / "nats-server.conf").read_text()
    assert "jetstream" in conf and "cluster" in conf
    # passwords are env-interpolated, never literal
    assert "$NATS_SYS_PASSWORD" in conf and "$NATS_SKCOMMS_PASSWORD" in conf


def test_k8s_helmchart_jetstream_cluster_secret_ref():
    doc = yaml.safe_load((_COMMS / "k8s-helmchart.yaml").read_text())
    vals = doc["spec"]["valuesContent"]
    assert "jetstream" in vals and "cluster" in vals
    assert "secretKeyRef" in vals                               # auth from ESO Secret


def test_no_inline_passwords_anywhere():
    for f in list(_SWARM.glob("*")) + list(_COMMS.rglob("*")):
        if f.is_file() and f.suffix in (".yml", ".yaml", ".conf"):
            t = f.read_text()
            # only env-interpolation ($VAR / ${VAR}) or secretKeyRef — no literal pw=...
            assert "password: \"sk" not in t.lower()
            assert "password=changeme" not in t.lower()
