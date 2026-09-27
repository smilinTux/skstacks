"""skstor's Garage replica wait must fit a slow first boot.

"Wait for Garage service to report 1/1 replicas" gave up after 1 minute
(12 x 5s) and failed the play, although a first install (image pull plus
Garage's own start) on a busy host routinely takes longer. Its retries now
derive from one guarded knob, skstor.first_install_wait_seconds (default
600), the same pattern as skhub.first_install_wait_seconds.
"""
import pathlib

import jinja2
import pytest
import yaml

SKSTOR = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skstor"
PLAYBOOKS = sorted(SKSTOR.glob("deploy_skstor-*.yml"))
KNOB = "first_install_wait_seconds"
DEFAULT_BUDGET = 600

_ENV = jinja2.Environment()


def _tasks(playbook):
    plays = yaml.safe_load(playbook.read_text())
    return [t for play in plays for t in play.get("tasks", [])]


def _wait(playbook):
    found = [t for t in _tasks(playbook) if t.get("register") == "garage_replicas"]
    assert len(found) == 1, playbook.name
    return found[0]


def _render_int(value, skstor):
    if isinstance(value, int):
        return value
    text = str(value).strip()
    assert text.startswith("{{") and text.endswith("}}"), f"not templated: {value!r}"
    return int(_ENV.from_string(text).render(skstor=skstor))


def _budget(task, skstor):
    return _render_int(task.get("retries"), skstor) * _render_int(task.get("delay"), skstor)


def test_all_three_envs_present():
    assert len(PLAYBOOKS) == 3


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_default_budget_covers_a_slow_first_install(playbook):
    assert _budget(_wait(playbook), {}) >= DEFAULT_BUDGET


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_budget_driven_by_the_knob(playbook):
    task = _wait(playbook)
    assert KNOB in str(task.get("retries")), "retries must derive from the knob"
    assert "default(" in str(task.get("retries")), "the knob must be guarded"
    for seconds in (120, 1800):
        budget = _budget(task, {KNOB: seconds})
        delay = _render_int(task.get("delay"), {KNOB: seconds})
        assert seconds <= budget < seconds + delay, (seconds, budget)


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_wait_reports_progress_on_stderr(playbook):
    shell = str(_wait(playbook).get("shell"))
    assert ">&2" in shell, "each attempt should report the current state on stderr"


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_budget_announced_before_waiting(playbook):
    tasks = _tasks(playbook)
    prev = tasks[tasks.index(_wait(playbook)) - 1]
    assert "debug" in prev and KNOB in str(prev["debug"]), (
        "announce the wait budget so a long wait is not mistaken for a hang"
    )
