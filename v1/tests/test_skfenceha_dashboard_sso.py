"""skfenceha dashboard protection, chosen by DASHBOARD_AUTH_MODE:
sso (the default, the authentik forward-auth middleware and nothing else) |
basic (DASHBOARD_AUTH_HASH, explicit opt-in) | lan (allowlist only). Basic auth
used to be the default whenever a hash was set, which left the SSO portal tile
for the dashboard on a 401 it could never satisfy. The dashboard router is
never rendered without a gate, so a forgotten setting cannot leave the Traefik
API public."""
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


def test_hash_without_explicit_basic_mode_is_still_sso_only():
    assert dash_mw(DASHBOARD_AUTH_HASH=AUTH) == ["dashboard-chain@file", "authentik@file"]
    mw = render("dynamic-middlewares.yml.j2", DASHBOARD_AUTH_HASH=AUTH)["http"]["middlewares"]
    assert "traefikAuth" not in mw


def test_basic_mode_uses_the_hash():
    assert dash_mw(DASHBOARD_AUTH_MODE="basic", DASHBOARD_AUTH_HASH=AUTH) == [
        "dashboard-chain@file", "traefikAuth@file"]
    mw = render("dynamic-middlewares.yml.j2", DASHBOARD_AUTH_MODE="basic", DASHBOARD_AUTH_HASH=AUTH)["http"]["middlewares"]
    assert mw["traefikAuth"]["basicAuth"]["users"] == [AUTH]


@pytest.mark.parametrize("sso", [None, ""])
def test_nothing_set_is_sso_only(sso):
    v = {} if sso is None else {"DASHBOARD_SSO_MIDDLEWARE": sso}
    assert dash_mw(**v) == ["dashboard-chain@file", "authentik@file"]
    assert "traefikAuth" not in render("dynamic-middlewares.yml.j2", **v)["http"]["middlewares"]


def test_lan_mode_is_lan_only():
    assert dash_mw(DASHBOARD_AUTH_MODE="lan") == ["dashboard-chain@file", "traefikAuth@file"]
    assert "ipAllowList" in render("dynamic-middlewares.yml.j2", DASHBOARD_AUTH_MODE="lan")["http"]["middlewares"]["traefikAuth"]


def test_dashboard_router_always_has_an_auth_middleware():
    for v in ({}, {"DASHBOARD_AUTH_HASH": AUTH}, {"DASHBOARD_SSO_MIDDLEWARE": "authentik@file"}):
        assert len(dash_mw(**v)) == 2, v


# -- the dynamic directory must not contain subdirectories (shadowing) --------

PLAYBOOKS = sorted((SRC.parents[2]).glob("deploy_skfenceha-*.yml"))


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.stem)
def test_dynamic_dir_subdirectory_guard_runs_before_the_render(playbook):
    """Traefik reads the dynamic directory recursively and the first definition
    wins, so a leftover `.bak-*` directory silently shadowed every rendered
    change on a production edge. The play must refuse to render over one."""
    tasks = yaml.safe_load(playbook.read_text())[2]["tasks"]
    names = [t.get("name", "") for t in tasks]
    guard = names.index("Refuse to deploy dynamic config while a subdirectory shadows it")
    render = next(i for i, n in enumerate(names) if n.startswith("Deploy dynamic config"))
    assert names.index("Find directories inside the dynamic config directories (they shadow the rendered files)") < guard < render
    find = next(t for t in tasks if t.get("name", "").startswith("Find directories"))["find"]
    assert find["hidden"] is True and find["file_type"] == "directory" and find["recurse"] is False
    assert any("dynamic_acme" in p for p in find["paths"])


def test_dynamic_dir_guard_has_an_explicit_opt_out():
    text = PLAYBOOKS[0].read_text()
    assert "skfenceha.ALLOW_DYNAMIC_SUBDIRS" in text
