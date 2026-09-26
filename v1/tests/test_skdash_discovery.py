"""skdash discovery: generic Traefik-API -> Dashy sections transform.

The transform lives in a plain Python module (an Ansible filter plugin) with
no Ansible dependency, so it is exercised here directly against a recorded,
estate-free Traefik `/api/http/routers` fixture. This is the framework-level
replacement for the old estate-specific SKStacks patterns; see
v1/docs/skdash-discovery.md for the design.
"""
import importlib.util
import json
import pathlib

V1 = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = V1 / "ansible/optional/skdash/filter_plugins/skdash_discovery.py"
FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "traefik_routers.json"


def _load_module():
    spec = importlib.util.spec_from_file_location("skdash_discovery", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _routers():
    return json.loads(FIXTURE.read_text())


def test_routers_without_a_host_rule_are_skipped():
    mod = _load_module()
    sections = mod.routers_to_sections(_routers())
    all_hosts = {item["description"] for section in sections for item in section["items"]}
    assert "PathPrefix(`/`)" not in all_hosts
    assert len(all_hosts) == 4  # 5 routers in, 1 has no Host() rule


def test_exclude_patterns_drop_matching_routers_by_name_or_host():
    mod = _load_module()
    sections = mod.routers_to_sections(_routers(), exclude_patterns=[r"^traefik"])
    titles = {item["title"] for section in sections for item in section["items"]}
    assert "Traefik Dashboard" not in titles
    assert "Grafana" in titles


def test_group_map_buckets_matching_routers_into_a_named_section():
    mod = _load_module()
    sections = mod.routers_to_sections(
        _routers(),
        exclude_patterns=[r"^traefik", r"^catchall"],
        group_map=[{"pattern": r"^grafana", "group": "Monitoring", "icon": "grafana"}],
    )
    by_name = {s["name"]: s for s in sections}
    assert "Monitoring" in by_name
    monitoring_titles = sorted(i["title"] for i in by_name["Monitoring"]["items"])
    assert monitoring_titles == ["Grafana", "Grafana Dev"]
    assert by_name["Monitoring"]["icon"] == "grafana"
    # wiki-notes matched no group_map entry, so it lands in the default group
    assert "Discovered Services" in by_name
    other_titles = [i["title"] for i in by_name["Discovered Services"]["items"]]
    assert other_titles == ["Wiki Notes"]


def test_entrypoint_security_controls_the_generated_url_scheme():
    mod = _load_module()
    sections = mod.routers_to_sections(_routers(), exclude_patterns=[r"^traefik", r"^catchall"])
    items = {i["title"]: i for s in sections for i in s["items"]}
    assert items["Grafana"]["url"] == "https://grafana.example.test"
    assert items["Grafana Dev"]["url"] == "http://grafana-dev.example.test"


def test_default_group_name_is_configurable():
    mod = _load_module()
    sections = mod.routers_to_sections(
        _routers(),
        exclude_patterns=[r"^traefik", r"^catchall"],
        default_group="Everything Else",
    )
    names = {s["name"] for s in sections}
    assert "Everything Else" in names
    assert "Discovered Services" not in names


def test_non_list_input_yields_no_sections():
    mod = _load_module()
    assert mod.routers_to_sections(None) == []
    assert mod.routers_to_sections({"error": "unreachable"}) == []


def test_select_sections_uses_discovery_when_enabled_and_available():
    mod = _load_module()
    discovered = [{"name": "Discovered Services", "items": [{"title": "Grafana"}]}]
    static = [{"name": "Static", "items": [{"title": "Manual Link"}]}]
    got = mod.select_sections(
        discovery_enabled=True, discovery_available=True,
        discovered_sections=discovered, static_sections=static,
    )
    assert got == discovered


def test_select_sections_appends_extra_sections_when_discovery_is_used():
    mod = _load_module()
    discovered = [{"name": "Discovered Services", "items": []}]
    extra = [{"name": "Pinned", "items": [{"title": "Runbook"}]}]
    got = mod.select_sections(
        discovery_enabled=True, discovery_available=True,
        discovered_sections=discovered, static_sections=[], extra_sections=extra,
    )
    assert got == discovered + extra


def test_select_sections_falls_back_to_static_when_discovery_is_off():
    mod = _load_module()
    discovered = [{"name": "Discovered Services", "items": [{"title": "Grafana"}]}]
    static = [{"name": "Static", "items": [{"title": "Manual Link"}]}]
    got = mod.select_sections(
        discovery_enabled=False, discovery_available=False,
        discovered_sections=discovered, static_sections=static,
    )
    assert got == static


def test_select_sections_falls_back_to_static_when_the_api_is_unreachable():
    mod = _load_module()
    static = [{"name": "Static", "items": [{"title": "Manual Link"}]}]
    got = mod.select_sections(
        discovery_enabled=True, discovery_available=False,
        discovered_sections=[], static_sections=static,
    )
    assert got == static
