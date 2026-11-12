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
   "client_secret"?, "external_host"?}], "retire"?: ["slug", ...]}
`retire` names applications of retired UIs: each is deleted by slug (a DELETE needs no lookup;
204 and 404 both succeed).
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

    def pages(self, path, **params):
        """Every result of a list endpoint, following `pagination.next` (the next page number,
        0 on the last page)."""
        page, out = 1, []
        while page:
            body = self.get(path, **{**params, "page": page})
            out += body.get("results", [])
            page = (body.get("pagination") or {}).get("next") or 0
        return out


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


def find_one(api, kind, path, field, value, **params):
    """The one object of `path` whose `field` equals `value` exactly. The query filter only
    narrows the listing (Authentik silently ignores a filter its viewset does not declare), so
    the exact match is re-checked client-side across every page. None when there is no match;
    more than one is an error naming the kind and the pks."""
    hits = [r for r in api.pages(path, **params) if r.get(field) == value]
    if len(hits) > 1:
        raise ApiError(f"{kind} '{value}' matches {len(hits)} objects (pk {', '.join(str(h.get('pk')) for h in hits)})")
    return hits[0] if hits else None


def flow_pk(api, slug):
    flow = find_one(api, "flow", "/flows/instances/", "slug", slug, slug=slug)
    if flow is None:
        raise ApiError(f"flow '{slug}' not found in Authentik")
    return flow["pk"]


def scope_mappings(api, modern):
    path = "/propertymappings/provider/scope/" if modern else "/propertymappings/scope/"
    pks = []
    for scope, managed in MANAGED_SCOPES.items():
        m = find_one(api, "scope mapping", path, "managed", managed, managed=managed) \
            or find_one(api, "scope mapping", path, "scope_name", scope, scope_name=scope)
        if m is None:
            raise ApiError(f"scope mapping for '{scope}' ({managed}) not found")
        pks.append(m["pk"])
    return pks


def signing_key(api, name):
    kp = find_one(api, "certificate keypair", "/crypto/certificatekeypairs/", "name", name, name=name, has_key="true")
    if kp is None:
        kp = next(iter(api.pages("/crypto/certificatekeypairs/", has_key="true")), None)
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


def find_proxy(api, slug):
    # ProxyProviderViewSet declares only name__iexact; a plain `name` would be ignored.
    return find_one(api, "proxy provider", "/providers/proxy/", "name", slug, name__iexact=slug)


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
                         lambda: find_one(api, "oauth2 provider", "/providers/oauth2/", "name", app["slug"], name=app["slug"]),
                         provider, app["slug"])
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
                         lambda: find_proxy(api, app["slug"]), provider, app["slug"])
    averb = ensure_application(api, app, prov["pk"])
    return f"proxy {app['slug']}: provider {pverb}, application {averb}"


def ensure_group_binding(api, app_slug, group):
    """Make the `group` binding the ONLY policy binding of the application (admin-only):
    create it when missing and delete every other binding, reporting deletions by pk only.
    The group must already exist (sksso creates it, never this script)."""
    app = get_application(api, app_slug)
    if app is None:
        raise ApiError(f"application '{app_slug}' not found")
    grp = find_one(api, "group", "/core/groups/", "name", group, name=group)
    if grp is None:
        raise ApiError(f"group '{group}' not found in Authentik (it is created by sksso, not here)")
    bindings = [b for b in api.pages("/policies/bindings/", target=app["pk"]) if b.get("target") == app["pk"]]
    mine = [b for b in bindings if b.get("group") == grp["pk"]]
    # Prefer a healthy binding: a disabled one is skipped by Authentik's policy engine (leaving
    # the application open) and a negated one admits everyone except the group.
    keep = next((b for b in mine if b.get("enabled") is True and b.get("negate") is False), mine[0] if mine else None)
    lines = []
    if keep is not None and not (keep.get("enabled") is True and keep.get("negate") is False):
        api.call("PATCH", f"/policies/bindings/{keep['pk']}/", {"enabled": True, "negate": False})
        lines.append(f"binding {app_slug} -> {group}: updated (enabled, negate)")
    if keep is None:
        api.call("POST", "/policies/bindings/", {
            "target": app["pk"], "group": grp["pk"], "order": 0, "enabled": True,
            "negate": False, "timeout": 30,
        })
        lines.append(f"binding {app_slug} -> {group}: created")
    for b in bindings:
        if b is not keep:
            api.call("DELETE", f"/policies/bindings/{b['pk']}/")
            lines.append(f"binding {app_slug}: deleted extra binding {b['pk']}")
    return "\n".join(lines) or f"binding {app_slug} -> {group}: unchanged"


def embedded_outpost(api):
    # OutpostFilter declares managed__iexact; select client-side by the exact value as well.
    hits = [o for o in api.pages("/outposts/instances/", managed__iexact=EMBEDDED_OUTPOST)
            if o.get("managed") == EMBEDDED_OUTPOST]
    if not hits:
        raise ApiError("the embedded outpost was not found in Authentik")
    if len(hits) > 1:
        raise ApiError(f"{len(hits)} embedded outposts found (pk {', '.join(str(o.get('pk')) for o in hits)})")
    return hits[0]


def ensure_outpost(api, slugs):
    """Attach the proxy providers named `slugs` to the embedded outpost. Adds
    only what is missing; every other provider stays attached."""
    if not slugs:
        return "outpost: no proxy providers"
    outpost = embedded_outpost(api)
    wanted = []
    for slug in slugs:
        prov = find_proxy(api, slug)
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
    # Resolve the group before any write: an application created without its binding is open.
    if find_one(api, "group", "/core/groups/", "name", group, name=group) is None:
        raise ApiError(f"group '{group}' not found in Authentik (it is created by sksso, not here)")
    proxies = []
    for app in cfg["apps"]:
        print(ensure_oidc(api, app) if app["kind"] == "oidc" else ensure_proxy(api, app))
        # Bound to skadmin before any proxy provider is attached to the outpost below.
        print(ensure_group_binding(api, app["slug"], group))
        if app["kind"] == "proxy":
            proxies.append(app["slug"])
    print(ensure_outpost(api, proxies))
    for slug in cfg.get("retire") or []:
        print(retire_application(api, slug))


def retire_application(api, slug):
    """Delete the application `slug`; an absent one (404) is already retired."""
    try:
        api.call("DELETE", f"/core/applications/{urllib.parse.quote(slug, safe='')}/", ok=(204,))
    except ApiError as exc:
        if "HTTP 404" not in str(exc):
            raise
        return f"retired {slug}: absent"
    return f"retired {slug}: deleted"


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
