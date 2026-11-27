"""src/skpatch-hosts/skpatch-netbird-keepup.sh: the opt-in timer that brings a
setup-key NetBird peer back after it fell into NeedsLogin.

The live failure this encodes: management was briefly down, the proxy's
catch-all answered 403, the client mapped that to PermissionDenied, left its
retry loop and stayed in NeedsLogin until someone ran `netbird up` by hand.

Run through sh, dash and bash against a fake `netbird` binary that records its
calls, with fake profiles under SKP_NETBIRD_ROOT."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skpatch-hosts"
SCRIPT = ROLE / "src" / "skpatch-hosts" / "skpatch-netbird-keepup.sh"
PLAYBOOK = ROLE / "provision_skpatch_hosts_nodes.yml"
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]
PROFILE = {"ManagementURL": {"Host": "skmesh.example.test:443"}, "DisableAutoConnect": False}


def run(tmp, shell, status, files=None, netbird=True):
    root = tmp / "root"
    root.mkdir(parents=True, exist_ok=True)
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp / "calls"
    if netbird:
        out = tmp / "status.json"
        out.write_text(json.dumps({"daemonStatus": status, "management": {"connected": status == "Connected"}},
                                  indent=2))
        nb = bindir / "netbird"
        nb.write_text(f'#!/bin/sh\necho "$*" >> {calls}\n'
                      f'[ "$1 $2" = "status --json" ] && cat {out}\n'
                      f'[ "$1" = up ] && echo "Connected"\nexit 0\n')
        nb.chmod(0o755)
    for rel, data in (files or {}).items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(data if isinstance(data, str) else json.dumps(data, indent=2))
    env = {"PATH": f"{bindir}:/usr/bin:/bin", "SKP_NETBIRD_ROOT": str(root)}
    proc = subprocess.run([shell, str(SCRIPT)], env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    ups = [l for l in (calls.read_text().splitlines() if calls.exists() else []) if l.startswith("up")]
    return ups, proc.stdout


@pytest.fixture(params=SHELLS)
def shell(request):
    return request.param


DEFAULT = {"var/lib/netbird/default.json": PROFILE}


def test_needs_login_runs_netbird_up_without_a_browser(tmp_path, shell):
    ups, out = run(tmp_path, shell, "NeedsLogin", DEFAULT)
    assert ups == ["up --no-browser"]
    assert "daemon is NeedsLogin" in out and "netbird up exited 0" in out


def test_idle_runs_netbird_up(tmp_path, shell):
    assert run(tmp_path, shell, "Idle", DEFAULT)[0] == ["up --no-browser"]


@pytest.mark.parametrize("status", ["Connected", "Connecting", "LoginFailed", "SessionExpired", ""])
def test_other_states_do_nothing(tmp_path, shell, status):
    assert run(tmp_path, shell, status, DEFAULT) == ([], "")


@pytest.mark.parametrize("status", ["Idle", "NeedsLogin"])
def test_disable_auto_connect_in_the_active_profile_is_respected(tmp_path, shell, status):
    files = {"var/lib/netbird/default.json": {**PROFILE, "DisableAutoConnect": True}}
    assert run(tmp_path, shell, status, files) == ([], "")


def test_the_active_profile_decides_not_any_profile(tmp_path, shell):
    files = {"var/lib/netbird/default.json": {**PROFILE, "DisableAutoConnect": True},
             "var/lib/netbird/work.json": PROFILE,
             "var/lib/netbird/active_profile.json": {"name": "work", "username": ""}}
    assert run(tmp_path, shell, "NeedsLogin", files)[0] == ["up --no-browser"]
    files["var/lib/netbird/work.json"] = {**PROFILE, "DisableAutoConnect": True}
    files["var/lib/netbird/default.json"] = PROFILE
    assert run(tmp_path / "b", shell, "NeedsLogin", files)[0] == []


def test_a_pre_migration_per_user_profile_is_found(tmp_path, shell):
    files = {"var/lib/netbird/alice/work.json": {**PROFILE, "DisableAutoConnect": True},
             "var/lib/netbird/active_profile.json": {"name": "work", "username": "alice"}}
    assert run(tmp_path, shell, "NeedsLogin", files)[0] == []


def test_the_old_config_path_is_the_default_profile_on_older_clients(tmp_path, shell):
    files = {"etc/netbird/config.json": {**PROFILE, "DisableAutoConnect": True}}
    assert run(tmp_path, shell, "Idle", files)[0] == []
    assert run(tmp_path / "b", shell, "Idle", {"etc/netbird/config.json": PROFILE})[0] == ["up --no-browser"]


def test_no_config_for_the_active_profile_leaves_it_alone(tmp_path, shell):
    ups, out = run(tmp_path, shell, "NeedsLogin", {})
    assert ups == [] and "no config for profile 'default'" in out


def test_no_netbird_installed_is_a_quiet_no_op(tmp_path, shell):
    assert run(tmp_path, shell, "NeedsLogin", DEFAULT, netbird=False) == ([], "")


def test_netbird_up_is_bounded_by_a_timeout():
    assert "timeout 60 netbird up --no-browser" in SCRIPT.read_text()


def _tasks():
    play = yaml.safe_load(PLAYBOOK.read_text())
    return [t for p in play for t in p.get("tasks", [])]


def test_playbook_installs_the_timer_only_on_opted_in_hosts_and_removes_it_otherwise():
    tasks = _tasks()
    decide = next(t for t in tasks if "skp_netbird_keepup" in t.get("set_fact", {}))
    expr = decide["set_fact"]["skp_netbird_keepup"]
    assert "skpatch_hosts_netbird_keepup | default(false)" in expr and "skpatch_state == 'present'" in expr
    installs = [t for t in tasks if t.get("when") == "skp_netbird_keepup | bool"]
    script = next(t for t in installs if "copy" in t and t["copy"].get("src", "").endswith("skpatch-netbird-keepup.sh"))
    assert script["copy"]["dest"] == "/usr/local/sbin/skpatch-netbird-keepup"
    assert script["copy"]["mode"] == "0755" and script["copy"]["owner"] == "root"
    units = next(t for t in installs if "copy" in t and "loop" in t)
    by_name = {u["name"]: u["content"] for u in units["loop"]}
    assert "ExecStart=/usr/local/sbin/skpatch-netbird-keepup" in by_name["skpatch-netbird-keepup.service"]
    assert "Type=oneshot" in by_name["skpatch-netbird-keepup.service"]
    assert "OnUnitActiveSec=5min" in by_name["skpatch-netbird-keepup.timer"]
    enable = next(t for t in installs if "systemd" in t)
    assert enable["systemd"]["name"] == "skpatch-netbird-keepup.timer"
    assert enable["systemd"]["enabled"] is True and enable["systemd"]["state"] == "started"
    removes = next(t for t in tasks if t.get("file", {}).get("state") == "absent"
                   and "/usr/local/sbin/skpatch-netbird-keepup" in t.get("loop", []))
    assert removes["when"] == "not (skp_netbird_keepup | bool)"
    # the timer is stopped before its unit file goes
    stop = next(t for t in tasks if "systemctl disable --now skpatch-netbird-keepup.timer" in str(t.get("shell", "")))
    assert tasks.index(stop) < tasks.index(removes)
