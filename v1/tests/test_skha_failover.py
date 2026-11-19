"""skha keepalived.conf.j2: the VIP must fail over.

Before 2026-11-19 the ACME master was rendered state MASTER, priority 150,
with no track_script, so the VIP was locked to it even when it ran no
Traefik worker (NAM: nammgr1002 held the VIP with its worker down after an
NFS mount failure, public 443 refused). Now every node is BACKUP with the
health checks tracked; the ACME master is only preferred (highest priority);
VIP gate checks (default [traefik]) render weight 0, so a failure is FAULT
and the VIP leaves. skha.lock_vip_to_acme_master: true keeps the old lock as
an explicit opt-in."""
import pathlib
import re

import jinja2

CONF = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skha/src/keepalived/config/keepalived.conf.j2"

CHECKS = [
    {"name": "traefik", "ports": [80, 443], "container_pattern": "traefik-worker"},
    {"name": "mail", "ports": [25], "container_pattern": "skmail", "require": True},
]


def render(acme_master=False, priority=80, checks=CHECKS, **skha_extra):
    skha = {"vip": "10.0.0.70/32", "auth_pass": "x", **skha_extra}
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    # Ansible's bool filter, which plain jinja2 does not ship.
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")
    return env.from_string(CONF.read_text()).render(
        inventory_hostname="node1",
        app_health_checks=checks,
        acme_master_node=acme_master,
        skha_priority=priority,
        skha=skha,
        primary_interface="ens18",
        ansible_facts={"default_ipv4": {"address": "10.0.0.71"}},
        skha_peers=["10.0.0.72", "10.0.0.73"],
    )


def instance(text):
    return text[text.index("vrrp_instance VI_1"):]


def script_weight(text, name):
    m = re.search(r"vrrp_script chk_%s \{.*?weight (-?\d+)" % name, text, re.S)
    assert m, name
    return int(m.group(1))


def test_acme_master_is_backup_with_tracked_checks_by_default():
    vi = instance(render(acme_master=True, priority=150))
    assert "state BACKUP" in vi
    assert "state MASTER" not in vi
    assert "priority 150" in vi
    assert "track_script" in vi and "chk_traefik" in vi and "chk_mail" in vi
    assert "nopreempt" in vi


def test_worker_is_backup_with_tracked_checks():
    vi = instance(render(acme_master=False, priority=70))
    assert "state BACKUP" in vi and "priority 70" in vi
    assert "chk_traefik" in vi


def test_traefik_gate_check_is_decisive_weight_zero():
    text = render()
    assert script_weight(text, "traefik") == 0
    assert script_weight(text, "mail") == -10


def test_explicit_weight_on_a_gate_check_wins():
    checks = [dict(CHECKS[0], weight=-20), CHECKS[1]]
    assert script_weight(render(checks=checks), "traefik") == -20


def test_vip_gate_checks_is_configurable():
    text = render(vip_gate_checks=["mail"])
    assert script_weight(text, "mail") == 0
    assert script_weight(text, "traefik") == -10


def test_lock_opt_in_restores_the_old_locked_master():
    vi = instance(render(acme_master=True, priority=150, lock_vip_to_acme_master=True))
    assert "state MASTER" in vi and "priority 150" in vi
    assert "track_script" not in vi


def test_lock_opt_in_does_not_affect_workers():
    vi = instance(render(acme_master=False, priority=80, lock_vip_to_acme_master=True))
    assert "state BACKUP" in vi and "track_script" in vi
