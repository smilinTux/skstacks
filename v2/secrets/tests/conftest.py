"""
Test config for the skstacks-secrets package.

Puts v2/ on sys.path so `import secrets.<x>` resolves to this package
(the deploy tooling does the same via PYTHONPATH), and provides a fake
`hvac` client so the Vault/OpenBao backends can be unit-tested without a
live server.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# v2/ (the parent of the `secrets` package) must be importable as the
# top-level path so `import secrets.factory` finds this package.
_V2 = Path(__file__).resolve().parents[2]
if str(_V2) not in sys.path:
    sys.path.insert(0, str(_V2))


class _FakeKVv2:
    """In-memory stand-in for hvac client.secrets.kv.v2."""

    def __init__(self, store: dict):
        self._store = store

    def read_secret_version(self, path, mount_point=None, raise_on_deleted_version=True):
        if path not in self._store:
            import hvac.exceptions  # type: ignore
            raise hvac.exceptions.InvalidPath(path)
        return {"data": {"data": dict(self._store[path]),
                         "metadata": {"version": 1, "created_time": "2026-06-11T00:00:00Z"}}}

    def create_or_update_secret(self, path, secret, mount_point=None):
        self._store[path] = dict(secret)
        return {"data": {"version": 1}}

    def delete_metadata_and_all_versions(self, path, mount_point=None):
        self._store.pop(path, None)

    def list_secrets(self, path, mount_point=None):
        prefix = path.rstrip("/") + "/"
        keys = sorted({k[len(prefix):].split("/")[0]
                       for k in self._store if k.startswith(prefix)})
        if not keys:
            import hvac.exceptions  # type: ignore
            raise hvac.exceptions.InvalidPath(path)
        return {"data": {"keys": keys}}


def _make_fake_hvac():
    fake = MagicMock(name="hvac")

    class InvalidPath(Exception):
        pass

    class Forbidden(Exception):
        pass

    exceptions = MagicMock()
    exceptions.InvalidPath = InvalidPath
    exceptions.Forbidden = Forbidden
    fake.exceptions = exceptions

    def _client(url=None, namespace=None, verify=True, **kw):
        store: dict = {}
        c = MagicMock(name="hvac.Client")
        c.url = url
        c.token = None
        c.is_authenticated.return_value = True
        c.sys.read_health_status.return_value = {"sealed": False}
        c.secrets.kv.v2 = _FakeKVv2(store)
        c.auth.approle.login.return_value = {"auth": {"client_token": "approle-tok"}}
        c.auth.kubernetes.login.return_value = {"auth": {"client_token": "k8s-tok"}}
        c._store = store
        return c

    fake.Client = _client
    return fake


@pytest.fixture
def fake_hvac(monkeypatch):
    fake = _make_fake_hvac()
    monkeypatch.setitem(sys.modules, "hvac", fake)
    monkeypatch.setitem(sys.modules, "hvac.exceptions", fake.exceptions)
    return fake
