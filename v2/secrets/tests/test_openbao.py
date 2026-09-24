"""
Tests for the OpenBao backend + its factory registration.

OpenBao is the Linux Foundation MPL-2.0 fork of HashiCorp Vault and is
wire/API-compatible, so the adapter reuses the HashiCorp client logic but
identifies as "openbao" and honours BAO_* environment variables.
"""
from __future__ import annotations

import pytest

from secrets.factory import get_backend, list_backends, _DEFAULT_BACKEND
from secrets.hashicorp_vault.backend import HashiCorpVaultBackend


def test_openbao_registered_in_factory():
    assert "openbao" in list_backends()


def test_default_backend_is_vault_file_no_catch22():
    # The factory default must be the server-less root of trust (vault-file),
    # so bootstrapping never depends on a running secret server.
    assert _DEFAULT_BACKEND == "vault-file"


def test_openbao_is_wire_compatible_subclass():
    from secrets.openbao.backend import OpenBaoBackend
    assert issubclass(OpenBaoBackend, HashiCorpVaultBackend)


def test_openbao_get_set_roundtrip(fake_hvac):
    b = get_backend("openbao", config={"token": "dev-root", "addr": "http://127.0.0.1:8200"})
    b.set("skfence", "prod", "cf_token", "s3cr3t")
    assert b.get("skfence", "prod", "cf_token") == "s3cr3t"


def test_openbao_health_reports_openbao(fake_hvac):
    b = get_backend("openbao", config={"token": "dev-root"})
    assert b.health_check()["backend"] == "openbao"


def test_openbao_honors_bao_addr_env(fake_hvac, monkeypatch):
    monkeypatch.setenv("BAO_ADDR", "https://bao.skworld.io:8200")
    monkeypatch.setenv("BAO_TOKEN", "env-tok")
    b = get_backend("openbao")
    assert b._client.url == "https://bao.skworld.io:8200"
