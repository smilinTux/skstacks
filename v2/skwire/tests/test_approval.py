"""
The trust anchor: the "hell yeah" binds to the exact plan-hash. If anything about
the plan changes, the prior approval is void — what runs is exactly what was OK'd.
"""
from __future__ import annotations

from skwire.resolver import build_plan
from skwire.approval import approve, verify_approval, ApprovalToken


_NODES = [
    {"name": "b", "provides": {"url": "x"}},
    {"name": "a", "needs": [{"service": "b", "secret": "k"}]},
]


def test_approve_then_verify_passes():
    plan = build_plan(_NODES)
    token = approve(plan, approver="chef")
    assert isinstance(token, ApprovalToken)
    assert token.approver == "chef"
    assert token.plan_hash == plan.plan_hash
    assert verify_approval(plan, token) is True


def test_changed_plan_voids_prior_approval():
    plan = build_plan(_NODES)
    token = approve(plan, approver="chef")
    # the plan changes (a new service added) → hash changes → approval no longer valid
    bigger = build_plan(_NODES + [{"name": "c", "needs": [{"service": "b", "secret": "k2"}]}])
    assert verify_approval(bigger, token) is False


def test_signer_is_pluggable():
    # default signer is a no-op marker; a real deployment plugs in capauth.
    plan = build_plan(_NODES)
    calls = {}

    def fake_signer(payload: str) -> str:
        calls["payload"] = payload
        return "capauth:SIGNED"

    token = approve(plan, approver="chef", signer=fake_signer)
    assert token.signature == "capauth:SIGNED"
    assert plan.plan_hash in calls["payload"]
