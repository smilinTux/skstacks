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
  if [ "$1" = restart ]; then
    echo fault > "$ST"
    if [ "$READY" = 1 ]; then (sleep 1; echo backup > "$ST") & fi
  fi
  [ "$1" = is-active ] && echo active
  return 0
}
export -f systemctl
"""


@pytest.mark.parametrize("ready,code", [(1, 0), (0, 1)])
def test_handler_waits_for_backup_and_fails_when_stuck(tmp_path, ready, code):
    state = tmp_path / "skha.state"
    script = handler_script(SKHA / "deploy_skha-prod.yml", 3, '"$ST"')
    env = {**os.environ, "ST": str(state), "READY": str(ready)}
    res = subprocess.run(["bash", "-c", FAKE_SYSTEMCTL + script], env=env, capture_output=True, text=True, timeout=30)
    assert res.returncode == code, res.stderr
    if not ready:
        assert "state: fault" in res.stderr
