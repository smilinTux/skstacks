"""provision_skpatch_hosts_nodes.yml: host registration lives in an
include_tasks that is skipped as a whole when registration is not configured.

ansible-core 2.19+ templates delegate_to before it evaluates `when`, so a
skipped task delegating to `selected_manager` (only set when registration
runs) failed with "'selected_manager' is undefined" on every provision run
WITHOUT registration (the v2.26.17 gate). A skipped include_tasks templates
nothing inside it. Happy path (the include and its condition), edge case
(nothing in the playbook itself delegates to selected_manager), failure
guard (the included file must not be an import, which would be static and
bring the bug back)."""
from pathlib import Path

import yaml

ROLE = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skpatch-hosts"
COND = "skp_reg_token | default('') | length > 0"


def _tasks(play):
    for t in play.get("tasks", []):
        yield t
        for key in ("block", "rescue", "always"):
            yield from t.get(key, [])


def test_registration_is_one_conditional_include():
    plays = yaml.safe_load((ROLE / "provision_skpatch_hosts_nodes.yml").read_text())
    incs = [t for p in plays for t in _tasks(p) if t.get("include_tasks") == "tasks/register_systems.yml"]
    assert len(incs) == 1 and COND in incs[0]["when"]
    assert not any(t.get("import_tasks") == "tasks/register_systems.yml" for p in plays for t in _tasks(p))


def test_nothing_in_the_playbook_delegates_to_selected_manager():
    text = (ROLE / "provision_skpatch_hosts_nodes.yml").read_text()
    assert "selected_manager" not in text


def test_included_file_selects_a_manager_then_registers_through_it():
    tasks = yaml.safe_load((ROLE / "tasks" / "register_systems.yml").read_text())
    names = [t["name"] for t in tasks]
    assert names[0] == "Select manager node to reach the dashboard's overlay network"
    assert (ROLE / "tasks" / tasks[0]["import_tasks"]).resolve().is_file()
    delegating = [t for t in tasks if t.get("delegate_to") == "{{ selected_manager }}"]
    assert {t["name"] for t in delegating} >= {"Stage the systems-sync script on the selected manager",
                                               "Register this run's target nodes as dashboard systems (idempotent, never deletes)"}
