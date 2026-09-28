#!/usr/bin/env python3
"""Create or update skmesh's side of Authentik, idempotently.

What NetBird needs from Authentik (AUTH_MODE: authentik):

* an OAuth2/OpenID provider with a PUBLIC client (the dashboard and the
  CLI use authorization code + PKCE), `sub` = the Authentik user's numeric
  id (NetBird's Authentik IdP manager looks users up by that pk), the
  openid/email/profile/offline_access scopes, and the goauthentik.io/api
  scope (the IdP manager's client_credentials token must be allowed to call
  the Authentik API);
* an application (slug = the issuer path /application/o/<slug>/);
* a service account with an app-password token, allowed to view users, for
  NetBird's IdP manager (it lists users and resolves ids to emails);
* optionally the default brand's device code flow, for `netbird up` on a
  machine with no browser.

Input: one JSON object on stdin (secrets included, so nothing is on a
command line or in a process environment). Output: one status line per
object, never a secret. Exit non-zero on any API error.

Runs with the Python standard library only (python:3-alpine), from a
throwaway container on sksso's overlay network, against sksso's server
directly; `host` is sent as the Host header because Authentik answers 404
for a Host it does not know (e.g. a swarm service name with an underscore).
Handles the API differences between Authentik 2024.4 (newline-separated
redirect_uris, /propertymappings/scope/, no invalidation flow) and 2024.8+.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

MANAGED_SCOPES = {
    "openid": "goauthentik.io/providers/oauth2/scope-openid",
    "email": "goauthentik.io/providers/oauth2/scope-email",
    "profile": "goauthentik.io/providers/oauth2/scope-profile",
    "offline_access": "goauthentik.io/providers/oauth2/scope-offline_access",
    "goauthentik.io/api": "goauthentik.io/providers/oauth2/scope-authentik_api",
}
IDP_MANAGER_PERMISSIONS = ["authentik_core.view_user"]


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
            # Authentik error bodies name fields, never echo the token.
            raise ApiError(f"{method} {path}: HTTP {status}: {raw[:400].decode(errors='replace')}")
        return json.loads(raw) if raw else None

    def get(self, path, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self.call("GET", path + q)

    def first(self, path, **params):
        res = self.get(path, **params).get("results", [])
        return res[0] if res else None


def version_tuple(v):
    parts = []
    for p in str(v).split(".")[:2]:
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def ensure(api, path, lookup, desired, label, key="pk"):
    """Create `desired` at `path` if `lookup` finds nothing, else PATCH the
    fields that differ. Returns the object."""
    current = lookup()
    if current is None:
        obj = api.call("POST", path, desired)
        print(f"{label}: created")
        return obj
    changed = {k: v for k, v in desired.items() if not same(current.get(k), v)}
    if not changed:
        print(f"{label}: unchanged")
        return current
    obj = api.call("PATCH", f"{path}{current[key]}/", changed)
    print(f"{label}: updated ({', '.join(sorted(changed))})")
    return obj


def same(have, want):
    if isinstance(want, list) and isinstance(have, list):
        norm = lambda xs: sorted(json.dumps(x, sort_keys=True) for x in xs)  # noqa: E731
        return norm(have) == norm(want)
    if isinstance(want, str) and isinstance(have, str) and "\n" in want:
        return sorted(have.split()) == sorted(want.split())
    return have == want


def flow_pk(api, slug, required=True):
    flow = api.first("/flows/instances/", slug=slug)
    if flow is None and required:
        raise ApiError(f"flow '{slug}' not found in Authentik")
    return flow["pk"] if flow else None


def scope_mappings(api, modern):
    path = "/propertymappings/provider/scope/" if modern else "/propertymappings/scope/"
    pks = []
    for scope, managed in MANAGED_SCOPES.items():
        try:
            m = api.first(path, managed=managed)
        except ApiError as exc:
            # older Authentik rejects an unknown `managed` value with a 400
            if "HTTP 400" not in str(exc):
                raise
            m = None
        if m is None:
            m = api.first(path, scope_name=scope)
        if m is None and scope == "goauthentik.io/api":
            # Authentik before 2024.8 ships no managed API-access mapping;
            # the scope only has to exist for a token to carry it.
            m = api.call("POST", path, {"name": "skmesh: authentik API access",
                                        "scope_name": scope, "expression": "return {}",
                                        "description": "authentik API access (NetBird IdP manager)"})
            print("scope mapping goauthentik.io/api: created")
        if m is None:
            raise ApiError(f"scope mapping for '{scope}' ({managed}) not found")
        pks.append(m["pk"])
    return pks


def signing_key(api, name):
    kp = api.first("/crypto/certificatekeypairs/", name=name, has_key="true")
    if kp is None:
        kp = api.first("/crypto/certificatekeypairs/", has_key="true")
    if kp is None:
        raise ApiError("no certificate keypair with a private key to sign tokens")
    return kp["pk"]


def provision(api, cfg):
    version = api.get("/admin/version/")["version_current"]
    modern = version_tuple(version) >= (2024, 8)
    print(f"authentik: {version}")

    provider = {
        "name": cfg["provider_name"],
        "authorization_flow": flow_pk(api, cfg["authorization_flow"]),
        "client_type": "public",
        "client_id": cfg["client_id"],
        "sub_mode": "user_id",
        "include_claims_in_id_token": True,
        "issuer_mode": "per_provider",
        "signing_key": signing_key(api, cfg["signing_key"]),
        "property_mappings": scope_mappings(api, modern),
    }
    if modern:
        provider["redirect_uris"] = [{"matching_mode": "strict", "url": u} for u in cfg["redirect_uris"]]
    else:
        provider["redirect_uris"] = "\n".join(cfg["redirect_uris"])
    inval = flow_pk(api, cfg["invalidation_flow"], required=False)
    if inval:
        provider["invalidation_flow"] = inval
    prov = ensure(api, "/providers/oauth2/",
                  lambda: api.first("/providers/oauth2/", name=cfg["provider_name"]),
                  provider, "provider")

    def app_lookup():
        try:
            return api.call("GET", f"/core/applications/{cfg['app_slug']}/")
        except ApiError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
    ensure(api, "/core/applications/", app_lookup, {
        "name": cfg["app_name"],
        "slug": cfg["app_slug"],
        "provider": prov["pk"],
        "meta_launch_url": cfg["launch_url"],
    }, "application", key="slug")

    # IdP manager service account: view users, nothing else.
    role = ensure(api, "/rbac/roles/",
                  lambda: api.first("/rbac/roles/", name=cfg["sa_role"]),
                  {"name": cfg["sa_role"]}, "role", key="pk")
    api.call("POST", f"/rbac/permissions/assigned_by_roles/{role['pk']}/assign/",
             {"permissions": IDP_MANAGER_PERMISSIONS})
    print(f"role permissions: {', '.join(IDP_MANAGER_PERMISSIONS)}")
    group = ensure(api, "/core/groups/",
                   lambda: api.first("/core/groups/", name=cfg["sa_group"]),
                   {"name": cfg["sa_group"], "is_superuser": False, "roles": [role["pk"]]},
                   "group", key="pk")
    user = ensure(api, "/core/users/",
                  lambda: api.first("/core/users/", username=cfg["sa_username"]),
                  {"username": cfg["sa_username"], "name": "skmesh IdP manager",
                   "type": "service_account", "is_active": True,
                   "path": "goauthentik.io/service-accounts"},
                  "service account", key="pk")
    api.call("POST", f"/core/groups/{group['pk']}/add_user/", {"pk": user["pk"]})
    ensure(api, "/core/tokens/",
           lambda: api.first("/core/tokens/", identifier=cfg["sa_token_identifier"]),
           {"identifier": cfg["sa_token_identifier"], "user": user["pk"],
            "intent": "app_password", "expiring": False,
            "description": "NetBird IdP manager (skmesh)"},
           "service account token", key="identifier")
    api.call("POST", f"/core/tokens/{cfg['sa_token_identifier']}/set_key/", {"key": cfg["sa_password"]})
    print("service account token key: set")

    if cfg.get("device_flow"):
        brand = api.first("/core/brands/", default="true")
        if brand is None:
            raise ApiError("no default brand to attach the device code flow to")
        if brand.get("flow_device_code"):
            print("brand device code flow: already set, left as is")
        else:
            api.call("PATCH", f"/core/brands/{brand['brand_uuid']}/",
                     {"flow_device_code": flow_pk(api, cfg["device_code_flow"])})
            print("brand device code flow: set")


REQUIRED = ("url", "token", "client_id", "provider_name", "app_name", "app_slug",
            "launch_url", "redirect_uris", "authorization_flow", "invalidation_flow",
            "signing_key", "sa_username", "sa_password", "sa_group", "sa_role",
            "sa_token_identifier")


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
