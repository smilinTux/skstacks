"""skhub Talk HPB TURN for callers outside the LAN.

talk-hpb (aio-talk) runs eturnal on 3478. Published in Swarm INGRESS mode,
eturnal sees the ingress SNAT address, so a STUN binding answers an internal
10.0.0.x address and a TURN client's permission for its own public address is
refused (private peer). eturnal also advertises the container IP as its relay
address, and the relay ports are not published, so a relayed candidate is
unreachable from the internet. Talk only works today because Janus (the only
peer it relays to) sits in the same container.

New optional knobs, all defaulting to the previous render:

- skhub.TALK_TURN_PUBLISH_MODE: ingress (default) | host. host publishes 3478
  tcp+udp in host mode so eturnal sees real client addresses.
- skhub.TALK_TURN_RELAY_MIN_PORT / TALK_TURN_RELAY_MAX_PORT: a bounded relay
  range (at most 200 ports), passed to eturnal as ETURNAL_RELAY_MIN_PORT /
  ETURNAL_RELAY_MAX_PORT (its documented fallback when eturnal.yml has no
  range) and published udp in host mode.
- skhub.TALK_TURN_RELAY_IPV4: the address eturnal advertises for relays (the
  public address the router forwards). aio-talk's start.sh always writes
  relay_ipv4_addr = the container IP, so a small command wrapper rewrites
  that one line after start.sh, then execs supervisord as before.
- skhub.TALK_HPB_PLACEMENT_CONSTRAINTS: talk-hpb's own placement (host-mode
  ports are only open on the node that runs the task, so it must be pinned),
  defaulting to skhub.placement_constraints.

The deploy playbooks assert the combination is coherent before rendering.
"""
import os
import pathlib
import re
import subprocess

import jinja2
import pytest
import yaml

SKHUB_DIR = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
SKHUB = SKHUB_DIR / "src/config/skhub"
COMPOSE = SKHUB / "skhub.yml.j2"
TALK_ENV = SKHUB / "talk-hpb.env.j2"
PLAYBOOKS = sorted(SKHUB_DIR.glob("deploy_skhub-*.yml"))
BASE_VARS = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "enable_talk_hpb": True}
HOST_SET = {
    "TALK_TURN_PUBLISH_MODE": "host",
    "TALK_TURN_RELAY_MIN_PORT": 62000,
    "TALK_TURN_RELAY_MAX_PORT": 62099,
    "TALK_TURN_RELAY_IPV4": "203.0.113.10",
    "TALK_HPB_PLACEMENT_CONSTRAINTS": ["node.hostname == worker3"],
}

# What aio-talk's /start.sh writes (secret redacted to a fake value).
ETURNAL_YML = """eturnal:
  listen:
    - ip: "::"
      port: 3478
      transport: udp
    - ip: "::"
      port: 3478
      transport: tcp
  log_dir: stdout
  log_level: warning
  secret: "fake-secret-for-test"
  relay_ipv4_addr: "172.16.200.9"
  blacklist_peers:
  - recommended
  whitelist_peers:
  - 127.0.0.1
  - ::1
  - "172.16.200.9"
"""


def _env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)


def render(tpl, **overrides):
    return _env().from_string(tpl.read_text()).render(
        app="skhub", env="prod", skhub=dict(BASE_VARS, **overrides), fence_service_name="skfenceha")


def services(**overrides):
    return yaml.safe_load(render(COMPOSE, **overrides))["services"]


def talk(**overrides):
    return services(**overrides)["talk-hpb"]


def env_lines(**overrides):
    return [l for l in render(TALK_ENV, **overrides).splitlines() if l and not l.startswith("#")]


# --- defaults: unchanged ----------------------------------------------------

def test_default_ports_are_ingress_short_syntax():
    assert talk()["ports"] == ["3478:3478/tcp", "3478:3478/udp"]


def test_default_has_no_command_override():
    t = talk()
    assert "command" not in t and "entrypoint" not in t


def test_default_placement_follows_skhub_placement():
    assert talk()["deploy"]["placement"]["constraints"] == []
    pc = ["node.role==worker"]
    assert talk(placement_constraints=pc)["deploy"]["placement"]["constraints"] == pc


def test_default_env_has_no_relay_range():
    assert not [l for l in env_lines() if l.startswith("ETURNAL_")]


def test_explicit_ingress_mode_renders_like_default():
    assert render(COMPOSE, TALK_TURN_PUBLISH_MODE="ingress") == render(COMPOSE)


# --- host mode --------------------------------------------------------------

def test_host_mode_publishes_3478_tcp_and_udp_in_host_mode():
    ports = talk(TALK_TURN_PUBLISH_MODE="host", TALK_HPB_PLACEMENT_CONSTRAINTS=["node.hostname == w3"])["ports"]
    assert {"target": 3478, "published": 3478, "protocol": "tcp", "mode": "host"} in ports
    assert {"target": 3478, "published": 3478, "protocol": "udp", "mode": "host"} in ports
    assert len(ports) == 2


def test_relay_range_published_udp_host_mode_and_passed_to_eturnal():
    ports = talk(**HOST_SET)["ports"]
    relay = [p for p in ports if p["target"] != 3478]
    assert len(relay) == 100
    assert {p["protocol"] for p in relay} == {"udp"} and {p["mode"] for p in relay} == {"host"}
    assert sorted(p["target"] for p in relay) == list(range(62000, 62100))
    assert all(p["published"] == p["target"] for p in relay)
    lines = env_lines(**HOST_SET)
    assert "ETURNAL_RELAY_MIN_PORT=62000" in lines
    assert "ETURNAL_RELAY_MAX_PORT=62099" in lines


def test_talk_placement_override_only_touches_talk_hpb():
    svcs = services(placement_constraints=["node.role==worker"], **HOST_SET)
    assert svcs["talk-hpb"]["deploy"]["placement"]["constraints"] == ["node.hostname == worker3"]
    for name, svc in svcs.items():
        if name != "talk-hpb" and "placement" in svc.get("deploy", {}):
            assert svc["deploy"]["placement"]["constraints"] == ["node.role==worker"], name


def test_host_mode_leaves_other_services_unchanged():
    base, host = services(), services(**HOST_SET)
    assert set(base) == set(host)
    for name in base:
        if name != "talk-hpb":
            assert base[name] == host[name], name


# --- relay address wrapper --------------------------------------------------

def _wrapper(**overrides):
    cmd = talk(**overrides)["command"]
    assert cmd[:2] == ["/bin/bash", "-c"]
    return cmd


def test_relay_ipv4_wrapper_escapes_dollars_for_stack_deploy():
    script = _wrapper(**HOST_SET)[2]
    # docker stack deploy interpolates $VAR / ${VAR}; every literal $ must be $$.
    assert not re.search(r"(?<!\$)\$(?!\$)", script.replace("$$", ""))
    assert "$$@" in script


def test_relay_ipv4_wrapper_execs_the_original_cmd():
    # v2.26.3: the default image is the dinit-based aio-talk, so the wrapper
    # execs its own CMD; a pinned TALK_HPB_IMAGE keeps supervisord.
    assert _wrapper(**HOST_SET)[4:] == ["dinit", "--system", "--container", "nats-server", "eturnal", "janus", "signaling"]
    assert _wrapper(TALK_HPB_IMAGE="ghcr.io/nextcloud-releases/aio-talk:20260122_105751", **HOST_SET)[4:] == ["supervisord", "-c", "/supervisord.conf"]


def _run_wrapper(tmp_path, eturnal_text, **overrides):
    cmd = _wrapper(**overrides)
    conf = tmp_path / "eturnal.yml"
    conf.write_text(eturnal_text)
    # What the container runs after docker's $$ -> $ interpolation, pointed at a
    # scratch copy of /conf/eturnal.yml and a stub instead of supervisord.
    script = cmd[2].replace("$$", "$").replace("/conf/eturnal.yml", str(conf))
    out = subprocess.run(["bash", "-c", script, cmd[3], "echo", "EXEC-OK"],
                         capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    return out, conf.read_text()


def test_relay_ipv4_wrapper_rewrites_only_relay_addr(tmp_path):
    out, text = _run_wrapper(tmp_path, ETURNAL_YML, **HOST_SET)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "EXEC-OK"
    assert '  relay_ipv4_addr: "203.0.113.10"' in text.splitlines()
    # the container IP stays whitelisted (Janus in the same container is a peer)
    assert text.replace('relay_ipv4_addr: "203.0.113.10"', 'relay_ipv4_addr: "172.16.200.9"') == ETURNAL_YML


def test_relay_ipv4_wrapper_fails_loudly_when_line_missing(tmp_path):
    changed = ETURNAL_YML.replace('  relay_ipv4_addr: "172.16.200.9"\n', "")
    out, text = _run_wrapper(tmp_path, changed, **HOST_SET)
    assert out.returncode != 0
    assert "EXEC-OK" not in out.stdout
    assert "relay_ipv4_addr" in out.stderr


def test_no_wrapper_without_relay_ipv4():
    over = dict(HOST_SET)
    del over["TALK_TURN_RELAY_IPV4"]
    assert "command" not in talk(**over)


# --- playbook guard ---------------------------------------------------------

def _assert_task(pb):
    plays = yaml.safe_load(pb.read_text())
    for play in plays:
        for section in ("pre_tasks", "tasks"):
            for task in play.get(section) or []:
                if "assert" in task and "TALK_TURN_PUBLISH_MODE" in yaml.safe_dump(task):
                    return task
    raise AssertionError(f"{pb.name}: no assert task validating the Talk TURN knobs")


def _guard_passes(task, skhub_vars):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    ctx = {"skhub": skhub_vars}
    for name, expr in (task.get("vars") or {}).items():
        inner = re.fullmatch(r"\s*\{\{(.*)\}\}\s*", str(expr), re.S)
        ctx[name] = env.compile_expression(inner.group(1))(**ctx) if inner else expr
    return all(env.compile_expression(cond)(**ctx) for cond in task["assert"]["that"])


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_guard_present_in_every_env_playbook(pb):
    assert len(PLAYBOOKS) == 3
    _assert_task(pb)


GOOD = [
    {},
    {"TALK_TURN_PUBLISH_MODE": "ingress"},
    {"TALK_TURN_PUBLISH_MODE": "host", "TALK_HPB_PLACEMENT_CONSTRAINTS": ["node.hostname == w3"]},
    HOST_SET,
    dict(HOST_SET, TALK_TURN_RELAY_MIN_PORT="62000", TALK_TURN_RELAY_MAX_PORT="62199"),
]
BAD = {
    "unknown mode": {"TALK_TURN_PUBLISH_MODE": "global"},
    "host mode, no pin": {"TALK_TURN_PUBLISH_MODE": "host"},
    "range in ingress mode": {"TALK_TURN_RELAY_MIN_PORT": 62000, "TALK_TURN_RELAY_MAX_PORT": 62099},
    "only min": dict({k: v for k, v in HOST_SET.items() if k != "TALK_TURN_RELAY_MAX_PORT"}),
    "min > max": dict(HOST_SET, TALK_TURN_RELAY_MIN_PORT=62100, TALK_TURN_RELAY_MAX_PORT=62000),
    "range over 200": dict(HOST_SET, TALK_TURN_RELAY_MAX_PORT=62200),
    "privileged port": dict(HOST_SET, TALK_TURN_RELAY_MIN_PORT=1000, TALK_TURN_RELAY_MAX_PORT=1099),
    "relay ip malformed": dict(HOST_SET, TALK_TURN_RELAY_IPV4="203.0.113.10; rm -rf /"),
    "relay ip without range": {k: v for k, v in HOST_SET.items() if "PORT" not in k},
    "relay ip in ingress mode": {"TALK_TURN_RELAY_IPV4": "203.0.113.10"},
}


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("over", GOOD, ids=range(len(GOOD)))
def test_guard_accepts_coherent_settings(pb, over):
    assert _guard_passes(_assert_task(pb), dict(BASE_VARS, **over))


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("case", sorted(BAD))
def test_guard_rejects_incoherent_settings(pb, case):
    assert not _guard_passes(_assert_task(pb), dict(BASE_VARS, **BAD[case]))


# --- secrets ----------------------------------------------------------------

@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_talk_env_file_is_not_world_readable(pb):
    # talk-hpb.env carries TURN_SECRET, SIGNALING_SECRET and INTERNAL_SECRET.
    text = pb.read_text()
    m = re.search(r'src: "talk-hpb\.env\.j2".*?mode: "(0[0-7]{3})"', text)
    assert m, pb.name
    assert int(m.group(1), 8) & 0o007 == 0, m.group(1)


# --- TALK_HPB_CMD: the command the relay wrapper execs -----------------------
# aio-talk moved from supervisord to dinit (seen on the 831a08f digest, signaling
# 2.1.1): with TALK_TURN_RELAY_IPV4 set the wrapper still exec'd
# `supervisord -c /supervisord.conf`, which is not in the newer image, and
# talk-hpb crash-looped with exit 127 on the first framework deploy that used
# TALK_HPB_IMAGE + the relay knobs together (2026-10-02, rolled back).

DINIT = ["dinit", "--system", "--container", "nats-server", "eturnal", "janus", "signaling"]


def test_talk_hpb_cmd_replaces_the_execd_command():
    cmd = _wrapper(**HOST_SET, TALK_HPB_CMD=DINIT)
    assert cmd[4:] == DINIT


def test_talk_hpb_cmd_unset_keeps_supervisord_for_a_pinned_image():
    assert _wrapper(TALK_HPB_IMAGE="ghcr.io/nextcloud-releases/aio-talk:20260122_105751", **HOST_SET)[4:] == ["supervisord", "-c", "/supervisord.conf"]


def test_talk_hpb_cmd_unset_uses_dinit_for_the_default_image():
    assert _wrapper(**HOST_SET)[4:] == ["dinit", "--system", "--container", "nats-server", "eturnal", "janus", "signaling"]


def test_wrapper_fails_loudly_when_the_command_is_not_in_the_image(tmp_path):
    cmd = _wrapper(**HOST_SET)
    conf = tmp_path / "eturnal.yml"
    conf.write_text(ETURNAL_YML)
    script = cmd[2].replace("$$", "$").replace("/conf/eturnal.yml", str(conf))
    out = subprocess.run(["bash", "-c", script, cmd[3], "no-such-supervisor-binary", "-c", "x"],
                         capture_output=True, text=True, env={"PATH": os.environ["PATH"]})
    assert out.returncode != 0
    assert "TALK_HPB_CMD" in out.stderr
