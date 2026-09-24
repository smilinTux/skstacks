"""Guard tests for the ESO ClusterSecretStore→OpenBao manifests."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ESO = Path(__file__).resolve().parents[1] / "eso"


def _load(name):
    return yaml.safe_load((_ESO / name).read_text())


def test_clustersecretstore_targets_openbao_via_vault_provider():
    doc = _load("clustersecretstore-openbao.yaml")
    assert doc["kind"] == "ClusterSecretStore"
    # OpenBao is wire-compatible → ESO 'vault' provider
    assert "vault" in doc["spec"]["provider"]


def test_uses_kubernetes_auth_no_preshared_secret():
    doc = _load("clustersecretstore-openbao.yaml")
    auth = doc["spec"]["provider"]["vault"]["auth"]
    # Kubernetes auth (SA JWT) — the no-catch-22 secure-introduction path
    assert "kubernetes" in auth
    assert "token" not in auth and "appRole" not in auth


def test_externalsecret_follows_skstacks_path_convention():
    doc = _load("externalsecret-example.yaml")
    assert doc["kind"] == "ExternalSecret"
    ref = doc["spec"]["data"][0]["remoteRef"]
    assert ref["key"].startswith("skstacks/")
    assert ref["property"] == "value"
