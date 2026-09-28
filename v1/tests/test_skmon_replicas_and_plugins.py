"""skmon must be able to stay scaled to zero across a redeploy, and an
instance must be able to choose the Grafana plugin list.

Without a replicas knob, an operator who scales the stack to 0 to save
resources gets it back at full size on the next deploy. Global services
(promtail, cadvisor, node-exporter) cannot be scaled at all and a swarm
service cannot change mode in place, so for them 0 means a placement
constraint that no node satisfies (mode stays global, scaling back up is a
plain redeploy). The plugin list was hardcoded and includes Angular plugins
that current Grafana refuses to load."""
import pathlib

import jinja2
import yaml

SRC = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmon/src"
COMPOSE = SRC / "config/skmon/skmon.yml.j2"
ENV = SRC / "config/skmon/skmon.env.j2"
DEPLOY = SRC / "skmon/deploy.j2"

BASE = {
    "CLUSTERNAME": "c1",
    "DOMAIN": "example.org",
    "GRAFANA_ADMIN_USER": "admin",
    "GRAFANA_ADMIN_PASSWORD": "x",
    "GRAFANA_SECRET_KEY": "y",
    "GRAFANA_ROOT_URL": "https://mon.example.org",
}
REPLICATED = {"prometheus", "grafana", "loki", "alertmanager", "jaeger"}
GLOBAL = {"promtail", "cadvisor", "node-exporter"}
DEFAULT_PLUGINS = ("grafana-clock-panel,grafana-simple-json-datasource,grafana-piechart-panel,"
                   "marcusolsson-json-datasource,grafana-worldmap-panel")


def _text(tpl, **extra):
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    # Same as the Ansible bool filter: strings like "false", "no", "0" are false.
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on", "y")
    return env.from_string(tpl.read_text()).render(app="skmon", env="prod", skmon={**BASE, **extra})


def _services(**extra):
    return yaml.safe_load(_text(COMPOSE, **extra))["services"]


def _env_lines(**extra):
    return [l for l in _text(ENV, **extra).splitlines() if l.startswith("GF_INSTALL_PLUGINS")]


def test_default_is_one_replica_and_unconstrained_globals():
    svcs = _services()
    assert {n for n, s in svcs.items() if s["deploy"]["mode"] == "global"} == GLOBAL
    for n in REPLICATED:
        assert svcs[n]["deploy"]["replicas"] == 1, n
    for n in GLOBAL:
        assert "placement" not in svcs[n]["deploy"], n


def test_replicas_zero_scales_every_service_to_zero():
    svcs = _services(replicas=0)
    for n in REPLICATED:
        assert svcs[n]["deploy"]["mode"] == "replicated"
        assert svcs[n]["deploy"]["replicas"] == 0, n
    for n in GLOBAL:
        d = svcs[n]["deploy"]
        assert d["mode"] == "global", n  # mode cannot change in place
        cons = d["placement"]["constraints"]
        assert len(cons) == 1 and cons[0].startswith("node.id == "), n


def test_replicas_string_from_vault_is_coerced():
    svcs = _services(replicas="2")
    assert all(svcs[n]["deploy"]["replicas"] == 2 for n in REPLICATED)
    assert all("placement" not in svcs[n]["deploy"] for n in GLOBAL)


def test_deploy_script_skips_health_waits_when_scaled_to_zero():
    up = _text(DEPLOY)
    down = _text(DEPLOY, replicas=0)
    assert "LOKI_REPLICAS" in up and "GRAFANA_REPLICAS" in up
    assert "LOKI_REPLICAS" not in down and "GRAFANA_REPLICAS" not in down
    assert "docker stack deploy" in down
    assert "scaled to 0" in down


def test_plugins_default_unchanged():
    assert _env_lines() == ["GF_INSTALL_PLUGINS=" + DEFAULT_PLUGINS]


def test_plugins_override_and_empty():
    assert _env_lines(GRAFANA_PLUGINS=["grafana-clock-panel", "marcusolsson-json-datasource"]) == [
        "GF_INSTALL_PLUGINS=grafana-clock-panel,marcusolsson-json-datasource"]
    assert _env_lines(GRAFANA_PLUGINS=[]) == []


def _env_all(**extra):
    return _text(ENV, **extra).splitlines()


def test_grafana_basic_auth_default_unchanged_and_can_be_disabled():
    # Behind the edge basic-auth middleware the browser Authorization header
    # reaches Grafana, which tries it as a Grafana login: with the same user
    # name as the Grafana admin, every request is a failed admin login and
    # Grafana locks the admin out. GRAFANA_BASIC_AUTH: false stops that.
    assert not any(l.startswith("GF_AUTH_BASIC_ENABLED") for l in _env_all())
    assert "GF_AUTH_BASIC_ENABLED=false" in _env_all(GRAFANA_BASIC_AUTH=False)
    assert "GF_AUTH_BASIC_ENABLED=false" in _env_all(GRAFANA_BASIC_AUTH="false")
    assert not any(l.startswith("GF_AUTH_BASIC_ENABLED") for l in _env_all(GRAFANA_BASIC_AUTH=True))


def test_grafana_health_start_period_knob():
    # First boot on a slow shared filesystem (hundreds of SQLite migrations,
    # plugin signature checks) outlasts 60s + 3 x 30s and swarm kills the
    # task as unhealthy before it ever listens.
    assert _services()["grafana"]["healthcheck"]["start_period"] == "60s"
    assert _services(GRAFANA_HEALTH_START_PERIOD="10m")["grafana"]["healthcheck"]["start_period"] == "10m"
