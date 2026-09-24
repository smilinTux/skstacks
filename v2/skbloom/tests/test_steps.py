"""The named-step state machine — skbloom's spine. Ordered, idempotent, resumable:
a failed run picks up where it left off; already-satisfied steps are skipped. The LLM
narrates + repairs BY STEP NAME, so the machine must be deterministic and inspectable."""
from __future__ import annotations

import pytest

from skbloom.steps import Step, StateStore, run_steps, StepResult, iter_steps


@pytest.fixture
def store(tmp_path):
    return StateStore(tmp_path / "skbloom-steps.json")


def test_runs_steps_in_order_and_marks_done(store):
    order = []
    steps = [Step(n, run=lambda n=n: order.append(n)) for n in ("a", "b", "c")]
    results = run_steps(steps, store)
    assert order == ["a", "b", "c"]
    assert [r.status for r in results] == ["done", "done", "done"]
    assert store.completed() == ["a", "b", "c"]


def test_idempotent_check_skips_already_satisfied_work(store):
    ran = []
    steps = [
        Step("present", run=lambda: ran.append("present"), check=lambda: True),   # already done
        Step("absent", run=lambda: ran.append("absent")),
    ]
    res = run_steps(steps, store)
    assert ran == ["absent"]                          # the satisfied step's action never ran
    assert res[0].status == "skipped" and res[1].status == "done"


def test_stops_on_failure_and_is_resumable(store):
    attempts = {"b": 0}
    def b_run():
        attempts["b"] += 1
        if attempts["b"] == 1:
            raise RuntimeError("transient boom")
    order = []
    steps = [
        Step("a", run=lambda: order.append("a")),
        Step("b", run=b_run),
        Step("c", run=lambda: order.append("c")),
    ]
    r1 = run_steps(steps, store)
    assert [r.status for r in r1] == ["done", "failed"]   # stopped at b; c not attempted
    assert "c" not in order
    assert store.completed() == ["a"]                     # only a persisted

    # resume: a is skipped, b retried (now succeeds), c runs
    r2 = run_steps(steps, store)
    assert [r.name for r in r2 if r.status != "skipped"] == ["b", "c"]
    assert order == ["a", "c"] and attempts["b"] == 2
    assert store.completed() == ["a", "b", "c"]


def test_state_persists_across_reload(tmp_path):
    p = tmp_path / "s.json"
    run_steps([Step("x", run=lambda: None)], StateStore(p))
    assert StateStore(p).is_done("x")                     # a fresh store reads the flag file


def test_reset_clears_progress(store):
    run_steps([Step("x", run=lambda: None)], store)
    store.reset()
    assert store.completed() == []


def test_state_store_holds_and_merges_meta(tmp_path):
    s = StateStore(tmp_path / "s.json")
    s.set_meta({"domain": "sk.local", "urls": {"login": "https://login.sk.local"}})
    assert s.meta()["urls"]["login"] == "https://login.sk.local"
    # persists across reload + merges new keys without dropping old
    s2 = StateStore(tmp_path / "s.json")
    assert s2.meta()["domain"] == "sk.local"
    s2.set_meta({"tls": True})
    assert s2.meta()["domain"] == "sk.local" and s2.meta()["tls"] is True


def test_events_are_emitted_for_narration(store):
    seen = []
    run_steps([Step("a", run=lambda: None), Step("b", run=lambda: None)],
              store, on_event=lambda ev, step: seen.append((ev, step.name)))
    assert ("start", "a") in seen and ("done", "a") in seen


def test_iter_steps_streams_live_events(store):
    # the streaming variant the web UI consumes — yields (event, name, detail) as it goes
    evs = list(iter_steps([Step("a", run=lambda: None), Step("b", run=lambda: None)], store))
    kinds = [(e["event"], e["step"]) for e in evs]
    assert ("start", "a") in kinds and ("done", "a") in kinds and ("done", "b") in kinds
    assert evs[-1]["event"] == "end" and evs[-1]["ok"] is True
    assert store.completed() == ["a", "b"]


def test_iter_steps_stops_and_reports_failure(store):
    def boom():
        raise RuntimeError("nope")
    evs = list(iter_steps([Step("a", run=lambda: None), Step("b", run=boom),
                           Step("c", run=lambda: None)], store))
    names_failed = [e["step"] for e in evs if e["event"] == "failed"]
    assert names_failed == ["b"]
    assert all(e["step"] != "c" for e in evs)          # c never reached
    assert evs[-1]["event"] == "end" and evs[-1]["ok"] is False
