"""skha deploy: restarts wait for a ready node; vip_gate_checks is validated.

The restart handler used to sleep a fixed 20 s per manager. It now waits until
the node reports MASTER or BACKUP (skha_notify.sh writes /run/skha.state) and
fails when it does not, so a manager that cannot carry the VIP is reported."""
import os
import pathlib
import subprocess

import pytest
import yaml

SKHA = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skha"
PLAYBOOKS = sorted(SKHA.glob("deploy_skha-*.yml"))
NOTIFY = SKHA / "src/keepalived/scripts/skha_notify.sh"


def tasks(path):
    plays = yaml.safe_load(path.read_text())
    out = {}
    for play in plays:
        for key in ("pre_tasks", "tasks", "handlers"):
            for t in play.get(key) or []:
                out[t["name"]] = t
    return out


def handler_script(path, wait, state_file):
    script = tasks(path)["restart keepalived"]["ansible.builtin.shell"]
    return script.replace("{{ skha_restart_wait }}", str(wait)).replace("/run/skha.state", state_file)


def test_three_playbooks_found():
    assert [p.name for p in PLAYBOOKS] == ["deploy_skha-dev.yml", "deploy_skha-prod.yml", "deploy_skha-staging.yml"]


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_handler_is_throttled_and_has_no_fixed_sleep(path):
    h = tasks(path)["restart keepalived"]
    assert h["throttle"] == 1
    assert "/run/skha.state" in h["ansible.builtin.shell"]
    assert "restart_settle_seconds" not in h["ansible.builtin.shell"]


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


def run_handler(tmp_path, path, ready=1, start_ok=1, stale=0, wait=3):
    script = handler_script(path, wait, '"$ST"')
    env = {**os.environ, "ST": str(tmp_path / "skha.state"), "READY": str(ready),
           "START_OK": str(start_ok), "STALE": str(stale)}
    return subprocess.run(["bash", "-c", FAKE_SYSTEMCTL + script], env=env, capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_handler_returns_once_node_is_backup(tmp_path, path):
    res = run_handler(tmp_path, path)
    assert res.returncode == 0, res.stderr


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_handler_fails_when_node_stays_fault(tmp_path, path):
    res = run_handler(tmp_path, path, ready=0)
    assert res.returncode == 1
    assert "state: fault" in res.stderr


def test_handler_fails_fast_with_journal_when_start_fails(tmp_path):
    res = run_handler(tmp_path, SKHA / "deploy_skha-prod.yml", start_ok=0, wait=20)
    assert res.returncode == 1
    assert "journal tail" in res.stderr


def test_stale_state_from_old_daemon_does_not_end_the_wait(tmp_path):
    # The old daemon writes backup while stopping and the new one writes
    # nothing, so only the marker keeps that write from ending the wait.
    res = run_handler(tmp_path, SKHA / "deploy_skha-prod.yml", ready=0, stale=1)
    assert res.returncode == 1


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_failed_node_stops_the_rollout(path):
    assert tasks(path)["restart keepalived"]["any_errors_fatal"] is True
