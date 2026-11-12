"""skmon v2 Alloy agent config (agent.alloy.j2) and the scrape-side relabelling that shapes its series.

Alloy syntax is not YAML, so assertions on the agent config are regexes over the rendered text.
VictoriaMetrics scrapes each exporter component's own path directly, so a relabel inside Alloy
would not touch those series: the systemd and cadvisor label filters live in vm-scrape.yml.j2 as
metric_relabel_configs, and the tests for them read that file.
"""
import re

import yaml

from skmon_support import CFG, cfg, render, run_script_args

COLLECTORS = {"cpu", "diskstats", "filesystem", "loadavg", "meminfo", "netdev", "netstat", "pressure",
              "processes", "stat", "time", "uname", "vmstat", "mountstats", "nfs", "zfs", "systemd", "hwmon", "textfile"}
CADVISOR_OFF = {"disk", "percpu", "process", "tcp", "udp", "advtcp", "sched", "hugetlb",
                "referenced_memory", "cpu_topology", "resctrl", "memory_numa"}
SWARM_LABELS = ["com.docker.stack.namespace", "com.docker.swarm.service.name",
                "com.docker.swarm.task.name", "com.docker.swarm.node.id"]
TARGETS = {"skmon_agent_targets": [{"node": "node-1.render.example.test", "addr": "192.0.2.10",
                                    "role": "swarm", "light": False}]}


def _list_arg(text: str, name: str) -> set[str]:
    m = re.search(rf"{name}\s*=\s*\[([^\]]*)\]", text)
    assert m, f"{name} not set"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def enabled_collectors(text: str) -> set[str]:
    return _list_arg(text, "set_collectors")


def disabled_cadvisor_metrics(text: str) -> set[str]:
    return _list_arg(text, "disabled_metrics")


def scrape_job(name: str) -> dict:
    doc = yaml.safe_load(render(CFG / "vm-scrape.yml.j2", extra_ctx=TARGETS))
    return next(j for j in doc["scrape_configs"] if j["job_name"] == name)


def test_unix_collectors_exactly_the_spec_list():
    assert enabled_collectors(cfg()) == COLLECTORS


def test_storage_host_adds_nfsd():
    assert enabled_collectors(cfg(agent_is_storage=True)) == COLLECTORS | {"nfsd"}


def test_vmstat_includes_oom_kill_and_systemd_failed_only():
    c = cfg()
    assert re.search(r'vmstat\s*\{[^}]*fields\s*=\s*"[^"]*oom_kill', c)
    assert re.search(r"systemd\s*\{", c)
    # failed units only: the collector cannot filter by state, so a metric relabel on the
    # alloy-unix job (where VictoriaMetrics stores the series) keeps only state="failed"
    rules = scrape_job("alloy-unix")["metric_relabel_configs"]
    assert 'node_systemd_unit_state;failed' in yaml.safe_dump(rules)
    assert {"source_labels": ["__name__", "__tmp_systemd_keep"], "regex": "node_systemd_unit_state;",
            "action": "drop"} in rules


def test_filesystem_mount_timeout_5s():
    assert re.search(r'filesystem\s*\{[^}]*mount_timeout\s*=\s*"5s"', cfg())


def test_host_paths_follow_the_install_mode():
    c = cfg()
    for arg, path in (("procfs_path", "/host/proc"), ("sysfs_path", "/host/sys"), ("rootfs_path", "/rootfs")):
        assert re.search(rf'{arg}\s*=\s*"{path}"', c), arg
    assert 'procfs_path = "/proc"' in cfg(agent_mode="systemd")


def test_cadvisor_settings():
    c = cfg()
    assert disabled_cadvisor_metrics(c) == CADVISOR_OFF
    assert 'housekeeping_interval = "15s"' in c and "docker_only = true" in c


def test_cadvisor_keeps_only_swarm_container_labels():
    c = cfg()
    assert "store_container_labels = false" in c
    assert _list_arg(c, "allowlisted_container_labels") == set(SWARM_LABELS)


def test_container_labels_relabelled_to_swarm_names():
    # on the stored cadvisor series: swarm labels become stack/service/task_slot, every other
    # container_label_* is dropped, and node is the scrape target's own label (the host's name)
    job = scrape_job("alloy-cadvisor")
    rules = job["metric_relabel_configs"]
    assert {r.get("target_label") for r in rules} >= {"stack", "service", "task_slot"}
    assert {"action": "labeldrop", "regex": "container_label_.*"} in rules
    assert job["static_configs"][0]["labels"]["node"] == "node-1.render.example.test"
    slot = next(r for r in rules if r.get("target_label") == "task_slot")
    assert re.fullmatch(slot["regex"], "app_web.3.abcdef") and not re.fullmatch(slot["regex"], "app_agent.nodeid.taskid")


def test_container_log_streams_carry_swarm_names():
    c = cfg()
    for target in ("stack", "service", "task_slot", "node"):
        assert re.search(rf'target_label\s*=\s*"{target}"', c), target
    assert 'node = "node-2.render.example.test"' in c


def test_blackbox_only_on_data_node():
    assert "prometheus.exporter.blackbox" not in cfg()
    assert "prometheus.exporter.blackbox" in cfg(agent_is_data_node=True)


def test_blackbox_targets_come_from_probes():
    probes = [{"name": "grafana", "url": "https://skmon.c1.example.test", "severity": "critical"},
              {"name": "api", "url": "https://api.c1.example.test/health", "severity": "warning", "expect": 200}]
    c = cfg(agent_is_data_node=True, probes=probes)
    assert 'name    = "grafana"' in c and 'address = "https://api.c1.example.test/health"' in c
    assert 'module  = "http_200"' in c and 'module  = "http_2xx_3xx"' in c
    assert 'severity = "warning"' in c


def test_logs_go_to_data_node_loki_with_wal():
    c = cfg()
    assert 'url = "http://192.0.2.1:3100/loki/api/v1/push"' in c  # DATA_NODE address from the test context
    assert re.search(r"wal\s*\{[^}]*enabled\s*=\s*true", c)


def test_log_sources_named_and_wired():
    c = cfg()
    assert 'loki.source.docker "containers"' in c and 'loki.source.journal "journal"' in c
    assert 'host       = "unix:///var/run/docker.sock"' in c
    assert c.count("forward_to = [loki.write.local.receiver]") == 2


def test_listen_address_is_private_addr():
    assert run_script_args()["--server.http.listen-addr"] == "192.0.2.10:12345"
