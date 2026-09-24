"""
Tests for the SOPS+age backend — a server-less sibling of vault-file for the
GitOps lane (age-encrypted YAML committed to git, decrypted in CI/at deploy).

Uses dependency injection (_decrypt/_encrypt) so the suite needs no sops/age
binaries; the default implementation shells out to `sops`.
"""
from __future__ import annotations

import pytest

from secrets.factory import get_backend, list_backends
from secrets.interface import SKSecretBackend, SecretNotFoundError


@pytest.fixture
def sops(tmp_path):
    """SOPS+age backend backed by an in-memory fake crypto layer."""
    store: dict = {}  # path -> dict

    def fake_decrypt(path):
        if str(path) not in store:
            raise FileNotFoundError(path)
        return dict(store[str(path)])

    def fake_encrypt(path, data):
        store[str(path)] = dict(data)

    b = get_backend("sops-age", config={
        "secrets_dir": str(tmp_path / "sops"),
        "_decrypt": fake_decrypt,
        "_encrypt": fake_encrypt,
    })
    b._store = store
    return b


def test_sops_age_registered():
    assert "sops-age" in list_backends()


def test_is_serverless_secret_backend(sops):
    assert isinstance(sops, SKSecretBackend)


def test_set_get_roundtrip(sops):
    sops.set("skcicd", "prod", "deploy_token", "ghp_x")
    assert sops.get("skcicd", "prod", "deploy_token") == "ghp_x"


def test_set_many_list_delete(sops):
    sops.set_many("skfence", "prod", {"a": "1", "b": "2"})
    assert sorted(sops.list_keys("skfence", "prod")) == ["a", "b"]
    sops.delete("skfence", "prod", "a")
    assert sops.list_keys("skfence", "prod") == ["b"]


def test_missing_key_raises(sops):
    sops.set("skfence", "prod", "k", "v")
    with pytest.raises(SecretNotFoundError):
        sops.get("skfence", "prod", "absent")


def test_health_reports_sops_age(sops):
    assert sops.health_check()["backend"] == "sops-age"
