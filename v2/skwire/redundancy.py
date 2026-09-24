"""
skwire redundancy advisor — "if you need one, get two."

skwire owns the wire graph, so it can see which services are *load-bearing*: a
provider that many things depend on (high fan-in), or a node flagged `critical` /
`ha` on its descriptor. For each, it offers a redundant pair — the mantra, automated.
"""
from __future__ import annotations

from collections import Counter

from .models import Plan
from .preflight import Suggestion

MANTRA = "if you need one, get two"


def critical_nodes(plan: Plan, nodes=None, threshold: int = 2) -> list[str]:
    """Load-bearing nodes: providers with >= threshold consumers, or critical-flagged."""
    fan_in = Counter(e.provider for e in plan.edges)
    crit = {p for p, n in fan_in.items() if n >= threshold}
    for node in (nodes or []):
        if node.get("critical") or node.get("ha"):
            crit.add(node["name"])
    return sorted(crit)


def _replicas(nodes, name) -> int:
    for n in (nodes or []):
        if n.get("name") == name:
            return int(n.get("replicas", 1))
    return 1


def redundancy_advice(plan: Plan, nodes=None, threshold: int = 2) -> list[Suggestion]:
    fan_in = Counter(e.provider for e in plan.edges)
    out: list[Suggestion] = []
    for name in critical_nodes(plan, nodes=nodes, threshold=threshold):
        if _replicas(nodes, name) >= 2:
            continue                       # already redundant — don't nag
        deps = fan_in.get(name, 0)
        why = (f"{deps} services depend on it" if deps else "it's flagged critical")
        out.append(Suggestion(
            "question",
            f"🔁 {name} is load-bearing ({why}) — want me to set up a redundant pair? "
            f"({MANTRA})",
        ))
    return out


def make_redundant(nodes, name, replicas: int = 2) -> list:
    """Mark a node for HA (replicas≥2). The platform renderer deploys the pair.
    Returns a new nodes list (does not mutate the input)."""
    out = []
    found = False
    for n in nodes:
        if n.get("name") == name:
            n = {**n, "replicas": max(2, replicas), "ha": True}
            found = True
        out.append(n)
    if not found:
        raise KeyError(name)
    return out
