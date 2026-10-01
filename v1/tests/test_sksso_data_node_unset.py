"""sksso deploys with DATA_NODE unset (the default) after the local-DB knobs.

The task that creates the local database directories has
`delegate_to: "{{ sksso.DATA_NODE }}"` and a `when` that skips it for paths
under /var/data. Ansible templates `delegate_to` for every loop item BEFORE
it evaluates `when`, so with DATA_NODE unset (every instance that did not
opt in) the task failed with "object of type 'dict' has no attribute
'DATA_NODE'" even though it would have been skipped, and the whole sksso
deploy stopped (found by the skstack06 v2.23.0 release run, stage sksso).
The Jinja-only tests in test_sksso_local_db.py could not see it: they render
with ChainableUndefined and never run Ansible.

This runs the playbook's own task, unchanged except for owner/group (tests
do not run as root), through real `ansible-playbook` on localhost for every
env: with the defaults it must be skipped, and with DATA_NODE plus local
paths set it must still create the directories on the data node.
"""
import os
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = {e: ROOT / f"v1/ansible/optional/sksso/deploy_sksso-{e}.yml" for e in ("dev", "staging", "prod")}
VAR_KEYS = ("sksso_postgres_data_path", "sksso_redis_data_path")


def _plays(env):
    return [p for p in yaml.safe_load(PLAYBOOKS[env].read_text()) if isinstance(p, dict)]


def local_dir_task(env):
    hits = [t for p in _plays(env) for t in p.get("tasks", []) or []
            if isinstance(t, dict) and "DATA_NODE" in str(t.get("delegate_to", ""))]
    assert len(hits) == 1, f"{env}: expected one task delegated to DATA_NODE, got {len(hits)}"
    return hits[0]


def play_vars(env):
    for p in _plays(env):
        v = p.get("vars") or {}
        if all(k in v for k in VAR_KEYS):
            return {k: v[k] for k in VAR_KEYS}
    raise AssertionError(f"{env}: play vars {VAR_KEYS} not found")


def run(tmp_path, env, sksso):
    task = dict(local_dir_task(env))
    task["file"] = {k: v for k, v in task["file"].items() if k not in ("owner", "group")}
    task["register"] = "local_dirs"
    out = tmp_path / "result.yml"
    tasks = [task, {"name": "record", "copy": {"dest": str(out), "content": "{{ local_dirs | to_yaml }}"}}]
    play = [{"hosts": "localhost", "gather_facts": False, "connection": "local",
             "vars": {"app": "sksso", "env": env, "sksso": sksso, **play_vars(env)}, "tasks": tasks}]
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump(play, sort_keys=False))
    env_vars = dict(os.environ, HOME=str(tmp_path), ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="False",
                    ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", str(pb)], capture_output=True, text=True, env=env_vars)
    return r, (yaml.safe_load(out.read_text()) if out.exists() else None)


needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


@needs_ansible
@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_default_vars_skip_the_local_dir_task_without_failing(tmp_path, env):
    r, res = run(tmp_path, env, {})
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert res and all(i.get("skipped") for i in res["results"]), res


@needs_ansible
@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_data_node_set_creates_the_local_dirs_on_it(tmp_path, env):
    pg, rd = tmp_path / "local/postgres", tmp_path / "local/redis"
    r, res = run(tmp_path, env, {"DATA_NODE": "localhost", "POSTGRES_DATA_PATH": str(pg), "REDIS_DATA_PATH": str(rd)})
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert pg.is_dir() and rd.is_dir()
    assert oct(pg.stat().st_mode & 0o777) == "0o700" and oct(rd.stat().st_mode & 0o777) == "0o700"


@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_delegate_to_renders_with_strict_undefined_when_data_node_is_unset(env):
    """No-Ansible guard for the same bug: the delegate_to template must not need DATA_NODE."""
    e = jinja2.Environment(undefined=jinja2.StrictUndefined)
    t = e.from_string(local_dir_task(env)["delegate_to"])
    assert t.render(sksso={}, inventory_hostname="mgr-1") == "mgr-1"
    assert t.render(sksso={"DATA_NODE": "node-db"}, inventory_hostname="mgr-1") == "node-db"
