"""Guard tests for the secure-DNS resolution options."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_RES = Path(__file__).resolve().parents[1]


def test_all_resolution_options_present():
    for opt in ("technitium", "blockchain", "failover"):
        assert (_RES / opt).exists(), f"missing resolution option {opt}"


def test_readme_covers_secure_blockchain_managed_selfhost_failover():
    r = (_RES / "README.md").read_text().lower()
    for kw in ("doh", "dot", "handshake", "ens", "cloudflare", "technitium", "failover", "tailscale"):
        assert kw in r, f"README missing {kw}"


def test_technitium_serves_encrypted_transport_secret_external():
    doc = yaml.safe_load((_RES / "technitium" / "docker-compose.yml").read_text())
    env = " ".join(doc["services"]["technitium"]["environment"])
    assert "DNS_OVER_HTTPS=true" in env and "DNS_OVER_TLS=true" in env
    assert doc["secrets"]["technitium_admin_password"]["external"] is True


def test_failover_policy_multiprovider_low_ttl_dnssec():
    pol = yaml.safe_load((_RES / "failover" / "failover-policy.yaml").read_text())
    spec = pol["spec"]
    assert len(spec["providers"]) >= 2                  # no single point of takedown
    assert spec["failover"]["dnssec"] is True
    assert spec["failover"]["ttl"] <= 300               # fast failover


def test_skdns_descriptor_default_technitium_with_options():
    app = yaml.safe_load((_RES.parent / "app.yaml").read_text())
    assert "Technitium" in app["provider"]
    assert set(app["resolution"]) >= {"technitium", "cloudflare", "blockchain", "failover"}
    assert set(app["secure_transport"]) >= {"DoH", "DoT", "DoQ"}
