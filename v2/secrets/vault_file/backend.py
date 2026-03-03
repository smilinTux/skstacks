"""
SKStacks v2 — Vault-File Backend
=================================

Wraps Ansible Vault (AES-256-GCM) encrypted YAML files as the secret store.
This is the zero-extra-infra option and the migration path from SKStacks v1.

Dependencies:
    pip install ansible-core   # or: ansible

Environment variables:
    SKSTACKS_VAULT_DIR         Path to the directory containing vault YAML files.
                               Default: ~/.skstacks/vaults
    SKSTACKS_VAULT_PASS_DIR    Path to the directory containing vault password files.
                               Default: ~/.vault_pass_env
    SKSTACKS_VAULT_PASS_CMD    Shell command that outputs the vault password to stdout.
                               Takes precedence over password files when set.
                               Example: "secret-tool lookup service skstacks env prod"

Vault file layout:
    {vault_dir}/{env}/{scope}-{env}_vault.yml   (ansible-vault encrypted)

Password file layout:
    {vault_pass_dir}/.{scope}_{env}_vault_pass  (plaintext, mode 600, NOT in git)
    {vault_pass_dir}/.{env}_vault_pass          (fallback shared password per env)

Vault YAML structure (when decrypted):
    vault_{scope}_{key}: "value"
    ...
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..interface import (
    SKSecretBackend,
    SecretMeta,
    SecretNotFoundError,
    SecretBackendError,
)


class VaultFileBackend(SKSecretBackend):
    """Ansible-vault encrypted YAML file backend."""

    def __init__(
        self,
        vault_dir: Optional[str] = None,
        vault_pass_dir: Optional[str] = None,
        vault_pass_cmd: Optional[str] = None,
    ):
        self._vault_dir = Path(
            vault_dir or os.environ.get("SKSTACKS_VAULT_DIR", "~/.skstacks/vaults")
        ).expanduser()
        self._vault_pass_dir = Path(
            vault_pass_dir
            or os.environ.get("SKSTACKS_VAULT_PASS_DIR", "~/.vault_pass_env")
        ).expanduser()
        self._vault_pass_cmd = vault_pass_cmd or os.environ.get("SKSTACKS_VAULT_PASS_CMD")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _vault_path(self, scope: str, env: str) -> Path:
        return self._vault_dir / env / f"{scope}-{env}_vault.yml"

    def _pass_path(self, scope: str, env: str) -> Optional[Path]:
        """Try scope-specific password file, fall back to env-wide password file."""
        scope_pass = self._vault_pass_dir / f".{scope}_{env}_vault_pass"
        if scope_pass.exists():
            return scope_pass
        env_pass = self._vault_pass_dir / f".{env}_vault_pass"
        if env_pass.exists():
            return env_pass
        return None

    def _decrypt(self, scope: str, env: str) -> dict[str, str]:
        """Decrypt a vault YAML file and return its contents as a flat dict."""
        vault_path = self._vault_path(scope, env)
        if not vault_path.exists():
            raise SecretBackendError(
                f"Vault file not found: {vault_path}. "
                f"Run vault_init.yml to create it."
            )

        cmd = ["ansible-vault", "decrypt", "--output=-", str(vault_path)]
        env_override = dict(os.environ)

        if self._vault_pass_cmd:
            # Write password from command to temp file
            pass_bytes = subprocess.check_output(
                self._vault_pass_cmd, shell=True
            ).strip()
            with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp:
                tmp.write(pass_bytes)
                tmp_path = tmp.name
            cmd = ["ansible-vault", "decrypt", "--vault-password-file", tmp_path,
                   "--output=-", str(vault_path)]
        else:
            pass_path = self._pass_path(scope, env)
            if pass_path is None:
                raise SecretBackendError(
                    f"No vault password file found for scope={scope!r} env={env!r}. "
                    f"Expected: {self._vault_pass_dir}/.{scope}_{env}_vault_pass"
                )
            cmd = ["ansible-vault", "decrypt",
                   "--vault-password-file", str(pass_path),
                   "--output=-", str(vault_path)]

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, check=True,
                env=env_override
            )
        except subprocess.CalledProcessError as exc:
            raise SecretBackendError(
                f"Failed to decrypt vault {vault_path}: {exc.stderr}"
            ) from exc
        finally:
            if self._vault_pass_cmd and "tmp_path" in locals():
                os.unlink(tmp_path)

        # Parse simple key: value YAML (no nested structures expected)
        import yaml  # type: ignore[import-untyped]
        data = yaml.safe_load(result.stdout) or {}
        # Flatten: vault_{scope}_{key} → {key}
        prefix = f"vault_{scope}_"
        return {
            k[len(prefix):] if k.startswith(prefix) else k: str(v)
            for k, v in data.items()
            if v is not None
        }

    # ── SKSecretBackend interface ─────────────────────────────────────────────

    def get(self, scope: str, env: str, key: str) -> str:
        secrets = self._decrypt(scope, env)
        if key not in secrets:
            raise SecretNotFoundError(scope, env, key)
        return secrets[key]

    def get_all(self, scope: str, env: str) -> dict[str, str]:
        return self._decrypt(scope, env)

    def get_with_meta(self, scope: str, env: str, key: str) -> tuple[str, SecretMeta]:
        value = self.get(scope, env, key)
        meta = SecretMeta(key=key, scope=scope, env=env)
        return value, meta

    def set(self, scope: str, env: str, key: str, value: str) -> None:
        """
        Update a single key inside an existing encrypted vault.

        Reads the whole vault, updates the key, re-encrypts in place.
        """
        vault_path = self._vault_path(scope, env)
        secrets = self._decrypt(scope, env) if vault_path.exists() else {}
        secrets[key] = value
        self._encrypt(scope, env, secrets)

    def set_many(self, scope: str, env: str, secrets: dict[str, str]) -> None:
        vault_path = self._vault_path(scope, env)
        existing = self._decrypt(scope, env) if vault_path.exists() else {}
        existing.update(secrets)
        self._encrypt(scope, env, existing)

    def _encrypt(self, scope: str, env: str, secrets: dict[str, str]) -> None:
        import yaml  # type: ignore[import-untyped]

        vault_path = self._vault_path(scope, env)
        vault_path.parent.mkdir(parents=True, exist_ok=True)

        # Re-add the vault_ prefix convention
        prefixed = {f"vault_{scope}_{k}": v for k, v in secrets.items()}

        with tempfile.NamedTemporaryFile(mode="w", suffix=".yml", delete=False) as tmp:
            yaml.dump(prefixed, tmp, default_flow_style=False, allow_unicode=True)
            tmp_path = tmp.name

        try:
            pass_path = self._pass_path(scope, env)
            if pass_path is None:
                raise SecretBackendError(
                    f"No vault password file found for scope={scope!r} env={env!r}."
                )
            subprocess.run(
                ["ansible-vault", "encrypt",
                 "--vault-password-file", str(pass_path),
                 "--output", str(vault_path),
                 tmp_path],
                check=True, capture_output=True
            )
        finally:
            os.unlink(tmp_path)

    def delete(self, scope: str, env: str, key: str) -> None:
        secrets = self._decrypt(scope, env)
        if key not in secrets:
            raise SecretNotFoundError(scope, env, key)
        del secrets[key]
        self._encrypt(scope, env, secrets)

    def list_keys(self, scope: str, env: str) -> list[str]:
        return list(self._decrypt(scope, env).keys())

    def list_scopes(self, env: str) -> list[str]:
        env_dir = self._vault_dir / env
        if not env_dir.exists():
            return []
        return [
            p.name.removesuffix(f"-{env}_vault.yml")
            for p in env_dir.glob(f"*-{env}_vault.yml")
        ]

    def health_check(self) -> dict:
        ansible_ok = True
        try:
            subprocess.run(
                ["ansible-vault", "--version"],
                capture_output=True, check=True
            )
        except (subprocess.CalledProcessError, FileNotFoundError):
            ansible_ok = False

        return {
            "status": "ok" if ansible_ok else "degraded",
            "backend": "vault-file",
            "ansible_vault": "available" if ansible_ok else "not found",
            "vault_dir": str(self._vault_dir),
            "vault_dir_exists": self._vault_dir.exists(),
        }
