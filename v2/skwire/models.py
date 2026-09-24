"""
skwire core models — pure stdlib, zero dependencies, so any project can embed it.

A WireGraph is built from nodes that declare `provides` (what they expose) and
`needs` (what they consume + which secret). The resolver turns that into a Plan.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WireEdge:
    """A consumer needs something a provider exposes, secured by `secret`."""
    consumer: str
    provider: str
    secret: str


@dataclass(frozen=True)
class Plan:
    """
    The wiring plan — the signed contract for both parties (human↔AI, service↔service).

    order:     services in bring-up order (providers before consumers)
    edges:     the wiring edges (consumer → provider via secret)
    mints:     the set of secrets skwire must mint up-front (mint-then-inject)
    plan_hash: stable content hash — the approval/trust anchor (order-independent)
    """
    order: tuple[str, ...]
    edges: tuple[WireEdge, ...]
    mints: frozenset[str]
    plan_hash: str
