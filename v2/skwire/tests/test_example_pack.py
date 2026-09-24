"""The shipped example pack must be valid + resolve (it's the copy-paste template)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from skwire import Pack, register_pack, all_nodes, build_plan, clear_registry

_EX = Path(__file__).resolve().parents[1] / "examples" / "hello_pack"


@pytest.fixture(autouse=True)
def _clean():
    clear_registry(); yield; clear_registry()


def test_example_pack_is_valid_and_resolves():
    sys.path.insert(0, str(_EX))
    try:
        import hello_pack
    finally:
        sys.path.pop(0)
    p = hello_pack.pack()
    assert isinstance(p, Pack) and p.name == "hello"
    assert "env-file" in p.injectors                  # ships its own injector
    register_pack(p)
    plan = build_plan(all_nodes())
    assert "greeter_api_key" in plan.mints            # the wiring resolves


def test_example_injector_satisfies_the_contract():
    from skwire import Injector
    sys.path.insert(0, str(_EX))
    try:
        import hello_pack
    finally:
        sys.path.pop(0)
    inj = hello_pack.EnvFileInjector()
    assert isinstance(inj, Injector)
    assert inj.inject("app", "greeter_api_key", "abc") is True
