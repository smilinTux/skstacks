"""skha deploy: restarts wait for a ready node; vip_gate_checks is validated.

The restart handler used to sleep a fixed 20 s per manager. It now waits until
the node reports MASTER or BACKUP (skha_notify.sh writes /run/skha.state) and
fails when it does not, so a manager that cannot carry the VIP is reported.
test_skha_rollout_stops.py proves the rollout then stops."""
import os
import pathlib
import subprocess

import pytest
import yaml

SKHA = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skha"
PLAYBOOKS = sorted(SKHA.glob("deploy_skha-*.yml"))
NOTIFY = SKHA / "src/keepalived/scripts/skha_notify.sh"
RESTART = SKHA / "src/keepalived/restart_keepalived.yml"


def tasks(path):
    plays = yaml.safe_load(path.read_text())
    out = {}
    for play in plays:
        for key in ("pre_tasks", "tasks", "handlers"):
            for t in play.get(key) or []:
                out[t["name"]] = t
    return out


def handler_script():
    return yaml.safe_load(RESTART.read_text())[0]["ansible.builtin.shell"]


def test_three_playbooks_found():
    assert [p.name for p in PLAYBOOKS] == ["deploy_skha-dev.yml", "deploy_skha-prod.yml", "deploy_skha-staging.yml"]


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_handler_is_one_sequential_run_once_loop(path):
    h = tasks(path)["restart keepalived"]
    assert h["ansible.builtin.include_tasks"].endswith("/src/keepalived/restart_keepalived.yml")
    assert h["run_once"] is True
    assert h["loop_control"]["loop_var"] == "skha_node"
    assert "skha_conf" in h["loop"]
    assert tasks(path)["Template keepalived config"]["register"] == "skha_conf"


def test_restart_task_has_no_fixed_sleep_and_uses_the_state_file():
    t = yaml.safe_load(RESTART.read_text())[0]
    assert t["delegate_to"] == "{{ skha_node }}"
    assert "/run/skha.state" in t["environment"]["SKHA_STATE"]
    assert "restart_settle_seconds" not in t["ansible.builtin.shell"]


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_gate_asserts_cover_mapping_and_unknown_names(path):
    t = tasks(path)
    assert "is mapping" in t["Verify skha.vip_gate_checks is a list of check names"]["when"]
    unknown = t["Verify every skha.vip_gate_checks name is a defined health check"]
    assert "difference" in unknown["vars"]["skha_unknown_gates"]
    assert "skha_restart_wait" in t["Compute the keepalived restart wait"]["set_fact"]


def test_notify_script_records_state():
    assert 'echo "${STATE}" > /run/skha.state' in NOTIFY.read_text()


FAKE_SYSTEMCTL = """
systemctl() {
  case "$1" in
    stop) [ "$STALE" = 1 ] && (sleep 0.5; echo backup > "$ST") & ;;
    start)
      [ "$START_OK" = 0 ] && return 1
      [ "$STALE" = 1 ] || echo fault > "$ST"
      if [ "$READY" = 1 ]; then (sleep 1; echo backup > "$ST") & fi ;;
    is-active) echo active ;;
  esac
  return 0
}
journalctl() { echo "journal tail"; }
export -f systemctl journalctl
"""


def run_handler(tmp_path, ready=1, start_ok=1, stale=0, wait=3):
    script = handler_script()
    st = str(tmp_path / "skha.state")
    env = {**os.environ, "ST": st, "SKHA_STATE": st, "SKHA_WAIT": str(wait), "SKHA_NODE": "n1",
           "READY": str(ready),
           "START_OK": str(start_ok), "STALE": str(stale)}
    return subprocess.run(["bash", "-c", FAKE_SYSTEMCTL + script], env=env, capture_output=True, text=True, timeout=30)


def test_handler_returns_once_node_is_backup(tmp_path):
    res = run_handler(tmp_path)
    assert res.returncode == 0, res.stderr


def test_handler_fails_when_node_stays_fault(tmp_path):
    res = run_handler(tmp_path, ready=0)
    assert res.returncode == 1
    assert "state: fault" in res.stderr


def test_handler_fails_fast_with_journal_when_start_fails(tmp_path):
    res = run_handler(tmp_path, start_ok=0, wait=20)
    assert res.returncode == 1
    assert "journal tail" in res.stderr


def test_stale_state_from_old_daemon_does_not_end_the_wait(tmp_path):
    # The old daemon writes backup while stopping and the new one writes
    # nothing, so only the marker keeps that write from ending the wait.
    res = run_handler(tmp_path, ready=0, stale=1)
    assert res.returncode == 1
