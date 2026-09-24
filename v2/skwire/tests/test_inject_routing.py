"""execute() routes each injection to the right per-app injector (by api_kind)."""
from __future__ import annotations

from skwire import build_plan, approve, execute
from skwire.mint import RandomSecretStore
from skwire.inject import RecordingInjector


def _nodes():
    return [
        {"name": "prowlarr", "provides": {"url": "x", "api_kind": "servarr"}},
        {"name": "qbittorrent", "provides": {"url": "y", "api_kind": "qbittorrent"}},
        {"name": "sonarr", "provides": {"url": "z", "api_kind": "servarr"},
         "needs": [{"service": "prowlarr", "secret": "prowlarr_api_key"},
                   {"service": "qbittorrent", "secret": "qbit_pw"}]},
    ]


def test_execute_routes_by_api_kind():
    nodes = _nodes()
    plan = build_plan(nodes)
    servarr, qbit = RecordingInjector(), RecordingInjector()
    res = execute(plan, approve(plan, "chef"), store=RandomSecretStore(),
                  injectors={"servarr": servarr, "qbittorrent": qbit}, nodes=nodes)
    assert res.ok
    # prowlarr/sonarr (servarr) went to the servarr injector
    servarr_targets = {c.target for c in servarr.calls}
    assert {"prowlarr", "sonarr"} <= servarr_targets
    # qbittorrent went to the qbit injector
    assert "qbittorrent" in {c.target for c in qbit.calls}
    assert "qbittorrent" not in servarr_targets


def test_unknown_api_kind_falls_back_to_default_injector():
    nodes = _nodes()
    plan = build_plan(nodes)
    default = RecordingInjector()
    res = execute(plan, approve(plan, "chef"), store=RandomSecretStore(),
                  injectors={}, nodes=nodes, injector=default)
    assert res.ok and default.calls           # everything fell back to default
