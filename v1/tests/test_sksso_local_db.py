"""sksso postgres and redis on local disk on one pinned node.

Their data paths were hardcoded under /var/data/runtime (NFS on a typical
instance) and the services floated on any worker. An NFS hang on one worker
took Authentik, and with it every forward-auth login, down. The knobs:

  sksso.DATA_NODE           pin postgres + redis to this node (hostname)
  sksso.POSTGRES_DATA_PATH  default /var/data/runtime/<app>-<env>/postgres
  sksso.REDIS_DATA_PATH     default /var/data/runtime/<app>-<env>/redis

A path outside /var/data is local disk: it is created on DATA_NODE (owner
999, 0700) and the play refuses to run without DATA_NODE, since local data
on a floating service is lost on the next reschedule.
"""
import json
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
APP = ANSIBLE / "optional/sksso"
T = APP / "src/config/sksso/sksso.yml.j2"
PLAYBOOKS = {e: APP / f"deploy_sksso-{e}.yml" for e in ("dev", "staging", "prod")}
LOCAL = {"DATA_NODE": "node-db", "POSTGRES_DATA_PATH": "/var/lib/sksso/postgres",
         "REDIS_DATA_PATH": "/var/lib/sksso/redis"}


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def _vars(env="prod", **over):
    return {"app": "sksso", "env": env,
            "sksso": {"CLUSTERNAME": "demo", "DOMAIN": "example.test", "postgres_user": "u",
                      "postgres_password": "p", "authentik_secret_key": "k", **over}}


def services(env="prod", **over):
    return yaml.safe_load(_env().from_string(T.read_text()).render(**_vars(env, **over)))["services"]


def constraints(svc):
    return (svc.get("deploy") or {}).get("placement", {}).get("constraints") or []


def data_mount(svc, target):
    return [v.split(":")[0] for v in svc["volumes"] if isinstance(v, str) and v.split(":")[1] == target]


# ---- compose ----------------------------------------------------------------

def test_default_keeps_nfs_paths_and_no_hostname_pin():
    s = services()
    assert data_mount(s["postgres"], "/var/lib/postgresql/data") == ["/var/data/runtime/sksso-prod/postgres"]
    assert data_mount(s["redis"], "/data") == ["/var/data/runtime/sksso-prod/redis"]
    for name, svc in s.items():
        assert not any(c.startswith("node.hostname ==") for c in constraints(svc)), name


def test_data_node_pins_postgres_and_redis_only():
    s = services(DATA_NODE="node-db")
    for name in ("postgres", "redis"):
        assert constraints(s[name]) == ["node.role == worker", "node.hostname == node-db"], name
    for name in set(s) - {"postgres", "redis"}:
        assert "node.hostname == node-db" not in constraints(s[name]), name


def test_data_node_without_the_worker_constraint():
    s = services(DATA_NODE="node-db", placement_use_worker_constraint=False)
    assert constraints(s["postgres"]) == ["node.hostname == node-db"]
    assert constraints(s["redis"]) == ["node.hostname == node-db"]


def test_custom_paths_render():
    s = services(**LOCAL)
    assert data_mount(s["postgres"], "/var/lib/postgresql/data") == ["/var/lib/sksso/postgres"]
    assert data_mount(s["redis"], "/data") == ["/var/lib/sksso/redis"]
    assert "/var/data/runtime/sksso-prod/postgres" not in json.dumps(s)


def test_backup_sidecar_is_not_pinned_by_data_node():
    s = services(**LOCAL)
    assert "node.hostname == node-db" not in constraints(s["postgres-db-backup"])


# ---- playbooks ----------------------------------------------------------------

def tasks(env):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env].read_text()):
        if isinstance(play, dict):
            for sec in ("pre_tasks", "tasks"):
                out += play.get(sec) or []
    return out


def _guard(env):
    hits = [t for t in tasks(env) if "assert" in t and "DATA_NODE" in json.dumps(t)]
    assert len(hits) == 1, env
    return hits[0]["assert"]["that"]


def _passes(env, **over):
    e = _env()
    return all(e.compile_expression(c)(**_vars(env, **over)) for c in _guard(env))


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
@pytest.mark.parametrize("over,ok", [
    ({}, True),
    (LOCAL, True),
    ({"POSTGRES_DATA_PATH": "/var/lib/sksso/postgres"}, False),
    ({"REDIS_DATA_PATH": "/var/lib/sksso/redis"}, False),
    ({"POSTGRES_DATA_PATH": "/var/data/runtime/x/postgres"}, True),
    ({"DATA_NODE": "node-db"}, True),
])
def test_local_data_without_a_pinned_node_fails(env, over, ok):
    assert _passes(env, **over) is ok


def _ctx(env, **over):
    """The task context: vault vars plus the play's own vars, resolved."""
    ctx = _vars(env, **over)
    play = [p for p in yaml.safe_load(PLAYBOOKS[env].read_text()) if isinstance(p, dict) and "vars" in p][0]
    for k, v in play["vars"].items():
        ctx[k] = _env().from_string(str(v)).render(**ctx)
    return ctx


def _db_dir_tasks(env):
    return [t for t in tasks(env) if "file" in t and "data_path" in json.dumps(t).lower()]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_local_dirs_are_created_on_the_data_node_as_999_0700(env):
    local = [t for t in _db_dir_tasks(env) if t.get("delegate_to")]
    assert len(local) == 1
    t = local[0]
    assert t["delegate_to"] == "{{ sksso.DATA_NODE }}"
    assert str(t["file"]["owner"]) == "999" and str(t["file"]["mode"]) == "0700"
    assert not t["file"].get("recurse")


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_old_nfs_path_is_not_created_when_a_custom_path_is_set(env):
    shared = [t for t in tasks(env) if "file" in t and "loop" in t]
    created = []
    for t in shared:
        loop = t["loop"] if isinstance(t["loop"], list) else []
        for item in loop:
            ctx = _ctx(env, **LOCAL)
            path = _env().from_string(str(item)).render(**ctx)
            when = t.get("when")
            if when is not None and not _env().compile_expression(str(when))(item=path, **ctx):
                continue
            created.append(path)
    assert f"/var/data/runtime/sksso-{env}/postgres" not in created
    assert f"/var/data/runtime/sksso-{env}/redis" not in created


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_default_still_creates_the_nfs_paths_from_the_manager(env):
    nfs = [t for t in _db_dir_tasks(env) if not t.get("delegate_to")]
    assert len(nfs) == 1
    t = nfs[0]
    items = [_env().from_string(str(i)).render(**_ctx(env)) for i in t["loop"]]
    when = t.get("when")
    assert f"/var/data/runtime/sksso-{env}/postgres" in items and f"/var/data/runtime/sksso-{env}/redis" in items
    assert "/var/data/" in json.dumps(when)


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_no_recursive_ownership_or_mode_changes(env):
    for t in tasks(env):
        if "file" in t:
            assert not t["file"].get("recurse"), t.get("name")
        assert "chown -R" not in json.dumps(t) and "chmod -R" not in json.dumps(t), t.get("name")


def test_readme_documents_local_db_migration_and_the_mount_rm_trap():
    r = (APP / "README.md").read_text()
    assert "## Local database on a pinned node" in r
    for word in ("DATA_NODE", "POSTGRES_DATA_PATH", "REDIS_DATA_PATH", "rsync -aHAX --numeric-ids",
                 "--mount-rm", "guard file"):
        assert word in r, word
