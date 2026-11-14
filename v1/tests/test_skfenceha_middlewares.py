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


def test_dashboard_auth_without_hash_is_lan_only_not_open():
    """No hash: the dashboard must not be public (was an empty chain until
    2026-11-09, which left one estate's dashboard and API open)."""
    src = render(DASHBOARD_AUTH_MODE="lan")["traefikAuth"]["ipAllowList"]["sourceRange"]
    assert "10.0.0.0/8" in src and "192.168.0.0/16" in src and "127.0.0.1/32" in src
    assert not any(c.startswith("0.0.0.0") or c == "::/0" for c in src)


def test_dashboard_fallback_cidrs_override():
    assert render(DASHBOARD_AUTH_MODE="lan", DASHBOARD_FALLBACK_ALLOW_CIDRS=["10.9.0.0/16"])["traefikAuth"]["ipAllowList"]["sourceRange"] == ["10.9.0.0/16"]


def test_dashboard_auth_basic_auth_when_hash_set():
    mw = render(DASHBOARD_AUTH_MODE="basic", DASHBOARD_AUTH_HASH="admin:$2y$10$examplehash")
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


def test_rate_limit_enabled_by_default():
    # Framework default (true) is fine for a fresh instance; skstack01-prod
    # sets this false in its vault because its live config has never had
    # rate limiting (see docs/runbooks/skfenceha.md).
    mw = render()
    assert "rate-limit@file" in mw["default"]["chain"]["middlewares"]
    assert "rate-limit@file" in mw["default-no-crowdsec"]["chain"]["middlewares"]
    assert "rate-limit@file" in mw["default-no-error-pages"]["chain"]["middlewares"]


def test_rate_limit_disabled_via_flag():
    mw = render(RATE_LIMIT_ENABLED=False)
    assert "rate-limit@file" not in mw["default"]["chain"]["middlewares"]
    assert "rate-limit@file" not in mw["default-no-crowdsec"]["chain"]["middlewares"]
    assert "rate-limit@file" not in mw["default-no-error-pages"]["chain"]["middlewares"]
    # The middleware definition itself still exists (harmless, reusable by
    # a future custom chain), only the built-in chains stop referencing it.
    assert "rate-limit" in mw


# -- dashboard auth mode (2026-11-14): SSO forward-auth is the only gate by default --

ROUTERS = MIDDLEWARES.parent / "routers.yml.j2"


def render_routers(**skfenceha_vars):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(ROUTERS.read_text()).render(
        skfenceha=skfenceha_vars, env="prod", cluster_name="c", domain="example.test")
    return yaml.safe_load(out)["http"]["routers"]


def test_dashboard_default_is_sso_only():
    """No basic auth and no allowlist by default: the forward-auth middleware
    is the only gate, so the SSO portal tile can open the dashboard."""
    assert "traefikAuth" not in render()
    mws = render_routers()["dashboard"]["middlewares"]
    assert "authentik@file" in mws
    assert "traefikAuth@file" not in mws


def test_dashboard_sso_mode_never_renders_the_htpasswd_even_if_the_hash_is_still_set():
    secret = "admin:$2y$10$examplehash"
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(MIDDLEWARES.read_text()).render(skfenceha={"DASHBOARD_AUTH_HASH": secret})
    assert secret not in out and "basicAuth" not in out
    assert "traefikAuth" not in yaml.safe_load(out)["http"]["middlewares"]


def test_dashboard_sso_middleware_name_is_overridable():
    mws = render_routers(DASHBOARD_SSO_MIDDLEWARE="authentik-admin@file")["dashboard"]["middlewares"]
    assert "authentik-admin@file" in mws and "authentik@file" not in mws


def test_dashboard_basic_mode_is_explicit_opt_in():
    mws = render_routers(DASHBOARD_AUTH_MODE="basic")["dashboard"]["middlewares"]
    assert "traefikAuth@file" in mws and "authentik@file" not in mws
    assert render(DASHBOARD_AUTH_MODE="basic", DASHBOARD_AUTH_HASH="u:h")["traefikAuth"]["basicAuth"]["users"] == ["u:h"]


def test_dashboard_basic_mode_without_a_hash_fails_safe_to_lan_only():
    assert "ipAllowList" in render(DASHBOARD_AUTH_MODE="basic")["traefikAuth"]


def test_dashboard_lan_mode_ignores_a_configured_hash():
    assert "ipAllowList" in render(DASHBOARD_AUTH_MODE="lan", DASHBOARD_AUTH_HASH="u:h")["traefikAuth"]


def test_authentik_forward_auth_middleware_is_always_defined():
    assert "forwardAuth" in render()["authentik"]
