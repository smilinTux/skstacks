"""
skwire doctor — a self-check the installer runs to catch problems before wiring:
is the environment sane, are packs registered, does the graph resolve? Returns a
list of checks the CLI / chat can show (the "pre-flight validator").
"""
from __future__ import annotations

from .preflight import probe_env
from .pack import list_packs, all_nodes
from .resolver import build_plan, WireError


def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def doctor() -> list[dict]:
    checks = []

    # environment
    try:
        p = probe_env()
        checks.append(_check("environment", True,
                             f"{p.os}, {p.cpu_cores} cores, {p.ram_gb:g}GB, docker={p.has_docker}"))
    except Exception as exc:
        checks.append(_check("environment", False, f"probe failed: {exc}"))

    # packs
    packs = list_packs()
    checks.append(_check("packs", bool(packs),
                         f"{len(packs)} registered: {', '.join(packs)}" if packs
                         else "none — install/register a pack (`skwire packs`)"))

    # plan / graph health
    nodes = all_nodes()
    if not nodes:
        checks.append(_check("plan", False, "no nodes to plan yet"))
    else:
        try:
            plan = build_plan(nodes)
            checks.append(_check("plan", True,
                                 f"{len(plan.order)} services, {len(plan.edges)} edges, "
                                 f"{len(plan.mints)} secrets — graph resolves"))
        except WireError as exc:
            checks.append(_check("plan", False, f"graph error: {exc}"))

    return checks
