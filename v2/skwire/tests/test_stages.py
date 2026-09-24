"""The bootloader stage-runner — named, resumable steps (failed run resumes)."""
from __future__ import annotations

import pytest

from skwire.stages import Stage, run_stages, completed_stages, reset_stages


def test_runs_all_stages_in_order(tmp_path):
    log = []
    stages = [Stage("a", lambda: log.append("a")),
              Stage("b", lambda: log.append("b")),
              Stage("c", lambda: log.append("c"))]
    res = run_stages(stages, state_file=str(tmp_path / "s.json"))
    assert res.ok and log == ["a", "b", "c"]
    assert completed_stages(str(tmp_path / "s.json")) == ["a", "b", "c"]


def test_failed_stage_stops_and_resumes(tmp_path):
    sf = str(tmp_path / "s.json")
    log = []
    boom = {"fail": True}

    def b():
        if boom["fail"]:
            raise RuntimeError("nope")
        log.append("b")

    stages = [Stage("a", lambda: log.append("a")), Stage("b", b), Stage("c", lambda: log.append("c"))]
    res = run_stages(stages, state_file=sf)
    assert not res.ok and res.failed == "b"
    assert completed_stages(sf) == ["a"]            # only 'a' done
    assert log == ["a"]                              # 'c' never ran

    # fix the cause, re-run → resumes from 'b' (skips 'a')
    boom["fail"] = False
    res2 = run_stages(stages, state_file=sf)
    assert res2.ok
    assert log == ["a", "b", "c"]                    # 'a' NOT re-run
    assert completed_stages(sf) == ["a", "b", "c"]


def test_reset_clears_progress(tmp_path):
    sf = str(tmp_path / "s.json")
    run_stages([Stage("a", lambda: None)], state_file=sf)
    assert completed_stages(sf) == ["a"]
    reset_stages(sf)
    assert completed_stages(sf) == []
