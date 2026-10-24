#!/usr/bin/env python3
"""Create or update skrac's side of Authentik, idempotently.

MeshCentral's OIDC strategy is a standard confidential-client authorization
code flow (docs.meshcentral.com/meshcentral/openidConnectStrategy): a
provider plus an application. Unlike skmesh/NetBird, MeshCentral needs no
IdP-manager service account (it reads group membership straight out of the
id_token's `groups` claim, which Authentik's default "profile" scope
mapping already includes) and no device-code flow.

Input: one JSON object on stdin (secrets included, so nothing is on a
command line or in a process environment). Output: one status line per
object, never a secret. Exit non-zero on any API error.

Runs with the Python standard library only, from a throwaway container on
sksso's overlay network, against sksso's server directly; `host` is sent as
the Host header because Authentik answers 404 for a Host it does not know
(e.g. a swarm service name with an underscore). Handles the API difference
between Authentik 2024.4 (newline-separated redirect_uris) and 2024.8+
(structured redirect_uris).
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
}


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
            # Authentik error bodies name fields, never echo the token or
            # the client secret we may have just sent.
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


def ensure(api, path, lookup, desired, label, key="pk", secret_fields=()):
    """Create `desired` at `path` if `lookup` finds nothing, else PATCH the
    fields that differ. `secret_fields` are always sent on an update (the
    API may not echo them back on GET, so a plain diff could never see a
    drifted value): returns the object."""
    current = lookup()
    if current is None:
        obj = api.call("POST", path, desired)
        print(f"{label}: created")
        return obj
    changed = {k: v for k, v in desired.items() if k in secret_fields or not same(current.get(k), v)}
    if not changed:
        print(f"{label}: unchanged")
        return current
    obj = api.call("PATCH", f"{path}{current[key]}/", changed)
    shown = sorted(k if k not in secret_fields else f"{k} (secret)" for k in changed)
    print(f"{label}: updated ({', '.join(shown)})")
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
        m = api.first(path, managed=managed)
        if m is None:
            m = api.first(path, scope_name=scope)
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
        "client_type": "confidential",
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
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
                  provider, "provider", secret_fields=("client_secret",))

    def app_lookup():
        try:
            return api.call("GET", f"/core/applications/{cfg['app_slug']}/")
        except ApiError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
    app_fields = {
        "name": cfg["app_name"],
        "slug": cfg["app_slug"],
        "provider": prov["pk"],
        "meta_launch_url": cfg["launch_url"],
    }
    if cfg.get("icon"):
        app_fields["meta_icon"] = cfg["icon"]
    ensure(api, "/core/applications/", app_lookup, app_fields, "application", key="slug")


REQUIRED = ("url", "token", "client_id", "client_secret", "provider_name", "app_name",
            "app_slug", "launch_url", "redirect_uris", "authorization_flow",
            "invalidation_flow", "signing_key")


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
