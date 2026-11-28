#!/usr/bin/env python3
"""Enable Dependency-Track's OSV vulnerability source, idempotently.

Dependency-Track matches a component against NVD by CPE, and an SBOM from Trivy
(CycloneDX) carries package URLs (purls) and no CPEs, so with only NVD enabled
every project shows zero vulnerabilities however many components it has. The OSV
source matches by purl. It is a runtime setting that Dependency-Track keeps in
its own database (not in the environment), so a fresh deploy starts without it
and this script puts it back after the apiserver is up.

Reads one JSON document on stdin (the admin password never goes in argv or the
environment, and is never printed):

    {"url": "http://skbom-prod_apiserver:8080",
     "username": "admin", "password": "...",
     "ecosystems": "Go;npm;PyPI;...",   # google.osv.enabled, ';'-separated
     "alias_sync": true,                # google.osv.alias.sync.enabled
     "wait_seconds": 600}               # how long to wait for /health

It logs in, reads the config properties, and POSTs
/api/v1/configProperty/aggregate only for what differs, then reads them back.
Output is status lines only. Exit status: 0 done (unchanged or updated),
1 the apiserver never answered or a call failed, 2 the login was refused.
Standard library only; it runs in a throwaway python container.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GROUP = "vuln-source"
ENABLED = "google.osv.enabled"
ALIAS = "google.osv.alias.sync.enabled"


def normalize_ecosystems(value):
    """';'-joined, whitespace trimmed, empty items dropped, duplicates removed in order."""
    items = value if isinstance(value, (list, tuple)) else str(value).split(";")
    seen = []
    for item in (str(i).strip() for i in items):
        if item and item not in seen:
            seen.append(item)
    return ";".join(seen)


def same_value(name, have, want):
    if name == ENABLED:
        return set(normalize_ecosystems(have or "").split(";")) - {""} == set(want.split(";")) - {""}
    return str(have or "").strip().lower() == str(want).strip().lower()


def desired_properties(ecosystems, alias_sync):
    return [
        {"groupName": GROUP, "propertyName": ENABLED, "propertyValue": normalize_ecosystems(ecosystems)},
        {"groupName": GROUP, "propertyName": ALIAS, "propertyValue": "true" if alias_sync else "false"},
    ]


def pending(current, desired):
    """The desired properties whose stored value differs (or that are not stored)."""
    have = {(p.get("groupName"), p.get("propertyName")): p.get("propertyValue") for p in current}
    return [d for d in desired
            if not same_value(d["propertyName"], have.get((d["groupName"], d["propertyName"])), d["propertyValue"])]


class Api:
    def __init__(self, url):
        self.url = url.rstrip("/")
        self.token = None

    def call(self, method, path, body=None, form=None, timeout=30, accept="application/json"):
        data = None
        headers = {"Accept": accept}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as err:
            return err.code, err.read().decode("utf-8", "replace")


def wait_healthy(api, seconds, sleep=time.sleep, now=time.monotonic):
    deadline = now() + seconds
    while True:
        try:
            status, _ = api.call("GET", "/health", timeout=10)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        if now() >= deadline:
            return False
        sleep(5)


def run(cfg, sleep=time.sleep, now=time.monotonic, out=print):
    if not normalize_ecosystems(cfg.get("ecosystems", "")):
        out("osv: no ecosystems given (skbom.OSV_ECOSYSTEMS is empty)")
        return 1
    api = Api(cfg["url"])
    if not wait_healthy(api, int(cfg.get("wait_seconds", 600)), sleep, now):
        out("osv: the apiserver did not become healthy in time")
        return 1
    # The login answers with the JWT as text/plain; asking for JSON gets HTTP 406.
    status, body = api.call("POST", "/api/v1/user/login", accept="text/plain",
                            form={"username": cfg.get("username", "admin"), "password": cfg["password"]})
    if status != 200 or not body.strip():
        out(f"osv: login as {cfg.get('username', 'admin')} refused (HTTP {status}): check skbom.ADMIN_PASSWORD, "
            "and that the account's forced password change was done once in the UI")
        return 2
    api.token = body.strip()

    desired = desired_properties(cfg.get("ecosystems", ""), cfg.get("alias_sync", True))
    status, body = api.call("GET", "/api/v1/configProperty")
    if status != 200:
        out(f"osv: could not read the config properties (HTTP {status})")
        return 1
    todo = pending(json.loads(body), desired)
    if not todo:
        out("osv: unchanged (source already enabled with the wanted ecosystems)")
        return 0
    status, body = api.call("POST", "/api/v1/configProperty/aggregate", body=todo)
    if status not in (200, 201):
        out(f"osv: could not update the config properties (HTTP {status}): {body[:200]}")
        return 1
    status, body = api.call("GET", "/api/v1/configProperty")
    if status != 200 or pending(json.loads(body), desired):
        out("osv: the update was accepted but the settings did not read back as wanted")
        return 1
    out("osv: updated (" + ", ".join(d["propertyName"] for d in todo) + ")")
    return 0


def main():
    cfg = json.load(sys.stdin)
    return run(cfg)


if __name__ == "__main__":
    sys.exit(main())
