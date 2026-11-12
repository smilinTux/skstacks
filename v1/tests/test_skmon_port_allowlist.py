"""skmon port allowlist (B4, R36-R38): one SKMON-<ENV> chain per host from one template."""
import subprocess

import yaml

from skmon_support import (AGENT_PLAY, FW_SCRIPT, PLAYS, PORT_ALLOWLIST, ROLE, eval_asserts, fw_context, fw_unit,
                           load_tasks, play_tasks, rules, shell_body, task_named)

ADDRS = ["192.0.2.10", "192.0.2.11"]
DATA = "node-1.render.example.test"
OTHER = "node-2.render.example.test"


def code(text: str) -> str:
    """The script without its comment lines."""
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_each_estate_address_allowed_then_drop():
    r = rules(ports={"loki": 3100})
    for a in ADDRS:
        assert f"-A SKMON-PROD -p tcp -m conntrack --ctorigdstport 3100 -s {a} -j RETURN" in r
    assert r.index("--ctorigdstport 3100 -j DROP") > max(r.index(a) for a in ADDRS)


def test_chain_hooked_from_docker_user_and_input():
    r = rules()
    assert "-I DOCKER-USER -j SKMON-PROD" in r and "-I INPUT -j SKMON-PROD" in r


def test_healthz_only_from_probe_hosts():
    r = rules(ports={"healthz": 9095}, HEALTHZ_ALLOW=["198.51.100.7"])
    assert "--ctorigdstport 9095 -s 198.51.100.7 -j RETURN" in r
    assert not any(f"--ctorigdstport 9095 -s {a}" in r for a in ADDRS)
    assert r.index("--ctorigdstport 9095 -j DROP") > r.index("198.51.100.7")


def test_shell_parses():
    body = shell_body(HEALTHZ_ALLOW=["198.51.100.7"], PROFILING={"enabled": True})
    assert "--ctorigdstport 4040" in body and "--ctorigdstport 9095" in body
    subprocess.run(["bash", "-n"], input=body, text=True, check=True)


def test_chain_name_is_the_upper_case_env():
    assert "SKMON-STAGING" in rules(env="staging") and "SKMON-PROD" not in rules(env="staging")


def test_agent_port_on_every_host_and_published_ports_only_on_data_node():
    assert fw_context(node=OTHER)["skmon_fw_ports"] == {"agent": 12345}
    assert fw_context(node=DATA)["skmon_fw_ports"] == {"agent": 12345, "loki": 3100, "healthz": 9095}
    r = rules(node=OTHER)
    assert "--ctorigdstport 12345 -j DROP" in r and "--ctorigdstport 3100" not in r and "--ctorigdstport 9095" not in r


def test_pyroscope_port_only_when_profiling_is_enabled():
    assert "pyroscope" not in fw_context()["skmon_fw_ports"]
    assert "pyroscope" not in fw_context(PROFILING={"enabled": False})["skmon_fw_ports"]
    assert fw_context(PROFILING={"enabled": "true"})["skmon_fw_ports"]["pyroscope"] == 4040
    assert "pyroscope" not in fw_context(node=OTHER, PROFILING={"enabled": True})["skmon_fw_ports"]


def test_port_knobs_are_honoured():
    ports = fw_context(AGENT_PORT=12400, LOKI_PUSH_PORT=3200, PYROSCOPE_PORT=4141, HEALTHZ_PORT=9199,
                       PROFILING={"enabled": True})["skmon_fw_ports"]
    assert ports == {"agent": 12400, "loki": 3200, "pyroscope": 4141, "healthz": 9199}


def test_loki_published_port_follows_the_knob_and_the_allowlist():
    from skmon_support import compose
    loki_ports = compose(LOKI_PUSH_PORT=3200)["services"]["loki"]["ports"]
    assert loki_ports == [{"target": 3100, "published": 3200, "protocol": "tcp", "mode": "host"}]
    assert compose()["services"]["loki"]["ports"][0]["published"] == 3100
    assert "--ctorigdstport 3200 -j DROP" in rules(LOKI_PUSH_PORT=3200)


def test_non_default_loki_port_rules_match_the_original_destination_port():  # R39
    from skmon_support import compose
    r = rules(LOKI_PUSH_PORT=3200)
    for a in ADDRS:
        assert f"-A SKMON-PROD -p tcp -m conntrack --ctorigdstport 3200 -s {a} -j RETURN" in r
    assert "-A SKMON-PROD -p tcp -m conntrack --ctorigdstport 3200 -j DROP" in r
    assert "--dport" not in code(r)
    port = compose(LOKI_PUSH_PORT=3200)["services"]["loki"]["ports"][0]
    assert port["target"] == 3100 and port["published"] == 3200


def test_agent_scrape_from_the_data_nodes_own_gwbridge_is_returned_before_drop():  # R37
    r = rules(node=DATA)
    gw = "-A SKMON-PROD -i docker_gwbridge -p tcp -m conntrack --ctorigdstport 12345 -j RETURN"
    assert gw in r and r.index(gw) < r.index("--ctorigdstport 12345 -j DROP")
    assert gw not in rules(node=OTHER)


def test_sources_are_deduplicated_estate_addresses_only():
    r = rules(AGENT_LISTEN_ADDR={DATA: "192.0.2.11"}, ports={"agent": 12345})
    assert r.count("--ctorigdstport 12345 -s 192.0.2.11 -j RETURN") == 1
    assert "-s 192.0.2.10 " not in r  # that host is no longer an agent address
    assert "198.51.100.7" not in r  # healthz probes never reach the agent port


def test_ruleset_is_one_atomic_restore_of_the_filter_table():
    r = rules()
    assert code(r).count("iptables-restore") == 1 and "--noflush" in r
    body = r.split("<<'SKMON_RULES'\n")[1].split("\nSKMON_RULES\n")[0] + "\n"
    assert body.startswith("*filter\n:SKMON-PROD - [0:0]\n-F SKMON-PROD\n") and body.endswith("COMMIT\n")
    assert all(l.startswith(("-A SKMON-PROD", "*", ":", "-F", "COMMIT")) for l in body.splitlines())


def test_hooks_are_inserted_only_when_missing():
    r = rules()
    for parent in ("INPUT", "DOCKER-USER"):
        assert f"iptables -w -C {parent} -j SKMON-PROD 2>/dev/null || iptables -w -I {parent} -j SKMON-PROD" in r
    assert "iptables -w -N DOCKER-USER 2>/dev/null || true" in r


def test_no_docker_means_no_docker_user_hook():
    r = rules(docker=False)
    assert "DOCKER-USER" not in code(r) and "-I INPUT -j SKMON-PROD" in r
    subprocess.run(["bash", "-n"], input=r, text=True, check=True)


def test_unit_is_a_remain_after_exit_oneshot_after_docker():
    u = fw_unit()
    assert "Type=oneshot" in u and "RemainAfterExit=yes" in u and "After=docker.service" in u
    assert "WantedBy=multi-user.target" in u and "ExecStart=/usr/local/sbin/skmon-prod-fw" in u
    assert "PartOf=docker.service" in u and "PartOf" not in fw_unit(docker=False)


def test_ipv6_is_documented_as_unfiltered():
    assert "IPv6 is not filtered" in rules()


def test_estate_address_that_is_a_name_is_refused():
    bad = fw_context(AGENT_LISTEN_ADDR={OTHER: "node-2.example.test"})
    msgs = eval_asserts(PORT_ALLOWLIST, bad)
    assert msgs and "node-2.example.test" in msgs[0]


def test_ipv4_addresses_and_probe_cidrs_pass_the_guard():
    assert eval_asserts(PORT_ALLOWLIST, fw_context(HEALTHZ_ALLOW=["198.51.100.7", "203.0.113.0/24"])) == []
    assert eval_asserts(PORT_ALLOWLIST, fw_context(HEALTHZ_ALLOW=["2001:db8::1"]))
    assert eval_asserts(PORT_ALLOWLIST, fw_context(HEALTHZ_ALLOW=["198.51.100.7; reboot"]))


def test_tasks_install_script_and_unit_and_rerun_on_change():
    tasks = load_tasks(PORT_ALLOWLIST)
    script = task_named(PORT_ALLOWLIST, "Write the port allowlist script")
    assert script["template"]["dest"] == "/usr/local/sbin/skmon-{{ env }}-fw" and script["template"]["mode"] == "0750"
    unit = task_named(PORT_ALLOWLIST, "Write the port allowlist unit")
    assert unit["template"]["dest"] == "/etc/systemd/system/skmon-{{ env }}-fw.service"
    run = task_named(PORT_ALLOWLIST, "Enable and run the port allowlist")["systemd"]
    assert run["enabled"] is True and run["state"] == "started"
    rerun = task_named(PORT_ALLOWLIST, "Re-run the port allowlist when its script or unit changed")
    assert rerun["systemd"]["state"] == "restarted" and "skmon_fw_script.changed" in rerun["when"]
    assert not any("skmon_parked" in str(t.get("when", "")) for t in tasks)  # parking leaves the rules


def test_every_host_touching_task_is_delegated_to_the_firewall_node():
    for t in load_tasks(PORT_ALLOWLIST):
        if "set_fact" not in t:
            assert t["delegate_to"] == "{{ skmon_fw_node }}", t["name"]


def test_absent_removes_unit_script_hooks_and_chain():
    names = [t["name"] for t in load_tasks(PORT_ALLOWLIST)]
    assert "Remove the port allowlist unit and script when absent" in names
    rm = task_named(PORT_ALLOWLIST, "Remove the port allowlist hooks and chain when absent")
    assert "-D" in rm["shell"] and "-X" in rm["shell"] and "INPUT DOCKER-USER" in rm["shell"]
    assert "absent'" in task_named(PORT_ALLOWLIST, "Remove the port allowlist unit and script when absent")["when"]
    install = [t for t in load_tasks(PORT_ALLOWLIST) if "Write the port allowlist" in t["name"]]
    assert all("!= 'absent'" in t["when"] for t in install)
    subprocess.run(["bash", "-n"], input=rm["shell"].replace("{{ env | upper }}", "PROD"), text=True, check=True)


def _include(tasks):
    return [t for t in tasks if "port_allowlist.yml" in str(t.get("include_tasks", ""))]


def test_agent_play_includes_it_on_every_host_and_removes_on_absent_systemd():
    inc = _include(play_tasks(AGENT_PLAY))
    assert len(inc) == 1 and "when" not in inc[0]
    assert inc[0]["vars"]["skmon_fw_node"] == "{{ inventory_hostname }}"
    assert "'systemd' and skmon_agent_state == 'absent'" in inc[0]["vars"]["skmon_fw_state"]


def test_env_plays_include_it_for_data_node_identically():
    incs = [_include(play_tasks(p)) for p in PLAYS]
    assert all(len(i) == 1 for i in incs) and incs[0] == incs[1] == incs[2]
    assert incs[0][0]["vars"] == {"skmon_fw_node": "{{ skmon_data_node }}"}
    assert "always" in incs[0][0]["tags"]


def test_one_template_serves_every_host():
    assert FW_SCRIPT.exists() and not list((ROLE / "src/config/skmon/firewall").glob("*.sh"))
    assert yaml.safe_load(PORT_ALLOWLIST.read_text())
