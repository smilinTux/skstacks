"""Task 16: skswarm-exporter behind a read-only socket proxy, the Swarm rules, and the optional
Proxmox exporter."""
import json
import re

import pytest
import yaml

from skmon_support import (AGENT_GUARDS, CFG, GUARDS, PLAYS, SECRET_TEMPLATES, all_rules, compose, ctx_with_configs,
                           eval_asserts, guard_facts, render, render_deploy, rule, rules_named, services, task_named)

A, B, C = "node-1.render.example.test", "node-2.render.example.test", "node-3.render.example.test"
SCRAPE = CFG / "vm-scrape.yml.j2"
CTX = {"skmon_agent_targets": [{"node": A, "addr": "192.0.2.10", "role": "swarm", "light": False}]}


def jobs(**kw):
    return {j["job_name"]: j for j in yaml.safe_load(render(SCRAPE, extra_ctx=CTX, **kw))["scrape_configs"]}


def env_of(service):
    return service["environment"]


# --- socket proxy --------------------------------------------------------------------

def test_socket_proxy_is_read_only_and_get_only():
    p = services()["socket-proxy"]
    assert {"SERVICES=1", "TASKS=1", "NODES=1", "INFO=1", "POST=0"} == set(env_of(p))
    assert p["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
    assert p["deploy"]["placement"]["constraints"] == ["node.role == manager"]
    assert "ports" not in p and not any(l.startswith("traefik.") and l != "traefik.enable=false" for l in p["deploy"].get("labels", []))
    assert p["image"].startswith("tecnativa/docker-socket-proxy:v0.5.0@sha256:")


def test_proxy_joins_only_the_internal_network_and_the_exporter_also_joins_skmon():
    s, c = services(), compose()
    assert s["socket-proxy"]["networks"] == ["docker-api"]
    assert set(s["skswarm-exporter"]["networks"]) == {"docker-api", "skmon-prod"}
    assert c["networks"]["docker-api"] == {"driver": "overlay", "internal": True}
    assert c["networks"]["skmon-prod"] == {"external": True}


# --- exporter ------------------------------------------------------------------------

def test_exporter_service_shape():
    e = services()["skswarm-exporter"]
    assert e["image"].startswith("python:3.13.") and "-slim-" in e["image"] and "@sha256:" in e["image"]
    assert e["command"] == ["python3", "/opt/skmon/skswarm_exporter.py"]
    assert {"source": "skswarm_exporter.py", "target": "/opt/skmon/skswarm_exporter.py"} in e["configs"]
    assert e["deploy"]["placement"]["constraints"] == ["node.role == manager"]
    assert "ports" not in e and "secrets" not in e and "volumes" not in e
    assert dict(kv.split("=", 1) for kv in env_of(e)) == {
        "SKSWARM_DOCKER_HOST": "http://skmon-prod_socket-proxy:2375", "SKSWARM_POLL_S": "15",
        "SKSWARM_PORT": "9323", "SKSWARM_TIMEOUT_S": "10"}


def test_exporter_urls_follow_the_environment_suffix():
    dev = yaml.safe_load(render(CFG / "skmon.yml.j2", env="dev", extra_ctx=ctx_with_configs(env="dev")))
    assert "SKSWARM_DOCKER_HOST=http://skmon-dev_socket-proxy:2375" in dev["services"]["skswarm-exporter"]["environment"]


def test_exporter_script_is_a_hash_named_config():
    c = compose()["configs"]["skswarm_exporter.py"]
    assert re.fullmatch(r"skmon_prod_skswarm_exporter\.py_[0-9a-f]{12}", c["name"])
    assert c["file"] == "/var/data/config/skmon-prod/configs/skswarm_exporter.py"


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_plays_ship_the_exporter_script_and_swarm_rules(play):
    configs = task_named(play, "Render the {{ app }} Swarm config contents")["set_fact"]["skmon_configs"]
    assert "swarm_exporter/skswarm_exporter.py" in configs["skswarm_exporter.py"]
    assert "rules/swarm.yml.j2" in configs["rules-swarm"]


def test_vmalert_loads_the_swarm_rules():
    assert {"source": "rules-swarm", "target": "/etc/vmalert/rules/swarm.yml"} in services()["vmalert"]["configs"]


@pytest.mark.parametrize("r", [0, "0"])
def test_all_three_new_services_are_parked(r):
    s = services(replicas=r, PVE={"enabled": True})
    assert all(s[n]["deploy"]["replicas"] == 0 for n in ("socket-proxy", "skswarm-exporter", "pve-exporter"))


def test_unparked_proxy_and_exporter_run_one_replica():
    s = services()
    assert s["socket-proxy"]["deploy"]["replicas"] == 1 and s["skswarm-exporter"]["deploy"]["replicas"] == 1


# --- scrape --------------------------------------------------------------------------

def test_exporter_scraped_every_15s_with_estate_label():
    j = jobs()["skswarm"]
    assert j["scrape_interval"] == "15s"
    assert j["static_configs"] == [{"targets": ["skmon-prod_skswarm-exporter:9323"], "labels": {"estate": "example"}}]
    dev = {x["job_name"]: x for x in yaml.safe_load(render(SCRAPE, env="dev", extra_ctx=CTX))["scrape_configs"]}
    assert dev["skswarm"]["static_configs"][0]["targets"] == ["skmon-dev_skswarm-exporter:9323"]


# --- rules ---------------------------------------------------------------------------

def test_swarm_rule_table():
    want = {"TasksStuckPreparing": ("0s", "critical", "swarm"), "SwarmApiUnresponsive": ("30s", "critical", "swarm"),
            "ContainerCrashloop": ("0s", "warning", "container"), "AgentMissing": ("5m", "warning", "meta"),
            "SwarmNodeNotReady": ("30s", "critical", "node")}
    for n, t in want.items():
        r = rule(n)
        assert (r.get("for", "0s"), r["labels"]["severity"], r["labels"]["class"]) == t, n


def test_rule_expressions_follow_the_spec():
    assert rule("AgentMissing")["expr"] == 'skswarm_node_info unless on(node) up{job="alloy-unix"}'
    assert rule("SwarmApiUnresponsive")["expr"] == "skswarm_up == 0 or skswarm_api_duration_seconds > 5"
    assert rule("TasksStuckPreparing")["expr"] == 'skswarm_task_state_age_seconds{state=~"preparing|starting|pending"} > 120'
    assert "increase_prometheus(skswarm_service_task_failures_total[10m]) >= 3" == rule("ContainerCrashloop")["expr"]


def test_service_below_replicas_two_severities_2m():
    rs = rules_named("ServiceBelowReplicas")
    assert {r["labels"]["severity"] for r in rs} == {"warning", "critical"} and {r["for"] for r in rs} == {"2m"}
    assert {r["labels"]["class"] for r in rs} == {"swarm"} and len(rs) == 2


def test_service_below_replicas_warning_cannot_double_fire_with_the_critical():
    warn, crit = sorted(rules_named("ServiceBelowReplicas"), key=lambda r: r["labels"]["severity"], reverse=True)
    assert "running > 0" in warn["expr"] and "running == 0" in crit["expr"] and "desired > 0" in crit["expr"]


def test_every_inhibition_target_alert_exists_as_a_rule():
    am = yaml.safe_load(render(CFG / "alertmanager-config.yml.j2"))
    names = {r["alert"] for r in all_rules()}
    target = next(m for m in am["inhibit_rules"][1]["target_matchers"] if m.startswith("alertname=~"))
    assert set(target.split('"')[1].split("|")) <= names
    assert {"NodeDown", "SwarmNodeNotReady"} <= names


# --- guards (R53, R54) -----------------------------------------------------------------

def agent_msgs(groups, swarm_nodes):
    return eval_asserts(AGENT_GUARDS, guard_facts(groups=groups, swarm_nodes=swarm_nodes))


def test_agent_host_must_be_a_swarm_hostname_passes():
    g = {"swarm_managers": [A], "swarm_workers": [B]}
    assert agent_msgs(g, [A, B]) == []


def test_agent_host_with_a_different_inventory_name_fails_and_says_why():
    g = {"swarm_managers": [A], "swarm_workers": ["inv-name-2"]}
    got = agent_msgs(g, [A, B])
    assert len(got) == 1 and "inv-name-2" in got[0] and A not in got[0].split("is not a Swarm node hostname")[0]
    assert "`node` label" in got[0] and "AgentMissing" in got[0] and "Swarm hostname" in got[0]


def test_extra_hosts_are_exempt_and_non_swarm_hosts_are_not_checked():
    g = {"swarm_managers": [A], "swarm_workers": [], "skmon_extra_hosts": ["nas-1"]}
    assert agent_msgs(g, [A]) == []
    g = {"swarm_managers": [A], "skmon_extra_hosts": [B]}
    assert agent_msgs(g, [A]) == []
    assert agent_msgs({"skmon_agent_hosts": ["somewhere"], "swarm_managers": [A]}, [A]) == []


def pve_msgs(**kw):
    return [m for m in eval_asserts(GUARDS, guard_facts(**kw)) if "PVE" in m]


FULL_PVE = {"PVE": {"enabled": True, "targets": ["pve1.example.test"]}, "PVE_TOKEN_ID": "mon@pve!skmon",
            "PVE_TOKEN_SECRET": "fake-pve-secret"}


def test_pve_off_needs_nothing():
    assert pve_msgs() == [] and pve_msgs(PVE={"enabled": False}) == [] and pve_msgs(PVE={"enabled": "false"}) == []


def test_pve_on_with_everything_passes():
    assert pve_msgs(**FULL_PVE) == []
    assert pve_msgs(**{**FULL_PVE, "PVE": {"enabled": "true", "targets": ["pve1.example.test"]}}) == []


@pytest.mark.parametrize("drop", [{"PVE_TOKEN_ID": ""}, {"PVE_TOKEN_SECRET": ""},
                                  {"PVE": {"enabled": True, "targets": []}}, {"PVE": {"enabled": True}}])
def test_pve_on_requires_token_and_targets(drop):
    got = pve_msgs(**{**FULL_PVE, **drop})
    assert got and "PVE_TOKEN_ID" in got[0] and "fake-pve-secret" not in got[0]


# --- the pve exporter --------------------------------------------------------------------

@pytest.mark.parametrize("v", [False, "no", "false"])
def test_pve_off_by_default_and_string_false(v):
    assert services(PVE={"enabled": v})["pve-exporter"]["deploy"]["replicas"] == 0 and "pve" not in jobs(PVE={"enabled": v})
    assert services()["pve-exporter"]["deploy"]["replicas"] == 0 and "pve" not in jobs()


@pytest.mark.parametrize("v", [True, "true", "yes"])
def test_pve_on_runs_one_replica_and_scrapes(v):
    assert services(PVE={"enabled": v, "targets": ["pve1.example.test"]})["pve-exporter"]["deploy"]["replicas"] == 1


def test_pve_job_is_multi_target_through_pve_path():
    j = jobs(PVE={"enabled": True, "targets": ["pve1.example.test", "pve2.example.test"]})["pve"]
    assert j["metrics_path"] == "/pve" and j["scrape_interval"] == "30s"
    assert j["static_configs"] == [{"targets": ["pve1.example.test", "pve2.example.test"], "labels": {"estate": "example"}}]
    rl = j["relabel_configs"]
    assert {"source_labels": ["__address__"], "target_label": "__param_target"} in rl
    assert {"target_label": "__address__", "replacement": "skmon-prod_pve-exporter:9221"} in rl


def test_pve_token_is_a_secret():
    doc = compose(PVE={"enabled": True}, PVE_TOKEN_SECRET="SECRET-MARKER", PVE_TOKEN_ID="mon@pve!SECRET-ID-MARKER")
    assert "SECRET-MARKER" not in json.dumps(doc) and "SECRET-ID-MARKER" not in json.dumps(doc)
    p = doc["services"]["pve-exporter"]
    assert "environment" not in p and "SECRET" not in json.dumps(p["command"])
    assert p["secrets"][0]["source"] == "pve_config" and p["secrets"][0]["target"] == "/etc/prometheus/pve.yml"
    assert p["secrets"][0]["mode"] == 0o400 and p["command"] == ["--config.file=/etc/prometheus/pve.yml"]
    assert re.fullmatch(r"skmon_prod_pve_config_[0-9a-f]{12}", doc["secrets"]["pve_config"]["name"])
    assert doc["secrets"]["pve_config"]["file"] == "/var/data/config/skmon-prod/secrets/pve_config"
    assert p["image"].startswith("prompve/prometheus-pve-exporter:3.10.2@sha256:")


def test_pve_secret_rolls_the_exporter_on_change_and_is_always_rendered():
    a = compose()["secrets"]["pve_config"]["name"]
    b = compose(PVE_TOKEN_SECRET="another-fake")["secrets"]["pve_config"]["name"]
    assert a != b and "pve_config" in compose(PVE={"enabled": False})["secrets"]


def test_pve_config_renders_user_token_name_and_value():
    doc = yaml.safe_load(render(CFG / SECRET_TEMPLATES["pve_config"], PVE_TOKEN_ID="mon@pve!skmon", PVE_TOKEN_SECRET='x"y: z'))
    assert doc == {"default": {"user": "mon@pve", "token_name": "skmon", "token_value": 'x"y: z', "verify_ssl": True}}
    off = yaml.safe_load(render(CFG / SECRET_TEMPLATES["pve_config"]))
    assert off == {"default": {"user": "", "token_name": "", "token_value": "", "verify_ssl": True}}
    assert yaml.safe_load(render(CFG / SECRET_TEMPLATES["pve_config"], PVE={"verify_ssl": "false"}))["default"]["verify_ssl"] is False


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_plays_stage_the_pve_config_as_a_secret(play):
    sec = task_named(play, "Render the {{ app }} Docker secret contents")
    assert sec["no_log"] is True and "pve.yml.j2" in sec["set_fact"]["skmon_secrets"]["pve_config"]


# --- deploy script -------------------------------------------------------------------

def test_deploy_script_waits_for_the_exporter_and_for_pve_only_when_enabled():
    d = render_deploy()
    assert "${STACK_NAME}_skswarm-exporter" in d and "pve-exporter" not in d
    on = render_deploy(PVE={"enabled": True})
    assert "${STACK_NAME}_pve-exporter" in on and "${STACK_NAME}_skswarm-exporter" in on
    assert "pve-exporter" not in render_deploy(PVE={"enabled": "false"})


def test_parked_deploy_script_waits_for_nothing():
    d = render_deploy(replicas=0, PVE={"enabled": True})
    assert "skswarm-exporter" not in d and "pve-exporter" not in d
