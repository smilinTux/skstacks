"""skhub post-deploy steps must run on the node hosting the Nextcloud task.

Instances pin skhub to workers (skhub.placement_constraints), but the
post-deploy tasks used to run `docker ps` / `docker exec` on the play's
manager host. There was no Nextcloud container there, so the lookup came
back empty and every post-deploy step was silently skipped
(`when: ...length > 0` plus `ignore_errors: yes`): notify_push was never
installed and the notify_push service crash-looped on a missing binary.

The fix mirrors skstor (deploy_skstor-dev.yml): resolve the swarm node
running `<app>-<env>_nextcloud` via `docker service ps`, then
`delegate_to` that node for the container lookup and every `docker exec`.
"""
import pathlib

import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB.glob("deploy_skhub-*.yml"))
NODE_VAR = "nextcloud_node"


def _tasks(playbook):
    plays = yaml.safe_load(playbook.read_text())
    return [t for play in plays for t in play.get("tasks", [])]


def _shell(task):
    cmd = task.get("shell") or task.get("command") or ""
    return cmd if isinstance(cmd, str) else str(cmd)


def _truthy(value):
    return value is True or str(value).lower() in ("yes", "true")


def test_all_three_envs_present():
    assert [p.name for p in PLAYBOOKS] == [
        "deploy_skhub-dev.yml",
        "deploy_skhub-prod.yml",
        "deploy_skhub-staging.yml",
    ]


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_node_resolution_polls_service_ps(playbook):
    tasks = _tasks(playbook)
    resolvers = [t for t in tasks if t.get("register") == NODE_VAR]
    assert len(resolvers) == 1, f"{playbook.name}: expected one task registering {NODE_VAR}"
    task = resolvers[0]
    shell = _shell(task)
    assert "docker service ps" in shell
    assert "_nextcloud" in shell
    # Bounded poll until the task is actually running somewhere.
    assert NODE_VAR in str(task.get("until", ""))
    assert int(task.get("retries", 0)) >= 30
    assert int(task.get("delay", 0)) >= 1
    assert not _truthy(task.get("ignore_errors")), "node resolution must fail loudly"
    # Resolution must happen before the container lookup.
    idx = tasks.index(task)
    lookup = next(i for i, t in enumerate(tasks) if t.get("register") == "nextcloud_container")
    assert idx < lookup


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_every_docker_exec_is_delegated_to_nextcloud_node(playbook):
    tasks = [t for t in _tasks(playbook) if "docker exec" in _shell(t)]
    assert len(tasks) >= 5, f"{playbook.name}: expected the post-deploy docker exec tasks"
    missing = [t["name"] for t in tasks if NODE_VAR not in str(t.get("delegate_to", ""))]
    assert not missing, f"{playbook.name}: docker exec tasks not delegated: {missing}"


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_container_lookup_on_node_and_not_ignored(playbook):
    lookups = [t for t in _tasks(playbook) if t.get("register") == "nextcloud_container"]
    assert len(lookups) == 1
    task = lookups[0]
    assert NODE_VAR in str(task.get("delegate_to", ""))
    assert not _truthy(task.get("ignore_errors")), (
        "a missing Nextcloud container must fail the play, not skip post-deploy"
    )


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_notify_push_steps_not_ignored(playbook):
    """The steps whose silent failure caused the incident must fail loudly."""
    critical = [t for t in _tasks(playbook) if "notify_push" in t.get("name", "")]
    assert len(critical) >= 3
    ignored = [t["name"] for t in critical if _truthy(t.get("ignore_errors"))]
    assert not ignored, f"{playbook.name}: notify_push steps still ignore errors: {ignored}"


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_waits_for_first_run_install_before_occ(playbook):
    """On a fresh cluster the Nextcloud entrypoint is still installing when
    the task reaches Running; `occ app:enable` then only prints "Nextcloud is
    not installed". Now that those steps are fatal, poll `occ status` first."""
    tasks = _tasks(playbook)
    waits = [t for t in tasks if "occ status" in _shell(t) and "installed" in _shell(t)]
    assert len(waits) == 1, f"{playbook.name}: expected one wait-for-installed task"
    wait = waits[0]
    assert "until" in wait and int(wait.get("retries", 0)) >= 30
    assert not _truthy(wait.get("ignore_errors"))
    first_config = next(i for i, t in enumerate(tasks) if t.get("register") == "redis_config_result")
    assert tasks.index(wait) < first_config
