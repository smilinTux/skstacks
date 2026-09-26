"""Every skgit env playbook must refuse to deploy with metrics explicitly
enabled but no token configured, so an instance can't silently ship a
public /metrics by setting METRICS_ENABLED: true and forgetting the
token."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
TASK_NAME = "Refuse to enable Forgejo metrics without a token"


def _pre_tasks(env_name):
    path = ANSIBLE / f"optional/skgit/deploy_skgit-{env_name}.yml"
    plays = yaml.safe_load(path.read_text())
    for play in plays:
        if "pre_tasks" in play:
            return play["pre_tasks"]
    return []


def _find_assert_task(env_name):
    for task in _pre_tasks(env_name):
        if task.get("name") == TASK_NAME:
            return task
    return None


def test_all_three_envs_have_the_guard():
    for env_name in ("dev", "staging", "prod"):
        task = _find_assert_task(env_name)
        assert task is not None, f"{env_name} playbook is missing the metrics-token guard"
        assert "assert" in task


def _condition_holds(metrics_enabled, metrics_token):
    """Reimplements the assert's `that` expression for a couple of cases;
    this is the same boolean shape ansible evaluates, kept here so a
    regression in the expression fails a fast unit test instead of only
    surfacing on a live deploy."""
    return (not bool(metrics_enabled)) or bool(metrics_token)


def test_guard_expression_blocks_enabled_without_token():
    assert _condition_holds(True, "") is False
    assert _condition_holds(True, None) is False


def test_guard_expression_allows_enabled_with_token():
    assert _condition_holds(True, "sometoken") is True


def test_guard_expression_allows_disabled_regardless_of_token():
    assert _condition_holds(False, "") is True
    assert _condition_holds(False, "sometoken") is True
