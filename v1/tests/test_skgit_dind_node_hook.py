"""start-runners.sh fails closed (exit 0, no runners) when SKGIT_DIND_NODE is
unset in its environment. The framework's deploy playbooks call the script
from a `shell` task without ever setting that variable, so a deploy from the
public framework silently never starts runners on ANY node, even when an
instance vault sets `skgit.DIND_NODE`.

The fix is a per-task `environment:` block passing SKGIT_DIND_NODE through
from `skgit.DIND_NODE` (default '', which preserves the script's existing
fail-safe skip when an instance hasn't set it).
"""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKGIT_PLAYBOOKS = sorted(ANSIBLE.glob("optional/skgit/deploy_skgit-*.yml"))


def _task_invoking_start_runners(playbook_path):
    plays = yaml.safe_load(playbook_path.read_text())
    for play in plays:
        if not isinstance(play, dict):
            continue
        for task in play.get("tasks", []) or []:
            shell = task.get("shell", "")
            if isinstance(shell, str) and "start-runners.sh" in shell:
                return task
    return None


def test_skgit_playbooks_exist():
    assert SKGIT_PLAYBOOKS, "expected deploy_skgit-{dev,staging,prod}.yml under optional/skgit"


def test_start_runners_task_found_in_every_skgit_playbook():
    for p in SKGIT_PLAYBOOKS:
        task = _task_invoking_start_runners(p)
        assert task is not None, f"{p.name}: no shell task invokes start-runners.sh"


def test_start_runners_task_passes_skgit_dind_node_env():
    for p in SKGIT_PLAYBOOKS:
        task = _task_invoking_start_runners(p)
        env = task.get("environment", {})
        assert "SKGIT_DIND_NODE" in env, (
            f"{p.name}: start-runners.sh task has no SKGIT_DIND_NODE in its "
            f"environment, so it always runs unset regardless of the vault"
        )
        assert "skgit.DIND_NODE" in env["SKGIT_DIND_NODE"], (
            f"{p.name}: SKGIT_DIND_NODE is not sourced from skgit.DIND_NODE"
        )
        assert "default(" in env["SKGIT_DIND_NODE"], (
            f"{p.name}: SKGIT_DIND_NODE must default to empty so the script's "
            f"fail-safe skip still applies when an instance hasn't set it"
        )
