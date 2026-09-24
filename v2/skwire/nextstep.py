"""
skwire next_step — never a dead end. Always offer a *few* options with exactly one
marked as the best/most-likely default, so the user can just accept (or pick another).
"""
from __future__ import annotations

from dataclasses import dataclass

from .pack import list_packs, all_nodes
from .resolver import build_plan, WireError


@dataclass(frozen=True)
class Step:
    label: str                 # human label, e.g. "Wire everything up"
    command: str               # the command behind it, e.g. "skwire up"
    default: bool = False       # exactly one Step is the default


def next_steps() -> list:
    """A few recommended next actions for the current state — exactly one default.

    Always non-empty. The first/default option is the best most-likely action;
    the rest are sensible alternatives so the user has a real choice."""
    if not list_packs():
        return [
            Step("Browse the catalog", "skwire catalog", default=True),
            Step("Tell me what you want", "skwire serve"),
        ]
    nodes = all_nodes()
    if not nodes:
        return [
            Step("Add an app", "skwire add <name>", default=True),
            Step("Browse the catalog", "skwire catalog"),
        ]
    try:
        plan = build_plan(nodes)
    except WireError:
        return [
            Step("Fix the wiring graph", "skwire doctor", default=True),
            Step("Review the catalog", "skwire catalog"),
        ]
    n = len(plan.order)
    return [
        Step(f"Wire all {n} service(s) & mint secrets", "skwire up", default=True),
        Step("Add another app", "skwire add <name>"),
        Step("Open the chat window", "skwire serve"),
    ]


def next_step() -> str:
    """One-line prose recommending the default option (always non-empty)."""
    steps = next_steps()
    d = next((s for s in steps if s.default), steps[0])
    alts = [s for s in steps if s is not d]
    tail = ("  (Or " + ", ".join(f"`{s.command}`" for s in alts) + ".)") if alts else ""
    return f"Next (recommended): `{d.command}` — {d.label}.{tail}"
