"""skmon v2 alert rules (spec 8): the vmalert service, the curated rule table, and Loki's ruler.

The behaviour of each rule (fires on a failing series, stays quiet on a healthy one) is
tested by vmalert-tool over src/config/skmon/rules/tests/*_test.yml in CI (F3); this file
pins the table, the labels and the wiring.
"""
import re

import pytest
import yaml

from skmon_support import (CFG, DATA_MOUNTPOINT, PLAYS, RULE_CLASSES, all_rules, compose, ctx_with_configs,
                           play_tasks, render, render_rules, rule, rules_named, services, task_named)

DATA_NODE = "node-1.render.example.test"

TABLE = {"NodeDown": ("30s", "critical", "node"), "ZfsPoolNotOnline": ("15s", "critical", "storage"),
         "NfsMountUnresponsive": ("15s", "critical", "storage"), "NfsMajorTimeouts": ("0s", "critical", "storage"),
         "DStatePileUp": ("30s", "critical", "storage"), "IoPressureHigh": ("30s", "warning", "storage"),
         "StorageHostStalled": ("30s", "critical", "storage"), "AuthentikDown": ("1m", "critical", "http"),
         "MemoryPressure": ("2m", "warning", "capacity"), "OomKill": ("0s", "warning", "container"),
         "CadvisorScrapeFailing": ("2m", "warning", "container"),
         "AlertDeliveryFailing": ("5m", "critical", "meta"), "MonitorDiskFilling": ("10m", "warning", "meta")}

# Every alert this task ships (the swarm class comes with skswarm-exporter).
ALERTS = set(TABLE) | {"HttpEndpointDown", "CertExpiringSoon", "DiskFilling", "Watchdog"}


def dur(text: str) -> int:
    """Seconds of a Prometheus duration like 15s, 1m, 2h."""
    return sum(int(n) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u] for n, u in re.findall(r"(\d+)([smhd])", text))


def group_wait(cls: str, severity: str) -> int:
    """group_wait of the Alertmanager route that matches class and severity (Task 11 adds it)."""
    doc = yaml.safe_load(render(CFG / "alertmanager-config.yml.j2"))

    def walk(route):
        for r in route.get("routes") or []:
            yield r
            yield from walk(r)

    for r in walk(doc["route"]):
        text = " ".join(map(str, r.get("matchers") or [])) + str(r.get("match") or "")
        if cls in text and severity in text and "group_wait" in r:
            return dur(r["group_wait"])
    raise LookupError(f"no route for class={cls} severity={severity}")


def test_rule_table_matches_spec_8():
    for name, (fr, sev, cls) in TABLE.items():
        r = rule(name)
        assert (r.get("for", "0s"), r["labels"]["severity"], r["labels"]["class"]) == (fr, sev, cls), name


def test_every_shipped_alert_exists_once_per_severity():
    names = [r["alert"] for r in all_rules()]
    assert set(names) == ALERTS
    assert {n for n in names if names.count(n) > 1} == {"CertExpiringSoon", "DiskFilling"}


def test_every_rule_has_runbook_estate_and_class():
    for r in all_rules():
        if r["alert"] == "Watchdog":
            continue
        assert r["annotations"]["runbook"].endswith(".") and r["labels"]["estate"] == "example"
        assert r["labels"]["class"] in {"node", "storage", "swarm", "http", "capacity", "container", "cert", "meta"}
        assert r["annotations"]["summary"] and "\n" not in r["annotations"]["summary"].strip(), r["alert"]


def test_estate_follows_the_vault():
    assert {r["labels"]["estate"] for r in all_rules(ESTATE="other")} == {"other"}


def test_class_files_hold_only_their_class():
    for c in RULE_CLASSES:
        groups = yaml.safe_load(render_rules(c))["groups"]
        assert [g["name"] for g in groups] == [f"skmon-{c}"]
        assert {r["labels"]["class"] for g in groups for r in g["rules"]} == {c}, c


def test_node_down_uses_unix_job_only():
    assert 'job="alloy-unix"' in rule("NodeDown")["expr"] and "cadvisor" not in rule("NodeDown")["expr"]


def test_cadvisor_scrape_failing_is_the_cadvisor_job():
    assert rule("CadvisorScrapeFailing")["expr"].strip() == 'up{job="alloy-cadvisor"} == 0'


def test_d_state_threshold_knob():
    assert "> 25" in rule("DStatePileUp", D_STATE_THRESHOLD="25")["expr"]
    assert "> 10" in rule("DStatePileUp")["expr"]
    assert "> 25" in rule("StorageHostStalled", D_STATE_THRESHOLD="25")["expr"]


def test_storage_host_stalled_is_storage_hosts_only():
    expr = rule("StorageHostStalled")["expr"]
    assert 'role="storage"' in expr and 'state="D"' in expr


def test_spec_8_storage_signals():
    assert 'node_zfs_zpool_state{state!="online"} == 1' in rule("ZfsPoolNotOnline")["expr"]
    assert 'node_filesystem_device_error{fstype=~"nfs4?"} == 1' in rule("NfsMountUnresponsive")["expr"]
    assert "increase(node_mountstats_nfs_operations_major_timeouts_total[1m]) > 0" in rule("NfsMajorTimeouts")["expr"]
    assert "rate(node_pressure_io_stalled_seconds_total[1m]) > 0.5" in rule("IoPressureHigh")["expr"]


def test_no_rule_keys_on_collector_success():
    # R30: node_scrape_collector_success{collector="zfs"} is 0 on every host without ZFS.
    assert not any("node_scrape_collector_success" in r["expr"] for r in all_rules())


def test_authentik_and_http_endpoint_never_double_report():
    assert 'probe_success{probe="authentik"} == 0' in rule("AuthentikDown")["expr"]
    http = rule("HttpEndpointDown")
    assert 'probe_success{probe!="authentik"} == 0' in http["expr"] and http["for"] == "1m"
    assert http["labels"]["severity"] == "{{ $labels.severity }}" and http["labels"]["class"] == "http"


def test_cert_and_disk_have_both_severities():
    assert {r["labels"]["severity"] for r in rules_named("CertExpiringSoon")} == {"warning", "critical"}
    assert {r["labels"]["severity"] for r in rules_named("DiskFilling")} == {"warning", "critical"}


def test_cert_thresholds_and_disk_windows():
    cert = {r["labels"]["severity"]: r for r in rules_named("CertExpiringSoon")}
    assert all("probe_ssl_earliest_cert_expiry" in r["expr"] and r["for"] == "10m" for r in cert.values())
    assert "14 * 86400" in cert["warning"]["expr"] and "3 * 86400" in cert["critical"]["expr"]
    disk = {r["labels"]["severity"]: r for r in rules_named("DiskFilling")}
    assert all(r["for"] == "5m" and "node_filesystem_files_free" in r["expr"] for r in disk.values())
    assert "> 90" in disk["warning"]["expr"] and "predict_linear" in disk["warning"]["expr"]
    assert "> 95" in disk["critical"]["expr"]


def test_watchdog_always_fires_and_reaches_no_person():
    w = rule("Watchdog")
    assert w["expr"].strip() == "vector(1)" and "for" not in w
    assert w["labels"]["severity"] == "none" and w["labels"]["class"] == "meta"


def test_monitor_disk_filling_watches_data_path_on_data_node():
    expr = rule("MonitorDiskFilling")["expr"]
    assert f'node="{DATA_NODE}"' in expr and f'mountpoint="{DATA_MOUNTPOINT}"' in expr and "> 85" in expr
    assert 'mountpoint="/srv"' in render_rules("meta", mountpoint="/srv")


def test_vmalert_eval_interval_and_limits():
    s = services()["vmalert"]
    assert "-evaluationInterval=15s" in s["command"]
    assert s["deploy"]["resources"]["limits"]["memory"] == "128M"
    assert s["deploy"]["resources"]["reservations"]["memory"] == "32M"
    assert str(s["deploy"]["resources"]["limits"]["cpus"]) == "0.25"


def test_vmalert_flags():
    cmd = services()["vmalert"]["command"]
    for flag in ("-datasource.url=http://victoriametrics:8428", "-notifier.url=http://alertmanager:9093",
                 "-rule=/etc/vmalert/rules/*.yml", "-external.label=estate=example"):
        assert flag in cmd, flag


def test_vmalert_mounts_one_hash_named_config_per_class():
    doc = compose()
    mounted = {c["source"]: c["target"] for c in doc["services"]["vmalert"]["configs"]}
    assert mounted == {f"rules-{c}": f"/etc/vmalert/rules/{c}.yml" for c in RULE_CLASSES}
    for c in RULE_CLASSES:
        assert re.fullmatch(rf"skmon_prod_rules-{c}_[0-9a-f]{{12}}", doc["configs"][f"rules-{c}"]["name"])
    before = doc["configs"]["rules-storage"]["name"]
    assert compose(D_STATE_THRESHOLD="25")["configs"]["rules-storage"]["name"] != before


def test_vmalert_is_internal_on_data_node_and_parks():
    s = services()["vmalert"]
    assert s["networks"] == ["skmon-prod"]
    assert "labels" not in s["deploy"] and "volumes" not in s and "ports" not in s
    assert f"node.hostname == {DATA_NODE}" in s["deploy"]["placement"]["constraints"]
    assert services(replicas="0")["vmalert"]["deploy"]["replicas"] == 0


def test_vmalert_is_scraped():
    scrape = ctx_with_configs()["skmon_configs"]["vm-scrape"]
    jobs = {j["job_name"]: j for j in yaml.safe_load(scrape)["scrape_configs"]}
    assert jobs["vmalert"]["static_configs"][0]["targets"] == ["vmalert:8880"]
    assert jobs["vmalert"]["scrape_interval"] == "30s"


@pytest.mark.xfail(strict=True, reason="Task 11")
def test_storage_timing_budget_within_60s():
    # spec 8.1: scrape + eval + for + group_wait + delivery
    assert 15 + 15 + dur(rule("NfsMountUnresponsive")["for"]) + group_wait("storage", "critical") + 2 <= 60


def loki_rules(**skmon) -> list[dict]:
    return [r for g in yaml.safe_load(render(CFG / "loki-rules/storage.yml.j2", **skmon))["groups"] for r in g["rules"]]


def test_loki_log_rules():
    exprs = [r["expr"] for r in loki_rules()]
    assert any("not responding" in e for e in exprs) and any("suspended" in e for e in exprs)


def test_loki_rules_labels():
    rs = {r["alert"]: r for r in loki_rules()}
    assert set(rs) == {"NfsServerNotResponding", "ZfsPoolSuspendedLog"}
    for r in rs.values():
        assert r["expr"].startswith('count_over_time({job="journal"} |~ ') and r["expr"].endswith("[1m]) > 0")
        assert r["for"] == "0s"
        assert r["labels"] == {"severity": "critical", "class": "storage", "estate": "example"}
        assert r["annotations"]["runbook"].endswith(".") and r["annotations"]["summary"]


def test_loki_ruler_sends_to_alertmanager_from_a_swarm_config():
    doc = yaml.safe_load(render(CFG / "loki-config.yml.j2"))
    ruler = doc["ruler"]
    assert ruler["alertmanager_url"] == "http://alertmanager:9093" and ruler["enable_api"] is True
    assert ruler["storage"] == {"type": "local", "local": {"directory": "/etc/loki/rules"}}
    assert ruler["rule_path"].startswith("/loki/")
    loki = services()["loki"]
    # auth_enabled false: the single tenant is "fake", so local storage reads <directory>/fake/.
    assert {"source": "loki-rules", "target": "/etc/loki/rules/fake/storage.yml"} in loki["configs"]


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_plays_read_the_data_path_mountpoint_on_data_node(play):
    probe = task_named(play, "Find the filesystem that holds DATA_PATH on the data node")
    assert probe["command"]["argv"] == ["findmnt", "-n", "-o", "TARGET", "-T", "{{ skmon_data_path }}"]
    assert probe["delegate_to"] == "{{ skmon_data_node }}" and probe["changed_when"] is False
    fact = task_named(play, "Set the DATA_PATH mountpoint")["set_fact"]["skmon_data_mountpoint"]
    assert "'/'" in fact
    names = [t.get("name") for t in play_tasks(play)]
    assert names.index("Set the DATA_PATH mountpoint") < names.index("Render the {{ app }} Swarm config contents")


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_plays_render_every_rule_config(play):
    configs = task_named(play, "Render the {{ app }} Swarm config contents")["set_fact"]["skmon_configs"]
    assert {f"rules-{c}" for c in RULE_CLASSES} | {"loki-rules"} <= set(configs)
    for c in RULE_CLASSES:
        assert f"/rules/{c}.yml.j2" in configs[f"rules-{c}"]
