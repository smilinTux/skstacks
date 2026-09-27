"""skgit's admin-bootstrap task was named "Create admin user if credentials
provided (DEV/STAGING ONLY)" in all three env playbooks, including prod -
but its `when` clause only checks `skgit.admin_enabled`/admin_password/
admin_email, with no environment condition at all. The label claimed a
restriction the task doesn't enforce: set skgit.admin_enabled in the prod
vault and it creates a real admin account in prod, contradicting its own
name. The task's name must describe what actually gates it (the opt-in
flag), not an environment restriction it doesn't have."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"


def _tasks(env_name):
    path = ANSIBLE / f"optional/skgit/deploy_skgit-{env_name}.yml"
    plays = yaml.safe_load(path.read_text())
    all_tasks = []
    for play in plays:
        all_tasks.extend(play.get("tasks", []) or [])
        all_tasks.extend(play.get("pre_tasks", []) or [])
    return all_tasks


def _find_admin_bootstrap_task(env_name):
    for task in _tasks(env_name):
        name = task.get("name", "")
        if "admin user" in name.lower() and "credentials provided" in name.lower():
            return task
    return None


def test_admin_bootstrap_task_exists_in_all_three_envs():
    for env_name in ("dev", "staging", "prod"):
        task = _find_admin_bootstrap_task(env_name)
        assert task is not None, f"{env_name} playbook is missing the admin-bootstrap task"


def test_admin_bootstrap_when_has_no_environment_condition():
    """Guards the premise: if this ever changes to genuinely gate on
    env != 'prod', the label test below would need to change too."""
    for env_name in ("dev", "staging", "prod"):
        task = _find_admin_bootstrap_task(env_name)
        when = task["when"]
        when_text = " ".join(when) if isinstance(when, list) else str(when)
        assert "env" not in when_text, (
            f"{env_name}: admin-bootstrap `when` now references env - the "
            f"task name should be revisited to match"
        )


def test_admin_bootstrap_task_name_does_not_claim_an_env_restriction_it_lacks():
    for env_name in ("dev", "staging", "prod"):
        task = _find_admin_bootstrap_task(env_name)
        name = task["name"]
        assert "DEV/STAGING ONLY" not in name.upper(), (
            f"{env_name}: admin-bootstrap task name still claims a "
            f"DEV/STAGING-only restriction it doesn't enforce:\n{name}"
        )


def test_admin_bootstrap_task_name_is_identical_across_envs():
    names = {env_name: _find_admin_bootstrap_task(env_name)["name"] for env_name in ("dev", "staging", "prod")}
    assert len(set(names.values())) == 1, f"admin-bootstrap task name drifted between envs: {names}"
