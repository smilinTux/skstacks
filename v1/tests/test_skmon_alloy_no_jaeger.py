"""skmon ships logs with Grafana Alloy (Promtail is EOL upstream) and no longer
bundles Jaeger (no trace producers, not in the strategic stack). Loki stays
the log store.

These tests pin the swap: Alloy replaces Promtail one for one (same Loki push
endpoint, same two file jobs, same parking constraint when scaled to zero),
and Jaeger is gone from the compose file, Prometheus, the deploy script, the
playbooks and the portal tiles."""
import pathlib
import re

import jinja2
import yaml

ROLE = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmon"
SRC = ROLE / "src"
COMPOSE = SRC / "config/skmon/skmon.yml.j2"
PROM = SRC / "config/skmon/prometheus.yml.j2"
ALLOY = SRC / "config/skmon/alloy-config.alloy.j2"
DEPLOY = SRC / "skmon/deploy.j2"
PLAYBOOKS = [ROLE / f"deploy_skmon-{e}.yml" for e in ("dev", "staging", "prod")]

BASE = {"CLUSTERNAME": "c1", "DOMAIN": "example.org"}


def _render(tpl, **extra):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on", "y")
    return env.from_string(tpl.read_text()).render(app="skmon", env="prod", skmon={**BASE, **extra})


def _services(**extra):
    return yaml.safe_load(_render(COMPOSE, **extra))["services"]


def test_alloy_replaces_promtail_and_jaeger_is_gone():
    svcs = _services()
    assert "alloy" in svcs
    assert "promtail" not in svcs
    assert "jaeger" not in svcs
    # The kept stack.
    assert {"prometheus", "grafana", "loki", "alertmanager", "node-exporter", "cadvisor"} <= set(svcs)


def test_alloy_image_is_an_exact_pinned_version():
    image = _services()["alloy"]["image"]
    assert re.fullmatch(r"grafana/alloy:v\d+\.\d+\.\d+", image), image
    assert _services(ALLOY_IMAGE_VERSION="v9.9.9")["alloy"]["image"] == "grafana/alloy:v9.9.9"


def test_alloy_is_global_and_parks_when_scaled_to_zero():
    up = _services()["alloy"]["deploy"]
    assert up["mode"] == "global" and "placement" not in up
    down = _services(replicas=0)["alloy"]["deploy"]
    assert down["mode"] == "global"
    assert down["placement"]["constraints"] == ["node.id == skmon-scaled-to-zero"]


def test_alloy_mounts_the_same_log_sources_without_the_docker_socket():
    vols = _services()["alloy"]["volumes"]
    assert "/var/lib/docker/containers:/var/lib/docker/containers:ro" in vols
    assert "/var/log:/var/log:ro" in vols
    assert not any("docker.sock" in v for v in vols)
    assert any(v.endswith(":/etc/alloy:ro") for v in vols)
    assert "/etc/alloy/config.alloy" in _services()["alloy"]["command"]


def test_alloy_config_translates_the_promtail_jobs():
    cfg = ALLOY.read_text()
    assert 'url = "http://loki:3100/loki/api/v1/push"' in cfg
    assert '"__path__" = "/var/log/**/*.log"' in cfg and '"job"      = "varlogs"' in cfg
    assert '"__path__" = "/var/lib/docker/containers/*/*.log"' in cfg and '"job"      = "docker"' in cfg
    assert cfg.count("forward_to = [loki.write.default.receiver]") == 2
    # Rendering through Jinja must not change the Alloy syntax.
    assert _render(ALLOY).strip() == cfg.strip()


def test_prometheus_scrapes_alloy_not_promtail_or_jaeger():
    jobs = {j["job_name"]: j for j in yaml.safe_load(_render(PROM))["scrape_configs"]}
    assert "jaeger" not in jobs and "promtail" not in jobs
    sd = jobs["alloy"]["dns_sd_configs"][0]
    assert sd["names"] == ["tasks.skmon-prod_alloy"] and sd["port"] == 12345


def test_no_jaeger_or_promtail_left_in_deploy_script_or_playbooks():
    for path in [DEPLOY, *PLAYBOOKS]:
        text = path.read_text().lower()
        assert "jaeger" not in text, path
        assert "promtail" not in text, path
    assert not (SRC / "config/skmon/icons/jaeger.png").exists()


def test_playbooks_render_the_alloy_config():
    for pb in PLAYBOOKS:
        text = pb.read_text()
        assert '{ src: "alloy-config.alloy.j2", dest: "/var/data/config/{{ app }}-{{ env }}/alloy/config.alloy" }' in text, pb
        assert '/var/data/config/{{ app }}-{{ env }}/alloy", owner' in text, pb
        tasks = [t for play in yaml.safe_load(text) for t in play.get("tasks", [])]
        loop = next(t for t in tasks if "portal tiles" in t.get("name", ""))["loop"]
        assert [t["key"] for t in loop] == ["skmon", "prometheus", "alertmanager"], pb
