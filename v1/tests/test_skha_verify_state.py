"""skha deploy: a manager left in keepalived FAULT is restarted or reported.

The restart handler runs only when the rendered config changes, so a manager
stuck in FAULT (or with keepalived stopped) after an earlier failed rollout
stayed that way on a rerun with no change, and the play reported success.
Every deploy now ends with tasks/verify_keepalived.yml on every
manager: read the state, restart once on FAULT or not active, wait with
retries/until, and fail naming the host if it is still FAULT. The second half
runs that file with ansible-playbook on three local hosts and a fake
systemctl."""
import os
import pathlib
import shutil
import subprocess

import pytest
import yaml

SKHA = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skha"
PLAYBOOKS = sorted(SKHA.glob("deploy_skha-*.yml"))
VERIFY = SKHA / "tasks/verify_keepalived.yml"


def block():
    return yaml.safe_load(VERIFY.read_text())[0]


def inner():
    return {t["name"]: t for t in block()["block"]}


def test_three_playbooks_found():
    assert [p.name for p in PLAYBOOKS] == ["deploy_skha-dev.yml", "deploy_skha-prod.yml", "deploy_skha-staging.yml"]


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_playbook_flushes_handlers_then_verifies_every_manager(path):
    tasks = yaml.safe_load(path.read_text())[1]["tasks"]
    names = [t["name"] for t in tasks]
    flush = names.index("Run the keepalived restart handler before the state check")
    verify = names.index("Verify keepalived state on every manager")
    assert tasks[flush]["ansible.builtin.meta"] == "flush_handlers"
    assert verify == flush + 1 == len(tasks) - 1
    inc = tasks[verify]
    assert inc["ansible.builtin.include_tasks"].endswith("/tasks/verify_keepalived.yml")
    assert "run_once" not in inc and "when" not in inc


def test_verify_runs_with_become_and_is_skipped_in_check_mode():
    b = block()
    assert b["become"] is True
    assert b["when"] == "not ansible_check_mode"
    assert "/run/skha.state" in b["environment"]["SKHA_STATE"]
    assert "systemctl is-active keepalived" in b["vars"]["skha_read_state"]


def test_restart_only_on_fault_or_not_active_and_clears_the_state_file():
    t = inner()
    decide = t["Decide whether keepalived needs a restart (FAULT or not active)"]["ansible.builtin.set_fact"]["skha_needs_restart"]
    assert "!= 'active'" in decide and "== 'fault'" in decide
    restart = t["Restart keepalived once on a manager in FAULT or not active"]
    assert restart["when"] == "skha_needs_restart | bool"
    script = restart["ansible.builtin.shell"]
    assert script.index("systemctl stop") < script.index('rm -f "$SKHA_STATE"') < script.index("systemctl start")


def test_wait_uses_retries_until_not_a_shell_sleep():
    w = inner()["Wait for the restarted keepalived to report MASTER or BACKUP"]
    assert "['master', 'backup']" in w["until"]
    assert "retries" in w and "delay" in w
    assert w["failed_when"] is False
    assert "sleep" not in w["ansible.builtin.shell"] and "sleep" not in block()["vars"]["skha_read_state"]


def test_fail_names_the_host_and_its_state():
    f = inner()["Fail when keepalived on this manager is still FAULT or not active"]
    msg = f["ansible.builtin.fail"]["msg"]
    assert "{{ inventory_hostname }}" in msg and "skha_final[1]" in msg
    assert "run_once" not in f
    assert "skha_final[1] == 'fault'" in f["when"] and "skha_final[0] != 'active'" in f["when"]


def test_master_count_only_warns():
    w = inner()["Warn unless exactly one manager reports MASTER"]
    assert "ansible.builtin.debug" in w and w["run_once"] is True
    assert w["when"] == "skha_masters | length != 1"


# --- the real task file on three local hosts --------------------------------

FAKE = {
    # <state>.active: is-active output; <state>.after: state the new daemon reaches
    "systemctl": """#!/bin/bash
case "$1" in
  is-active) a=$(cat "$SKHA_STATE.active" 2>/dev/null || echo active); echo "$a"; [ "$a" = active ] ;;
  stop) echo inactive > "$SKHA_STATE.active" ;;
  start)
    echo "$SKHA_NODE" >> "$(dirname "$SKHA_STATE")/starts.log"
    [ -e "$SKHA_STATE.nostart" ] && exit 1
    echo active > "$SKHA_STATE.active"
    echo fault > "$SKHA_STATE"
    after=$(cat "$SKHA_STATE.after" 2>/dev/null || echo fault)
    (sleep 1; echo "$after" > "$SKHA_STATE") >/dev/null 2>&1 & ;;
esac
exit 0
""",
    "journalctl": "#!/bin/bash\necho journal tail\n",
    "ip": '#!/bin/bash\n[ -e "$SKHA_STATE.vip_local" ] && echo "2: eth0    inet 192.0.2.50/24 scope global eth0"\nexit 0\n',
    "ping": '#!/bin/bash\n[ -e "$SKHA_STATE.vip_remote" ]\n',
}

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def run(tmp_path, nodes, check=False):
    """nodes: {name: dict(state=..., active=..., after=..., vip=local|remote|None, nostart=bool)}."""
    fakebin = tmp_path / "bin"
    fakebin.mkdir()
    for name, body in FAKE.items():
        (fakebin / name).write_text(body)
        (fakebin / name).chmod(0o755)
    hosts = {}
    for n, s in nodes.items():
        st = tmp_path / f"{n}.state"
        if s.get("state"):
            st.write_text(s["state"] + "\n")
        for key in ("active", "after"):
            if s.get(key):
                (tmp_path / f"{n}.state.{key}").write_text(s[key] + "\n")
        if s.get("vip"):
            (tmp_path / f"{n}.state.vip_{s['vip']}").write_text("")
        if s.get("nostart"):
            (tmp_path / f"{n}.state.nostart").write_text("")
        hosts[n] = {"ansible_connection": "local", "ansible_become": False,
                    "ansible_python_interpreter": shutil.which("python3"),
                    "skha_state_file": str(st), "skha_restart_wait": 4}
    (tmp_path / "inv.yml").write_text(yaml.safe_dump({"all": {"hosts": hosts, "vars": {"skha": {"vip": "192.0.2.50/24"}}}}))
    play = [{"hosts": "all", "gather_facts": False,
             "environment": {"PATH": f"{fakebin}:{os.environ['PATH']}"},
             "tasks": [{"ansible.builtin.include_tasks": str(VERIFY)}]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play))
    cmd = ["ansible-playbook", "-i", str(tmp_path / "inv.yml"), str(tmp_path / "play.yml")] + (["--check"] if check else [])
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env={**os.environ, "ANSIBLE_NOCOLOR": "1"})
    log = tmp_path / "starts.log"
    return res, (sorted(log.read_text().split()) if log.exists() else [])


@needs_ansible
def test_healthy_cluster_restarts_nothing(tmp_path):
    res, starts = run(tmp_path, {"h1": {"state": "master", "vip": "local"},
                                 "h2": {"state": "backup", "vip": "remote"},
                                 "h3": {"state": "backup", "vip": "remote"}})
    assert res.returncode == 0, res.stdout + res.stderr
    assert starts == []
    assert "expected exactly one MASTER" not in res.stdout


@needs_ansible
def test_fault_node_is_restarted_once_and_passes_when_it_recovers(tmp_path):
    res, starts = run(tmp_path, {"h1": {"state": "master", "vip": "local"},
                                 "h2": {"state": "fault", "after": "backup", "vip": "remote"},
                                 "h3": {"state": "backup", "vip": "remote"}})
    assert res.returncode == 0, res.stdout + res.stderr
    assert starts == ["h2"]


@needs_ansible
def test_inactive_keepalived_is_restarted(tmp_path):
    res, starts = run(tmp_path, {"h1": {"state": "master", "vip": "local"},
                                 "h2": {"state": "backup", "active": "inactive", "after": "backup"},
                                 "h3": {"state": "backup"}})
    assert res.returncode == 0, res.stdout + res.stderr
    assert starts == ["h2"]


@needs_ansible
def test_node_still_fault_fails_the_play_naming_that_host(tmp_path):
    res, starts = run(tmp_path, {"h1": {"state": "fault"}, "h2": {"state": "fault"},
                                 "h3": {"state": "fault", "after": "master", "vip": None}})
    assert res.returncode != 0
    assert starts == ["h1", "h2", "h3"]
    assert "keepalived on h1 is still FAULT" in res.stdout
    assert "keepalived on h2 is still FAULT" in res.stdout
    assert "keepalived on h3 is still" not in res.stdout


@needs_ansible
def test_failed_start_fails_with_the_journal(tmp_path):
    res, starts = run(tmp_path, {"h1": {"state": "master", "vip": "local"},
                                 "h2": {"state": "fault", "nostart": True}, "h3": {"state": "backup"}})
    assert res.returncode != 0
    assert starts == ["h2"]
    assert "keepalived on h2 failed to start" in res.stdout and "journal tail" in res.stdout


@needs_ansible
def test_fault_with_the_vip_held_elsewhere_only_warns(tmp_path):
    # plain skfence: Traefik on one manager, the others FAULT by design
    res, starts = run(tmp_path, {"h1": {"state": "master", "vip": "local"},
                                 "h2": {"state": "fault", "vip": "remote"},
                                 "h3": {"state": "backup", "vip": "remote"}})
    assert res.returncode == 0, res.stdout + res.stderr
    assert starts == ["h2"]
    assert "WARNING: keepalived on h2 is FAULT" in res.stdout


@needs_ansible
def test_not_exactly_one_master_only_warns(tmp_path):
    res, _ = run(tmp_path, {"h1": {"state": "master"}, "h2": {"state": "master"}, "h3": {"state": "backup"}})
    assert res.returncode == 0, res.stdout + res.stderr
    assert "expected exactly one MASTER, found 2 (h1, h2)" in res.stdout


@needs_ansible
def test_check_mode_skips_and_does_not_fail(tmp_path):
    res, starts = run(tmp_path, {"h1": {"state": "fault"}, "h2": {"state": "fault"}, "h3": {"state": "fault"}},
                      check=True)
    assert res.returncode == 0, res.stdout + res.stderr
    assert starts == []
    assert "keepalived on h1 is still" not in res.stdout
    assert "skipping: [h1]" in res.stdout
