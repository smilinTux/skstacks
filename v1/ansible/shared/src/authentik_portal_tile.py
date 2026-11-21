#!/usr/bin/env python3
"""Create or update an Authentik portal tile and its access bindings, idempotently.

Some roles (skpulse/Uptime Kuma, sksec-web/CrowdSec Web UI, skpatch-hosts/linux
update dashboard, and others) sit behind the cluster's domain-wide Traefik
ForwardAuth catch-all (authentik@file on the router): Authentik already
gates the host, so no OAuth2/Proxy *provider* is needed for auth. What was
missing is the dashboard *tile* itself: Authentik only shows a service on
a user's portal ("My applications") if a `core.Application` object exists
for it, bound to a group. Without one the service works (forward-auth
still 302s an anonymous visitor to SSO) but never appears in the portal,
which is exactly the class of bug this script exists to prevent.

This intentionally creates NO provider: `provider` stays null on a new
Application, same as the fleet's existing hand-made bookmark tiles. When the
slug already has a provider (an OIDC login made by a blueprint) the provider
is kept and only the tile fields are managed. When a role's own HTTP app
needs its own login flow, use the heavier pattern in
optional/skrac/src/skrac/authentik_provision.py or
optional/skmesh/src/skmesh/authentik_provision.py instead.

Access model (docs/design/sso-app-access-provisioning.md, sections 4.2 to 4.6,
normative names from the sk-standards APPLICATION_ACCESS_SSO_STANDARD):

  mode          bound
  admin-only    admin_global                       (the default)
  app-groups    admin_global, admins, users        (+ user_global when in_baseline)
  any-account   the expression policy sso-allow-any-account
  public        the expression policy sso-allow-public

Steps, in this order, any error stops before the next one:
  1. validate the access object (mode, group names, exemption record)
  2. ensure groups (create missing ones with an `sk-managed` marker, no
     members, no parent, never superuser)
  3. ensure the application (as before)
  4. add bindings for every desired group (or the named policy), additive
     only: this script never removes a binding
  5. postcondition: re-read the bindings, fail if the application has no
     enabled binding or (outside any-account and public) lacks the global
     admin group

If step 3 created the application in this run and a later step fails, the
application is deleted again, so a run never leaves behind a new tile with
no bindings.

Input: one JSON object on stdin (secrets included, so nothing is on a
command line or in a process environment):
  url, host, token      - reach Authentik's API (see tasks/authentik_portal_tile.yml)
  slug, name             - the Application's slug and display name
  launch_url             - where the tile links to
  access                 - the resolved access object (tasks/sso_access_resolve.yml):
                           mode, in_baseline, admin_global, user_global, admins,
                           users, extra, legacy, bindings, provider, exemption,
                           bound_groups (optional cross-check)
  group                  - DEPRECATED compatibility input, used only when `access`
                           is absent: the run becomes admin-only plus this group
                           as a legacy additive binding
  icon                   - optional meta_icon ("fa://fa-..." or "application-icons/...")
  open_in_new_tab         - optional bool, default true
  description             - optional meta_description; when empty the tile's existing
                            description is left alone
  category                - optional UI category (the Application's own "group" field);
                            when empty the tile's existing section is left alone
  today                   - optional YYYY-MM-DD used for exemption expiry (tests)
Output: one status line per object, never a secret. Exit 2 on an invalid
access declaration, 1 on any API error or a failed postcondition.
"""
import datetime
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MODES = ("admin-only", "app-groups", "any-account", "public")
POLICY_MODES = {"any-account": "sso-allow-any-account", "public": "sso-allow-public"}
GROUP_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]*$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ORDER_BASE = 10  # reserved band 10 to 19 for bindings this script writes
ORDER_MAX = 19
DEFAULT_ADMIN_GLOBAL = "skadmin"
DEFAULT_USER_GLOBAL = "skuser"


RETRY_ATTEMPTS = 6  # 503 retries back off 5, 10, 15, 20, 25 s: about 75 s in all
RETRY_DELAY_S = 5


class ApiError(Exception):
    pass


class ConfigError(Exception):
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
        # A 503 "authentik starting" comes from Authentik's front server while its
        # backend is not ready, before the request is handled, so any method is
        # safe to resend. A v2.26.30 release gate failed a deploy on one such 503
        # while the server had been up for 22 minutes.
        for attempt in range(RETRY_ATTEMPTS):
            try:
                with urllib.request.urlopen(req, timeout=30) as resp:
                    status, raw = resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, exc.read()
            if status != 503 or attempt == RETRY_ATTEMPTS - 1:
                break
            time.sleep(RETRY_DELAY_S * (attempt + 1))
        if status not in ok:
            raise ApiError(f"{method} {path}: HTTP {status}: {raw[:400].decode(errors='replace')}")
        return json.loads(raw) if raw else None

    def get(self, path, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        return self.call("GET", path + q)

    def first(self, path, **params):
        res = self.get(path, **params).get("results", [])
        return res[0] if res else None

    def get_all(self, path, **params):
        """Every result of a list endpoint, following pagination to the end."""
        out, page = [], 1
        while True:
            res = self.get(path, **{**params, "page": page})
            out.extend(res.get("results", []))
            nxt = (res.get("pagination") or {}).get("next") or 0
            if not nxt or nxt == page:
                return out
            page = nxt


def same(have, want):
    if isinstance(want, list) and isinstance(have, list):
        norm = lambda xs: sorted(json.dumps(x, sort_keys=True) for x in xs)  # noqa: E731
        return norm(have) == norm(want)
    return have == want


# --- access declaration ----------------------------------------------------

def legacy_access(cfg):
    """The compatibility shim for callers that still pass only `group`."""
    group = cfg.get("group") or DEFAULT_ADMIN_GLOBAL
    slug = cfg["slug"]
    return {
        "mode": "admin-only",
        "in_baseline": False,
        "admin_global": DEFAULT_ADMIN_GLOBAL,
        "user_global": DEFAULT_USER_GLOBAL,
        "admins": f"app-{slug}-admins",
        "users": f"app-{slug}-users",
        "extra": [],
        "legacy": [] if group == DEFAULT_ADMIN_GLOBAL else [group],
        "bindings": "additive",
        "provider": "catch-all",
    }


def _add_one_year(date_str):
    year, rest = date_str.split("-", 1)
    return f"{int(year) + 1:04d}-{rest}"


def _valid_date(s):
    if not isinstance(s, str) or not DATE_RE.match(s):
        return False
    try:
        datetime.date.fromisoformat(s)
    except ValueError:
        return False
    return True


def validate_exemption(exemption, today):
    if not isinstance(exemption, dict):
        raise ConfigError("an exemption record (reason, owner, approved, review_by) is required")
    missing = [k for k in ("reason", "owner", "approved", "review_by") if not str(exemption.get(k) or "").strip()]
    if missing:
        raise ConfigError("exemption record is missing: " + ", ".join(missing))
    approved, review_by = str(exemption["approved"]), str(exemption["review_by"])
    for key, value in (("approved", approved), ("review_by", review_by)):
        if not _valid_date(value):
            raise ConfigError(f"exemption {key} must be a YYYY-MM-DD date, got {value!r}")
    if review_by < approved:
        raise ConfigError("exemption review_by is before approved")
    if review_by > _add_one_year(approved):
        raise ConfigError("exemption review_by is more than 12 months after approved")
    if review_by < today:
        raise ConfigError(f"exemption expired (review_by {review_by} is before {today})")


def desired_groups(access):
    """The group set the declared mode derives (section 4.2), without legacy or extra."""
    mode = access["mode"]
    if mode == "admin-only":
        return [access["admin_global"]]
    if mode == "app-groups":
        out = [access["admin_global"], access["admins"], access["users"]]
        if access.get("in_baseline"):
            out.append(access["user_global"])
        return out
    return []


def _unique(xs):
    seen, out = set(), []
    for x in xs:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def validate_access(access, today):
    if not isinstance(access, dict):
        raise ConfigError("access must be an object")
    mode = access.get("mode") or "admin-only"
    if mode not in MODES:
        raise ConfigError(f"unknown access mode {mode!r} (allowed: {', '.join(MODES)})")
    access = {**access, "mode": mode}
    access["in_baseline"] = bool(access.get("in_baseline", False))
    access["extra"] = list(access.get("extra") or [])
    access["legacy"] = list(access.get("legacy") or [])
    access["bindings"] = access.get("bindings") or "additive"
    access["provider"] = access.get("provider") or ("catch-all" if mode == "admin-only" else "own")
    for key in ("admin_global", "user_global", "admins", "users"):
        if not access.get(key):
            raise ConfigError(f"access.{key} is required")
    names = [access[k] for k in ("admin_global", "user_global", "admins", "users")] + access["extra"] + access["legacy"]
    bad = [n for n in names if not isinstance(n, str) or not GROUP_NAME_RE.match(n)]
    if bad:
        raise ConfigError("invalid group name(s): " + ", ".join(repr(n) for n in bad))
    if access["in_baseline"] and mode != "app-groups":
        raise ConfigError(f"in_baseline (SSO_BASELINE) is only valid with app-groups, not {mode}")
    if access["provider"] not in ("catch-all", "own"):
        raise ConfigError(f"unknown provider {access['provider']!r} (catch-all or own)")
    if access["provider"] == "catch-all" and mode != "admin-only":
        raise ConfigError(f"provider catch-all only serves admin-only apps, not {mode}")
    if access["bindings"] == "authoritative":
        raise ConfigError("bindings: authoritative (pruning) is not available in this release, use additive")
    if access["bindings"] != "additive":
        raise ConfigError(f"unknown bindings value {access['bindings']!r}")
    if mode in POLICY_MODES:
        validate_exemption(access.get("exemption"), today)
    elif not desired_groups(access):
        raise ConfigError("the desired group set is empty")
    if "bound_groups" in access and list(access["bound_groups"]) != desired_groups(access):
        raise ConfigError(
            f"access.bound_groups {access['bound_groups']} disagrees with what mode {mode} derives "
            f"{desired_groups(access)}"
        )
    return access


# --- Authentik objects -----------------------------------------------------

def ensure_group(api, name):
    """Look a group up by exact name; create it when missing. Never adds members."""
    match = [g for g in api.get("/core/groups/", name=name).get("results", []) if g.get("name") == name]
    if match:
        print(f"group: unchanged {name}")
        return match[0]
    obj = api.call(
        "POST",
        "/core/groups/",
        # No parent and no roles: a parent would carry access by inheritance,
        # which the standard forbids (design section 4.4).
        {"name": name, "is_superuser": False, "attributes": {"sk-managed": True}},
    )
    print(f"group: created {name}")
    return obj


def ensure_expression_policy(api, name):
    match = [p for p in api.get("/policies/expression/", name=name).get("results", []) if p.get("name") == name]
    if match:
        print(f"policy: unchanged {name}")
        return match[0]
    obj = api.call(
        "POST",
        "/policies/expression/",
        {"name": name, "expression": "return True", "execution_logging": False},
    )
    print(f"policy: created {name}")
    return obj


def ensure_application(api, cfg, state=None):
    """Returns the application. When `state` is a dict, state["created"] says
    whether this call created it."""
    if state is not None:
        state["created"] = False

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
        "policy_engine_mode": "any",
    }
    # The portal section and the description are managed only when the role
    # names one. An empty value used to be PATCHed in on every run, which
    # blanked a section or description an operator (or an earlier run) had
    # set; now an unset value leaves an existing tile alone and a new tile
    # starts without it.
    for field, key in (("group", "category"), ("meta_description", "description")):
        value = cfg.get(key, "") or ""
        if value:
            desired[field] = value
    current = lookup()
    if current is None:
        obj = api.call("POST", "/core/applications/", desired)
        print("application: created")
        if state is not None:
            state["created"] = True
        return obj
    if current.get("provider") is not None:
        # The slug is already a provider-backed app (an OIDC login, say).
        # Nulling its provider would 404 its discovery URL and break every
        # SSO login through it, so keep the provider and manage only the tile.
        del desired["provider"]
        print("application: keeping its existing provider")
    changed = {k: v for k, v in desired.items() if not same(current.get(k), v)}
    if not changed:
        print("application: unchanged")
        return current
    api.call("PATCH", f"/core/applications/{current['slug']}/", changed)
    print(f"application: updated ({', '.join(sorted(changed))})")
    return {**current, **changed}


def _binding_body(app_pk, order, group=None, policy=None):
    return {
        "target": app_pk,
        "group": group,
        "policy": policy,
        "user": None,
        "negate": False,
        "enabled": True,
        "order": order,
        "timeout": 30,
        "failure_result": False,
    }


def ensure_binding(api, app, order, group=None, policy=None, label=""):
    """Additive: create the binding when no binding of this app to that group
    (or policy) exists. Never edits or removes an existing one."""
    key = {"group": group["pk"]} if group is not None else {"policy": policy["pk"]}
    existing = api.get("/policies/bindings/", target=app["pk"], **key).get("results", [])
    if existing:
        print(f"binding: unchanged {label}")
        return existing[0]
    obj = api.call(
        "POST",
        "/policies/bindings/",
        _binding_body(app["pk"], order, group=group["pk"] if group else None, policy=policy["pk"] if policy else None),
    )
    print(f"binding: created {label}")
    return obj


def ensure_group_binding(api, app, group_name):
    """DEPRECATED, the pre access-model helper, kept with its old behaviour for
    existing callers: bind ONE group that must already exist (order 0)."""
    group = api.first("/core/groups/", name=group_name)
    if group is None:
        raise ApiError(f"group '{group_name}' not found in Authentik (this helper never creates groups)")
    existing = api.get("/policies/bindings/", target=app["pk"], group=group["pk"]).get("results", [])
    if existing:
        print("binding: unchanged")
        return
    api.call("POST", "/policies/bindings/", _binding_body(app["pk"], 0, group=group["pk"]))
    print("binding: created")


def check_postcondition(api, app, access, admin_group_pk, policy_pk):
    bindings = api.get_all("/policies/bindings/", target=app["pk"])
    enabled = [b for b in bindings if b.get("enabled", True) and not b.get("negate", False)]
    if not enabled:
        raise ApiError(f"postcondition failed: application {app.get('slug')} has no enabled binding")
    if access["mode"] in POLICY_MODES:
        if not any(b.get("policy") == policy_pk for b in enabled):
            raise ApiError(
                f"postcondition failed: application {app.get('slug')} lacks its "
                f"{POLICY_MODES[access['mode']]} binding"
            )
    elif not any(b.get("group") == admin_group_pk for b in enabled):
        raise ApiError(
            f"postcondition failed: application {app.get('slug')} has no enabled binding "
            f"for the global admin group {access['admin_global']}"
        )
    print(f"postcondition: ok ({len(enabled)} enabled binding(s))")


def provision(api, cfg, access):
    # 2. groups, for every app regardless of mode (provisioning ready, section 4.2)
    wanted = desired_groups(access)
    group_names = _unique(
        [access["admin_global"], access["admins"], access["users"]]
        + ([access["user_global"]] if access["in_baseline"] else [])
        + access["extra"]
        + access["legacy"]
    )
    groups = {name: ensure_group(api, name) for name in group_names}
    policy = None
    if access["mode"] in POLICY_MODES:
        policy = ensure_expression_policy(api, POLICY_MODES[access["mode"]])

    # 3. application
    state = {}
    app = ensure_application(api, cfg, state)
    created = state["created"]
    if access["provider"] == "own":
        print(
            "provider: WARNING own provider requested but not managed by this release; "
            "the tile is written, the gate stays whatever provider serves the host"
        )
    try:
        # 4. bindings, additive (add before remove; this release never removes)
        bind = _unique(wanted + access["extra"] + access["legacy"])
        for i, name in enumerate(bind):
            ensure_binding(api, app, min(ORDER_BASE + i, ORDER_MAX), group=groups[name], label=f"group {name}")
        if policy is not None:
            ensure_binding(api, app, ORDER_BASE, policy=policy, label=f"policy {policy['name']}")
        # 5. postcondition
        check_postcondition(
            api, app, access, groups[access["admin_global"]]["pk"], policy["pk"] if policy else None
        )
    except (ApiError, urllib.error.URLError):
        if created:
            try:
                api.call("DELETE", f"/core/applications/{app['slug']}/")
                print("application: rolled back (created this run, bindings did not complete)")
            except (ApiError, urllib.error.URLError) as exc:
                print(f"application: rollback FAILED, delete {app.get('slug')} by hand: {exc}", file=sys.stderr)
        raise
    return app


REQUIRED = ("url", "token", "slug", "name", "launch_url")


def main():
    cfg = json.load(sys.stdin)
    missing = [k for k in REQUIRED if not cfg.get(k)]
    if not cfg.get("access") and not cfg.get("group"):
        missing.append("access (or the deprecated group)")
    if missing:
        print("missing config: " + ", ".join(missing), file=sys.stderr)
        return 2
    today = cfg.get("today") or datetime.date.today().isoformat()
    try:
        if cfg.get("access"):
            access = dict(cfg["access"])
            if cfg.get("group") and cfg["group"] != access.get("admin_global"):
                access["legacy"] = list(access.get("legacy") or []) + [cfg["group"]]
        else:
            access = legacy_access(cfg)
        for name in access.get("legacy") or []:
            print(f"deprecation: group {name!r} is bound as a legacy additive binding; "
                  "declare SSO_ACCESS instead of AUTHENTIK_TILE_GROUP")
        access = validate_access(access, today)
    except ConfigError as exc:
        print(f"invalid access declaration: {exc}", file=sys.stderr)
        return 2
    try:
        api = Api(cfg["url"], cfg.get("host"), cfg["token"])
        provision(api, cfg, access)
    except (ApiError, urllib.error.URLError) as exc:
        print(f"authentik portal tile provisioning failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
