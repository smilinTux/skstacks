#!/usr/bin/env python3
"""Create or update a provider-less Authentik portal tile, idempotently.

Some roles (skpulse/Uptime Kuma, sksec-web/CrowdSec Web UI, skpatch/linux
update dashboard, and others) sit behind the cluster's domain-wide Traefik
ForwardAuth catch-all (authentik@file on the router): Authentik already
gates the host, so no OAuth2/Proxy *provider* is needed for auth. What was
missing is the dashboard *tile* itself: Authentik only shows a service on
a user's portal ("My applications") if a `core.Application` object exists
for it, bound to a group. Without one the service works (forward-auth
still 302s an anonymous visitor to SSO) but never appears in the portal,
which is exactly the class of bug this script exists to prevent.

This intentionally creates NO provider: `provider` stays null on the
Application, same as the fleet's existing hand-made bookmark tiles
(skmon, skfenceha, skport's visibility tile, alertmanager, prometheus,
etc.) When a role's own HTTP app needs its own login flow, use the
heavier pattern in optional/skrac/src/skrac/authentik_provision.py or
optional/skmesh/src/skmesh/authentik_provision.py instead (provider +
application + redirect URIs); this script is strictly for the "already
behind forward auth, just needs a bookmark" case.

Input: one JSON object on stdin (secrets included, so nothing is on a
command line or in a process environment):
  url, host, token      - reach Authentik's API (see tasks/authentik_portal_tile.yml)
  slug, name             - the Application's slug and display name
  launch_url             - where the tile links to
  group                  - group name the tile is visible to (e.g. "authentik Admins")
  icon                   - optional meta_icon ("fa://fa-..." or "application-icons/...")
  open_in_new_tab         - optional bool, default true
  description             - optional meta_description
  category                - optional UI category (the Application's own "group" field)
Output: one status line per object, never a secret. Exit non-zero on any
API error (including: the named group does not exist - this script never
creates a group, only looks one up, so a typo fails loudly instead of
silently leaving the tile with no visibility).
"""
import json
import sys
import urllib.error
import urllib.request


class ApiError(Exception):
    pass


class Api:
    def __init__(self, url, host, token):
        self.base = url.rstrip("/") + "/api/v3"
        self.host = host
        self.token = token

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/json")
        if self.host:
            req.add_header("Host", self.host)
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

    def get(self, path, **params):
        import urllib.parse

        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self.call("GET", path + q)

    def first(self, path, **params):
        res = self.get(path, **params).get("results", [])
        return res[0] if res else None


def same(have, want):
    if isinstance(want, list) and isinstance(have, list):
        norm = lambda xs: sorted(json.dumps(x, sort_keys=True) for x in xs)  # noqa: E731
        return norm(have) == norm(want)
    return have == want


def ensure_application(api, cfg):
    def lookup():
        try:
            return api.call("GET", f"/core/applications/{cfg['slug']}/")
        except ApiError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    desired = {
        "name": cfg["name"],
        "slug": cfg["slug"],
        "provider": None,
        "launch_url": cfg["launch_url"],
        "meta_launch_url": cfg["launch_url"],
        "open_in_new_tab": bool(cfg.get("open_in_new_tab", True)),
        "meta_icon": cfg.get("icon", "") or "",
        "meta_description": cfg.get("description", "") or "",
        "policy_engine_mode": "any",
        "group": cfg.get("category", "") or "",
    }
    current = lookup()
    if current is None:
        obj = api.call("POST", "/core/applications/", desired)
        print("application: created")
        return obj
    changed = {k: v for k, v in desired.items() if not same(current.get(k), v)}
    if not changed:
        print("application: unchanged")
        return current
    api.call("PATCH", f"/core/applications/{current['slug']}/", changed)
    print(f"application: updated ({', '.join(sorted(changed))})")
    return {**current, **changed}


def ensure_group_binding(api, app, group_name):
    group = api.first("/core/groups/", name=group_name)
    if group is None:
        raise ApiError(f"group '{group_name}' not found in Authentik (this script never creates groups)")
    existing = api.get("/policies/bindings/", target=app["pk"], group=group["pk"]).get("results", [])
    if existing:
        print("binding: unchanged")
        return
    api.call(
        "POST",
        "/policies/bindings/",
        {
            "target": app["pk"],
            "group": group["pk"],
            "policy": None,
            "user": None,
            "negate": False,
            "enabled": True,
            "order": 0,
            "timeout": 30,
            "failure_result": False,
        },
    )
    print("binding: created")


REQUIRED = ("url", "token", "slug", "name", "launch_url", "group")


def main():
    cfg = json.load(sys.stdin)
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        print("missing config: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        api = Api(cfg["url"], cfg.get("host"), cfg["token"])
        app = ensure_application(api, cfg)
        ensure_group_binding(api, app, cfg["group"])
    except (ApiError, urllib.error.URLError) as exc:
        print(f"authentik portal tile provisioning failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
