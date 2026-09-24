"""
skwire chat — turn free-text the user TYPES into an actionable intent.

Zero-dep, deterministic intent router built on the catalog recommender. The user
isn't trapped in pre-written buttons: they can say "just install the hf downloader"
(→ add only skhf) or "install everything" (→ the whole stack). When an OpenAI-compatible
LLM endpoint is configured (SKWIRE_LLM_URL), `llm_interpret()` can layer richer NLU on
top — but the keyword router below is the always-available, tested default.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .catalog import list_offerings, recommend, best_offering

# whole-stack triggers — ONLY these expand to "add everything"; a bare app name never does.
_ALL = ("everything", "whole stack", "the works", "all of it", "full stack", "the lot")
# words that signal "I want this set up"
_ADD = ("install", "add", "want", "just", "only", "set up", "setup", "get me",
        "give me", "deploy", "i need", "i'd like", "lets do", "let's do")
# words that mean "proceed / show me the plan"
_PROCEED = ("do it", "go ahead", "plan", "wire it", "proceed", "ship it", "make it so",
            "heck yeah", "yes please")


@dataclass(frozen=True)
class Intent:
    kind: str                       # add_one | add_all | search | plan | unknown
    target: str = ""                # offering name (add_one) or query (search)
    reply: str = ""                 # a friendly line to show the user
    suggestions: list = field(default_factory=list)   # offering names to offer on unknown


def _has(m: str, words) -> bool:
    return any(w in m for w in words)


def interpret(message: str, offerings=None) -> Intent:
    """Map a typed message to an Intent. Deterministic, never a dead end."""
    m = (message or "").lower().strip()
    offs = offerings if offerings is not None else list_offerings()

    if not m:
        return Intent("unknown", reply="What would you like to set up?",
                      suggestions=[o.name for o in offs[:4]])

    # explicit "everything" — the ONLY path to the whole stack
    if _has(m, _ALL):
        return Intent("add_all", reply="You got it — I'll set up the whole stack.")

    # live search ("search …", "search for …", "find …")
    for kw in ("search for ", "search ", "find "):
        if m.startswith(kw):
            q = message.strip()[len(kw):].strip()
            return Intent("search", target=q, reply=f"Searching for “{q}”…")

    # an explicit app-name mention ALWAYS wins over fuzzy text overlap
    # (regression: "install skhf" must pick skhf, not GIMP via the word "install")
    for o in offs:
        if re.search(rf"\b{re.escape(o.name.lower())}\b", m):
            return Intent("add_one", target=o.name,
                          reply=f"Adding just {o.title} — not the whole stack. Want me to wire it up?")

    # a clear catalog match routes directly — no action-word required
    # ("watch movies" → skmedia), but chit-chat ("hello") has no match → stays unknown
    best = best_offering(m, offerings=offs)
    if best:
        return Intent("add_one", target=best.name,
                      reply=f"Adding just {best.title} — not the whole stack. Want me to wire it up?")

    # proceed / show the plan
    if _has(m, _PROCEED):
        return Intent("plan", reply="Here's the plan…")

    # nothing matched — offer options, don't fail
    return Intent("unknown",
                  reply="I can set up any of these — tell me which (or say 'everything'):",
                  suggestions=[o.name for o in offs[:4]])
