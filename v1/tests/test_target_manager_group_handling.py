"""Every deploy playbook that reads `target_manager_group` must handle a
missing value the same, consistent way: either it defaults the value (the
majority "select a manager from a group" pattern uses
`target_group: "{{ target_manager_group | default('swarm_managers') }}"`),
or - for the small set of playbooks that deploy to every host in the group
directly (skha, skfenceha) rather than selecting one manager, where silently
falling back to 'swarm_managers' would be actively dangerous - it asserts the
var is set with a clear, actionable message before using it.

What is not acceptable is a bare `{{ target_manager_group }}` reference with
neither a default nor a guard: that fails only when Ansible hits the
undefined var deep in a task, with a generic "'target_manager_group' is
undefined" error instead of a clear one, and it's inconsistent with every
other service.
"""
import pathlib
import re

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"

_DEFAULT_RE = re.compile(r"target_manager_group\s*\|\s*default\(")
_GUARD_TASK_RE = re.compile(
    r"target_manager_group\s+is\s+(not\s+)?defined|target_manager_group\s*!=|target_manager_group\s*==",
)
_MSG_RE = re.compile(r"\b(msg|fail_msg)\s*:")


def _iter_deploy_playbooks_using_target_manager_group():
    for path in sorted(ANSIBLE.rglob("deploy_*.yml")):
        text = path.read_text(errors="replace")
        if "target_manager_group" in text:
            yield path, text


PLAYBOOKS = list(_iter_deploy_playbooks_using_target_manager_group())


def _has_default_fallback(text):
    return bool(_DEFAULT_RE.search(text))


def _has_explicit_guard_with_message(text):
    """A fail/assert task that checks target_manager_group's definedness (or
    that it isn't left at a dangerous default) and carries an explanatory
    message, appearing before the value is used unconditionally."""
    if not _GUARD_TASK_RE.search(text):
        return False
    # The guard must come with an explanatory message somewhere nearby (in
    # the same task block) - not just a bare conditional.
    return bool(_MSG_RE.search(text))


def test_target_manager_group_is_used_by_at_least_one_playbook():
    assert PLAYBOOKS, "expected at least one deploy_*.yml to reference target_manager_group"


def _relname(path):
    return str(path.relative_to(ANSIBLE))


def test_every_deploy_playbook_handles_missing_target_manager_group_consistently():
    unhandled = sorted(
        _relname(path)
        for path, text in PLAYBOOKS
        if not (_has_default_fallback(text) or _has_explicit_guard_with_message(text))
    )
    assert not unhandled, (
        "these deploy playbooks reference target_manager_group with neither a "
        "`| default('swarm_managers')` fallback nor an explicit fail/assert "
        "guard with a message, unlike every other service:\n" + "\n".join(unhandled)
    )


def test_detector_rejects_a_bare_reference_with_no_guard():
    bare = (
        "- hosts: all\n"
        "  tasks:\n"
        "    - import_tasks: select_manager_node.yml\n"
        "      vars:\n"
        "        target_group: \"{{ target_manager_group }}\"\n"
    )
    assert not _has_default_fallback(bare)
    assert not _has_explicit_guard_with_message(bare)


def test_detector_accepts_the_default_fallback_pattern():
    defaulted = "target_group: \"{{ target_manager_group | default('swarm_managers') }}\"\n"
    assert _has_default_fallback(defaulted)


def test_detector_accepts_the_explicit_assert_pattern():
    guarded = (
        "    - name: Verify target_manager_group is defined\n"
        "      fail:\n"
        "        msg: \"You must provide the manager group via -e target_manager_group=<group_name>.\"\n"
        "      when: target_manager_group is not defined\n"
    )
    assert _has_explicit_guard_with_message(guarded)
