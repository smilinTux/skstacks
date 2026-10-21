"""sksec CrowdSec LAPI db on local disk on one pinned node (coord card
d6fa93ec / 81e36611).

The LAPI's SQLite db was hardcoded under /var/data/runtime (NFS on a
typical instance). The bouncer queries it on every protected request and
writes last_pull every time; under load that write serializes on NFS, and
once a lookup exceeds CROWDSEC_TIMEOUT the bouncer fails CLOSED (403) to
every protected route, not just slow ones. The knobs (same shape as
sksso's DATA_NODE for postgres/redis):

  sksec.DATA_NODE          pin crowdsec (+ the HA acme-master constraint,
                            if TRAEFIK_HA_MODE is also set) to this node
  sksec.CROWDSEC_DB_PATH   default /var/data/runtime/<app>-<env>/crowdsec-db

A path outside /var/data is local disk: it is created on DATA_NODE (owner
root, 0750, never recursive) and the play refuses to run without
DATA_NODE, since local data on a floating service is an empty db after the
next reschedule. With DATA_NODE set, the deploy also sets
db_config.use_wal: true in config.yaml (never on the NFS path: WAL is not
safe on NFS).
"""
import json
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
APP = ANSIBLE / "core/sksec"
T = APP / "src/config/sksec/sksec.yml.j2"
PLAYBOOKS = {e: APP / f"deploy_sksec-{e}.yml" for e in ("dev", "staging", "prod")}
LOCAL = {"DATA_NODE": "node-db", "CROWDSEC_DB_PATH": "/var/lib/sksec/crowdsec-db"}


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def _vars(env="prod", traefik_service_name="skfence", crowdsec_db_path=None, **over):
    db_path = crowdsec_db_path or over.get("CROWDSEC_DB_PATH") or f"/var/data/runtime/sksec-{env}/crowdsec-db"
    return {"app": "sksec", "env": env, "traefik_service_name": traefik_service_name,
            "sksec": {"CROWDSEC_CPU_LIMIT": "0.5", "CROWDSEC_CPU_RESERVATION": "0.1", **over},
            "sksec_crowdsec_db_path": db_path}


def services(env="prod", **over):
    return yaml.safe_load(_env().from_string(T.read_text()).render(**_vars(env, **over)))["services"]


def constraints(svc):
    return (svc.get("deploy") or {}).get("placement", {}).get("constraints") or []


def data_mount(svc, target):
    return [v.split(":")[0] for v in svc["volumes"] if isinstance(v, str) and v.split(":")[1] == target]


# ---- compose ----------------------------------------------------------------

def test_default_keeps_nfs_path_and_no_hostname_pin():
    s = services()
    assert data_mount(s["crowdsec"], "/var/lib/crowdsec/data/") == ["/var/data/runtime/sksec-prod/crowdsec-db"]
    assert not constraints(s["crowdsec"])
    assert not constraints(s["bouncer-traefik"])


def test_ha_mode_only_renders_the_legacy_single_constraint():
    """Byte-identical to every instance already running TRAEFIK_HA_MODE
    without DATA_NODE: the single acme-master constraint, unchanged."""
    s = services(traefik_service_name="skfenceha", TRAEFIK_HA_MODE=True)
    assert constraints(s["crowdsec"]) == ["node.labels.traefik.acme.master == true"]


def test_data_node_pins_crowdsec_only():
    s = services(**LOCAL)
    assert constraints(s["crowdsec"]) == ["node.hostname == node-db"]
    for name in set(s) - {"crowdsec"}:
        assert "node.hostname == node-db" not in constraints(s[name]), name


def test_data_node_and_ha_mode_combine():
    s = services(traefik_service_name="skfenceha", TRAEFIK_HA_MODE=True, **LOCAL)
    assert constraints(s["crowdsec"]) == [
        "node.labels.traefik.acme.master == true",
        "node.hostname == node-db",
    ]


def test_custom_path_renders_and_nfs_path_is_absent():
    s = services(**LOCAL)
    assert data_mount(s["crowdsec"], "/var/lib/crowdsec/data/") == ["/var/lib/sksec/crowdsec-db"]
    assert "/var/data/runtime/sksec-prod/crowdsec-db" not in json.dumps(s)


def test_bouncer_is_not_pinned_by_data_node():
    s = services(**LOCAL)
    assert "node.hostname == node-db" not in constraints(s["bouncer-traefik"])


# ---- playbooks ----------------------------------------------------------------

def tasks(env):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env].read_text()):
        if isinstance(play, dict):
            for sec in ("pre_tasks", "tasks"):
                out += play.get(sec) or []
    return out


def _guard(env):
    hits = [t for t in tasks(env) if "assert" in t and "DATA_NODE" in json.dumps(t)
            and "crowdsec" in json.dumps(t).lower()]
    assert len(hits) == 1, env
    return hits[0]["assert"]["that"]


def _passes(env, **over):
    e = _env()
    v = _vars(env, **over)
    return all(e.compile_expression(c)(**v) for c in _guard(env))


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
@pytest.mark.parametrize("over,ok", [
    ({}, True),
    (LOCAL, True),
    ({"CROWDSEC_DB_PATH": "/var/lib/sksec/crowdsec-db"}, False),
    ({"CROWDSEC_DB_PATH": f"/var/data/runtime/x/crowdsec-db"}, True),
    ({"DATA_NODE": "node-db"}, True),
])
def test_fail_closed_guard(env, over, ok):
    assert _passes(env, **over) is ok, (env, over)


def test_wal_include_is_gated_on_data_node(env="prod"):
    hits = [t for t in tasks(env) if "include_tasks" in t
            and "enable_wal.yml" in json.dumps(t.get("include_tasks"))]
    assert len(hits) == 1, env
    e = _env()
    assert e.compile_expression(hits[0]["when"])(**_vars(env)) is False
    assert e.compile_expression(hits[0]["when"])(**_vars(env, DATA_NODE="node-db")) is True
