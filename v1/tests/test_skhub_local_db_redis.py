"""skhub db (MariaDB) and redis on local disk on one pinned node.

Their data paths were hardcoded under /var/data/runtime (NFS on a typical
instance) and the services floated on any node matching placement_constraints.
NFS write latency there showed up as slow occ capability checks and Talk HPB
backend timeouts. The knobs:

  skhub.DATA_NODE       pin db + redis to this node (hostname)
  skhub.DB_DATA_PATH    default /var/data/runtime/<app>-<env>/db
  skhub.REDIS_DATA_PATH default /var/data/runtime/<app>-<env>/redis

A path outside /var/data is local disk: it is created on DATA_NODE (same
owner/mode as the shared-storage copy: db root:root 0755, redis root:root
0777) and the play refuses to run without DATA_NODE, since local data on a
floating service is lost on the next reschedule.
"""
import json
import pathlib

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
README = SKHUB / "README.md"
ENVS = ("dev", "staging", "prod")
PLAYBOOKS = {e: SKHUB / f"deploy_skhub-{e}.yml" for e in ENVS}
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
LOCAL = {"DATA_NODE": "node-db", "DB_DATA_PATH": "/var/lib/skhub-prod/db",
         "REDIS_DATA_PATH": "/var/lib/skhub-prod/redis"}
PINNED_SERVICES = ("db", "redis")


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def render(env_name="prod", **over):
    return _env().from_string(COMPOSE.read_text()).render(app="skhub", env=env_name, skhub=dict(BASE, **over))


def services(env_name="prod", **over):
    return yaml.safe_load(render(env_name, **over))["services"]


def constraints(svc):
    return (svc.get("deploy") or {}).get("placement", {}).get("constraints") or []


def data_mount(svc, target):
    return [v.split(":")[0] for v in svc.get("volumes", []) if isinstance(v, str) and v.split(":")[1] == target]


# ---- compose ------------------------------------------------------------------

@pytest.mark.parametrize("env_name", ENVS)
def test_default_keeps_nfs_paths_and_no_hostname_pin(env_name):
    s = services(env_name)
    assert data_mount(s["db"], "/var/lib/mysql") == [f"/var/data/runtime/skhub-{env_name}/db"]
    assert data_mount(s["redis"], "/data") == [f"/var/data/runtime/skhub-{env_name}/redis"]
    for name, svc in s.items():
        assert not any(c.startswith("node.hostname ==") for c in constraints(svc)), name


@pytest.mark.parametrize("env_name", ENVS)
def test_empty_knobs_render_byte_identical_to_unset(env_name):
    assert render(env_name) == render(env_name, DATA_NODE="", DB_DATA_PATH="", REDIS_DATA_PATH="")


def test_data_node_pins_db_and_redis_only():
    s = services(**LOCAL)
    for name in PINNED_SERVICES:
        assert constraints(s[name]) == ["node.hostname == node-db"], name
    for name in set(s) - set(PINNED_SERVICES):
        assert "node.hostname == node-db" not in constraints(s[name]), name


def test_data_node_is_appended_after_existing_placement_constraints():
    s = services(placement_constraints=["node.role == worker"], **LOCAL)
    for name in PINNED_SERVICES:
        assert constraints(s[name]) == ["node.role == worker", "node.hostname == node-db"], name


def test_custom_paths_render():
    s = services(**LOCAL)
    assert data_mount(s["db"], "/var/lib/mysql") == ["/var/lib/skhub-prod/db"]
    assert data_mount(s["redis"], "/data") == ["/var/lib/skhub-prod/redis"]
    assert "/var/data/runtime/skhub-prod/db" not in json.dumps(s)
    assert "/var/data/runtime/skhub-prod/redis" not in json.dumps(s)


def test_only_db_and_redis_change():
    a, b = services(), services(**LOCAL)
    assert {n for n in a if a[n] != b[n]} == set(PINNED_SERVICES)


def test_db_backup_clamav_and_imaginary_are_not_pinned_by_data_node():
    s = services(**LOCAL)
    for name in ("db-backup", "clamav", "imaginary"):
        assert "node.hostname == node-db" not in constraints(s[name]), name


# ---- playbooks ----------------------------------------------------------------

def tasks(env_name):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env_name].read_text()):
        if isinstance(play, dict):
            for sec in ("pre_tasks", "tasks"):
                out += play.get(sec) or []
    return out


def _guard(env_name):
    hits = [t for t in tasks(env_name) if "assert" in t and "DATA_NODE" in json.dumps(t.get("vars", {}))
            and "DB_DATA_PATH" in json.dumps(t.get("vars", {}))]
    assert len(hits) == 1, env_name
    return hits[0]


def _passes(env_name, **over):
    e = _env()
    t = _guard(env_name)
    ctx = {"app": "skhub", "env": env_name, "skhub": dict(BASE, **over)}
    for k, v in (t.get("vars") or {}).items():
        ctx[k] = e.from_string(v).render(**ctx)
    return all(e.compile_expression(c)(**ctx) for c in t["assert"]["that"])


@pytest.mark.parametrize("env_name", ENVS)
@pytest.mark.parametrize("over,ok", [
    ({}, True),
    (LOCAL, True),
    ({"DB_DATA_PATH": "/var/lib/skhub-prod/db"}, False),
    ({"REDIS_DATA_PATH": "/var/lib/skhub-prod/redis"}, False),
    ({"DB_DATA_PATH": "/var/lib/skhub-prod/db", "REDIS_DATA_PATH": "/var/lib/skhub-prod/redis"}, False),
    ({"DB_DATA_PATH": "/var/data/runtime/x/db"}, True),
    ({"DATA_NODE": "node-db"}, True),
    ({"DATA_NODE": "", "DB_DATA_PATH": "/var/lib/skhub-prod/db", "REDIS_DATA_PATH": ""}, False),
])
def test_guard_fails_closed(env_name, over, ok):
    assert _passes(env_name, **over) is ok


def _db_local_task(env_name):
    hits = [t for t in tasks(env_name) if "file" in t and "data node" in t.get("name", "").lower()
            and "db" in t["name"].lower() and "redis" not in t["name"].lower()]
    assert len(hits) == 1, env_name
    return hits[0]


def _redis_local_task(env_name):
    hits = [t for t in tasks(env_name) if "file" in t and "local redis" in t.get("name", "").lower()]
    assert len(hits) == 1, env_name
    return hits[0]


@pytest.mark.parametrize("env_name", ENVS)
def test_local_db_dir_is_created_on_the_data_node_as_root_0755(env_name):
    t = _db_local_task(env_name)
    assert t["delegate_to"] == "{{ skhub.DATA_NODE | default(inventory_hostname, true) }}"
    assert "skhub_db_data_path" in t["when"] or "skhub_db_data_path" in json.dumps(t)
    assert str(t["file"]["owner"]) == "root" and str(t["file"]["mode"]) == "0755"
    assert not t["file"].get("recurse")


@pytest.mark.parametrize("env_name", ENVS)
def test_local_redis_dir_is_created_on_the_data_node_as_root_0777(env_name):
    t = _redis_local_task(env_name)
    assert t["delegate_to"] == "{{ skhub.DATA_NODE | default(inventory_hostname, true) }}"
    assert str(t["file"]["owner"]) == "root" and str(t["file"]["mode"]) == "0777"
    assert not t["file"].get("recurse")


@pytest.mark.parametrize("env_name", ENVS)
def test_every_data_node_delegate_renders_with_strict_undefined_when_unset(env_name):
    """Ansible templates delegate_to before when (the sksso #125 bug)."""
    e = jinja2.Environment(undefined=jinja2.StrictUndefined)
    hits = [t for t in tasks(env_name) if "DATA_NODE" in str(t.get("delegate_to", ""))]
    assert len(hits) >= 2, env_name
    for t in hits:
        tpl = e.from_string(t["delegate_to"])
        assert tpl.render(skhub={}, inventory_hostname="mgr-1") == "mgr-1"
        assert tpl.render(skhub={"DATA_NODE": "node-db"}, inventory_hostname="mgr-1") == "node-db"


@pytest.mark.parametrize("env_name", ENVS)
def test_old_nfs_dirs_are_not_created_unconditionally(env_name):
    text = PLAYBOOKS[env_name].read_text()
    shared_tasks = [t for t in tasks(env_name) if "file" in t
                    and ("skhub_db_data_path" in json.dumps(t) or "skhub_redis_data_path" in json.dumps(t))
                    and not t.get("delegate_to")]
    assert len(shared_tasks) == 2, env_name
    for t in shared_tasks:
        assert "startswith('/var/data/')" in str(t["when"])
    assert '"/var/data/runtime/{{ app }}-{{ env }}/db"' not in text


@pytest.mark.parametrize("env_name", ENVS)
def test_no_recursive_ownership_or_mode_changes(env_name):
    for t in tasks(env_name):
        if "file" in t:
            assert not t["file"].get("recurse"), t.get("name")
        assert "chown -R" not in json.dumps(t) and "chmod -R" not in json.dumps(t), t.get("name")


# ---- docs ---------------------------------------------------------------------

def test_readme_documents_the_knobs_and_the_migration():
    text = README.read_text()
    for key in ("DATA_NODE", "DB_DATA_PATH", "REDIS_DATA_PATH", "rsync -aHAX --numeric-ids",
                "--mount-rm", "STALE"):
        assert key in text, key
    assert "Local database on a pinned node" in text
