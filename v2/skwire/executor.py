"""
skwire executor — turn an approved Plan into a wired stack, and rotate keys across
the whole vertical.

execute(): verify approval → mint every secret → inject each into EVERYONE who
touches it (the provider, so it accepts the key, + every consumer, so they can use
it) → masked report. Refuses if the approval doesn't match the plan.

rotate(): re-mint the target key(s) and re-inject them everywhere they're used — in
ONE call, because skwire owns the wire graph. That's the whole point: rotating a key
manually means hunting down every consumer; here it's automatic across the vertical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .models import Plan
from .approval import verify_approval, ApprovalToken
from .mint import RandomSecretStore, mint_secrets
from .inject import RecordingInjector


@dataclass
class ExecutionResult:
    ok: bool
    minted: dict = field(default_factory=dict)      # key -> "***" (never raw)
    injected: list = field(default_factory=list)    # [{"target","secret","ok"}]
    rotated: list = field(default_factory=list)      # keys rotated (rotate())
    error: Optional[str] = None


def _targets_for(plan: Plan, secret: str) -> list[str]:
    """Everyone who touches a secret: its provider(s) + all consumers."""
    providers = {e.provider for e in plan.edges if e.secret == secret}
    consumers = {e.consumer for e in plan.edges if e.secret == secret}
    return sorted(providers | consumers)


def _inject(plan, secrets, pick, only=None):
    """`pick(target) -> Injector` chooses which injector handles each target."""
    injected, ok_all = [], True
    for secret in (only if only is not None else sorted(secrets)):
        value = secrets[secret]
        for target in _targets_for(plan, secret):
            try:
                ok = bool(pick(target).inject(target, secret, value))
            except Exception:
                ok = False
            ok_all = ok_all and ok
            injected.append({"target": target, "secret": secret, "ok": ok})
    return injected, ok_all


def _picker(injector=None, injectors=None, nodes=None):
    """Build a target→Injector chooser. With injectors+nodes, route by the target's
    provides.api_kind; otherwise everything goes to the single (default) injector."""
    default = injector or RecordingInjector()
    if injectors and nodes:
        api_kind = {n["name"]: (n.get("provides") or {}).get("api_kind") for n in nodes}
        return lambda target: injectors.get(api_kind.get(target), default)
    return lambda _target: default


def execute(plan: Plan, token: ApprovalToken, store=None, injector=None,
            injectors=None, nodes=None) -> ExecutionResult:
    if not verify_approval(plan, token):
        return ExecutionResult(ok=False, error="approval does not match this plan")
    store = store or RandomSecretStore()
    secrets = mint_secrets(plan, store)
    injected, ok = _inject(plan, secrets, _picker(injector, injectors, nodes))
    return ExecutionResult(ok=ok, minted={k: "***" for k in secrets}, injected=injected)


def rotate(plan: Plan, store, injector, keys=None) -> ExecutionResult:
    """Re-mint key(s) and re-inject across the whole vertical. keys=None → all."""
    targets = sorted(keys) if keys else sorted(plan.mints)
    secrets = {k: store.mint(k) for k in targets}        # re-mint = new values
    injected, ok = _inject(plan, secrets, _picker(injector), only=targets)
    return ExecutionResult(ok=ok, minted={k: "***" for k in secrets},
                           injected=injected, rotated=targets)
