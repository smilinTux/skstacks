"""skmon v2 compose: VictoriaMetrics, Loki, Grafana and Alertmanager on DATA_NODE local disk
with content-hash-named Swarm configs."""
import re

import pytest
import yaml

from skmon_support import CFG, ROLE, compose, ctx_with_configs, play_tasks, PLAYS, render, services

DATA_NODE = "node-1.render.example.test"
STATEFUL = ("victoriametrics", "loki", "grafana", "alertmanager")
# Services on the alert path (spec 4, D13): nothing of theirs under /var/data.
ALERT_PATH = ("victoriametrics", "vmalert", "alertmanager")


def walk(node):
    """Every scalar in a nested structure."""
    if isinstance(node, dict):
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)
    else:
        yield node


def test_service_set():
    assert set(services()) == {"victoriametrics", "loki", "grafana", "alertmanager", "vmalert"}


def test_alert_path_reads_nothing_from_var_data():
    for n in ALERT_PATH:
        s = services()[n]
        assert "env_file" not in s, n
        assert not any(str(v).startswith("/var/data") for v in walk({k: s.get(k) for k in ("volumes", "command")})), n


def test_no_var_data_bind_on_any_stateful_service():
    for n, s in services().items():
        for v in s.get("volumes", []):
            src = v["source"] if isinstance(v, dict) else v.split(":")[0]
            assert not src.startswith("/var/data"), (n, src)
        assert "env_file" not in s or n == "grafana"


def test_local_volumes_under_data_path():
    s = services(DATA_PATH="/srv/local/x")
    assert "/srv/local/x/vm:/storage" in s["victoriametrics"]["volumes"]
    assert "/srv/local/x/loki:/loki" in s["loki"]["volumes"]
    assert "/srv/local/x/grafana:/var/lib/grafana" in s["grafana"]["volumes"]
    assert "/srv/local/x/grafana-provisioning:/etc/grafana/provisioning:ro" in s["grafana"]["volumes"]
    assert "/srv/local/x/alertmanager:/alertmanager" in s["alertmanager"]["volumes"]


def test_pinned_to_data_node():
    for n in (*STATEFUL, "vmalert"):
        assert f"node.hostname == {DATA_NODE}" in services()[n]["deploy"]["placement"]["constraints"], n


def test_victoriametrics_flags():
    cmd = services()["victoriametrics"]["command"]
    assert "-retentionPeriod=90d" in cmd and "-promscrape.config=/etc/skmon/vm-scrape.yml" in cmd
    assert "-storageDataPath=/storage" in cmd
    assert "-promscrape.config.strictParse=true" in cmd and "-selfScrapeInterval=30s" in cmd
    assert "-retentionPeriod=30d" in services(METRICS_RETENTION="30d")["victoriametrics"]["command"]


def test_victoriametrics_vmui_router_uses_forward_auth():
    labels = services()["victoriametrics"]["deploy"]["labels"]
    assert any("Host(`vmui.c1.example.test`)" in l for l in labels)
    mw = [l for l in labels if l.endswith(".middlewares=" + l.split(".middlewares=")[-1]) and ".middlewares=" in l]
    assert mw and all("authentik@file" in l and "traefikAuth" not in l for l in mw)
    assert any("loadbalancer.server.port=8428" in l for l in labels)
    custom = services(FORWARD_AUTH_MIDDLEWARE="sso@file")["victoriametrics"]["deploy"]["labels"]
    assert any("sso@file" in l for l in custom)
    dev = yaml.safe_load(render(CFG / "skmon.yml.j2", env="dev", extra_ctx=ctx_with_configs(env="dev")))
    assert any("Host(`vmui-dev.c1.example.test`)" in l for l in dev["services"]["victoriametrics"]["deploy"]["labels"])


def test_alertmanager_uses_forward_auth_not_basic_auth():
    labels = services()["alertmanager"]["deploy"]["labels"]
    mw = [l for l in labels if ".middlewares=" in l]
    assert mw and all("authentik@file" in l and "traefikAuth" not in l for l in mw)


def test_limits_match_spec_13():
    want = {"victoriametrics": ("256M", "1G", "1.0"), "loki": ("384M", "1G", "1.0"),
            "grafana": ("128M", "512M", "0.5"), "alertmanager": ("32M", "128M", "0.25"),
            "vmalert": ("32M", "128M", "0.25")}
    for n, (res, lim, cpu) in want.items():
        r = services()[n]["deploy"]["resources"]
        assert (r["reservations"]["memory"], r["limits"]["memory"], str(r["limits"]["cpus"])) == (res, lim, cpu), n


def test_images_are_pinned_without_floating_tags():
    for n, s in services().items():
        assert re.search(r":v?\d+\.\d+\.\d+", s["image"]), (n, s["image"])
    assert services(LOKI_IMAGE="example.test/loki:9.9.9")["loki"]["image"] == "example.test/loki:9.9.9"


def test_configs_are_hash_named_and_change_with_content():
    a = compose()["configs"]; b = compose(METRICS_RETENTION="30d")["configs"]
    rule_keys = {f"rules-{c}" for c in ("node", "storage", "http", "cert", "capacity", "container", "meta")}
    assert set(a) == {"vm-scrape", "loki", "alertmanager", "loki-rules"} | rule_keys
    assert all(re.fullmatch(r"skmon_prod_[a-z0-9_-]+_[0-9a-f]{12}", c["name"]) for c in a.values())
    assert a["vm-scrape"]["name"] == b["vm-scrape"]["name"]  # retention is a flag, not scrape content
    assert a["loki"]["name"] != compose(LOGS_RETENTION="7d")["configs"]["loki"]["name"]
    assert a["loki"]["file"] == "/var/data/config/skmon-prod/configs/loki"


def test_services_consume_their_configs():
    s = services()
    assert {"source": "vm-scrape", "target": "/etc/skmon/vm-scrape.yml"} in s["victoriametrics"]["configs"]
    assert {"source": "loki", "target": "/etc/loki/local-config.yaml"} in s["loki"]["configs"]
    assert {"source": "alertmanager", "target": "/etc/alertmanager/config.yml"} in s["alertmanager"]["configs"]


def test_loki_retention_and_local_paths():
    doc = yaml.safe_load(render(CFG / "loki-config.yml.j2"))
    assert doc["limits_config"]["retention_period"] == "30d"
    assert doc["compactor"]["retention_enabled"] is True
    assert doc["compactor"]["delete_request_store"] == "filesystem"
    assert not any(str(v).startswith("/var/data") for v in walk(doc))
    assert yaml.safe_load(render(CFG / "loki-config.yml.j2", LOGS_RETENTION="7d"))["limits_config"]["retention_period"] == "7d"


def test_loki_push_port_published_host_mode():
    ports = services()["loki"]["ports"]
    assert {"target": 3100, "published": 3100, "protocol": "tcp", "mode": "host"} in ports


@pytest.mark.parametrize("r", [0, "0"])
def test_replicas_string_zero_parks_everything(r):
    assert all(s["deploy"]["replicas"] == 0 for s in services(replicas=r).values())


def test_default_is_one_replica():
    assert all(s["deploy"]["replicas"] == 1 for s in services().values())


def test_grafana_plugins_default_empty_and_knob_works():
    assert "GF_INSTALL_PLUGINS=" in render(CFG / "skmon.env.j2").splitlines()
    assert "GF_INSTALL_PLUGINS=grafana-clock-panel" in render(CFG / "skmon.env.j2", GRAFANA_PLUGINS=["grafana-clock-panel"]).splitlines()


def test_grafana_basic_auth_default_unchanged_and_can_be_disabled():
    lines = lambda **kw: render(CFG / "skmon.env.j2", **kw).splitlines()
    assert not any(l.startswith("GF_AUTH_BASIC_ENABLED") for l in lines())
    assert "GF_AUTH_BASIC_ENABLED=false" in lines(GRAFANA_BASIC_AUTH=False)
    assert "GF_AUTH_BASIC_ENABLED=false" in lines(GRAFANA_BASIC_AUTH="false")


def test_grafana_health_start_period_knob():
    assert services()["grafana"]["healthcheck"]["start_period"] == "60s"
    assert services(GRAFANA_HEALTH_START_PERIOD="10m")["grafana"]["healthcheck"]["start_period"] == "10m"


def test_env_files_carry_no_retired_component_lines():
    for name in ("skmon.env.j2", "skmon.sh.env.j2"):
        text = render(CFG / name).lower()
        assert not any(w in text for w in ("prometheus", "promtail", "cadvisor", "node_exporter", "node-exporter")), name


def test_grafana_datasource_points_at_victoriametrics():
    text = render(CFG / "grafana-datasources.yml.j2")
    assert "http://victoriametrics:8428" in text and "prometheus:9090" not in text


def test_only_traefik_swarm_labels():
    labels = [l for s in services().values() for l in s.get("deploy", {}).get("labels", [])]
    assert not any(l.startswith("traefik.docker.") for l in labels)


def test_prometheus_and_promtail_templates_not_deployed_by_plays():
    assert not (CFG / "promtail-config.yml.j2").exists()
    for play in PLAYS:
        text = play.read_text()
        assert "promtail" not in text.lower() and "prometheus.yml.j2" not in text, play.name


@pytest.mark.parametrize("play", PLAYS, ids=lambda p: p.name)
def test_plays_deliver_configs_and_grafana_provisioning_to_data_node(play):
    tasks = play_tasks(play)
    assert any("skmon_configs" in str(t.get("set_fact", "")) for t in tasks)
    assert any(t.get("copy") and "/configs/" in str(t["copy"].get("dest", "")) for t in tasks)
    prov = [t for t in tasks if "grafana-provisioning" in str(t)]
    assert prov and all(t.get("delegate_to") == "{{ skmon_data_node }}" for t in prov if "template" in t or "copy" in t)
    text = play.read_text()
    assert "/var/data/config/{{ app }}-{{ env }}/grafana" not in text
    assert "/var/data/config/{{ app }}-{{ env }}/loki" not in text
