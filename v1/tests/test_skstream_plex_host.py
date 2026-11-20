"""skstream PLEX_MODE host: Plex as a host-level unit on the media node.

A swarm service cannot get a device, so swarm Plex cannot open /dev/dri for
hardware transcoding. PLEX_MODE host moves Plex out of the stack into
skstream-plex.service (same pattern as skstream-dumb.service) and routes it
through a Traefik file-provider route. These tests hold that contract:
  * default stays swarm: the stack and deploy script are unchanged
  * host mode: no swarm Plex, the unit keeps the "plex" overlay alias, the
    same binds (local SQLite dir, rslave debrid mount, read-only extras),
    publishes 32400 on the LAN bind only, and never starts beside a swarm task
  * /dev/dri and the render group only with PLEX_HW_TRANSCODE
  * the file route mirrors the swarm labels (host, TLS options, middlewares)
  * the play validates the mode and orders stop/remove/start safely
"""
import pathlib

import jinja2
import pytest
import yaml

from test_skstream_app import APP, SRC, _env, _vars, render, services, tasks

UNIT = SRC / "skstream-plex.service.j2"
ROUTE = SRC / "skstream-traefik.yml.j2"
DEPLOY = APP / "src/skstream/deploy.j2"
HOST = {"PLEX_MODE": "host", "PLEX_LAN_BIND_ADDRESS": "192.0.2.10"}


def _deploy(**over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    return env.from_string(DEPLOY.read_text()).render(**_vars(**over))


def unit(env="prod", extra=None, **over):
    v = _vars(env, **{**HOST, **over})
    v.update(extra or {})
    return _env().from_string(UNIT.read_text()).render(**v)


def exec_start(text):
    lines = text.splitlines()
    i = next(n for n, l in enumerate(lines) if l.startswith("ExecStart="))
    out = []
    for l in lines[i:]:
        out.append(l.rstrip(" \\"))
        if not l.endswith("\\"):
            break
    return " ".join(out)


# ---- stack --------------------------------------------------------------------

def test_default_mode_keeps_plex_in_the_stack():
    assert "plex" in services()


@pytest.mark.parametrize("mode", ["host", "HOST"])
def test_host_mode_drops_plex_from_the_stack(mode):
    svcs = services(PLEX_MODE=mode, KOMETA_ENABLED=True)
    assert "plex" not in svcs and "kometa" in svcs


def test_host_mode_without_kometa_renders_an_empty_services_map():
    assert services(PLEX_MODE="host") == {}


def test_kometa_still_calls_plex_by_its_overlay_name():
    env = services(PLEX_MODE="host", KOMETA_ENABLED=True)["kometa"]["environment"]
    assert "KOMETA_PLEX_URL=http://plex:32400" in env


# ---- deploy script ----------------------------------------------------------------

def test_deploy_script_default_is_unchanged_swarm_flow():
    s = _deploy()
    assert "docker stack deploy" in s
    assert "preexisted ${STACK_NAME}_plex &&" in s
    assert "Checking service health" in s
    assert "PLEX_MODE host" not in s


def test_deploy_script_host_mode_removes_swarm_plex_and_skips_its_health_check():
    s = _deploy(PLEX_MODE="host", KOMETA_ENABLED=True)
    assert "docker stack deploy" in s and "--prune" in s
    assert 'docker service rm "${STACK_NAME}_plex"' in s
    assert "preexisted ${STACK_NAME}_plex &&" not in s
    assert "Checking service health" not in s


def test_deploy_script_host_mode_without_kometa_has_no_empty_stack_deploy():
    s = _deploy(PLEX_MODE="host")
    assert "docker stack deploy" not in s
    assert 'docker service rm "${STACK_NAME}_plex"' in s


# ---- unit ---------------------------------------------------------------------

def test_unit_keeps_the_plex_overlay_alias_and_lan_only_port():
    cmd = exec_start(unit())
    assert "--network skstream-prod --network-alias plex" in cmd
    assert "-p 192.0.2.10:32400:32400" in cmd
    assert " -p 32400" not in cmd and "0.0.0.0" not in cmd


def test_unit_keeps_the_swarm_binds():
    cmd = exec_start(unit(PLEX_EXTRA_BINDS=[{"source": "/var/data/skfetch-prod/media", "target": "/skfetch/media"}]))
    assert "-v /var/data/skstream-prod/plex/config:/config" in cmd
    assert '"--volume=/var/lib/skstream/plex-db:/config/Library/Application Support/Plex Media Server/Plug-in Support/Databases"' in cmd
    assert "-v /var/data/skstream-prod/zurg/mnt:/mnt/debrid:ro,rslave" in cmd
    assert "-v /var/data/skfetch-prod/media:/skfetch/media:ro" in cmd
    assert "--env-file /var/data/config/skstream-prod/plex.env" in cmd


def test_unit_transcode_dir_defaults_to_the_swarm_path_and_is_a_knob():
    assert "-v /var/data/skstream-prod/plex/transcode:/transcode" in exec_start(unit())
    cmd = exec_start(unit(PLEX_TRANSCODE_DIR="/var/lib/skstream/plex-transcode"))
    assert "-v /var/lib/skstream/plex-transcode:/transcode" in cmd


def test_unit_image_matches_the_stack_default():
    stack_image = services()["plex"]["image"]
    assert exec_start(unit()).endswith(stack_image)
    assert "@sha256:" in stack_image


def test_unit_limits_follow_the_plex_knobs():
    cmd = exec_start(unit(PLEX_RESOURCES_LIMITS_CPUS="4.00", PLEX_RESOURCES_LIMITS_MEMORY="3G"))
    assert "--cpus 4.00" in cmd and "--memory 3G" in cmd


def test_unit_gets_no_gpu_unless_asked():
    cmd = exec_start(unit())
    assert "/dev/dri" not in cmd and "--group-add" not in cmd


def test_unit_gets_dri_and_render_group_with_hw_transcode():
    cmd = exec_start(unit(PLEX_HW_TRANSCODE=True, extra={"skstream_render_gid": 109}))
    assert "--device /dev/dri:/dev/dri" in cmd
    assert "--group-add 109" in cmd


def test_unit_render_gid_knob_overrides_the_detected_one():
    cmd = exec_start(unit(PLEX_HW_TRANSCODE=True, PLEX_RENDER_GID=44, extra={"skstream_render_gid": 109}))
    assert "--group-add 44" in cmd and "--group-add 109" not in cmd


def test_unit_refuses_to_start_beside_a_swarm_plex_task():
    text = unit()
    guard = next(l for l in text.splitlines() if "com.docker.swarm.service.name=skstream-prod_plex" in l)
    assert guard.startswith("ExecStartPre=/bin/sh -c")
    assert "$$(seq" in guard and "$(seq" not in guard.replace("$$(seq", "")
    assert "exit 1" in guard


def test_unit_waits_for_swarm_net_and_dumb_and_never_gives_up():
    text = unit()
    assert "ExecStartPre=/usr/local/sbin/sk-wait-swarm-net skstream-prod" in text
    assert "After=docker.service network-online.target remote-fs.target skstream-dumb.service" in text
    assert "StartLimitIntervalSec=0" in text and "Restart=always" in text
    assert "WantedBy=multi-user.target docker.service" in text


def test_unit_is_env_scoped_outside_prod():
    text = unit("dev")
    assert "--name skstream-plex-dev" in text
    assert "--network skstream-dev --network-alias plex" in exec_start(text)
    assert "skstream-dumb-dev.service" in text


def test_unit_holds_no_secrets():
    text = unit(PLEX_CLAIM="claim-secret-example")
    for secret in ("claim-secret-example", "plextokenexample0000", "RDKEYexample"):
        assert secret not in text


# ---- Traefik file route ---------------------------------------------------------

def route(env="prod", **over):
    v = _vars(env, **{**HOST, **over})
    return yaml.safe_load(_env().from_string(ROUTE.read_text()).render(**v))["http"]


def test_route_mirrors_the_swarm_router():
    r = route(tls_options="intermediate@file", router_middlewares=["default-no-crowdsec@file"])
    router = r["routers"]["skstream"]
    assert router["rule"] == "Host(`skstream.demo.example.test`)"
    assert router["entryPoints"] == ["websecure"]
    assert router["tls"] == {"certResolver": "main", "options": "intermediate@file"}
    assert router["middlewares"] == ["default-no-crowdsec@file"]
    assert r["services"]["skstream"]["loadBalancer"]["servers"] == [{"url": "http://192.0.2.10:32400"}]


def test_route_without_middlewares_or_tls_options():
    router = route()["routers"]["skstream"]
    assert "middlewares" not in router and router["tls"] == {"certResolver": "main"}


def test_route_is_env_scoped():
    r = route("staging")
    assert "skstream-staging" in r["routers"] and "skstream-staging" in r["services"]


# ---- playbooks --------------------------------------------------------------------

def _named(env, prefix):
    return [t for t in tasks(env) if t.get("name", "").startswith(prefix)]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_play_validates_the_plex_mode(env):
    (t,) = _named(env, "Validate the Plex mode")
    that = " ".join(t["assert"]["that"])
    assert "['swarm', 'host']" in that and "PLEX_HW_TRANSCODE" in that and "PLEX_LAN_BIND_ADDRESS" in that


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_play_orders_stop_deploy_start(env):
    names = [t.get("name", "") for t in tasks(env)]
    stop = names.index("Stop host-level Plex before the stack deploy when PLEX_MODE is swarm")
    install = names.index("Install skstream-plex.service on the media node (PLEX_MODE host)")
    deploy = names.index("Deploy {{ app }} stack")
    start = next(i for i, n in enumerate(names) if n.startswith("Enable and start skstream-plex.service"))
    assert stop < deploy and install < deploy < start


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_play_host_tasks_run_on_the_media_node(env):
    for prefix in ("Stop host-level Plex", "Install skstream-plex.service", "Enable and start skstream-plex.service",
                   "Find the render group"):
        (t,) = _named(env, prefix)
        assert t.get("delegate_to") == "{{ skstream.MEDIA_NODE }}", prefix


def test_playbooks_agree_on_the_new_tasks():
    def body(env):
        keep = ("Validate the Plex mode", "Validate skstream.PLEX_EXTRA_BINDS", "Stop host-level Plex",
                "Remove the Plex Traefik", "Find the render group", "Warn when hardware", "Install skstream-plex",
                "Install the Plex Traefik", "Enable and start skstream-plex")
        return [t for t in tasks(env) if t.get("name", "").startswith(keep)]
    assert body("dev") == body("staging") == body("prod")
    assert len(body("prod")) == 9
