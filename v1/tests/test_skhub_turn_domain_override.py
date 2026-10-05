"""skhub Talk TURN server domain override (skhub.TURN_DOMAIN).

NAM relays Talk's TURN/STUN through nor's eturnal (a Chef-approved cross-cluster
exception: NAM's own ingress blocks inbound 3478 at the ISP). Before this knob,
the "Configure Talk signaling and TURN servers in Nextcloud" task always
registered the local skhub hostname as the TURN server
(`skhub.{{ CLUSTERNAME }}.{{ DOMAIN }}:3478`), with no way to point Nextcloud's
`turn_servers` config at a different TURN host while still rendering that
instance's own `turn_secret` (which, for NAM, is set to the remote cluster's
TURN secret rather than a value NAM's own eturnal uses).

skhub.TURN_DOMAIN (optional, default unset): overrides the TURN server
hostname advertised to Nextcloud. Default (unset) renders exactly as before
(`{{ CLUSTERNAME }}.{{ DOMAIN }}`); the port (3478) and `turn_secret` are
unaffected by this knob.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

SKHUB_DIR = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB_DIR.glob("deploy_skhub-*.yml"))
BASE_VARS = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "turn_secret": "x" * 32}


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    return env


def _turn_task(pb):
    plays = yaml.safe_load(pb.read_text())
    for play in plays:
        for task in play.get("tasks") or []:
            if task.get("name") == "Configure Talk signaling and TURN servers in Nextcloud":
                return task
    raise AssertionError(f"{pb.name}: no Talk signaling/TURN task found")


def _turn_server_value(pb, **overrides):
    task = _turn_task(pb)
    shell = task["shell"]
    rendered = _env().from_string(shell).render(skhub=dict(BASE_VARS, **overrides))
    turn_line = next(l for l in rendered.splitlines() if "turn_servers" in l)
    m = re.search(r'"server":"([^"]+)"', turn_line)
    assert m, turn_line
    return m.group(1)


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_default_unchanged_without_turn_domain(pb):
    assert _turn_server_value(pb) == "cluster1.example.com:3478"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_turn_domain_override_replaces_the_hostname(pb):
    assert _turn_server_value(pb, TURN_DOMAIN="turn.example.com") == "turn.example.com:3478"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_turn_domain_override_does_not_touch_the_secret(pb):
    task = _turn_task(pb)
    shell = task["shell"]
    rendered = _env().from_string(shell).render(skhub=dict(BASE_VARS, turn_secret="y" * 32, TURN_DOMAIN="turn.example.com"))
    assert '"secret":"' + "y" * 32 + '"' in rendered


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_playbook_still_parses_as_yaml(pb):
    assert yaml.safe_load(pb.read_text())
