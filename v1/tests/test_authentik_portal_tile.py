"""shared/src/authentik_portal_tile.py: the provider-less bookmark-tile
provisioner used by skpulse/sksec-web/skpatch (and any future role) to put
a dashboard tile on the Authentik portal for a service that already sits
behind the cluster's domain-wide ForwardAuth catch-all.

Exercises `ensure_application`/`ensure_group_binding` against a mocked
Authentik API (urllib is monkeypatched, nothing touches the network):
happy path (create on first run, then a no-op "unchanged" on a second run
with identical desired state), the edge case of an update touching only
the fields that actually changed, and the failure case of a group name
that does not exist in Authentik (refuses rather than silently shipping a
tile nobody can see)."""
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
    "icon": "fa://fa-heart-pulse",
    "open_in_new_tab": True,
    "description": "",
    "category": "",
}


def test_ensure_application_creates_when_missing():
    api = FakeApi(application=None)
    app = tile.ensure_application(api, CFG)
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
        "meta_icon": "fa://fa-heart-pulse",
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
        "meta_icon": "fa://fa-heart-pulse",
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


def test_ensure_group_binding_fails_loudly_on_missing_group():
    api = FakeApi(group=None)
    app = {"pk": "app-pk", "slug": "skpulse"}
    with pytest.raises(tile.ApiError, match="not found"):
        tile.ensure_group_binding(api, app, "authentik Admins")


def test_ensure_group_binding_creates_when_absent():
    group = {"pk": "group-pk", "name": "authentik Admins"}
    api = FakeApi(group=group, bindings=[])
    app = {"pk": "app-pk", "slug": "skpulse"}
    tile.ensure_group_binding(api, app, "authentik Admins")
    assert len(api.bindings) == 1
    assert api.bindings[0]["target"] == "app-pk"
    assert api.bindings[0]["group"] == "group-pk"
    assert api.bindings[0]["order"] == 0


def test_ensure_group_binding_is_idempotent():
    group = {"pk": "group-pk", "name": "authentik Admins"}
    existing_binding = {"target": "app-pk", "group": "group-pk"}
    api = FakeApi(group=group, bindings=[existing_binding])
    app = {"pk": "app-pk", "slug": "skpulse"}
    tile.ensure_group_binding(api, app, "authentik Admins")
    assert len(api.bindings) == 1  # no duplicate binding created


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
