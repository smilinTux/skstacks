"""skfenceha closes the gap documented in docs/runbooks/skfence.md (private
skstack01-prod repo, 2026-09-25): framework's skfence has no CrowdSec
bouncer wiring, no IP allowlisting, and a different dashboard-auth schema
than the private skfenceha it was compared against. This test locks in that
skfenceha (unlike skfence) actually renders all three, opt-in and
empty-by-default so a fresh instance's behaviour is unchanged until an
operator sets the corresponding instance var."""
import pathlib

import jinja2
import yaml

MIDDLEWARES = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/config/skfenceha/dynamic-middlewares.yml.j2"


def render(**skfenceha_vars):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(MIDDLEWARES.read_text()).render(skfenceha=skfenceha_vars)
    return yaml.safe_load(out)["http"]["middlewares"]


def test_crowdsec_disabled_by_default():
    mw = render()
    assert "crowdsec-bouncer@file" not in mw["default"]["chain"]["middlewares"]


def test_crowdsec_opt_in():
    mw = render(CROWDSEC_ENABLED=True)
    assert "crowdsec-bouncer@file" in mw["default"]["chain"]["middlewares"]


def test_whitelist_empty_by_default():
    mw = render()
    assert "ipwhitelist" not in mw
    assert "ipwhitelist@file" not in mw["default"]["chain"]["middlewares"]


def test_whitelist_renders_source_range_when_set():
    mw = render(WHITELIST_IPS=["10.0.0.0/8", "203.0.113.5/32"])
    assert mw["ipwhitelist"]["ipAllowList"]["sourceRange"] == ["10.0.0.0/8", "203.0.113.5/32"]
    assert "ipwhitelist@file" in mw["default"]["chain"]["middlewares"]


def test_dashboard_auth_stub_when_hash_empty():
    mw = render()
    assert mw["traefikAuth"] == {"chain": {"middlewares": []}}


def test_dashboard_auth_basic_auth_when_hash_set():
    mw = render(DASHBOARD_AUTH_HASH="admin:$2y$10$examplehash")
    assert mw["traefikAuth"]["basicAuth"]["users"] == ["admin:$2y$10$examplehash"]


def test_rate_limit_defaults_and_override():
    assert render()["rate-limit"]["rateLimit"]["average"] == 100
    assert render(RATE_LIMIT_AVERAGE=25)["rate-limit"]["rateLimit"]["average"] == 25


def test_cors_absent_by_default():
    mw = render()
    assert "accessControlAllowOriginList" not in mw["default-security-headers"]["headers"]


def test_cors_renders_when_origins_set():
    mw = render(CORS_ALLOWED_ORIGINS=["https://skdash.example.com"])
    headers = mw["default-security-headers"]["headers"]
    assert headers["accessControlAllowOriginList"] == ["https://skdash.example.com"]


def test_cross_service_middlewares_present():
    # These are not used by any router in this file - other services
    # (skgit, skport, skai, skgallery, ...) reference them by name via
    # their own Docker labels through Traefik's shared file-provider
    # namespace. Losing one silently breaks whichever service used it.
    mw = render()
    for name in ("authentik", "default-no-crowdsec", "default-no-error-pages",
                 "skport-headers", "skai-headers", "skai-chain",
                 "large-upload-buffering", "upload-chain",
                 "upload-chain-no-crowdsec", "error-pages-all"):
        assert name in mw, f"missing cross-service middleware: {name}"
