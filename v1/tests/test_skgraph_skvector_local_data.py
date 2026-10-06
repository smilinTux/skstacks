"""skgraph (FalkorDB) and skvector (Qdrant) data on local disk on one pinned node.

Both bind their data under /var/data (NFS on a typical instance) and float on
any node matching their placement. The knobs, same design as skhub's
DATA_NODE / DB_DATA_PATH (README "Local database on a pinned node"):

  skgraph.DATA_NODE         pin falkordb to this node (hostname)
  skgraph.DATA_PATH         default /var/data/skgraph-<env>/data
  skvector.DATA_NODE        pin qdrant to this node (hostname)
  skvector.STORAGE_PATH     default /var/data/skvector-<env>/storage
  skvector.SNAPSHOTS_PATH   default /var/data/skvector-<env>/snapshots

A path outside /var/data is local disk: it is created on DATA_NODE (not
recursive, same owner/mode as the shared-storage copy) and the old /var/data
path is no longer created; the play refuses to run with a local path and no
DATA_NODE, since local data on a floating service is an empty database after
the next reschedule.

Unset knobs must render byte-identical to skstacks-v2.26.1: the baselines in
fixtures/local_data_baseline/ were rendered from the v2.26.1 templates with
the render gate's example vars, before this change.
"""
import json
import os
import pathlib
import shutil
import subprocess

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
OPTIONAL = V1 / "ansible/optional"
BASELINE = pathlib.Path(__file__).resolve().parent / "fixtures/local_data_baseline"
RENDER_VARS = pathlib.Path(__file__).resolve().parent / "render/vars"
ENVS = ("dev", "staging", "prod")

STACKS = {
    "skgraph": {
        "service": "falkordb",
        "mounts": {"DATA_PATH": "/var/lib/falkordb/data"},
        "default": {"DATA_PATH": "/var/data/skgraph-{env}/data"},
        "path_vars": {"DATA_PATH": "skgraph_data_path"},
        "base_constraints": ["node.role == worker"],
        "owner": "root", "group": "root", "mode": "0755",
    },
    "skvector": {
        "service": "qdrant",
        "mounts": {"STORAGE_PATH": "/qdrant/storage", "SNAPSHOTS_PATH": "/qdrant/snapshots"},
        "default": {"STORAGE_PATH": "/var/data/skvector-{env}/storage",
                    "SNAPSHOTS_PATH": "/var/data/skvector-{env}/snapshots"},
        "path_vars": {"STORAGE_PATH": "skvector_storage_path", "SNAPSHOTS_PATH": "skvector_snapshots_path"},
        "base_constraints": [],
        "owner": "1000", "group": "1000", "mode": "0755",
    },
}
LOCAL = {
    "skgraph": {"DATA_NODE": "node-db", "DATA_PATH": "/var/lib/skgraph-prod/data"},
    "skvector": {"DATA_NODE": "node-db", "STORAGE_PATH": "/var/lib/skvector-prod/storage",
                 "SNAPSHOTS_PATH": "/var/lib/skvector-prod/snapshots"},
}
PIN = "node.hostname == node-db"


def compose_path(stack):
    return OPTIONAL / stack / f"src/config/{stack}/{stack}.yml.j2"


def playbook(stack, env):
    return OPTIONAL / stack / f"deploy_{stack}-{env}.yml"


def example_vars(stack):
    return yaml.safe_load((RENDER_VARS / f"{stack}.example.yml").read_text())[stack]


def _env():
    e = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    e.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return e


def render(stack, env="prod", **over):
    return _env().from_string(compose_path(stack).read_text()).render(
        app=stack, env=env, **{stack: dict(example_vars(stack), **over)})


def service(stack, env="prod", **over):
    return yaml.safe_load(render(stack, env, **over))["services"][STACKS[stack]["service"]]


def constraints(svc):
    return svc["deploy"]["placement"]["constraints"]


def mount_source(svc, target):
    hits = [v.split(":")[0] for v in svc.get("volumes", []) if isinstance(v, str) and v.split(":")[1] == target]
    assert len(hits) == 1, (target, svc.get("volumes"))
    return hits[0]


# ---- compose: unset is byte-identical to v2.26.1 ------------------------------

@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_unset_knobs_render_byte_identical_to_v2_26_1(stack, env):
    assert render(stack, env) == (BASELINE / f"{stack}-{env}.yml").read_text()


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_empty_knobs_render_byte_identical_to_unset(stack, env):
    empty = {k: "" for k in LOCAL[stack]}
    assert render(stack, env, **empty) == render(stack, env)


@pytest.mark.parametrize("stack", STACKS)
def test_data_node_alone_only_pins(stack):
    """DATA_NODE with default paths: same mounts, one extra constraint."""
    a, b = service(stack), service(stack, DATA_NODE="node-db")
    assert constraints(b) == STACKS[stack]["base_constraints"] + [PIN]
    for target in STACKS[stack]["mounts"].values():
        assert mount_source(a, target) == mount_source(b, target)


# ---- compose: local paths + DATA_NODE -----------------------------------------

@pytest.mark.parametrize("stack", STACKS)
def test_local_paths_and_data_node_render_mounts_and_constraint(stack):
    svc = service(stack, **LOCAL[stack])
    for key, target in STACKS[stack]["mounts"].items():
        assert mount_source(svc, target) == LOCAL[stack][key]
    assert constraints(svc) == STACKS[stack]["base_constraints"] + [PIN]
    text = render(stack, **LOCAL[stack])
    for default in STACKS[stack]["default"].values():
        assert default.format(env="prod") not in text


def test_skvector_named_volume_devices_follow_the_paths():
    vols = yaml.safe_load(render("skvector", **LOCAL["skvector"]))["volumes"]
    assert vols["skvector-prod-storage"]["driver_opts"]["device"] == LOCAL["skvector"]["STORAGE_PATH"]
    assert vols["skvector-prod-snapshots"]["driver_opts"]["device"] == LOCAL["skvector"]["SNAPSHOTS_PATH"]


def test_skvector_data_node_is_appended_after_placement_constraints():
    svc = service("skvector", placement_constraints=["node.role == worker"], **LOCAL["skvector"])
    assert constraints(svc) == ["node.role == worker", PIN]


def test_skvector_identical_constraint_is_not_duplicated():
    svc = service("skvector", placement_constraints=["node.role == worker", PIN], **LOCAL["skvector"])
    assert constraints(svc) == ["node.role == worker", PIN]


def test_skgraph_data_node_follows_the_worker_constraint():
    assert constraints(service("skgraph", **LOCAL["skgraph"])) == ["node.role == worker", PIN]


@pytest.mark.parametrize("stack", STACKS)
def test_only_data_mounts_and_placement_change(stack):
    a, b = service(stack), service(stack, **LOCAL[stack])
    a.pop("volumes"), b.pop("volumes")
    a["deploy"].pop("placement"), b["deploy"].pop("placement")
    assert a == b


# ---- playbooks ----------------------------------------------------------------

def plays(stack, env):
    return [p for p in yaml.safe_load(playbook(stack, env).read_text()) if isinstance(p, dict)]


def deploy_play(stack, env):
    hits = [p for p in plays(stack, env) if (p.get("vars") or {}).get("app") == stack]
    assert len(hits) == 1
    return hits[0]


def tasks(stack, env):
    p = deploy_play(stack, env)
    return (p.get("pre_tasks") or []) + (p.get("tasks") or [])


def _guard(stack, env):
    hits = [t for t in tasks(stack, env) if "assert" in t and "DATA_NODE" in json.dumps(t.get("vars", {}))]
    assert len(hits) == 1, (stack, env)
    return hits[0]


def _guard_passes(stack, env, **over):
    e = _env()
    t = _guard(stack, env)
    ctx = {"app": stack, "env": env, stack: dict(example_vars(stack), **over)}
    for k, v in (deploy_play(stack, env).get("vars") or {}).items():
        if isinstance(v, str):
            ctx[k] = e.from_string(v).render(**ctx)
    for k, v in (t.get("vars") or {}).items():
        ctx[k] = e.from_string(v).render(**ctx)
    return all(e.compile_expression(c)(**ctx) for c in t["assert"]["that"])


GUARD_CASES = {
    "skgraph": [
        ({}, True),
        (LOCAL["skgraph"], True),
        ({"DATA_PATH": "/var/lib/skgraph-prod/data"}, False),
        ({"DATA_NODE": "", "DATA_PATH": "/var/lib/skgraph-prod/data"}, False),
        ({"DATA_PATH": "/var/data/elsewhere/data"}, True),
        ({"DATA_NODE": "node-db"}, True),
        ({"DATA_PATH": ""}, True),
    ],
    "skvector": [
        ({}, True),
        (LOCAL["skvector"], True),
        ({"STORAGE_PATH": "/var/lib/skvector-prod/storage"}, False),
        ({"SNAPSHOTS_PATH": "/var/lib/skvector-prod/snapshots"}, False),
        ({"STORAGE_PATH": "/var/lib/skvector-prod/storage", "SNAPSHOTS_PATH": "/var/lib/skvector-prod/snapshots"},
         False),
        ({"DATA_NODE": "", "STORAGE_PATH": "/var/lib/skvector-prod/storage"}, False),
        ({"STORAGE_PATH": "/var/data/elsewhere/storage"}, True),
        ({"DATA_NODE": "node-db"}, True),
    ],
}


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack,over,ok", [(s, o, k) for s, cases in GUARD_CASES.items() for o, k in cases])
def test_guard_fails_closed_on_local_path_without_data_node(stack, over, ok, env):
    assert _guard_passes(stack, env, **over) is ok


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_guard_runs_before_any_task(stack, env):
    p = deploy_play(stack, env)
    assert _guard(stack, env) in (p.get("pre_tasks") or [])


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_play_path_vars_default_to_the_old_paths(stack, env):
    e = _env()
    pv = deploy_play(stack, env)["vars"]
    for key, var in STACKS[stack]["path_vars"].items():
        for over in ({}, {key: ""}):
            got = e.from_string(pv[var]).render(app=stack, env=env, **{stack: over})
            assert got == STACKS[stack]["default"][key].format(env=env), (var, over)
        got = e.from_string(pv[var]).render(app=stack, env=env, **{stack: {key: "/srv/x"}})
        assert got == "/srv/x"


def _dir_tasks(stack, env):
    return [t for t in tasks(stack, env) if "file" in t
            and any(v in json.dumps(t) for v in STACKS[stack]["path_vars"].values())]


def _local_task(stack, env):
    hits = [t for t in _dir_tasks(stack, env) if t.get("delegate_to")]
    assert len(hits) == 1, (stack, env)
    return hits[0]


def _shared_task(stack, env):
    hits = [t for t in _dir_tasks(stack, env) if not t.get("delegate_to")]
    assert len(hits) == 1, (stack, env)
    return hits[0]


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_local_dirs_are_created_on_the_data_node_with_the_container_owner(stack, env):
    t = _local_task(stack, env)
    s = STACKS[stack]
    assert t["delegate_to"] == "{{ %s.DATA_NODE | default(inventory_hostname, true) }}" % stack
    assert "not " in str(t["when"]) and "startswith('/var/data/')" in str(t["when"])
    f = t["file"]
    assert (str(f["owner"]), str(f["group"]), str(f["mode"])) == (s["owner"], s["group"], s["mode"])
    assert f["state"] == "directory" and not f.get("recurse")


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_shared_storage_dirs_only_when_the_path_stays_on_var_data(stack, env):
    t = _shared_task(stack, env)
    s = STACKS[stack]
    assert "not " not in str(t["when"]) and "startswith('/var/data/')" in str(t["when"])
    f = t["file"]
    assert (str(f["owner"]), str(f["group"]), str(f["mode"])) == (s["owner"], s["group"], s["mode"])
    assert not f.get("recurse")


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_old_data_paths_are_not_created_unconditionally(stack, env):
    text = json.dumps(tasks(stack, env))
    for default in STACKS[stack]["default"].values():
        literal = default.replace("{env}", "{{ env }}").replace(stack, "{{ app }}", 1)
        assert literal not in text, literal


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_no_recursive_ownership_or_mode_changes(stack, env):
    for t in tasks(stack, env):
        if "file" in t:
            assert not t["file"].get("recurse"), t.get("name")
        assert "chown -R" not in json.dumps(t) and "chmod -R" not in json.dumps(t), t.get("name")


@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_data_node_delegate_renders_with_strict_undefined_when_unset(stack, env):
    """Ansible templates delegate_to before when (the sksso #125 bug)."""
    e = jinja2.Environment(undefined=jinja2.StrictUndefined)
    tpl = e.from_string(_local_task(stack, env)["delegate_to"])
    assert tpl.render(**{stack: {}}, inventory_hostname="mgr-1") == "mgr-1"
    assert tpl.render(**{stack: {"DATA_NODE": "node-db"}}, inventory_hostname="mgr-1") == "node-db"


# ---- real ansible-playbook: guard + directory tasks --------------------------

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _run(tmp_path, stack, env, over):
    """Run the play's own guard and data-dir tasks on localhost (owner/group
    dropped: tests do not run as root; created paths re-rooted under
    tmp_path, after the task's own when has seen the real path)."""
    root = tmp_path / "root"
    root.mkdir()
    picked = [_guard(stack, env), _shared_task(stack, env), _local_task(stack, env)]
    run_tasks = []
    for i, t in enumerate(picked):
        t = dict(t)
        if "file" in t:
            t["file"] = {k: v for k, v in t["file"].items() if k not in ("owner", "group")}
            t["file"]["path"] = str(root) + t["file"]["path"]
            t["register"] = f"r{i}"
        run_tasks.append(t)
    out = tmp_path / "result.json"
    run_tasks.append({"name": "record", "copy": {"dest": str(out), "content": "{{ {'shared': r1, 'local': r2} | to_json }}"}})
    pvars = dict(deploy_play(stack, env)["vars"])
    play = [{"hosts": "localhost", "gather_facts": False, "connection": "local",
             "vars": {**pvars, stack: dict(example_vars(stack), **over)}, "tasks": run_tasks}]
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump(play, sort_keys=False))
    env_vars = dict(os.environ, HOME=str(tmp_path), ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="False",
                    ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", str(pb)], capture_output=True, text=True,
                       env=env_vars)
    return r, (json.loads(out.read_text()) if out.exists() else None), root


@needs_ansible
@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_ansible_local_path_without_data_node_stops_the_play(tmp_path, stack, env):
    over = {k: v for k, v in LOCAL[stack].items() if k != "DATA_NODE"}
    r, res, root = _run(tmp_path, stack, env, over)
    assert r.returncode != 0
    assert "DATA_NODE" in r.stdout
    assert res is None and not any(root.iterdir())


@needs_ansible
@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_ansible_unset_knobs_create_only_the_shared_storage_dirs(tmp_path, stack, env):
    r, res, root = _run(tmp_path, stack, env, {})
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    for default in STACKS[stack]["default"].values():
        assert (root / default.format(env=env).lstrip("/")).is_dir()
    assert all(i.get("skipped") for i in res["local"].get("results", [res["local"]]))


@needs_ansible
@pytest.mark.parametrize("env", ENVS)
@pytest.mark.parametrize("stack", STACKS)
def test_ansible_local_paths_with_data_node_create_only_the_local_dirs(tmp_path, stack, env):
    over = dict(LOCAL[stack], DATA_NODE="localhost")
    r, res, root = _run(tmp_path, stack, env, over)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    for key in STACKS[stack]["mounts"]:
        assert (root / LOCAL[stack][key].lstrip("/")).is_dir()
    for default in STACKS[stack]["default"].values():
        assert not (root / default.format(env=env).lstrip("/")).exists()


# ---- docs ---------------------------------------------------------------------

@pytest.mark.parametrize("stack", STACKS)
def test_readme_documents_the_knobs_and_the_migration(stack):
    text = (OPTIONAL / stack / "README.md").read_text()
    for key in ["DATA_NODE", *STACKS[stack]["mounts"], "Migrating a running instance",
                "rsync -aHAX --numeric-ids", "STALE-moved-to-", "Never delete"]:
        assert key in text, key
    assert "\u2014" not in text and "\u2013" not in text
