"""
The pack/plugin model — how a project ships its own skwire module ("pack n ship")
and how skwire discovers + merges them. This is what lets Hermes/skstacks/anyone
bundle their descriptors + injectors + questions and have skwire configure them
conversationally instead of a hand-edited TUI.
"""
from __future__ import annotations

import pytest

from skwire import (
    Pack, register_pack, get_pack, list_packs, all_nodes, clear_registry,
    load_packs_from_entrypoints,
)
from skwire.resolver import build_plan


@pytest.fixture(autouse=True)
def _clean():
    clear_registry()
    yield
    clear_registry()


def _hermes_pack():
    return Pack(
        name="hermes",
        nodes=[
            {"name": "openrouter", "provides": {"url": "https://openrouter.ai/api/v1", "api_kind": "openai"}},
            {"name": "hermes", "needs": [{"service": "openrouter", "secret": "openrouter_api_key"}]},
        ],
        injectors={"hermes-config": object()},     # a project-specific injector
        questions=["Which model provider? (OpenRouter / OpenAI / local Ollama)"],
    )


def test_register_and_get_pack():
    register_pack(_hermes_pack())
    assert get_pack("hermes").name == "hermes"
    assert "hermes" in list_packs()


def test_all_nodes_merges_descriptors_across_packs():
    register_pack(_hermes_pack())
    register_pack(Pack(name="skstacks", nodes=[{"name": "openbao", "provides": {"url": "https://openbao:8200"}}]))
    names = {n["name"] for n in all_nodes()}
    assert {"openrouter", "hermes", "openbao"} <= names
    # the merged graph resolves end-to-end (Hermes wired to its provider)
    plan = build_plan(all_nodes())
    assert "openrouter_api_key" in plan.mints


def test_duplicate_pack_name_rejected():
    register_pack(_hermes_pack())
    with pytest.raises(ValueError):
        register_pack(_hermes_pack())


def test_pack_carries_project_specific_injectors_and_questions():
    register_pack(_hermes_pack())
    p = get_pack("hermes")
    assert "hermes-config" in p.injectors          # project ships its own injector
    assert p.questions and "provider" in p.questions[0].lower()


def test_discovery_via_entrypoints_is_pluggable():
    # simulate `pip install hermes` exposing an entry point that returns a Pack
    loaded = load_packs_from_entrypoints(_fake_entry_points([("hermes", _hermes_pack)]))
    assert "hermes" in loaded
    assert get_pack("hermes").name == "hermes"


def _fake_entry_points(pairs):
    class _EP:
        def __init__(self, name, fn): self.name = name; self._fn = fn
        def load(self): return self._fn
    return [_EP(n, fn) for n, fn in pairs]
