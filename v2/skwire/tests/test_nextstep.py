"""Always suggest a next step — and better yet, a few options with a default."""
from __future__ import annotations

import pytest

from skwire import next_step, next_steps, Step, Pack, register_pack, clear_registry


@pytest.fixture(autouse=True)
def _clean():
    clear_registry(); yield; clear_registry()


def test_always_returns_a_nonempty_suggestion():
    assert next_step().strip()                       # never empty


def test_next_steps_offers_a_few_options():
    steps = next_steps()
    assert len(steps) >= 2                            # a few, not one
    assert all(isinstance(s, Step) and s.label and s.command for s in steps)


def test_exactly_one_option_is_the_default():
    assert sum(1 for s in next_steps() if s.default) == 1


def test_next_step_string_matches_the_default_option():
    default = next(s for s in next_steps() if s.default)
    assert default.command in next_step()            # the prose recommends the default


def test_with_a_plan_the_default_is_up():
    register_pack(Pack(name="m", nodes=[
        {"name": "b", "provides": {"url": "x"}},
        {"name": "a", "needs": [{"service": "b", "secret": "k"}]},
    ]))
    default = next(s for s in next_steps() if s.default)
    assert "up" in default.command


def test_no_packs_points_to_the_catalog():
    s = next_step().lower()
    assert "catalog" in s or "add" in s or "install" in s


def test_with_a_valid_plan_points_to_up():
    register_pack(Pack(name="m", nodes=[
        {"name": "b", "provides": {"url": "x"}},
        {"name": "a", "needs": [{"service": "b", "secret": "k"}]},
    ]))
    assert "up" in next_step().lower()


def test_a_broken_graph_points_to_doctor():
    register_pack(Pack(name="bad", nodes=[
        {"name": "a", "provides": {"url": "x"}, "needs": [{"service": "b", "secret": "k"}]},
        {"name": "b", "provides": {"url": "y"}, "needs": [{"service": "a", "secret": "k2"}]},
    ]))
    assert "doctor" in next_step().lower()
