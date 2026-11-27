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
import time
from pathlib import Path

import pytest
import yaml

ROLE = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skpatch-hosts"
SCRIPT = ROLE / "src" / "skpatch-hosts" / "skpatch-netbird-keepup.sh"
PLAYBOOK = ROLE / "provision_skpatch_hosts_nodes.yml"
SHELLS = [s for s in ("sh", "dash", "bash") if shutil.which(s)]
PROFILE = {"ManagementURL": {"Host": "skmesh.example.test:443"}, "DisableAutoConnect": False}


LEGACY_NEEDS_LOGIN = ("Daemon status: NeedsLogin\n\nRun UP command to log in with SSO (interactive login):\n\n"
                      " netbird up \n\nMore info: https://docs.netbird.io/how-to/register-machines-using-setup-keys\n\n")
DEVICE_FLOW = ("Please do the SSO login in your browser.\nIf your browser didn't open automatically, use this URL"
               " to log in:\n\nhttps://idp.example.test/device?user_code=ABCD-EFGH\n")


def run(tmp, shell, status, files=None, netbird=True, json_out=None, plain_out=None, up_out="Connected"):
    """json_out: what `status --json` prints (default: the compact one-line JSON
    real clients print, with no trailing newline); plain_out: plain `status`."""
    root = tmp / "root"
    root.mkdir(parents=True, exist_ok=True)
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    calls = tmp / "calls"
    if netbird:
        if json_out is None:
            json_out = json.dumps({"daemonStatus": status, "management": {"connected": status == "Connected"}},
                                  separators=(",", ":"))
        (tmp / "status.json").write_text(json_out)
        (tmp / "status.txt").write_text(plain_out if plain_out is not None else f"Daemon status: {status}\n")
        (tmp / "up.txt").write_text(up_out + "\n")
        nb = bindir / "netbird"
        nb.write_text(f'#!/bin/sh\necho "$*" >> {calls}\n'
                      f'case "$*" in\n'
                      f'  "status --json") cat {tmp / "status.json"} ;;\n'
                      f'  status) cat {tmp / "status.txt"} ;;\n'
                      f'  up*) cat {tmp / "up.txt"} ;;\n'
                      f'esac\nexit 0\n')
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


def test_compact_json_without_a_trailing_newline_is_parsed(tmp_path, shell):
    js = '{"peers":{"total":0},"daemonStatus":"NeedsLogin","management":{"url":"https://skmesh.example.test:443"}}'
    assert not js.endswith("\n")
    assert run(tmp_path, shell, "NeedsLogin", DEFAULT, json_out=js)[0] == ["up --no-browser"]


def test_pretty_json_is_parsed_too(tmp_path, shell):
    js = json.dumps({"daemonStatus": "Idle"}, indent=2)
    assert run(tmp_path, shell, "Idle", DEFAULT, json_out=js)[0] == ["up --no-browser"]


def test_a_pre_0_67_client_printing_plain_text_needs_login_is_handled(tmp_path, shell):
    # older clients print the NeedsLogin banner as plain text even with --json
    assert run(tmp_path, shell, "NeedsLogin", DEFAULT, json_out=LEGACY_NEEDS_LOGIN)[0] == ["up --no-browser"]


def test_json_without_daemon_status_falls_back_to_plain_status(tmp_path, shell):
    ups, _ = run(tmp_path, shell, "Idle", DEFAULT, json_out='{"management":{"connected":false}}',
                 plain_out="Daemon status: Idle\n")
    assert ups == ["up --no-browser"]
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[:2] == ["status --json", "status"]
    ups, _ = run(tmp_path / "b", shell, "Connected", DEFAULT, json_out='{"management":{"connected":true}}',
                 plain_out="OS: linux/amd64\nManagement: Connected\n")
    assert ups == []


def test_a_login_url_backs_off_for_six_hours_and_is_never_logged(tmp_path, shell):
    ups, out = run(tmp_path, shell, "NeedsLogin", DEFAULT, up_out=DEVICE_FLOW)
    assert ups == ["up --no-browser"]
    assert "WARNING" in out and "needs SSO" in out
    assert "http" not in out and "ABCD-EFGH" not in out
    until = int((tmp_path / "root/var/lib/skpatch-netbird-keepup/backoff-until").read_text())
    assert 21600 - 60 <= until - int(time.time()) <= 21600
    # the next runs inside the window do nothing, not even a status call
    (tmp_path / "calls").unlink()
    assert run(tmp_path, shell, "NeedsLogin", DEFAULT, up_out=DEVICE_FLOW) == ([], "")
    assert not (tmp_path / "calls").exists()


def test_an_expired_or_garbled_backoff_is_ignored(tmp_path, shell):
    for i, text in enumerate([str(int(time.time()) - 1), "garbage", ""]):
        files = {**DEFAULT, "var/lib/skpatch-netbird-keepup/backoff-until": text}
        assert run(tmp_path / str(i), shell, "NeedsLogin", files)[0] == ["up --no-browser"]


def test_status_calls_are_bounded_and_the_unit_has_a_start_timeout():
    text = SCRIPT.read_text()
    assert "timeout 20 netbird status --json" in text and "timeout 20 netbird status " in text
    tasks = _tasks()
    units = next(t for t in tasks if "copy" in t and "loop" in t and t.get("when") == "skp_netbird_keepup | bool")
    service = {u["name"]: u["content"] for u in units["loop"]}["skpatch-netbird-keepup.service"]
    assert "TimeoutStartSec=120" in service
    removes = next(t for t in tasks if t.get("file", {}).get("state") == "absent"
                   and "/usr/local/sbin/skpatch-netbird-keepup" in t.get("loop", []))
    assert "/var/lib/skpatch-netbird-keepup" in removes["loop"]
