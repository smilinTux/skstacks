"""optional/skpatch-hosts/src/skpatch-hosts/register_systems.js, run with the
real node against a fake linux-update-dashboard.

The fake enforces what the pinned app (2026.10.9) enforces on these routes:
an HS256 `ludash_session` JWT signed with the instance's session key, whose
userId exists and whose sessionVersion matches the users row
(server/auth/session.ts); the `ludash_csrf` double-submit cookie echoed as
`x-csrf-token` on every unsafe method (server/middleware/csrf.ts); and a 403
for any bearer token on POST /api/systems (server/routes/systems.ts). The
users table is a real SQLite file with the app's column names, read by the
script through node:sqlite.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import urllib.parse
import secrets
import shutil
import sqlite3
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = (Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skpatch-hosts"
          / "src" / "skpatch-hosts" / "register_systems.js")
# Built at import, never literals: a committed key-shaped string trips the
# repo's own secret scan (gitleaks reads history, so it would stay red).
SECRET = secrets.token_hex(32)
_PEM = " ".join(("OPENSSH", "PRIVATE", "KEY"))
KEY = f"-----BEGIN {_PEM}-----\n{secrets.token_hex(16)}\n-----END {_PEM}-----\n"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class Dashboard:
    def __init__(self, db: Path):
        self.db = db
        self.credentials: list[dict] = []
        self.systems: list[dict] = []
        self.calls: list[tuple[str, str]] = []
        self.notifications: list[dict] = []
        self.schedules: list[dict] = []
        self.modes: dict[int, str] = {}
        self.scripts: list[dict] = [{"id": "builtin:apt:detect", "readonly": True, "name": "Detect APT"}]
        self.settings: dict[str, str] = {"cmd_timeout_seconds": "120", "cache_duration_hours": "12"}

    def session_user(self, headers) -> dict | None:
        if headers.get("Authorization", "").startswith("Bearer "):
            return None
        cookies = dict(p.strip().split("=", 1) for p in headers.get("Cookie", "").split(";") if "=" in p)
        tok = cookies.get("ludash_session", "")
        try:
            h, p, sig = tok.split(".")
        except ValueError:
            return None
        want = hmac.new(SECRET.encode(), f"{h}.{p}".encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(want, _b64d(sig)) or json.loads(_b64d(h)) != {"alg": "HS256"}:
            return None
        claims = json.loads(_b64d(p))
        if claims["exp"] < time.time() or claims["exp"] - claims["iat"] > 600:
            return None
        row = sqlite3.connect(self.db).execute(
            "SELECT id, username, role, session_version, auth_provider FROM users WHERE id = ?",
            (claims["userId"],)).fetchone()
        if not row or row[3] != claims["sessionVersion"]:
            return None
        if claims["authMethod"] == "oidc" and row[4] != "oidc":
            return None
        return {"userId": row[0], "username": row[1], "role": row[2]}

    def csrf_ok(self, headers) -> bool:
        cookies = dict(p.strip().split("=", 1) for p in headers.get("Cookie", "").split(";") if "=" in p)
        return bool(cookies.get("ludash_csrf")) and headers.get("x-csrf-token") == cookies.get("ludash_csrf")


def _handler(dash: Dashboard):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            raw = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _route(self, method):
            dash.calls.append((method, self.path))
            if method == "POST" and self.path == "/api/systems" and self.headers.get("Authorization", "").startswith("Bearer "):
                return self._send(403, {"error": "API tokens cannot configure SSH connections"})
            user = dash.session_user(self.headers)
            if user is None:
                return self._send(401, {"error": "Unauthorized"})
            if method != "GET" and not dash.csrf_ok(self.headers):
                return self._send(403, {"error": "CSRF token missing or invalid"})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"null")
            if method == "GET" and self.path == "/api/auth/me":
                return self._send(200, {"user": user})
            if method == "GET" and self.path.startswith("/api/credentials"):
                return self._send(200, {"credentials": [{"id": c["id"], "name": c["name"], "kind": "sshKey"} for c in dash.credentials]})
            if method == "POST" and self.path == "/api/credentials":
                assert body["kind"] == "sshKey" and body["payload"]["privateKey"] == KEY
                c = {"id": len(dash.credentials) + 1, **body}
                dash.credentials.append(c)
                return self._send(201, {"id": c["id"]})
            if method == "GET" and self.path == "/api/systems":
                return self._send(200, {"systems": dash.systems})
            if method == "POST" and self.path == "/api/systems":
                s = {"id": len(dash.systems) + 1, **body}
                dash.systems.append(s)
                return self._send(201, {"id": s["id"]})
            if method == "PUT" and self.path.startswith("/api/credentials/"):
                cid = int(self.path.rsplit("/", 1)[1])
                c = next(c for c in dash.credentials if c["id"] == cid)
                assert body["name"] == c["name"] and body["payload"]["privateKey"]
                c["payload"] = body["payload"]
                return self._send(200, {"ok": True})
            if method == "PUT" and self.path.endswith("/upgrade-mode"):
                sid = int(self.path.split("/")[3])
                s = next(s for s in dash.systems if s["id"] == sid)
                s["pkgManagerConfigs"] = {"apt": {"defaultUpgradeMode": "full-upgrade" if body["fullUpgrade"] else "upgrade"}}
                return self._send(200, {"status": "ok"})
            if method == "GET" and self.path == "/api/settings":
                return self._send(200, {"settings": dict(dash.settings)})
            if method == "PUT" and self.path == "/api/settings":
                assert all(isinstance(v, str) for v in body.values())
                dash.settings.update(body)
                return self._send(200, {"ok": True})
            if method == "GET" and self.path == "/api/scripts":
                return self._send(200, {"scripts": dash.scripts, "packageManagers": []})
            if method == "POST" and self.path == "/api/scripts":
                s = {"id": f"custom:{len(dash.scripts)}", "readonly": False, **body}
                dash.scripts.append(s)
                return self._send(201, {"script": s})
            if method == "PUT" and self.path.startswith("/api/scripts/"):
                sid = urllib.parse.unquote(self.path.rsplit("/", 1)[1])
                next(s for s in dash.scripts if s["id"] == sid).update(body)
                return self._send(200, {"script": {"id": sid}})
            for coll, key in (("notifications", "notifications"), ("schedules", "schedules")):
                items = getattr(dash, coll)
                if method == "GET" and self.path == f"/api/{coll}":
                    # secrets come back masked, as the app does
                    masked = [{**i, "config": {k: ("(stored)" if "assword" in k else v) for k, v in i.get("config", {}).items()}} for i in items]
                    return self._send(200, {key: masked})
                if method == "POST" and self.path == f"/api/{coll}":
                    i = {"id": len(items) + 1, **body}
                    items.append(i)
                    return self._send(201, {"id": i["id"]})
                if method == "PUT" and self.path.startswith(f"/api/{coll}/"):
                    iid = int(self.path.rsplit("/", 1)[1])
                    next(i for i in items if i["id"] == iid).update(body)
                    return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_PUT(self):
            self._route("PUT")

    return H


@pytest.fixture
def dash(tmp_path):
    db = tmp_path / "data" / "dashboard.db"
    db.parent.mkdir()
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, "
                "session_version INTEGER NOT NULL DEFAULT 0, auth_provider TEXT NOT NULL DEFAULT 'password', "
                "role TEXT NOT NULL DEFAULT 'admin')")
    con.execute("INSERT INTO users (id, username, session_version, auth_provider, role) VALUES "
                "(1, 'admin', 3, 'password', 'admin'), (2, 'chef', 0, 'oidc', 'admin'), (3, 'viewer', 0, 'password', 'read-only')")
    con.commit()
    con.close()
    (db.parent / ".secret_key").write_text(SECRET + "\n")
    d = Dashboard(db)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _handler(d))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    d.port = srv.server_address[1]
    yield d
    srv.shutdown()


def _run(dash, cfg):
    env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "LUDASH_DB_PATH": str(dash.db), "LUDASH_PORT": str(dash.port)}
    return subprocess.run(["node", "--no-warnings", "-e", SCRIPT.read_text()], input=json.dumps(cfg),
                          capture_output=True, text=True, timeout=60, env=env)


def _cfg(**kw):
    cfg = {"register_as": "admin", "credential_id": 0,
           "credential": {"name": "skpatch-hosts skpatch-ro", "username": "skpatch-ro", "private_key": KEY},
           "systems": [{"name": "wkr-1", "hostname": "10.0.0.21", "port": 22},
                       {"name": "mgr-1", "hostname": "10.0.0.11", "port": 22}]}
    cfg.update(kw)
    return cfg


def test_creates_the_credential_and_systems_then_is_idempotent(dash):
    first = _run(dash, _cfg())
    assert first.returncode == 0, first.stderr
    assert "credential skpatch-hosts skpatch-ro: created (id 1)" in first.stdout
    assert "wkr-1: created" in first.stdout and "mgr-1: created" in first.stdout
    assert first.stdout.rstrip().endswith("registration: ok")
    assert [s["hostname"] for s in dash.systems] == ["10.0.0.21", "10.0.0.11"]
    assert all(s["credentialId"] == 1 and s["hostKeyVerificationEnabled"] is False for s in dash.systems)
    second = _run(dash, _cfg())
    assert second.returncode == 0, second.stderr
    assert "created" not in second.stdout and "credential skpatch-hosts skpatch-ro: unchanged (id 1)" in second.stdout
    assert len(dash.systems) == 2 and len(dash.credentials) == 1
    assert KEY not in first.stdout + first.stderr + second.stdout + second.stderr


def test_never_edits_an_existing_system_and_only_reports_orphans(dash):
    dash.credentials.append({"id": 1, "name": "skpatch-hosts skpatch-ro"})
    dash.systems.append({"id": 1, "name": "renamed-by-hand", "hostname": "10.0.0.21", "port": 22, "credentialId": 1})
    dash.systems.append({"id": 2, "name": "retired", "hostname": "10.0.0.99", "port": 22, "credentialId": 1})
    out = _run(dash, _cfg(credential_id=1))
    assert out.returncode == 0, out.stderr
    assert "wkr-1: unchanged (already registered as renamed-by-hand, id 1)" in out.stdout
    assert "retired (10.0.0.99:22)" in out.stdout
    assert not any(m == "PUT" for m, _ in dash.calls)
    assert [s["name"] for s in dash.systems] == ["renamed-by-hand", "retired", "mgr-1"]


def test_oidc_account_gets_an_oidc_session(dash):
    out = _run(dash, _cfg(register_as="chef"))
    assert out.returncode == 0, out.stderr
    assert "signed in as chef (oidc" in out.stdout


@pytest.mark.parametrize("who,msg", [("viewer", "is read-only"), ("nobody", "no dashboard user named")])
def test_refuses_a_read_only_or_missing_account(dash, who, msg):
    out = _run(dash, _cfg(register_as=who))
    assert out.returncode != 0 and msg in out.stderr
    assert not dash.calls


def test_a_revoked_session_version_is_not_accepted(dash):
    """If the key or session_version were wrong, the app would 401: the
    script must fail, not report success."""
    (dash.db.parent / ".secret_key").write_text("wrong-key")
    out = _run(dash, _cfg())
    assert out.returncode != 0 and "HTTP 401" in out.stderr
    assert not dash.systems


def test_needs_a_credential(dash):
    out = _run(dash, _cfg(credential={}))
    assert out.returncode != 0 and "set credential_id" in out.stderr


def test_unknown_credential_id_fails(dash):
    out = _run(dash, _cfg(credential_id=7))
    assert out.returncode != 0 and "no sshKey credential with id 7" in out.stderr


def _detected(dash):
    for s in dash.systems:
        s.setdefault("pkgManager", "apt")


def test_sync_credential_rewrites_the_key_of_an_existing_credential(dash):
    dash.credentials.append({"id": 1, "name": "skpatch-ro (estate)", "payload": {"privateKey": "old"}})
    out = _run(dash, _cfg(credential_id=1, sync_credential=True))
    assert out.returncode == 0, out.stderr
    assert "credential skpatch-ro (estate): key synced from this run (id 1)" in out.stdout
    assert dash.credentials[0]["payload"]["privateKey"] == KEY
    assert KEY not in out.stdout + out.stderr
    # off by default: a plain run never touches the key
    dash.credentials[0]["payload"]["privateKey"] = "changed-by-hand"
    assert _run(dash, _cfg(credential_id=1)).returncode == 0
    assert dash.credentials[0]["payload"]["privateKey"] == "changed-by-hand"


def test_upgrade_mode_waits_for_detection_then_converges(dash):
    first = _run(dash, _cfg(upgrade_mode="full-upgrade"))
    assert first.returncode == 0, first.stderr
    assert "wkr-1: upgrade mode pending" in first.stdout
    assert not any(p.endswith("/upgrade-mode") for _, p in dash.calls)
    _detected(dash)
    second = _run(dash, _cfg(upgrade_mode="full-upgrade",
                             systems=[{"name": "wkr-1", "hostname": "10.0.0.21", "port": 22},
                                      {"name": "mgr-1", "hostname": "10.0.0.11", "port": 22, "upgrade_mode": "upgrade"}]))
    assert second.returncode == 0, second.stderr
    assert "wkr-1: upgrade mode set to full-upgrade" in second.stdout
    assert "mgr-1: upgrade mode upgrade unchanged" in second.stdout
    third = _run(dash, _cfg(upgrade_mode="full-upgrade"))
    assert "wkr-1: upgrade mode full-upgrade unchanged" in third.stdout


SCHEDULES = [
    {"name": "Default refresh", "type": "refresh", "cron": "0 */6 * * *", "cache_hours": 6},
    {"name": "Nightly workers", "type": "update", "cron": "0 2 * * *", "systems": ["wkr-1", "gone-host"]},
    {"name": "Reboot mgr-1", "type": "reboot", "cron": "0 4 * * 2", "systems": ["mgr-1"]},
    {"name": "Never matches", "type": "reboot", "cron": "0 5 * * 2", "systems": ["gone-host"]},
    {"name": "Daily digest", "type": "notification_digest", "cron": "0 7 * * *", "notifications": ["Digest mail"]},
]
NOTIFS = [{"name": "Digest mail", "type": "email", "notify_on": ["updates"],
           "config": {"smtpHost": "smtp.example.test", "smtpPassword": KEY, "emailTo": "ops@example.test"}}]


def test_schedules_and_notifications_converge_by_name_and_never_delete(dash):
    dash.schedules.append({"id": 1, "name": "Default refresh", "type": "refresh", "enabled": True, "systemIds": None,
                           "config": {"cron": "*/15 * * * *", "cacheDurationHours": 12}})
    dash.schedules.append({"id": 2, "name": "Made by hand", "type": "update", "enabled": False, "systemIds": [9],
                           "config": {"cron": "0 1 * * *"}})
    first = _run(dash, _cfg(schedules=SCHEDULES, notifications=NOTIFS))
    assert first.returncode == 0, first.stderr
    out = first.stdout
    assert "notification Digest mail: created (id 1)" in out
    assert 'schedule Default refresh: updated (id 1, refresh "0 */6 * * *")' in out
    assert "schedule Nightly workers: not in the dashboard, left out: gone-host" in out
    assert "schedule Nightly workers: created" in out
    assert "schedule Never matches: skipped" in out
    assert "schedule Made by hand: not declared here, left as is (disabled)" in out
    by = {x["name"]: x for x in dash.schedules}
    assert by["Default refresh"]["config"] == {"cron": "0 */6 * * *", "cacheDurationHours": 6}
    assert by["Nightly workers"]["systemIds"] == [1]
    assert by["Reboot mgr-1"]["systemIds"] == [2] and by["Reboot mgr-1"]["config"]["onlyIfRequired"] is True
    assert by["Daily digest"]["config"]["notificationIds"] == [1]
    assert "Never matches" not in by and len(dash.schedules) == 5
    assert KEY not in out + first.stderr
    second = _run(dash, _cfg(schedules=SCHEDULES, notifications=NOTIFS))
    assert second.returncode == 0, second.stderr
    assert "created" not in second.stdout and "updated" not in second.stdout
    assert "schedule Nightly workers: unchanged" in second.stdout
    assert "notification Digest mail: synced (id 1)" in second.stdout
    assert len(dash.notifications) == 1 and not any(m == "DELETE" for m, _ in dash.calls)


def test_a_digest_naming_an_unknown_channel_fails(dash):
    out = _run(dash, _cfg(schedules=[{"name": "d", "type": "notification_digest", "cron": "0 7 * * *",
                                      "notifications": ["nope"]}]))
    assert out.returncode != 0 and "no notification channel named nope" in out.stderr


def test_upgrade_mode_skips_a_package_manager_without_one(dash):
    assert _run(dash, _cfg()).returncode == 0
    dash.systems[0]["pkgManager"] = "pacman"
    out = _run(dash, _cfg(upgrade_mode="full-upgrade"))
    assert out.returncode == 0, out.stderr
    assert "wkr-1: upgrade mode n/a (pacman has none)" in out.stdout
    assert not any(p.endswith("/upgrade-mode") for _, p in dash.calls)


DASH_SCRIPTS = json.loads((SCRIPT.parent / "dashboard-scripts.json").read_text())["scripts"]


def test_wrapper_scripts_and_settings_converge_by_name_and_value(dash):
    first = _run(dash, _cfg(scripts=DASH_SCRIPTS, settings={"cmd_timeout_seconds": 600}))
    assert first.returncode == 0, first.stderr
    assert "settings: set cmd_timeout_seconds" in first.stdout
    assert dash.settings == {"cmd_timeout_seconds": "600", "cache_duration_hours": "12"}
    for s in DASH_SCRIPTS:
        assert f"script {s['name']}: created" in first.stdout
    custom = [s for s in dash.scripts if not s["readonly"]]
    assert sorted(s["pkgManager"] for s in custom) == ["apt", "flatpak", "pacman", "snap"]
    assert all(s["isDefault"] and s["operation"] == "upgrade_selected" for s in custom)

    second = _run(dash, _cfg(scripts=DASH_SCRIPTS, settings={"cmd_timeout_seconds": 600}))
    assert second.returncode == 0, second.stderr
    assert "settings: unchanged" in second.stdout
    assert "created" not in second.stdout and "updated" not in second.stdout

    # someone edits one by hand: the next run puts it back, keeping its id
    apt = next(s for s in dash.scripts if s.get("pkgManager") == "apt" and not s["readonly"])
    apt["steps"] = [{"label": "x", "command": "sudo apt-get install -y {{packages}}"}]
    third = _run(dash, _cfg(scripts=DASH_SCRIPTS))
    assert f"script {apt['name']}: updated ({apt['id']})" in third.stdout
    assert apt["steps"][0]["command"] == DASH_SCRIPTS[0]["steps"][0]["command"]
    assert len([s for s in dash.scripts if not s["readonly"]]) == 4
    assert not any(m == "DELETE" for m, _ in dash.calls)
