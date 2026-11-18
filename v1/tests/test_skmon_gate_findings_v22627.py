"""skmon v2 faults the skstack06 tier-2 gate found (v2.26.27), locked in.

1. VictoriaMetrics and vmalert listen on IPv4 only (-enableTCP6 defaults to
   false). busybox wget resolves "localhost" to ::1 first and is refused, so
   a healthcheck on http://localhost:<port> never passes and Swarm stops the
   task over and over. Every wget healthcheck targets 127.0.0.1.
2. The dashboards tree ships SOURCES.yml (vendoring provenance). Copied to the
   top of Grafana's provisioning/dashboards it is parsed as a provider config
   and Grafana refuses to start. The deploy removes it after the copy.
"""
import pathlib
import re

import pytest
import yaml

SKMON = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmon"
COMPOSE = (SKMON / "src/config/skmon/skmon.yml.j2").read_text()
PLAYBOOKS = sorted(SKMON.glob("deploy_skmon-*.yml"))


def test_no_wget_healthcheck_targets_localhost():
    wgets = [l for l in COMPOSE.splitlines() if "wget" in l and "test:" in l]
    assert len(wgets) >= 4
    assert not [l for l in wgets if "localhost" in l]


@pytest.mark.parametrize("port", ["8428/health", "8880/health", "3000/api/health", "9093/-/healthy"])
def test_healthchecks_use_the_ipv4_loopback(port):
    assert f"http://127.0.0.1:{port}" in COMPOSE


def _tasks(pb):
    out = []
    def walk(ts):
        for t in ts or []:
            out.append(t)
            for k in ("block", "rescue", "always", "tasks"):
                walk(t.get(k))
    for play in yaml.safe_load(pb.read_text()):
        walk(play.get("tasks"))
    return out


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_sources_yml_is_removed_after_the_dashboards_copy(pb):
    names = [t.get("name", "") for t in _tasks(pb)]
    copy = names.index("Copy the dashboards tree to the data node")
    rm = next(i for i, n in enumerate(names) if "provenance file" in n)
    assert rm > copy
    t = _tasks(pb)[rm]
    assert t["file"]["state"] == "absent"
    assert t["file"]["path"].endswith("/grafana-provisioning/dashboards/SOURCES.yml")
    assert t["delegate_to"] == "{{ skmon_data_node }}"


def test_sources_yml_is_the_only_yaml_beside_the_boards():
    # dashboards.yml (the real provider config) is templated separately; any other
    # top-level YAML in the shipped tree would hit the same Grafana startup failure.
    top = [p.name for p in (SKMON / "src/config/skmon/dashboards").iterdir()
           if p.is_file() and re.search(r"\.ya?ml$", p.name)]
    assert top == ["SOURCES.yml"]


# 3. The agent is on the host network, so its port is reached through INPUT, where the
#    framework's swarm-firewall UFW (default deny incoming) dropped every scrape after
#    SKMON-<ENV> returned. With UFW active the agent port is opened in UFW; SKMON-<ENV>
#    still runs first and keeps the allowlist.
ALLOWLIST = yaml.safe_load((SKMON / "tasks/port_allowlist.yml").read_text())


def _named(prefix):
    return next(t for t in ALLOWLIST if t.get("name", "").startswith(prefix))


def test_ufw_state_is_probed_without_failing_or_changing():
    t = _named("Check whether UFW is active")
    assert t["command"] == "ufw status"
    assert t["failed_when"] is False and t["changed_when"] is False
    assert t["delegate_to"] == "{{ skmon_fw_node }}" and t["become"] is True


def test_active_needs_rc_0_and_the_status_line():
    expr = _named("Derive whether UFW is active")["set_fact"]["skmon_fw_ufw_active"]
    assert "'Status: active'" in expr and "rc" in expr


def _argv(state):
    import jinja2
    expr = _named("Open the agent port in UFW")["command"]["argv"]
    return jinja2.Environment().from_string(expr).render(
        skmon_fw_state=state, skmon_fw_ports={"agent": 12345}, env="dev")


def test_agent_port_opened_in_ufw_only_when_active():
    t = _named("Open the agent port in UFW")
    assert t["when"] == "skmon_fw_ufw_active | bool"
    assert t["delegate_to"] == "{{ skmon_fw_node }}" and t["become"] is True
    assert _argv("present") == str(["ufw", "allow", "12345/tcp", "comment", "skmon-dev agent (allowlist in SKMON-DEV)"])


def test_agent_port_rule_deleted_when_absent():
    assert _argv("absent") == str(["ufw", "delete", "allow", "12345/tcp"])


def test_existing_rule_is_no_change():
    assert "'Skipping' not in" in _named("Open the agent port in UFW")["changed_when"]


def test_only_the_agent_port_goes_through_ufw():
    # Loki, Pyroscope and /healthz are Swarm-published and bypass INPUT; opening them in UFW
    # would widen nothing useful and is not done.
    ufw = [t for t in ALLOWLIST if "ufw" in str(t.get("command", "")) and "allow" in str(t.get("command", ""))]
    assert len(ufw) == 1
