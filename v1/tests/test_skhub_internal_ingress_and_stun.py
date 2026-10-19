"""skhub INTERNAL_INGRESS_IP (internal routing to the public hostname) and
TALK_STUN_SERVERS (Talk's STUN list).

INTERNAL_INGRESS_IP: on an instance published through a CDN or tunnel
(Cloudflare + cloudflared), the public skhub hostname resolves to the CDN, so
every call the cluster makes to ITSELF by that name (talk-hpb's backend
requests and room pings to Nextcloud, Nextcloud's requests to the HPB under
/standalone-signaling, the recording server's callbacks) leaves the cluster
and loops back through the tunnel. Seen on a production instance: "connection
reset by peer" / "incomplete OCS response" ping failures through the single
tunnel replica. The knob pins the public name(s) to the cluster's own ingress
(an address the containers reach Traefik on, ideally a keepalived VIP that
follows a healthy Traefik) with extra_hosts, in nextcloud, cron, talk-hpb and
talk-recording only. URLs, TLS names and certificates are unchanged.

TALK_STUN_SERVERS: Talk's stun_servers list (host:port strings). Unset, the
post-deploy task never touches stun_servers, as before.

Defaults (unset or empty) render byte-identical to before.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess

import jinja2
import pytest
import yaml

SKHUB_DIR = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB_DIR / "src/config/skhub/skhub.yml.j2"
PLAYBOOKS = sorted(SKHUB_DIR.glob("deploy_skhub-*.yml"))
README = SKHUB_DIR / "README.md"
BASE = {
    "CLUSTERNAME": "cluster1",
    "DOMAIN": "example.com",
    "enable_talk_hpb": True,
    "TALK_RECORDING_ENABLED": True,
    "TALK_RECORDING_NODE": "worker2",
    "turn_secret": "t" * 32,
    "signaling_secret": "s" * 32,
}
PINNED = {"nextcloud", "cron", "webhook-worker", "talk-hpb", "talk-recording"}
IP = "10.20.30.40"


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["to_json"] = json.dumps
    env.tests["match"] = lambda value, pattern: re.match(pattern, str(value)) is not None
    return env


def render(env_name="prod", **over):
    return _env().from_string(COMPOSE.read_text()).render(app="skhub", env=env_name, skhub=dict(BASE, **over))


def services(env_name="prod", **over):
    return yaml.safe_load(render(env_name, **over))["services"]


# --- INTERNAL_INGRESS_IP: render ---------------------------------------------

@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
@pytest.mark.parametrize("empty", [None, ""])
def test_unset_or_empty_renders_byte_identical(env_name, empty):
    over = {} if empty is None else {"INTERNAL_INGRESS_IP": empty}
    assert render(env_name, **over) == render(env_name)
    assert "extra_hosts" not in render(env_name)


@pytest.mark.parametrize("env_name,host", [("prod", "skhub.cluster1.example.com"),
                                           ("dev", "skhub-dev.cluster1.example.com"),
                                           ("staging", "skhub-staging.cluster1.example.com")])
def test_pins_the_public_host_in_the_four_callers(env_name, host):
    svcs = services(env_name, INTERNAL_INGRESS_IP=IP)
    for name in PINNED:
        assert svcs[name]["extra_hosts"] == [f"{host}:{IP}"], name


def test_only_the_four_callers_change():
    base, pinned = services(), services(INTERNAL_INGRESS_IP=IP)
    assert set(base) == set(pinned)
    for name in base:
        if name in PINNED:
            assert {k: v for k, v in pinned[name].items() if k != "extra_hosts"} == base[name], name
        else:
            assert pinned[name] == base[name], name


def test_cloudflared_instance_pins_the_bare_domain_host():
    svcs = services(CLOUDFLARED=True, DOMAIN="example.org", INTERNAL_INGRESS_IP=IP)
    assert svcs["talk-hpb"]["extra_hosts"] == [f"skhub.example.org:{IP}"]


def test_custom_hostname_pins_both_router_host_and_talk_nc_domain():
    # The routers answer on SKHUB_HOSTNAME; talk-hpb.env still builds NC_DOMAIN
    # (the HPB's backend URL) from the base domain. Both must stay internal.
    svcs = services(SKHUB_HOSTNAME="cloud.example.org", INTERNAL_INGRESS_IP=IP)
    assert svcs["nextcloud"]["extra_hosts"] == [f"cloud.example.org:{IP}", f"skhub.cluster1.example.com:{IP}"]


def test_hostname_equal_to_default_is_not_duplicated():
    svcs = services(SKHUB_HOSTNAME="skhub.cluster1.example.com", INTERNAL_INGRESS_IP=IP)
    assert svcs["talk-hpb"]["extra_hosts"] == [f"skhub.cluster1.example.com:{IP}"]


def test_works_with_talk_and_recording_off():
    svcs = services(enable_talk_hpb=False, TALK_RECORDING_ENABLED=False, INTERNAL_INGRESS_IP=IP)
    assert "talk-hpb" not in svcs and "talk-recording" not in svcs
    assert svcs["nextcloud"]["extra_hosts"] == svcs["cron"]["extra_hosts"] == [f"skhub.cluster1.example.com:{IP}"]


def test_works_with_the_turn_host_mode_wrapper():
    t = services(INTERNAL_INGRESS_IP=IP, TALK_TURN_PUBLISH_MODE="host", TALK_TURN_RELAY_MIN_PORT=62000,
                 TALK_TURN_RELAY_MAX_PORT=62009, TALK_TURN_RELAY_IPV4="203.0.113.10",
                 TALK_HPB_PLACEMENT_CONSTRAINTS=["node.hostname == w3"])["talk-hpb"]
    assert t["extra_hosts"] == [f"skhub.cluster1.example.com:{IP}"]
    assert t["command"][:2] == ["/bin/bash", "-c"]


# --- the playbook guard -------------------------------------------------------

def _guard(pb):
    for play in yaml.safe_load(pb.read_text()):
        for task in play.get("pre_tasks") or []:
            if "assert" in task and "INTERNAL_INGRESS_IP" in yaml.safe_dump(task):
                return task
    raise AssertionError(f"{pb.name}: no pre_tasks assert validating INTERNAL_INGRESS_IP / TALK_STUN_SERVERS")


def _guard_passes(task, skhub_vars):
    env = _env()
    env.filters["bool"] = bool
    ctx = {"skhub": skhub_vars}
    for name, expr in (task.get("vars") or {}).items():
        inner = re.fullmatch(r"\s*\{\{(.*)\}\}\s*", str(expr), re.S)
        ctx[name] = env.compile_expression(inner.group(1))(**ctx) if inner else expr
    return all(env.compile_expression(cond)(**ctx) for cond in task["assert"]["that"])


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_guard_present_in_every_env_playbook_before_tasks(pb):
    assert len(PLAYBOOKS) == 3
    _guard(pb)


GOOD = [
    {},
    {"INTERNAL_INGRESS_IP": ""},
    {"INTERNAL_INGRESS_IP": "10.20.30.40"},
    {"INTERNAL_INGRESS_IP": "172.16.5.10"},
    {"TALK_STUN_SERVERS": []},
    {"TALK_STUN_SERVERS": ["turn.example.com:3478"]},
    {"TALK_STUN_SERVERS": ["turn.example.com:3478", "stun.nextcloud.com:443"]},
    {"INTERNAL_INGRESS_IP": "10.20.30.40", "TALK_STUN_SERVERS": ["turn.example.com:3478"]},
    {"TALK_TURN_TLS_443": False},
    {"TALK_TURN_TLS_443": True, "TURN_DOMAIN": "turn.example.com"},
    {"TALK_TURN_TLS_443": "false"},
]
BAD = {
    "ip not an address": {"INTERNAL_INGRESS_IP": "traefik"},
    "ip with port": {"INTERNAL_INGRESS_IP": "10.20.30.40:443"},
    "ip octet out of range": {"INTERNAL_INGRESS_IP": "10.20.30.256"},
    "ip loopback": {"INTERNAL_INGRESS_IP": "127.0.0.1"},
    "ip zero": {"INTERNAL_INGRESS_IP": "0.0.0.0"},
    "ip broadcast": {"INTERNAL_INGRESS_IP": "255.255.255.255"},
    "ip injection": {"INTERNAL_INGRESS_IP": "10.20.30.40\n    privileged: true"},
    "ip trailing newline": {"INTERNAL_INGRESS_IP": "10.20.30.40\n"},
    "ip list": {"INTERNAL_INGRESS_IP": ["10.20.30.40", "10.20.30.41"]},
    "ip v6": {"INTERNAL_INGRESS_IP": "fd00::70"},
    "stun string not list": {"TALK_STUN_SERVERS": "turn.example.com:3478"},
    "stun mapping": {"TALK_STUN_SERVERS": {"server": "turn.example.com:3478"}},
    "stun no port": {"TALK_STUN_SERVERS": ["turn.example.com"]},
    "stun port 0": {"TALK_STUN_SERVERS": ["turn.example.com:0"]},
    "stun port too big": {"TALK_STUN_SERVERS": ["turn.example.com:65536"]},
    "stun scheme": {"TALK_STUN_SERVERS": ["stun:turn.example.com:3478"]},
    "stun quote injection": {"TALK_STUN_SERVERS": ["x.example.com:3478'; rm -rf /; echo '"]},
    "stun trailing newline": {"TALK_STUN_SERVERS": ["turn.example.com:3478\n"]},
    "stun space": {"TALK_STUN_SERVERS": ["turn.example.com :3478"]},
    "stun too many": {"TALK_STUN_SERVERS": [f"s{i}.example.com:3478" for i in range(11)]},
    "stun without talk": {"enable_talk_hpb": False, "TALK_STUN_SERVERS": ["turn.example.com:3478"]},
    "tls443 without TURN_DOMAIN": {"TALK_TURN_TLS_443": True},
    "tls443 bare TURN_DOMAIN": {"TALK_TURN_TLS_443": True, "TURN_DOMAIN": "turn"},
    "tls443 TURN_DOMAIN with port": {"TALK_TURN_TLS_443": True, "TURN_DOMAIN": "turn.example.com:3478"},
    "tls443 TURN_DOMAIN rule injection": {"TALK_TURN_TLS_443": True, "TURN_DOMAIN": "turn.example.com`) || HostSNI(`*"},
    "tls443 without talk": {"TALK_TURN_TLS_443": True, "TURN_DOMAIN": "turn.example.com", "enable_talk_hpb": False},
}


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("over", GOOD, ids=range(len(GOOD)))
def test_guard_accepts_valid_settings(pb, over):
    assert _guard_passes(_guard(pb), dict(BASE, **over))


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("case", sorted(BAD))
def test_guard_refuses_bad_settings(pb, case):
    assert not _guard_passes(_guard(pb), dict(BASE, **BAD[case]))


# --- TALK_TURN_TLS_443: Traefik TCP router + a turns:443 entry ------------------

TLS = {"TALK_TURN_TLS_443": True, "TURN_DOMAIN": "turn.example.com"}


def _labels(svc):
    return svc["deploy"]["labels"]


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_tls443_false_renders_byte_identical(env_name):
    assert render(env_name, TALK_TURN_TLS_443=False) == render(env_name)
    assert render(env_name, TALK_TURN_TLS_443="false") == render(env_name)
    assert "traefik.tcp." not in render(env_name)


def test_tls443_adds_a_tcp_sni_router_to_eturnal():
    labels = _labels(services(**TLS)["talk-hpb"])
    tcp = [l for l in labels if l.startswith("traefik.tcp.")]
    assert "traefik.tcp.routers.talk-turns.rule=HostSNI(`turn.example.com`)" in tcp
    assert "traefik.tcp.routers.talk-turns.entrypoints=websecure" in tcp
    assert "traefik.tcp.routers.talk-turns.tls=true" in tcp
    assert "traefik.tcp.routers.talk-turns.tls.certresolver=main" in tcp
    assert "traefik.tcp.services.talk-turns-svc.loadbalancer.server.port=3478" in tcp
    assert len(tcp) == 6


def test_tls443_changes_only_talk_hpb_labels():
    base, on = services(), services(**TLS)
    for name in base:
        if name == "talk-hpb":
            b, o = dict(base[name]), dict(on[name])
            bl, ol = b["deploy"].pop("labels"), o["deploy"].pop("labels")
            assert b == o
            assert [l for l in ol if not l.startswith("traefik.tcp.")] == bl
        else:
            assert on[name] == base[name], name


# --- TALK_STUN_SERVERS: the post-deploy Talk task ------------------------------

def _talk_task(pb):
    for play in yaml.safe_load(pb.read_text()):
        for task in play.get("tasks") or []:
            if task.get("name") == "Configure Talk signaling and TURN servers in Nextcloud":
                return task
    raise AssertionError(f"{pb.name}: no Talk signaling/TURN task")


def _talk_shell(pb, **over):
    return _env().from_string(_talk_task(pb)["shell"]).render(
        skhub=dict(BASE, **over), skhub_public_host="skhub.cluster1.example.com",
        nextcloud_container={"stdout": "cid"})


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("empty", [None, []])
def test_stun_unset_or_empty_leaves_the_task_unchanged(pb, empty):
    over = {} if empty is None else {"TALK_STUN_SERVERS": empty}
    shell = _talk_shell(pb, **over)
    assert shell == _talk_shell(pb)
    assert "stun_servers" not in shell
    assert len(shell.strip().splitlines()) == 3


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_stun_set_writes_stun_servers_as_a_json_list(pb):
    shell = _talk_shell(pb, TALK_STUN_SERVERS=["turn.example.com:3478", "stun.nextcloud.com:443"])
    line = next(l for l in shell.splitlines() if "stun_servers" in l)
    m = re.search(r"--value='([^']*)'", line)
    assert m, line
    assert json.loads(m.group(1)) == ["turn.example.com:3478", "stun.nextcloud.com:443"]
    assert line.startswith('docker exec "$CONTAINER_ID" php occ config:app:set spreed stun_servers ')


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_stun_does_not_touch_signaling_or_turn_lines(pb):
    base = _talk_shell(pb).splitlines()
    with_stun = [l for l in _talk_shell(pb, TALK_STUN_SERVERS=["turn.example.com:3478"]).splitlines()
                 if "stun_servers" not in l]
    assert with_stun == base


def _turn_servers(shell):
    line = next(l for l in shell.splitlines() if "spreed turn_servers" in l)
    return json.loads(re.search(r"--value='([^']*)'", line).group(1))


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_tls443_off_keeps_the_single_turn_entry(pb):
    assert _talk_shell(pb, TALK_TURN_TLS_443=False) == _talk_shell(pb)
    assert len(_turn_servers(_talk_shell(pb))) == 1


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_tls443_appends_a_turns_443_tcp_entry(pb):
    ts = _turn_servers(_talk_shell(pb, **TLS))
    assert ts[0] == _turn_servers(_talk_shell(pb, TURN_DOMAIN="turn.example.com"))[0]
    assert ts[1] == {"schemes": "turns", "server": "turn.example.com:443", "secret": "t" * 32, "protocols": "tcp"}
    assert len(ts) == 2


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_talk_task_stays_no_log(pb):
    assert _talk_task(pb).get("no_log") is True


# --- the guard and the Talk task through real Ansible ---------------------------
# The plain-jinja tests above approximate Ansible (filters, native types,
# trim_blocks on module args). This runs the real guard task and renders the
# real Talk task body exactly as Ansible templates a module argument.

ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")


def _real(tmp_path, pb_path, over):
    plays = yaml.safe_load(pb_path.read_text())
    guard = _guard(pb_path)
    out = tmp_path / "talk.sh"
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"skhub": dict(BASE, **over), "skhub_public_host": "skhub.cluster1.example.com",
                 "nextcloud_container": {"stdout": "cid"}},
        "tasks": [guard, {"set_fact": {"rendered": _talk_task(pb_path)["shell"]}},
                  {"copy": {"content": "{{ rendered }}", "dest": str(out)}}]}]))
    env = dict(os.environ, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_INVENTORY_UNPARSED_WARNING="0")
    r = subprocess.run([ANSIBLE_PLAYBOOK, "-i", "localhost,", str(pb)], capture_output=True, text=True, env=env, cwd=tmp_path)
    assert plays
    return r, (out.read_text() if r.returncode == 0 else None)


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_real_ansible_default_and_set(tmp_path, pb):
    r0, unset = _real(tmp_path, pb, {})
    assert r0.returncode == 0, r0.stdout[-2000:]
    r1, empty = _real(tmp_path, pb, {"TALK_STUN_SERVERS": [], "INTERNAL_INGRESS_IP": ""})
    assert r1.returncode == 0 and empty == unset
    assert "stun_servers" not in unset and len(unset.strip().splitlines()) == 3
    r2, st = _real(tmp_path, pb, {"TALK_STUN_SERVERS": ["turn.example.com:3478"], "INTERNAL_INGRESS_IP": "10.20.30.40",
                                  "TALK_TURN_TLS_443": True, "TURN_DOMAIN": "turn.example.com"})
    assert r2.returncode == 0, r2.stdout[-2000:]
    line = next(l for l in st.splitlines() if "stun_servers" in l)
    assert json.loads(re.search(r"--value='([^']*)'", line).group(1)) == ["turn.example.com:3478"]
    assert [t["server"] for t in _turn_servers(st)] == ["turn.example.com:3478", "turn.example.com:443"]
    other = [l for l in st.splitlines() if "stun_servers" not in l and "spreed turn_servers" not in l]
    assert other == [l for l in unset.splitlines() if "spreed turn_servers" not in l]
    assert subprocess.run(["bash", "-n"], input=st, text=True).returncode == 0


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("case", ["ip loopback", "ip trailing newline", "stun string not list", "stun port too big", "stun without talk",
                                  "tls443 without TURN_DOMAIN", "tls443 TURN_DOMAIN rule injection"])
def test_real_ansible_guard_refuses(tmp_path, case):
    r, _ = _real(tmp_path, PLAYBOOKS[0], BAD[case])
    assert r.returncode != 0
    assert "INTERNAL_INGRESS_IP" in r.stdout and "TALK_STUN_SERVERS" in r.stdout


# --- docs ---------------------------------------------------------------------

def test_readme_documents_both_knobs():
    text = README.read_text()
    assert "## Internal routing to the public hostname" in text
    assert "`INTERNAL_INGRESS_IP`" in text
    assert "`TALK_STUN_SERVERS`" in text
    assert "`TALK_TURN_TLS_443`" in text
