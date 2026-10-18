#!/usr/bin/env python3
"""Disable Nextcloud accounts whose Authentik person is gone, inactive or no longer in skhub-users.

Authentik is the source of truth for people. Nextcloud accounts are matched to Authentik users
through the user attribute `nextcloud_uid`. Accounts with NO Authentik counterpart (the local
break-glass admin, service accounts such as the automation user) are never touched.

Dry run by default. Pass --apply to disable. Re-enabling is deliberate and manual
(`occ user:enable <uid>`), so a mistaken deactivation cannot flap.

Two Nextcloud backends, chosen by environment:
  OCC                          command that runs occ, e.g. "docker exec -u www-data <container> php occ"
  NC_URL, NC_USER, NC_APP_PASSWORD   Nextcloud OCS provisioning API (needs an admin account; use an app
                               password of a dedicated service user, never the human admin)
Authentik:
  AUTHENTIK_URL    e.g. https://sso.example.com
  AUTHENTIK_TOKEN  API token that may read users (role with only authentik_core.view_user; never on argv)
Test without a network: --authentik-json users.json (the /core/users/ "results" list).

As a Windmill script: the same file works; `main(apply)` reads the f/ops/* variables named in its docstring.
"""
import argparse, base64, json, os, shlex, subprocess, sys, urllib.request

GROUP = "skhub-users"


def authentik_users(path=None, url=None, token=None):
    if path:
        return json.load(open(path))
    url = (url or os.environ["AUTHENTIK_URL"]).rstrip("/") + "/api/v3/core/users/?page_size=500&include_groups=true"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + (token or os.environ["AUTHENTIK_TOKEN"])})
    return json.load(urllib.request.urlopen(req, timeout=30))["results"]


class Occ:
    def __init__(self, cmd):
        self.cmd = shlex.split(cmd)

    def _run(self, *args):
        return subprocess.check_output(self.cmd + list(args), text=True)

    def users(self):
        return list(json.loads(self._run("user:list", "--output=json")))

    def enabled(self, uid):
        return bool(json.loads(self._run("user:info", uid, "--output=json")).get("enabled"))

    def disable(self, uid):
        self._run("user:disable", uid)


class Ocs:
    def __init__(self, url, user, password):
        self.base = url.rstrip("/") + "/ocs/v2.php/cloud/users"
        self.auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()

    def _call(self, method, path="", query=""):
        req = urllib.request.Request(
            f"{self.base}{path}?format=json{query}", method=method,
            headers={"Authorization": self.auth, "OCS-APIRequest": "true"})
        d = json.load(urllib.request.urlopen(req, timeout=30))["ocs"]
        if d["meta"]["statuscode"] != 200:
            raise RuntimeError(f"OCS {method} {path}: {d['meta']}")
        return d["data"]

    def users(self):
        out, offset = [], 0
        while True:
            page = self._call("GET", "", f"&limit=200&offset={offset}")["users"]
            out += page
            if len(page) < 200:
                return out
            offset += 200

    def enabled(self, uid):
        return bool(self._call("GET", "/" + uid).get("enabled"))

    def disable(self, uid):
        self._call("PUT", f"/{uid}/disable")


def reconcile(people_rows, nc, apply, skip=()):
    people = {}
    for u in people_rows:
        uid = (u.get("attributes") or {}).get("nextcloud_uid")
        if uid:
            groups = {g["name"] if isinstance(g, dict) else g for g in u.get("groups_obj") or u.get("groups") or []}
            people[uid] = bool(u.get("is_active")) and GROUP in groups
    done = []
    for uid in sorted(nc.users()):
        if uid not in people or uid in skip:
            continue  # no Authentik counterpart: local-only account, leave alone
        if people[uid] or not nc.enabled(uid):
            continue
        print(("DISABLE " if apply else "would disable ") + uid)
        if apply:
            nc.disable(uid)
        done.append(uid)
    return {"linked": len(people), "apply": apply, "disabled" if apply else "would_disable": done}


def _wm_var(path):
    """Read a Windmill variable with the job token (no wmill package needed on the worker)."""
    base = os.environ.get("BASE_INTERNAL_URL", "http://localhost:8000").rstrip("/")
    req = urllib.request.Request(
        f"{base}/api/w/{os.environ['WM_WORKSPACE']}/variables/get_value/{path}",
        headers={"Authorization": "Bearer " + os.environ["WM_TOKEN"]})
    return json.load(urllib.request.urlopen(req, timeout=30))


def main(apply: bool = False):
    """Windmill entry point. Variables: f/ops/authentik_url (plain), f/ops/authentik_deprovision_token,
    f/ops/nextcloud_url (plain), f/ops/nextcloud_service_user (plain), f/ops/svc_provisioning_app_password."""
    v = _wm_var
    nc = Ocs(v("f/ops/nextcloud_url"), v("f/ops/nextcloud_service_user"), v("f/ops/svc_provisioning_app_password"))
    rows = authentik_users(url=v("f/ops/authentik_url"), token=v("f/ops/authentik_deprovision_token"))
    return reconcile(rows, nc, apply, skip={v("f/ops/nextcloud_service_user")})


def cli():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--authentik-json")
    a = ap.parse_args()
    if os.environ.get("OCC"):
        nc = Occ(os.environ["OCC"])
    else:
        nc = Ocs(os.environ["NC_URL"], os.environ["NC_USER"], os.environ["NC_APP_PASSWORD"])
    print(json.dumps(reconcile(authentik_users(a.authentik_json), nc, a.apply)))


if __name__ == "__main__":
    sys.exit(cli())
