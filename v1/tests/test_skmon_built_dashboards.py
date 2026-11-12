"""Task 19: the dashboards built in this repo (estate overview, container, swarm, ZFS, logs, Proxmox)."""
import json
import re
import urllib.parse

from skmon_support import CFG

DASH = CFG / "dashboards"


def board(rel):
    return json.loads((DASH / rel).read_text())


def walk_panels(panels):
    for p in panels or []:
        yield p
        yield from walk_panels(p.get("panels"))


def panels(rel):
    return list(walk_panels(board(rel)["panels"]))


def titles(rel):
    return {p["title"] for p in panels(rel)}


def queries(rel):
    return [t["expr"] for p in panels(rel) for t in p.get("targets", []) if "expr" in t]


def all_links(rel):
    """Every link URL on the board: dashboard links, panel links and field data links, URL-decoded too."""
    d = board(rel)
    urls = [lk.get("url", "") for lk in d.get("links", [])]
    for p in panels(rel):
        urls += [lk.get("url", "") for lk in p.get("links", [])]
        urls += [lk.get("url", "") for lk in p.get("fieldConfig", {}).get("defaults", {}).get("links", [])]
    return urls + [urllib.parse.unquote(u) for u in urls]


def var(rel, name):
    return next(v for v in board(rel)["templating"]["list"] if v["name"] == name)


def test_estate_overview_panels():
    t = titles("estate-overview.json")
    assert {"Nodes", "Storage health", "Services below replicas", "Firing alerts", "Certificates expiring soonest",
            "Top containers by CPU", "Top containers by memory"} <= t


def test_estate_overview_queries_reference_spec_series():
    q = queries("estate-overview.json")
    for s in ('up{job="alloy-unix"}', "node_zfs_zpool_state", "node_processes_state", "node_pressure_io_stalled_seconds_total",
              "skswarm_service_replicas_desired", "probe_ssl_earliest_cert_expiry", "ALERTS"):
        assert any(s in x for x in q), s
    assert any('ALERTS{alertstate="firing"}' in x for x in q)


def test_container_board_covers_goal_4():
    q = queries("infrastructure/container.json")
    for s in ("container_cpu_usage_seconds_total", "container_cpu_cfs_throttled_periods_total", "container_memory_working_set_bytes",
              "container_spec_memory_limit_bytes", "container_oom_events_total", "container_network_receive_bytes_total",
              "container_network_transmit_bytes_total", "container_fs_reads_bytes_total", "container_fs_writes_bytes_total",
              "container_start_time_seconds"):
        assert any(s in x for x in q), s


def test_container_board_links_logs_and_profiles():
    links = all_links("infrastructure/container.json")
    assert any("skmon-loki" in lk and "${__from}" in lk and "${__to}" in lk for lk in links)
    assert any("pyroscope" in lk.lower() and "service_name" in lk and "${__from}" in lk for lk in links)
    # The logs link selects by the stack and service labels the Alloy agent puts on Docker log streams.
    assert any(re.search(r"stack=~?\\?\"\$\{stack", lk) and re.search(r"service=~?\\?\"\$\{service", lk) for lk in links)


def test_container_board_has_logs_and_flame_graph_panels():
    by_type = {p["type"]: p for p in panels("infrastructure/container.json")}
    assert by_type["logs"]["datasource"]["uid"] == "skmon-loki"
    assert by_type["flamegraph"]["datasource"]["uid"] == "skmon-pyroscope"


def test_container_board_variables_use_real_series_and_all_value():
    for name in ("stack", "service", "node"):
        v = var("infrastructure/container.json", name)
        assert v["query"]["query"].startswith("label_values(container_") and v["query"]["query"].rstrip(")").endswith(name)
        assert v["includeAll"] is True and v["allValue"] == ".*"


def test_swarm_board_series():
    q = queries("infrastructure/swarm-services.json")
    assert all(any(s in x for x in q) for s in ("skswarm_service_replicas_running", "skswarm_task_state_age_seconds", "skswarm_service_task_failures_total"))


def test_zfs_board_series():
    q = queries("infrastructure/zfs.json")
    assert all(any(s in x for x in q) for s in ("node_zfs_zpool_state", "node_zfs_arc_size", "node_zfs_zpool_dataset_nread", "node_zfs_zpool_dataset_nwritten",
                                                             "node_zfs_zpool_dataset_reads", "node_zfs_zpool_dataset_writes"))


def test_loki_logs_board_uses_stream_labels():
    d = board("infrastructure/loki-logs.json")
    for name in ("stack", "service", "node"):
        v = var("infrastructure/loki-logs.json", name)
        assert v["datasource"]["uid"] == "skmon-loki" and v["query"]["label"] == name
        assert v["includeAll"] is True and v["allValue"] == ".*"
    exprs = [t["expr"] for p in walk_panels(d["panels"]) for t in p.get("targets", [])]
    assert exprs and all(re.search(r'stack=~"\$stack".*service=~"\$service"', e) for e in exprs)
    assert any(p["type"] == "logs" for p in walk_panels(d["panels"]))


def test_proxmox_board_series():
    q = queries("infrastructure/proxmox.json")
    for s in ("pve_up", "pve_cpu_usage_ratio", "pve_memory_usage_bytes", "pve_memory_size_bytes", "pve_disk_usage_bytes",
              "pve_disk_size_bytes", "pve_guest_info", "pve_node_info"):
        assert any(s in x for x in q), s


def test_fixed_uids():
    for f, uid in [("estate-overview.json", "estate-overview"), ("infrastructure/container.json", "container"),
                   ("infrastructure/swarm-services.json", "swarm-services"), ("infrastructure/zfs.json", "zfs"),
                   ("infrastructure/loki-logs.json", "loki-logs"), ("infrastructure/proxmox.json", "proxmox")]:
        assert board(f)["uid"] == uid


def test_estate_overview_is_at_the_provider_root_and_is_the_home_dashboard():
    assert (DASH / "estate-overview.json").is_file() and not (DASH / "General").exists()
    env = (CFG / "skmon.env.j2").read_text()
    assert "GF_DASHBOARDS_DEFAULT_HOME_DASHBOARD_PATH=/etc/grafana/provisioning/dashboards/estate-overview.json" in env


def test_built_boards_are_current_schema_and_use_only_uid_datasources():
    for rel in ("estate-overview.json", "infrastructure/container.json", "infrastructure/swarm-services.json",
                "infrastructure/zfs.json", "infrastructure/loki-logs.json", "infrastructure/proxmox.json"):
        d = board(rel)
        assert d["schemaVersion"] >= 39 and d["id"] is None and d["title"] and d["panels"], rel
        for p in walk_panels(d["panels"]):
            if p["type"] != "row" and p["type"] != "text":
                assert isinstance(p["datasource"], dict) and p["datasource"]["uid"].startswith("skmon-"), (rel, p["title"])


def test_container_board_stack_and_service_are_single_select():
    # The :percentencode data links are only valid for a single value.
    for name in ("stack", "service"):
        v = var("infrastructure/container.json", name)
        assert v["multi"] is False and v["includeAll"] is True and v["allValue"] == ".*"


def test_proxmox_joins_use_id_and_instance():
    q = queries("infrastructure/proxmox.json")
    joins = [x for x in q if " on (" in x]
    assert joins and all("on (id, instance)" in x for x in joins)
    assert not any("on (id)" in x for x in q)


def test_zfs_pool_io_is_summed_per_node_and_pool():
    q = [x for x in queries("infrastructure/zfs.json") if "zpool_dataset_" in x]
    assert q and all(x.startswith("sum by (node, zpool) (rate(") for x in q)
    assert not any(re.search(r"node_zfs_zpool_(nread|nwritten|reads|writes)\b", x) for x in queries("infrastructure/zfs.json"))
