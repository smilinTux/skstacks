"""
skwire rotation scheduling — read each secret's `rotation_days` (already declared
on the descriptors), compute what's due, and OFFER to schedule it. The actual timer
is the host's (cron / systemd-timer / skscheduler / the sksec closed-loop); skwire
provides the policy + the cron expression + the "want me to schedule these?" offer.
"""
from __future__ import annotations

from dataclasses import dataclass


def rotation_schedule(nodes) -> dict[str, int]:
    """key -> rotation_days, read from each node's declared secrets[]."""
    sched: dict[str, int] = {}
    for n in nodes:
        for s in n.get("secrets", []) or []:
            if isinstance(s, dict) and s.get("key") and s.get("rotation_days"):
                sched[s["key"]] = int(s["rotation_days"])
    return sched


def due_for_rotation(schedule: dict[str, int], last_rotated_days: dict[str, int],
                     now_days: int) -> list[str]:
    """Which keys are past their rotation window (day-numbered clock)."""
    return sorted(
        k for k, days in schedule.items()
        if now_days - last_rotated_days.get(k, 0) >= days
    )


def cron_for(days: int) -> str:
    """A cron expression for an N-day rotation (03:00)."""
    if days <= 0:
        return ""
    if days <= 28:
        return f"0 3 */{days} * *"
    months = max(1, round(days / 30))
    return f"0 3 1 */{months} *"


@dataclass
class RotationOffer:
    schedule: dict     # key -> days
    crons: dict        # key -> cron expression
    message: str       # the "want me to schedule these?" prompt


def offer_rotation_schedule(nodes) -> RotationOffer:
    sched = rotation_schedule(nodes)
    if not sched:
        return RotationOffer({}, {}, "No rotation policy is declared on these secrets.")
    crons = {k: cron_for(d) for k, d in sched.items()}
    items = ", ".join(f"{k} every {d}d" for k, d in sorted(sched.items()))
    return RotationOffer(sched, crons, f"I can auto-rotate: {items}. Want me to schedule these?")
