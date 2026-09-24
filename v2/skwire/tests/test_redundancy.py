"""
The "if you need one, get two" advisor — skwire owns the graph, so it spots
load-bearing services (high fan-in) + critical-flagged nodes and offers a pair.
"""
from __future__ import annotations

from skwire import build_plan
from skwire.redundancy import critical_nodes, redundancy_advice


def _nodes():
    # prowlarr is depended on by sonarr AND radarr → load-bearing (fan-in 2)
    return [
        {"name": "prowlarr", "provides": {"url": "x"}},
        {"name": "sonarr", "needs": [{"service": "prowlarr", "secret": "k1"}]},
        {"name": "radarr", "needs": [{"service": "prowlarr", "secret": "k2"}]},
        {"name": "seerr", "needs": [{"service": "sonarr", "secret": "k3"}]},  # sonarr fan-in 1
    ]


def test_critical_nodes_are_high_fan_in_providers():
    plan = build_plan(_nodes())
    crit = critical_nodes(plan, threshold=2)
    assert "prowlarr" in crit          # 2 consumers
    assert "sonarr" not in crit        # only 1 consumer


def test_critical_flag_on_descriptor_is_honored():
    nodes = _nodes() + [{"name": "openbao", "provides": {"url": "y"}, "critical": True}]
    plan = build_plan(nodes)
    crit = critical_nodes(plan, nodes=nodes, threshold=2)
    assert "openbao" in crit           # flagged critical even with 0 consumers


def test_advice_offers_a_pair_with_the_mantra():
    plan = build_plan(_nodes())
    advice = redundancy_advice(plan)
    assert advice and any("prowlarr" in a.text for a in advice)
    joined = " ".join(a.text.lower() for a in advice)
    assert "redundant" in joined or "pair" in joined
    assert "if you need one, get two" in joined


def test_no_advice_when_nothing_is_load_bearing():
    plan = build_plan([{"name": "solo", "provides": {"url": "x"}}])
    assert redundancy_advice(plan) == []


def test_make_redundant_marks_ha_and_stops_nagging():
    from skwire import make_redundant
    from skwire.redundancy import redundancy_advice
    nodes = _nodes()
    nodes2 = make_redundant(nodes, "prowlarr")
    pro = next(n for n in nodes2 if n["name"] == "prowlarr")
    assert pro["replicas"] == 2 and pro["ha"] is True
    # original not mutated
    assert all("replicas" not in n for n in nodes)
    # advice no longer nags about prowlarr (it's now redundant)
    plan = build_plan(nodes2)
    assert not any("prowlarr" in a.text for a in redundancy_advice(plan, nodes=nodes2))
