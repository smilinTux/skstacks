"""skgit's first-install waits must fit a slow first boot, and a requested
admin user must never be skipped silently.

Forgejo has a Docker healthcheck with start_period 300s, so Swarm reports its
task Running only once Forgejo answers, which on a busy host takes minutes.
Before this fix:

- "Wait for Forgejo container to start and fix permissions" polled 30 x 2s
  (and, as `{1..30}` under the shell module's /bin/sh, really only once),
  then ran the chown and start-runners.sh anyway under ignore_errors.
- "Create admin user" polled the API 60 x 2s (same /bin/sh caveat), then ran
  `forgejo admin user create ... || true` under ignore_errors + no_log, so a
  slow first boot skipped the admin user with no visible trace.

Now one guarded knob, skgit.first_install_wait_seconds (default 1800),
drives both waits (same pattern as skhub.first_install_wait_seconds), and
when skgit.admin_enabled asks for an admin user the play fails loudly,
without echoing the password, if it cannot be created.
"""
import os
import pathlib
import stat
import subprocess
import uuid

import jinja2
import pytest
import yaml

SKGIT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgit"
PLAYBOOKS = sorted(SKGIT.glob("deploy_skgit-*.yml"))
KNOB = "first_install_wait_seconds"
DEFAULT_BUDGET = 1800
PASSWORD = "test-only-" + uuid.uuid4().hex

_ENV = jinja2.Environment()


def _plays(playbook):
    return yaml.safe_load(playbook.read_text())


def _tasks(playbook):
    return [t for play in _plays(playbook) for t in play.get("tasks", []) or []]


def _pre_tasks(playbook):
    return [t for play in _plays(playbook) for t in play.get("pre_tasks", []) or []]


def _shell(task):
    cmd = task.get("shell") or task.get("command") or ""
    return cmd if isinstance(cmd, str) else str(cmd)


def _by_register(playbook, name):
    found = [t for t in _tasks(playbook) if t.get("register") == name]
    assert len(found) == 1, (playbook.name, name)
    return found[0]


def _admin_task(playbook):
    found = [
        t for t in _tasks(playbook)
        if "admin user" in t.get("name", "").lower()
        and "credentials provided" in t.get("name", "").lower()
    ]
    assert len(found) == 1, playbook.name
    return found[0]


def _when_text(task):
    when = task.get("when", "")
    return " ".join(when) if isinstance(when, list) else str(when)


def _render_int(value, skgit):
    if isinstance(value, int):
        return value
    text = str(value).strip()
    assert text.startswith("{{") and text.endswith("}}"), f"not templated: {value!r}"
    return int(_ENV.from_string(text).render(skgit=skgit))


def _budget(task, skgit):
    return _render_int(task.get("retries"), skgit) * _render_int(task.get("delay"), skgit)


WAITS = ["forgejo_node", "forgejo_api"]


def test_all_three_envs_present():
    assert len(PLAYBOOKS) == 3


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("which", WAITS)
def test_default_budget_covers_a_slow_first_install(playbook, which):
    assert _budget(_by_register(playbook, which), {}) >= DEFAULT_BUDGET


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("which", WAITS)
def test_budget_driven_by_the_knob(playbook, which):
    task = _by_register(playbook, which)
    assert KNOB in str(task.get("retries")), "retries must derive from the knob"
    assert "default(" in str(task.get("retries")), "the knob must be guarded"
    for seconds in (600, 3600):
        budget = _budget(task, {KNOB: seconds})
        delay = _render_int(task.get("delay"), {KNOB: seconds})
        assert seconds <= budget < seconds + delay, (which, seconds, budget)


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_node_wait_fails_the_play_and_reports_progress(playbook):
    task = _by_register(playbook, "forgejo_node")
    assert not task.get("ignore_errors"), "Forgejo never coming up must fail the play"
    shell = _shell(task)
    assert "CurrentState" in shell and ">&2" in shell


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_budget_announced_before_waiting(playbook):
    tasks = _tasks(playbook)
    prev = tasks[tasks.index(_by_register(playbook, "forgejo_node")) - 1]
    assert "debug" in prev and KNOB in str(prev["debug"])


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_permissions_and_runners_run_only_after_forgejo_is_up(playbook):
    tasks = _tasks(playbook)
    wait_idx = tasks.index(_by_register(playbook, "forgejo_node"))
    perm = [t for t in tasks if t.get("name") == "Fix Forgejo data permissions and start runners"]
    assert len(perm) == 1
    assert tasks.index(perm[0]) > wait_idx
    assert "start-runners.sh" in _shell(perm[0])


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_no_short_inline_wait_loops_left(playbook):
    for task in _tasks(playbook):
        shell = _shell(task)
        assert "{1.." not in shell, (task.get("name"), "use until/retries with the knob")


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_waits_run_on_the_forgejo_node(playbook):
    for task in (_by_register(playbook, "forgejo_api"), _admin_task(playbook)):
        assert "forgejo_node" in str(task.get("delegate_to")), task.get("name")


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_creation_failure_is_not_hidden(playbook):
    task = _admin_task(playbook)
    shell = _shell(task)
    assert not task.get("ignore_errors"), "ignore_errors hides a failed admin bootstrap"
    assert "|| true" not in shell, "`|| true` hides a failed admin bootstrap"
    assert task.get("no_log") is True, "the task carries the admin password"
    assert "admin_password" not in shell, "pass the password via environment, not the script text"
    assert (task.get("args") or {}).get("executable") == "/bin/bash"
    register = task.get("register")
    assert register

    tasks = _tasks(playbook)
    after = tasks[tasks.index(task) + 1:]
    fails = [t for t in after if "fail" in t and register in _when_text(t)]
    assert len(fails) == 1, "a visible fail task must follow the no_log admin task"
    fail = fails[0]
    assert not fail.get("no_log")
    assert not fail.get("ignore_errors")
    assert "admin_password" not in str(fail["fail"]), "the failure message must not carry the secret"
    assert "admin_enabled" in _when_text(fail)


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_requested_without_credentials_is_refused(playbook):
    refuse = [
        t for t in _pre_tasks(playbook)
        if "assert" in t and "admin_enabled" in str(t["assert"].get("that"))
    ]
    assert len(refuse) == 1
    that = str(refuse[0]["assert"]["that"])
    assert "admin_password" in that and "admin_email" in that


# --- behaviour of the rendered admin-create script -------------------------

FAKE_DOCKER = r"""#!/bin/bash
echo "$*" >> "$CALLS"
case "$*" in
  "ps "*) [ -n "$NO_CONTAINER" ] || echo abc123 ;;
  *"admin user list"*)
    printf 'ID   Username   Email              IsActive IsAdmin 2FA\n'
    [ -n "$USER_EXISTS" ] && printf '1    skadmin    admin@example.org  true     true    false\n'
    exit 0 ;;
  *"admin user create"*)
    case "$CREATE_MODE" in
      ok) echo "New user 'skadmin' has been successfully created!" ;;
      exists) echo "Command error: CreateUser: user already exists [name: skadmin]" >&2; exit 1 ;;
      *) echo "Command error: password $ADMIN_PASSWORD does not meet complexity" >&2; exit 1 ;;
    esac ;;
esac
"""


def _run_admin(tmp_path, playbook, **flags):
    task = _admin_task(playbook)
    skgit = {"admin_username": "skadmin", "admin_password": PASSWORD, "admin_email": "admin@example.org"}
    # The playbook resolves these facts once (set_fact, no_log) from either key spelling.
    ctx = {"app": "skgit", "env": "dev", "skgit": skgit,
           "skgit_admin_username": skgit["admin_username"],
           "skgit_admin_password": skgit["admin_password"],
           "skgit_admin_email": skgit["admin_email"]}
    script = _ENV.from_string(_shell(task)).render(**ctx)
    task_env = {k: _ENV.from_string(str(v)).render(**ctx) for k, v in (task.get("environment") or {}).items()}
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CALLS=str(calls), **task_env)
    for k, v in flags.items():
        env[k] = v
    r = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True)
    return r, (calls.read_text() if calls.exists() else "")


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_script_skips_existing_user(tmp_path, playbook):
    r, calls = _run_admin(tmp_path, playbook, USER_EXISTS="1")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "admin user create" not in calls


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_script_creates_missing_user(tmp_path, playbook):
    r, calls = _run_admin(tmp_path, playbook, CREATE_MODE="ok")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "admin user create" in calls
    assert "--must-change-password=false" in calls


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_script_tolerates_already_exists(tmp_path, playbook):
    r, _ = _run_admin(tmp_path, playbook, CREATE_MODE="exists")
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_script_fails_without_leaking_the_password(tmp_path, playbook):
    r, _ = _run_admin(tmp_path, playbook, CREATE_MODE="error")
    assert r.returncode != 0
    assert "complexity" in r.stdout, "the reason should reach the fail message"
    assert PASSWORD not in r.stdout + r.stderr


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_script_fails_without_a_container(tmp_path, playbook):
    r, _ = _run_admin(tmp_path, playbook, NO_CONTAINER="1")
    assert r.returncode != 0
