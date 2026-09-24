"""
skwire resolver — the core graph engine (pure stdlib, embeddable).

build_plan(nodes) → Plan:
  1. derive wiring edges from each node's `needs` (consumer → provider via secret),
  2. topologically order nodes so every provider boots before its consumers,
  3. collect the set of secrets to mint up-front (mint-then-inject), and
  4. compute a stable, order-independent plan_hash (the trust/approval anchor).

A node is any wireable thing — an sk* service, a 3rd-party app, or an agent-
toolchain component (e.g. OpenCode needing an OpenRouter endpoint+key). Same model.

Node shape (dict):
  {
    "name": "sonarr",
    "provides": {"url": "http://sonarr:8989", "api_kind": "servarr"},   # optional
    "needs": [{"service": "prowlarr", "secret": "prowlarr_api_key"}],   # optional
  }
"""
from __future__ import annotations

import hashlib
import json
from typing import Iterable

from .models import Plan, WireEdge


class WireError(Exception):
    """Base error for skwire resolution."""


class MissingProviderError(WireError):
    """A node `needs` a service that isn't in the graph."""


class WireCycleError(WireError):
    """The dependency graph has a cycle (cannot be ordered)."""


def build_plan(nodes: Iterable[dict]) -> Plan:
    nodes = list(nodes)
    names = {n["name"] for n in nodes}

    edges: list[WireEdge] = []
    deps: dict[str, set[str]] = {n["name"]: set() for n in nodes}
    mints: set[str] = set()

    for n in nodes:
        for need in n.get("needs", []) or []:
            provider = need["service"]
            if provider not in names:
                raise MissingProviderError(
                    f"{n['name']!r} needs {provider!r}, which is not in the graph"
                )
            edges.append(WireEdge(consumer=n["name"], provider=provider, secret=need["secret"]))
            deps[n["name"]].add(provider)
            mints.add(need["secret"])

    order = _toposort(deps)
    plan_hash = _hash(names, edges, mints)
    return Plan(
        order=tuple(order),
        edges=tuple(sorted(edges, key=lambda e: (e.consumer, e.provider, e.secret))),
        mints=frozenset(mints),
        plan_hash=plan_hash,
    )


def _toposort(deps: dict[str, set[str]]) -> list[str]:
    """Kahn's algorithm; deterministic (sorted ready-set). Providers come first."""
    remaining = {k: set(v) for k, v in deps.items()}
    order: list[str] = []
    while remaining:
        ready = sorted(k for k, d in remaining.items() if not d)
        if not ready:
            raise WireCycleError(f"dependency cycle among: {sorted(remaining)}")
        for node in ready:
            order.append(node)
            del remaining[node]
            for d in remaining.values():
                d.discard(node)
    return order


def _hash(names: set[str], edges: list[WireEdge], mints: set[str]) -> str:
    """Stable, input-order-independent content hash of the plan."""
    canonical = {
        "nodes": sorted(names),
        "edges": sorted([e.consumer, e.provider, e.secret] for e in edges),
        "mints": sorted(mints),
    }
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode()).hexdigest()
