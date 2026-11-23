#!/usr/bin/env python3
"""Read-only SSO access conformance check for an Authentik instance.

The SKStacks implementation of the standard's `sso-access-check`
(sk-standards `standards/APPLICATION_ACCESS_SSO_STANDARD.md`, section 9.1).
Design: docs/design/sso-app-access-provisioning.md, section 6.

It only ever sends GET requests (enforced in Api.get, not just by habit).
Python stdlib only, so it runs in the same throwaway python container as the
portal tile script.

Usage:
  check-access.py --url <authentik base url> [--host <sso host header>]
                  [--declarations <file.json>] [--strict]
                  [--who-passes <slug>]

The API token is read from the AUTHENTIK_TOKEN environment variable or, when
that is unset, from stdin (first line). It is never taken from argv and
never printed. A read-only token is enough; the token needs view rights on
applications, policy bindings, groups, users, proxy providers and outposts.

Output: one line per finding, `<subject> <check> <detail>`, where subject is
an application slug, or `group:<name>` for a group-level finding. A detail
that starts with `warning:` or `info:` is not a violation. Lines that start
with `#` are summaries (counts, and the checks that were skipped).

Exit codes: 0 conforms, 1 at least one violation, 2 the check could not run
(unreachable API, an API error, no token, a bad declarations file, or zero
applications returned: a check that saw nothing never reports clean).

Checks (standard ids):
  C1  zero enabled bindings (Authentik admits everyone to such an app)
  C2  a binding or a declared OIDC role map names `authentik Admins`
      (warning unless --strict)
  C3  a binding to a single user
  C4  only disabled bindings, or a negated group binding
  C5  more than one enabled group binding with policy engine mode `all`
  C6  bindings differ from the declared mode; `skadmin` missing (also without
      declarations, unless the app is bound to an opt-out policy);
      any-account/public without a valid exemption; `skuser` in an admin role
      map; an app-* group parented to `skadmin` or `skuser`
  C7  an app that is not admin-only is gated by the catch-all (no provider of
      its own, or a domain forward-auth provider); also, without
      declarations, the catch-all itself: missing (no forward_domain provider
      with an application), unbound, bound to anything but `skadmin` (another
      group, a user, a policy), or attached to no outpost
  C8  a tile's groups differ from its gate's groups
  C9  app-* group has members while the app is admin-only; an application
      with no declared access block; info: an empty `sk-managed` group
  C10 a required group is missing (`skadmin`, `skuser`, and with
      declarations each app's admins/users group), or an app-* group that
      belongs to no declared app

Without --declarations the parts of C6, C7, C8, C9 and C10 that need the
declared modes are not run, and a `# skipped:` line says so.

Declarations file (JSON, as rendered by the roles):
  {"admin_group": "skadmin", "user_group": "skuser",     (optional, defaults)
   "catchall_slug": "<slug>",                            (optional)
   "apps": {
     "<slug>": {"mode": "admin-only|app-groups|any-account|public",
                "in_baseline": false,
                "admins": "app-<slug>-admins", "users": "app-<slug>-users",
                "adopt": [], "tile": "<tile slug, when not the same app>",
                "oidc_admin_groups": [], "oidc_user_groups": [],
                "exemption": {"reason": "", "owner": "",
                              "approved": "YYYY-MM-DD", "review_by": "YYYY-MM-DD"}}}}

--who-passes <slug> prints the usernames (never emails) of the active users
that the application's current bindings would admit, one per line. Users
whose result depends on a policy this script cannot evaluate are printed as
`? <username>` and make the exit code 2.
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

AUTHENTIK_ADMINS = "authentik Admins"
POLICY_ANY_ACCOUNT = "sso-allow-any-account"
POLICY_PUBLIC = "sso-allow-public"
OPT_OUT_POLICIES = {POLICY_ANY_ACCOUNT, POLICY_PUBLIC}
MODES = ("admin-only", "app-groups", "any-account", "public")
APP_GROUP_RE = re.compile(r"^app-(.+)-(admins|users)$")
PAGE_SIZE = 100
MAX_PAGES = 10000


class CheckError(Exception):
    """The check could not run (exit 2)."""


class Api:
    """GET-only Authentik API client. There is deliberately no other verb."""

    def __init__(self, url, host, token, opener=None):
        self.base = url.rstrip("/") + "/api/v3"
        self.host = host
        self.token = token
        self.opener = opener or urllib.request.urlopen

    def get(self, path, **params):
        q = ("?" + urllib.parse.urlencode(params)) if params else ""
        req = urllib.request.Request(self.base + path + q, method="GET")
        # Read-only by construction: refuse to send anything that is not a
        # bodyless GET. Not an `assert`, which python -O strips.
        if req.get_method() != "GET" or req.data is not None:
            raise RuntimeError("check-access is read-only")
        req.add_header("Authorization", "Bearer " + self.token)
        req.add_header("Accept", "application/json")
        if self.host:
            req.add_header("Host", self.host)
        try:
            with self.opener(req, timeout=30) as resp:
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            status, raw = exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise CheckError(f"GET {path}: {exc}") from None
        if status != 200:
            raise CheckError(f"GET {path}: HTTP {status}: {raw[:300].decode(errors='replace')}")
        try:
            return json.loads(raw)
        except ValueError:
            raise CheckError(f"GET {path}: not JSON") from None

    def list_all(self, path, **params):
        """Every result of a paginated list endpoint, following pages to the end."""
        out, page = [], 1
        for _ in range(MAX_PAGES):
            body = self.get(path, page=page, page_size=PAGE_SIZE, **params)
            if not isinstance(body, dict) or not isinstance(body.get("results"), list):
                raise CheckError(f"GET {path}: no results list in the answer")
            out.extend(body["results"])
            nxt = (body.get("pagination") or {}).get("next")
            if not nxt:
                return out
            if int(nxt) <= page:
                raise CheckError(f"GET {path}: pagination does not advance (page {page}, next {nxt})")
            page = int(nxt)
        raise CheckError(f"GET {path}: more than {MAX_PAGES} pages")


def today():
    return datetime.date.today()


def subject_group(name):
    return "group:" + (json.dumps(name) if re.search(r"\s", name) else name)


class State:
    """Everything read from Authentik, indexed."""

    def __init__(self, api):
        # superuser_full_list: without it Authentik lists only the apps the
        # token's own user may access, and the check would miss the rest.
        self.apps = api.list_all("/core/applications/", superuser_full_list="true")
        if not self.apps:
            raise CheckError("zero applications returned (wrong URL, host or token rights?)")
        self.groups = api.list_all("/core/groups/", include_users="true")
        self.users = api.list_all("/core/users/")
        self.bindings = api.list_all("/policies/bindings/")
        self.proxy = api.list_all("/providers/proxy/")
        self.outposts = api.list_all("/outposts/instances/")
        self.group_by_pk = {str(g["pk"]): g for g in self.groups}
        self.group_by_name = {g["name"]: g for g in self.groups}
        self.user_by_pk = {str(u["pk"]): u for u in self.users}
        self.proxy_by_pk = {str(p["pk"]): p for p in self.proxy}
        self.on_outpost = {str(pk) for o in self.outposts for pk in (o.get("providers") or [])}
        self.policy_names = {}
        for b in self.bindings:
            if b.get("policy") and (b.get("policy_obj") or {}).get("name"):
                self.policy_names[str(b["policy"])] = b["policy_obj"]["name"]
        if any(b.get("policy") and str(b["policy"]) not in self.policy_names for b in self.bindings):
            for p in api.list_all("/policies/all/"):
                self.policy_names.setdefault(str(p["pk"]), p.get("name", str(p["pk"])))
        self.by_target = {}
        for b in self.bindings:
            self.by_target.setdefault(str(b.get("target")), []).append(b)
        # group pk -> set of member user pks, from both directions of the API
        self.members = {str(g["pk"]): {str(u) for u in (g.get("users") or [])} for g in self.groups}
        for u in self.users:
            for gpk in u.get("groups") or []:
                self.members.setdefault(str(gpk), set()).add(str(u["pk"]))

    def app_bindings(self, app):
        return self.by_target.get(str(app.get("pk")), [])

    def group_name(self, pk):
        g = self.group_by_pk.get(str(pk))
        return g["name"] if g else f"<group {pk}>"

    def policy_name(self, pk):
        return self.policy_names.get(str(pk), f"<policy {pk}>")

    def parents(self, group):
        ps = group.get("parents")
        if ps is None:
            ps = [group["parent"]] if group.get("parent") else []
        return [str(p) for p in ps]

    def gate_sets(self, app):
        """(group names, policy names) of the enabled, non-negated bindings."""
        groups, policies = set(), set()
        for b in self.app_bindings(app):
            if not b.get("enabled", True) or b.get("negate"):
                continue
            if b.get("group"):
                groups.add(self.group_name(b["group"]))
            elif b.get("policy"):
                policies.add(self.policy_name(b["policy"]))
        return groups, policies

    def provider_kind(self, app):
        """'none', 'catch-all' (forward_domain), 'own-proxy', 'own-proxy-offline' or 'other'."""
        prov = app.get("provider")
        if prov is None:
            return "none"
        p = self.proxy_by_pk.get(str(prov))
        if p is None:
            return "other"  # OAuth2, SAML, RAC and so on: the app is its own gate
        if p.get("mode") == "forward_domain":
            return "catch-all"
        return "own-proxy" if str(prov) in self.on_outpost else "own-proxy-offline"


class Report:
    def __init__(self, strict):
        self.strict = strict
        self.violations = self.warnings = self.infos = 0

    def violation(self, subject, check, detail):
        self.violations += 1
        print(f"{subject} {check} {detail}")

    def warning(self, subject, check, detail):
        if self.strict:
            return self.violation(subject, check, detail)
        self.warnings += 1
        print(f"{subject} {check} warning: {detail}")

    def info(self, subject, check, detail):
        self.infos += 1
        print(f"{subject} {check} info: {detail}")


def load_declarations(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise CheckError(f"--declarations {path}: {exc}") from None
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        raise CheckError(f"--declarations {path}: needs an object with an `apps` object")
    for slug, d in data["apps"].items():
        if not isinstance(d, dict) or d.get("mode", "admin-only") not in MODES:
            raise CheckError(f"--declarations {path}: app {slug}: mode must be one of {', '.join(MODES)}")
    return data


def parse_date(value):
    try:
        return datetime.date.fromisoformat(str(value))
    except ValueError:
        return None


def exemption_problem(ex):
    if not isinstance(ex, dict):
        return "no exemption record"
    missing = [k for k in ("reason", "owner", "approved", "review_by") if not str(ex.get(k) or "").strip()]
    if missing:
        return "exemption missing " + ", ".join(missing)
    approved, review = parse_date(ex["approved"]), parse_date(ex["review_by"])
    if approved is None or review is None:
        return "exemption dates are not YYYY-MM-DD"
    try:
        limit = approved.replace(year=approved.year + 1)
    except ValueError:  # 29 February
        limit = approved.replace(year=approved.year + 1, day=28)
    if review > limit:
        return f"exemption review_by {review} is more than 12 months after approved {approved}"
    if review < today():
        return f"exemption expired (review_by {review})"
    return None


def check_bindings(state, app, rep):
    """C1 to C5: needs no declarations."""
    slug = app["slug"]
    bs = state.app_bindings(app)
    enabled = [b for b in bs if b.get("enabled", True)]
    if not bs:
        rep.violation(slug, "C1", "no policy bindings: Authentik admits every authenticated user")
    elif not enabled:
        rep.violation(slug, "C1", f"no enabled policy bindings ({len(bs)} disabled): Authentik admits every authenticated user")
        rep.violation(slug, "C4", "only disabled bindings")
    for b in bs:
        if b.get("group") and state.group_name(b["group"]) == AUTHENTIK_ADMINS:
            rep.warning(slug, "C2", f"bound to {AUTHENTIK_ADMINS!r} (legacy, migrate to the global admin group)")
        if b.get("user"):
            u = state.user_by_pk.get(str(b["user"]))
            rep.violation(slug, "C3", f"bound to a single user ({u['username'] if u else b['user']})")
        if b.get("negate") and b.get("enabled", True) and b.get("group"):
            rep.violation(slug, "C4", f"negated binding on group {state.group_name(b['group'])!r}")
    group_bs = [b for b in enabled if b.get("group")]
    if app.get("policy_engine_mode") == "all" and len(group_bs) > 1:
        rep.violation(slug, "C5", f"policy engine mode 'all' with {len(group_bs)} group bindings (a user must be in every group)")


def check_undeclared(state, app, admin_group, rep):
    """The parts of C6 and C7 that hold whatever the declared mode is."""
    slug = app["slug"]
    groups, policies = state.gate_sets(app)
    # An app with no enabled binding at all is C1 already; say it once.
    if (groups or policies) and not (policies & OPT_OUT_POLICIES) and admin_group not in groups:
        rep.violation(slug, "C6", f"global admin group {admin_group!r} not bound")
    if state.provider_kind(app) == "catch-all":
        check_catchall(state, app, admin_group, groups, policies, rep)


def check_catchall(state, app, admin_group, groups, policies, rep):
    """C7 for the domain-wide forward-auth catch-all application: it must be
    gated by the global admin group and nothing else, and its provider must be
    attached to an outpost. Needs no declarations.

    An application with no enabled binding admits every authenticated user, so
    an unbound catch-all is reported as such; so is one bound to a single
    user, a policy or another group (it admits more, or other, than the admin
    group), or whose provider is on no outpost (forward auth cannot match a
    request, so every app behind authentik@file is denied)."""
    slug = app["slug"]
    users = sorted(
        (state.user_by_pk.get(str(b["user"])) or {}).get("username", str(b["user"]))
        for b in state.app_bindings(app)
        if b.get("enabled", True) and not b.get("negate") and b.get("user"))
    extra = sorted(groups - {admin_group, AUTHENTIK_ADMINS})
    if not groups and not policies and not users:
        rep.violation(slug, "C7", "catch-all application is unbound (no enabled binding): Authentik admits every "
                      f"authenticated user; bind it to {admin_group!r} only")
    elif extra or policies or users:
        rep.violation(slug, "C7", "catch-all application admits more than the global admin group: "
                      + ", ".join(extra + sorted(f"policy {p}" for p in policies)
                                  + [f"user {u}" for u in users]))
    if admin_group not in groups and (groups or policies or users):
        rep.violation(slug, "C7", f"catch-all application is not bound to the global admin group {admin_group!r}")
    if str(app.get("provider")) not in state.on_outpost:
        rep.violation(slug, "C7", "catch-all provider is attached to no outpost: forward auth cannot match a request, "
                      "so every app behind authentik@file is denied")


def expected_for(decl, slug, admin_group, user_group):
    mode = decl.get("mode", "admin-only")
    admins = decl.get("admins") or f"app-{slug}-admins"
    users = decl.get("users") or f"app-{slug}-users"
    if mode == "admin-only":
        return {admin_group}, set(), set()
    if mode == "app-groups":
        want = {admin_group, admins, users} | ({user_group} if decl.get("in_baseline") else set())
        return want, set(), set()
    pol = POLICY_ANY_ACCOUNT if mode == "any-account" else POLICY_PUBLIC
    return set(), {pol}, {admin_group}


def check_declared(state, app, decl, cfg, rep):
    slug = app["slug"]
    admin_group, user_group = cfg["admin_group"], cfg["user_group"]
    mode = decl.get("mode", "admin-only")
    groups, policies = state.gate_sets(app)
    want_g, want_p, tolerated = expected_for(decl, slug, admin_group, user_group)
    tolerated = tolerated | set(decl.get("adopt") or []) | {AUTHENTIK_ADMINS}
    if mode in ("admin-only",) and decl.get("in_baseline"):
        rep.violation(slug, "C6", "in_baseline is set on an admin-only app")
    for g in sorted(want_g - groups):
        if g != admin_group:  # a missing admin group is already reported above
            rep.violation(slug, "C6", f"{mode}: group {g!r} not bound")
    for g in sorted(groups - want_g - tolerated):
        why = " (skuser on a non-baseline app)" if g == user_group else ""
        rep.violation(slug, "C6", f"{mode}: group {g!r} bound but not declared{why}")
    for p in sorted(want_p - policies):
        rep.violation(slug, "C6", f"{mode}: policy {p!r} not bound")
    for p in sorted(policies - want_p):
        rep.violation(slug, "C6", f"{mode}: policy {p!r} bound but not declared")
    if mode in ("any-account", "public"):
        problem = exemption_problem(decl.get("exemption"))
        if problem:
            rep.violation(slug, "C6", f"{mode}: {problem}")
    oidc_admin = list(decl.get("oidc_admin_groups") or [])
    oidc_user = list(decl.get("oidc_user_groups") or [])
    if user_group in oidc_admin:
        rep.violation(slug, "C6", f"{user_group!r} in the OIDC admin role map")
    if AUTHENTIK_ADMINS in oidc_admin + oidc_user:
        rep.warning(slug, "C2", f"OIDC role map names {AUTHENTIK_ADMINS!r}")
    # C7: a non-admin-only app must have a gate of its own
    kind = state.provider_kind(app)
    if mode != "admin-only":
        if kind == "none":
            rep.violation(slug, "C7", f"{mode} but no provider of its own: the catch-all gates it (admins only)")
        elif kind == "catch-all":
            rep.violation(slug, "C7", f"{mode} but its provider is the domain forward-auth catch-all")
        elif kind == "own-proxy-offline":
            rep.violation(slug, "C7", f"{mode}: its forward-auth provider is on no outpost")
    # C8: tile groups versus gate groups
    tile_slug = decl.get("tile")
    gate = None
    if tile_slug and tile_slug != slug:
        tile = next((a for a in state.apps if a["slug"] == tile_slug), None)
        if tile is None:
            rep.violation(slug, "C8", f"declared tile {tile_slug!r} does not exist")
        else:
            gate, tile_app = app, tile
    elif mode == "admin-only" and kind == "none":
        catchall = cfg.get("catchall")
        if catchall is not None and catchall["slug"] != slug:
            gate, tile_app = catchall, app
    if gate is not None:
        tg = state.gate_sets(tile_app)[0] - {AUTHENTIK_ADMINS}
        gg = state.gate_sets(gate)[0] - {AUTHENTIK_ADMINS}
        if tg != gg:
            rep.violation(slug, "C8", f"tile {tile_app['slug']!r} groups {sorted(tg)} differ from gate "
                          f"{gate['slug']!r} groups {sorted(gg)}")
    # C9: grants in per-app groups that have no effect
    if mode == "admin-only":
        for g in (decl.get("admins") or f"app-{slug}-admins", decl.get("users") or f"app-{slug}-users"):
            grp = state.group_by_name.get(g)
            if grp and state.members.get(str(grp["pk"])):
                rep.violation(slug, "C9", f"admin-only but {g!r} has {len(state.members[str(grp['pk'])])} member(s) (ineffective grant)")


def check_groups(state, cfg, decls, rep):
    admin_group, user_group = cfg["admin_group"], cfg["user_group"]
    required = [admin_group, user_group]
    owned = set()
    if decls is not None:
        for slug, d in decls["apps"].items():
            pair = (d.get("admins") or f"app-{slug}-admins", d.get("users") or f"app-{slug}-users")
            owned.update(pair)
            required.extend(pair)
    for g in dict.fromkeys(required):
        if g not in state.group_by_name:
            rep.violation(subject_group(g), "C10", "required group does not exist")
    for g in state.groups:
        name = g["name"]
        if APP_GROUP_RE.match(name):
            for p in state.parents(g):
                pname = state.group_name(p)
                if pname in (admin_group, user_group):
                    rep.violation(subject_group(name), "C6", f"parent is {pname!r}; parents never carry access")
            if decls is not None and name not in owned:
                rep.violation(subject_group(name), "C10", "app-* group belongs to no declared app")
        if (g.get("attributes") or {}).get("sk-managed") and not state.members.get(str(g["pk"])):
            rep.info(subject_group(name), "C9", "sk-managed group has no members (a typo in a group override?)")


def run_check(state, decls, strict):
    rep = Report(strict)
    cfg = {
        "admin_group": (decls or {}).get("admin_group") or "skadmin",
        "user_group": (decls or {}).get("user_group") or "skuser",
    }
    catchall_slug = (decls or {}).get("catchall_slug")
    catchalls = [a for a in state.apps if state.provider_kind(a) == "catch-all"]
    if catchall_slug:
        cfg["catchall"] = next((a for a in state.apps if a["slug"] == catchall_slug), None)
        if cfg["catchall"] is None:
            rep.violation(catchall_slug, "C7", "declared catch-all application does not exist")
    elif len(catchalls) == 1:
        cfg["catchall"] = catchalls[0]
    if not catchalls and not (catchall_slug and cfg.get("catchall") is None):
        # The catch-all is what authentik@file routers without a provider of
        # their own are authorised against. None at all means those apps are
        # unreachable (or someone is about to hand-make one with no binding).
        orphan = [p["name"] for p in state.proxy
                  if p.get("mode") == "forward_domain" and not any(str(a.get("provider")) == str(p["pk"]) for a in state.apps)]
        rep.violation("catch-all", "C7", "no domain-wide forward-auth catch-all application exists "
                      "(a forward_domain proxy provider with an application)"
                      + (f"; provider(s) with no application: {', '.join(sorted(orphan))}" if orphan else ""))
    for app in sorted(state.apps, key=lambda a: a["slug"]):
        check_bindings(state, app, rep)
        check_undeclared(state, app, cfg["admin_group"], rep)
        if decls is not None:
            decl = decls["apps"].get(app["slug"])
            if decl is None:
                if cfg.get("catchall") is not app:
                    rep.violation(app["slug"], "C9", "no declared access block")
            else:
                check_declared(state, app, decl, cfg, rep)
    if decls is not None:
        known = {a["slug"] for a in state.apps}
        for slug in sorted(set(decls["apps"]) - known):
            rep.info(slug, "C10", "declared app has no Authentik application")
    check_groups(state, cfg, decls, rep)
    print(f"# summary: {len(state.apps)} applications, {rep.violations} violations, "
          f"{rep.warnings} warnings, {rep.infos} info")
    if decls is None:
        print("# skipped: C6 declared-mode comparison and exemptions, C7 per-app provider, C8, "
              "C9 ineffective grants and missing access blocks, C10 per-app and orphan app-* groups "
              "(no --declarations); these are NOT clean, they were not checked")
    return 1 if rep.violations else 0


def who_passes(state, slug):
    app = next((a for a in state.apps if a["slug"] == slug), None)
    if app is None:
        raise CheckError(f"no application with slug {slug!r}")
    bs = [b for b in state.app_bindings(app) if b.get("enabled", True)]
    mode = app.get("policy_engine_mode", "any")

    def user_groups(u):
        """The user's groups plus every ancestor (a parent group's binding admits a child's members)."""
        seen, todo = set(), [g for g, m in state.members.items() if str(u["pk"]) in m]
        while todo:
            g = todo.pop()
            if g in seen:
                continue
            seen.add(g)
            grp = state.group_by_pk.get(g)
            if grp:
                todo.extend(state.parents(grp))
        return seen

    def one(b, u, ugroups):
        if b.get("group"):
            r = str(b["group"]) in ugroups
        elif b.get("user"):
            r = str(b["user"]) == str(u["pk"])
        elif b.get("policy"):
            if state.policy_name(b["policy"]) in OPT_OUT_POLICIES:
                r = True
            else:
                return None
        else:
            return None
        return (not r) if b.get("negate") else r

    passed, unknown = [], []
    for u in state.users:
        if not u.get("is_active", True):
            continue
        if not bs:
            res = True  # no bindings: Authentik admits everyone
        else:
            ug = user_groups(u)
            rs = [one(b, u, ug) for b in bs]
            if mode == "all":
                res = False if False in rs else (None if None in rs else True)
            else:
                res = True if True in rs else (None if None in rs else False)
        if res is True:
            passed.append(u["username"])
        elif res is None:
            unknown.append(u["username"])
    for name in sorted(passed):
        print(name)
    for name in sorted(unknown):
        print(f"? {name}")
    note = " (NO enabled bindings: every active user passes)" if not bs else ""
    print(f"# summary: {len(passed)} of {sum(1 for u in state.users if u.get('is_active', True))} "
          f"active users would pass {slug}{note}; {len(unknown)} undetermined")
    return 2 if unknown else 0


def read_token(stdin):
    token = os.environ.get("AUTHENTIK_TOKEN", "")
    if not token:
        token = stdin.readline()
    token = token.strip()
    if not token:
        raise CheckError("no API token (set AUTHENTIK_TOKEN or pipe it on stdin)")
    return token


def main(argv=None, stdin=None, opener=None):
    ap = argparse.ArgumentParser(description="Read-only SSO access conformance check (C1 to C10).")
    ap.add_argument("--url", default=os.environ.get("AUTHENTIK_URL", ""), help="Authentik base URL")
    ap.add_argument("--host", default="", help="Host header to send (when the URL is an internal service name)")
    ap.add_argument("--declarations", help="JSON file: slug to mode, in_baseline and exemption")
    ap.add_argument("--strict", action="store_true", help="C2 (authentik Admins) is a violation, not a warning")
    ap.add_argument("--who-passes", metavar="SLUG", help="print the users the app's current bindings admit")
    args = ap.parse_args(argv)
    try:
        if not args.url:
            raise CheckError("--url (or AUTHENTIK_URL) is required")
        decls = load_declarations(args.declarations) if args.declarations else None
        api = Api(args.url, args.host, read_token(stdin or sys.stdin), opener=opener)
        state = State(api)
        if args.who_passes:
            return who_passes(state, args.who_passes)
        return run_check(state, decls, args.strict)
    except CheckError as exc:
        print(f"# error: {exc}", file=sys.stderr)
        print("# result: could not run (exit 2)")
        return 2


if __name__ == "__main__":
    sys.exit(main())
