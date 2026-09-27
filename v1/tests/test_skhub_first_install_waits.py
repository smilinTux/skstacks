"""skhub's first-install waits must fit a slow first boot.

Nextcloud has a Docker healthcheck, so Swarm only reports its task Running
once it is healthy, i.e. after the entrypoint copy and the first DB install.
On a memory-pressured host that took ~9 minutes (skstack06 v2.19.1), while
"Wait for the swarm node running the Nextcloud task" gave up after 5 minutes
(60 x 5s) and failed the play although Nextcloud finished fine. The
follow-up "Wait for Nextcloud to finish its first-run install" had the same
shape (60 x 10s). Both loops now derive their retries from one guarded knob,
skhub.first_install_wait_seconds (default 1800).
"""
import pathlib

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB.glob("deploy_skhub-*.yml"))
KNOB = "first_install_wait_seconds"
DEFAULT_BUDGET = 1800

_ENV = jinja2.Environment()


def _tasks(playbook):
    plays = yaml.safe_load(playbook.read_text())
    return [t for play in plays for t in play.get("tasks", [])]


def _shell(task):
    cmd = task.get("shell") or task.get("command") or ""
    return cmd if isinstance(cmd, str) else str(cmd)


def _waits(playbook):
    tasks = _tasks(playbook)
    node = [t for t in tasks if t.get("register") == "nextcloud_node"]
    installed = [t for t in tasks if t.get("register") == "nextcloud_installed"]
    assert len(node) == 1 and len(installed) == 1, playbook.name
    return {"node": node[0], "installed": installed[0]}


def _render_int(value, skhub):
    """Render an Ansible templated int field the way Ansible would."""
    if isinstance(value, int):
        return value
    text = str(value).strip()
    assert text.startswith("{{") and text.endswith("}}"), f"not templated: {value!r}"
    return int(_ENV.from_string(text).render(skhub=skhub))


def _budget(task, skhub):
    return _render_int(task.get("retries"), skhub) * _render_int(task.get("delay"), skhub)


def test_all_three_envs_present():
    assert len(PLAYBOOKS) == 3


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("which", ["node", "installed"])
def test_default_budget_covers_a_slow_first_install(playbook, which):
    task = _waits(playbook)[which]
    assert _budget(task, {}) >= DEFAULT_BUDGET


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("which", ["node", "installed"])
def test_budget_driven_by_the_knob(playbook, which):
    task = _waits(playbook)[which]
    assert KNOB in str(task.get("retries")), "retries must derive from the knob"
    assert "default(" in str(task.get("retries")), "the knob must be guarded"
    for seconds in (600, 3600):
        budget = _budget(task, {KNOB: seconds})
        delay = _render_int(task.get("delay"), {KNOB: seconds})
        assert seconds <= budget < seconds + delay, (which, seconds, budget)


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_node_wait_reports_current_task_state(playbook):
    """A slow install must be visibly progressing: each attempt emits the
    current task states, not just "no Running task yet"."""
    shell = _shell(_waits(playbook)["node"])
    assert "CurrentState" in shell
    assert "STATES" in shell and ">&2" in shell


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_budget_announced_before_waiting(playbook):
    tasks = _tasks(playbook)
    idx = tasks.index(_waits(playbook)["node"])
    prev = tasks[idx - 1]
    assert "debug" in prev and KNOB in str(prev["debug"]), (
        "announce the wait budget so a long wait is not mistaken for a hang"
    )
