"""skmon dead-man's switch wiring (Tasks 10 to 13 end to end): the Watchdog rule, its route, the bridge healthz."""
import re

import pytest
import yaml

from skmon_support import CFG, fw_context, get, load, post, render, rule, rules, services, server, server_failing  # noqa: F401

import skmon_bridge  # noqa: E402  (on sys.path via skmon_support)

HEALTHZ_WINDOW_S = skmon_bridge.WATCHDOG_MAX_AGE_S  # the bridge reports unhealthy when no Watchdog arrived within this window
DATA_NODE = "node-1.render.example.test"


def am() -> dict:
    return yaml.safe_load(render(CFG / "alertmanager-config.yml.j2"))


def watchdog_route() -> dict:
    """The child route of the root that matches the Watchdog alert."""
    found = [c for c in am()["route"]["routes"] if 'alertname="Watchdog"' in c.get("matchers", [])]
    assert len(found) == 1
    return found[0]


def dur(text: str) -> int:
    """Seconds of a Prometheus duration like 15s, 1m, 2h."""
    return sum(int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u] for n, u in re.findall(r"(\d+)([smhd])", text))


def test_watchdog_always_fires_and_has_no_person_route():
    assert rule("Watchdog")["expr"] == "vector(1)"
    watchdog_routes = [c for c in am()["route"]["routes"] if 'alertname="Watchdog"' in c.get("matchers", [])]
    assert watchdog_routes and all(c["receiver"] != "bridge" for c in watchdog_routes)
    assert am()["route"]["routes"][0] == watchdog_route()  # first under the root
    assert watchdog_route().get("continue") is not True  # it never falls through to the person route
    receiver = next(r for r in am()["receivers"] if r["name"] == watchdog_route()["receiver"])
    assert [w["url"].rsplit("/", 1)[1] for w in receiver["webhook_configs"]] == ["watchdog"]


def test_watchdog_repeat_is_shorter_than_healthz_window():
    assert dur(watchdog_route()["repeat_interval"]) * 3 <= HEALTHZ_WINDOW_S


def test_watchdog_post_updates_healthz(server):
    post(server, "/watchdog", load("am_watchdog.json"))
    assert get(server, "/healthz", main=False).status == 200


def test_delivery_failure_makes_healthz_fail(server_failing):
    post(server_failing, "/watchdog", load("am_watchdog.json"))
    post(server_failing, "/alert", load("am_firing.json"))
    assert get(server_failing, "/healthz", main=False).status != 200


def test_healthz_port_is_published_in_host_mode_and_allowlisted_from_healthz_allow():
    port = services()["bridge"]["ports"]
    assert len(port) == 1 and port[0]["mode"] == "host"
    published = port[0]["published"]
    assert fw_context(node=DATA_NODE)["skmon_fw_ports"]["healthz"] == published
    r = rules(node=DATA_NODE, HEALTHZ_ALLOW=["198.51.100.7"])
    assert f"--ctorigdstport {published} --ctdir ORIGINAL -s 198.51.100.7 -j RETURN" in r
    assert f"--ctorigdstport {published} --ctdir ORIGINAL -j DROP" in r
