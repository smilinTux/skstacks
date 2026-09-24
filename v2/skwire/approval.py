"""
skwire approval — the "heck yeah" handshake.

The user's approval binds to the EXACT plan_hash. If anything about the plan
changes afterward, the hash changes and the prior approval is void — so what runs
is exactly what was approved. The signer is pluggable: the default is an inert
marker; a real deployment plugs in capauth (PGP) so the token is cryptographically
attributable. This same token is what an executor checks before mutating anything.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .models import Plan

Signer = Callable[[str], str]


@dataclass(frozen=True)
class ApprovalToken:
    plan_hash: str
    approver: str
    signature: str


def _default_signer(payload: str) -> str:
    # Inert marker — NOT a real signature. Plug in capauth for attributable approval.
    return "unsigned:" + payload


def approve(plan: Plan, approver: str, signer: Optional[Signer] = None) -> ApprovalToken:
    """Record an approval bound to this exact plan."""
    sign = signer or _default_signer
    payload = f"{plan.plan_hash}|{approver}"
    return ApprovalToken(
        plan_hash=plan.plan_hash,
        approver=approver,
        signature=sign(payload),
    )


def verify_approval(plan: Plan, token: ApprovalToken) -> bool:
    """True only if the token approves THIS plan (hash unchanged since approval)."""
    return token.plan_hash == plan.plan_hash
