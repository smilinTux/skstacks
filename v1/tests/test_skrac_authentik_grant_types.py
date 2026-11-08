"""optional/skrac's Authentik provisioner sets OAuth2 grant_types on 2026.2+.

Authentik 2026.2 added OAuth2Provider.grant_types and gives a provider
created without it an empty list, so MeshCentral's sign-in would fail with
invalid_request on a fresh install. Happy path (2026.8 gets the code flow),
edge case (a provider left with [] by an older deploy is repaired), and the
older-API case (2025.12 is never sent the unknown field)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ansible/optional/skrac/src/skrac"))
import authentik_provision as ap  # noqa: E402


class FakeApi:
    def __init__(self, version):
        self.version = version
        self.provider = None
        self.application = None

    def get(self, path):
        assert path == "/admin/version/"
        return {"version_current": self.version}

    def first(self, path, **q):
        if path == "/providers/oauth2/":
            return dict(self.provider) if self.provider else None
        return {"pk": f"pk-{path}"}

    def call(self, method, path, body=None):
        if path.startswith("/core/applications/"):
            if method == "GET":
                if self.application is None:
                    raise ap.ApiError(f"GET {path}: HTTP 404")
                return dict(self.application)
            self.application = {**(self.application or {}), **body}
            return self.application
        if method == "POST":
            self.provider = {"pk": 7, **body}
        elif method == "PATCH":
            self.provider.update(body)
        return dict(self.provider)


CFG = {"provider_name": "skrac", "client_id": "skrac", "client_secret": "s" * 32,
       "authorization_flow": "f", "invalidation_flow": "i", "signing_key": "k",
       "redirect_uris": ["https://rac.example.test/auth-oidc-callback"],
       "app_name": "SKRac", "app_slug": "skrac", "launch_url": "https://rac.example.test/"}


def test_code_flow_grant_types_on_2026():
    api = FakeApi("2026.8.5")
    ap.provision(api, CFG)
    assert api.provider["grant_types"] == ["authorization_code", "refresh_token"]


def test_empty_grant_list_is_repaired(capsys):
    api = FakeApi("2026.8.5")
    ap.provision(api, CFG)
    api.provider["grant_types"] = []
    ap.provision(api, CFG)
    assert api.provider["grant_types"] == ["authorization_code", "refresh_token"]
    assert "grant_types" in capsys.readouterr().out


def test_older_authentik_is_not_sent_grant_types():
    api = FakeApi("2025.12.3")
    ap.provision(api, CFG)
    assert "grant_types" not in api.provider
