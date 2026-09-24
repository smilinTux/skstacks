"""
Real-roundtrip tests for the vault-file backend — the server-less ROOT OF TRUST.

These use the actual `ansible-vault` binary (AES-256) so we prove the backend
that the whole no-catch-22 bootstrap leans on actually encrypts/decrypts.
"""
from __future__ import annotations

import shutil

import pytest

from secrets.factory import get_backend
from secrets.interface import SecretNotFoundError

pytestmark = pytest.mark.skipif(
    shutil.which("ansible-vault") is None, reason="ansible-vault not installed"
)


@pytest.fixture
def vf(tmp_path):
    vault_dir = tmp_path / "vaults"
    pass_dir = tmp_path / "pass"
    pass_dir.mkdir()
    # env-wide password file: .{env}_vault_pass
    (pass_dir / ".test_vault_pass").write_text("correct horse battery staple\n")
    return get_backend("vault-file", config={
        "vault_dir": str(vault_dir),
        "vault_pass_dir": str(pass_dir),
    })


def test_set_then_get_roundtrip(vf):
    vf.set("skfence", "test", "cf_token", "topsecret")
    assert vf.get("skfence", "test", "cf_token") == "topsecret"


def test_file_on_disk_is_actually_encrypted(vf, tmp_path):
    vf.set("skfence", "test", "cf_token", "topsecret")
    blob = (tmp_path / "vaults" / "test" / "skfence-test_vault.yml").read_text()
    assert blob.startswith("$ANSIBLE_VAULT")
    assert "topsecret" not in blob


def test_set_many_list_and_delete(vf):
    vf.set_many("sksec", "test", {"a": "1", "b": "2"})
    assert sorted(vf.list_keys("sksec", "test")) == ["a", "b"]
    vf.delete("sksec", "test", "a")
    assert vf.list_keys("sksec", "test") == ["b"]


def test_missing_key_raises(vf):
    vf.set("skfence", "test", "k", "v")
    with pytest.raises(SecretNotFoundError):
        vf.get("skfence", "test", "absent")
