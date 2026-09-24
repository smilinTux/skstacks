"""
SKStacks v2 — Secret Backend Interface
=======================================

All secret backends implement SKSecretBackend.  The deploy tooling only
calls this interface; backends are swappable at runtime via the factory.

Usage:
    from secrets.factory import get_backend

    backend = get_backend()          # reads SKSTACKS_SECRET_BACKEND env var
    token = backend.get("skfence", "prod", "cloudflare_dns_token")
    backend.set("skfence", "prod", "cloudflare_dns_token", new_token)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SecretMeta:
    """Metadata returned alongside a secret value."""
    key: str
    scope: str
    env: str
    version: Optional[str] = None
    created_at: Optional[str] = None
    expires_at: Optional[str] = None
    rotated_at: Optional[str] = None
    tags: list[str] = field(default_factory=list)


class SKSecretBackend(ABC):
    """
    Abstract base for all SKStacks secret backends.

    Implementations:
      - VaultFileBackend   (vault-file/backend.py)
      - HashiCorpVaultBackend (hashicorp-vault/backend.py)
      - CapAuthBackend     (capauth/backend.py)
    """

    # ── Read ──────────────────────────────────────────────────────────────────

    @abstractmethod
    def get(self, scope: str, env: str, key: str) -> str:
        """
        Retrieve a single secret value.

        Args:
            scope: Service scope, e.g. "skfence", "sksec", "my-app"
            env:   Environment, e.g. "prod", "staging", "dev"
            key:   Secret key, e.g. "cloudflare_dns_token"

        Returns:
            Plaintext secret value (string).

        Raises:
            SecretNotFoundError: if the key does not exist.
            SecretBackendError:  on backend connectivity/auth failures.
        """

    @abstractmethod
    def get_all(self, scope: str, env: str) -> dict[str, str]:
        """
        Retrieve all secrets for a scope/env as a flat dict.

        Useful for rendering Jinja2 templates in a single pass.
        """

    @abstractmethod
    def get_with_meta(self, scope: str, env: str, key: str) -> tuple[str, SecretMeta]:
        """Return (value, metadata) for a key."""

    # ── Write ─────────────────────────────────────────────────────────────────

    @abstractmethod
    def set(self, scope: str, env: str, key: str, value: str) -> None:
        """
        Store or update a secret.

        The old value (if any) is archived / versioned by the backend.
        """

    @abstractmethod
    def set_many(self, scope: str, env: str, secrets: dict[str, str]) -> None:
        """Bulk-write multiple secrets in one atomic operation (where possible)."""

    # ── Delete ────────────────────────────────────────────────────────────────

    @abstractmethod
    def delete(self, scope: str, env: str, key: str) -> None:
        """Permanently delete a secret key."""

    # ── Discovery ─────────────────────────────────────────────────────────────

    @abstractmethod
    def list_keys(self, scope: str, env: str) -> list[str]:
        """List all key names for a scope/env (values not returned)."""

    @abstractmethod
    def list_scopes(self, env: str) -> list[str]:
        """List all scopes registered for an environment."""

    # ── Rotation ──────────────────────────────────────────────────────────────

    def rotate(self, scope: str, env: str, key: str) -> str:
        """
        Generate a new value for a key and store it.

        Default implementation raises NotImplementedError.
        Backends that support dynamic/auto-rotation override this.

        Returns:
            The newly generated value.
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not support automatic rotation. "
            "Rotate manually and call set()."
        )

    # ── Health ────────────────────────────────────────────────────────────────

    @abstractmethod
    def health_check(self) -> dict:
        """
        Return backend health information.

        Returns dict with at minimum:
            {"status": "ok" | "degraded" | "unavailable", "backend": str, "details": str}
        """

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        """Release any backend resources (connections, file handles, etc.)."""


# ── Exceptions ────────────────────────────────────────────────────────────────

class SecretBackendError(Exception):
    """Base exception for all secret backend errors."""


class SecretNotFoundError(SecretBackendError):
    """Raised when a requested key does not exist in the backend."""
    def __init__(self, scope: str, env: str, key: str):
        super().__init__(f"Secret not found: {env}/{scope}/{key}")
        self.scope = scope
        self.env = env
        self.key = key


class SecretBackendAuthError(SecretBackendError):
    """Raised when the backend rejects authentication."""


class SecretBackendUnavailableError(SecretBackendError):
    """Raised when the backend server is unreachable."""
