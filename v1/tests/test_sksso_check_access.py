"""optional/sksso/src/sksso/check-access.py: the read-only SSO access
conformance check (design docs/design/sso-app-access-provisioning.md,
section 6; standard checks C1 to C10).

Runs the checker against a fake Authentik API (an `opener` that serves
recorded-shape list responses, paginated, and refuses anything but GET), so
nothing touches the network. Includes the negative control the design
requires (an unbound app must exit 1; a checker never seen to fail is not
evidence) and the zero-applications case (exit 2, never clean)."""
import datetime
import importlib.util
import io
import json
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "sksso" / "src" / "sksso" / "check-access.py"
_spec = importlib.util.spec_from_file_location("sksso_check_access", SCRIPT)
ca = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ca)

TOKEN = "t" * 40


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._raw = json.dumps(body).encode()

    def read(self):
        return self._raw

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeAuthentik:
    """Serves /api/v3/<list>/ endpoints from in-memory lists, paginated like
    Authentik ({"pagination": {"next": N or 0}, "results": [...]})."""

    def __init__(self, apps=(), groups=(), users=(), bindings=(), proxy=(), outposts=(), policies=()):
        self.data = {
            "/core/applications/": list(apps),
            "/core/groups/": list(groups),
            "/core/users/": list(users),
            "/policies/bindings/": list(bindings),
            "/providers/proxy/": list(proxy),
            "/outposts/instances/": list(outposts),
            "/policies/all/": list(policies),
        }
        self.requests = []

    def __call__(self, req, timeout=None):
        assert req.get_method() == "GET" and req.data is None, "checker sent a non-GET request"
        assert req.get_header("Authorization") == "Bearer " + TOKEN
        u = urllib.parse.urlsplit(req.full_url)
        self.requests.append(req.full_url)
        path = u.path.split("/api/v3", 1)[1]
        q = dict(urllib.parse.parse_qsl(u.query))
        if path not in self.data:
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"detail":"not found"}'))
        items = self.data[path]
        page, size = int(q.get("page", 1)), int(q.get("page_size", 100))
        chunk = items[(page - 1) * size: page * size]
        nxt = page + 1 if page * size < len(items) else 0
        return FakeResponse(200, {"pagination": {"next": nxt, "current": page}, "results": chunk})


def app(slug, pk=None, provider=None, mode="any"):
    return {"pk": pk or f"app-{slug}", "slug": slug, "name": slug, "provider": provider, "policy_engine_mode": mode}


def group(name, pk=None, users=(), parent=None, managed=False):
    return {"pk": pk or f"g-{name}", "name": name, "users": list(users), "parent": parent,
            "attributes": {"sk-managed": True} if managed else {}}


def user(pk, username, groups=(), active=True):
    return {"pk": pk, "username": username, "email": f"{username}@example.invalid",
            "groups": list(groups), "is_active": active}


def gbind(target, gname, enabled=True, negate=False):
    return {"pk": f"b-{target}-{gname}", "target": target, "group": f"g-{gname}", "user": None,
            "policy": None, "enabled": enabled, "negate": negate, "order": 10}


def pbind(target, ppk, pname):
    return {"pk": f"b-{target}-{ppk}", "target": target, "group": None, "user": None, "policy": ppk,
            "policy_obj": {"name": pname}, "enabled": True, "negate": False, "order": 10}


def gate(**kw):
    """FakeAuthentik kwargs for a conforming catch-all: a forward_domain provider with
    an application bound to skadmin only, attached to the embedded outpost. Merge into a
    fixture with with_gate(); a check that sees no catch-all reports C7."""
    return {"apps": [app("gate", provider=99)], "proxy": [{"pk": 99, "mode": "forward_domain"}],
            "outposts": [{"providers": [99]}], "bindings": [gbind("app-gate", "skadmin")], **kw}


def with_gate(**kw):
    base = gate()
    for key, val in kw.items():
        base[key] = list(val) + base.get(key, []) if key in base else val
    return FakeAuthentik(**base)


BASE_GROUPS = [group("skadmin", users=[1]), group("skuser", users=[2])]
BASE_USERS = [user(1, "alice", ["g-skadmin"]), user(2, "bob", ["g-skuser"]), user(3, "carol")]


def run(fake, *args, env_token=True, monkeypatch=None):
    argv = ["--url", "http://sksso-prod_server:9000", "--host", "sso.example.invalid", *args]
    return ca.main(argv, stdin=io.StringIO(TOKEN + "\n"), opener=fake)


def findings(out):
    return [ln for ln in out.splitlines() if ln and not ln.startswith("#")]


@pytest.fixture(autouse=True)
def _no_env_token(monkeypatch):
    monkeypatch.delenv("AUTHENTIK_TOKEN", raising=False)


def test_conforming_instance_exits_0(capsys):
    fake = with_gate(apps=[app("skpulse")], groups=BASE_GROUPS, users=BASE_USERS,
                     bindings=[gbind("app-skpulse", "skadmin")])
    assert run(fake) == 0
    out = capsys.readouterr().out
    assert findings(out) == []
    assert "# summary: 2 applications, 0 violations" in out


def test_negative_control_unbound_app_exits_1(capsys):
    fake = FakeAuthentik(apps=[app("skpulse"), app("catchall")], groups=BASE_GROUPS, users=BASE_USERS,
                         bindings=[gbind("app-skpulse", "skadmin")])
    assert run(fake) == 1
    lines = findings(capsys.readouterr().out)
    assert any(ln.startswith("catchall C1 ") for ln in lines)
    assert not any(ln.startswith("skpulse ") for ln in lines)


def test_zero_applications_exits_2(capsys):
    fake = FakeAuthentik(apps=[], groups=BASE_GROUPS, users=BASE_USERS)
    assert run(fake) == 2
    out = capsys.readouterr()
    assert "zero applications" in out.err
    assert "could not run" in out.out


def test_unreachable_api_exits_2(capsys):
    def opener(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    assert run(opener) == 2


def test_http_error_exits_2(capsys):
    def opener(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, io.BytesIO(b'{"detail":"no"}'))
    assert run(opener) == 2
    assert "HTTP 403" in capsys.readouterr().err


def test_no_token_exits_2_and_token_never_printed(capsys):
    fake = FakeAuthentik(apps=[app("x")])
    assert ca.main(["--url", "http://x"], stdin=io.StringIO(""), opener=fake) == 2
    fake = FakeAuthentik(apps=[app("skpulse")], groups=BASE_GROUPS, users=BASE_USERS)
    run(fake)
    out = capsys.readouterr()
    assert TOKEN not in out.out and TOKEN not in out.err


def test_token_from_env(monkeypatch, capsys):
    monkeypatch.setenv("AUTHENTIK_TOKEN", TOKEN)
    fake = with_gate(apps=[app("skpulse")], groups=BASE_GROUPS, users=BASE_USERS,
                     bindings=[gbind("app-skpulse", "skadmin")])
    assert ca.main(["--url", "http://x"], stdin=io.StringIO(""), opener=fake) == 0


def test_token_is_not_an_argument():
    with pytest.raises(SystemExit):
        ca.main(["--url", "http://x", "--token", TOKEN], stdin=io.StringIO(""), opener=None)


def test_pagination_followed_to_the_end(monkeypatch, capsys):
    monkeypatch.setattr(ca, "PAGE_SIZE", 2)
    apps = [app(f"a{i}") for i in range(5)]
    bindings = [gbind(f"app-a{i}", "skadmin") for i in range(4)]  # a4, on page 3, is unbound
    fake = with_gate(apps=apps, groups=BASE_GROUPS, users=BASE_USERS, bindings=bindings)
    assert run(fake) == 1
    out = capsys.readouterr().out
    assert findings(out) == ["a4 C1 no policy bindings: Authentik admits every authenticated user"]
    assert "6 applications" in out
    assert sum("/core/applications/" in r for r in fake.requests) == 3


def test_api_client_refuses_non_get():
    api = ca.Api("http://x", "", TOKEN, opener=lambda *a, **k: pytest.fail("sent"))
    orig = ca.urllib.request.Request

    class Post(orig):
        def get_method(self):
            return "POST"

    ca.urllib.request.Request = Post
    try:
        with pytest.raises(RuntimeError, match="read-only"):
            api.get("/core/applications/")
    finally:
        ca.urllib.request.Request = orig


def test_c2_is_a_warning_unless_strict(capsys):
    groups = BASE_GROUPS + [group("authentik Admins", users=[1])]
    bindings = [gbind("app-skpulse", "skadmin"), gbind("app-skpulse", "authentik Admins")]
    fake = with_gate(apps=[app("skpulse")], groups=groups, users=BASE_USERS, bindings=bindings)
    assert run(fake) == 0
    assert "skpulse C2 warning: bound to 'authentik Admins'" in capsys.readouterr().out
    assert run(fake, "--strict") == 1
    out = capsys.readouterr().out
    assert "skpulse C2 bound to 'authentik Admins'" in out


def test_c3_c4_c5(capsys):
    apps = [app("one"), app("two"), app("three", mode="all"), app("four")]
    groups = BASE_GROUPS + [group("ops")]
    bindings = [
        gbind("app-one", "skadmin"),
        {"pk": "bu", "target": "app-one", "group": None, "user": 3, "policy": None, "enabled": True, "negate": False},
        gbind("app-two", "skadmin", enabled=False),
        gbind("app-three", "skadmin"), gbind("app-three", "ops"),
        gbind("app-four", "skadmin"), gbind("app-four", "ops", negate=True),
    ]
    fake = FakeAuthentik(apps=apps, groups=groups, users=BASE_USERS, bindings=bindings)
    assert run(fake) == 1
    lines = findings(capsys.readouterr().out)
    assert "one C3 bound to a single user (carol)" in lines
    assert any(ln.startswith("two C1 no enabled") for ln in lines)
    assert "two C4 only disabled bindings" in lines
    assert any(ln.startswith("three C5 ") for ln in lines)
    assert "four C4 negated binding on group 'ops'" in lines


def test_c6_admin_group_missing_without_declarations_but_not_for_opt_out(capsys):
    apps = [app("legacy"), app("status")]
    groups = BASE_GROUPS + [group("authentik Admins")]
    bindings = [gbind("app-legacy", "authentik Admins"), pbind("app-status", "p1", "sso-allow-public")]
    fake = FakeAuthentik(apps=apps, groups=groups, users=BASE_USERS, bindings=bindings)
    assert run(fake) == 1
    lines = findings(capsys.readouterr().out)
    assert "legacy C6 global admin group 'skadmin' not bound" in lines
    assert not any(ln.startswith("status C6") for ln in lines)


def test_skipped_summary_without_declarations(capsys):
    fake = FakeAuthentik(apps=[app("skpulse")], groups=BASE_GROUPS, users=BASE_USERS,
                         bindings=[gbind("app-skpulse", "skadmin")])
    run(fake)
    out = capsys.readouterr().out
    skipped = [ln for ln in out.splitlines() if ln.startswith("# skipped:")]
    assert len(skipped) == 1
    for part in ("C6", "C7", "C8", "C9", "C10", "NOT clean"):
        assert part in skipped[0]


def test_catchall_admitting_non_admins_is_c7(capsys):
    apps = [app("catchall", provider=7)]
    groups = BASE_GROUPS + [group("community")]
    bindings = [gbind("app-catchall", "skadmin"), gbind("app-catchall", "community")]
    fake = FakeAuthentik(apps=apps, groups=groups, users=BASE_USERS, bindings=bindings,
                         proxy=[{"pk": 7, "mode": "forward_domain"}], outposts=[{"providers": [7]}])
    assert run(fake) == 1
    assert "catchall C7 catch-all application admits more than the global admin group: community" in \
        findings(capsys.readouterr().out)


def test_c10_global_groups_missing(capsys):
    fake = FakeAuthentik(apps=[app("x")], groups=[group("skadmin")], users=BASE_USERS,
                         bindings=[gbind("app-x", "skadmin")])
    assert run(fake) == 1
    assert "group:skuser C10 required group does not exist" in findings(capsys.readouterr().out)


def test_c6_app_group_parented_to_skuser(capsys):
    groups = BASE_GROUPS + [group("app-x-admins", parent="g-skuser")]
    fake = FakeAuthentik(apps=[app("x")], groups=groups, users=BASE_USERS, bindings=[gbind("app-x", "skadmin")])
    assert run(fake) == 1
    assert any(ln.startswith("group:app-x-admins C6 parent is 'skuser'") for ln in findings(capsys.readouterr().out))


def test_empty_sk_managed_group_is_info_only(capsys):
    groups = BASE_GROUPS + [group("app-x-users", managed=True)]
    fake = with_gate(apps=[app("x")], groups=groups, users=BASE_USERS, bindings=[gbind("app-x", "skadmin")])
    assert run(fake) == 0
    assert "group:app-x-users C9 info: sk-managed group has no members" in capsys.readouterr().out


# ---- with --declarations ----------------------------------------------------

def write_decls(tmp_path, apps, **top):
    p = tmp_path / "decl.json"
    p.write_text(json.dumps({"apps": apps, **top}))
    return str(p)


def per_app_groups(*slugs, members=None):
    members = members or {}
    out = []
    for s in slugs:
        for kind in ("admins", "users"):
            n = f"app-{s}-{kind}"
            out.append(group(n, users=members.get(n, [])))
    return out


def test_declared_conforming_estate(tmp_path, capsys):
    apps = [app("pulse"), app("hub", provider=11), app("status", provider=12), app("catchall", provider=7)]
    groups = BASE_GROUPS + per_app_groups("pulse", "hub", "status")
    bindings = [
        gbind("app-pulse", "skadmin"),
        gbind("app-hub", "skadmin"), gbind("app-hub", "app-hub-admins"), gbind("app-hub", "app-hub-users"),
        gbind("app-hub", "skuser"),
        pbind("app-status", "p1", "sso-allow-public"),
        gbind("app-catchall", "skadmin"),
    ]
    future = (datetime.date.today() + datetime.timedelta(days=60)).isoformat()
    decls = write_decls(tmp_path, {
        "pulse": {"mode": "admin-only"},
        "hub": {"mode": "app-groups", "in_baseline": True, "oidc_admin_groups": ["skadmin", "app-hub-admins"]},
        "status": {"mode": "public", "exemption": {"reason": "status page", "owner": "chef",
                                                    "approved": datetime.date.today().isoformat(),
                                                    "review_by": future}},
    }, catchall_slug="catchall")
    fake = FakeAuthentik(apps=apps, groups=groups, users=BASE_USERS, bindings=bindings,
                         proxy=[{"pk": 7, "mode": "forward_domain"}, {"pk": 12, "mode": "forward_single"}],
                         outposts=[{"providers": [7, 12]}])
    assert run(fake, "--declarations", decls) == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert findings(out) == []
    assert "# skipped:" not in out


def test_declared_drift_c6_c7_c9_c10(tmp_path, capsys):
    apps = [app("pulse"), app("hub"), app("open", provider=12), app("stray")]
    groups = BASE_GROUPS + per_app_groups("pulse", "hub", members={"app-pulse-users": [3]}) + [
        group("app-ghost-users"), group("app-open-admins")]
    bindings = [
        gbind("app-pulse", "skadmin"), gbind("app-pulse", "skuser"),       # skuser on a non-baseline app
        gbind("app-hub", "skadmin"), gbind("app-hub", "app-hub-users"),    # app-hub-admins missing
        pbind("app-open", "p2", "sso-allow-any-account"),
        gbind("app-stray", "skadmin"),
    ]
    decls = write_decls(tmp_path, {
        "pulse": {"mode": "admin-only"},
        "hub": {"mode": "app-groups", "oidc_admin_groups": ["skuser"]},
        "open": {"mode": "any-account", "exemption": {"reason": "r", "owner": "o",
                                                     "approved": "2020-01-01", "review_by": "2020-06-01"}},
    })
    fake = FakeAuthentik(apps=apps, groups=groups, users=BASE_USERS, bindings=bindings,
                         proxy=[{"pk": 12, "mode": "forward_single"}], outposts=[])
    assert run(fake, "--declarations", decls) == 1
    lines = findings(capsys.readouterr().out)
    assert "pulse C6 admin-only: group 'skuser' bound but not declared (skuser on a non-baseline app)" in lines
    assert "pulse C9 admin-only but 'app-pulse-users' has 1 member(s) (ineffective grant)" in lines
    assert "hub C6 app-groups: group 'app-hub-admins' not bound" in lines
    assert "hub C6 'skuser' in the OIDC admin role map" in lines
    assert "hub C7 app-groups but no provider of its own: the catch-all gates it (admins only)" in lines
    assert "open C6 any-account: exemption expired (review_by 2020-06-01)" in lines
    assert "open C7 any-account: its forward-auth provider is on no outpost" in lines
    assert "stray C9 no declared access block" in lines
    assert "group:app-ghost-users C10 app-* group belongs to no declared app" in lines
    assert "group:app-open-users C10 required group does not exist" in lines


def test_exemption_rules(monkeypatch):
    monkeypatch.setattr(ca, "today", lambda: datetime.date(2026, 11, 20))
    ok = {"reason": "r", "owner": "o", "approved": "2026-06-01", "review_by": "2027-06-01"}
    assert ca.exemption_problem(ok) is None
    assert "12 months" in ca.exemption_problem({**ok, "review_by": "2027-06-02"})
    assert "expired" in ca.exemption_problem({**ok, "approved": "2025-01-01", "review_by": "2025-12-01"})
    assert "missing owner" in ca.exemption_problem({**ok, "owner": ""})
    assert ca.exemption_problem(None) == "no exemption record"


def test_c8_tile_differs_from_catchall_gate(tmp_path, capsys):
    apps = [app("pulse"), app("catchall", provider=7)]
    groups = BASE_GROUPS + per_app_groups("pulse") + [group("ops")]
    bindings = [gbind("app-pulse", "skadmin"), gbind("app-pulse", "ops"), gbind("app-catchall", "skadmin")]
    decls = write_decls(tmp_path, {"pulse": {"mode": "admin-only", "adopt": ["ops"]}})
    fake = FakeAuthentik(apps=apps, groups=groups, users=BASE_USERS, bindings=bindings,
                         proxy=[{"pk": 7, "mode": "forward_domain"}], outposts=[{"providers": [7]}])
    assert run(fake, "--declarations", decls) == 1
    lines = findings(capsys.readouterr().out)
    assert any(ln.startswith("pulse C8 tile 'pulse' groups ['ops', 'skadmin'] differ from gate 'catchall'") for ln in lines)


def test_bad_declarations_exit_2(tmp_path):
    p = tmp_path / "d.json"
    p.write_text('{"apps": {"x": {"mode": "everyone"}}}')
    fake = FakeAuthentik(apps=[app("x")])
    assert run(fake, "--declarations", str(p)) == 2
    assert run(fake, "--declarations", str(tmp_path / "missing.json")) == 2


# ---- --who-passes ------------------------------------------------------------

def test_who_passes_lists_usernames_only(capsys):
    groups = BASE_GROUPS + [group("child", users=[3], parent="g-skadmin")]
    users = BASE_USERS + [user(4, "dave", ["g-skadmin"], active=False)]
    fake = FakeAuthentik(apps=[app("tool")], groups=groups, users=users, bindings=[gbind("app-tool", "skadmin")])
    assert run(fake, "--who-passes", "tool") == 0
    out = capsys.readouterr().out
    assert findings(out) == ["alice", "carol"]  # carol via the child group; dave inactive; bob not
    assert "@" not in out


def test_who_passes_unbound_app_admits_everyone(capsys):
    fake = FakeAuthentik(apps=[app("catchall")], groups=BASE_GROUPS, users=BASE_USERS)
    assert run(fake, "--who-passes", "catchall") == 0
    out = capsys.readouterr().out
    assert findings(out) == ["alice", "bob", "carol"]
    assert "NO enabled bindings" in out


def test_who_passes_unknown_policy_is_undetermined(capsys):
    fake = FakeAuthentik(apps=[app("x")], groups=BASE_GROUPS, users=BASE_USERS,
                         bindings=[gbind("app-x", "skadmin"), pbind("app-x", "p9", "custom-expression")])
    assert run(fake, "--who-passes", "x") == 2
    assert findings(capsys.readouterr().out) == ["alice", "? bob", "? carol"]


def test_who_passes_unknown_slug_exits_2(capsys):
    fake = FakeAuthentik(apps=[app("x")], groups=BASE_GROUPS, users=BASE_USERS)
    assert run(fake, "--who-passes", "nope") == 2
