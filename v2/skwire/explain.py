"""
skwire explain — Plan → plain English. The bilingual interpreter: it lets the AI
concierge SAY what it's about to do, then ask "want me to just do it?". Both the
user and the machine are looking at the same plan (and the same plan-hash).
"""
from __future__ import annotations

from .models import Plan


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def explain(plan: Plan) -> str:
    n = len(plan.order)
    if n <= 1 and not plan.edges:
        only = plan.order[0] if plan.order else "(nothing)"
        return f"I'll set up {only}. Want me to just do it?"

    lines = [f"I'll set up {_plural(n, 'service')} in order: {', '.join(plan.order)}."]
    if plan.mints or plan.edges:
        lines.append(
            f"I'll generate {_plural(len(plan.mints), 'secret')} and "
            f"wire {_plural(len(plan.edges), 'connection')}:"
        )
        for e in plan.edges:
            lines.append(f"  • {e.consumer} → {e.provider} (via {e.secret})")
    lines.append("Want me to just do it?")
    return "\n".join(lines)
