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
    base = {"id": 7, "name": name, "reachable": True, "needs_reboot": False, "boot_id": "b1", "package_issues": 0,
            "updates": 0, "security": 0, "lifecycle": {"status": "supported"}, "sudo_denied": [], "failed": []}
    return {**base, **kw}


REPORT = {"systems": [
    system("ok-1"),
    system("fail-1", failed=[{"action": "upgrade_all", "at": "2026-11-14T07:00:00Z", "error": "E: dpkg was interrupted",
                              "tail": ["dpkg: error processing package foo (--configure):", "E: dpkg was interrupted"],
                              "tail_is_raw": False}]),
    system("sudo-1", sudo_denied=[{"action": "upgrade_package", "pkgManager": "apt", "at": "2026-11-14T02:00:00Z",
                                    "error": "sudo: a password is required",
                                    "command": "apt-get -o DPkg::Lock::Timeout=60 install --only-upgrade -y vim"}]),
    system("repair-1", package_issues=2, package_issue_list=[{"title": "APT needs repair", "message": "dpkg was interrupted.",
                                                             "detected_at": "2026-11-09T17:44:36Z"}]),
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


GAPS = {"upgrade_package": {"gap": "selected upgrades need a wrapper", "fix": "PR #403", "until": "use Upgrade all"}}
URL = "https://dash.example.test"


def test_each_immediate_class_is_one_issue_per_host_and_kind():
    issues, levels, _ = sw.watch_issues(REPORT, {}, NOW, 8, URL, GAPS)
    assert set(issues) == {"fail-1|upgrade-failed:upgrade_all", "sudo-1|sudo-denied:upgrade_package",
                           "repair-1|package-manager"}
    assert levels["fail-1|upgrade-failed:upgrade_all"] == "error"
    # unreachable is the dashboard's own immediate channel: never doubled here
    assert not [k for k in issues if k.startswith("down-1")]


def test_an_alert_quotes_the_run_links_the_system_and_says_what_to_do():
    issues, _, _ = sw.watch_issues(REPORT, {}, NOW, 8, URL, GAPS)
    text = issues["fail-1|upgrade-failed:upgrade_all"]
    assert "```\ndpkg: error processing package foo (--configure):\nE: dpkg was interrupted\n```" in text
    assert "Dashboard: https://dash.example.test/systems/7" in text
    assert "\nNext: " in text and "resolves by itself" in text
    assert "no error line recorded" not in "".join(issues.values())
    rep = issues["repair-1|package-manager"]
    assert rep.startswith("APT needs repair since 2026-11-09 17:44Z:") and "press Repair" in rep


def test_a_cut_off_run_is_quoted_and_labelled_never_an_empty_error():
    cut = {"systems": [system("cut-1", failed=[{"action": "upgrade_all", "at": "2026-11-14T07:00:00Z",
                                                "tail": ["Get:6 https://deb.example/x.deb [15.4 M"], "tail_is_raw": True}])]}
    text = sw.watch_issues(cut, {}, NOW, 8, URL, GAPS)[0]["cut-1|upgrade-failed:upgrade_all"]
    assert "it was cut off" in text and "Get:6" in text


def test_a_known_sudo_gap_names_its_fix_instead_of_reprovisioning():
    issues, levels, _ = sw.watch_issues(REPORT, {}, NOW, 8, URL, GAPS)
    text = issues["sudo-1|sudo-denied:upgrade_package"]
    assert "known gap, fix in PR #403; until then use Upgrade all." in text
    assert "provision this host again" not in text and levels["sudo-1|sudo-denied:upgrade_package"] == "warn"
    assert "$ sudo apt-get -o DPkg::Lock::Timeout=60 install --only-upgrade -y vim" in text
    other = {"systems": [system("sudo-2", sudo_denied=[{"action": "reboot", "at": "2026-11-14T02:00:00Z",
                                                        "error": "sudo: a password is required"}])]}
    text, = sw.watch_issues(other, {}, NOW, 8, URL, GAPS)[0].values()
    assert "provision this host again (provision_skpatch_hosts_nodes.yml -l sudo-2" in text


def test_a_sudo_denial_older_than_the_hosts_coverage_stamp_is_resolved():
    rep = {**REPORT, "coverage": {"sudo-1": {"provisioned_at": "2026-11-14T05:00:00Z", "actions": True}}}
    assert "sudo-1|sudo-denied:upgrade_package" not in sw.watch_issues(rep, {}, NOW, 8, URL, GAPS)[0]
    # a stamp from before the denial, or without actions, does not cover it
    for stamp in ({"provisioned_at": "2026-11-14T01:00:00Z", "actions": True},
                  {"provisioned_at": "2026-11-14T05:00:00Z", "actions": False}):
        rep = {**REPORT, "coverage": {"sudo-1": stamp}}
        assert "sudo-1|sudo-denied:upgrade_package" in sw.watch_issues(rep, {}, NOW, 8, URL, GAPS)[0]


def test_each_cleared_issue_gets_a_short_reason():
    rep = {"systems": [system("fail-1", last_ok={"check": "2026-11-14T08:00:00Z"}, last_clean_check="2026-11-14T08:00:00Z"),
                       system("sudo-1"), system("repair-1"), system("up-1", last_ok={"upgrade_all": "2026-11-14T09:00:00Z"})],
           "coverage": {"sudo-1": {"provisioned_at": "2026-11-14T05:00:00Z", "actions": True}}}
    why = sw.resolutions(rep, ["fail-1|upgrade-failed:upgrade_all", "sudo-1|sudo-denied:upgrade_package",
                               "repair-1|package-manager", "up-1|upgrade-failed:upgrade_package", "gone|reboot-pending"])
    assert why == {"fail-1|upgrade-failed:upgrade_all": "a clean check at 2026-11-14 08:00Z",
                   "sudo-1|sudo-denied:upgrade_package": "sudoers now covers it (provisioned 2026-11-14 05:00Z)",
                   "repair-1|package-manager": "the dashboard no longer reports a package manager issue",
                   "up-1|upgrade-failed:upgrade_package": "an upgrade succeeded at 2026-11-14 09:00Z",
                   "gone|reboot-pending": "the system is no longer in the dashboard"}


def test_a_reboot_alerts_only_after_eight_days_on_the_same_boot():
    _, _, ages = sw.watch_issues(REPORT, {}, NOW, 8)
    assert ages == {"boot-1": {"boot_id": "b9", "since": NOW.isoformat(timespec="seconds")}}
    later = NOW + dt.timedelta(days=8, hours=1)
    issues, _, ages2 = sw.watch_issues(REPORT, ages, later, 8, URL, GAPS)
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


def _fake_ssh(tmp_path, report, rc=0, coverage=None):
    stub = tmp_path / "bin" / "ssh"
    stub.parent.mkdir()
    out = tmp_path / "report.out"
    out.write_text("noise\n@@SKP-COVERAGE@@" + json.dumps(coverage or {}) + "\n@@SKP-REPORT@@" + json.dumps(report) + "\n")
    stub.write_text(f"#!/bin/sh\ncat > {tmp_path}/stdin\nprintf '%s\\n' \"$@\" > {tmp_path}/argv\n"
                    f"cat {out}\nexit {rc}\n")
    stub.chmod(0o755)
    return stub.parent


def _run(tmp_path, *extra, report=REPORT, rc=0, coverage=None):
    bindir = _fake_ssh(tmp_path, report, rc, coverage)
    log = tmp_path / "hook.log"
    hook = tmp_path / "hook.sh"
    # like the Talk hook: print a message id on raise, log the ref on resolve
    hook.write_text(f'#!/bin/sh\nprintf "%s|%s|%s%s\\n" "$1" "$2" "$3" "${{5:+|ref=$5}}" >> {log}\n'
                    '[ "$1" = raise ] && echo 4242\nexit 0\n')
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
                                                    "raise|sudo-1|sudo-denied:upgrade_package|warn"]
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
    assert log.read_text().splitlines()[3:] == ["resolve|repair-1|package-manager|info|ref=4242"]


def test_a_reprovisioned_host_resolves_its_sudo_denial_in_the_same_thread(tmp_path):
    _run(tmp_path)
    state = json.loads((tmp_path / "state" / "e.json").read_text())
    assert state["sudo-1|sudo-denied:upgrade_package"]["ref"] == "4242"
    (tmp_path / "bin").rename(tmp_path / "bin1")
    proc, log = _run(tmp_path, coverage={"sudo-1": {"provisioned_at": "2026-11-14T05:00:00Z", "actions": True}})
    assert log.read_text().splitlines()[3:] == ["resolve|sudo-1|sudo-denied:upgrade_package|info|ref=4242"]
    assert "sudo-1|sudo-denied:upgrade_package" not in json.loads((tmp_path / "state" / "e.json").read_text())


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
