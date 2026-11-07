#!/usr/bin/env python3
"""Register this estate's provisioned nodes as linux-update-dashboard
systems, idempotently, never deleting.

Why this exists: without it, a fresh skpatch-hosts dashboard is an empty
shell until someone adds every node by hand through the UI. This upserts
one system per node provision_skpatch_hosts_nodes.yml just provisioned,
so the dashboard is never empty after a deploy.

Auth: a dashboard API token (Settings > API Tokens, `systems:add` and
`systems:edit` permissions, created once by a signed-in admin -- local or
OIDC, either works, since token auth is a separate path from session
login and is unaffected by AUTH_MODE/LUDASH_PASSWORD_LOGIN_ENABLED).
`POST /api/credentials` (creating the SSH credential object itself) is
OUT OF SCOPE for every API token, by design on the upstream app's own
token-scope list -- so the SSH credential this script points every
system at (`credential_id`) must already exist, created once by hand in
the dashboard (kind sshKey, username = skpatch_hosts.PROVISION_USER,
private key = the SAME estate keypair whose public half
provision_skpatch_hosts_nodes.yml installs). See README.md "Automatic
host registration" for the full one-time bootstrap.

Identity / idempotency: the upstream app's own uniqueness constraint is
(hostname, port, username), with username resolved from the credential
-- confirmed from the pinned source (server/services/system-service.ts,
DuplicateSystemConnectionError). Every system this script creates shares
the one estate credential, so hostname+port alone is this script's own
match key. A name-only change updates in place (PUT); a system whose
hostname/port is not in the current run's target list is reported, NEVER
deleted -- removing a host from inventory does not remove its history.

Host-key verification: created with hostKeyVerificationEnabled=false.
These are the operator's own already-provisioned fleet nodes, reached
over the same estate keypair provision_skpatch_hosts_nodes.yml just
installed; strict host-key pinning is for a human adding an individual
system through the UI, not this bulk sync. An operator wanting it for a
given system can turn it on by hand in the dashboard afterward.

Input: one JSON object on stdin (secrets included, so nothing is on a
command line or in a process environment):
  url            - the dashboard's internal URL (see tasks that build this)
  token          - the API token (Bearer)
  credential_id  - the SSH credential this script registers every system
                    against (created once by hand, see above)
  systems        - [{"name": ..., "hostname": ..., "port": 22}, ...] --
                    the full target list for this run (one per node
                    provision_skpatch_hosts_nodes.yml just provisioned)
Output: one status line per system, plus a final section listing any
system under this credential_id that this run's target list no longer
names (reported, never removed). Exit non-zero on any API error.
"""
import json
import sys
import urllib.error
import urllib.request


class ApiError(Exception):
    pass


class Api:
    def __init__(self, url, token):
        self.base = url.rstrip("/") + "/api"
        self.token = token

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        if status not in ok:
            raise ApiError(f"{method} {path}: HTTP {status}: {raw[:400].decode(errors='replace')}")
        return json.loads(raw) if raw else None


def list_systems(api):
    return api.call("GET", "/systems").get("systems", [])


def ensure_system(api, existing_by_key, credential_id, wanted):
    key = (wanted["hostname"], int(wanted.get("port") or 22))
    current = existing_by_key.get(key)
    desired = {
        "name": wanted["name"],
        "hostname": wanted["hostname"],
        "port": int(wanted.get("port") or 22),
        "credentialId": credential_id,
        "hostKeyVerificationEnabled": False,
    }
    if current is None:
        obj = api.call("POST", "/systems", desired)
        print(f"{wanted['name']}: created")
        return {**desired, "id": obj["id"], "username": None}
    if current.get("name") == desired["name"] and current.get("credentialId") == credential_id:
        print(f"{wanted['name']}: unchanged")
        return current
    api.call("PUT", f"/systems/{current['id']}", desired)
    print(f"{wanted['name']}: updated")
    return {**current, **desired}


REQUIRED = ("url", "token", "credential_id", "systems")


def main():
    cfg = json.load(sys.stdin)
    missing = [k for k in REQUIRED if not cfg.get(k) and cfg.get(k) != 0]
    if missing:
        print("missing config: " + ", ".join(missing), file=sys.stderr)
        return 2
    credential_id = int(cfg["credential_id"])
    wanted_list = cfg["systems"]
    try:
        api = Api(cfg["url"], cfg["token"])
        existing = list_systems(api)
        existing_by_key = {(s.get("hostname"), int(s.get("port") or 22)): s for s in existing}
        seen_keys = set()
        for wanted in wanted_list:
            ensure_system(api, existing_by_key, credential_id, wanted)
            seen_keys.add((wanted["hostname"], int(wanted.get("port") or 22)))

        orphaned = [
            s for s in existing
            if s.get("credentialId") == credential_id and (s.get("hostname"), int(s.get("port") or 22)) not in seen_keys
        ]
        if orphaned:
            print("not in this run's target list (left in the dashboard, not removed):")
            for s in orphaned:
                print(f"  {s.get('name')} ({s.get('hostname')}:{s.get('port')})")
    except (ApiError, urllib.error.URLError) as exc:
        print(f"skpatch-hosts systems sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
