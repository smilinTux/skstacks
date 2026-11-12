"""skmon v2 VictoriaMetrics scrape config: one Alloy agent target per inventory host."""
import yaml

from skmon_support import CFG, PLAYS, agent_targets, guard_facts, play_tasks, render

SCRAPE = CFG / "vm-scrape.yml.j2"
A, B = "node-1.render.example.test", "node-2.render.example.test"
CTX = {"skmon_agent_targets": [
    {"node": A, "addr": "192.0.2.10", "role": "swarm", "light": False},
    {"node": B, "addr": "192.0.2.11", "role": "storage", "light": False},
]}


def jobs(skmon_probes=None, **kw):
    ctx = {**CTX, **({"skmon_probes": skmon_probes} if skmon_probes is not None else {})}
    return {j["job_name"]: j for j in yaml.safe_load(render(SCRAPE, extra_ctx=ctx, **kw))["scrape_configs"]}


def test_unix_and_cadvisor_are_separate_jobs_with_spec_intervals():
    j = jobs()
    assert j["alloy-unix"]["scrape_interval"] == "15s" and j["alloy-cadvisor"]["scrape_interval"] == "30s"
    assert j["alloy-unix"]["metrics_path"].endswith("prometheus.exporter.unix.host/metrics")
    assert j["alloy-cadvisor"]["metrics_path"].endswith("prometheus.exporter.cadvisor.docker/metrics")


def test_interval_knobs():
    j = jobs(SCRAPE_INTERVAL_FAST="10s", SCRAPE_INTERVAL="45s")
    assert j["alloy-unix"]["scrape_interval"] == "10s" and j["alloy-self"]["scrape_interval"] == "45s"


def test_one_target_per_agent_host_with_node_label():
    tg = jobs()["alloy-unix"]["static_configs"]
    assert {t["labels"]["node"] for t in tg} == {h["node"] for h in CTX["skmon_agent_targets"]}
    assert all(t["targets"] == [f'{h["addr"]}:12345'] for t, h in zip(tg, CTX["skmon_agent_targets"]))
    assert [t["labels"]["role"] for t in tg] == ["swarm", "storage"]


def test_estate_label_on_every_target():
    assert all(t["labels"]["estate"] == "example" for j in jobs().values() for t in j.get("static_configs", []))


def test_agent_port_knob():
    assert jobs(AGENT_PORT=9999)["alloy-unix"]["static_configs"][0]["targets"][0].endswith(":9999")


def test_blackbox_is_the_data_node_agent_only():
    bb = jobs(skmon_probes=[{"name": "app", "url": "https://app.render.example.test"}])["alloy-blackbox"]
    assert bb["scrape_interval"] == "15s" and bb["metrics_path"].endswith("prometheus.exporter.blackbox.probes/metrics")
    assert [(t["targets"], t["labels"]["node"]) for t in bb["static_configs"]] == [(["https://app.render.example.test"], A)]
    assert bb["relabel_configs"][-1]["replacement"] == "192.0.2.10:12345"


def test_no_blackbox_job_when_data_node_is_not_an_agent_host():
    assert "alloy-blackbox" not in jobs(DATA_NODE="other.render.example.test", skmon_probes=[{"name": "a", "url": "https://a.example.test"}])


def test_extra_scrape_configs_pass_through():
    job = {"job_name": "edge", "dns_sd_configs": [{"names": ["tasks.edge"], "type": "A", "port": 8082}],
           "relabel_configs": [{"source_labels": ["__meta_dns_name"], "target_label": "svc"}]}
    assert jobs(EXTRA_SCRAPE_CONFIGS=[job])["edge"] == job


def test_self_monitoring_jobs():
    j = jobs()
    assert {"victoriametrics", "loki", "alertmanager", "alloy-self", "traefik"} <= set(j)
    assert j["alloy-self"]["metrics_path"] == "/metrics"
    assert j["loki"]["static_configs"][0]["targets"] == ["skmon-prod_loki:3100"]
    assert j["alertmanager"]["static_configs"][0]["targets"] == ["skmon-prod_alertmanager:9093"]
    assert j["victoriametrics"]["static_configs"][0]["targets"] == ["localhost:8428"]


def test_traefik_uses_swarm_dns_discovery_and_estate_label():
    doc = yaml.safe_load(render(SCRAPE, env="dev", extra_ctx=CTX))
    t = next(j for j in doc["scrape_configs"] if j["job_name"] == "traefik")
    assert t["dns_sd_configs"][0]["names"] == ["tasks.skfence-dev_traefik"] and t["dns_sd_configs"][0]["port"] == 8082
    assert {"target_label": "estate", "replacement": "example"} in t["relabel_configs"]
    assert jobs(TRAEFIK_SCRAPE_NAME="tasks.x_traefik")["traefik"]["dns_sd_configs"][0]["names"] == ["tasks.x_traefik"]


def test_no_remote_write_or_federation():
    txt = render(SCRAPE, extra_ctx=CTX)
    assert "remote_write" not in txt and "/federate" not in txt


def test_only_keys_victoriametrics_accepts():
    doc = yaml.safe_load(render(SCRAPE, extra_ctx=CTX))
    assert set(doc) == {"global", "scrape_configs"} and set(doc["global"]) == {"scrape_interval"}
    ok = {"job_name", "scrape_interval", "metrics_path", "static_configs", "dns_sd_configs", "relabel_configs",
          "metric_relabel_configs"}
    assert all(set(j) <= ok for j in doc["scrape_configs"])


# --- the inventory-derived facts the plays build the targets from ---

def test_agent_host_list_prefers_the_group_when_defined():
    g = {"skmon_agent_hosts": ["a"], "swarm_managers": ["m"]}
    assert guard_facts(groups=g)["skmon_agent_host_list"] == ["a"]


def test_agent_host_list_fallback_is_unique_and_ordered():
    g = {"swarm_managers": ["m1", "m2"], "swarm_workers": ["w1", "m1"], "skmon_extra_hosts": ["x1"]}
    assert guard_facts(groups=g)["skmon_agent_host_list"] == ["m1", "m2", "w1", "x1"]
    assert guard_facts(groups={})["skmon_agent_host_list"] == []


def test_agent_targets_derivation_rules():
    hv = {"m1": {"ansible_host": "192.0.2.1"}, "m2": {"ansible_host": "192.0.2.2", "skmon_role": "storage"},
          "bare": {}}
    got = {t["node"]: t for t in agent_targets(hv, AGENT_LISTEN_ADDR={"m1": "198.51.100.1"})}
    assert got["m1"]["addr"] == "198.51.100.1" and got["m2"]["addr"] == "192.0.2.2"
    assert got["bare"]["addr"] == "bare" and got["m2"]["role"] == "storage" and got["m1"]["role"] == "swarm"
    assert not any(t["light"] for t in got.values())


def test_plays_derive_targets_identically():
    # the derivation lives in tasks/agent_targets.yml; every play includes that one file
    names = "Derive the skmon agent scrape targets"
    tasks = [next(t for t in play_tasks(p) if t.get("name") == names) for p in PLAYS]
    assert tasks[0] == tasks[1] == tasks[2]
    assert tasks[0]["include_tasks"] == "{{ playbook_dir }}/tasks/agent_targets.yml"
