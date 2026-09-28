#!/usr/bin/env python3
"""Create or update sksso's CapAuth login in Authentik, idempotently.

sksso.CAPAUTH_ENABLED runs a CapAuth OIDC identity provider next to
Authentik (the `capauth` service of this stack). This script registers it
in Authentik and builds a login flow that uses it:

* an OAuth/OIDC **source** (provider type openidconnect, PKCE S256, client
  secret sent with HTTP basic auth). The browser goes to CapAuth's public
  authorize URL; Authentik calls the token and userinfo endpoints on the
  stack's overlay network. Users are matched by the OIDC `sub` (the PGP
  fingerprint) only, and the source has NO enrollment flow: a fingerprint
  that is not linked to an Authentik user is refused, so nobody gets an
  Authentik account by holding a CapAuth key;
* an identification stage that offers only that source (no username or
  password field), a user login stage, and an authentication **flow** with
  both. The flow is bound to nothing here: an application opts in by using
  it as its provider's authentication flow (skmesh:
  skmesh.AUTHENTIK_AUTHENTICATION_FLOW). Every other application keeps the
  brand's default (password) login;
* one **source connection** per `users` entry, linking a PGP fingerprint to
  an existing Authentik user (the user must exist; it is never created).

Input: one JSON object on stdin (the API token and the client secret are in
it, so neither is on a command line or in a process environment). Output:
one status line per object, never a secret. Exit 1 on any API error, 2 on
missing input.

Standard library only (python:3-alpine), run from a throwaway container on
sksso's overlay network against the server directly; `host` is sent as the
Host header because Authentik answers 404 for a Host it does not know.
Needs Authentik 2025.2+ (OAuth sources have no PKCE before that, and
CapAuth refuses a code flow without PKCE S256).
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

MIN_VERSION = (2025, 2)


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
            # Authentik error bodies name fields, never echo a secret we sent.
            raise ApiError(f"{method} {path}: HTTP {status}: {raw[:400].decode(errors='replace')}")
        return json.loads(raw) if raw else None

    def get(self, path, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self.call("GET", path + q)

    def first(self, path, **params):
        res = self.get(path, **params).get("results", [])
        return res[0] if res else None

    def by_key(self, path, key):
        try:
            return self.call("GET", f"{path}{urllib.parse.quote(str(key))}/")
        except ApiError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise


def version_tuple(v):
    parts = []
    for p in str(v).split(".")[:2]:
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def same(have, want):
    if isinstance(want, list) and isinstance(have, list):
        return sorted(json.dumps(x, sort_keys=True) for x in have) == sorted(json.dumps(x, sort_keys=True) for x in want)
    return have == want


def ensure(api, path, lookup, desired, label, key="pk"):
    """Create `desired` if `lookup` finds nothing, else PATCH what differs."""
    current = lookup()
    if current is None:
        obj = api.call("POST", path, desired)
        print(f"{label}: created")
        return obj, True
    changed = {k: v for k, v in desired.items() if not same(current.get(k), v)}
    if not changed:
        print(f"{label}: unchanged")
        return current, False
    obj = api.call("PATCH", f"{path}{current[key]}/", changed)
    print(f"{label}: updated ({', '.join(sorted(changed))})")
    return obj, False


def flow_pk(api, slug):
    flow = api.first("/flows/instances/", slug=slug)
    if flow is None:
        raise ApiError(f"flow '{slug}' not found in Authentik")
    return flow["pk"]


def provision(api, cfg):
    version = api.get("/admin/version/")["version_current"]
    print(f"authentik: {version}")
    if version_tuple(version) < MIN_VERSION:
        raise ApiError(f"Authentik {version} has no PKCE on OAuth sources; the CapAuth source needs 2025.2+")

    source = {
        "name": cfg["source_name"],
        "slug": cfg["source_slug"],
        "enabled": True,
        "provider_type": "openidconnect",
        "consumer_key": cfg["client_id"],
        "authorization_url": cfg["authorization_url"],
        "access_token_url": cfg["token_url"],
        "profile_url": cfg["userinfo_url"],
        "pkce": "S256",
        "authorization_code_auth_method": "basic_auth",
        "additional_scopes": "",
        "user_matching_mode": "identifier",
        "enrollment_flow": None,
        "authentication_flow": flow_pk(api, cfg["source_authentication_flow"]),
    }

    # consumer_secret is write-only (never read back), so it cannot be
    # compared: it goes into every write, and is set on every run (like the
    # skmesh IdP manager key). Every PATCH carries the whole source because
    # the source serializer re-validates provider type and URLs on each write.
    with_secret = dict(source, consumer_secret=cfg["client_secret"])
    current = api.by_key("/sources/oauth/", cfg["source_slug"])
    if current is None:
        src = api.call("POST", "/sources/oauth/", with_secret)
        print("source: created")
    else:
        changed = sorted(k for k, v in source.items() if not same(current.get(k), v))
        src = api.call("PATCH", f"/sources/oauth/{urllib.parse.quote(cfg['source_slug'])}/", with_secret)
        print(f"source: updated ({', '.join(changed)})" if changed else "source: unchanged")
    print("source client secret: set")
    source_pk = src["pk"]

    ident, _ = ensure(api, "/stages/identification/",
                      lambda: api.first("/stages/identification/", name=cfg["identification_stage"]),
                      {"name": cfg["identification_stage"], "user_fields": [], "password_stage": None,
                       "sources": [source_pk], "show_source_labels": True},
                      "identification stage")
    login, _ = ensure(api, "/stages/user_login/",
                      lambda: api.first("/stages/user_login/", name=cfg["login_stage"]),
                      {"name": cfg["login_stage"]}, "user login stage")
    flow, _ = ensure(api, "/flows/instances/", lambda: api.by_key("/flows/instances/", cfg["flow_slug"]),
                     {"name": cfg["flow_name"], "slug": cfg["flow_slug"], "title": cfg["flow_title"],
                      "designation": "authentication"},
                     "flow", key="slug")
    for stage, order, label in ((ident, 10, "identification"), (login, 100, "user login")):
        ensure(api, "/flows/bindings/",
               lambda stage=stage: api.first("/flows/bindings/", target=flow["pk"], stage=stage["pk"]),
               {"target": flow["pk"], "stage": stage["pk"], "order": order},
               f"flow binding {label}")

    if not cfg.get("users"):
        return
    conns = api.get("/sources/user_connections/oauth/", source__slug=cfg["source_slug"], page_size=1000)
    by_ident = {c["identifier"]: c for c in conns.get("results", [])}
    for entry in cfg["users"]:
        username, fp = entry["username"], str(entry["fingerprint"]).replace(" ", "").upper()
        if len(fp) not in (40, 64) or any(ch not in "0123456789ABCDEF" for ch in fp):
            raise ApiError(f"users: '{username}' has no 40/64 hex digit PGP fingerprint")
        user = api.first("/core/users/", username=username)
        if user is None:
            raise ApiError(f"users: Authentik user '{username}' does not exist (create it first)")
        ensure(api, "/sources/user_connections/oauth/", lambda fp=fp: by_ident.get(fp),
               {"user": user["pk"], "source": source_pk, "identifier": fp},
               f"link {username} <- {fp[-16:]}")


REQUIRED = ("url", "token", "source_name", "source_slug", "client_id", "client_secret",
            "authorization_url", "token_url", "userinfo_url", "source_authentication_flow",
            "identification_stage", "login_stage", "flow_name", "flow_slug", "flow_title")


def main():
    cfg = json.load(sys.stdin)
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if missing:
        print("missing config: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        provision(Api(cfg["url"], cfg.get("host"), cfg["token"]), cfg)
    except (ApiError, urllib.error.URLError) as exc:
        print(f"authentik provisioning failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
