"""skfenceha (and skfence)'s dashboard/API router always builds
`<name>[-env].<cluster>.<domain>` for its Host() rule. NAM's old dashboard
router had no cluster segment (`skfenceha.<domain>`) while every other
router in the same file kept building the full cluster-qualified hostname.
`skfenceha.DASHBOARD_HOST` (and `skfence.DASHBOARD_HOST`) is an optional
literal-hostname override applied only to the dashboard/API router group;
catch-all and the wildcard routers keep the normal computed hostname.
Default (unset) is unchanged."""
import pathlib

import jinja2
import pytest
import yaml

CASES = [
    ("skfence", pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfence/src/config/skfence/routers.yml.j2"),
    ("skfenceha", pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/config/skfenceha/routers.yml.j2"),
]


def render(path, key, **overrides):
    tpl_vars = {"ACME_ENABLED": False}
    tpl_vars.update(overrides)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(path.read_text()).render(
        env="prod", domain="example.com", cluster_name="cluster1",
        **{key: tpl_vars})
    return yaml.safe_load(out)["http"]["routers"]


@pytest.mark.parametrize("key,path", CASES)
def test_default_dashboard_host_is_unchanged(key, path):
    routers = render(path, key)
    expected = f"{key}.cluster1.example.com"
    assert routers["dashboard"]["rule"].startswith(f"Host(`{expected}`)")
    assert routers["catch-all"]["rule"] == f"Host(`{expected}`)"


@pytest.mark.parametrize("key,path", CASES)
def test_dashboard_host_override_applies_only_to_the_dashboard_group(key, path):
    override_host = f"{key}.example.com"
    routers = render(path, key, DASHBOARD_HOST=override_host)
    assert routers["dashboard"]["rule"].startswith(f"Host(`{override_host}`)")
    assert routers["dashboard-redirect-http"]["rule"].startswith(f"Host(`{override_host}`)")
    assert routers["dashboard-redirect-https"]["rule"].startswith(f"Host(`{override_host}`)")
    # unaffected: catch-all and both wildcard routers keep the computed host
    assert routers["catch-all"]["rule"] == f"Host(`{key}.cluster1.example.com`)"
    assert "cluster1.example.com" in routers["wildcard-cluster"]["rule"]
