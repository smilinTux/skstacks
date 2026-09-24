"""
Tests for the skwire resolver — the core graph engine.

A "node" is anything wireable: an sk* service, a 3rd-party app (Sonarr), OR an
agent-toolchain component (OpenCode needing an OpenRouter endpoint+key). The
resolver doesn't care which — it's all provides/needs → graph → plan.
"""
from __future__ import annotations

import pytest

from skwire.resolver import build_plan, WireCycleError, MissingProviderError


def test_single_need_creates_edge_and_mint():
    nodes = [
        {"name": "prowlarr", "provides": {"url": "http://prowlarr:9696", "api_kind": "servarr"}},
        {"name": "sonarr", "needs": [{"service": "prowlarr", "secret": "prowlarr_api_key"}]},
    ]
    plan = build_plan(nodes)
    assert ("sonarr", "prowlarr", "prowlarr_api_key") in [(e.consumer, e.provider, e.secret) for e in plan.edges]
    assert "prowlarr_api_key" in plan.mints
    # provider must come up before its consumer
    assert plan.order.index("prowlarr") < plan.order.index("sonarr")


def test_topological_order_chain():
    nodes = [
        {"name": "qbittorrent", "provides": {"url": "http://qbit:8080"}},
        {"name": "sonarr", "provides": {"url": "http://sonarr:8989"},
         "needs": [{"service": "qbittorrent", "secret": "qbit_pw"}]},
        {"name": "seerr", "needs": [{"service": "sonarr", "secret": "sonarr_api_key"}]},
    ]
    order = build_plan(nodes).order
    assert order.index("qbittorrent") < order.index("sonarr") < order.index("seerr")


def test_agent_toolchain_node_wires_like_any_other():
    # OpenCode (agent) needs an OpenRouter endpoint + key — same provides/needs model.
    nodes = [
        {"name": "openrouter", "provides": {"url": "https://openrouter.ai/api/v1", "api_kind": "openai"}},
        {"name": "opencode", "needs": [{"service": "openrouter", "secret": "openrouter_api_key"}]},
    ]
    plan = build_plan(nodes)
    assert "openrouter_api_key" in plan.mints
    assert list(plan.order) == ["openrouter", "opencode"]


def test_plan_hash_is_stable_and_order_independent():
    a = [{"name": "b", "provides": {"url": "x"}},
         {"name": "a", "needs": [{"service": "b", "secret": "k"}]}]
    b = list(reversed(a))                      # same graph, different input order
    assert build_plan(a).plan_hash == build_plan(b).plan_hash


def test_cycle_is_rejected():
    nodes = [
        {"name": "a", "provides": {"url": "x"}, "needs": [{"service": "b", "secret": "kb"}]},
        {"name": "b", "provides": {"url": "y"}, "needs": [{"service": "a", "secret": "ka"}]},
    ]
    with pytest.raises(WireCycleError):
        build_plan(nodes)


def test_missing_provider_is_rejected():
    nodes = [{"name": "sonarr", "needs": [{"service": "ghost", "secret": "k"}]}]
    with pytest.raises(MissingProviderError):
        build_plan(nodes)
