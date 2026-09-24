"""
skwire mint — generate every secret up front (mint-then-inject). The default
RandomSecretStore is in-memory; a real deployment plugs in skvault/OpenBao/capauth
(anything satisfying the SecretStore contract). mint() generates a NEW value and
persists it, so calling it again = rotation.
"""
from __future__ import annotations

import os

from .models import Plan


class RandomSecretStore:
    """In-memory SecretStore (default). Swap for skvault/OpenBao/capauth in prod."""

    def __init__(self):
        self._d: dict[str, str] = {}

    def mint(self, key: str) -> str:
        # os.urandom (CSPRNG) — NOT stdlib `secrets`, which the v2/secrets package
        # shadows when v2/ is on sys.path. New value each call → rotation-ready.
        self._d[key] = os.urandom(24).hex()
        return self._d[key]

    def get(self, key: str) -> str:
        return self._d[key]

    def put(self, key: str, value: str) -> None:
        self._d[key] = value


def mint_secrets(plan: Plan, store) -> dict[str, str]:
    """Mint every secret the plan needs, up front."""
    return {k: store.mint(k) for k in sorted(plan.mints)}
