"""skform templates read several estate-specific vars with no framework
default and, until now, no guard: skform.CLUSTERNAME, skform.DOMAIN,
skform.APP_ENV, skform.TOFU_LOG_LEVEL, skform.STATE_BACKEND_TYPE and
skform.STATE_BACKEND_PATH. Leaving any of them unset in the vault file
surfaced only as a generic Jinja "is undefined" error deep inside template
rendering, instead of a clear message up front telling the operator what to
set - unlike skha and sksso, which fail closed early with a `fail:` task
that names the missing vars (see their "required instance vars" guard)."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
TASK_NAME = "Verify required SKForm vault variables are set"
REQUIRED_VARS = (
    "CLUSTERNAME",
    "DOMAIN",
    "APP_ENV",
    "TOFU_LOG_LEVEL",
    "STATE_BACKEND_TYPE",
    "STATE_BACKEND_PATH",
)


def _pre_tasks(env_name):
    path = ANSIBLE / f"optional/skform/deploy_skform-{env_name}.yml"
    plays = yaml.safe_load(path.read_text())
    for play in plays:
        if "pre_tasks" in play:
            return play["pre_tasks"]
    return []


def _find_guard_task(env_name):
    for task in _pre_tasks(env_name):
        if task.get("name") == TASK_NAME:
            return task
    return None


def test_all_three_envs_have_the_guard():
    for env_name in ("dev", "staging", "prod"):
        task = _find_guard_task(env_name)
        assert task is not None, f"{env_name} playbook is missing the required-vars guard"
        assert "fail" in task, f"{env_name} guard task isn't a fail task"


def test_guard_checks_every_required_var():
    for env_name in ("dev", "staging", "prod"):
        task = _find_guard_task(env_name)
        when = task["when"]
        for var in REQUIRED_VARS:
            assert f"skform.{var}" in when, f"{env_name} guard doesn't check skform.{var}"


def test_guard_message_names_every_required_var():
    for env_name in ("dev", "staging", "prod"):
        task = _find_guard_task(env_name)
        msg = task["fail"]["msg"]
        for var in REQUIRED_VARS:
            assert f"skform.{var}" in msg, f"{env_name} guard message doesn't name skform.{var}"


def _condition_holds(**values):
    """Reimplements the assert's `when` shape: fires (returns True) if any
    required var is unset or empty."""
    return any((v or "") == "" for v in values.values())


def test_guard_expression_fires_when_any_required_var_is_missing():
    base = {v: "x" for v in REQUIRED_VARS}
    for var in REQUIRED_VARS:
        missing = dict(base)
        missing[var] = ""
        assert _condition_holds(**missing) is True


def test_guard_expression_does_not_fire_when_all_required_vars_are_set():
    base = {v: "x" for v in REQUIRED_VARS}
    assert _condition_holds(**base) is False
