"""
skwire stages — the bootloader's resumable named-step state machine.

Run an install as ordered, named steps; each completed step is recorded to a state
file, so a failed run resumes from where it stopped (completed steps are skipped) —
the "reset + resume on error" pattern. This is what makes skwire a real bootloader
(seed → resolve → mint → bring-up → wire → handoff), not a fragile one-shot.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class Stage:
    name: str
    fn: Callable[[], object]


@dataclass
class StageResult:
    ok: bool
    completed: list = field(default_factory=list)
    failed: Optional[str] = None
    error: Optional[str] = None


def completed_stages(state_file: str) -> list:
    p = Path(state_file)
    if not p.exists():
        return []
    try:
        return list(json.loads(p.read_text()).get("completed", []))
    except Exception:
        return []


def _save(state_file: str, completed: list) -> None:
    Path(state_file).write_text(json.dumps({"completed": completed}))


def reset_stages(state_file: str) -> None:
    Path(state_file).write_text(json.dumps({"completed": []}))


def run_stages(stages: list, state_file: str) -> StageResult:
    done = completed_stages(state_file)
    for stage in stages:
        if stage.name in done:
            continue                          # already completed in a prior run
        try:
            stage.fn()
        except Exception as exc:              # stop, leave state so it resumes
            return StageResult(ok=False, completed=list(done), failed=stage.name, error=str(exc))
        done.append(stage.name)
        _save(state_file, done)
    return StageResult(ok=True, completed=list(done))
