"""skmesh (NetBird) must be deployable against a shared TURN server, reach
its own relay through Traefik, pin its images, and keep its management
state private.

* TURN: management.json hardcoded `turn:turn.<base>:3478` with static
  credentials and `TimeBasedCredentials: false`. A cluster that already runs
  one TURN server with a shared secret (coturn `use-auth-secret`, eturnal
  `secret`, the TURN REST scheme) could not point NetBird at it without a
  second TURN server. New optional vars: TURN_URI, STUN_URIS and
  TURN_TIME_BASED_CREDENTIALS (management then mints HMAC-SHA1 time-limited
  credentials from TURN_SECRET). Unset, management.json renders as before.
* Relay: the relay service had no Traefik route and sat only on the
  internal network, and skmesh.env stripped the scheme from
  NETBIRD_RELAY_ENDPOINT into NB_EXPOSED_ADDRESS, so the relay announced
  `rel://` (plain) for a TLS-terminated `:443`. Peers could never reach it.
  It now listens on a fixed port, is routed at `/relay` on the skmesh host
  (the WebSocket path NetBird clients dial), and keeps the `rels://` scheme.
* Images: MANAGEMENT_IMAGE, SIGNAL_IMAGE, RELAY_IMAGE, POSTGRES_IMAGE and
  POSTGRES_BACKUP_IMAGE let an instance pin by digest; defaults unchanged.
* Modes: the post-deploy task ran `chmod -R 755` over the management data
  dir (NetBird's /var/lib/netbird: store, keys, GeoLite and IdP data), the
  same bug class as skgit's data dir. Management runs as root, so the dir is
  now 0700 and its contents owner-only.
"""
import json
import os
import pathlib
import stat
import subprocess

import jinja2
import pytest
import yaml

from test_secret_file_modes_survive_deploy import CHMOD, _grants_other_read

SKMESH = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmesh"
SRC = SKMESH / "src/config/skmesh"
PLAYBOOK = SKMESH / "deploy_skmesh-prod.yml"

BASE = {
    "CLUSTERNAME": "demo",
    "DOMAIN": "example.test",
    "SSO_HOST_IP": "192.0.2.5",
    "POSTGRES_PASSWORD": "pw",
    "NETBIRD_AUTH_CLIENT_ID": "netbird",
    "NETBIRD_DATASTORE_ENC_KEY": "enc",
    "NETBIRD_RELAY_AUTH_SECRET": "relaysecret",
    "NETBIRD_RELAY_ENDPOINT": "rels://skmesh.demo.example.test:443",
    "TURN_SECRET": "turnsecret",
    "TURN_USER": "u",
    "TURN_PASSWORD": "p",
    "COTURN_EXTERNAL_IP": "192.0.2.20",
}


def _render(name, skmesh, strict=False):
    undefined = jinja2.StrictUndefined if strict else jinja2.ChainableUndefined
    env = jinja2.Environment(undefined=undefined, trim_blocks=True)
    return env.from_string((SRC / name).read_text()).render(env="prod", skmesh=skmesh, inventory_hostname="n1")


def _mgmt(skmesh, strict=False):
    return json.loads(_render("management.json.j2", skmesh, strict))


def _compose(skmesh):
    return yaml.safe_load(_render("skmesh.yml.j2", skmesh))


def _envfile(skmesh):
    out = {}
    for line in _render("skmesh.env.j2", skmesh).splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _labels(svc):
    return (svc.get("deploy") or {}).get("labels") or []


# --- TURN / STUN -----------------------------------------------------------

def test_turn_defaults_unchanged():
    m = _mgmt(dict(BASE))
    assert m["Stuns"] == [{"Proto": "udp", "URI": "stun:turn.demo.example.test:3478", "Username": "", "Password": None}]
    turn = m["TURNConfig"]
    assert turn["Turns"] == [{"Proto": "udp", "URI": "turn:turn.demo.example.test:3478", "Username": "u", "Password": "p"}]
    assert turn["TimeBasedCredentials"] is False
    assert turn["Secret"] == "turnsecret"


def test_turn_can_point_at_a_shared_secret_server():
    m = _mgmt(dict(
        BASE,
        TURN_URI="turn:turn.shared.example.test:3478",
        STUN_URIS=["stun:stun.example.test:3478"],
        TURN_TIME_BASED_CREDENTIALS=True,
    ))
    turn = m["TURNConfig"]
    assert turn["TimeBasedCredentials"] is True
    assert turn["Secret"] == "turnsecret"
    assert [t["URI"] for t in turn["Turns"]] == ["turn:turn.shared.example.test:3478"]
    assert [s["URI"] for s in m["Stuns"]] == ["stun:stun.example.test:3478"]


def test_time_based_turn_needs_no_static_user_or_password():
    skmesh = {k: v for k, v in BASE.items() if k not in ("TURN_USER", "TURN_PASSWORD")}
    skmesh["TURN_TIME_BASED_CREDENTIALS"] = True
    m = _mgmt(skmesh, strict=True)
    assert m["TURNConfig"]["Turns"][0]["Username"] == ""
    assert m["TURNConfig"]["Turns"][0]["Password"] == ""


def test_coturn_still_not_bundled_by_default():
    assert "coturn" not in _compose(dict(BASE))["services"]


# --- relay ------------------------------------------------------------------

def test_relay_keeps_its_tls_scheme():
    assert _envfile(dict(BASE))["NB_EXPOSED_ADDRESS"] == "rels://skmesh.demo.example.test:443"


def test_relay_is_routed_by_traefik_at_the_websocket_path():
    doc = _compose(dict(BASE))
    relay = doc["services"]["relay"]
    assert "cloud-public-prod" in relay["networks"]
    labels = _labels(relay)
    assert "traefik.enable=true" in labels
    rule = next(label for label in labels if ".rule=" in label)
    assert "Host(`skmesh.demo.example.test`)" in rule and "PathPrefix(`/relay`)" in rule
    env = dict(e.split("=", 1) for e in relay["environment"])
    port = env["NB_LISTEN_ADDRESS"].rsplit(":", 1)[1]
    assert f"loadbalancer.server.port={port}" in " ".join(labels)
    assert "traefik.docker.network=cloud-public-prod" in labels


def test_dashboard_does_not_swallow_the_relay_path():
    rule = next(label for label in _labels(_compose(dict(BASE))["services"]["dashboard"]) if ".rule=" in label)
    assert "!PathPrefix(`/relay`)" in rule


# --- images -----------------------------------------------------------------

IMAGE_VARS = {
    "management": "MANAGEMENT_IMAGE",
    "signal": "SIGNAL_IMAGE",
    "relay": "RELAY_IMAGE",
    "postgres": "POSTGRES_IMAGE",
    "postgres-db-backup": "POSTGRES_BACKUP_IMAGE",
}


def test_image_defaults_unchanged():
    svcs = _compose(dict(BASE))["services"]
    assert svcs["management"]["image"] == "netbirdio/management:0.79.0"
    assert svcs["signal"]["image"] == "netbirdio/signal:0.79.0"
    assert svcs["relay"]["image"] == "netbirdio/relay:0.79.0"
    assert svcs["postgres"]["image"] == "postgres:16.15-alpine"
    assert svcs["postgres-db-backup"]["image"].startswith("postgres:16.14@sha256:")


@pytest.mark.parametrize("svc,var", sorted(IMAGE_VARS.items()))
def test_images_are_overridable(svc, var):
    pin = f"registry.example.test/{svc}:1.0.0@sha256:" + "0" * 64
    assert _compose(dict(BASE, **{var: pin}))["services"][svc]["image"] == pin


# --- management data dir modes -----------------------------------------------

POST_TASK = "Wait for management container to start and fix permissions"


def _tasks():
    out = []
    for play in yaml.safe_load(PLAYBOOK.read_text()):
        for section in ("pre_tasks", "tasks", "post_tasks"):
            out.extend(play.get(section) or [])
    return out


def _fix_script():
    task = next(t for t in _tasks() if t.get("name") == POST_TASK)
    body = task["shell"].replace("{{ '{{' }}", "{{").replace("{{ '}}' }}", "}}")
    return body.replace("{{ app }}", "skmesh").replace("{{ env }}", "prod")


def test_no_chmod_grants_other_read_on_management():
    mgmt = "/var/data/skmesh-prod/management"
    bad = []
    for flags, mode, target in CHMOD.findall(_fix_script()):
        target = target.strip("\"'").rstrip("/")
        if target in ("$M", "${M}"):
            target = mgmt
        if _grants_other_read(mode) and (target == mgmt or target.startswith(mgmt + "/")):
            bad.append(f"chmod {flags}{mode} {target}")
    assert not bad, bad


def test_management_dir_is_created_0700():
    modes = [
        str(item.get("mode"))
        for t in _tasks()
        for item in (t.get("loop") or [])
        if isinstance(item, dict) and item.get("path") == "/var/data/{{ app }}-{{ env }}/management"
    ]
    assert modes == ["0700"], modes


def test_fix_script_leaves_management_private(tmp_path):
    root = tmp_path / "skmesh-prod"
    mgmt = root / "management"
    for rel in ("store.db", "GeoLite2-City.mmdb", "idp/idp.db"):
        p = mgmt / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        p.chmod(0o755)
    for d in ("signal", "relay"):
        (root / d).mkdir(parents=True)
    for d in [mgmt, mgmt / "idp"]:
        d.chmod(0o755)

    script = _fix_script().replace("/var/data/skmesh-prod", str(root))
    # the replica wait loop and chown need docker/root: stub them
    stub = tmp_path / "bin"
    stub.mkdir()
    for tool in ("docker", "chown"):
        (stub / tool).write_text("#!/bin/sh\necho 1/1\n")
        (stub / tool).chmod(0o755)
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True,
                       env={**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"})
    assert r.returncode == 0, r.stderr

    def mode(p):
        return stat.S_IMODE(p.stat().st_mode)

    assert mode(mgmt) == 0o700
    for p in mgmt.rglob("*"):
        assert mode(p) & 0o077 == 0, f"{p.relative_to(root)} is {oct(mode(p))}"
        assert mode(p) & 0o600 == 0o600, f"{p.relative_to(root)} lost owner access"
