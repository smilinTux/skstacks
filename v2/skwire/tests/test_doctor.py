"""skwire doctor — self-check the install environment + wiring health."""
from __future__ import annotations

import pytest

from skwire import doctor, Pack, register_pack, clear_registry


@pytest.fixture(autouse=True)
def _clean():
    clear_registry(); yield; clear_registry()


def test_doctor_returns_checks_with_status():
    report = doctor()
    assert report and all({"name", "ok", "detail"} <= set(c) for c in report)
    names = {c["name"] for c in report}
    assert {"environment", "packs", "plan"} <= names


def test_doctor_flags_no_packs():
    report = {c["name"]: c for c in doctor()}
    assert report["packs"]["ok"] is False        # nothing registered → warn


def test_doctor_validates_plan_when_packs_present():
    register_pack(Pack(name="ok", nodes=[
        {"name": "b", "provides": {"url": "x"}},
        {"name": "a", "needs": [{"service": "b", "secret": "k"}]},
    ]))
    report = {c["name"]: c for c in doctor()}
    assert report["packs"]["ok"] is True and report["plan"]["ok"] is True


def test_doctor_detects_a_broken_graph():
    register_pack(Pack(name="bad", nodes=[
        {"name": "a", "provides": {"url": "x"}, "needs": [{"service": "b", "secret": "k"}]},
        {"name": "b", "provides": {"url": "y"}, "needs": [{"service": "a", "secret": "k2"}]},
    ]))
    report = {c["name"]: c for c in doctor()}
    assert report["plan"]["ok"] is False         # cycle → caught
