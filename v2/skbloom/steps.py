"""
skbloom steps — the named-step state machine (the installer spine).

Ordered, idempotent, resumable: each step has a name, an action, and an optional
`check` (is it already satisfied?). Completed step names persist to a flag file
(`skbloom-steps.json`), so a failed/interrupted run resumes exactly where it stopped.
The LLM concierge narrates + repairs BY STEP NAME — so this layer is deterministic,
inspectable, and never lets the model touch the cluster directly.

Mirrors Kubefirst's provisionWatcher (InstallTools → … → ArgoCD → VaultInit → Final),
but the steps are plain Python callables our deterministic engine owns.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass
class Step:
    name: str
    run: Callable[[], None]                       # the action — must be idempotent
    check: Optional[Callable[[], bool]] = None    # already satisfied? → skip the action
    description: str = ""


@dataclass
class StepResult:
    name: str
    status: str                                   # "done" | "skipped" | "failed"
    error: str = ""


class StateStore:
    """Persists completed step names to a JSON flag file (resumable across runs)."""
    def __init__(self, path):
        self.path = Path(path)
        self._done = self._load()

    def _load(self) -> list:
        self._meta = {}
        if self.path.exists():
            try:
                doc = json.loads(self.path.read_text())
                self._meta = dict(doc.get("meta", {}))
                return list(doc.get("completed", []))
            except (ValueError, OSError):
                return []
        return []

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"completed": self._done, "meta": self._meta}, indent=2))

    def meta(self) -> dict:
        return dict(getattr(self, "_meta", {}))

    def set_meta(self, d: dict) -> None:
        self._meta = {**getattr(self, "_meta", {}), **(d or {})}
        self._save()

    def is_done(self, name: str) -> bool:
        return name in self._done

    def mark_done(self, name: str) -> None:
        if name not in self._done:
            self._done.append(name)
            self._save()

    def completed(self) -> list:
        return list(self._done)

    def reset(self) -> None:
        self._done = []
        self._save()


def run_steps(steps, store: StateStore, *, on_event: Optional[Callable] = None) -> list:
    """Run steps in order. Skip already-done/satisfied ones. Stop on the first failure
    (resumable on the next call). Emits ('start'|'done'|'skipped'|'failed', step) events."""
    def emit(ev, step):
        if on_event:
            on_event(ev, step)

    results = []
    for step in steps:
        if store.is_done(step.name) or (step.check and _safe_check(step.check)):
            store.mark_done(step.name)
            emit("skipped", step)
            results.append(StepResult(step.name, "skipped"))
            continue
        emit("start", step)
        try:
            step.run()
            store.mark_done(step.name)
            emit("done", step)
            results.append(StepResult(step.name, "done"))
        except Exception as e:                    # stop here; the next run resumes
            emit("failed", step)
            results.append(StepResult(step.name, "failed", str(e)))
            break
    return results


def iter_steps(steps, store: StateStore):
    """Generator variant for live UIs: yields a dict per event as it happens
    ({'event','step','detail'}), then a final {'event':'complete'|'failed'}.
    Stops on the first failure (resumable next call)."""
    failed = False
    for step in steps:
        if store.is_done(step.name) or (step.check and _safe_check(step.check)):
            store.mark_done(step.name)
            yield {"event": "skipped", "step": step.name, "detail": step.description}
            continue
        yield {"event": "start", "step": step.name, "detail": step.description}
        try:
            step.run()
            store.mark_done(step.name)
            yield {"event": "done", "step": step.name, "detail": step.description}
        except Exception as e:
            yield {"event": "failed", "step": step.name, "detail": str(e)}
            failed = True
            break
    yield {"event": "end", "ok": not failed, "step": "", "detail": ""}


def _safe_check(check) -> bool:
    try:
        return bool(check())
    except Exception:
        return False
