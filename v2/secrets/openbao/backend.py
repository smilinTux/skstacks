"""
SKStacks v2 — OpenBao Backend
==============================

OpenBao is the Linux Foundation MPL-2.0 fork of HashiCorp Vault, built from
Vault's last MPL release. It is wire/API-compatible (KV-v2, AppRole, Kubernetes
auth, HA Raft), so this adapter reuses the HashiCorp client logic and only:
  - identifies as the "openbao" backend, and
  - prefers BAO_* environment variables (falling back to the VAULT_* names,
    which OpenBao's own CLI/SDK also accept for drop-in compatibility).

This is the **default server backend** for SKStacks v2 (sovereign, OSS, no BSL).
HashiCorp Vault remains available via the "hashicorp-vault" adapter for anyone
who needs Vault Enterprise (cross-cluster Performance/DR replication, FIPS).

Environment variables (BAO_* preferred, VAULT_* honoured as fallback):
    BAO_ADDR / VAULT_ADDR            server URL (default https://127.0.0.1:8200)
    BAO_TOKEN / VAULT_TOKEN          direct token (bootstrap/dev only)
    BAO_ROLE_ID / VAULT_ROLE_ID      AppRole role_id (automation)
    BAO_SECRET_ID / VAULT_SECRET_ID  AppRole secret_id
    BAO_K8S_ROLE / VAULT_K8S_ROLE    Kubernetes auth role (in-pod)
    BAO_MOUNT / VAULT_MOUNT          KV-v2 mount (default "kv")
    BAO_PATH_PREFIX / VAULT_PATH_PREFIX   path prefix (default "skstacks")
    BAO_NAMESPACE / VAULT_NAMESPACE  namespace (optional)
    BAO_SKIP_VERIFY / VAULT_SKIP_VERIFY   "true" to disable TLS verify (dev only)
"""
from __future__ import annotations

import os
from typing import Optional

from ..hashicorp_vault.backend import HashiCorpVaultBackend

_BACKEND_NAME = "openbao"


def _env(bao_key: str, vault_key: str) -> Optional[str]:
    """BAO_* takes precedence, then the VAULT_* compatibility name."""
    val = os.environ.get(bao_key)
    return val if val is not None else os.environ.get(vault_key)


class OpenBaoBackend(HashiCorpVaultBackend):
    """OpenBao KV-v2 secret backend (wire-compatible with HashiCorp Vault)."""

    def __init__(
        self,
        addr: Optional[str] = None,
        token: Optional[str] = None,
        role_id: Optional[str] = None,
        secret_id: Optional[str] = None,
        k8s_role: Optional[str] = None,
        mount: Optional[str] = None,
        path_prefix: Optional[str] = None,
        namespace: Optional[str] = None,
        skip_verify: bool = False,
    ):
        super().__init__(
            addr=addr or _env("BAO_ADDR", "VAULT_ADDR"),
            token=token or _env("BAO_TOKEN", "VAULT_TOKEN"),
            role_id=role_id or _env("BAO_ROLE_ID", "VAULT_ROLE_ID"),
            secret_id=secret_id or _env("BAO_SECRET_ID", "VAULT_SECRET_ID"),
            k8s_role=k8s_role or _env("BAO_K8S_ROLE", "VAULT_K8S_ROLE"),
            mount=mount or _env("BAO_MOUNT", "VAULT_MOUNT") or "kv",
            path_prefix=path_prefix or _env("BAO_PATH_PREFIX", "VAULT_PATH_PREFIX") or "skstacks",
            namespace=namespace or _env("BAO_NAMESPACE", "VAULT_NAMESPACE"),
            skip_verify=skip_verify
            or (_env("BAO_SKIP_VERIFY", "VAULT_SKIP_VERIFY") or "").lower() == "true",
        )

    def health_check(self) -> dict:
        result = super().health_check()
        result["backend"] = _BACKEND_NAME
        return result
