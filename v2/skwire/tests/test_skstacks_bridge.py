"""The bridge loads the real v2 descriptors → a skwire Pack that plans."""
from __future__ import annotations

import pytest

pytest.importorskip("yaml")

from skwire import build_plan, redundancy_advice
from skwire.packs.skstacks_bridge import load_skstacks


def test_bridge_loads_the_real_sk_stack():
    pack = load_skstacks()                     # defaults to this repo's v2/
    names = {n["name"] for n in pack.nodes}
    assert {"skvault", "skdata", "skobject", "skfence", "skfile"} <= names


def test_bridge_wires_skfile_to_object_and_data():
    plan = build_plan(load_skstacks().nodes)
    # skfile depends_on [skobject, skdata] → skwire needs/edges
    edges = {(e.consumer, e.provider) for e in plan.edges}
    assert ("skfile", "skobject") in edges and ("skfile", "skdata") in edges
    assert plan.order.index("skobject") < plan.order.index("skfile")


def test_bridge_carries_ha_so_the_mantra_applies():
    nodes = load_skstacks().nodes
    # ha:true descriptors (skvault/skdata/…) come through as critical
    assert any(n.get("critical") for n in nodes if n["name"] == "skvault")
    plan = build_plan(nodes)
    advice = redundancy_advice(plan, nodes=nodes)
    assert any("skvault" in a.text for a in advice)


def test_enriched_wiring_makes_core_services_load_bearing():
    nodes = load_skstacks().nodes
    plan = build_plan(nodes)
    edges = {(e.consumer, e.provider) for e in plan.edges}
    assert ("sksso", "skdata") in edges          # Authentik → Postgres
    assert ("skbackup", "skobject") in edges     # Restic → Garage
    # high fan-in core → the mantra flags them for a pair
    flagged = " ".join(a.text for a in redundancy_advice(plan, nodes=nodes))
    assert "skdata" in flagged and "skbus" in flagged
