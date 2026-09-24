"""Characterization tests for the HashiCorp Vault backend (base of OpenBao)."""
from __future__ import annotations

import pytest

from secrets.factory import get_backend
from secrets.interface import SecretNotFoundError, SecretBackendAuthError


def test_token_auth_roundtrip(fake_hvac):
    b = get_backend("hashicorp-vault", config={"token": "root", "addr": "http://x:8200"})
    b.set("sksec", "prod", "crowdsec_key", "abc123")
    assert b.get("sksec", "prod", "crowdsec_key") == "abc123"


def test_missing_key_raises_not_found(fake_hvac):
    b = get_backend("hashicorp-vault", config={"token": "root"})
    with pytest.raises(SecretNotFoundError):
        b.get("sksec", "prod", "nope")


def test_health_labels_hashicorp(fake_hvac):
    b = get_backend("hashicorp-vault", config={"token": "root"})
    h = b.health_check()
    assert h["backend"] == "hashicorp-vault"
    assert h["status"] == "ok"


def test_approle_auth_used_when_no_token(fake_hvac):
    b = get_backend("hashicorp-vault", config={"role_id": "r", "secret_id": "s"})
    # fake hvac returns "approle-tok" from approle.login
    assert b._client.token == "approle-tok"


def test_no_credentials_raises_auth_error(fake_hvac, monkeypatch):
    for v in ("VAULT_TOKEN", "VAULT_ROLE_ID", "VAULT_SECRET_ID", "VAULT_K8S_ROLE"):
        monkeypatch.delenv(v, raising=False)
    with pytest.raises(SecretBackendAuthError):
        get_backend("hashicorp-vault")


def test_list_keys_and_get_all(fake_hvac):
    b = get_backend("hashicorp-vault", config={"token": "root"})
    b.set_many("skfence", "prod", {"a": "1", "b": "2"})
    assert sorted(b.list_keys("skfence", "prod")) == ["a", "b"]
    assert b.get_all("skfence", "prod") == {"a": "1", "b": "2"}
