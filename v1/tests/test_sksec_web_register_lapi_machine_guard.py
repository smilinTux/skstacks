"""sksec-web's tasks/register_lapi_machine.yml must survive two scenarios
where the three read-only shell/command lookups
(sksw_crowdsec_node/sksw_crowdsec_container/sksw_lapi_machines) never run
and their registers are entirely undefined, not merely empty:

1. A real `ansible-playbook --check` dry run (the three lookups are
   `check_mode: false`, same convention as shared/tasks/select_manager_node.yml,
   so in a real run they execute anyway; this test instead removes the
   registers outright to prove the downstream set_fact survives even a
   total lookup failure, not just confirm the check_mode markers exist).
2. skstack06's `tools/dry_render.py`, which renders a service's deploy
   templates through real Ansible but drops every shell/command task
   outright (including these three) rather than honoring check_mode at
   all. This is the real bug this test guards: found live by that tool
   (`tools/dry_render.sh sksec-web`, 2026-10-23, run
   20261023T192928Z-proof-traefik-v22616): "'sksw_lapi_machines' is
   undefined" out of the plain `sksw_lapi_machines.stdout` reference that
   used to be in "Check whether sksec-web's machine is already registered".

Both are reproduced the same way here: run ONLY the tasks that are not a
shell/command module from the real task file (exactly dry_render.py's own
drop rule) through real ansible-playbook, with the register names those
dropped tasks would have produced deliberately absent from the starting
vars (not just empty).
"""
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

ROOT = Path(__file__).resolve().parents[2]
TASK_FILE = ROOT / "v1" / "ansible" / "optional" / "sksec-web" / "tasks" / "register_lapi_machine.yml"
READONLY_LOOKUPS = {
    "Wait for the swarm node running the CrowdSec task",
    "Wait for the CrowdSec LAPI to answer (cscli lapi status)",
    "List registered CrowdSec machines",
}
EXPECTED_DROPPED_REGISTERS = {
    "sksw_crowdsec_node", "sksw_crowdsec_container", "sksw_lapi_machines",
    "sksw_machine_add", "sksw_lapi_machines_after",
}


def _tasks():
    return yaml.safe_load(TASK_FILE.read_text())


def test_readonly_lookups_stay_check_mode_false():
    """The three read-only lookups must stay `check_mode: false` (same
    convention as shared/tasks/select_manager_node.yml): a real
    `--check --diff` reports live registration status instead of skipping
    them, same as every other info-gathering shell/command task in this
    framework. The mutating "Register" task and the "Verify" task that
    depends on it are left check-mode-skippable on purpose (Ansible's own
    default for shell/command): neither should claim check_mode: false."""
    by_name = {t["name"]: t for t in _tasks()}
    for name in READONLY_LOOKUPS:
        assert by_name[name].get("check_mode") is False, f"{name} must be check_mode: false"
    for name in ("Register the sksec-web machine with CrowdSec (first deploy only)",
                 "Verify the sksec-web machine is registered"):
        assert "check_mode" not in by_name[name], f"{name} must stay check-mode-skippable (no check_mode override)"


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
    """Real bug (2026-10-23, skstack06 tools/dry_render.sh sksec-web):
    'sksw_lapi_machines' is undefined. Reproduced by running only the
    tasks that survive dry_render.py's drop rule (set_fact/debug), with
    the registers those dropped tasks would have set deliberately absent
    -- not merely empty -- from the starting vars."""
    survivors, dropped = _drop_shell_and_command(_tasks())
    assert dropped == EXPECTED_DROPPED_REGISTERS
    for t in survivors:
        assert t.get("register") not in dropped

    playbook = [{
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {
            "sksec_web": {"LAPI_USERNAME": "sksec-web", "LAPI_PASSWORD": "x" * 24},
            "sksw_crowdsec_service": "sksec-dev_crowdsec",
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
    assert 'failed=0' in out


def test_set_fact_resolves_false_with_no_machines_registered(tmp_path):
    """Same dry-render reproduction, but asserts the actual resolved value:
    with the register entirely absent, sksw_machine_exists must resolve to
    false (treat "can't determine" as "not registered"), never raise."""
    survivors, _ = _drop_shell_and_command(_tasks())
    # Replace the final debug task with one that prints the real fact, so
    # the test can assert on it without parsing prose.
    survivors = [t for t in survivors if t["name"] != "Report machine registration (never the password)"]
    survivors.append({"name": "expose", "debug": {"var": "sksw_machine_exists"}})

    playbook = [{
        "hosts": "localhost",
        "gather_facts": False,
        "vars": {
            "sksec_web": {"LAPI_USERNAME": "sksec-web", "LAPI_PASSWORD": "x" * 24},
        },
        "tasks": survivors,
    }]
    pb_path = tmp_path / "playbook.yml"
    pb_path.write_text(yaml.safe_dump(playbook, sort_keys=False))
    result = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", "-v", str(pb_path)],
        capture_output=True, text=True, timeout=60)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert '"sksw_machine_exists": false' in out, out
