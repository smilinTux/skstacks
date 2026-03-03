"""
SKStacks v2 — Secret Backend Factory
=====================================

Reads SKSTACKS_SECRET_BACKEND (or explicit argument) and returns a fully
configured SKSecretBackend instance.

Usage:
    from secrets.factory import get_backend

    # From environment variable SKSTACKS_SECRET_BACKEND
    backend = get_backend()

    # Explicit backend
    backend = get_backend("hashicorp-vault")

    # With custom config
    backend = get_backend("capauth", config={"pgp_fingerprint": "DEADBEEF..."})
"""

from __future__ import annotations

import os
from typing import Optional

from .interface import SKSecretBackend

# Lazy imports — only the selected backend module is loaded.
_BACKEND_MAP: dict[str, str] = {
    "vault-file":       "secrets.vault_file.backend.VaultFileBackend",
    "hashicorp-vault":  "secrets.hashicorp_vault.backend.HashiCorpVaultBackend",
    "capauth":          "secrets.capauth.backend.CapAuthBackend",
}

_DEFAULT_BACKEND = "vault-file"


def get_backend(
    name: Optional[str] = None,
    config: Optional[dict] = None,
) -> SKSecretBackend:
    """
    Instantiate and return the requested secret backend.

    Args:
        name:   Backend identifier. If None, reads SKSTACKS_SECRET_BACKEND env var.
                One of: "vault-file", "hashicorp-vault", "capauth"
        config: Optional dict of backend-specific config overrides.
                Falls back to environment variables when not provided.

    Returns:
        Configured SKSecretBackend instance.

    Raises:
        ValueError:  if an unknown backend name is given.
        ImportError: if a backend's optional dependencies are not installed.
    """
    if name is None:
        name = os.environ.get("SKSTACKS_SECRET_BACKEND", _DEFAULT_BACKEND)

    name = name.lower().strip()
    if name not in _BACKEND_MAP:
        raise ValueError(
            f"Unknown secret backend: {name!r}. "
            f"Choose one of: {', '.join(_BACKEND_MAP)}"
        )

    module_path, class_name = _BACKEND_MAP[name].rsplit(".", 1)

    import importlib
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise ImportError(
            f"Could not import backend {name!r}. "
            f"Install its dependencies first.\n"
            f"Original error: {exc}"
        ) from exc

    cls = getattr(module, class_name)
    return cls(**(config or {}))


def list_backends() -> list[str]:
    """Return all available backend identifiers."""
    return list(_BACKEND_MAP)


def health_report() -> dict[str, dict]:
    """
    Run health_check() against all backends that can be instantiated.
    Useful for deployment pre-flight checks.
    """
    results = {}
    for name in _BACKEND_MAP:
        try:
            backend = get_backend(name)
            results[name] = backend.health_check()
        except Exception as exc:  # noqa: BLE001
            results[name] = {"status": "unavailable", "backend": name, "error": str(exc)}
    return results
