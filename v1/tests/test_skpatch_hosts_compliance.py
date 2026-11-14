"""skpatch-hosts fleet compliance (tools/skpatch_compliance.py and the
read-only dashboard report it relies on, src/skpatch-hosts/dashboard_report.js).

The analysis is tested on synthetic ansible.posix.json output and a synthetic
dashboard report, the report script against a fake dashboard API, and the
de-duplication through a real hook script."""
import base64
import hashlib
import hmac
import importlib.util
import json
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

V1 = Path(__file__).resolve().parents[1]
ROLE = V1 / "ansible" / "optional" / "skpatch-hosts"
TOOL = ROLE / "tools" / "skpatch_compliance.py"
REPORT_JS = ROLE / "src" / "skpatch-hosts" / "dashboard_report.js"

spec = importlib.util.spec_from_file_location("skpatch_compliance", TOOL)
sc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sc)

SUPPORTED = ["apt", "snap", "pacman", "flatpak"]


def task(name, **hosts):
    return {"task": {"name": name}, "hosts": hosts}


def result(*tasks, stats=None):
    return {"plays": [{"tasks": list(tasks)}, {"tasks": []}], "stats": stats or {}}


def system(name, hostname, detected=("apt",), disabled=(), denied=()):
    return {"id": 1, "name": name, "hostname": hostname, "port": 22, "detected": list(detected),
            "disabled": list(disabled), "sudo_denied": list(denied)}


HOSTS = {"ok-1": {"ansible_host": "10.0.0.1"}, "drift-1": {"ansible_host": "10.0.0.2"},
         "new-1": {"ansible_host": "10.0.0.3"}, "dnf-1": {"ansible_host": "10.0.0.4"},
         "down-1": {"ansible_host": "10.0.0.5"}, "pv-1": {"ansible_host": "10.0.0.6", "skpatch_hosts_paused": True},
         "nodash-1": {"ansible_host": "10.0.0.7"}, "alias-1": {"ansible_host": "a.example",
                                                             "skpatch_hosts_dashboard_address": "10.0.0.8"}}


def full_case():
    res = result(
        task("Report the posture for this host", **{h: {"changed": False} for h in HOSTS}),
        task(sc.REFUSE_TASK, **{"dnf-1": {"failed": True, "msg": "dnf-1 has package manager(s) dnf that ..."}}),
        task(sc.ACCOUNT_TASK, **{"new-1": {"changed": True}, "ok-1": {"changed": False}}),
        task("Write the sudoers rule (present)", **{"drift-1": {"changed": True}, "new-1": {"changed": True},
                                                    "ok-1": {"changed": False}}),
        task("Gathering Facts", **{"down-1": {"unreachable": True, "msg": "ssh: connect timed out"}}),
        stats={h: {"unreachable": 1 if h == "down-1" else 0} for h in HOSTS if h != "pv-1"},
    )
    report = {"systems": [
        system("ok-1", "10.0.0.1", detected=("apt", "snap")),
        system("drift-1", "10.0.0.2", denied=[{"action": "upgrade_package", "pkgManager": "apt",
                                                "at": "2026-11-14T02:34:04Z", "error": "sudo: a password is required"}]),
        system("whoami", "10.9.9.9"),
        system("old-name", "10.0.0.8", detected=("apt", "deb-get"), disabled=("deb-get",)),
        system("yum-box", "10.0.0.1", detected=("yum",)),
        system("pv-1", "10.0.0.6", detected=("apt", "dnf")),
        system("left-out", "10.0.0.20"),
    ]}
    outside = {"left-out": {"ansible_host": "10.0.0.20"}}
    return res, report, outside


def test_split_args_separates_inventory_limit_and_the_rest():
    inv, limit, rest = sc.split_args(["-i", "a", "--inventory=b", "-l", "g:!x", "-e", "k=v",
                                      "--vault-password-file", "f"])
    assert inv == ["-i", "a", "--inventory=b", "--vault-password-file", "f"]
    assert limit == "g:!x"
    assert rest == ["-e", "k=v", "--vault-password-file", "f"]


def test_analyse_classifies_every_issue_kind():
    res, report, outside = full_case()
    issues, status = sc.analyse(res, HOSTS, {"pv-1"}, report, SUPPORTED, outside)
    assert set(issues) == {
        "dnf-1|uncovered-manager",
        "new-1|not-provisioned",
        "drift-1|drift",
        "drift-1|sudo-denied:upgrade_package",
        "down-1|unreachable",
        "whoami|dashboard-only",
        "ok-1|uncovered-manager",       # "yum-box" matches ok-1 by address and detected yum
        "nodash-1|not-in-dashboard",
    }
    assert "Write the sudoers rule (present)" in issues["drift-1|drift"]
    assert status["pv-1"] == "paused (host var)"
    assert status["alias-1"] == "compliant"          # matched through skpatch_hosts_dashboard_address
    assert status["left-out"] == "not checked (outside --limit)"
    assert status["drift-1"] == "issues"


def test_a_paused_host_is_never_drift_and_its_dashboard_managers_are_not_judged():
    res, report, outside = full_case()
    issues, _ = sc.analyse(res, HOSTS, {"pv-1"}, report, SUPPORTED, outside)
    assert not [k for k in issues if k.startswith("pv-1|")]


def test_a_pause_marker_file_is_reported_as_paused_not_drift():
    res = result(task(sc.PAUSE_TASK, **{"ok-1": {"changed": False, "msg": "PAUSED"}}),
                 task("Write the sudoers rule (present)", **{"ok-1": {"skipped": True}}))
    issues, status = sc.analyse(res, {"ok-1": {}}, set(), {"systems": [system("ok-1", "x")]}, SUPPORTED)
    assert status["ok-1"] == "paused (/etc/skpatch-hosts.paused)"
    assert set(issues) == {"ok-1|paused-marker-file"}


def test_no_dashboard_report_is_an_issue_not_a_pass():
    issues, _ = sc.analyse(result(), {"ok-1": {}}, set(), None, SUPPORTED)
    assert "dashboard|no-report" in issues


def test_alerts_are_deduplicated_by_host_and_issue(tmp_path):
    log = tmp_path / "hook.log"
    hook = tmp_path / "hook.sh"
    hook.write_text(f'#!/bin/sh\nprintf "%s|%s|%s\\n" "$1" "$2" "$3" >> {log}\n')
    hook.chmod(0o755)
    state = tmp_path / "state" / "open.json"
    first = sc.reconcile(state, {"a|drift": "x", "b|unreachable": "y"}, str(hook))
    assert first == {"opened": ["a|drift", "b|unreachable"], "closed": []}
    sc.reconcile(state, {"a|drift": "x changed text", "b|unreachable": "y"}, str(hook))
    third = sc.reconcile(state, {"a|drift": "x"}, str(hook))
    assert third == {"opened": [], "closed": ["b|unreachable"]}
    assert log.read_text().splitlines() == ["raise|a|drift|warn", "raise|b|unreachable|warn",
                                            "resolve|b|unreachable|info"]
    assert set(json.loads(state.read_text())) == {"a|drift"}


def test_tool_never_writes_hosts_it_only_checks():
    text = TOOL.read_text()
    assert '"--check"' in text
    assert "skpatch_hosts_compliance_report" in text
    assert "with_exclusions(limit, paused)" in text  # paused hosts are never contacted


def test_paused_hosts_join_the_limit_as_one_ansible_pattern():
    assert sc.with_exclusions("grp:!a", {"p2", "p1"}) == "grp,!a,!p1,!p2"
    assert sc.with_exclusions("g1,g2:x", {"p"}) == "g1,g2:x,!p"
    assert sc.with_exclusions(None, {"p"}) == "all,!p"
    assert sc.with_exclusions("grp", set()) == "grp"


# --- playbook wiring --------------------------------------------------------

def test_report_step_is_read_only_and_runs_under_check():
    tasks = yaml.safe_load((ROLE / "tasks" / "register_systems.yml").read_text())
    rep = next(t for t in tasks if t["name"].startswith("Read the dashboard's systems"))
    save = next(t for t in tasks if t["name"] == "Save the compliance report on the controller")
    for t in (rep, save):
        assert t["check_mode"] is False and t["when"] == "skpatch_hosts_compliance_report is defined"
    assert rep["changed_when"] is False and rep["delegate_to"] == "{{ skp_reg_node }}"
    assert "dashboard_report.js" in rep["environment"]["SKP_REPORT_JS"]
    assert save["delegate_to"] == "localhost" and save["copy"]["mode"] == "0600"


def test_report_script_only_sends_get():
    js = REPORT_JS.read_text()
    assert "fetch(" in js and "method" not in js.split("async function get")[1].split("}\n")[0]
    for verb in ('"POST"', '"PUT"', '"DELETE"', '"PATCH"'):
        assert verb not in js


# --- the report script against a fake dashboard ---------------------------

SECRET = "report-test-secret"


def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class FakeDash:
    def __init__(self, db):
        self.db = db
        self.methods = set()
        now = time.time()
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))  # noqa: E731
        self.systems = [
            {"id": 1, "name": "mgr-1", "hostname": "10.0.0.1", "port": 22, "pkgManager": "apt",
             "detectedPkgManagers": ["apt", "snap"], "disabledPkgManagers": None},
            {"id": 2, "name": "arch-1", "hostname": "10.0.0.2", "port": 22, "pkgManager": "pacman",
             "detectedPkgManagers": '["pacman","flatpak"]', "disabledPkgManagers": "[]"},
        ]
        self.history = {
            1: [{"action": "upgrade_package", "pkgManager": "apt", "status": "failed", "startedAt": fmt(now - 3600),
                 "error": "sudo: a password is required\n", "output": ""},
                {"action": "check", "pkgManager": "apt,snap", "status": "failed", "startedAt": fmt(now - 7200),
                 "error": "sudo: a password is required", "output": ""},
                {"action": "check", "pkgManager": "apt", "status": "success", "startedAt": fmt(now - 60),
                 "error": None, "output": "ok"},
                {"action": "reboot", "pkgManager": "", "status": "failed", "startedAt": fmt(now - 40 * 86400),
                 "error": "sudo: a password is required", "output": ""}],
            2: [{"action": "check", "pkgManager": "pacman,flatpak", "status": "warning", "startedAt": fmt(now - 100),
                 "error": "[pacman]\nWe trust you have received the usual lecture from the local System", "output": ""},
                {"action": "upgrade", "pkgManager": "pacman", "status": "failed", "startedAt": fmt(now - 100),
                 "error": "error: target not found: foo", "output": ""}],
        }

    def user_ok(self, headers):
        cookies = dict(p.strip().split("=", 1) for p in headers.get("Cookie", "").split(";") if "=" in p)
        h, p, sig = cookies["ludash_session"].split(".")
        want = hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        claims = json.loads(_b64d(p))
        row = sqlite3.connect(self.db).execute("SELECT session_version FROM users WHERE id = ?",
                                               (claims["userId"],)).fetchone()
        return hmac.compare_digest(want, _b64d(sig)) and row and row[0] == claims["sessionVersion"]


@pytest.fixture
def fake(tmp_path):
    if shutil.which("node") is None:
        pytest.skip("node not installed")
    db = tmp_path / "data" / "dashboard.db"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, session_version INTEGER, "
                "auth_provider TEXT, role TEXT)")
    con.execute("INSERT INTO users VALUES (1, 'admin', 2, 'password', 'admin')")
    con.commit()
    con.close()
    (db.parent / ".secret_key").write_text(SECRET + "\n")
    d = FakeDash(db)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            d.methods.add("GET")
            if not d.user_ok(self.headers):
                return self._send(401, {"error": "Unauthorized"})
            if self.path == "/api/systems":
                return self._send(200, {"systems": d.systems})
            if self.path.startswith("/api/systems/") and self.path.endswith("/history"):
                return self._send(200, {"history": d.history[int(self.path.split("/")[3])]})
            return self._send(404, {})

        def do_POST(self):
            d.methods.add("POST")
            self._send(405, {})

        do_PUT = do_DELETE = do_POST

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d.port = srv.server_address[1]
    yield d
    srv.shutdown()


def test_report_lists_systems_and_only_the_latest_sudo_denial_per_action(fake):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "LUDASH_DB_PATH": str(fake.db), "LUDASH_PORT": str(fake.port)}
    proc = subprocess.run(["node", "--no-warnings", "-e", REPORT_JS.read_text()],
                          input=json.dumps({"register_as": "admin", "history_days": 14}),
                          capture_output=True, text=True, timeout=60, env=env)
    assert proc.returncode == 0, proc.stderr
    line = next(l for l in proc.stdout.splitlines() if l.startswith("@@SKP-REPORT@@"))
    systems = {s["name"]: s for s in json.loads(line[len("@@SKP-REPORT@@"):])["systems"]}
    assert systems["mgr-1"]["detected"] == ["apt", "snap"] and systems["mgr-1"]["disabled"] == []
    assert systems["arch-1"]["detected"] == ["pacman", "flatpak"]
    # the failed check is cleared by the later successful one; the 40-day-old
    # reboot is outside the window; the upgrade denial is current
    assert [d["action"] for d in systems["mgr-1"]["sudo_denied"]] == ["upgrade_package"]
    assert systems["mgr-1"]["sudo_denied"][0]["error"] == "sudo: a password is required"
    # sudo's lecture means it asked for a password: same class; a plain
    # pacman error is not a sudo denial
    assert [d["action"] for d in systems["arch-1"]["sudo_denied"]] == ["check"]
    assert fake.methods == {"GET"}


# --- shipping: timer unit and the example hook ------------------------------

def test_systemd_unit_treats_open_issues_as_a_successful_run():
    unit = (ROLE / "tools" / "systemd" / "skpatch-compliance@.service").read_text()
    assert "SuccessExitStatus=1" in unit and "Type=oneshot" in unit
    assert '--state "$STATE_DIRECTORY/%i.json"' in unit and "StateDirectory=skpatch-compliance" in unit
    assert "EnvironmentFile=%h/.config/skpatch-compliance/%i.env" in unit
    timer = (ROLE / "tools" / "systemd" / "skpatch-compliance@.timer").read_text()
    assert "OnCalendar=" in timer and "Persistent=true" in timer


def test_example_hook_maps_raise_and_resolve_to_one_thread_per_key(tmp_path):
    log = tmp_path / "calls"
    stub = tmp_path / "bin" / "sk-alert"
    stub.parent.mkdir()
    stub.write_text(f'#!/bin/sh\nfor a in "$@"; do printf "[%s]" "$a"; done >> {log}\necho >> {log}\n')
    stub.chmod(0o755)
    hook = ROLE / "tools" / "skpatch-compliance-alert.example.sh"
    env = {"PATH": f"{stub.parent}:/usr/bin:/bin"}
    for args in (["raise", "host-1|sudo-denied:check", "warn", "msg one"],
                 ["resolve", "host-1|sudo-denied:check", "info", "cleared"]):
        assert subprocess.run(["sh", str(hook), *args], env=env).returncode == 0
    assert log.read_text().splitlines() == [
        "[-l][warn][-g][skpatch/host-1/sudo-denied/check][--title][skpatch: host-1|sudo-denied:check][msg one]",
        "[-g][skpatch/host-1/sudo-denied/check][--resolve][cleared]",
    ]
    assert subprocess.run(["sh", str(hook), "bogus", "k", "info", "m"], env=env, capture_output=True).returncode == 2


def test_dns_conflict_warning_becomes_its_own_issue():
    res = result(task(sc.DNS_TASK, **{"ok-1": {"changed": False, "msg": "ok-1 serves DNS and has a port 53 conflict: netbird ..."},
                                     "ok-2": {"skipped": True}}))
    hosts = {"ok-1": {}, "ok-2": {}}
    issues, _ = sc.analyse(res, hosts, set(), {"systems": [system("ok-1", "a"), system("ok-2", "b")]}, SUPPORTED)
    assert set(issues) == {"ok-1|dns-conflict"} and "netbird" in issues["ok-1|dns-conflict"]
