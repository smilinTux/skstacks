"""
The executor — turns an approved Plan into a wired stack: mint every secret,
inject it into everyone who touches it (provider + consumers), report. Refuses
anything not matching the approval. ROTATION is first-class: re-mint a key and
re-inject it across the whole vertical in one call.
"""
from __future__ import annotations

from skwire import build_plan, approve
from skwire.executor import execute, rotate, ExecutionResult
from skwire.mint import RandomSecretStore, mint_secrets
from skwire.inject import RecordingInjector


def _nodes(extra=False):
    n = [
        {"name": "prowlarr", "provides": {"url": "http://prowlarr:9696"}},
        {"name": "sonarr", "needs": [{"service": "prowlarr", "secret": "prowlarr_api_key"}]},
    ]
    if extra:
        n.append({"name": "seerr", "needs": [{"service": "sonarr", "secret": "sonarr_api_key"}]})
    return n


def test_mint_generates_every_planned_secret():
    plan = build_plan(_nodes())
    store = RandomSecretStore()
    secrets = mint_secrets(plan, store)
    assert set(secrets) == plan.mints
    assert all(secrets.values())
    assert store.get("prowlarr_api_key") == secrets["prowlarr_api_key"]   # persisted


def test_execute_mints_and_injects_provider_and_consumers():
    plan = build_plan(_nodes())
    res = execute(plan, approve(plan, "chef"), store=RandomSecretStore(), injector=RecordingInjector())
    assert isinstance(res, ExecutionResult) and res.ok
    assert set(res.minted) == plan.mints
    targets = {(r["target"], r["secret"]) for r in res.injected if r["ok"]}
    # the key lands on BOTH the provider (so it accepts it) and the consumer (so it can use it)
    assert ("prowlarr", "prowlarr_api_key") in targets
    assert ("sonarr", "prowlarr_api_key") in targets


def test_report_masks_secret_values():
    plan = build_plan(_nodes())
    res = execute(plan, approve(plan, "chef"), store=RandomSecretStore(), injector=RecordingInjector())
    assert all(v == "***" for v in res.minted.values())


def test_execute_refuses_when_approval_does_not_match_plan():
    plan = build_plan(_nodes())
    stale = approve(build_plan(_nodes(extra=True)), "chef")     # approves a DIFFERENT plan
    res = execute(plan, stale, store=RandomSecretStore(), injector=RecordingInjector())
    assert not res.ok and "approval" in (res.error or "").lower()


def test_failing_injector_is_reported_not_swallowed():
    class Boom(RecordingInjector):
        def inject(self, target, key, value):
            raise RuntimeError("nope")
    plan = build_plan(_nodes())
    res = execute(plan, approve(plan, "chef"), store=RandomSecretStore(), injector=Boom())
    assert not res.ok and any(r["ok"] is False for r in res.injected)


# ── rotation (throughout the vertical) ────────────────────────────────────────
def test_rotate_one_key_remints_and_reinjects_everywhere_it_is_used():
    plan = build_plan(_nodes(extra=True))
    store, inj = RandomSecretStore(), RecordingInjector()
    execute(plan, approve(plan, "chef"), store=store, injector=inj)
    old = store.get("prowlarr_api_key")
    inj.calls.clear()

    res = rotate(plan, store, injector=inj, keys=["prowlarr_api_key"])
    assert res.ok and res.rotated == ["prowlarr_api_key"]
    assert store.get("prowlarr_api_key") != old                # re-minted
    touched = {c.target for c in inj.calls}
    assert {"prowlarr", "sonarr"} <= touched                   # provider + every consumer
    assert all(c.key == "prowlarr_api_key" for c in inj.calls)  # ONLY the rotated key


def test_rotate_all_rotates_every_secret_in_the_vertical():
    plan = build_plan(_nodes(extra=True))
    store, inj = RandomSecretStore(), RecordingInjector()
    execute(plan, approve(plan, "chef"), store=store, injector=inj)
    res = rotate(plan, store, injector=inj)                     # keys=None → all
    assert res.ok and set(res.rotated) == plan.mints
