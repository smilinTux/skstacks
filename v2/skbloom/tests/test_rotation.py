"""Unified rotation: creds AND certs, automatic where possible. The plan shows every
rotating thing for an install — its cadence and whether it self-rotates."""
from __future__ import annotations

from pathlib import Path

import pytest

from skbloom.rotation import rotation_plan, Rotation
from skbloom.tls import cert_for, ca_bootstrap_manifests

V2 = str(Path(__file__).resolve().parents[2])


def test_certs_carry_duration_and_renewbefore_so_they_self_rotate():
    c = cert_for("login.sk.local", namespace="login")
    assert c["spec"]["duration"] and c["spec"]["renewBefore"]
    assert c["spec"]["privateKey"]["rotationPolicy"] == "Always"     # roll the key too
    ca = next(m for m in ca_bootstrap_manifests() if m["kind"] == "Certificate")
    assert ca["spec"]["duration"] and ca["spec"]["renewBefore"]


def test_rotation_plan_lists_each_secret_with_its_cadence():
    plan = rotation_plan(V2, ["cloud/skfence"])
    secrets = [r for r in plan if r.kind == "secret"]
    assert secrets and all(isinstance(r, Rotation) for r in secrets)
    dns = next(r for r in secrets if r.target == "cloudflare_dns_token")
    assert dns.every_days == 90 and dns.automatic is True          # backend+ESO auto-sync
    assert dns.service == "skfence"


def test_rotation_plan_adds_a_cert_entry_when_tls():
    plan = rotation_plan(V2, ["cloud/skfence"], tls=True)
    certs = [r for r in plan if r.kind == "cert"]
    assert certs and certs[0].automatic is True                    # cert-manager auto-renew
    assert certs[0].every_days > 0


def test_rotation_plan_skips_certs_without_tls():
    assert not any(r.kind == "cert" for r in rotation_plan(V2, ["cloud/skfence"], tls=False))


def test_secret_without_explicit_days_defaults_sensibly():
    # capauth secrets may not declare rotation_days → a safe default, still listed
    plan = rotation_plan(V2, ["core/capauth"])
    assert all(r.every_days >= 1 for r in plan if r.kind == "secret")
