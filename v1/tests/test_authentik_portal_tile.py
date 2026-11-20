"""shared/src/authentik_portal_tile.py: the provider-less bookmark-tile
provisioner used by skpulse/sksec-web/skpatch-hosts (and any future role) to put
a dashboard tile on the Authentik portal for a service that already sits
behind the cluster's domain-wide ForwardAuth catch-all.

Exercises `ensure_application` against a mocked Authentik API (urllib is
monkeypatched, nothing touches the network): happy path (create on first
run, then a no-op "unchanged" on a second run with identical desired
state) and the edge case of an update touching only the fields that
actually changed. The access model (groups, bindings, modes, the
postcondition, the compatibility shim) is covered further down against a
stateful fake Authentik (`FakeAuthentik`, which subclasses the real `Api`
and replaces only its HTTP call). The Ansible resolver
(shared/tasks/sso_access_resolve.yml) is exercised under ansible-playbook
on localhost at the end of this file."""
import json
import sys
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ansible" / "shared" / "src"))
import authentik_portal_tile as tile  # noqa: E402


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeApi:
    """Records calls and answers from a scripted {(method, path): body} map,
    with /core/applications/<slug>/ and /policies/bindings/ handled
    specially so tests can drive create-vs-update-vs-unchanged."""

    def __init__(self, application=None, group=None, bindings=None):
        self.application = application  # None = does not exist yet
        self.group = group
        self.bindings = bindings if bindings is not None else []
        self.calls = []

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        self.calls.append((method, path, body))
        if method == "GET" and path.startswith("/core/applications/"):
            if self.application is None:
                raise tile.ApiError(f"GET {path}: HTTP 404: not found")
            return dict(self.application)
        if method == "POST" and path == "/core/applications/":
            self.application = dict(body)
            return dict(self.application)
        if method == "PATCH" and path.startswith("/core/applications/"):
            self.application.update(body)
            return dict(self.application)
        if method == "POST" and path == "/policies/bindings/":
            self.bindings.append(dict(body))
            return dict(body)
        raise AssertionError(f"unexpected call: {method} {path}")

    def get(self, path, **params):
        if path == "/core/groups/":
            results = [self.group] if (self.group and self.group["name"] == params.get("name")) else []
            return {"results": results}
        if path == "/policies/bindings/":
            results = [b for b in self.bindings if b["target"] == params.get("target") and b["group"] == params.get("group")]
            return {"results": results}
        raise AssertionError(f"unexpected get: {path} {params}")

    def first(self, path, **params):
        res = self.get(path, **params).get("results", [])
        return res[0] if res else None


CFG = {
    "slug": "skpulse",
    "name": "SKPulse (Uptime Kuma)",
    "launch_url": "https://skpulse.example.com",
    "group": "authentik Admins",
    "icon": "fa://fa-heartbeat",
    "open_in_new_tab": True,
    "description": "",
    "category": "",
}


def test_ensure_application_creates_when_missing():
    api = FakeApi(application=None)
    app, created = tile.ensure_application(api, CFG)
    assert created
    assert app["slug"] == "skpulse"
    assert app["provider"] is None
    assert app["meta_launch_url"] == "https://skpulse.example.com"
    methods = [c[0] for c in api.calls]
    assert methods == ["GET", "POST"]


def test_ensure_application_unchanged_is_a_noop():
    existing = {
        "slug": "skpulse",
        "name": "SKPulse (Uptime Kuma)",
        "provider": None,
        "launch_url": "https://skpulse.example.com",
        "meta_launch_url": "https://skpulse.example.com",
        "open_in_new_tab": True,
        "meta_icon": "fa://fa-heartbeat",
        "meta_description": "",
        "policy_engine_mode": "any",
        "group": "",
    }
    api = FakeApi(application=existing)
    tile.ensure_application(api, CFG)
    methods = [c[0] for c in api.calls]
    assert methods == ["GET"]  # no PATCH: nothing differs


def test_ensure_application_updates_only_changed_fields():
    existing = {
        "slug": "skpulse",
        "name": "SKPulse (Uptime Kuma)",
        "provider": None,
        "launch_url": "https://old.example.com",
        "meta_launch_url": "https://old.example.com",
        "open_in_new_tab": False,
        "meta_icon": "fa://fa-heartbeat",
        "meta_description": "",
        "policy_engine_mode": "any",
        "group": "",
    }
    api = FakeApi(application=existing)
    tile.ensure_application(api, CFG)
    patch_calls = [c for c in api.calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    changed = patch_calls[0][2]
    assert set(changed) == {"launch_url", "meta_launch_url", "open_in_new_tab"}
    assert changed["meta_launch_url"] == "https://skpulse.example.com"


def test_ensure_application_keeps_an_existing_provider():
    # The slug may already be a real OIDC/proxy app (for example a Nextcloud
    # user_oidc login). Nulling its provider 404s the discovery URL and
    # breaks every SSO login, so an update must leave `provider` alone.
    existing = {
        "slug": "skpulse",
        "name": "SKPulse (Uptime Kuma)",
        "provider": 9,
        "launch_url": "https://old.example.com",
        "meta_launch_url": "https://old.example.com",
        "open_in_new_tab": True,
        "meta_icon": "fa://fa-heartbeat",
        "meta_description": "",
        "policy_engine_mode": "any",
        "group": "",
    }
    api = FakeApi(application=existing)
    app, _ = tile.ensure_application(api, CFG)
    patch_calls = [c for c in api.calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert "provider" not in patch_calls[0][2]
    assert set(patch_calls[0][2]) == {"launch_url", "meta_launch_url"}
    assert api.application["provider"] == 9
    assert app["provider"] == 9


def test_ensure_application_provider_backed_and_otherwise_unchanged_is_a_noop():
    existing = {
        "slug": "skpulse",
        "name": "SKPulse (Uptime Kuma)",
        "provider": 9,
        "launch_url": "https://skpulse.example.com",
        "meta_launch_url": "https://skpulse.example.com",
        "open_in_new_tab": True,
        "meta_icon": "fa://fa-heartbeat",
        "meta_description": "",
        "policy_engine_mode": "any",
        "group": "",
    }
    api = FakeApi(application=existing)
    tile.ensure_application(api, CFG)
    assert [c[0] for c in api.calls] == ["GET"]  # provider 9 is not a difference
    assert api.application["provider"] == 9


def test_main_rejects_missing_required_fields(capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(read=lambda: "{}"))
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"slug": "skpulse"})))
    rc = tile.main()
    assert rc == 2
    captured = capsys.readouterr()
    assert "missing config" in captured.err


def test_api_call_raises_on_http_error(monkeypatch):
    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://x", 404, "not found", {}, None)

        def read(self):
            return b'{"detail": "not found"}'

    def fake_urlopen(req, timeout=30):
        raise FakeHTTPError()

    monkeypatch.setattr(tile.urllib.request, "urlopen", fake_urlopen)
    api = tile.Api("http://sksso-prod_server:9000", "sso.example.com", "token")
    with pytest.raises(tile.ApiError, match="HTTP 404"):
        api.call("GET", "/core/applications/nope/")


def _section_app(group):
    return {
        "slug": "skpulse",
        "name": "SKPulse (Uptime Kuma)",
        "provider": None,
        "launch_url": "https://skpulse.example.com",
        "meta_launch_url": "https://skpulse.example.com",
        "open_in_new_tab": True,
        "meta_icon": "fa://fa-heartbeat",
        "meta_description": "",
        "policy_engine_mode": "any",
        "group": group,
    }


def test_unset_category_leaves_an_existing_section_alone():
    """The tile task used to PATCH `group` to "" on every run, blanking a
    section an operator had set. An empty category now means "not managed"."""
    api = FakeApi(application=_section_app("Monitoring"))
    tile.ensure_application(api, CFG)  # CFG has category ""
    assert [c[0] for c in api.calls] == ["GET"]  # no PATCH
    assert api.application["group"] == "Monitoring"


def test_set_category_still_moves_the_tile():
    api = FakeApi(application=_section_app("Monitoring"))
    tile.ensure_application(api, {**CFG, "category": "Security"})
    patch = [c for c in api.calls if c[0] == "PATCH"]
    assert len(patch) == 1 and patch[0][2] == {"group": "Security"}


def test_new_tile_without_category_has_no_section_key():
    api = FakeApi(application=None)
    tile.ensure_application(api, CFG)
    post = [c for c in api.calls if c[0] == "POST"][0]
    assert "group" not in post[2]


def test_unset_description_leaves_an_existing_description_alone():
    app = _section_app("")
    app["meta_description"] = "Forgejo git hosting"
    api = FakeApi(application=app)
    tile.ensure_application(api, CFG)  # CFG has description ""
    assert [c[0] for c in api.calls] == ["GET"]
    assert api.application["meta_description"] == "Forgejo git hosting"


def test_set_description_is_written():
    api = FakeApi(application=_section_app(""))
    tile.ensure_application(api, {**CFG, "description": "New text"})
    patch = [c for c in api.calls if c[0] == "PATCH"]
    assert len(patch) == 1 and patch[0][2] == {"meta_description": "New text"}


# ===========================================================================
# Access model (design docs/design/sso-app-access-provisioning.md, 4.2 to 4.8)
# ===========================================================================

import itertools  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
import textwrap  # noqa: E402
import urllib.parse  # noqa: E402

TODAY = "2026-11-20"
EXEMPTION = {"reason": "status page", "owner": "chef", "approved": "2026-11-01", "review_by": "2027-05-01"}


class FakeAuthentik(tile.Api):
    """A small stateful Authentik: applications, groups, expression policies
    and policy bindings, with the list filters and pagination the script uses.
    Only `call` is replaced, so the real get/first/get_all run on top of it."""

    def __init__(self, page_size=2, fail_on=None):
        super().__init__("http://fake", "sso.example.com", "t" * 40)
        self.apps, self.groups, self.policies, self.bindings = {}, [], [], []
        self.calls = []
        self.page_size = page_size
        self.fail_on = fail_on  # (method, path prefix) that answers HTTP 500
        self._ids = itertools.count(1)

    def pk(self, prefix):
        return f"{prefix}-{next(self._ids)}"

    def add_group(self, name):
        g = {"pk": self.pk("g"), "name": name, "attributes": {}}
        self.groups.append(g)
        return g

    def group(self, name):
        return next((g for g in self.groups if g["name"] == name), None)

    def bound_group_names(self, slug):
        app = self.apps[slug]
        by_pk = {g["pk"]: g["name"] for g in self.groups}
        return sorted(by_pk[b["group"]] for b in self.bindings if b["target"] == app["pk"] and b.get("group"))

    def bound_policy_names(self, slug):
        app = self.apps[slug]
        by_pk = {p["pk"]: p["name"] for p in self.policies}
        return sorted(by_pk[b["policy"]] for b in self.bindings if b["target"] == app["pk"] and b.get("policy"))

    def _page(self, items, params):
        page = int(params.get("page", 1))
        chunk = items[(page - 1) * self.page_size: page * self.page_size]
        nxt = page + 1 if page * self.page_size < len(items) else 0
        return {"pagination": {"next": nxt}, "results": [dict(x) for x in chunk]}

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        self.calls.append((method, path, body))
        if self.fail_on and method == self.fail_on[0] and path.startswith(self.fail_on[1]):
            raise tile.ApiError(f"{method} {path}: HTTP 500: boom")
        base, _, query = path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        if base.startswith("/core/applications/") and base != "/core/applications/":
            slug = base.split("/")[3]
            if method == "GET":
                if slug not in self.apps:
                    raise tile.ApiError(f"GET {path}: HTTP 404: not found")
                return dict(self.apps[slug])
            if method == "PATCH":
                self.apps[slug].update(body)
                return dict(self.apps[slug])
            if method == "DELETE":
                del self.apps[slug]
                return None
        if base == "/core/applications/" and method == "POST":
            self.apps[body["slug"]] = {**body, "pk": self.pk("app")}
            return dict(self.apps[body["slug"]])
        if base == "/core/groups/":
            if method == "GET":
                return self._page([g for g in self.groups if g["name"] == params.get("name")], params)
            if method == "POST":
                assert "parent" not in body and body["is_superuser"] is False
                g = {**body, "pk": self.pk("g")}
                self.groups.append(g)
                return dict(g)
        if base == "/policies/expression/":
            if method == "GET":
                return self._page([p for p in self.policies if p["name"] == params.get("name")], params)
            if method == "POST":
                p = {**body, "pk": self.pk("p")}
                self.policies.append(p)
                return dict(p)
        if base == "/policies/bindings/":
            if method == "GET":
                res = [b for b in self.bindings if b["target"] == params.get("target")]
                for key in ("group", "policy"):
                    if key in params:
                        res = [b for b in res if b.get(key) == params[key]]
                return self._page(res, params)
            if method == "POST":
                b = {**body, "pk": self.pk("b")}
                self.bindings.append(b)
                return dict(b)
        raise AssertionError(f"unexpected call: {method} {path}")


def access(mode="admin-only", slug="skpulse", **kw):
    a = {
        "mode": mode,
        "in_baseline": False,
        "admin_global": "skadmin",
        "user_global": "skuser",
        "admins": f"app-{slug}-admins",
        "users": f"app-{slug}-users",
        "extra": [],
        "legacy": [],
        "bindings": "additive",
        "provider": "catch-all" if mode == "admin-only" else "own",
    }
    if mode in ("any-account", "public"):
        a["exemption"] = dict(EXEMPTION)
    a.update(kw)
    return a


def run_provision(api, acc, cfg=CFG):
    acc = tile.validate_access(acc, TODAY)
    return tile.provision(api, cfg, acc)


def run_main(monkeypatch, api, payload):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(tile, "Api", lambda *a, **k: api)
    return tile.main()


BASE_INPUT = {"url": "http://fake", "token": "t" * 40, "today": TODAY, **CFG}


# --- every mode produces the table in 4.2 ---------------------------------

@pytest.mark.parametrize(
    "acc,groups,policies",
    [
        (access("admin-only"), ["skadmin"], []),
        (access("app-groups"), ["app-skpulse-admins", "app-skpulse-users", "skadmin"], []),
        (access("app-groups", in_baseline=True),
         ["app-skpulse-admins", "app-skpulse-users", "skadmin", "skuser"], []),
        (access("any-account"), [], ["sso-allow-any-account"]),
        (access("public"), [], ["sso-allow-public"]),
    ],
    ids=["admin-only", "app-groups", "app-groups-baseline", "any-account", "public"],
)
def test_every_mode_binds_its_table_row(acc, groups, policies):
    api = FakeAuthentik()
    run_provision(api, acc)
    assert api.bound_group_names("skpulse") == groups
    assert api.bound_policy_names("skpulse") == policies
    assert api.apps["skpulse"]["policy_engine_mode"] == "any"
    orders = [b["order"] for b in api.bindings]
    assert all(10 <= o <= 19 for o in orders)


def test_skuser_is_never_bound_on_a_non_baseline_app():
    for mode in ("admin-only", "app-groups"):
        api = FakeAuthentik()
        run_provision(api, access(mode))
        assert "skuser" not in api.bound_group_names("skpulse")


def test_per_app_groups_are_created_for_every_mode():
    # Provisioning ready (standard 8.1): A and U exist even for admin-only apps.
    for mode in ("admin-only", "app-groups", "any-account", "public"):
        api = FakeAuthentik()
        run_provision(api, access(mode))
        for name in ("skadmin", "app-skpulse-admins", "app-skpulse-users"):
            g = api.group(name)
            assert g is not None, (mode, name)
            assert g["attributes"] == {"sk-managed": True}
            assert "users" not in g  # never any members


def test_public_policy_is_return_true_and_shared():
    api = FakeAuthentik()
    run_provision(api, access("public"))
    run_provision(api, access("public", slug="skstatus"), cfg={**CFG, "slug": "skstatus"})
    pols = [p for p in api.policies if p["name"] == "sso-allow-public"]
    assert len(pols) == 1 and pols[0]["expression"] == "return True"
    assert api.bound_policy_names("skstatus") == ["sso-allow-public"]


# --- idempotency -------------------------------------------------------------

def test_second_run_is_unchanged(capsys):
    api = FakeAuthentik()
    run_provision(api, access("app-groups", in_baseline=True))
    capsys.readouterr()
    n_groups, n_bindings = len(api.groups), len(api.bindings)
    run_provision(api, access("app-groups", in_baseline=True))
    out = capsys.readouterr().out
    assert "created" not in out
    assert "group: unchanged skadmin" in out and "binding: unchanged group skuser" in out
    assert (len(api.groups), len(api.bindings)) == (n_groups, n_bindings)


def test_existing_groups_are_reused_not_duplicated():
    api = FakeAuthentik()
    existing = api.add_group("skadmin")
    run_provision(api, access())
    assert [g for g in api.groups if g["name"] == "skadmin"] == [existing]


# --- additive: never removes ----------------------------------------------

def test_existing_foreign_bindings_are_kept():
    api = FakeAuthentik()
    old = api.add_group("authentik Admins")
    tile.ensure_application(api, CFG)
    app = api.apps["skpulse"]
    api.bindings.append({"pk": "b-old", "target": app["pk"], "group": old["pk"], "policy": None,
                         "enabled": True, "negate": False, "order": 0})
    run_provision(api, access())
    assert api.bound_group_names("skpulse") == ["authentik Admins", "skadmin"]
    assert not [c for c in api.calls if c[0] == "DELETE"]


def test_authoritative_bindings_are_refused_in_this_release():
    with pytest.raises(tile.ConfigError, match="authoritative"):
        tile.validate_access(access(bindings="authoritative"), TODAY)


# --- the compatibility shim -------------------------------------------------

def test_legacy_group_input_keeps_working(monkeypatch, capsys):
    api = FakeAuthentik()
    api.add_group("authentik Admins")
    rc = run_main(monkeypatch, api, {**BASE_INPUT, "group": "authentik Admins"})
    assert rc == 0
    out = capsys.readouterr().out
    assert api.bound_group_names("skpulse") == ["authentik Admins", "skadmin"]
    assert "deprecation: group 'authentik Admins'" in out


def test_legacy_default_group_is_plain_admin_only(monkeypatch, capsys):
    api = FakeAuthentik()
    rc = run_main(monkeypatch, api, {**BASE_INPUT, "group": "skadmin"})
    assert rc == 0
    assert api.bound_group_names("skpulse") == ["skadmin"]
    assert "deprecation" not in capsys.readouterr().out


def test_explicit_access_plus_legacy_group_is_additive(monkeypatch):
    api = FakeAuthentik()
    rc = run_main(monkeypatch, api, {**BASE_INPUT, "access": access("app-groups"), "group": "ops"})
    assert rc == 0
    assert "ops" in api.bound_group_names("skpulse")
    assert "skadmin" in api.bound_group_names("skpulse")


def test_main_needs_access_or_group(monkeypatch, capsys):
    rc = run_main(monkeypatch, FakeAuthentik(), {k: v for k, v in BASE_INPUT.items() if k != "group"})
    assert rc == 2
    assert "access (or the deprecated group)" in capsys.readouterr().err


# --- validation -------------------------------------------------------------

@pytest.mark.parametrize(
    "acc,match",
    [
        (access("everyone"), "unknown access mode"),
        (access(in_baseline=True), "only valid with app-groups"),
        (access("app-groups", provider="catch-all"), "catch-all only serves admin-only"),
        (access(admins="bad/name"), "invalid group name"),
        (access(extra=[" leading-space"]), "invalid group name"),
        (access("public", exemption=None), "exemption record"),
        (access("any-account", exemption={**EXEMPTION, "owner": ""}), "missing: owner"),
        (access("public", exemption={**EXEMPTION, "review_by": "2027-11-02"}), "more than 12 months"),
        (access("public", exemption={**EXEMPTION, "approved": "2025-11-01", "review_by": "2026-11-01"}), "expired"),
        (access("public", exemption={**EXEMPTION, "review_by": "2027-02-30"}), "YYYY-MM-DD"),
        (access("app-groups", bound_groups=["skadmin"]), "disagrees"),
    ],
    ids=["mode", "baseline-on-admin-only", "catch-all-non-admin", "bad-name", "bad-extra",
         "no-exemption", "exemption-missing-field", "exemption-too-long", "exemption-expired",
         "exemption-bad-date", "bound-groups-drift"],
)
def test_invalid_declarations_are_refused(acc, match):
    with pytest.raises(tile.ConfigError, match=match):
        tile.validate_access(acc, TODAY)


def test_exemption_exactly_twelve_months_is_accepted():
    tile.validate_access(access("public", exemption={**EXEMPTION, "review_by": "2027-11-01"}), TODAY)


def test_invalid_declaration_exits_2_before_any_api_call(monkeypatch, capsys):
    api = FakeAuthentik()
    rc = run_main(monkeypatch, api, {**BASE_INPUT, "access": access("public", exemption=None)})
    assert rc == 2
    assert api.calls == []
    assert "invalid access declaration" in capsys.readouterr().err


# --- never zero bindings: postcondition and rollback ------------------------

def test_postcondition_fails_without_global_admin_binding():
    api = FakeAuthentik()
    tile.ensure_application(api, CFG)
    app = api.apps["skpulse"]
    other = api.add_group("ops")
    api.bindings.append({"pk": "b1", "target": app["pk"], "group": other["pk"], "enabled": True})
    admin = api.add_group("skadmin")
    with pytest.raises(tile.ApiError, match="global admin group skadmin"):
        tile.check_postcondition(api, app, tile.validate_access(access(), TODAY), admin["pk"], None)


def test_postcondition_fails_on_zero_enabled_bindings():
    api = FakeAuthentik()
    tile.ensure_application(api, CFG)
    app = api.apps["skpulse"]
    admin = api.add_group("skadmin")
    api.bindings.append({"pk": "b1", "target": app["pk"], "group": admin["pk"], "enabled": False})
    with pytest.raises(tile.ApiError, match="no enabled binding"):
        tile.check_postcondition(api, app, tile.validate_access(access(), TODAY), admin["pk"], None)


def test_postcondition_follows_pagination():
    # 5 foreign bindings on page 1 to 3, the admin binding last: must still be found.
    api = FakeAuthentik(page_size=2)
    tile.ensure_application(api, CFG)
    app = api.apps["skpulse"]
    for i in range(5):
        g = api.add_group(f"other{i}")
        api.bindings.append({"pk": f"b{i}", "target": app["pk"], "group": g["pk"], "enabled": True})
    admin = api.add_group("skadmin")
    api.bindings.append({"pk": "bA", "target": app["pk"], "group": admin["pk"], "enabled": True})
    tile.check_postcondition(api, app, tile.validate_access(access(), TODAY), admin["pk"], None)


def test_group_creation_failure_leaves_no_application(monkeypatch):
    api = FakeAuthentik(fail_on=("POST", "/core/groups/"))
    rc = run_main(monkeypatch, api, {**BASE_INPUT, "access": access()})
    assert rc == 1
    assert api.apps == {}  # never created, so never an unbound app


def test_binding_failure_rolls_back_a_new_application(monkeypatch, capsys):
    api = FakeAuthentik(fail_on=("POST", "/policies/bindings/"))
    rc = run_main(monkeypatch, api, {**BASE_INPUT, "access": access()})
    assert rc == 1
    assert api.apps == {}
    assert "rolled back" in capsys.readouterr().out


def test_binding_failure_keeps_a_preexisting_application(monkeypatch):
    api = FakeAuthentik(fail_on=("POST", "/policies/bindings/"))
    tile.ensure_application(api, CFG)
    rc = run_main(monkeypatch, api, {**BASE_INPUT, "access": access()})
    assert rc == 1
    assert "skpulse" in api.apps  # only an app made in this run is ever deleted


def test_own_provider_is_warned_not_managed(capsys):
    api = FakeAuthentik()
    run_provision(api, access("app-groups"))
    assert "own provider requested but not managed" in capsys.readouterr().out
    assert api.apps["skpulse"]["provider"] is None


# ===========================================================================
# shared/tasks/sso_access_resolve.yml under ansible-playbook (no network)
# ===========================================================================

RESOLVE = Path(__file__).resolve().parents[1] / "ansible" / "shared" / "tasks" / "sso_access_resolve.yml"
needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def resolve(tmp_path, cfg=None, sksso=None, legacy=None, forward_auth=False, slug="skpulse"):
    out = tmp_path / "sso_access.json"
    inc_vars = {"sso_access_slug": slug, "sso_access_today": TODAY, "sso_access_forward_auth": forward_auth}
    if cfg is not None:
        inc_vars["sso_access_cfg"] = cfg
    if sksso is not None:
        inc_vars["sso_access_sksso"] = sksso
    if legacy is not None:
        inc_vars["sso_access_legacy_groups"] = legacy
    play = tmp_path / "play.yml"
    play.write_text(textwrap.dedent(f"""\
        - hosts: localhost
          gather_facts: false
          tasks:
            - include_tasks: {RESOLVE}
              vars: {json.dumps(inc_vars)}
            - copy:
                content: "{{{{ sso_access | to_json }}}}"
                dest: {out}
        """))
    env = dict(os.environ, ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="false",
               ANSIBLE_INVENTORY_UNPARSED_WARNING="false")
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(play)],
                          capture_output=True, text=True, env=env, timeout=180)
    result = json.loads(out.read_text()) if out.exists() else None
    return proc, result


@needs_ansible
def test_resolver_default_is_admin_only(tmp_path):
    proc, r = resolve(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert r["mode"] == "admin-only" and r["provider"] == "catch-all"
    assert r["bound_groups"] == ["skadmin"]
    assert r["admins"] == "app-skpulse-admins" and r["users"] == "app-skpulse-users"
    assert r["oidc_admin_csv"] == "skadmin,app-skpulse-admins" and r["oidc_user_csv"] == ""
    assert r["in_baseline"] is False and r["legacy"] == [] and r["bindings"] == "additive"
    # the resolver's output is accepted by the script as-is
    tile.validate_access(r, TODAY)


@needs_ansible
def test_resolver_app_groups_baseline_and_estate_names(tmp_path):
    proc, r = resolve(tmp_path, cfg={"SSO_ACCESS": "app-groups", "SSO_BASELINE": True},
                      sksso={"ADMIN_GROUP": "kingdom-admins", "USER_GROUP": "kingdom"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert r["bound_groups"] == ["kingdom-admins", "app-skpulse-admins", "app-skpulse-users", "kingdom"]
    assert r["provider"] == "own" and r["in_baseline"] is True
    assert r["oidc_admin_csv"] == "kingdom-admins,app-skpulse-admins"
    assert r["oidc_user_csv"] == "app-skpulse-users,kingdom"
    assert "kingdom" not in r["oidc_admin_csv"].split(",")  # skuser never maps to admin
    tile.validate_access(r, TODAY)


@needs_ansible
def test_resolver_estate_baseline_list_wins(tmp_path):
    # Role says baseline; the estate list (even empty) replaces it.
    proc, r = resolve(tmp_path, cfg={"SSO_ACCESS": "app-groups", "SSO_BASELINE": True},
                      sksso={"BASELINE_APPS": []})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert r["in_baseline"] is False and "skuser" not in r["bound_groups"]
    proc, r = resolve(tmp_path, cfg={"SSO_ACCESS": "app-groups"}, sksso={"BASELINE_APPS": ["skpulse"]})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert r["in_baseline"] is True and r["bound_groups"][-1] == "skuser"


@needs_ansible
def test_resolver_legacy_group_is_kept_with_a_deprecation(tmp_path):
    proc, r = resolve(tmp_path, legacy=["authentik Admins", "skadmin", ""])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert r["legacy"] == ["authentik Admins"]  # the global admin and blanks are dropped
    assert "DEPRECATION" in proc.stdout


@needs_ansible
def test_resolver_public_with_exemption(tmp_path):
    proc, r = resolve(tmp_path, cfg={"SSO_ACCESS": "public", "SSO_EXEMPTION": EXEMPTION})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert r["bound_groups"] == [] and r["policy"] == "sso-allow-public" and r["provider"] == "own"
    tile.validate_access(r, TODAY)


@needs_ansible
@pytest.mark.parametrize(
    "cfg,sksso,forward_auth,needle",
    [
        ({"SSO_BASELINE": True}, None, False, "Invalid SSO access declaration"),
        ({"SSO_ACCESS": "app-groups"}, {"BASELINE_APPS": ["skpulse"], "ADMIN_GROUP": "x"}, False, None),
        ({"SSO_ACCESS": "app-groups", "SSO_PROVIDER": "catch-all"}, None, True, "Invalid SSO access declaration"),
        ({"SSO_ACCESS": "nope"}, None, False, "Invalid SSO access declaration"),
        ({"SSO_ADMINS_GROUP": "bad/name"}, None, False, "Invalid SSO access declaration"),
        ({"SSO_ACCESS": "public"}, None, False, "needs a valid SSO_EXEMPTION"),
        ({"SSO_ACCESS": "any-account", "SSO_EXEMPTION": {**EXEMPTION, "review_by": "2027-12-01"}},
         None, False, "needs a valid SSO_EXEMPTION"),
        ({"SSO_ACCESS": "public", "SSO_EXEMPTION": {**EXEMPTION, "approved": "2025-01-01", "review_by": "2025-12-01"}},
         None, False, "needs a valid SSO_EXEMPTION"),
    ],
    ids=["baseline-on-admin-only", "estate-list-ok-control", "catch-all-non-admin", "bad-mode",
         "bad-group-name", "public-no-exemption", "exemption-too-long", "exemption-expired"],
)
def test_resolver_rejects_bad_declarations(tmp_path, cfg, sksso, forward_auth, needle):
    proc, r = resolve(tmp_path, cfg=cfg, sksso=sksso, forward_auth=forward_auth)
    if needle is None:  # negative control: a valid declaration in the same harness passes
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return
    assert proc.returncode != 0
    assert needle in proc.stdout
    assert r is None


# ===========================================================================
# The compatibility shim inside shared/tasks/authentik_portal_tile.yml
# ===========================================================================

TILE_TASKS = Path(__file__).resolve().parents[1] / "ansible" / "shared" / "tasks" / "authentik_portal_tile.yml"


def tile_input(tmp_path, portal_tile_extra, sksso_extra=""):
    """Run the real tile task up to the provisioner input and dump that input.
    A fake `docker` answers the network check; the run stops at the staging
    copy (root-owned path), and a rescue block writes the built input out."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text("#!/bin/sh\nexit 0\n")
    (bindir / "docker").chmod(0o755)
    vault = tmp_path / "sksso-vault.yml"
    vault.write_text(f"sksso:\n  api_token: {'x' * 40}\n{sksso_extra}")
    out = tmp_path / "input.json"
    portal_tile = {
        "enabled": True, "slug": "skpulse", "name": "SKPulse", "launch_url": "https://skpulse.example.com",
        "authentik_vault_file": str(vault), **portal_tile_extra,
    }
    play = tmp_path / "play.yml"
    play.write_text(textwrap.dedent(f"""\
        - hosts: localhost
          gather_facts: false
          vars:
            app: skpulse
            env: dev
            domain: example.com
            vault_file_path: {vault}
            portal_tile: {json.dumps(portal_tile)}
          tasks:
            - block:
                - include_tasks: {TILE_TASKS}
              rescue:
                - copy:
                    content: "{{{{ portal_tile_input | to_json }}}}"
                    dest: {out}
        """))
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false")
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(play)],
                          capture_output=True, text=True, env=env, timeout=180)
    assert out.exists(), proc.stdout + proc.stderr
    return proc, json.loads(out.read_text())


@needs_ansible
def test_tile_task_shim_resolves_a_legacy_group(tmp_path):
    proc, data = tile_input(tmp_path, {"group": "authentik Admins"})
    acc = data["access"]
    assert acc["mode"] == "admin-only" and acc["bound_groups"] == ["skadmin"]
    assert acc["legacy"] == ["authentik Admins"]
    assert data["group"] == ""  # carried inside access, not twice
    assert "DEPRECATION" in proc.stdout
    tile.validate_access(acc, TODAY)


@needs_ansible
def test_tile_task_shim_default_group_and_estate_names(tmp_path):
    proc, data = tile_input(tmp_path, {"group": "skadmin", "sso": {"SSO_ACCESS": "app-groups"}},
                            sksso_extra="  BASELINE_APPS: [skpulse]\n")
    acc = data["access"]
    assert acc["legacy"] == []
    assert acc["bound_groups"] == ["skadmin", "app-skpulse-admins", "app-skpulse-users", "skuser"]
    assert "DEPRECATION" not in proc.stdout


@needs_ansible
def test_tile_task_passes_an_explicit_access_through(tmp_path):
    explicit = access("app-groups")
    proc, data = tile_input(tmp_path, {"access": explicit, "group": "ops"})
    assert data["access"] == explicit
    assert data["group"] == "ops"  # the script binds it additively on top
