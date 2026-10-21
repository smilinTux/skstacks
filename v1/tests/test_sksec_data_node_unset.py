"""sksec deploys with DATA_NODE unset (the default) after the local LAPI db
knobs.

The task that creates the local CrowdSec db directory has
`delegate_to: "{{ sksec.DATA_NODE }}"` and a `when` that skips it for paths
under /var/data. Ansible templates `delegate_to` for every loop item BEFORE
it evaluates `when`, so with DATA_NODE unset (every instance that did not
opt in) a naive template would fail with "object of type 'dict' has no
attribute 'DATA_NODE'" even though the task would have been skipped --
exactly the bug the skstack06 v2.23.0 release run found in sksso's own
DATA_NODE task (test_sksso_data_node_unset.py). This mirrors that test for
sksec's crowdsec LAPI db task.

This runs the playbook's own task, unchanged except for owner/group (tests
do not run as root), through real `ansible-playbook` on localhost for every
env: with the defaults it must be skipped, and with DATA_NODE plus a local
path set it must still create the directory on the data node.
"""
import os
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PLAYBOOKS = {e: ROOT / f"v1/ansible/core/sksec/deploy_sksec-{e}.yml" for e in ("dev", "staging", "prod")}


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
        if "sksec_crowdsec_db_path" in v:
            return {"sksec_crowdsec_db_path": v["sksec_crowdsec_db_path"]}
    raise AssertionError(f"{env}: play var sksec_crowdsec_db_path not found")


def run(tmp_path, env, sksec):
    task = dict(local_dir_task(env))
    task["file"] = {k: v for k, v in task["file"].items() if k not in ("owner", "group")}
    task["register"] = "local_dir"
    out = tmp_path / "result.yml"
    tasks = [task, {"name": "record", "copy": {"dest": str(out), "content": "{{ local_dir | to_yaml }}"}}]
    play = [{"hosts": "localhost", "gather_facts": False, "connection": "local",
             "vars": {"app": "sksec", "env": env, "sksec": sksec, **play_vars(env)}, "tasks": tasks}]
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
    # Unlike sksso's loop-based task (two paths: postgres + redis, so a
    # skip registers {"results": [...]}), this task has a single path and
    # no loop, so a skip registers {"skipped": true} directly.
    assert res and res.get("skipped"), res


@needs_ansible
@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_data_node_set_creates_the_local_dir_on_it(tmp_path, env):
    db = tmp_path / "local/crowdsec-db"
    r, res = run(tmp_path, env, {"DATA_NODE": "localhost", "CROWDSEC_DB_PATH": str(db)})
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert db.is_dir()
    assert oct(db.stat().st_mode & 0o777) == "0o750"


@pytest.mark.parametrize("env", list(PLAYBOOKS))
def test_delegate_to_renders_with_strict_undefined_when_data_node_is_unset(env):
    """No-Ansible guard for the same bug: the delegate_to template must not need DATA_NODE."""
    e = jinja2.Environment(undefined=jinja2.StrictUndefined)
    t = e.from_string(local_dir_task(env)["delegate_to"])
    assert t.render(sksec={}, inventory_hostname="mgr-1") == "mgr-1"
    assert t.render(sksec={"DATA_NODE": "node-db"}, inventory_hostname="mgr-1") == "node-db"
