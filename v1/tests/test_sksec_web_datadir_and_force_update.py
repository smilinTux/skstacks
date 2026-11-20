"""sksec-web deploy regressions from issues #446 and #464.

#446: the deploy pinned the app's data dir to root:root 0700 on every run.
The image's entrypoint chowns /app/data to its unprivileged user at startup,
so a redeploy took the dir back from a running app ("unable to open database
file") until a forced restart. The dir may be created root 0700, but only
when it is missing: an existing dir's owner and mode are the entrypoint's.

#464: the config.yaml force-restart can hit Swarm's transient "update out of
sequence" right after the deploy's own update converged. It must retry.
"""
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
ROLE = ROOT / "v1" / "ansible" / "optional" / "sksec-web"
PLAYBOOKS = sorted(ROLE.glob("deploy_sksec-web-*.yml"))
TASK_FILE = ROLE / "tasks" / "patch_app_config_yaml.yml"


def _all_tasks(plays):
    for play in plays:
        for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
            for t in play.get(section) or []:
                yield t
                for inner in ("block", "rescue", "always"):
                    yield from t.get(inner) or []


def _when_list(task):
    w = task.get("when", [])
    return w if isinstance(w, list) else [w]


def test_three_env_playbooks_found():
    assert {p.name for p in PLAYBOOKS} == {
        "deploy_sksec-web-dev.yml", "deploy_sksec-web-prod.yml", "deploy_sksec-web-staging.yml",
    }


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_data_dir_owner_only_set_when_missing(playbook):
    tasks = list(_all_tasks(yaml.safe_load(playbook.read_text())))
    on_data_path = [
        t for t in tasks
        if isinstance(t.get("file"), dict) and t["file"].get("path") == "{{ sksw_data_path }}"
    ]
    assert on_data_path, "expected the data dir creation tasks"
    stat = [t for t in tasks if isinstance(t.get("stat"), dict) and t["stat"].get("path") == "{{ sksw_data_path }}"]
    assert len(stat) == 1 and stat[0].get("register") == "sksw_data_dir_stat"
    assert stat[0].get("check_mode") is False
    for t in on_data_path:
        f = t["file"]
        if "owner" in f or "group" in f or "mode" in f:
            assert any("sksw_data_dir_stat.stat.exists" in str(w) and "not" in str(w) for w in _when_list(t)), (
                f"{t['name']}: sets owner/mode on {{{{ sksw_data_path }}}} without the missing-dir guard (#446)"
            )


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_stat_runs_where_the_dir_is_created(playbook):
    """The stat must look on the same host the local-dir task creates on
    (DATA_NODE), or it would report 'missing' and re-chown every run."""
    tasks = list(_all_tasks(yaml.safe_load(playbook.read_text())))
    stat = next(t for t in tasks if t.get("register") == "sksw_data_dir_stat")
    assert "sksec_web.DATA_NODE" in stat.get("delegate_to", "")


def test_force_update_retries_on_transient_swarm_errors():
    tasks = yaml.safe_load(TASK_FILE.read_text())
    force = [t for t in tasks if "docker service update --force" in str(t.get("shell", ""))]
    assert len(force) == 1
    t = force[0]
    assert t.get("register") == "sksw_app_force_update"
    assert "sksw_app_force_update.rc == 0" in str(t.get("until"))
    assert int(t.get("retries", 0)) >= 3
    assert int(t.get("delay", 0)) >= 1
