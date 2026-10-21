#!/usr/bin/env python3
"""Configure Windmill's Authentik login (the `oauths` global setting) and the
workspace admins, idempotently.

Input: one JSON object on stdin (the superadmin secret and the OIDC client
secret are in it, so neither is on a command line or in the environment):

  {"url": "http://skflow-<env>_server:8000",
   "superadmin_secret": "...",
   "oauths": {"authentik": {...}},          # the full desired value
   "workspace_admins": {"<workspace>": ["email", ...]}}

Output: one status line per object, never a secret. Exit 1 on an API error or
a missing workspace, 2 on bad input.

Steps (all against the Windmill API, as the virtual superadmin that
SUPERADMIN_SECRET gives):
1. GET /api/settings/global/oauths; POST {"value": ...} only when it differs.
2. For each workspace: GET /api/w/<ws>/users/list; add every missing email with
   POST /api/w/<ws>/workspaces/add_user {email, is_admin: true, operator: false};
   promote a present non-admin with POST /api/w/<ws>/users/update/<username>.

Community edition allows 10 SSO users in total (Windmill's own limit); the
10th and later SSO logins are refused by Windmill, not by this script.
Standard library only (python:3-alpine).
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    pass


def call(base, token, method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except urllib.error.HTTPError as e:
        # The body of an error can echo the request; print the status only.
        raise ApiError(f"{method} {path}: HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise ApiError(f"{method} {path}: {e.reason}") from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw


def ensure_oauths(base, token, desired):
    current = call(base, token, "GET", "/api/settings/global/oauths")
    if current == desired:
        print("oauths: unchanged")
        return
    call(base, token, "POST", "/api/settings/global/oauths", {"value": desired})
    print("oauths: updated" if current else "oauths: created")


def ensure_admins(base, token, workspace, emails):
    ws = urllib.parse.quote(workspace, safe="")
    try:
        users = call(base, token, "GET", f"/api/w/{ws}/users/list")
    except ApiError as e:
        raise ApiError(f"workspace {workspace} is not readable (does it exist?): {e}") from None
    by_email = {u.get("email", "").lower(): u for u in users or []}
    for email in emails:
        user = by_email.get(email.lower())
        if user is None:
            call(base, token, "POST", f"/api/w/{ws}/workspaces/add_user",
                 {"email": email, "is_admin": True, "operator": False})
            print(f"workspace {workspace}: {email} added as admin")
        elif not user.get("is_admin"):
            name = urllib.parse.quote(user["username"], safe="")
            call(base, token, "POST", f"/api/w/{ws}/users/update/{name}",
                 {"is_admin": True, "operator": False})
            print(f"workspace {workspace}: {email} promoted to admin")
        else:
            print(f"workspace {workspace}: {email} unchanged")


def main():
    try:
        cfg = json.load(sys.stdin)
        base, token = cfg["url"], cfg["superadmin_secret"]
        oauths = cfg["oauths"]
        admins = cfg.get("workspace_admins") or {}
        if not (base and token and isinstance(oauths, dict) and isinstance(admins, dict)):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        print("bad input: url, superadmin_secret and oauths are required", file=sys.stderr)
        return 2
    try:
        ensure_oauths(base, token, oauths)
        for workspace, emails in admins.items():
            ensure_admins(base, token, workspace, emails)
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
