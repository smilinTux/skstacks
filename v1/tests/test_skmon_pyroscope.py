"""skmon v2 optional Pyroscope and opt-in eBPF profiling per host (Task 22, R75, R76)."""
import re

import pytest
import yaml

from skmon_support import (AGENT_GUARDS, CFG, DEPLOY, FACTS_FILE, agent_ctx, cfg, compose, data_dir_items,
                           eval_asserts, guard_facts, render, render_deploy, rules, router_hosts, run_script_args,
                           services, tile_items)

ON = {"enabled": True, "hosts": ["h1"]}
DATA = "node-1.render.example.test"


@pytest.mark.parametrize("v", [False, "false", "no", 0])
def test_profiling_string_false_is_off(v):
    p = {"enabled": v, "hosts": ["h1"]}
    assert services(PROFILING=p)["pyroscope"]["deploy"]["replicas"] == 0
    assert "pyroscope" not in cfg(**agent_ctx("h1", PROFILING=p))
    assert "--cap-add" not in run_script_args(**agent_ctx("h1", PROFILING=p))
    assert "pyroscope" not in render_deploy(PROFILING=p)
    assert "pyroscope" not in render(CFG / "vm-scrape.yml.j2", extra_ctx=_scrape_ctx(), PROFILING=p)
    assert "skmon-pyroscope" not in render(CFG / "grafana/datasources.yml.j2", PROFILING=p)


def _scrape_ctx():
    from skmon_support import agent_targets
    return {"skmon_agent_targets": agent_targets(), "skmon_probes": []}


def test_profiling_only_on_listed_hosts():
    assert "pyroscope.ebpf" in cfg(**agent_ctx("h1", PROFILING=ON))
    assert "pyroscope.ebpf" not in cfg(**agent_ctx("h2", PROFILING=ON))


def test_ebpf_block_pushes_to_data_node_with_the_port_knob():
    c = cfg(**agent_ctx("h1", PROFILING=ON, PYROSCOPE_PORT=4141))
    assert 'url = "http://192.0.2.10:4141"' in c
    assert "pyroscope.write" in c and "pyroscope.ebpf" in c and "estate" in c
    assert 'url = "http://192.0.2.10:4040"' in cfg(**agent_ctx("h1", PROFILING=ON))


def test_profiling_agent_caps_and_memory():
    a = run_script_args(**agent_ctx("h1", PROFILING=ON))
    assert set(a.getall("--cap-add")) == {"BPF", "PERFMON", "SYS_ADMIN", "SYS_PTRACE"} and a["--memory"] == "640m"
    assert "--privileged" not in a and "SYS_RESOURCE" not in a.getall("--cap-add")


def test_tracefs_mount_only_for_profiling_agents():
    mount = "/sys/kernel/tracing:/sys/kernel/tracing:ro"
    assert mount in run_script_args(**agent_ctx("h1", PROFILING=ON)).getall("-v")
    assert mount not in run_script_args(**agent_ctx("h2", PROFILING=ON)).getall("-v")
    assert mount not in run_script_args().getall("-v")


def test_non_profiling_agents_keep_their_flags():
    base = run_script_args()
    for ctx in (agent_ctx("h2", PROFILING=ON), agent_ctx("h1", PROFILING={"enabled": False, "hosts": ["h1"]})):
        a = run_script_args(**ctx)
        assert a.pairs == base.pairs and "--cap-add" not in a and a["--memory"] == "384m" and "--privileged" not in a


def test_pyroscope_service_shape():
    s = services(PROFILING={"enabled": True, "hosts": []})["pyroscope"]
    assert s["deploy"]["replicas"] == 1 and any("-compactor.blocks-retention-period=14d" in c for c in s["command"])
    assert s["deploy"]["resources"]["limits"]["memory"] == "1G"
    r = s["deploy"]["resources"]
    assert (r["reservations"]["memory"], str(r["limits"]["cpus"])) == ("256M", "0.5")
    assert f"node.hostname == {DATA}" in s["deploy"]["placement"]["constraints"]
    assert "/srv/local/skmon-prod/pyroscope:/data" in s["volumes"]
    assert re.search(r":\d+\.\d+\.\d+@sha256:", s["image"]) and "2.4.2" in s["image"]


def test_retention_knob_and_port_knob():
    s = services(PROFILING={"enabled": True}, PROFILES_RETENTION="7d", PYROSCOPE_PORT=4141)["pyroscope"]
    assert "-compactor.blocks-retention-period=7d" in s["command"] and "-retention-period=7d" in s["command"]
    assert "-server.http-listen-port=4141" in s["command"]
    assert {"target": 4141, "published": 4141, "protocol": "tcp", "mode": "host"} in s["ports"]


def test_published_port_default_host_mode():
    assert {"target": 4040, "published": 4040, "protocol": "tcp", "mode": "host"} in services()["pyroscope"]["ports"]


def test_parked_pyroscope_is_scaled_to_zero():
    assert services(PROFILING={"enabled": True}, replicas=0)["pyroscope"]["deploy"]["replicas"] == 0


def test_router_behind_forward_auth_and_matches_the_tile_host():
    s = services(PROFILING={"enabled": True})
    labels = s["pyroscope"]["deploy"]["labels"]
    mw = [l for l in labels if ".middlewares=" in l]
    assert mw and all("authentik@file" in l for l in mw)
    assert any("Host(`pyroscope.c1.example.test`)" in l for l in labels) and any("server.port=4040" in l for l in labels)
    assert not any(l.startswith("traefik.docker.") for l in labels)
    assert router_hosts(s) == {f'{i["host_prefix"]}.c1.example.test' for i in tile_items(PROFILING={"enabled": True})}
    dev = yaml.safe_load(render(CFG / "skmon.yml.j2", env="dev", PROFILING={"enabled": True},
                                extra_ctx=__import__("skmon_support").ctx_with_configs(env="dev", PROFILING={"enabled": True})))
    assert any("Host(`pyroscope-dev.c1.example.test`)" in l for l in dev["services"]["pyroscope"]["deploy"]["labels"])


def test_off_has_no_pyroscope_router():
    assert not [l for l in services()["pyroscope"]["deploy"].get("labels", []) if "pyroscope" in l]


def test_light_mode_never_profiles():
    assert "pyroscope" not in cfg(agent_mode="systemd", agent_light=True, agent_profiling=True)
    assert "pyroscope" not in cfg(agent_mode="container", agent_light=True, agent_profiling=True)


def test_agent_profiling_is_false_for_light_or_systemd_hosts():
    assert agent_ctx("h1", PROFILING=ON, agent_mode="systemd")["agent_profiling"] is False
    assert agent_ctx("h1", PROFILING=ON, agent_light=True)["agent_profiling"] is False
    assert agent_ctx("h1", PROFILING=ON)["agent_profiling"] is True
    assert agent_ctx("h1", PROFILING="true")["agent_profiling"] is False  # a non-mapping PROFILING is off


def test_profiling_fact_is_mapping_safe():
    for p, want in (({"enabled": True}, True), ({"enabled": "false"}, False), ("yes", False), (None, False), ({}, False)):
        assert guard_facts(PROFILING=p)["skmon_profiling_enabled"] is want, p
    assert "skmon_profiling_enabled" in FACTS_FILE.read_text()


def test_deploy_script_uses_the_fact_and_waits_only_when_enabled():
    text = DEPLOY.read_text()  # plain-Jinja framework tests render it: the fact, never `PROFILING | bool`
    assert "skmon_profiling_enabled" in text and "PROFILING" not in text.replace("skmon_profiling_enabled", "")
    assert "${STACK_NAME}_pyroscope" in render_deploy(PROFILING={"enabled": True})
    assert "pyroscope" not in render_deploy(PROFILING={"enabled": False})
    assert "pyroscope" not in render_deploy(PROFILING={"enabled": True}, replicas=0)


def test_scrape_job_only_when_enabled_with_estate_label():
    def jobs(**kw):
        return {j["job_name"]: j for j in yaml.safe_load(render(CFG / "vm-scrape.yml.j2", extra_ctx=_scrape_ctx(), **kw))["scrape_configs"]}
    assert "pyroscope" not in jobs()
    j = jobs(PROFILING={"enabled": True})["pyroscope"]
    assert j["static_configs"][0]["targets"] == ["skmon-prod_pyroscope:4040"] and j["static_configs"][0]["labels"]["estate"] == "example"


def test_grafana_datasource_agrees_with_the_service():
    ds = yaml.safe_load(render(CFG / "grafana/datasources.yml.j2", PROFILING={"enabled": True}))["datasources"]
    d = next(x for x in ds if x["uid"] == "skmon-pyroscope")
    assert d["url"] == "http://skmon-prod_pyroscope:4040"


def test_firewall_allows_only_profiling_agents_on_the_pyroscope_port():
    r = rules(PROFILING={"enabled": True, "hosts": [DATA]})
    assert "--ctorigdstport 4040 --ctdir ORIGINAL -s 192.0.2.10 -j RETURN" in r
    assert "--ctorigdstport 4040 --ctdir ORIGINAL -s 192.0.2.11" not in r
    assert "--ctorigdstport 4040 --ctdir ORIGINAL -j DROP" in r
    assert "-s 192.0.2.11 -j RETURN" in r  # the other agent stays allowed on the agent and Loki ports
    none = rules(PROFILING={"enabled": True, "hosts": []})
    assert "--ctorigdstport 4040 --ctdir ORIGINAL -j DROP" in none and "--ctorigdstport 4040 --ctdir ORIGINAL -s" not in none


def test_data_dir_owned_by_the_image_user():
    assert {"sub": "pyroscope", "owner": "10001", "mode": "0750"} in data_dir_items()


def test_guard_rejects_a_systemd_mode_profiling_host():
    t = [{"node": "h1", "addr": "192.0.2.50", "role": "swarm", "agent_mode": "systemd", "light": False},
         {"node": "h2", "addr": "192.0.2.51", "role": "swarm", "agent_mode": "container", "light": False}]
    facts = guard_facts(PROFILING={"enabled": True, "hosts": ["h1", "h2"]}, skmon_agent_targets=t)
    msgs = [m for m in eval_asserts(AGENT_GUARDS, facts) if "rofiling" in m]
    assert len(msgs) == 1 and "h1" in msgs[0] and "h2" not in msgs[0] and "container" in msgs[0]
    for kw in ({"replicas": 0}, {"PROFILING": {"enabled": False, "hosts": ["h1"]}}, {"PROFILING": {"enabled": True, "hosts": ["h2"]}}):
        p = {"PROFILING": {"enabled": True, "hosts": ["h1"]}, **kw}
        assert not [m for m in eval_asserts(AGENT_GUARDS, guard_facts(skmon_agent_targets=t, **p)) if "rofiling" in m], kw
