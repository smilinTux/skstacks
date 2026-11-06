"""sksec-web's tasks/patch_app_config_yaml.yml must survive the same two
scenarios test_sksec_web_register_lapi_machine_guard.py already guards for
register_lapi_machine.yml:

1. A real `ansible-playbook --check` dry run.
2. skstack06's `tools/dry_render.py`, which drops every shell/command task
   outright (dry_render.py's own drop rule), including the node-resolution
   lookup this task file's every `delegate_to` depends on.

Found while writing this task file, 2026-10-28, before it was ever run
through dry_render.py for real: every `delegate_to:
"{{ sksw_app_node.stdout | trim }}"` dereferenced `.stdout` with no
`default(...)` guard. Under the drop rule, `sksw_app_node` is undefined, so
Ansible raises "has no attribute 'stdout'" while templating delegate_to --
*before* any task's own `when:` gets a chance to skip it, same failure
shape as the register_lapi_machine bug this mirrors. Fixed with the same
`| default(inventory_hostname)` pattern used for skpulse_node and
sksw_crowdsec_node: a delegate_to that would just resolve to the controller
is still a valid host, so it never raises, and the real `when:` conditions
on every dependent task's body still correctly skip it on that path -- the
fallback value is never actually used for anything.
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = ROOT / "v1" / "ansible" / "optional" / "sksec-web" / "tasks" / "patch_app_config_yaml.yml"

READONLY_LOOKUPS = {
    "Find the swarm node running the {{ app }} task (for the config.yaml patch)",
    "Check whether the app's config.yaml exists yet (not yet on first install -- env vars seed it)",
    "Read the existing config.yaml",
}
EXPECTED_DROPPED_REGISTERS = {"sksw_app_node", "sksw_app_config_patch", "sksw_app_config_backup"}


def _tasks():
    return yaml.safe_load(TASK_FILE.read_text())


def test_readonly_lookups_stay_check_mode_false():
    """The node-resolution lookup, the config.yaml stat, and the slurp read
    must stay `check_mode: false` -- same convention as
    shared/tasks/select_manager_node.yml and register_lapi_machine.yml's own
    read-only lookups -- so a real `--check --diff` reports live state
    instead of skipping them."""
    by_name = {t["name"]: t for t in _tasks()}
    for name in READONLY_LOOKUPS:
        assert by_name[name].get("check_mode") is False, f"{name} must be check_mode: false"


def test_every_delegate_to_guards_against_undefined_node():
    """No `delegate_to` in this file may dereference `sksw_app_node.stdout`
    without a `default(...)` guard -- that is exactly the bug this file
    guards against (see module docstring)."""
    for t in _tasks():
        dt = t.get("delegate_to")
        if isinstance(dt, str) and "sksw_app_node" in dt:
            assert "default(" in dt, f"{t['name']}: delegate_to {dt!r} has no default() guard"


def test_changed_detection_never_uses_a_substring_match():
    """Real bug, found live 2026-10-28 on the kept skstack06 cluster: a
    `'CHANGED' in sksw_app_config_patch.stdout` check is ALWAYS true,
    because the string "UNCHANGED" itself contains "CHANGED" as a
    contiguous substring. That silently turned every genuinely-unchanged
    run into a false "changed", which backed up the real config.yaml, then
    overwrote it with an empty tempfile (the patch script only writes
    SKSW_OUT_FILE inside its own `if changed:` branch -- when the script's
    real answer was UNCHANGED, the tempfile was never written and stayed
    at the empty state `tempfile:` created it in). Caught by live
    validation before this role was ever committed; the file was restored
    from its own timestamped backup. Every comparison against this
    register's stdout must use an exact match, never `in`."""
    import re
    for t in _tasks():
        for field in ("changed_when", "when"):
            val = t.get(field)
            if not isinstance(val, str) or "sksw_app_config_patch" not in val:
                continue
            assert not re.search(r"'CHANGED'\s+in\s", val), (
                f"{t['name']}: {field} {val!r} uses a substring match against "
                "'CHANGED', which 'UNCHANGED' also contains -- use == instead"
            )
            assert "== 'CHANGED'" in val, f"{t['name']}: {field} {val!r} must use an exact == 'CHANGED' comparison"


def test_force_restart_and_converge_never_delegate_to_the_worker():
    """docker service update / docker service ls are swarm CONTROL-PLANE
    commands and must run on this play's own manager host (hosts:
    selected_manager_group), never delegated to sksw_app_node (the worker
    running the app's own task/data). Real bug, found live 2026-10-28:
    delegating the force-restart to the worker failed immediately with
    "This node is not a swarm manager." """
    for t in _tasks():
        if t["name"].startswith("Force-restart") or t["name"].startswith("Wait for"):
            assert "delegate_to" not in t, f"{t['name']}: must not delegate_to (runs on the manager play host)"


def _drop_shell_and_command(tasks):
    """skstack06 tools/dry_render.py's own drop rule: every shell/command
    task is removed outright, along with the register it would have set."""
    survivors, dropped_registers = [], set()
    for t in tasks:
        module = "shell" if "shell" in t else ("command" if "command" in t else None)
        if module:
            if t.get("register"):
                dropped_registers.add(t["register"])
            continue
        survivors.append(t)
    return survivors, dropped_registers


def test_dropping_shell_and_command_tasks_drops_the_expected_registers():
    _, dropped = _drop_shell_and_command(_tasks())
    assert dropped == EXPECTED_DROPPED_REGISTERS


def test_survives_the_dry_render_path(tmp_path):
    """Reproduced the same way as the register_lapi_machine guard test: run
    only the tasks that survive dry_render.py's drop rule, with the
    registers those dropped tasks would have set deliberately absent (not
    merely empty) from the starting vars, through real ansible-playbook."""
    survivors, dropped = _drop_shell_and_command(_tasks())
    assert dropped == EXPECTED_DROPPED_REGISTERS
    for t in survivors:
        assert t.get("register") not in dropped

    playbook = [{
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {
            "app": "sksec-web",
            "env": "dev",
            "sksw_data_path": "/var/data/sksec-web-dev/data",
            "sksec_web": {"READ_ONLY": False, "TZ": "UTC", "SYNC_LOOKBACK": "168h", "LAPI_USERNAME": "sksec-web"},
            "sksw_lapi_url": "http://sksec-dev_crowdsec:8080",
        },
        "tasks": survivors,
    }]
    pb_path = tmp_path / "playbook.yml"
    pb_path.write_text(yaml.safe_dump(playbook, sort_keys=False))
    result = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(pb_path)],
        capture_output=True, text=True, timeout=60)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "is undefined" not in out, out
    assert "has no attribute" not in out, out
    assert "failed=0" in out
