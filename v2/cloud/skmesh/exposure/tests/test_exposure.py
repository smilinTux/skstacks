"""Guard tests for the pluggable ingress exposure adapters."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_EXP = Path(__file__).resolve().parents[1]
_ADAPTERS = ["lan", "tailscale", "cloudflared", "netbird", "pangolin"]


def test_all_exposure_adapters_present():
    for a in _ADAPTERS:
        assert (_EXP / a).is_dir(), f"missing exposure adapter {a}"


def test_readme_documents_adapters_stackability_and_catch22():
    r = (_EXP / "README.md").read_text().lower()
    for a in _ADAPTERS:
        assert a in r
    assert "stackable" in r and "catch-22" in r
    assert "lan" in r  # the always-available bootstrap baseline


@pytest.mark.parametrize("manifest", [
    "cloudflared/k8s-cloudflared.yaml",
    "tailscale/k8s-tailscale.yaml",
    "netbird/k8s-netbird.yaml",
])
def test_k8s_manifests_parse(manifest):
    docs = [d for d in yaml.safe_load_all((_EXP / manifest).read_text()) if d]
    assert docs and all("kind" in d for d in docs)


@pytest.mark.parametrize("stack", [
    "cloudflared/swarm-cloudflared.yml",
    "tailscale/swarm-tailscale.yml",
    "netbird/swarm-netbird.yml",
])
def test_swarm_stacks_route_to_traefik_network(stack):
    doc = yaml.safe_load((_EXP / stack).read_text())
    assert any(n.get("external") for n in doc["networks"].values())


def test_tailscale_readme_covers_headscale_maturity_and_bootstrap_order():
    r = (_EXP / "tailscale" / "README.md").read_text().lower()
    assert "headscale" in r and "pre-1.0" in r          # honest maturity
    assert "bootstrap ordering" in r and "lan" in r     # the catch-22 resolution


def test_both_ladders_ease_to_sovereign():
    import yaml  # noqa
    app = yaml.safe_load((_EXP.parent / "app.yaml").read_text())
    assert app["mesh_ladder"] == ["tailscale", "netbird"]          # private: ease → sovereign
    assert app["tunnel_ladder"] == ["cloudflared", "pangolin"]     # public: ease → sovereign
    assert "sovereign" in app["provider"].lower() and "Netbird" in app["provider"]
    assert any("not recommended" in str(a).lower() and "headscale" in str(a).lower()
               for a in app["alternates"])


def test_pangolin_is_sovereign_cloudflared_replacement_no_inbound():
    import yaml  # noqa
    doc = yaml.safe_load((_EXP / "pangolin" / "swarm-newt.yml").read_text())
    # Newt is outbound-only (no published ports) and reaches the public Pangolin node
    assert "ports" not in doc["services"]["newt"]
    env = " ".join(doc["services"]["newt"]["environment"])
    assert "PANGOLIN_ENDPOINT" in env
    r = (_EXP / "pangolin" / "README.md").read_text().lower()
    assert "cloudflared" in r and "$100k" in r          # role + license threshold


def test_netbird_documents_bootstrap_catch22():
    for f in (_EXP / "netbird").glob("*"):
        if f.suffix in (".yaml", ".yml"):
            assert "catch-22" in f.read_text().lower() or "bootstrap caveat" in f.read_text().lower()
