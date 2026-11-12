#!/usr/bin/env python3
"""Create or update skmon's side of Authentik, idempotently.

Objects (spec 10, SSO standard names: group `skadmin`, mode `admin-only`):
  - one OAuth2/OIDC provider plus application for Grafana (generic OAuth);
  - one proxy (forward-auth, single application) provider plus application
    for each other web UI (vmui, Alertmanager, Pyroscope when enabled);
  - each application bound to `skadmin` only, by a policy binding on the
    application (order 0, never `authentik Admins`);
  - every proxy provider attached to Authentik's embedded outpost (only
    what is missing is added, other providers are never removed).

Input: one JSON object on stdin (secrets included, so nothing is on a command
line or in a process environment):
  {"host", "url"?, "token", "group": "skadmin", "apps": [{"slug", "name",
   "kind": "oidc"|"proxy", "launch_url", "redirect_uris"?, "client_id"?,
   "client_secret"?, "external_host"?}]}
Output: one status line per object, never a secret. Exit non-zero on any API
error.

Runs with the Python standard library only, from a throwaway container on
sksso's overlay network, against sksso's server directly; `host` is sent as the
Host header because Authentik answers 404 for a Host it does not know.
"""
import json
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = "http://server:9000"
DEFAULT_GROUP = "skadmin"
DEFAULT_AUTHORIZATION_FLOW = "default-provider-authorization-implicit-consent"
DEFAULT_INVALIDATION_FLOW = "default-provider-invalidation-flow"
DEFAULT_SIGNING_KEY = "authentik Self-signed Certificate"
EMBEDDED_OUTPOST = "goauthentik.io/outposts/embedded"
MANAGED_SCOPES = {
    "openid": "goauthentik.io/providers/oauth2/scope-openid",
    "email": "goauthentik.io/providers/oauth2/scope-email",
    "profile": "goauthentik.io/providers/oauth2/scope-profile",
}
SECRET_FIELDS = ("client_secret",)


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
            # Authentik error bodies name fields, they do not echo the token
            # or the client secret we may have just sent.
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


def same(have, want):
    if isinstance(want, list) and isinstance(have, list):
        # Compare list-of-object entries on the keys we set only: newer
        # Authentik adds keys (redirect_uri_type) to what it returns.
        keys = set().union(*(x.keys() for x in want if isinstance(x, dict)))
        trim = lambda x: {k: x.get(k) for k in keys} if isinstance(x, dict) and keys else x  # noqa: E731
        norm = lambda xs: sorted(json.dumps(trim(x), sort_keys=True) for x in xs)  # noqa: E731
        return norm(have) == norm(want)
    if isinstance(want, str) and isinstance(have, str) and "\n" in want:
        return sorted(have.split()) == sorted(want.split())
    return have == want


def ensure(api, path, lookup, desired, label, key="pk"):
    """Create `desired` at `path` when `lookup` finds nothing, else PATCH the
    fields that differ. A secret field the API does not echo back cannot be
    compared, so it is left alone on an existing object. Returns (object, verb)."""
    current = lookup()
    if current is None:
        return api.call("POST", path, desired), "created"
    changed = {k: v for k, v in desired.items()
               if not (k in SECRET_FIELDS and k not in current) and not same(current.get(k), v)}
    if not changed:
        return current, "unchanged"
    obj = api.call("PATCH", f"{path}{current[key]}/", changed)
    shown = sorted(k if k not in SECRET_FIELDS else f"{k} (secret)" for k in changed)
    return obj, f"updated ({', '.join(shown)})"


def _not_found(exc):
    return "HTTP 404" in str(exc)


def get_application(api, slug):
    try:
        return api.call("GET", f"/core/applications/{slug}/")
    except ApiError as exc:
        if _not_found(exc):
            return None
        raise


def flow_pk(api, slug):
    flow = api.first("/flows/instances/", slug=slug)
    if flow is None:
        raise ApiError(f"flow '{slug}' not found in Authentik")
    return flow["pk"]


def scope_mappings(api, modern):
    path = "/propertymappings/provider/scope/" if modern else "/propertymappings/scope/"
    pks = []
    for scope, managed in MANAGED_SCOPES.items():
        m = api.first(path, managed=managed) or api.first(path, scope_name=scope)
        if m is None:
            raise ApiError(f"scope mapping for '{scope}' ({managed}) not found")
        pks.append(m["pk"])
    return pks


def signing_key(api, name):
    kp = api.first("/crypto/certificatekeypairs/", name=name, has_key="true") \
        or api.first("/crypto/certificatekeypairs/", has_key="true")
    if kp is None:
        raise ApiError("no certificate keypair with a private key to sign tokens")
    return kp["pk"]


def ensure_application(api, app, provider_pk):
    fields = {
        "name": app["name"],
        "slug": app["slug"],
        "provider": provider_pk,
        "meta_launch_url": app["launch_url"],
        "policy_engine_mode": "any",
    }
    if app.get("icon"):
        fields["meta_icon"] = app["icon"]
    _, verb = ensure(api, "/core/applications/", lambda: get_application(api, app["slug"]),
                     fields, app["slug"], key="slug")
    return verb


def ensure_oidc(api, app):
    """Grafana's OAuth2 provider and application. Returns one status line."""
    version = version_tuple(api.get("/admin/version/")["version_current"])
    modern = version >= (2024, 8)
    provider = {
        "name": app["slug"],
        "authorization_flow": flow_pk(api, app.get("authorization_flow", DEFAULT_AUTHORIZATION_FLOW)),
        "invalidation_flow": flow_pk(api, app.get("invalidation_flow", DEFAULT_INVALIDATION_FLOW)),
        "client_type": "confidential",
        "client_id": app["client_id"],
        "client_secret": app["client_secret"],
        "sub_mode": "user_id",
        "include_claims_in_id_token": True,
        "issuer_mode": "per_provider",
        "signing_key": signing_key(api, app.get("signing_key", DEFAULT_SIGNING_KEY)),
        "property_mappings": scope_mappings(api, modern),
    }
    if version >= (2026, 2):
        # 2026.2+ gives a provider created without grant_types an empty list,
        # and every authorize then fails with invalid_request.
        provider["grant_types"] = ["authorization_code", "refresh_token"]
    if modern:
        provider["redirect_uris"] = [{"matching_mode": "strict", "url": u} for u in app["redirect_uris"]]
    else:
        provider["redirect_uris"] = "\n".join(app["redirect_uris"])
    prov, pverb = ensure(api, "/providers/oauth2/",
                         lambda: api.first("/providers/oauth2/", name=app["slug"]), provider, app["slug"])
    averb = ensure_application(api, app, prov["pk"])
    return f"oidc {app['slug']}: provider {pverb}, application {averb}"


def ensure_proxy(api, app):
    """A forward-auth (single application) proxy provider and its application."""
    provider = {
        "name": app["slug"],
        "authorization_flow": flow_pk(api, app.get("authorization_flow", DEFAULT_AUTHORIZATION_FLOW)),
        "invalidation_flow": flow_pk(api, app.get("invalidation_flow", DEFAULT_INVALIDATION_FLOW)),
        "mode": "forward_single",
        "external_host": app["external_host"],
    }
    prov, pverb = ensure(api, "/providers/proxy/",
                         lambda: api.first("/providers/proxy/", name=app["slug"]), provider, app["slug"])
    averb = ensure_application(api, app, prov["pk"])
    return f"proxy {app['slug']}: provider {pverb}, application {averb}"


def ensure_group_binding(api, app_slug, group):
    """Bind the application to `group` by a policy binding (order 0). Existing
    bindings are additive, so none is removed. The group must already exist."""
    app = get_application(api, app_slug)
    if app is None:
        raise ApiError(f"application '{app_slug}' not found")
    grp = api.first("/core/groups/", name=group)
    if grp is None:
        raise ApiError(f"group '{group}' not found in Authentik (it is created by sksso, not here)")
    bindings = api.get("/policies/bindings/", target=app["pk"]).get("results", [])
    if any(b.get("group") == grp["pk"] for b in bindings):
        return f"binding {app_slug} -> {group}: unchanged"
    api.call("POST", "/policies/bindings/", {
        "target": app["pk"], "group": grp["pk"], "order": 0, "enabled": True,
        "negate": False, "timeout": 30,
    })
    return f"binding {app_slug} -> {group}: created"


def ensure_outpost(api, slugs):
    """Attach the proxy providers named `slugs` to the embedded outpost. Adds
    only what is missing; every other provider stays attached."""
    if not slugs:
        return "outpost: no proxy providers"
    outpost = api.first("/outposts/instances/", managed=EMBEDDED_OUTPOST)
    if outpost is None:
        raise ApiError("the embedded outpost was not found in Authentik")
    wanted = []
    for slug in slugs:
        prov = api.first("/providers/proxy/", name=slug)
        if prov is None:
            raise ApiError(f"proxy provider '{slug}' not found")
        wanted.append(prov["pk"])
    have = list(outpost.get("providers", []))
    missing = [pk for pk in wanted if pk not in have]
    if not missing:
        return "outpost: unchanged"
    api.call("PATCH", f"/outposts/instances/{outpost['pk']}/", {"providers": have + missing})
    return f"outpost: attached {len(missing)} provider(s)"


def provision(api, cfg):
    group = cfg.get("group") or DEFAULT_GROUP
    print(f"authentik: {api.get('/admin/version/')['version_current']}")
    proxies = []
    for app in cfg["apps"]:
        print(ensure_oidc(api, app) if app["kind"] == "oidc" else ensure_proxy(api, app))
        print(ensure_group_binding(api, app["slug"], group))
        if app["kind"] == "proxy":
            proxies.append(app["slug"])
    print(ensure_outpost(api, proxies))


def validate(cfg):
    """Names of missing fields (never values)."""
    missing = [k for k in ("token", "apps") if not cfg.get(k)]
    need = {"oidc": ("launch_url", "redirect_uris", "client_id", "client_secret"),
            "proxy": ("launch_url", "external_host")}
    for i, app in enumerate(cfg.get("apps") or []):
        if app.get("kind") not in need:
            missing.append(f"apps[{i}].kind")
            continue
        missing += [f"apps[{i}].{k}" for k in ("slug", "name", *need[app["kind"]]) if not app.get(k)]
    return missing


def main():
    cfg = json.load(sys.stdin)
    missing = validate(cfg)
    if missing:
        print("missing config: " + ", ".join(missing), file=sys.stderr)
        return 2
    try:
        provision(Api(cfg.get("url") or DEFAULT_URL, cfg.get("host"), cfg["token"]), cfg)
    except (ApiError, urllib.error.URLError) as exc:
        print(f"authentik provisioning failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
