"""Characterization tests for the backend factory."""
from __future__ import annotations

import pytest

from secrets import factory
from secrets.factory import get_backend, list_backends, health_report
from secrets.interface import SKSecretBackend


def test_all_backends_registered():
    assert set(list_backends()) == {"vault-file", "sops-age", "openbao", "hashicorp-vault", "capauth"}


def test_unknown_backend_raises():
    with pytest.raises(ValueError):
        get_backend("does-not-exist")


def test_env_var_selects_backend(fake_hvac, monkeypatch):
    monkeypatch.setenv("SKSTACKS_SECRET_BACKEND", "openbao")
    monkeypatch.setenv("BAO_TOKEN", "t")
    b = get_backend()
    assert isinstance(b, SKSecretBackend)
    assert b.health_check()["backend"] == "openbao"


def test_openbao_is_recommended_server_default():
    # vault-file is the server-less factory default (bootstrap root);
    # openbao is the recommended backend once a secret *server* is run.
    assert factory._DEFAULT_BACKEND == "vault-file"
    assert factory._DEFAULT_SERVER_BACKEND == "openbao"


def test_health_report_covers_all_backends(fake_hvac):
    report = health_report()
    assert set(report) == {"vault-file", "sops-age", "openbao", "hashicorp-vault", "capauth"}
    # each entry has a status key
    assert all("status" in v for v in report.values())
