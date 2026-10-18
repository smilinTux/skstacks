#!/usr/bin/env python3
"""Disable Nextcloud accounts whose Authentik person is gone, inactive or no longer in skhub-users.

Authentik is the source of truth for people. Nextcloud accounts are matched to Authentik users
through the user attribute `nextcloud_uid`. Accounts with NO Authentik counterpart (the local
break-glass admin, service accounts such as the automation user) are never touched.

Dry run by default. Pass --apply to run `occ user:disable`. Re-enabling is deliberate and manual
(`occ user:enable <uid>`), so a mistaken deactivation cannot flap.

Environment:
  AUTHENTIK_URL    e.g. https://sso.example.com
  AUTHENTIK_TOKEN  API token that may read users (never put it on argv)
  OCC              command that runs occ, e.g. "docker exec -u www-data <container> php occ"
Test without a network: --authentik-json users.json (the /core/users/ "results" list).
"""
import argparse, json, os, shlex, subprocess, sys, urllib.request

GROUP = "skhub-users"


def authentik_users(path):
    if path:
        return json.load(open(path))
    url = os.environ["AUTHENTIK_URL"].rstrip("/") + "/api/v3/core/users/?page_size=500&include_groups=true"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + os.environ["AUTHENTIK_TOKEN"]})
    return json.load(urllib.request.urlopen(req))["results"]


def occ(*args):
    return subprocess.check_output(shlex.split(os.environ["OCC"]) + list(args), text=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--authentik-json")
    a = ap.parse_args()

    people = {}
    for u in authentik_users(a.authentik_json):
        uid = (u.get("attributes") or {}).get("nextcloud_uid")
        if uid:
            groups = {g["name"] if isinstance(g, dict) else g for g in u.get("groups_obj") or u.get("groups") or []}
            people[uid] = bool(u.get("is_active")) and GROUP in groups

    nc = json.loads(occ("user:list", "--output=json"))
    for uid in sorted(nc):
        if uid not in people:
            continue  # no Authentik counterpart: local-only account, leave alone
        info = json.loads(occ("user:info", uid, "--output=json"))
        if people[uid] or not info.get("enabled"):
            continue
        print(("DISABLE " if a.apply else "would disable ") + uid)
        if a.apply:
            occ("user:disable", uid)


if __name__ == "__main__":
    sys.exit(main())
