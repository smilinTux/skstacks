"""skrac and skmesh portal tiles open in a new tab, like every other tile.

Both provisioners create their Authentik application without
open_in_new_tab, so Authentik's default (False) applied, while the shared
portal-tile helper defaults it to true. Found on gen 2026-11-17: the
skmesh and skrac tiles were the only two opening in the same tab.
"""
import importlib.util
import pathlib

import pytest

OPTIONAL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
ROLES = ("skrac", "skmesh")


def _load(role):
    path = OPTIONAL / role / "src" / role / "authentik_provision.py"
    spec = importlib.util.spec_from_file_location(f"{role}_authentik_provision", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.flow_pk = lambda api, slug, required=True: "flow-pk"
    mod.signing_key = lambda api, name: "key-pk"
    mod.scope_mappings = lambda api, modern: []
    return mod


class FakeApi:
    """Records application writes; every other object looks new."""

    def __init__(self, mod, existing_app=None):
        self.mod = mod
        self.existing_app = existing_app
        self.app_writes = []

    def get(self, path, **params):
        return {"version_current": "2025.12.1"}

    def first(self, path, **params):
        return None

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        if path.startswith("/core/applications/"):
            if method == "GET":
                if self.existing_app is None:
                    raise self.mod.ApiError("HTTP 404")
                return dict(self.existing_app)
            self.app_writes.append((method, body))
        return {"pk": 1, "identifier": "x", "brand_uuid": "b"}


def _cfg(**extra):
    cfg = {k: "v" for k in ("provider_name", "app_name", "app_slug", "launch_url", "client_id",
                             "client_secret", "authorization_flow", "invalidation_flow",
                             "signing_key", "sa_username", "sa_password", "sa_group",
                             "sa_role", "sa_token_identifier")}
    cfg["redirect_uris"] = ["https://h/cb"]
    cfg.update(extra)
    return cfg


@pytest.mark.parametrize("role", ROLES)
def test_create_opens_in_new_tab_by_default(role):
    mod = _load(role)
    api = FakeApi(mod)
    mod.provision(api, _cfg())
    assert api.app_writes == [("POST", api.app_writes[0][1])]
    assert api.app_writes[0][1]["open_in_new_tab"] is True


@pytest.mark.parametrize("role", ROLES)
def test_redeploy_restores_drifted_flag(role):
    mod = _load(role)
    api = FakeApi(mod, existing_app={"pk": "app-pk", "slug": "v", "name": "v", "provider": 1,
                                     "meta_launch_url": "v", "open_in_new_tab": False})
    mod.provision(api, _cfg())
    assert api.app_writes == [("PATCH", {"open_in_new_tab": True})]


@pytest.mark.parametrize("role", ROLES)
def test_redeploy_leaves_matching_app_alone(role):
    mod = _load(role)
    api = FakeApi(mod, existing_app={"pk": "app-pk", "slug": "v", "name": "v", "provider": 1,
                                     "meta_launch_url": "v", "open_in_new_tab": True})
    mod.provision(api, _cfg())
    assert api.app_writes == []


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize("value", [False, "False", "false", "no", "0"])
def test_knob_can_turn_it_off(role, value):
    mod = _load(role)
    api = FakeApi(mod)
    mod.provision(api, _cfg(open_in_new_tab=value))
    assert api.app_writes[0][1]["open_in_new_tab"] is False


@pytest.mark.parametrize("role", ROLES)
def test_deploy_task_passes_the_knob(role):
    text = (OPTIONAL / role / "tasks/authentik.yml").read_text()
    assert f"open_in_new_tab: \"{{{{ {role}.AUTHENTIK_TILE_OPEN_IN_NEW_TAB | default(true) | bool }}}}\"" in text
