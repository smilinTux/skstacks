"""skhub's first-install waits must fit a slow first boot.

Nextcloud has a Docker healthcheck, so Swarm only reports its task Running
once it is healthy, i.e. after the entrypoint copy and the first DB install.
On a memory-pressured host that took ~9 minutes (skstack06 v2.19.1), while
"Wait for the swarm node running the Nextcloud task" gave up after 5 minutes
(60 x 5s) and failed the play although Nextcloud finished fine. The
follow-up "Wait for Nextcloud to finish its first-run install" had the same
shape (60 x 10s). Both loops now derive their retries from one guarded knob,
skhub.first_install_wait_seconds (default 1800).

That fixed the ansible-side patience, but left a deeper trap: the
nextcloud service's own Docker HEALTHCHECK start_period was still a fixed
300s (interval 120s x retries 3 = 660s total grace). Swarm's
restart_policy kills and restarts a container whose healthcheck has not
yet passed once that grace runs out ("task: non-zero exit (137):
dockerexec: unhealthy container"), and each restart resets the one-time
entrypoint copy + first DB install back to zero. On a busy host (skstack06
gate, 2026-10-17/18, host load 28-37 on 20 cores) the container was killed
and restarted every ~9-11 minutes, every time, and NEVER accumulated
enough uninterrupted time to finish: the ansible wait loop faithfully
waited its full ~1800s budget while Docker killed the container two or
three times underneath it. Same root class as skmail's
HEALTHCHECK_START_PERIOD (default 1800s, same reasoning, see
skmail.yml.j2). Fix: the nextcloud healthcheck's start_period now derives
from the SAME skhub.first_install_wait_seconds knob, so the one place that
actually gates "does the container survive long enough to finish" has the
same budget as the ansible wait that was already designed around it.
"""
import pathlib

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
PLAYBOOKS = sorted(SKHUB.glob("deploy_skhub-*.yml"))
SKHUB_YML = SKHUB / "src/config/skhub/skhub.yml.j2"
KNOB = "first_install_wait_seconds"
DEFAULT_BUDGET = 1800

_ENV = jinja2.Environment()


def _render_yml(**skhub_extra):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    rendered = env.from_string(SKHUB_YML.read_text()).render(
        app="skhub", env="prod",
        skhub={"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", **skhub_extra},
        fence_service_name="skfenceha",
    )
    return yaml.safe_load(rendered)


def _nextcloud_healthcheck(**skhub_extra):
    return _render_yml(**skhub_extra)["services"]["nextcloud"]["healthcheck"]


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


# --- the Docker healthcheck must not kill the container before the ansible
#     wait's own budget runs out (the actual bug: start_period was a fixed
#     300s while the ansible side already waited up to 1800s) -------------

def test_default_healthcheck_start_period_matches_the_knobs_default():
    hc = _nextcloud_healthcheck()
    assert hc["start_period"] == f"{DEFAULT_BUDGET}s"


def test_healthcheck_start_period_driven_by_the_knob():
    for seconds in (600, 3600):
        hc = _nextcloud_healthcheck(**{KNOB: seconds})
        assert hc["start_period"] == f"{seconds}s"


def test_healthcheck_start_period_covers_the_ansible_wait_budget():
    """The two must never drift apart again: whatever the ansible wait is
    willing to wait for, the container must still be alive to finish in."""
    for seconds in (300, 600, 1800, 3600):
        hc = _nextcloud_healthcheck(**{KNOB: seconds})
        start_period = int(hc["start_period"].rstrip("s"))
        for playbook in PLAYBOOKS:
            task = _waits(playbook)["node"]
            ansible_budget = _budget(task, {KNOB: seconds})
            assert start_period >= min(seconds, ansible_budget), (playbook.name, seconds)


def test_healthcheck_interval_and_retries_unchanged():
    """Only start_period changes; the steady-state detection cadence (once
    healthy) is a separate concern and was never the problem."""
    hc = _nextcloud_healthcheck()
    assert hc["interval"] == "120s"
    assert hc["timeout"] == "90s"
    assert hc["retries"] == 3
    assert hc["test"] == ["CMD-SHELL", "curl -sf --max-time 60 http://localhost/status.php || exit 1"]
