"""
SKStacks v2 — HashiCorp Vault Backend
======================================

Uses the HashiCorp Vault HTTP API (KV v2 engine) as the secret store.
Supports AppRole and Kubernetes auth methods.

Dependencies:
    pip install hvac   # HashiCorp Vault Python client

Environment variables:
    VAULT_ADDR          Vault server URL, e.g. https://vault.your-domain.com:8200
    VAULT_TOKEN         Direct token auth (development / bootstrap only)
    VAULT_ROLE_ID       AppRole role_id (recommended for automation)
    VAULT_SECRET_ID     AppRole secret_id
    VAULT_K8S_ROLE      Kubernetes auth role (used when running inside a K8s pod)
    VAULT_MOUNT         KV-v2 mount path (default: "kv")
    VAULT_PATH_PREFIX   Path prefix inside the mount (default: "skstacks")
    VAULT_NAMESPACE     Vault Enterprise namespace (optional)
    VAULT_SKIP_VERIFY   Set to "true" to disable TLS verification (dev only)

KV path convention:
    {mount}/data/{prefix}/{env}/{scope}/{key}
    e.g. kv/data/skstacks/prod/skfence/cloudflare_dns_token
"""

from __future__ import annotations

import os
from typing import Optional

from ..interface import (
    SKSecretBackend,
    SecretMeta,
    SecretNotFoundError,
    SecretBackendError,
    SecretBackendAuthError,
    SecretBackendUnavailableError,
)


class HashiCorpVaultBackend(SKSecretBackend):
    """HashiCorp Vault KV-v2 secret backend."""

    def __init__(
        self,
        addr: Optional[str] = None,
        token: Optional[str] = None,
        role_id: Optional[str] = None,
        secret_id: Optional[str] = None,
        k8s_role: Optional[str] = None,
        mount: str = "kv",
        path_prefix: str = "skstacks",
        namespace: Optional[str] = None,
        skip_verify: bool = False,
    ):
        try:
            import hvac  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ImportError(
                "hvac is required for the hashicorp-vault backend. "
                "Install it: pip install hvac"
            ) from exc

        self._mount = mount or os.environ.get("VAULT_MOUNT", "kv")
        self._prefix = path_prefix or os.environ.get("VAULT_PATH_PREFIX", "skstacks")
        self._namespace = namespace or os.environ.get("VAULT_NAMESPACE")

        vault_addr = addr or os.environ.get("VAULT_ADDR", "https://127.0.0.1:8200")
        vault_token = token or os.environ.get("VAULT_TOKEN")
        _role_id = role_id or os.environ.get("VAULT_ROLE_ID")
        _secret_id = secret_id or os.environ.get("VAULT_SECRET_ID")
        _k8s_role = k8s_role or os.environ.get("VAULT_K8S_ROLE")
        _skip_verify = skip_verify or os.environ.get("VAULT_SKIP_VERIFY", "").lower() == "true"

        self._client = hvac.Client(
            url=vault_addr,
            namespace=self._namespace,
            verify=not _skip_verify,
        )

        # Auth priority: token > AppRole > K8s
        if vault_token:
            self._client.token = vault_token
        elif _role_id and _secret_id:
            resp = self._client.auth.approle.login(
                role_id=_role_id,
                secret_id=_secret_id,
            )
            self._client.token = resp["auth"]["client_token"]
        elif _k8s_role:
            # Read the service account JWT from the pod
            sa_jwt_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
            try:
                with open(sa_jwt_path) as f:
                    jwt = f.read().strip()
            except FileNotFoundError as exc:
                raise SecretBackendAuthError(
                    "Kubernetes auth requested but not running inside a K8s pod. "
                    "Set VAULT_ROLE_ID + VAULT_SECRET_ID for AppRole auth instead."
                ) from exc
            resp = self._client.auth.kubernetes.login(role=_k8s_role, jwt=jwt)
            self._client.token = resp["auth"]["client_token"]
        else:
            raise SecretBackendAuthError(
                "No Vault auth credentials provided. Set one of: "
                "VAULT_TOKEN, VAULT_ROLE_ID+VAULT_SECRET_ID, VAULT_K8S_ROLE"
            )

        if not self._client.is_authenticated():
            raise SecretBackendAuthError("Vault authentication failed.")

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _kv_path(self, scope: str, env: str, key: Optional[str] = None) -> str:
        """Build the KV path for a secret or scope."""
        parts = [self._prefix, env, scope]
        if key:
            parts.append(key)
        return "/".join(parts)

    def _read_kv(self, scope: str, env: str, key: str) -> tuple[str, dict]:
        """Read a KV-v2 secret; returns (value, metadata dict)."""
        import hvac.exceptions  # type: ignore[import-untyped]

        path = self._kv_path(scope, env, key)
        try:
            resp = self._client.secrets.kv.v2.read_secret_version(
                path=path,
                mount_point=self._mount,
                raise_on_deleted_version=True,
            )
        except hvac.exceptions.InvalidPath:
            raise SecretNotFoundError(scope, env, key)
        except hvac.exceptions.Forbidden as exc:
            raise SecretBackendAuthError(
                f"Permission denied reading {self._mount}/{path}"
            ) from exc

        data = resp["data"]["data"] or {}
        meta = resp["data"]["metadata"]
        if "value" not in data:
            raise SecretBackendError(
                f"Vault secret at {path} does not have a 'value' key. "
                f"Found keys: {list(data.keys())}"
            )
        return data["value"], meta

    # ── SKSecretBackend interface ─────────────────────────────────────────────

    def get(self, scope: str, env: str, key: str) -> str:
        value, _ = self._read_kv(scope, env, key)
        return value

    def get_all(self, scope: str, env: str) -> dict[str, str]:
        keys = self.list_keys(scope, env)
        return {k: self.get(scope, env, k) for k in keys}

    def get_with_meta(self, scope: str, env: str, key: str) -> tuple[str, SecretMeta]:
        value, raw_meta = self._read_kv(scope, env, key)
        meta = SecretMeta(
            key=key, scope=scope, env=env,
            version=str(raw_meta.get("version", "")),
            created_at=raw_meta.get("created_time"),
        )
        return value, meta

    def set(self, scope: str, env: str, key: str, value: str) -> None:
        path = self._kv_path(scope, env, key)
        self._client.secrets.kv.v2.create_or_update_secret(
            path=path,
            secret={"value": value},
            mount_point=self._mount,
        )

    def set_many(self, scope: str, env: str, secrets: dict[str, str]) -> None:
        for key, value in secrets.items():
            self.set(scope, env, key, value)

    def delete(self, scope: str, env: str, key: str) -> None:
        path = self._kv_path(scope, env, key)
        self._client.secrets.kv.v2.delete_metadata_and_all_versions(
            path=path,
            mount_point=self._mount,
        )

    def list_keys(self, scope: str, env: str) -> list[str]:
        import hvac.exceptions  # type: ignore[import-untyped]

        path = self._kv_path(scope, env)
        try:
            resp = self._client.secrets.kv.v2.list_secrets(
                path=path,
                mount_point=self._mount,
            )
        except hvac.exceptions.InvalidPath:
            return []
        return resp["data"].get("keys", [])

    def list_scopes(self, env: str) -> list[str]:
        import hvac.exceptions  # type: ignore[import-untyped]

        path = f"{self._prefix}/{env}"
        try:
            resp = self._client.secrets.kv.v2.list_secrets(
                path=path,
                mount_point=self._mount,
            )
        except hvac.exceptions.InvalidPath:
            return []
        return [k.rstrip("/") for k in resp["data"].get("keys", [])]

    def rotate(self, scope: str, env: str, key: str) -> str:
        """
        For static secrets, generates a new random 32-byte hex value.
        For database/PKI dynamic secrets, use Vault's dedicated engines instead.
        """
        import secrets as _secrets
        new_value = _secrets.token_hex(32)
        self.set(scope, env, key, new_value)
        return new_value

    def health_check(self) -> dict:
        try:
            status = self._client.sys.read_health_status(method="GET")
            return {
                "status": "ok" if not status.get("sealed") else "degraded",
                "backend": "hashicorp-vault",
                "initialized": status.get("initialized"),
                "sealed": status.get("sealed"),
                "version": status.get("version"),
                "cluster": status.get("cluster_name"),
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "unavailable",
                "backend": "hashicorp-vault",
                "error": str(exc),
            }

    def close(self):
        # hvac client does not need explicit teardown
        pass
