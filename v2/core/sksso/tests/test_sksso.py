"""Guard tests for sksso (Authentik) — secrets external, cache = Valkey not Redis."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_SK = Path(__file__).resolve().parents[1]


def test_swarm_authentik_server_and_worker():
    doc = yaml.safe_load((_SK / "swarm-compose.yml").read_text())
    svcs = doc["services"]
    assert "server" in svcs and "worker" in svcs
    assert svcs["server"]["image"].startswith("ghcr.io/goauthentik/server")


def test_swarm_secrets_external_not_inline():
    doc = yaml.safe_load((_SK / "swarm-compose.yml").read_text())
    for s in ("authentik_secret_key", "authentik_pg_password"):
        assert doc["secrets"][s]["external"] is True
    # secret key referenced by FILE, never an inline value
    env = " ".join(doc["services"]["server"]["environment"])
    assert "AUTHENTIK_SECRET_KEY__FILE=" in env


def test_swarm_cache_is_valkey_not_redis_host():
    doc = yaml.safe_load((_SK / "swarm-compose.yml").read_text())
    env = " ".join(doc["services"]["server"]["environment"])
    assert "AUTHENTIK_REDIS__HOST=valkey" in env


def test_k8s_disables_bundled_redis_for_skcache_valkey():
    doc = yaml.safe_load((_SK / "k8s-helmchart.yaml").read_text())
    vals = yaml.safe_load(doc["spec"]["valuesContent"])
    assert vals["redis"]["enabled"] is False
    assert "valkey" in str(vals["global"]["env"])
