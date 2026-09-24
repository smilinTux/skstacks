"""
SKStacks v2 — SOPS + age Backend
=================================

A server-less sibling of the vault-file backend for the **GitOps lane**:
secrets are stored as `sops`-encrypted YAML (age recipients), safe to commit to
git, and decrypted in-memory by CI / at deploy time. Like vault-file and capauth,
it needs no running secret server — so it carries no bootstrap catch-22.

When to use vs vault-file:
  - vault-file  → Ansible-driven node/Swarm deploys (ansible-vault native).
  - sops-age    → GitOps/ArgoCD/Flux + Kustomize lanes (age keys, per-file
                  recipients, partial-file encryption, clean git diffs).

File layout:
    {secrets_dir}/{env}/{scope}-{env}.sops.yaml    (sops/age-encrypted)

Environment variables:
    SKSTACKS_SOPS_DIR    secrets dir (default: ~/.skstacks/sops)
    SOPS_AGE_KEY_FILE    age identity file used by `sops` to decrypt
    SOPS_AGE_RECIPIENTS  comma-separated age public keys for `sops` to encrypt to

The crypto layer is injectable (_decrypt / _encrypt) for testing and to allow
alternative SOPS invocations; the defaults shell out to the `sops` binary.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from ..interface import (
    SKSecretBackend,
    SecretMeta,
    SecretNotFoundError,
    SecretBackendError,
)

_BACKEND_NAME = "sops-age"


class SopsAgeBackend(SKSecretBackend):
    """SOPS+age encrypted-YAML secret backend (server-less, GitOps-friendly)."""

    def __init__(
        self,
        secrets_dir: Optional[str] = None,
        age_key_file: Optional[str] = None,
        age_recipients: Optional[str] = None,
        _decrypt: Optional[Callable[[Path], dict]] = None,
        _encrypt: Optional[Callable[[Path, dict], None]] = None,
    ):
        self._dir = Path(
            secrets_dir or os.environ.get("SKSTACKS_SOPS_DIR", "~/.skstacks/sops")
        ).expanduser()
        self._age_key_file = age_key_file or os.environ.get("SOPS_AGE_KEY_FILE")
        self._age_recipients = age_recipients or os.environ.get("SOPS_AGE_RECIPIENTS")
        self._decrypt = _decrypt or self._sops_decrypt
        self._encrypt = _encrypt or self._sops_encrypt

    # ── path ──────────────────────────────────────────────────────────────────
    def _path(self, scope: str, env: str) -> Path:
        return self._dir / env / f"{scope}-{env}.sops.yaml"

    def _load(self, scope: str, env: str) -> dict[str, str]:
        """Decrypt a scope/env file. Raises SecretBackendError if it doesn't exist."""
        path = self._path(scope, env)
        try:
            data = self._decrypt(path)
        except FileNotFoundError as exc:
            raise SecretBackendError(
                f"SOPS file not found: {path}. Create it with `sops {path}`."
            ) from exc
        return {k: str(v) for k, v in (data or {}).items() if v is not None}

    def _load_or_empty(self, scope: str, env: str) -> dict[str, str]:
        try:
            return self._load(scope, env)
        except SecretBackendError:
            return {}

    # ── default sops crypto (shelled out) ──────────────────────────────────────
    def _sops_decrypt(self, path: Path) -> dict:
        import yaml  # type: ignore[import-untyped]

        if not path.exists():
            raise FileNotFoundError(path)
        env = dict(os.environ)
        if self._age_key_file:
            env["SOPS_AGE_KEY_FILE"] = str(Path(self._age_key_file).expanduser())
        try:
            out = subprocess.run(
                ["sops", "--decrypt", "--output-type", "yaml", str(path)],
                capture_output=True, text=True, check=True, env=env,
            ).stdout
        except subprocess.CalledProcessError as exc:
            raise SecretBackendError(f"sops decrypt failed for {path}: {exc.stderr}") from exc
        return yaml.safe_load(out) or {}

    def _sops_encrypt(self, path: Path, data: dict) -> None:
        import yaml  # type: ignore[import-untyped]

        path.parent.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        if self._age_key_file:
            env["SOPS_AGE_KEY_FILE"] = str(Path(self._age_key_file).expanduser())
        recipients = self._age_recipients
        if not recipients:
            raise SecretBackendError(
                "SOPS_AGE_RECIPIENTS (age public keys) required to encrypt."
            )
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as tmp:
            yaml.dump(data, tmp, default_flow_style=False, allow_unicode=True)
            tmp_path = tmp.name
        try:
            subprocess.run(
                ["sops", "--encrypt", "--age", recipients,
                 "--output", str(path), tmp_path],
                check=True, capture_output=True, text=True, env=env,
            )
        except subprocess.CalledProcessError as exc:
            raise SecretBackendError(f"sops encrypt failed for {path}: {exc.stderr}") from exc
        finally:
            os.unlink(tmp_path)

    # ── SKSecretBackend interface ──────────────────────────────────────────────
    def get(self, scope: str, env: str, key: str) -> str:
        data = self._load(scope, env)
        if key not in data:
            raise SecretNotFoundError(scope, env, key)
        return data[key]

    def get_all(self, scope: str, env: str) -> dict[str, str]:
        return self._load(scope, env)

    def get_with_meta(self, scope: str, env: str, key: str) -> tuple[str, SecretMeta]:
        return self.get(scope, env, key), SecretMeta(key=key, scope=scope, env=env)

    def set(self, scope: str, env: str, key: str, value: str) -> None:
        data = self._load_or_empty(scope, env)
        data[key] = value
        self._encrypt(self._path(scope, env), data)

    def set_many(self, scope: str, env: str, secrets: dict[str, str]) -> None:
        data = self._load_or_empty(scope, env)
        data.update(secrets)
        self._encrypt(self._path(scope, env), data)

    def delete(self, scope: str, env: str, key: str) -> None:
        data = self._load(scope, env)
        if key not in data:
            raise SecretNotFoundError(scope, env, key)
        del data[key]
        self._encrypt(self._path(scope, env), data)

    def list_keys(self, scope: str, env: str) -> list[str]:
        return list(self._load(scope, env).keys())

    def list_scopes(self, env: str) -> list[str]:
        env_dir = self._dir / env
        if not env_dir.is_dir():
            return []
        suffix = f"-{env}.sops.yaml"
        return sorted(
            p.name[: -len(suffix)] for p in env_dir.glob(f"*{suffix}")
        )

    def health_check(self) -> dict:
        return {
            "status": "ok",
            "backend": _BACKEND_NAME,
            "details": f"secrets_dir={self._dir}",
        }

    def close(self):
        pass
