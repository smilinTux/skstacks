"""
Unit tests for skstacks v2 secret backends.

Covers: VaultFileBackend, HashiCorpVaultBackend, CapAuthBackend
All external calls (subprocess, hvac, gpg, filesystem) are mocked.

Run from repo root:
    pytest skstacks/v2/tests/test_secrets_backends.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, mock_open, patch

import pytest

# ── sys.path setup ────────────────────────────────────────────────────────────
# Add skstacks/v2/ to sys.path so 'secrets.*' package imports resolve.
# NOTE: this shadows the stdlib `secrets` module for this test process;
#       the HashiCorpVaultBackend.rotate() workaround is handled in test_rotate.
_V2_ROOT = str(Path(__file__).parent.parent)
if _V2_ROOT not in sys.path:
    sys.path.insert(0, _V2_ROOT)

from secrets.interface import (  # noqa: E402
    SecretBackendAuthError,
    SecretBackendError,
    SecretMeta,
    SecretNotFoundError,
)
from secrets.vault_file.backend import VaultFileBackend  # noqa: E402
from secrets.hashicorp_vault.backend import HashiCorpVaultBackend  # noqa: E402
from secrets.capauth.backend import CapAuthBackend  # noqa: E402


# =============================================================================
# VaultFileBackend
# =============================================================================

class TestVaultFileBackend:

    @pytest.fixture
    def backend(self, tmp_path):
        vault_dir = tmp_path / "vaults"
        vault_pass_dir = tmp_path / "pass"
        vault_dir.mkdir()
        vault_pass_dir.mkdir()
        return VaultFileBackend(
            vault_dir=str(vault_dir),
            vault_pass_dir=str(vault_pass_dir),
        )

    @pytest.fixture
    def backend_with_files(self, tmp_path):
        """Backend whose vault and password files already exist on disk."""
        vault_dir = tmp_path / "vaults"
        vault_pass_dir = tmp_path / "pass"
        env_dir = vault_dir / "prod"
        env_dir.mkdir(parents=True)
        vault_pass_dir.mkdir()
        vault_file = env_dir / "myapp-prod_vault.yml"
        vault_file.touch()
        pass_file = vault_pass_dir / ".myapp_prod_vault_pass"
        pass_file.write_text("s3cr3t_pass\n")
        b = VaultFileBackend(
            vault_dir=str(vault_dir),
            vault_pass_dir=str(vault_pass_dir),
        )
        return b, vault_file, pass_file

    # ── get ───────────────────────────────────────────────────────────────────

    def test_get_happy(self, backend):
        with patch.object(backend, "_decrypt", return_value={"db_pass": "hunter2"}):
            assert backend.get("myapp", "prod", "db_pass") == "hunter2"

    def test_get_not_found_raises(self, backend):
        with patch.object(backend, "_decrypt", return_value={"other": "val"}):
            with pytest.raises(SecretNotFoundError) as exc:
                backend.get("myapp", "prod", "db_pass")
        assert exc.value.key == "db_pass"
        assert exc.value.scope == "myapp"
        assert exc.value.env == "prod"

    # ── get_all ───────────────────────────────────────────────────────────────

    def test_get_all(self, backend):
        secrets = {"db_pass": "hunter2", "api_key": "abc123"}
        with patch.object(backend, "_decrypt", return_value=secrets):
            assert backend.get_all("myapp", "prod") == secrets

    def test_get_all_empty(self, backend):
        with patch.object(backend, "_decrypt", return_value={}):
            assert backend.get_all("myapp", "prod") == {}

    # ── get_with_meta ─────────────────────────────────────────────────────────

    def test_get_with_meta(self, backend):
        with patch.object(backend, "_decrypt", return_value={"token": "tok"}):
            value, meta = backend.get_with_meta("myapp", "prod", "token")
        assert value == "tok"
        assert isinstance(meta, SecretMeta)
        assert meta.key == "token"
        assert meta.scope == "myapp"
        assert meta.env == "prod"
        assert meta.version is None  # vault-file has no version info

    # ── set ───────────────────────────────────────────────────────────────────

    def test_set_merges_with_existing_vault(self, backend, tmp_path):
        vault_file = tmp_path / "vaults" / "prod" / "myapp-prod_vault.yml"
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.touch()

        with patch.object(backend, "_decrypt", return_value={"old": "val"}) as mock_dec, \
             patch.object(backend, "_encrypt") as mock_enc:
            backend.set("myapp", "prod", "new", "nval")

        mock_dec.assert_called_once_with("myapp", "prod")
        mock_enc.assert_called_once_with("myapp", "prod", {"old": "val", "new": "nval"})

    def test_set_creates_new_vault_when_missing(self, backend):
        with patch.object(backend, "_encrypt") as mock_enc:
            backend.set("myapp", "prod", "key", "val")
        mock_enc.assert_called_once_with("myapp", "prod", {"key": "val"})

    def test_set_overwrites_existing_key(self, backend, tmp_path):
        vault_file = tmp_path / "vaults" / "prod" / "myapp-prod_vault.yml"
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.touch()

        with patch.object(backend, "_decrypt", return_value={"key": "old"}), \
             patch.object(backend, "_encrypt") as mock_enc:
            backend.set("myapp", "prod", "key", "new")

        mock_enc.assert_called_once_with("myapp", "prod", {"key": "new"})

    # ── set_many ──────────────────────────────────────────────────────────────

    def test_set_many_new_vault(self, backend):
        with patch.object(backend, "_encrypt") as mock_enc:
            backend.set_many("myapp", "prod", {"a": "1", "b": "2"})
        mock_enc.assert_called_once_with("myapp", "prod", {"a": "1", "b": "2"})

    def test_set_many_merges_with_existing(self, backend, tmp_path):
        vault_file = tmp_path / "vaults" / "prod" / "myapp-prod_vault.yml"
        vault_file.parent.mkdir(parents=True, exist_ok=True)
        vault_file.touch()

        with patch.object(backend, "_decrypt", return_value={"x": "old"}), \
             patch.object(backend, "_encrypt") as mock_enc:
            backend.set_many("myapp", "prod", {"x": "new", "y": "added"})

        mock_enc.assert_called_once_with("myapp", "prod", {"x": "new", "y": "added"})

    # ── delete ────────────────────────────────────────────────────────────────

    def test_delete_happy(self, backend):
        with patch.object(backend, "_decrypt", return_value={"keep": "me", "rm": "gone"}), \
             patch.object(backend, "_encrypt") as mock_enc:
            backend.delete("myapp", "prod", "rm")
        mock_enc.assert_called_once_with("myapp", "prod", {"keep": "me"})

    def test_delete_not_found_raises(self, backend):
        with patch.object(backend, "_decrypt", return_value={"other": "key"}):
            with pytest.raises(SecretNotFoundError):
                backend.delete("myapp", "prod", "missing")

    # ── list_keys ─────────────────────────────────────────────────────────────

    def test_list_keys(self, backend):
        with patch.object(backend, "_decrypt", return_value={"a": "1", "b": "2"}):
            assert sorted(backend.list_keys("myapp", "prod")) == ["a", "b"]

    def test_list_keys_empty(self, backend):
        with patch.object(backend, "_decrypt", return_value={}):
            assert backend.list_keys("myapp", "prod") == []

    # ── list_scopes ───────────────────────────────────────────────────────────

    def test_list_scopes_no_env_dir(self, backend):
        assert backend.list_scopes("staging") == []

    def test_list_scopes(self, backend, tmp_path):
        env_dir = tmp_path / "vaults" / "prod"
        env_dir.mkdir(parents=True)
        (env_dir / "skfence-prod_vault.yml").touch()
        (env_dir / "sksec-prod_vault.yml").touch()
        assert sorted(backend.list_scopes("prod")) == ["skfence", "sksec"]

    def test_list_scopes_ignores_non_vault_files(self, backend, tmp_path):
        env_dir = tmp_path / "vaults" / "prod"
        env_dir.mkdir(parents=True)
        (env_dir / "myapp-prod_vault.yml").touch()
        (env_dir / "README.md").touch()       # should be ignored
        (env_dir / ".gitkeep").touch()         # should be ignored
        assert backend.list_scopes("prod") == ["myapp"]

    # ── health_check ──────────────────────────────────────────────────────────

    def test_health_check_ok(self, backend):
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = backend.health_check()
        assert result["status"] == "ok"
        assert result["backend"] == "vault-file"
        assert result["ansible_vault"] == "available"
        assert "vault_dir" in result

    def test_health_check_no_ansible_vault(self, backend):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = backend.health_check()
        assert result["status"] == "degraded"
        assert result["ansible_vault"] == "not found"

    def test_health_check_ansible_vault_error(self, backend):
        err = subprocess.CalledProcessError(1, "ansible-vault")
        with patch("subprocess.run", side_effect=err):
            result = backend.health_check()
        assert result["status"] == "degraded"

    # ── _decrypt internals ────────────────────────────────────────────────────

    def test_decrypt_vault_not_found(self, backend):
        with pytest.raises(SecretBackendError, match="Vault file not found"):
            backend._decrypt("myapp", "prod")

    def test_decrypt_no_password_file(self, backend, tmp_path):
        vault_file = tmp_path / "vaults" / "prod" / "myapp-prod_vault.yml"
        vault_file.parent.mkdir(parents=True)
        vault_file.touch()
        with pytest.raises(SecretBackendError, match="No vault password file"):
            backend._decrypt("myapp", "prod")

    def test_decrypt_subprocess_error(self, backend_with_files):
        b, _, _ = backend_with_files
        err = subprocess.CalledProcessError(1, "ansible-vault", stderr="bad password")
        with patch("subprocess.run", side_effect=err):
            with pytest.raises(SecretBackendError, match="Failed to decrypt"):
                b._decrypt("myapp", "prod")

    def test_decrypt_success_strips_vault_prefix(self, backend_with_files):
        b, _, _ = backend_with_files
        mock_result = MagicMock()
        mock_result.stdout = ""
        yaml_data = {"vault_myapp_db_pass": "s3cr3t", "vault_myapp_api_key": "abc"}
        with patch("subprocess.run", return_value=mock_result), \
             patch("yaml.safe_load", return_value=yaml_data):
            result = b._decrypt("myapp", "prod")
        assert result == {"db_pass": "s3cr3t", "api_key": "abc"}

    def test_decrypt_keeps_keys_without_prefix(self, backend_with_files):
        b, _, _ = backend_with_files
        mock_result = MagicMock()
        mock_result.stdout = ""
        yaml_data = {"vault_myapp_secret": "val1", "other_key": "val2"}
        with patch("subprocess.run", return_value=mock_result), \
             patch("yaml.safe_load", return_value=yaml_data):
            result = b._decrypt("myapp", "prod")
        assert result == {"secret": "val1", "other_key": "val2"}

    def test_decrypt_with_vault_pass_cmd(self, tmp_path):
        vault_dir = tmp_path / "vaults"
        vault_pass_dir = tmp_path / "pass"
        (vault_dir / "prod").mkdir(parents=True)
        vault_pass_dir.mkdir()
        (vault_dir / "prod" / "myapp-prod_vault.yml").touch()
        b = VaultFileBackend(
            vault_dir=str(vault_dir),
            vault_pass_dir=str(vault_pass_dir),
            vault_pass_cmd="echo password",
        )
        mock_result = MagicMock()
        mock_result.stdout = ""
        with patch("subprocess.check_output", return_value=b"password"), \
             patch("subprocess.run", return_value=mock_result) as mock_run, \
             patch("yaml.safe_load", return_value={}):
            b._decrypt("myapp", "prod")
        cmd_args = mock_run.call_args[0][0]
        assert "--vault-password-file" in cmd_args

    # ── path helpers ──────────────────────────────────────────────────────────

    def test_vault_path(self, backend, tmp_path):
        p = backend._vault_path("myapp", "prod")
        assert p == tmp_path / "vaults" / "prod" / "myapp-prod_vault.yml"

    def test_pass_path_scope_specific(self, backend, tmp_path):
        f = tmp_path / "pass" / ".myapp_prod_vault_pass"
        f.write_text("pw")
        assert backend._pass_path("myapp", "prod") == f

    def test_pass_path_env_fallback(self, backend, tmp_path):
        f = tmp_path / "pass" / ".prod_vault_pass"
        f.write_text("pw")
        assert backend._pass_path("myapp", "prod") == f

    def test_pass_path_scope_takes_priority_over_env(self, backend, tmp_path):
        scope_f = tmp_path / "pass" / ".myapp_prod_vault_pass"
        env_f = tmp_path / "pass" / ".prod_vault_pass"
        scope_f.write_text("scope-pw")
        env_f.write_text("env-pw")
        assert backend._pass_path("myapp", "prod") == scope_f

    def test_pass_path_none_when_missing(self, backend):
        assert backend._pass_path("myapp", "prod") is None

    # ── context manager ───────────────────────────────────────────────────────

    def test_context_manager(self, tmp_path):
        with VaultFileBackend(
            vault_dir=str(tmp_path / "vaults"),
            vault_pass_dir=str(tmp_path / "pass"),
        ) as b:
            assert b is not None


# =============================================================================
# HashiCorpVaultBackend
# =============================================================================

class TestHashiCorpVaultBackend:
    """
    hvac is mocked entirely so no Vault server is needed.
    The autouse fixture patches sys.modules['hvac'] for every test.
    """

    @pytest.fixture(autouse=True)
    def mock_hvac_module(self):
        mock_hvac_mod = MagicMock()

        # Real exception subclasses so except clauses catch them correctly.
        class InvalidPath(Exception):
            pass

        class Forbidden(Exception):
            pass

        mock_hvac_mod.exceptions.InvalidPath = InvalidPath
        mock_hvac_mod.exceptions.Forbidden = Forbidden

        mock_client = MagicMock()
        mock_client.is_authenticated.return_value = True
        mock_hvac_mod.Client.return_value = mock_client

        # Expose on self for use in tests without re-importing from fixture.
        self._mock_hvac = mock_hvac_mod
        self._mock_client = mock_client
        self._InvalidPath = InvalidPath
        self._Forbidden = Forbidden

        with patch.dict(sys.modules, {
            "hvac": mock_hvac_mod,
            "hvac.exceptions": mock_hvac_mod.exceptions,
        }):
            yield

    @pytest.fixture
    def backend(self):
        return HashiCorpVaultBackend(
            addr="https://vault.test:8200",
            token="root-token",
            mount="kv",
            path_prefix="skstacks",
        )

    # ── __init__ auth methods ─────────────────────────────────────────────────

    def test_init_token_auth(self):
        HashiCorpVaultBackend(addr="https://vault.test:8200", token="mytoken")
        assert self._mock_client.token == "mytoken"
        self._mock_client.is_authenticated.assert_called()

    def test_init_approle_auth(self):
        self._mock_client.auth.approle.login.return_value = {
            "auth": {"client_token": "approle-tok"}
        }
        HashiCorpVaultBackend(
            addr="https://vault.test:8200",
            role_id="role-123",
            secret_id="secret-456",
        )
        self._mock_client.auth.approle.login.assert_called_once_with(
            role_id="role-123",
            secret_id="secret-456",
        )
        assert self._mock_client.token == "approle-tok"

    def test_init_k8s_auth(self):
        self._mock_client.auth.kubernetes.login.return_value = {
            "auth": {"client_token": "k8s-tok"}
        }
        m = mock_open(read_data="k8s-jwt-token")
        with patch("builtins.open", m):
            HashiCorpVaultBackend(
                addr="https://vault.test:8200",
                k8s_role="my-k8s-role",
            )
        self._mock_client.auth.kubernetes.login.assert_called_once_with(
            role="my-k8s-role", jwt="k8s-jwt-token"
        )

    def test_init_k8s_no_sa_token_raises(self):
        with patch("builtins.open", side_effect=FileNotFoundError):
            with pytest.raises(SecretBackendAuthError, match="Kubernetes auth"):
                HashiCorpVaultBackend(
                    addr="https://vault.test:8200",
                    k8s_role="my-k8s-role",
                )

    def test_init_no_credentials_raises(self):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SecretBackendAuthError, match="No Vault auth credentials"):
                HashiCorpVaultBackend(addr="https://vault.test:8200")

    def test_init_not_authenticated_raises(self):
        self._mock_client.is_authenticated.return_value = False
        with pytest.raises(SecretBackendAuthError, match="Vault authentication failed"):
            HashiCorpVaultBackend(addr="https://vault.test:8200", token="bad-token")

    def test_init_missing_hvac_raises(self):
        with patch.dict(sys.modules, {"hvac": None}):
            with pytest.raises(ImportError, match="hvac is required"):
                HashiCorpVaultBackend(addr="https://vault.test:8200", token="t")

    # ── _kv_path ──────────────────────────────────────────────────────────────

    def test_kv_path_no_key(self, backend):
        assert backend._kv_path("myapp", "prod") == "skstacks/prod/myapp"

    def test_kv_path_with_key(self, backend):
        assert backend._kv_path("myapp", "prod", "api_key") == "skstacks/prod/myapp/api_key"

    # ── get ───────────────────────────────────────────────────────────────────

    def test_get_happy(self, backend):
        self._mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {
                "data": {"value": "s3cr3t"},
                "metadata": {"version": 3, "created_time": "2026-01-01T00:00:00Z"},
            }
        }
        assert backend.get("myapp", "prod", "api_key") == "s3cr3t"

    def test_get_not_found(self, backend):
        self._mock_client.secrets.kv.v2.read_secret_version.side_effect = \
            self._InvalidPath("not found")
        with pytest.raises(SecretNotFoundError) as exc:
            backend.get("myapp", "prod", "api_key")
        assert exc.value.key == "api_key"
        assert exc.value.scope == "myapp"
        assert exc.value.env == "prod"

    def test_get_permission_denied(self, backend):
        self._mock_client.secrets.kv.v2.read_secret_version.side_effect = \
            self._Forbidden("forbidden")
        with pytest.raises(SecretBackendAuthError, match="Permission denied"):
            backend.get("myapp", "prod", "api_key")

    def test_get_missing_value_key_raises(self, backend):
        """Secret exists in Vault but wasn't stored with the 'value' key convention."""
        self._mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {
                "data": {"wrong_key": "oops"},
                "metadata": {},
            }
        }
        with pytest.raises(SecretBackendError, match="does not have a 'value' key"):
            backend.get("myapp", "prod", "api_key")

    # ── get_all ───────────────────────────────────────────────────────────────

    def test_get_all(self, backend):
        self._mock_client.secrets.kv.v2.list_secrets.return_value = {
            "data": {"keys": ["db_pass", "api_key"]}
        }
        self._mock_client.secrets.kv.v2.read_secret_version.side_effect = [
            {"data": {"data": {"value": "pw"}, "metadata": {}}},
            {"data": {"data": {"value": "tok"}, "metadata": {}}},
        ]
        result = backend.get_all("myapp", "prod")
        assert result == {"db_pass": "pw", "api_key": "tok"}

    def test_get_all_empty_scope(self, backend):
        self._mock_client.secrets.kv.v2.list_secrets.side_effect = \
            self._InvalidPath("no secrets")
        assert backend.get_all("myapp", "prod") == {}

    # ── get_with_meta ─────────────────────────────────────────────────────────

    def test_get_with_meta(self, backend):
        self._mock_client.secrets.kv.v2.read_secret_version.return_value = {
            "data": {
                "data": {"value": "tok"},
                "metadata": {"version": 7, "created_time": "2026-02-01T12:00:00Z"},
            }
        }
        value, meta = backend.get_with_meta("myapp", "prod", "token")
        assert value == "tok"
        assert meta.version == "7"
        assert meta.created_at == "2026-02-01T12:00:00Z"
        assert meta.key == "token"
        assert meta.scope == "myapp"
        assert meta.env == "prod"

    # ── set ───────────────────────────────────────────────────────────────────

    def test_set(self, backend):
        backend.set("myapp", "prod", "api_key", "newval")
        self._mock_client.secrets.kv.v2.create_or_update_secret.assert_called_once_with(
            path="skstacks/prod/myapp/api_key",
            secret={"value": "newval"},
            mount_point="kv",
        )

    # ── set_many ──────────────────────────────────────────────────────────────

    def test_set_many(self, backend):
        backend.set_many("myapp", "prod", {"k1": "v1", "k2": "v2"})
        assert self._mock_client.secrets.kv.v2.create_or_update_secret.call_count == 2

    def test_set_many_calls_set_per_key(self, backend):
        backend.set_many("myapp", "prod", {"k1": "v1", "k2": "v2"})
        paths_written = [
            c.kwargs["path"]
            for c in self._mock_client.secrets.kv.v2.create_or_update_secret.call_args_list
        ]
        assert sorted(paths_written) == [
            "skstacks/prod/myapp/k1",
            "skstacks/prod/myapp/k2",
        ]

    # ── delete ────────────────────────────────────────────────────────────────

    def test_delete(self, backend):
        backend.delete("myapp", "prod", "old_key")
        self._mock_client.secrets.kv.v2.delete_metadata_and_all_versions.assert_called_once_with(
            path="skstacks/prod/myapp/old_key",
            mount_point="kv",
        )

    # ── list_keys ─────────────────────────────────────────────────────────────

    def test_list_keys_happy(self, backend):
        self._mock_client.secrets.kv.v2.list_secrets.return_value = {
            "data": {"keys": ["k1", "k2", "k3"]}
        }
        assert backend.list_keys("myapp", "prod") == ["k1", "k2", "k3"]

    def test_list_keys_empty_path(self, backend):
        self._mock_client.secrets.kv.v2.list_secrets.side_effect = \
            self._InvalidPath("no secrets here")
        assert backend.list_keys("myapp", "prod") == []

    def test_list_keys_uses_correct_mount(self, backend):
        self._mock_client.secrets.kv.v2.list_secrets.return_value = {"data": {"keys": []}}
        backend.list_keys("myapp", "prod")
        call_kwargs = self._mock_client.secrets.kv.v2.list_secrets.call_args.kwargs
        assert call_kwargs["mount_point"] == "kv"
        assert call_kwargs["path"] == "skstacks/prod/myapp"

    # ── list_scopes ───────────────────────────────────────────────────────────

    def test_list_scopes(self, backend):
        self._mock_client.secrets.kv.v2.list_secrets.return_value = {
            "data": {"keys": ["skfence/", "sksec/"]}
        }
        assert sorted(backend.list_scopes("prod")) == ["skfence", "sksec"]

    def test_list_scopes_strips_trailing_slash(self, backend):
        self._mock_client.secrets.kv.v2.list_secrets.return_value = {
            "data": {"keys": ["myapp/"]}
        }
        assert backend.list_scopes("prod") == ["myapp"]

    def test_list_scopes_empty(self, backend):
        self._mock_client.secrets.kv.v2.list_secrets.side_effect = \
            self._InvalidPath("no scopes")
        assert backend.list_scopes("prod") == []

    # ── rotate ────────────────────────────────────────────────────────────────

    def test_rotate(self, backend):
        """
        rotate() calls secrets.token_hex(32) then set().
        Our 'secrets' package shadows stdlib; we patch token_hex onto it.
        """
        local_secrets_pkg = sys.modules.get("secrets")
        fake_token = "cafe" * 16  # 64 hex chars
        with patch.object(local_secrets_pkg, "token_hex", return_value=fake_token, create=True):
            with patch.object(backend, "set") as mock_set:
                result = backend.rotate("myapp", "prod", "api_key")
        assert result == fake_token
        mock_set.assert_called_once_with("myapp", "prod", "api_key", fake_token)

    # ── health_check ──────────────────────────────────────────────────────────

    def test_health_check_ok(self, backend):
        self._mock_client.sys.read_health_status.return_value = {
            "initialized": True,
            "sealed": False,
            "version": "1.15.0",
            "cluster_name": "sk-cluster",
        }
        result = backend.health_check()
        assert result["status"] == "ok"
        assert result["backend"] == "hashicorp-vault"
        assert result["sealed"] is False
        assert result["version"] == "1.15.0"

    def test_health_check_sealed_is_degraded(self, backend):
        self._mock_client.sys.read_health_status.return_value = {
            "initialized": True,
            "sealed": True,
            "version": "1.15.0",
            "cluster_name": "sk-cluster",
        }
        result = backend.health_check()
        assert result["status"] == "degraded"

    def test_health_check_unreachable(self, backend):
        self._mock_client.sys.read_health_status.side_effect = ConnectionError("unreachable")
        result = backend.health_check()
        assert result["status"] == "unavailable"
        assert "error" in result

    # ── context manager ───────────────────────────────────────────────────────

    def test_context_manager(self):
        with HashiCorpVaultBackend(
            addr="https://vault.test:8200",
            token="root-token",
        ) as b:
            assert b is not None


# =============================================================================
# CapAuthBackend
# =============================================================================

class TestCapAuthBackend:

    SECRETS = {"db_pass": "hunter2", "api_key": "abc123"}

    # ── Helpers ───────────────────────────────────────────────────────────────

    @pytest.fixture
    def gnupg_backend(self, tmp_path):
        secrets_dir = tmp_path / "secrets"
        env_dir = secrets_dir / "prod"
        env_dir.mkdir(parents=True)
        b = CapAuthBackend(
            mode="gnupg",
            key_id="DEADBEEF",
            secrets_dir=str(secrets_dir),
        )
        return b, secrets_dir, env_dir

    def _make_blob(self, env_dir: Path, scope: str, secrets: dict) -> Path:
        """Write a plaintext (un-encrypted) blob for mocking purposes."""
        blob = env_dir / f"{scope}.gpg"
        blob.write_text(json.dumps(secrets))
        return blob

    def _gpg_ok(self, secrets: dict) -> MagicMock:
        r = MagicMock()
        r.returncode = 0
        r.stdout = json.dumps(secrets)
        return r

    def _gpg_fail(self, stderr: str = "no secret key") -> MagicMock:
        r = MagicMock()
        r.returncode = 1
        r.stderr = stderr
        return r

    # ── __init__ ──────────────────────────────────────────────────────────────

    def test_init_default_mcp_mode(self, tmp_path):
        b = CapAuthBackend(mode="mcp", secrets_dir=str(tmp_path))
        assert b._mode == "mcp"

    def test_init_gnupg_mode(self, tmp_path):
        b = CapAuthBackend(mode="gnupg", key_id="DEADBEEF", secrets_dir=str(tmp_path))
        assert b._mode == "gnupg"
        assert b._key_id == "DEADBEEF"

    def test_init_invalid_mode_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown CapAuth mode"):
            CapAuthBackend(mode="bad-mode", secrets_dir=str(tmp_path))

    def test_init_gnupg_without_key_id_raises(self, tmp_path):
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(SecretBackendAuthError, match="CAPAUTH_KEY_ID"):
                CapAuthBackend(mode="gnupg", secrets_dir=str(tmp_path))

    def test_init_agent_defaults_to_opus(self, tmp_path):
        b = CapAuthBackend(mode="mcp", secrets_dir=str(tmp_path))
        assert b._agent == "opus"

    # ── _blob_path ────────────────────────────────────────────────────────────

    def test_blob_path(self, gnupg_backend):
        b, secrets_dir, _ = gnupg_backend
        assert b._blob_path("myapp", "prod") == secrets_dir / "prod" / "myapp.gpg"

    # ── mcp mode: _call_mcp (httpx-based) ────────────────────────────────────

    def test_call_mcp_connection_error_raises(self, tmp_path):
        """_call_mcp raises ConnectionError when skcapstone HTTP is unreachable."""
        import httpx as _httpx
        b = CapAuthBackend(mode="mcp", secrets_dir=str(tmp_path))
        mock_client_inst = MagicMock()
        mock_client_inst.post.side_effect = _httpx.ConnectError("connection refused")
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_client_inst
            MockClient.return_value.__exit__.return_value = False
            with pytest.raises(ConnectionError, match="skcapstone MCP unreachable"):
                b._call_mcp("memory_search", {})

    def test_call_mcp_http_error_raises_backend_error(self, tmp_path):
        """_call_mcp raises SecretBackendError on non-2xx HTTP status."""
        import httpx as _httpx
        b = CapAuthBackend(mode="mcp", secrets_dir=str(tmp_path))
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "internal error"
        mock_client_inst = MagicMock()
        mock_client_inst.post.return_value = mock_response
        mock_response.raise_for_status.side_effect = _httpx.HTTPStatusError(
            "500", request=MagicMock(), response=mock_response
        )
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_client_inst
            MockClient.return_value.__exit__.return_value = False
            with pytest.raises(SecretBackendError, match="skcapstone MCP HTTP"):
                b._call_mcp("memory_search", {})

    def test_call_mcp_jsonrpc_error_raises_backend_error(self, tmp_path):
        """_call_mcp raises SecretBackendError on JSON-RPC error object."""
        b = CapAuthBackend(mode="mcp", secrets_dir=str(tmp_path))
        mock_response = MagicMock()
        mock_response.raise_for_status.return_value = None
        mock_response.json.return_value = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        }
        mock_client_inst = MagicMock()
        mock_client_inst.post.return_value = mock_response
        with patch("httpx.Client") as MockClient:
            MockClient.return_value.__enter__.return_value = mock_client_inst
            MockClient.return_value.__exit__.return_value = False
            with pytest.raises(SecretBackendError, match="skcapstone MCP error"):
                b._call_mcp("memory_search", {})

    def test_get_mcp_falls_back_to_gnupg_on_connection_error(self, tmp_path):
        """In mcp mode, ConnectionError from httpx triggers silent gnupg fallback."""
        import httpx as _httpx
        secrets_dir = tmp_path / "secrets"
        (secrets_dir / "prod").mkdir(parents=True)
        (secrets_dir / "prod" / "myapp.gpg").write_text(json.dumps({"key": "val"}))
        b = CapAuthBackend(mode="mcp", secrets_dir=str(secrets_dir))
        mock_client_inst = MagicMock()
        mock_client_inst.post.side_effect = _httpx.ConnectError("refused")
        gpg_ok = MagicMock()
        gpg_ok.returncode = 0
        gpg_ok.stdout = json.dumps({"key": "val"})
        with patch("httpx.Client") as MockClient, \
             patch("subprocess.run", return_value=gpg_ok):
            MockClient.return_value.__enter__.return_value = mock_client_inst
            MockClient.return_value.__exit__.return_value = False
            result = b.get("myapp", "prod", "key")
        assert result == "val"

    # ── gnupg: _decrypt_blob_gnupg ────────────────────────────────────────────

    def test_decrypt_gnupg_happy(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", self.SECRETS)
        with patch("subprocess.run", return_value=self._gpg_ok(self.SECRETS)):
            result = b._decrypt_blob_gnupg("myapp", "prod")
        assert result == self.SECRETS

    def test_decrypt_gnupg_blob_missing_raises(self, gnupg_backend):
        b, _, _ = gnupg_backend
        with pytest.raises(SecretBackendError, match="Encrypted blob not found"):
            b._decrypt_blob_gnupg("noapp", "prod")

    def test_decrypt_gnupg_gpg_failure_raises(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {})
        with patch("subprocess.run", return_value=self._gpg_fail()):
            with pytest.raises(SecretBackendAuthError, match="GPG decryption failed"):
                b._decrypt_blob_gnupg("myapp", "prod")

    def test_decrypt_gnupg_invalid_json_raises(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {})
        bad = MagicMock()
        bad.returncode = 0
        bad.stdout = "not valid json {"
        with patch("subprocess.run", return_value=bad):
            with pytest.raises(SecretBackendError, match="not valid JSON"):
                b._decrypt_blob_gnupg("myapp", "prod")

    # ── get (gnupg) ───────────────────────────────────────────────────────────

    def test_get_gnupg_happy(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", self.SECRETS)
        with patch("subprocess.run", return_value=self._gpg_ok(self.SECRETS)):
            assert b.get("myapp", "prod", "db_pass") == "hunter2"

    def test_get_gnupg_not_found_raises(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {"other": "val"})
        with patch("subprocess.run", return_value=self._gpg_ok({"other": "val"})):
            with pytest.raises(SecretNotFoundError) as exc:
                b.get("myapp", "prod", "missing_key")
        assert exc.value.key == "missing_key"
        assert exc.value.scope == "myapp"

    # ── get_all (gnupg) ───────────────────────────────────────────────────────

    def test_get_all_gnupg(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", self.SECRETS)
        with patch("subprocess.run", return_value=self._gpg_ok(self.SECRETS)):
            assert b.get_all("myapp", "prod") == self.SECRETS

    def test_get_all_gnupg_empty(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {})
        with patch("subprocess.run", return_value=self._gpg_ok({})):
            assert b.get_all("myapp", "prod") == {}

    # ── get_with_meta (gnupg) ─────────────────────────────────────────────────

    def test_get_with_meta_gnupg(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", self.SECRETS)
        with patch("subprocess.run", return_value=self._gpg_ok(self.SECRETS)):
            value, meta = b.get_with_meta("myapp", "prod", "api_key")
        assert value == "abc123"
        assert isinstance(meta, SecretMeta)
        assert meta.key == "api_key"
        assert meta.scope == "myapp"
        assert meta.env == "prod"

    # ── set (gnupg) ───────────────────────────────────────────────────────────

    def test_set_gnupg_new_blob(self, gnupg_backend):
        """set() with no existing blob starts from empty dict."""
        b, _, env_dir = gnupg_backend
        encrypt_ok = MagicMock()
        encrypt_ok.returncode = 0
        with patch.object(b, "_load_recipients", return_value=["DEADBEEF"]), \
             patch("subprocess.run", return_value=encrypt_ok) as mock_run:
            b.set("myapp", "prod", "new_key", "new_val")
        gpg_encrypt_calls = [
            c for c in mock_run.call_args_list
            if "--encrypt" in c[0][0]
        ]
        assert len(gpg_encrypt_calls) == 1

    def test_set_gnupg_existing_blob_merges(self, gnupg_backend):
        """set() reads the existing blob, merges, then re-encrypts."""
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {"old_key": "old_val"})
        decrypt_result = self._gpg_ok({"old_key": "old_val"})
        encrypt_ok = MagicMock()
        encrypt_ok.returncode = 0
        with patch.object(b, "_load_recipients", return_value=["DEADBEEF"]), \
             patch("subprocess.run", side_effect=[decrypt_result, encrypt_ok]):
            b.set("myapp", "prod", "new_key", "new_val")

    # ── set_many (gnupg) ──────────────────────────────────────────────────────

    def test_set_many_gnupg_new_blob(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        encrypt_ok = MagicMock()
        encrypt_ok.returncode = 0
        with patch.object(b, "_load_recipients", return_value=["DEADBEEF"]), \
             patch("subprocess.run", return_value=encrypt_ok):
            b.set_many("myapp", "prod", {"k1": "v1", "k2": "v2"})

    def test_set_many_gnupg_existing_merges(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {"x": "old"})
        encrypt_ok = MagicMock()
        encrypt_ok.returncode = 0
        with patch.object(b, "_load_recipients", return_value=["DEADBEEF"]), \
             patch("subprocess.run", side_effect=[self._gpg_ok({"x": "old"}), encrypt_ok]):
            b.set_many("myapp", "prod", {"x": "new", "y": "added"})

    # ── delete (gnupg) ────────────────────────────────────────────────────────

    def test_delete_gnupg_happy(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {"keep": "me", "rm": "gone"})
        encrypt_ok = MagicMock()
        encrypt_ok.returncode = 0
        with patch.object(b, "_load_recipients", return_value=["DEADBEEF"]), \
             patch("subprocess.run", side_effect=[
                 self._gpg_ok({"keep": "me", "rm": "gone"}), encrypt_ok
             ]):
            b.delete("myapp", "prod", "rm")

    def test_delete_gnupg_not_found_raises(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {"other": "key"})
        with patch("subprocess.run", return_value=self._gpg_ok({"other": "key"})):
            with pytest.raises(SecretNotFoundError):
                b.delete("myapp", "prod", "missing")

    # ── list_keys (gnupg) ─────────────────────────────────────────────────────

    def test_list_keys_gnupg(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", self.SECRETS)
        with patch("subprocess.run", return_value=self._gpg_ok(self.SECRETS)):
            assert sorted(b.list_keys("myapp", "prod")) == ["api_key", "db_pass"]

    def test_list_keys_gnupg_empty(self, gnupg_backend):
        b, _, env_dir = gnupg_backend
        self._make_blob(env_dir, "myapp", {})
        with patch("subprocess.run", return_value=self._gpg_ok({})):
            assert b.list_keys("myapp", "prod") == []

    # ── list_scopes ───────────────────────────────────────────────────────────

    def test_list_scopes_no_env_dir(self, gnupg_backend):
        b, _, _ = gnupg_backend
        assert b.list_scopes("staging") == []

    def test_list_scopes(self, gnupg_backend):
        b, secrets_dir, _ = gnupg_backend
        env_dir = secrets_dir / "staging"
        env_dir.mkdir()
        (env_dir / "appA.gpg").touch()
        (env_dir / "appB.gpg").touch()
        assert sorted(b.list_scopes("staging")) == ["appA", "appB"]

    def test_list_scopes_ignores_non_gpg_files(self, gnupg_backend):
        b, secrets_dir, _ = gnupg_backend
        env_dir = secrets_dir / "staging"
        env_dir.mkdir()
        (env_dir / "myapp.gpg").touch()
        (env_dir / "README.md").touch()
        assert b.list_scopes("staging") == ["myapp"]

    # ── _load_recipients ──────────────────────────────────────────────────────

    def test_load_recipients_from_capauth_yaml(self, gnupg_backend):
        import yaml
        b, secrets_dir, _ = gnupg_backend
        config = {
            "prod": {
                "recipients": [
                    {"fingerprint": "AABBCCDD"},
                    {"fingerprint": "EEFF0011"},
                ]
            }
        }
        config_file = secrets_dir.parent / "capauth.yaml"
        config_file.write_text(yaml.dump(config))
        assert b._load_recipients("prod") == ["AABBCCDD", "EEFF0011"]

    def test_load_recipients_yaml_default_env_fallback(self, gnupg_backend):
        import yaml
        b, secrets_dir, _ = gnupg_backend
        config = {
            "default": {
                "recipients": [{"fingerprint": "DEFAULTFP"}]
            }
        }
        config_file = secrets_dir.parent / "capauth.yaml"
        config_file.write_text(yaml.dump(config))
        assert b._load_recipients("staging") == ["DEFAULTFP"]

    def test_load_recipients_fallback_to_key_id(self, gnupg_backend):
        """No capauth.yaml, fall back to key_id."""
        b, _, _ = gnupg_backend
        assert b._load_recipients("prod") == ["DEADBEEF"]

    def test_load_recipients_no_yaml_no_key_id_raises(self, gnupg_backend):
        b, _, _ = gnupg_backend
        b._key_id = None  # force missing key_id after construction
        with pytest.raises(SecretBackendError, match="capauth.yaml not found"):
            b._load_recipients("prod")

    def test_load_recipients_yaml_no_env_entry_raises(self, gnupg_backend):
        import yaml
        b, secrets_dir, _ = gnupg_backend
        config = {"other_env": {"recipients": [{"fingerprint": "FP"}]}}
        (secrets_dir.parent / "capauth.yaml").write_text(yaml.dump(config))
        with pytest.raises(SecretBackendError, match="No recipients found"):
            b._load_recipients("prod")

    # ── health_check ──────────────────────────────────────────────────────────

    def test_health_check_gnupg_ok(self, gnupg_backend):
        b, _, _ = gnupg_backend
        with patch("subprocess.run", return_value=MagicMock(returncode=0)):
            result = b.health_check()
        assert result["status"] == "ok"
        assert result["backend"] == "capauth"
        assert result["mode"] == "gnupg"
        assert result["gnupg"] == "available"

    def test_health_check_gnupg_degraded(self, gnupg_backend):
        b, _, _ = gnupg_backend
        with patch("subprocess.run", side_effect=FileNotFoundError):
            result = b.health_check()
        assert result["status"] == "degraded"
        assert result["gnupg"] == "not found"

    def test_health_check_gnupg_degraded_on_error(self, gnupg_backend):
        b, _, _ = gnupg_backend
        err = subprocess.CalledProcessError(1, "gpg")
        with patch("subprocess.run", side_effect=err):
            result = b.health_check()
        assert result["status"] == "degraded"

    def test_health_check_mcp_reachable(self, tmp_path):
        """MCP health check: HTTP server reachable → status ok."""
        b = CapAuthBackend(mode="mcp", secrets_dir=str(tmp_path))
        mock_client_inst = MagicMock()
        mock_client_inst.post.return_value = MagicMock()
        with patch("httpx.Client") as MockClient, \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            MockClient.return_value.__enter__.return_value = mock_client_inst
            MockClient.return_value.__exit__.return_value = False
            result = b.health_check()
        assert result["status"] == "ok"
        assert result["mcp_reachable"] is True
        assert result["backend"] == "capauth"
        assert result["mode"] == "mcp"

    def test_health_check_mcp_unreachable_gpg_ok(self, tmp_path):
        """MCP health check: server unreachable, GPG available → degraded."""
        import httpx as _httpx
        b = CapAuthBackend(mode="mcp", secrets_dir=str(tmp_path))
        mock_client_inst = MagicMock()
        mock_client_inst.post.side_effect = _httpx.ConnectError("refused")
        with patch("httpx.Client") as MockClient, \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            MockClient.return_value.__enter__.return_value = mock_client_inst
            MockClient.return_value.__exit__.return_value = False
            result = b.health_check()
        assert result["status"] == "degraded"
        assert result["mcp_reachable"] is False
        assert result["gnupg_fallback"] is True

    def test_health_check_mcp_reports_agent_name(self, tmp_path):
        import httpx as _httpx
        b = CapAuthBackend(
            mode="mcp",
            agent_name="my-agent",
            secrets_dir=str(tmp_path),
        )
        mock_client_inst = MagicMock()
        mock_client_inst.post.side_effect = _httpx.ConnectError("refused")
        with patch("httpx.Client") as MockClient, \
             patch("subprocess.run", return_value=MagicMock(returncode=0)):
            MockClient.return_value.__enter__.return_value = mock_client_inst
            MockClient.return_value.__exit__.return_value = False
            result = b.health_check()
        assert result["agent"] == "my-agent"

    # ── context manager ───────────────────────────────────────────────────────

    def test_context_manager(self, tmp_path):
        with CapAuthBackend(mode="mcp", secrets_dir=str(tmp_path)) as b:
            assert b is not None
