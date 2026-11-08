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
    # never the shared select_manager_node.yml: it filters managers by the
    # play's -l hosts, and this play's hosts are the nodes being registered
    assert not any("select_manager_node" in str(t.get("import_tasks", "")) for t in tasks)
    assert "groups[skpatch_hosts_register_manager_group" in tasks[0]["set_fact"]["skp_reg_managers"]
    assert names.index("Use the first swarm manager that answered") < names.index(
        "Stage the systems-sync script on the selected manager")
    delegating = [t for t in tasks if t.get("delegate_to") == "{{ selected_manager }}"]
    assert {t["name"] for t in delegating} >= {"Stage the systems-sync script on the selected manager",
                                               "Register this run's target nodes as dashboard systems (idempotent, never deletes)"}


def test_manager_selection_runs_under_real_ansible_with_a_limited_play(tmp_path):
    """The play's hosts are only the node being registered (as with -l); the
    selection must still find a manager from the group, and skip one that
    does not answer."""
    import shutil
    import subprocess
    import pytest
    if shutil.which("ansible-playbook") is None:
        pytest.skip("ansible-playbook not installed")
    tasks = yaml.safe_load((ROLE / "tasks" / "register_systems.yml").read_text())
    stop = [t["name"] for t in tasks].index("Fail when no swarm manager answered") + 1
    sel = tmp_path / "select.yml"
    sel.write_text(yaml.safe_dump(tasks[:stop]))
    inv = tmp_path / "inv.ini"
    inv.write_text("[swarm_managers]\nmgr-down ansible_host=127.0.0.1 ansible_port=1 ansible_connection=ssh\n"
                   "mgr-up ansible_connection=local\n[targets]\nnode-1 ansible_connection=local\n")
    play = tmp_path / "play.yml"
    play.write_text(yaml.safe_dump([{"hosts": "targets", "gather_facts": False, "tasks": [
        {"include_tasks": str(sel)},
        {"debug": {"msg": "SELECTED={{ selected_manager }}"}}]}]))
    proc = subprocess.run(["ansible-playbook", "-i", str(inv), str(play)], capture_output=True, text=True,
                          timeout=180, env={**__import__("os").environ, "ANSIBLE_HOST_KEY_CHECKING": "false"})
    assert proc.returncode == 0, proc.stdout[-3000:]
    assert "SELECTED=mgr-up" in proc.stdout
