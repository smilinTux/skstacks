#!/usr/bin/env python3
"""Complete Uptime Kuma's own first-run setup end to end, from inside the
container, against its own localhost port -- never through the public
SSO-gated router, since this is Kuma's own admin bootstrap, not a user
login.

A fresh Kuma comes up in "setup-database" mode: a separate, lightweight
temp HTTP server (no socket.io yet) that only answers /api/entry-page and
POST /setup-database. Once that POST succeeds (for AUTH_MODE's purposes,
always {"dbConfig": {"type": "sqlite"}}: this framework never runs
anything else), the temp server closes and the real server.js starts,
socket.io mounts, and the first admin account is created via the "setup"
event (username, password). AUTH_MODE: sso then wants disableAuth set via
"setSettings" -- same write the existing "Align Kuma's own login
(disableAuth) with AUTH_MODE" task already makes directly against
sqlite once a user exists, so doing it here too (through the supported
API, immediately after creating that very user, with a login+verify in
between) is a deliberate belt-and-suspenders overlap, not a second
implementation to maintain: same value, same idempotent INSERT ... ON
CONFLICT ... underneath either way, and this script additionally proves
the just-created account can actually log in before moving on.

Reused by two callers: the role's own fresh-install task (setup only is
load-bearing there; the login+setSettings tail is this redundant but
harmless proof), and an operator completing first-run setup by hand on
an existing instance (nor, NAM) exactly as done live on nor 2026-10-24.

Reads {"host": "...", "port": 3001} from argv[1] (no secrets) and a
password from argv[2] (a local file, never a command-line argument).
Generates nothing itself: the password is the Ansible role's own
`password` lookup (or, run by hand, whatever the operator already
generated into that file). Prints only step/status lines, never the
password.
"""
import json
import socket as socketlib
import sys
import time
import urllib.error
import urllib.request

RS = "\x1e"
TIMEOUT = 20
USERNAME = "skadmin"


def http_get(base, path):
    with urllib.request.urlopen(base + path, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read().decode())


def http_post_json(base, path, body):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST",
                                  headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def sio_get(base, sid=None):
    url = base + "/socket.io/?EIO=4&transport=polling" + (f"&sid={sid}" if sid else "")
    with urllib.request.urlopen(url, timeout=TIMEOUT) as r:
        return r.read().decode()


def sio_post(base, sid, body):
    url = base + f"/socket.io/?EIO=4&transport=polling&sid={sid}"
    req = urllib.request.Request(url, data=body.encode(), method="POST",
                                  headers={"Content-Type": "text/plain;charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode()


def packets(raw):
    return [p for p in raw.split(RS) if p]


def sio_poll_for(base, sid, prefix, tries=40):
    # A busy instance broadcasts its full live state (every monitor's
    # heartbeat/uptime/certInfo, one "42" packet each) to every connecting
    # socket immediately; the ack we want can end up dozens of packets
    # deep, taking several polls to surface (seen live: buried behind 180+
    # packets on one run, immediate on another). 40 is generous on
    # purpose; this is a one-time bootstrap script, not a hot path.
    for i in range(tries):
        got = packets(sio_get(base, sid))
        if i < 3 or len(got) > 5:
            print(f"debug: poll {i} got {len(got)} packet(s)")
        for p in got:
            if p.startswith(prefix):
                return p
            if p.startswith("1"):
                raise RuntimeError("engine.io session closed by server")
        time.sleep(0.3)
    raise RuntimeError(f"no packet with prefix {prefix!r} after {tries} polls")


def sio_connect(base):
    opening = packets(sio_get(base))[0]
    sid = json.loads(opening[1:])["sid"]
    sio_post(base, sid, "40")
    sio_poll_for(base, sid, "40")
    return sid


def sio_emit_ack(base, sid, ack_id, event, *args):
    body = f"4{2}{ack_id}" + json.dumps([event, *args])
    sio_post(base, sid, body)
    ack = sio_poll_for(base, sid, f"4{3}{ack_id}")
    return json.loads(ack[3:])[0]


def main():
    cfg = json.load(open(sys.argv[1]))
    password = open(sys.argv[2]).read().strip()
    base = f"http://{cfg['host']}:{cfg['port']}"

    status, entry = http_get(base, "/api/entry-page")
    print(f"step: /api/entry-page -> {entry.get('type')!r}")

    if entry.get("type") == "setup-database":
        status, resp = http_post_json(base, "/setup-database", {"dbConfig": {"type": "sqlite"}})
        if status != 200 or not resp.get("ok"):
            print(f"FAIL: /setup-database -> HTTP {status} {resp!r}")
            return 1
        print("step: /setup-database (sqlite) -> ok, main server starting")
        # the temp server closes and server.js needs a moment to bind the same port
        for _ in range(20):
            time.sleep(1)
            try:
                status, entry = http_get(base, "/api/entry-page")
                if entry.get("type") != "setup-database":
                    break
            except (urllib.error.URLError, OSError):
                continue
        else:
            print("FAIL: main server did not come up after /setup-database")
            return 1
        print(f"step: main server is up, /api/entry-page -> {entry.get('type')!r}")
    else:
        print("step: database already configured, skipping /setup-database")

    sid = sio_connect(base)
    need = sio_emit_ack(base, sid, 0, "needSetup")
    if need:
        r = sio_emit_ack(base, sid, 1, "setup", USERNAME, password)
        if not r.get("ok"):
            print(f"FAIL: setup({USERNAME!r}, ...) -> {r!r}")
            return 1
        print(f"step: setup({USERNAME!r}, ...) -> ok (first admin created)")
    else:
        print("step: an admin already exists, skipping setup")

    sid = sio_connect(base)
    r = sio_emit_ack(base, sid, 0, "login", {"username": USERNAME, "password": password, "token": ""})
    if not r.get("ok"):
        print(f"FAIL: login as {USERNAME!r} -> {r.get('msg', 'no message')!r}")
        return 1
    print(f"step: logged in as {USERNAME!r}")

    r = sio_emit_ack(base, sid, 1, "setSettings", {"disableAuth": True}, password)
    if not r.get("ok"):
        print(f"FAIL: setSettings(disableAuth=true) -> {r!r}")
        return 1
    print("PASS: first-run setup complete, admin created, disableAuth=true")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"FAIL: {exc!r}")
        sys.exit(1)
