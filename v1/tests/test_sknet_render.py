"""sknet's swarm deploy rendered through REAL Ansible (render/render_playbooks.py)
with a given vault (and, where a test needs it, an exporter inventory group):
the flattened Akvorado stack (orchestrator, inlet, outlet, console, Kafka,
ClickHouse and the optional live proxy), every image pinned, the flow inlet's
UDP ports published in host mode, every data service pinned to DATA_NODE with
its volumes on local disk under DATA_DIR, the Traefik routers with the
ipAllowList middleware always applied (also without SSO), Akvorado's own
config (static exporter metadata from inventory hostvars or the vault
fallback, sampling rate, TTLs), the port allowlist for the flow ports, the
default memory budget, and loud failures for the inputs that would otherwise
leave the collector open, blind or unplaceable."""
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROLE = rp.ANSIBLE / "optional" / "sknet"
BASE = yaml.safe_load((rp.VARS / "sknet.example.yml").read_text())["sknet"]
SET = yaml.safe_load((rp.VARS / "sknet.set.yml").read_text())["sknet"]
DEFAULTS = yaml.safe_load((ROLE / "vars" / "defaults.yml").read_text())["sknet_defaults"] if (ROLE / "vars" / "defaults.yml").exists() else {}

AKVORADO_IMAGE = "quay.io/akvorado/akvorado:2026.10.1"
CLICKHOUSE_IMAGE = "clickhouse/clickhouse-server:26.8"
KAFKA_IMAGE = "apache/kafka:4.3.1"
DEFAULT_CIDRS = ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]

# Two exporter hosts as an instance-private inventory would define them.
EXPORTERS = {
    "gw-inv-a": {
        "sknet_export_address": "192.0.2.20",
        "sknet_interfaces": [
            {"name": "eth0", "index": 101, "class": "lan", "speed_mbps": 10000, "description": "Inventory LAN"},
            {"name": "wt0", "index": 102, "class": "mesh"},
            {"name": "ens3", "index": 103, "class": "internet"},
        ],
    },
    "site-inv-b": {
        "sknet_export_address": "192.0.2.21",
        "sknet_interfaces": [{"name": "wg-peer", "index": 101, "class": "estate-link"}],
    },
}
INV_SOURCES = ["192.0.2.20", "192.0.2.21"]


def render(tmp: Path, vault, env: str = "prod", exporters=None) -> tuple[dict, Path]:
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    doc = {} if vault is None else {"sknet": vault}
    (vars_dir / "sknet.example.yml").write_text(yaml.safe_dump(doc))
    run_dir = work / f"sknet-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved_vars, saved_inv = rp.VARS, rp.write_inventory

    def inventory_with_exporters(path, target_group):
        saved_inv(path, target_group)
        if exporters is not None:
            inv = yaml.safe_load(path.read_text())
            inv["all"]["children"]["sknet_exporter_hosts"] = {
                "hosts": {name: {"ansible_connection": "local", **hv} for name, hv in exporters.items()}}
            path.write_text(yaml.safe_dump(inv))

    rp.VARS, rp.write_inventory = vars_dir, inventory_with_exporters
    try:
        pb = work / "ansible" / "optional" / "sknet" / f"deploy_sknet-{env}.yml"
        res = rp.run_one(work, "optional", "sknet", env, pb, "base")
    finally:
        rp.VARS, rp.write_inventory = saved_vars, saved_inv
    res["log"] = (run_dir / "ansible.log").read_text() if (run_dir / "ansible.log").exists() else ""
    return res, run_dir / "out"


def ok(tmp, vault, env="prod", exporters=None):
    res, out = render(tmp, vault, env, exporters)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


def fails(tmp, vault, env="prod", exporters=None):
    res, _ = render(tmp, vault, env, exporters)
    assert res["status"] == "FAIL", "the deploy rendered although it must refuse this input"
    return res["log"]


def compose(out, env="prod"):
    return yaml.safe_load((out / f"var/data/config/sknet-{env}/sknet.yml").read_text())


def akvorado(out, env="prod"):
    return yaml.safe_load((out / f"_copy/var/data/config/sknet-{env}/configs/akvorado-yaml").read_text())


def config_text(out, key, env="prod"):
    return (out / f"_copy/var/data/config/sknet-{env}/configs/{key}").read_text()


def labels(svc):
    return dict(l.split("=", 1) for l in svc["deploy"]["labels"])


def mem_mb(value):
    m = re.fullmatch(r"(\d+)([MG])", str(value))
    assert m, value
    return int(m.group(1)) * (1024 if m.group(2) == "G" else 1)


@pytest.fixture(scope="module")
def base_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("sknet-base"), BASE)


@pytest.fixture(scope="module")
def live_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("sknet-live"), BASE | {"LIVE_UPSTREAM": "http://192.0.2.1:3000"})


@pytest.fixture(scope="module")
def inv_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("sknet-inv"), BASE | {"FLOW_SOURCES": INV_SOURCES}, exporters=EXPORTERS)


@pytest.fixture(scope="module")
def set_out(tmp_path_factory):
    return ok(tmp_path_factory.mktemp("sknet-set"), BASE | SET)


# --- services, images, placement, storage -----------------------------------

def test_services_without_and_with_live_upstream(base_out, live_out):
    core = ["clickhouse", "console", "inlet", "kafka", "orchestrator", "outlet"]
    assert sorted(compose(base_out)["services"]) == core
    assert sorted(compose(live_out)["services"]) == sorted(core + ["live-proxy"])


def test_every_image_is_pinned_and_the_defaults_are_the_documented_ones(live_out):
    svcs = compose(live_out)["services"]
    for name, svc in svcs.items():
        image = svc["image"]
        assert "@sha256:" in image or (":" in image.split("/")[-1] and not image.endswith(":latest")), (name, image)
    for name in ("orchestrator", "inlet", "outlet", "console"):
        assert svcs[name]["image"] == AKVORADO_IMAGE
    assert svcs["clickhouse"]["image"] == CLICKHOUSE_IMAGE
    assert svcs["kafka"]["image"] == KAFKA_IMAGE
    assert svcs["live-proxy"]["image"] == DEFAULTS["PROXY_IMAGE"]
    assert ":latest" not in svcs["live-proxy"]["image"]


def test_inlet_publishes_the_flow_ports_in_host_mode(base_out):
    ports = compose(base_out)["services"]["inlet"]["ports"]
    assert sorted((p["published"], p["target"], p["protocol"], p["mode"]) for p in ports) == [
        (2055, 2055, "udp", "host"), (4739, 4739, "udp", "host")]
    # nothing else is published: the other services are reached over the overlay or Traefik
    for name, svc in compose(base_out)["services"].items():
        if name != "inlet":
            assert "ports" not in svc, name


def test_data_services_are_pinned_to_the_data_node_and_the_proxy_is_not(live_out):
    svcs = compose(live_out)["services"]
    for name in ("orchestrator", "inlet", "outlet", "console", "kafka", "clickhouse"):
        assert svcs[name]["deploy"]["placement"]["constraints"] == ["node.hostname == render-node-1"], name
    assert not any("render-node-1" in c for c in
                   svcs["live-proxy"]["deploy"].get("placement", {}).get("constraints", []))


def test_clickhouse_and_kafka_live_on_local_disk_under_data_dir(base_out):
    svcs = compose(base_out)["services"]
    assert svcs["clickhouse"]["volumes"] == ["/var/lib/sknet-prod/clickhouse:/var/lib/clickhouse"]
    assert svcs["kafka"]["volumes"] == ["/var/lib/sknet-prod/kafka:/var/lib/kafka/data"]
    assert svcs["outlet"]["volumes"] == ["/var/lib/sknet-prod/outlet:/run/akvorado"]
    assert svcs["console"]["volumes"] == ["/var/lib/sknet-prod/console:/run/akvorado"]
    for svc in svcs.values():
        for vol in svc.get("volumes", []):
            assert not vol.startswith("/var/data"), vol   # never NFS


def test_every_service_restarts_forever_and_has_a_memory_limit(live_out):
    for name, svc in compose(live_out)["services"].items():
        assert svc["deploy"]["restart_policy"]["condition"] == "any", name
        assert "max_attempts" not in svc["deploy"]["restart_policy"], name
        assert mem_mb(svc["deploy"]["resources"]["limits"]["memory"]) > 0, name
    for name in ("kafka", "clickhouse"):
        assert compose(live_out)["services"][name]["deploy"]["update_config"]["order"] == "stop-first"


def test_default_memory_budget_is_at_most_3_5_gb(live_out):
    total = sum(mem_mb(s["deploy"]["resources"]["limits"]["memory"]) for s in compose(live_out)["services"].values())
    assert total <= int(3.5 * 1024), total


def test_kafka_is_kraft_single_node(base_out):
    env = dict(e.split("=", 1) for e in compose(base_out)["services"]["kafka"]["environment"])
    assert env["KAFKA_PROCESS_ROLES"] == "controller,broker"
    assert env["KAFKA_CONTROLLER_QUORUM_VOTERS"] == "1@kafka:9093"
    assert env["KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR"] == "1"
    assert "-Xmx" in env["KAFKA_HEAP_OPTS"]


# --- routers, allowlist, sign-in --------------------------------------------

def test_console_router_host_and_middleware_chain(base_out):
    lab = labels(compose(base_out)["services"]["console"])
    assert lab["traefik.http.routers.sknet.rule"] == "Host(`sknet.render.example.test`)"
    assert lab["traefik.http.routers.sknet.entrypoints"] == "websecure"
    assert lab["traefik.http.routers.sknet.service"] == "sknet"
    assert lab["traefik.http.services.sknet.loadbalancer.server.port"] == "8080"
    assert lab["traefik.swarm.network"] == "cloud-public-prod"
    chain = lab["traefik.http.routers.sknet.middlewares"].split(",")
    assert "sknet-prod-allow" in chain and "authentik@file" in chain
    # the allowlist runs first: an address outside it never reaches the SSO provider
    assert chain.index("sknet-prod-allow") < chain.index("authentik@file")


def test_allowlist_middleware_defaults_to_private_and_mesh_ranges(base_out):
    lab = labels(compose(base_out)["services"]["console"])
    assert lab["traefik.http.middlewares.sknet-prod-allow.ipallowlist.sourcerange"].split(",") == DEFAULT_CIDRS


def test_allowlist_middleware_follows_allow_cidrs(tmp_path):
    cidrs = ["203.0.113.0/24", "198.51.100.7/32"]
    out = ok(tmp_path, BASE | {"ALLOW_CIDRS": cidrs, "LIVE_UPSTREAM": "http://192.0.2.1:3000"})
    svcs = compose(out)["services"]
    assert labels(svcs["console"])["traefik.http.middlewares.sknet-prod-allow.ipallowlist.sourcerange"] == ",".join(cidrs)
    assert "sknet-prod-allow" in labels(svcs["live-proxy"])["traefik.http.routers.sknet-live.middlewares"].split(",")


def test_live_router_and_proxy(live_out):
    svc = compose(live_out)["services"]["live-proxy"]
    lab = labels(svc)
    assert lab["traefik.http.routers.sknet-live.rule"] == "Host(`sknet-live.render.example.test`)"
    assert lab["traefik.http.services.sknet-live.loadbalancer.server.port"] == "8080"
    chain = lab["traefik.http.routers.sknet-live.middlewares"].split(",")
    assert chain[0] == "sknet-prod-allow" and "authentik@file" in chain
    caddy = config_text(live_out, "caddyfile")
    assert "reverse_proxy http://192.0.2.1:3000" in caddy
    assert "admin off" in caddy and "auto_https off" in caddy


def test_no_live_router_without_live_upstream(base_out):
    for svc in compose(base_out)["services"].values():
        assert not any("sknet-live" in l for l in svc["deploy"].get("labels", []))
    assert not (base_out / "_copy/var/data/config/sknet-prod/configs/caddyfile").exists()


def test_allowlist_still_applies_when_sso_is_off(tmp_path):
    out = ok(tmp_path, BASE | {"AUTH_MODE": "allowlist", "LIVE_UPSTREAM": "http://192.0.2.1:3000"})
    svcs = compose(out)["services"]
    for name, router in (("console", "sknet"), ("live-proxy", "sknet-live")):
        chain = labels(svcs[name])[f"traefik.http.routers.{router}.middlewares"].split(",")
        assert "sknet-prod-allow" in chain and "authentik@file" not in chain, name
    # without a forwarded login the console needs a fixed user, or it would deny everyone
    assert akvorado(out)["console"]["auth"]["default-user"]["login"]


def test_authentik_mode_denies_a_request_without_the_login_header(base_out):
    auth = akvorado(base_out)["console"]["auth"]
    assert auth["headers"] == {"login": "X-authentik-username", "name": "X-authentik-name",
                               "email": "X-authentik-email"}
    assert not auth["default-user"]["login"]


def test_custom_sso_and_base_middlewares(tmp_path):
    out = ok(tmp_path, BASE | {"SSO_MIDDLEWARE": "sso-chain@file", "TRAEFIK_MIDDLEWARES": "gzip@file"})
    chain = labels(compose(out)["services"]["console"])["traefik.http.routers.sknet.middlewares"]
    assert chain == "sknet-prod-allow,gzip@file,sso-chain@file"


@pytest.mark.parametrize("env,host,router,subnet", [
    ("dev", "sknet-dev.render.example.test", "sknet-dev", "172.16.69.0/24"),
    ("staging", "sknet-staging.render.example.test", "sknet-staging", "172.16.68.0/24"),
])
def test_dev_and_staging_hosts_routers_and_subnets(tmp_path, env, host, router, subnet):
    out = ok(tmp_path, BASE, env)
    lab = labels(compose(out, env)["services"]["console"])
    assert lab[f"traefik.http.routers.{router}.rule"] == f"Host(`{host}`)"
    assert f"traefik.http.middlewares.sknet-{env}-allow.ipallowlist.sourcerange" in lab
    assert subnet in (ROLE / f"deploy_sknet-{env}.yml").read_text()


# --- Akvorado configuration --------------------------------------------------

def test_akvorado_sampling_ttl_and_listeners_by_default(base_out):
    cfg = akvorado(base_out)
    assert cfg["outlet"]["core"]["default-sampling-rate"] == 1
    ttl = {str(r["interval"]): r["ttl"] for r in cfg["clickhouse"]["resolutions"]}
    assert ttl["0"] == "720h"            # FLOW_TTL_DAYS 30
    assert ttl["1m"] == "720h" and ttl["5m"] == "2160h" and ttl["1h"] == "8640h"   # FLOW_AGG_TTL_DAYS 90, 1h = 4x
    listen = sorted(i["listen"] for i in cfg["inlet"]["flow"]["inputs"])
    assert listen == [":2055", ":4739"]
    assert cfg["kafka"]["brokers"] == ["kafka:9092"]
    assert cfg["clickhousedb"]["servers"] == ["clickhouse:9000"]


def test_static_exporters_fall_back_to_the_vault(base_out):
    providers = akvorado(base_out)["outlet"]["metadata"]["providers"]
    assert [p["type"] for p in providers] == ["static"]
    exp = providers[0]["exporters"]["192.0.2.10/32"]
    assert exp["name"] == "gw-example"
    assert exp["ifindexes"][101] == {"name": "eth0", "description": "Example LAN", "speed": 1000}
    assert exp["ifindexes"][102]["name"] == "wt0" and exp["ifindexes"][103]["name"] == "wg-site"


def test_static_exporters_come_from_inventory_hostvars_when_the_group_exists(inv_out):
    exps = akvorado(inv_out)["outlet"]["metadata"]["providers"][0]["exporters"]
    assert sorted(exps) == ["192.0.2.20/32", "192.0.2.21/32"]   # the vault fallback is not merged in
    assert exps["192.0.2.20/32"]["name"] == "gw-inv-a"
    assert exps["192.0.2.20/32"]["ifindexes"][101]["speed"] == 10000
    assert exps["192.0.2.21/32"]["ifindexes"][101]["name"] == "wg-peer"


def test_interface_classes_drive_the_classifiers(inv_out):
    rules = akvorado(inv_out)["outlet"]["core"]["interface-classifiers"]
    by_text = "\n".join(rules)
    assert 'Exporter.IP == "192.0.2.20" && Interface.Index == 103' in by_text
    ens3 = next(r for r in rules if "Interface.Index == 103" in r)
    assert 'ClassifyConnectivity("internet")' in ens3 and "ClassifyExternal()" in ens3
    lan = next(r for r in rules if 'Exporter.IP == "192.0.2.20" && Interface.Index == 101' in r)
    assert 'ClassifyConnectivity("lan")' in lan and "ClassifyInternal()" in lan
    link = next(r for r in rules if 'Exporter.IP == "192.0.2.21"' in r)
    assert 'ClassifyConnectivity("estate-link")' in link


def test_home_filters_only_use_the_external_boundary_when_an_internet_interface_exists(base_out, inv_out):
    assert akvorado(base_out)["console"].get("homepage-graph-filter", "") == ""
    assert akvorado(inv_out)["console"]["homepage-graph-filter"] == "InIfBoundary = external"


def test_absent_exporters_leave_the_metadata_and_keep_the_others(tmp_path):
    exporters = {**EXPORTERS, "site-inv-b": {**EXPORTERS["site-inv-b"], "sknet_state": "absent"}}
    out = ok(tmp_path, BASE | {"FLOW_SOURCES": INV_SOURCES}, exporters=exporters)
    assert sorted(akvorado(out)["outlet"]["metadata"]["providers"][0]["exporters"]) == ["192.0.2.20/32"]


def test_flow_ttl_days_sets_the_raw_and_one_minute_tables(tmp_path):
    out = ok(tmp_path, BASE | {"FLOW_TTL_DAYS": 14})
    ttl = {str(r["interval"]): r["ttl"] for r in akvorado(out)["clickhouse"]["resolutions"]}
    assert ttl["0"] == "336h" and ttl["1m"] == "336h" and ttl["5m"] == "2160h" and ttl["1h"] == "8640h"


def test_aggregate_ttls_follow_flow_agg_ttl_days_and_nothing_is_hardcoded(tmp_path):
    out = ok(tmp_path, BASE | {"FLOW_AGG_TTL_DAYS": 30})
    ttl = {str(r["interval"]): r["ttl"] for r in akvorado(out)["clickhouse"]["resolutions"]}
    assert ttl["5m"] == "720h" and ttl["1h"] == "2880h" and ttl["0"] == "720h"
    assert "8760" not in (ROLE / "src/config/sknet/akvorado.yaml.j2").read_text()


def test_clickhouse_keeps_free_space_from_min_free_gb(base_out, tmp_path):
    assert "<keep_free_space_bytes>5368709120</keep_free_space_bytes>" in config_text(base_out, "clickhouse-lowmem")
    out = ok(tmp_path, BASE | {"MIN_FREE_GB": 8})
    assert "<keep_free_space_bytes>8589934592</keep_free_space_bytes>" in config_text(out, "clickhouse-lowmem")


def test_the_disk_gate_counts_space_the_data_dir_already_uses():
    text = (ROLE / "tasks/data_dirs.yml").read_text()
    assert "du -sk" in text and "already" in text
    gate = yaml.safe_load(text)
    assert_task = next(t for t in gate if "DISK_BUDGET_GB" in str(t.get("assert", "")))
    cond = str(assert_task["assert"]["that"])
    assert "used" in cond and "free" in cond
    assert "GB used" in assert_task["assert"]["fail_msg"] and "GB free" in assert_task["assert"]["fail_msg"]


def test_allowlist_middleware_uses_the_direct_remote_address(base_out):
    lab = labels(compose(base_out)["services"]["console"])
    assert lab["traefik.http.middlewares.sknet-prod-allow.ipallowlist.ipstrategy.depth"] == "0"


def test_allowlist_mode_is_refused_behind_cloudflared(tmp_path):
    log = fails(tmp_path, BASE | {"AUTH_MODE": "allowlist", "CLOUDFLARED": True})
    assert "CLOUDFLARED" in log and "real client" in log
    ok(tmp_path / "b", BASE | {"AUTH_MODE": "authentik", "CLOUDFLARED": True})


def test_networks_map_names_the_cidrs(tmp_path):
    out = ok(tmp_path, BASE | {"NETWORKS": {"192.0.2.0/24": "site-a-lan"}})
    nets = akvorado(out)["outlet"]["networks"]["networks"]
    assert nets["192.0.2.0/24"]["name"] == "site-a-lan"


def test_geoip_is_off_by_default_and_optional_when_on(base_out, tmp_path):
    assert "geoip" not in akvorado(base_out)["outlet"]
    out = ok(tmp_path, BASE | {"GEOIP_ENABLED": True})
    geo = akvorado(out)["outlet"]["geoip"]
    assert geo["optional"] is True and geo["asn-database"] and geo["geo-database"]


def test_clickhouse_low_memory_config(base_out):
    lowmem = config_text(base_out, "clickhouse-lowmem")
    assert "<max_server_memory_usage_to_ram_ratio>" in lowmem and "<mark_cache_size>" in lowmem
    assert '<text_log remove="1"/>' in lowmem and '<trace_log remove="1"/>' in lowmem
    assert "<max_threads>2</max_threads>" in lowmem
    assert "INTERVAL 7 DAY DELETE" in config_text(base_out, "clickhouse-logs")
    cfgs = compose(base_out)["services"]["clickhouse"]["configs"]
    assert sorted(c["target"] for c in cfgs) == [
        "/etc/clickhouse-server/config.d/akvorado.xml", "/etc/clickhouse-server/config.d/lowmem.xml"]


def test_swarm_configs_are_named_after_their_content(base_out, tmp_path):
    cfgs = compose(base_out)["configs"]
    assert all(re.fullmatch(r"sknet_prod_[a-z-]+_[0-9a-f]{12}", c["name"]) for c in cfgs.values()), cfgs
    out = ok(tmp_path, BASE | {"FLOW_TTL_DAYS": 7})
    assert compose(out)["configs"]["akvorado-yaml"]["name"] != cfgs["akvorado-yaml"]["name"]


# --- port allowlist, directories, deploy script ------------------------------

def test_port_allowlist_script_protects_the_flow_ports(base_out):
    script = (base_out / "usr/local/sbin/sknet-prod-fw").read_text()
    assert ":SKNET-PROD - [0:0]" in script
    for port in (2055, 4739):
        assert f"-A SKNET-PROD -p udp -m conntrack --ctorigdstport {port} --ctdir ORIGINAL -s 192.0.2.10 -j RETURN" in script
        assert f"-A SKNET-PROD -p udp -m conntrack --ctorigdstport {port} --ctdir ORIGINAL -j DROP" in script
    assert "DOCKER-USER" in script and "-I INPUT" in script
    unit = (base_out / "etc/systemd/system/sknet-prod-fw.service").read_text()
    assert "ExecStart=/usr/local/sbin/sknet-prod-fw" in unit


def test_port_allowlist_follows_custom_ports_and_sources(tmp_path):
    out = ok(tmp_path, BASE | {"NETFLOW_PORT": 2056, "IPFIX_PORT": 4740, "FLOW_SOURCES": ["192.0.2.10", "192.0.2.11"],
                               "STATIC_EXPORTERS": {"a": {"sknet_export_address": "192.0.2.11", "sknet_interfaces":
                                                          [{"name": "eth0", "index": 101, "class": "lan"}]}}})
    script = (out / "usr/local/sbin/sknet-prod-fw").read_text()
    assert "--ctorigdstport 2056 --ctdir ORIGINAL -s 192.0.2.11 -j RETURN" in script
    assert "--ctorigdstport 4740 --ctdir ORIGINAL -s 192.0.2.10 -j RETURN" in script
    assert "--ctorigdstport 2055" not in script
    inlet = compose(out)["services"]["inlet"]["ports"]
    assert sorted(p["published"] for p in inlet) == [2056, 4740]
    assert sorted(i["listen"] for i in akvorado(out)["inlet"]["flow"]["inputs"]) == [":2055", ":4739"]


def test_data_dirs_and_deploy_script_exist(base_out):
    assert (base_out / "var/data/sknet-prod/deploy").exists()
    deploy = (base_out / "var/data/sknet-prod/deploy").read_text()
    assert "docker stack deploy" in deploy and "sknet-prod" in deploy and "--prune" in deploy
    dirs = yaml.safe_load((ROLE / "tasks" / "data_dirs.yml").read_text())
    loop = next(t for t in dirs if "Create the sknet data directories" in t["name"])["loop"]
    owners = {i["sub"]: str(i["owner"]) for i in loop}
    assert owners == {"kafka": "1000", "clickhouse": "101", "outlet": "0", "console": "0"}
    assert all(t.get("delegate_to") == "{{ sknet_data_node }}" for t in dirs if "file" in t or "shell" in t)


def test_data_dir_override(tmp_path):
    out = ok(tmp_path, BASE | {"DATA_DIR": "/srv/sknet-x"})
    assert compose(out)["services"]["clickhouse"]["volumes"] == ["/srv/sknet-x/clickhouse:/var/lib/clickhouse"]


# --- every knob set ------------------------------------------------------------

def test_set_pass_images_sizing_and_ports(set_out):
    svcs = compose(set_out)["services"]
    assert svcs["outlet"]["image"] == SET["AKVORADO_IMAGE"]
    assert svcs["clickhouse"]["image"] == SET["CLICKHOUSE_IMAGE"]
    assert svcs["kafka"]["image"] == SET["KAFKA_IMAGE"]
    assert svcs["live-proxy"]["image"] == SET["PROXY_IMAGE"]
    assert svcs["clickhouse"]["deploy"]["resources"]["limits"]["memory"] == "2048M"
    assert svcs["outlet"]["deploy"]["resources"]["limits"]["memory"] == "768M"
    assert svcs["live-proxy"]["deploy"]["placement"]["constraints"] == ["node.role == worker"]
    assert sorted(p["published"] for p in svcs["inlet"]["ports"]) == [2056, 4740]
    assert svcs["clickhouse"]["volumes"] == ["/srv/sknet-render/clickhouse:/var/lib/clickhouse"]


def test_set_pass_fqdn_hostnames_and_sso(set_out):
    svcs = compose(set_out)["services"]
    lab = labels(svcs["console"])
    assert lab["traefik.http.routers.sknet.rule"] == "Host(`sknet.example.test`)"
    assert lab["traefik.http.routers.sknet.middlewares"] == \
        "sknet-prod-allow,crowdsec-bouncer@file,default-no-error-pages@file,sso-example@file"
    assert lab["traefik.http.middlewares.sknet-prod-allow.ipallowlist.sourcerange"] == "203.0.113.0/24,198.51.100.0/24"
    assert labels(svcs["live-proxy"])["traefik.http.routers.sknet-live.rule"] == "Host(`sknet-live.example.test`)"


# --- refusals ------------------------------------------------------------------

def test_empty_flow_sources_fails(tmp_path):
    log = fails(tmp_path, BASE | {"FLOW_SOURCES": []})
    assert "FLOW_SOURCES" in log


def test_missing_flow_sources_fails(tmp_path):
    assert "FLOW_SOURCES" in fails(tmp_path, {k: v for k, v in BASE.items() if k != "FLOW_SOURCES"})


@pytest.mark.parametrize("source", ["flows.example.test", "192.0.2.0/24", "2001:db8::1", "192.0.2.300"])
def test_flow_sources_must_be_ipv4_literals(tmp_path, source):
    assert "IPv4 literal" in fails(tmp_path, BASE | {"FLOW_SOURCES": ["192.0.2.10", source]})


def test_missing_data_node_fails(tmp_path):
    log = fails(tmp_path, {k: v for k, v in BASE.items() if k != "DATA_NODE"})
    assert "DATA_NODE" in log


def test_missing_vault_block_fails(tmp_path):
    assert "no sknet vault block" in fails(tmp_path, None)


def test_both_exporter_sources_empty_fails(tmp_path):
    log = fails(tmp_path, {k: v for k, v in BASE.items() if k != "STATIC_EXPORTERS"})
    assert "exporter" in log.lower() and "STATIC_EXPORTERS" in log


def test_inventory_group_with_only_absent_hosts_and_no_fallback_fails(tmp_path):
    only_absent = {"gw-inv-a": {**EXPORTERS["gw-inv-a"], "sknet_state": "absent"}}
    fails(tmp_path, {k: v for k, v in BASE.items() if k != "STATIC_EXPORTERS"} | {"FLOW_SOURCES": INV_SOURCES},
          exporters=only_absent)


def test_exporter_address_missing_from_flow_sources_fails(tmp_path):
    log = fails(tmp_path, BASE | {"FLOW_SOURCES": ["192.0.2.99"]})
    assert "192.0.2.10" in log and "FLOW_SOURCES" in log


@pytest.mark.parametrize("ifaces,why", [
    ([{"name": "eth0", "index": 99, "class": "lan"}], "index"),
    ([{"name": "eth0", "index": 101, "class": "lan"}, {"name": "wt0", "index": 101, "class": "mesh"}], "index"),
    ([{"name": "eth0", "index": 101, "class": "wan"}], "class"),
    ([{"name": "", "index": 101, "class": "lan"}], "name"),
    ([], "interfaces"),
], ids=["low-index", "duplicate-index", "unknown-class", "empty-name", "no-interfaces"])
def test_bad_exporter_interfaces_fail(tmp_path, ifaces, why):
    exporters = {"gw-inv-a": {"sknet_export_address": "192.0.2.20", "sknet_interfaces": ifaces}}
    log = fails(tmp_path, BASE | {"FLOW_SOURCES": ["192.0.2.20"]}, exporters=exporters)
    assert "gw-inv-a" in log


def test_exporter_address_must_be_an_ipv4_literal(tmp_path):
    exporters = {"gw-inv-a": {**EXPORTERS["gw-inv-a"], "sknet_export_address": "gw.example.test"}}
    assert "IPv4 literal" in fails(tmp_path, BASE, exporters=exporters)


@pytest.mark.parametrize("vault", [
    BASE | {"AUTH_MODE": "none"},
    BASE | {"AUTH_MODE": "allowlist", "SSO_MIDDLEWARE": "authentik@file"},
], ids=["unknown-mode", "allowlist-with-sso"])
def test_bad_auth_mode_fails(tmp_path, vault):
    fails(tmp_path, vault)


@pytest.mark.parametrize("cidrs", [[], ["10.0.0.0/8", "not-a-cidr"], ["10.0.0.0/33"], ["10.0.0.1"], "10.0.0.0/8"],
                         ids=["empty", "garbage", "bad-prefix", "no-prefix", "string"])
def test_bad_allow_cidrs_fail(tmp_path, cidrs):
    assert "ALLOW_CIDRS" in fails(tmp_path, BASE | {"ALLOW_CIDRS": cidrs})


@pytest.mark.parametrize("upstream", ["192.0.2.1:3000", "http://ntop.example.test:3000", "https://192.0.2.1:3000",
                                      "http://192.0.2.1", "http://192.0.2.1:3000/path"])
def test_bad_live_upstream_fails(tmp_path, upstream):
    assert "LIVE_UPSTREAM" in fails(tmp_path, BASE | {"LIVE_UPSTREAM": upstream})


@pytest.mark.parametrize("knob,value", [
    ("AKVORADO_IMAGE", "quay.io/akvorado/akvorado:latest"),
    ("KAFKA_IMAGE", "apache/kafka"),
    ("CLICKHOUSE_IMAGE", "clickhouse/clickhouse-server:latest"),
    ("PROXY_IMAGE", "caddy:stable"),
])
def test_floating_image_fails(tmp_path, knob, value):
    vault = BASE | {knob: value, "LIVE_UPSTREAM": "http://192.0.2.1:3000"}
    assert "pinned" in fails(tmp_path, vault)


@pytest.mark.parametrize("knob,value", [
    ("FLOW_TTL_DAYS", 0), ("FLOW_TTL_DAYS", 4000), ("FLOW_TTL_DAYS", "many"),
    ("DISK_BUDGET_GB", 1), ("FLOW_AGG_TTL_DAYS", 366), ("FLOW_AGG_TTL_DAYS", 0), ("MIN_FREE_GB", 0), ("NETFLOW_PORT", 70000), ("IPFIX_PORT", 2055),
])
def test_out_of_range_numbers_fail(tmp_path, knob, value):
    fails(tmp_path, BASE | {knob: value})


# --- static --------------------------------------------------------------------

def test_playbook_defaults_equal_the_compose_literals():
    template = (ROLE / "src/config/sknet/sknet.yml.j2").read_text()
    assert DEFAULTS["AKVORADO_IMAGE"] == AKVORADO_IMAGE
    assert DEFAULTS["CLICKHOUSE_IMAGE"] == CLICKHOUSE_IMAGE
    assert DEFAULTS["KAFKA_IMAGE"] == KAFKA_IMAGE
    for key in ("AKVORADO_IMAGE", "CLICKHOUSE_IMAGE", "KAFKA_IMAGE", "PROXY_IMAGE"):
        assert f"default('{DEFAULTS[key]}', true)" in template, key
    assert DEFAULTS["NTOPNG_IMAGE"] and ":latest" not in DEFAULTS["NTOPNG_IMAGE"]


def test_default_allow_cidrs_and_ports_are_the_documented_ones():
    assert DEFAULTS["ALLOW_CIDRS"] == DEFAULT_CIDRS
    assert (DEFAULTS["NETFLOW_PORT"], DEFAULTS["IPFIX_PORT"], DEFAULTS["FLOW_TTL_DAYS"], DEFAULTS["DISK_BUDGET_GB"]) \
        == (2055, 4739, 30, 20)
    assert DEFAULTS["FLOW_SOURCES"] == [] and DEFAULTS["GEOIP_ENABLED"] is False and DEFAULTS["NETWORKS"] == {}
    assert DEFAULTS["HOSTNAME"] == "sknet" and DEFAULTS["LIVE_HOSTNAME"] == "sknet-live"


def test_env_playbooks_differ_only_in_env_and_subnets():
    def norm(text):
        text = re.sub(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])", "ENV", text)
        return re.sub(r"\d+\.\d+\.\d+\.\d+/\d+", "CIDR", text)
    prod = norm((ROLE / "deploy_sknet-prod.yml").read_text())
    for env in ("dev", "staging"):
        assert norm((ROLE / f"deploy_sknet-{env}.yml").read_text()) == prod, env


def test_no_em_or_en_dashes_in_the_role():
    for path in list(ROLE.rglob("*")) + [Path(__file__), rp.VARS / "sknet.example.yml", rp.VARS / "sknet.set.yml"]:
        if path.is_file():
            text = path.read_text()
            assert chr(0x2014) not in text and chr(0x2013) not in text, path
