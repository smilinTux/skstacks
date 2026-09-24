"""Guard tests for the Swarm core-service stacks (valid compose + ratified images)."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_STACKS = Path(__file__).resolve().parents[1] / "stacks"


def _compose(svc):
    return yaml.safe_load((_STACKS / svc / "docker-compose.yml").read_text())


def _images(doc):
    return " ".join(s.get("image", "") for s in doc["services"].values())


@pytest.mark.parametrize("svc", ["traefik", "skcache", "skobject", "sksec"])
def test_stack_is_valid_compose_on_external_network(svc):
    doc = _compose(svc)
    assert "services" in doc and doc["services"]
    # every stack joins an external overlay network (skstacks for new core stacks,
    # traefik-public for the pre-existing edge stack)
    assert any(n.get("external") for n in doc["networks"].values())


@pytest.mark.parametrize("svc", ["skcache", "skobject", "sksec"])
def test_new_core_stacks_use_skstacks_network(svc):
    doc = _compose(svc)
    assert doc["networks"]["skstacks"]["external"] is True


def test_skcache_is_valkey_not_redis():
    img = _images(_compose("skcache"))
    assert "valkey" in img and "redis" not in img


def test_skobject_is_garage_not_minio():
    img = _images(_compose("skobject"))
    assert "garage" in img and "minio" not in img


def test_sksec_is_crowdsec():
    assert "crowdsec" in _images(_compose("sksec"))


def test_traefik_is_v3():
    assert "traefik:v3" in _images(_compose("traefik"))
