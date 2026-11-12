"""skmon v2 Alertmanager (spec 8.1, 8.2, 9.1, 9.5): timers, routes, receivers, inhibition tree."""
import yaml

from skmon_support import CFG, render, route_for, services


def text(env: str = "prod") -> str:
    return render(CFG / "alertmanager-config.yml.j2", env=env)


def am(env: str = "prod") -> dict:
    return yaml.safe_load(text(env))


def route(severity: str, cls: str, **more) -> dict:
    return route_for(text(), severity=severity, **{"class": cls}, **more)


def test_grouping_and_timers():
    r = am()["route"]
    assert r["group_by"] == ["alertname", "estate", "class"] and r["group_wait"] == "30s" and r["group_interval"] == "2m"
    assert r["receiver"] == "bridge"


def test_fast_group_wait_for_critical_storage_and_node():
    fast = [c for c in am()["route"]["routes"] if c.get("group_wait") == "5s"]
    assert {tuple(sorted(c["matchers"])) for c in fast} == {('class=~"storage|node"', 'severity="critical"')}
    assert route("critical", "storage")["group_wait"] == "5s" and route("critical", "node")["group_wait"] == "5s"
    for sev, cls in (("critical", "swarm"), ("warning", "storage"), ("warning", "node"), ("critical", "http")):
        assert route(sev, cls)["group_wait"] == "30s", (sev, cls)


def test_repeat_intervals():
    assert route("critical", "storage")["repeat_interval"] == "4h"
    assert route("critical", "swarm")["repeat_interval"] == "4h"
    assert route("warning", "swarm")["repeat_interval"] == "24h"
    assert route("warning", "storage")["repeat_interval"] == "24h"


def test_watchdog_route():
    routes = am()["route"]["routes"]
    assert routes[0]["receiver"] == "watchdog"
    w = next(c for c in routes if c["receiver"] == "watchdog")
    assert w["matchers"] == ['alertname="Watchdog"'] and w["repeat_interval"] == "1m" and w.get("continue") is not True
    got = route_for(text(), alertname="Watchdog", severity="none", **{"class": "meta"})
    assert got["receiver"] == "watchdog" and got["repeat_interval"] == "1m"
    assert route("critical", "storage")["receiver"] == "bridge"


def test_route_for_honours_continue_and_inheritance():
    cfg = yaml.safe_dump({"route": {"receiver": "r", "group_wait": "30s", "repeat_interval": "1h", "routes": [
        {"matchers": ['a="1"'], "receiver": "first", "continue": True},
        {"matchers": ['a=~"1|2"'], "group_wait": "5s"}]}})
    got = route_for(cfg, a="1")
    assert got["receiver"] == "first" and got["group_wait"] == "30s" and got["repeat_interval"] == "1h"
    assert route_for(cfg, a="3")["receiver"] == "r"


def test_inhibition_tree_spec_8_2():
    ir = am()["inhibit_rules"]
    assert len(ir) == 4
    assert ir[0]["source_matchers"] == ['alertname=~"NodeDown|SwarmNodeNotReady"', 'severity="critical"'] and ir[0]["equal"] == ["node"]
    assert ir[0]["target_matchers"] == ['alertname=~".+"']
    assert ir[1]["source_matchers"] == ['class="storage"', 'alertname!="IoPressureHigh"']
    assert set(ir[1]["target_matchers"][0].split('"')[1].split("|")) == {
        "ServiceBelowReplicas", "TasksStuckPreparing", "ContainerCrashloop", "HttpEndpointDown", "IoPressureHigh"}
    assert ir[1]["equal"] == ["estate"]
    assert ir[2]["source_matchers"] == ['alertname="AuthentikDown"'] and ir[2]["target_matchers"] == ['alertname="HttpEndpointDown"', 'forward_auth="true"']
    assert ir[3]["source_matchers"] == ['severity="critical"'] and ir[3]["target_matchers"] == ['severity="warning"']
    assert ir[3]["equal"] == ["alertname", "node", "instance", "service"]


def test_receivers_address_the_bridge_by_full_service_name():
    for env in ("prod", "dev"):
        rc = {r["name"]: r["webhook_configs"] for r in am(env)["receivers"]}
        assert set(rc) == {"bridge", "watchdog"}
        assert rc["bridge"] == [{"url": f"http://skmon-{env}_bridge:8080/alert", "send_resolved": True}]
        assert [w["url"] for w in rc["watchdog"]] == [f"http://skmon-{env}_bridge:8080/watchdog"]


def test_alertmanager_has_no_var_data():
    assert not any(str(v).startswith("/var/data") for v in services()["alertmanager"].get("volumes", []))
    assert any(str(v).endswith(":/alertmanager") for v in services()["alertmanager"]["volumes"])


def test_no_short_service_aliases_anywhere():
    """R46: on the shared overlay a short alias can resolve to another stack's service."""
    import re
    names = "victoriametrics|loki|alertmanager|vmalert|bridge|grafana"
    for tpl in ("alertmanager-config.yml.j2", "vm-scrape.yml.j2", "loki-config.yml.j2", "grafana/datasources.yml.j2", "skmon.yml.j2"):
        body = (CFG / tpl).read_text()
        assert not re.search(rf"http://(?:{names})[:/]", body), tpl
