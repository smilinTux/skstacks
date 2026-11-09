"""skfenceha dashboard protection, in order of preference:
DASHBOARD_SSO_MIDDLEWARE (e.g. authentik@file) > DASHBOARD_AUTH_HASH (basic
auth) > LAN-only allowlist. The dashboard router is never rendered without
one of them, so a forgotten setting cannot leave the Traefik API public."""
import pathlib

import jinja2
import pytest
import yaml

SRC = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/config/skfenceha"
AUTH = "admin:$2y$05$abcdefghijklmnopqrstuv"


def render(name, **v):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return yaml.safe_load(env.from_string((SRC / name).read_text()).render(
        env="prod", app="skfenceha", domain="example.com", cluster_name="cluster1", skfenceha=v))


def dash_mw(**v):
    return render("routers.yml.j2", **v)["http"]["routers"]["dashboard"]["middlewares"]


def test_sso_middleware_replaces_basic_auth():
    assert dash_mw(DASHBOARD_SSO_MIDDLEWARE="authentik@file", DASHBOARD_AUTH_HASH=AUTH) == [
        "dashboard-chain@file", "authentik@file"]


def test_hash_without_sso_uses_basic_auth():
    assert dash_mw(DASHBOARD_AUTH_HASH=AUTH) == ["dashboard-chain@file", "traefikAuth@file"]
    mw = render("dynamic-middlewares.yml.j2", DASHBOARD_AUTH_HASH=AUTH)["http"]["middlewares"]
    assert mw["traefikAuth"]["basicAuth"]["users"] == [AUTH]


@pytest.mark.parametrize("sso", [None, ""])
def test_nothing_set_is_lan_only(sso):
    v = {} if sso is None else {"DASHBOARD_SSO_MIDDLEWARE": sso}
    assert dash_mw(**v) == ["dashboard-chain@file", "traefikAuth@file"]
    assert "ipAllowList" in render("dynamic-middlewares.yml.j2", **v)["http"]["middlewares"]["traefikAuth"]


def test_dashboard_router_always_has_an_auth_middleware():
    for v in ({}, {"DASHBOARD_AUTH_HASH": AUTH}, {"DASHBOARD_SSO_MIDDLEWARE": "authentik@file"}):
        assert len(dash_mw(**v)) == 2, v
