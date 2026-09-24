"""
skwire extension points — the contracts an embedder implements. Pure typing; no
deps. A consuming project (skstacks, Hermes, …) supplies concrete impls and ships
them in a Pack.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class SecretStore(Protocol):
    """Where minted secrets live (skvault/OpenBao/capauth/env-file…)."""
    def mint(self, key: str) -> str: ...
    def get(self, key: str) -> str: ...
    def put(self, key: str, value: str) -> None: ...


@runtime_checkable
class Injector(Protocol):
    """How a secret/config value reaches a target (env-override / file / app API)."""
    method: str
    def inject(self, target: str, key: str, value: str) -> bool: ...


@runtime_checkable
class Signer(Protocol):
    """Signs the approval payload (capauth/PGP in prod)."""
    def sign(self, payload: str) -> str: ...


@runtime_checkable
class Probe(Protocol):
    """Reports environment facts for preflight (see preflight.SystemProbe)."""
    os: str
    cpu_cores: int
    ram_gb: float
    gpus: list
    disk_free_gb: float
    has_docker: bool
    has_kubectl: bool
    has_ollama: bool
    public_ip: object
    free_ports: set
