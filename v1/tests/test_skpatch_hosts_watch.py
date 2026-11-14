"""skpatch-hosts dashboard watch (tools/skpatch_watch.py): the immediate alert
classes the dashboard cannot send itself, and the daily summary. Tested on
synthetic reports and through a fake ssh that serves one."""
import datetime as dt
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

V1 = Path(__file__).resolve().parents[1]
ROLE = V1 / "ansible" / "optional" / "skpatch-hosts"
TOOL = ROLE / "tools" / "skpatch_watch.py"

spec = importlib.util.spec_from_file_location("skpatch_watch", TOOL)
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)

NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=dt.timezone.utc)


def system(name, **kw):
    base = {"name": name, "reachable": True, "needs_reboot": False, "boot_id": "b1", "package_issues": 0,
            "updates": 0, "security": 0, "lifecycle": {"status": "supported"}, "sudo_denied": [], "failed": []}
    return {**base, **kw}


REPORT = {"systems": [
    system("ok-1"),
    system("fail-1", failed=[{"action": "upgrade_all", "at": "2026-11-14T07:00:00Z", "error": "E: dpkg was interrupted"}]),
    system("sudo-1", sudo_denied=[{"action": "upgrade_package", "pkgManager": "apt", "at": "2026-11-14T02:00:00Z",
                                    "error": "sudo: a password is required"}]),
    system("repair-1", package_issues=2),
    system("boot-1", needs_reboot=True, boot_id="b9"),
    system("eol-1", updates=4, security=1, lifecycle={"status": "eol", "label": "Debian 11", "eol": "2026-08-31",
                                                       "days_to_eol": -75}),
    system("down-1", reachable=False, updates=2),
], "schedules": [
    {"name": "Nightly 01", "type": "update", "enabled": True, "last_run_at": "2026-11-14T06:30:00Z",
     "status": "success", "message": "Checked 6 systems, upgraded 6, skipped 0"},
    {"name": "Nightly 02", "type": "update", "enabled": True, "last_run_at": "2026-11-14T08:40:00Z",
     "status": "warning", "message": "Checked 1 system, upgraded 0, skipped 0, 1 failed"},
    {"name": "Last week", "type": "reboot", "enabled": True, "last_run_at": "2026-11-05T09:00:00Z",
     "status": "success", "message": "old"},
    {"name": "Refresh", "type": "refresh", "enabled": True, "last_run_at": "2026-11-14T11:00:00Z",
     "status": "success", "message": "not a run of interest"},
]}


def test_each_immediate_class_is_one_issue_per_host_and_kind():
    issues, levels, _ = sw.watch_issues(REPORT, {}, NOW, 8)
    assert set(issues) == {"fail-1|upgrade-failed:upgrade_all", "sudo-1|sudo-denied:upgrade_package",
                           "repair-1|package-manager"}
    assert "dpkg was interrupted" in issues["fail-1|upgrade-failed:upgrade_all"]
    assert levels["sudo-1|sudo-denied:upgrade_package"] == "error"
    # unreachable is the dashboard's own immediate channel: never doubled here
    assert not [k for k in issues if k.startswith("down-1")]


def test_a_reboot_alerts_only_after_eight_days_on_the_same_boot():
    _, _, ages = sw.watch_issues(REPORT, {}, NOW, 8)
    assert ages == {"boot-1": {"boot_id": "b9", "since": NOW.isoformat(timespec="seconds")}}
    later = NOW + dt.timedelta(days=8, hours=1)
    issues, _, ages2 = sw.watch_issues(REPORT, ages, later, 8)
    assert "boot-1|reboot-pending" in issues and ages2 == ages
    # a new boot (the host was rebooted, then needed another) starts the clock again
    rebooted = {"systems": [system("boot-1", needs_reboot=True, boot_id="b10")]}
    issues, _, ages3 = sw.watch_issues(rebooted, ages, later, 8)
    assert not issues and ages3["boot-1"]["boot_id"] == "b10"
    # no longer required: forgotten
    assert sw.watch_issues({"systems": [system("boot-1")]}, ages, later, 8)[2] == {}


def test_the_digest_has_updates_runs_lifecycle_and_open_issues():
    text = sw.digest_text(REPORT, {"sudo-1|sudo-denied:upgrade_package": {}}, NOW, 90, "skpatch daily summary")
    assert "Updates: 6 on 2 of 7 systems, 1 security" in text
    assert "- eol-1: 4 (1 security)" in text
    assert "Last 24 hours of scheduled runs: 2" in text and "Last week" not in text and "Refresh" not in text
    assert "⚠️ Nightly 02: Checked 1 system, upgraded 0, skipped 0, 1 failed" in text
    assert "- eol-1: Debian 11, eol, end of life 75 days ago (2026-08-31)" in text
    assert "Open issues: 1\n- sudo-1|sudo-denied:upgrade_package" in text
    assert "Unreachable now: down-1" in text


def test_a_support_end_inside_the_warning_window_is_listed():
    rep = {"systems": [system("lts-1", lifecycle={"status": "supported", "label": "Ubuntu 22.04",
                                                  "support_end": "2027-01-01", "days_to_support_end": 48})]}
    assert sw.lifecycle_lines(rep, 90) == ["- lts-1: Ubuntu 22.04, supported, end of standard support in 48 days (2027-01-01)"]
    assert sw.lifecycle_lines(rep, 30) == []


def _fake_ssh(tmp_path, report, rc=0):
    stub = tmp_path / "bin" / "ssh"
    stub.parent.mkdir()
    out = tmp_path / "report.out"
    out.write_text("noise\n@@SKP-REPORT@@" + json.dumps(report) + "\n")
    stub.write_text(f"#!/bin/sh\ncat > {tmp_path}/stdin\nprintf '%s\\n' \"$@\" > {tmp_path}/argv\n"
                    f"cat {out}\nexit {rc}\n")
    stub.chmod(0o755)
    return stub.parent


def _run(tmp_path, *extra, report=REPORT, rc=0):
    bindir = _fake_ssh(tmp_path, report, rc)
    log = tmp_path / "hook.log"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/bin/sh\nprintf "%s|%s|%s\\n" "$1" "$2" "$3" >> {log}\n')
    hook.chmod(0o755)
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    proc = subprocess.run([sys.executable, str(TOOL), "--ssh", "dash-node", "--register-as", "admin",
                           "--state", str(tmp_path / "state" / "e.json"), "--alert-hook", str(hook), *extra],
                          capture_output=True, text=True, env=env, timeout=60)
    return proc, log


def test_watch_raises_once_then_resolves_through_the_hook(tmp_path):
    proc, log = _run(tmp_path)
    assert proc.returncode == 1, proc.stderr
    assert sorted(log.read_text().splitlines()) == ["raise|fail-1|upgrade-failed:upgrade_all|error",
                                                    "raise|repair-1|package-manager|warn",
                                                    "raise|sudo-1|sudo-denied:upgrade_package|error"]
    stdin = json.loads((tmp_path / "stdin").read_text())
    assert stdin == {"register_as": "admin", "history_days": 3}
    argv = (tmp_path / "argv").read_text()
    assert "BatchMode=yes" in argv and "dash-node" in argv and "docker" in argv and "node --no-warnings" in argv
    assert (tmp_path / "state" / "e.reboot.json").exists()


def test_watch_is_quiet_on_an_unchanged_fleet_and_resolves_a_fixed_host(tmp_path):
    _run(tmp_path)
    (tmp_path / "bin").rename(tmp_path / "bin1")
    fixed = {"systems": [s for s in REPORT["systems"] if s["name"] != "repair-1"], "schedules": []}
    proc, log = _run(tmp_path, report=fixed)
    assert proc.returncode == 1
    assert log.read_text().splitlines()[3:] == ["resolve|repair-1|package-manager|info"]


def test_the_digest_posts_one_message(tmp_path):
    proc, log = _run(tmp_path, "--digest")
    assert proc.returncode == 0, proc.stderr
    assert log.read_text().splitlines() == ["post|digest|info"]
    assert "skpatch daily summary" in proc.stdout


def test_a_failed_report_is_exit_2_and_alerts_nothing(tmp_path):
    proc, log = _run(tmp_path, rc=3)
    assert proc.returncode == 2 and "report failed" in proc.stderr
    assert not log.exists()


def test_the_watch_units_share_state_and_the_talk_env():
    units = ROLE / "tools" / "systemd"
    watch = (units / "skpatch-watch@.service").read_text()
    digest = (units / "skpatch-watch-digest@.service").read_text()
    for u in (watch, digest):
        assert "StateDirectory=skpatch-hosts/watch" in u and '--state "$STATE_DIRECTORY/%i.json"' in u
        assert "EnvironmentFile=-%h/.config/skpatch-talk/%i.env" in u
    assert "SuccessExitStatus=1" in watch and "--digest" in digest
    assert "OnCalendar=*:0/15" in (units / "skpatch-watch@.timer").read_text()
    assert "OnCalendar=*-*-* 07:00:00" in (units / "skpatch-watch-digest@.timer").read_text()
