"""skai (Open WebUI) sign-in through sksso (Authentik) over OIDC, as code:
the sksso.SKAI_OIDC_ENABLED blueprint (same mechanism as skgit's), with the
client secret only in the 0600 env file (read with !Env), off by default
with nothing rendered, and the redirects matching what skai configures."""
import json
import pathlib
import re

import jinja2
import pytest
import yaml

OPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
SKSSO = OPT / "sksso"
SECRET = "skai-oidc-client-secret-0123456789abcdef-0123"
ENVS = ("dev", "staging", "prod")


class _Loader(yaml.SafeLoader):
    pass


for _tag in ("Find", "KeyOf", "Env"):
    _Loader.add_constructor("!" + _tag, lambda loader, node: (node.tag, loader.construct_sequence(node, deep=True)
                                                              if isinstance(node, yaml.SequenceNode)
                                                              else loader.construct_scalar(node)))


def _jenv(strict=False):
    j = jinja2.Environment(undefined=jinja2.StrictUndefined if strict else jinja2.ChainableUndefined,
                           trim_blocks=True)
    j.filters["to_json"] = json.dumps
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    return j


def _blueprint(**over):
    ctx = dict(sksso_skai_client_id="skai", sksso_skai_host="skai.example.com",
               sksso_skai_app_name="SKAI", sksso_skai_authentication_flow="")
    ctx.update(over)
    text = (SKSSO / "src/config/sksso/blueprints/skai-oidc.yaml.j2").read_text()
    return text, yaml.load(_jenv(True).from_string(text).render(**ctx), _Loader)


def _entries(bp):
    return {e["model"]: e for e in bp["entries"]}


def test_blueprint_provider_and_application():
    _, bp = _blueprint()
    e = _entries(bp)
    prov = e["authentik_providers_oauth2.oauth2provider"]
    assert prov["identifiers"] == {"name": "skai"}
    a = prov["attrs"]
    assert a["client_type"] == "confidential" and a["client_id"] == "skai"
    assert a["client_secret"] == ("!Env", "SKAI_OIDC_CLIENT_SECRET")
    assert a["grant_types"] == ["authorization_code", "refresh_token"]
    assert a["authentication_flow"] is None
    scopes = [m[1][1][1] for m in a["property_mappings"]]
    assert scopes == ["goauthentik.io/providers/oauth2/scope-openid", "goauthentik.io/providers/oauth2/scope-email",
                      "goauthentik.io/providers/oauth2/scope-profile"]
    app = e["authentik_core.application"]
    assert app["identifiers"] == {"slug": "skai"} and app["attrs"]["name"] == "SKAI"
    assert app["attrs"]["meta_launch_url"] == "https://skai.example.com/"


def test_redirects_are_strict_and_match_open_webui_callbacks():
    _, bp = _blueprint()
    uris = _entries(bp)["authentik_providers_oauth2.oauth2provider"]["attrs"]["redirect_uris"]
    assert uris == [{"matching_mode": "strict", "url": "https://skai.example.com/oauth/oidc/login/callback"},
                    {"matching_mode": "strict", "url": "https://skai.example.com/oauth/oidc/callback"}]
    # skai's own env sets the same callback as OPENID_REDIRECT_URI.
    env_tpl = (OPT / "skai/src/config/skai/skai.env.j2").read_text()
    assert "OPENID_REDIRECT_URI=https://{{ ska_host }}/oauth/oidc/login/callback" in env_tpl


def test_capauth_flow_binds_when_set():
    _, bp = _blueprint(sksso_skai_authentication_flow="capauth-authentication")
    a = _entries(bp)["authentik_providers_oauth2.oauth2provider"]["attrs"]
    assert a["authentication_flow"] == ("!Find", ["authentik_flows.flow", ["slug", "capauth-authentication"]])


def test_blueprint_never_carries_the_secret():
    text, _ = _blueprint()
    assert SECRET not in text
    assert "sksso.SKAI_OIDC_CLIENT_SECRET" not in re.sub(r"(?m)^\s*#.*$", "", text)
    tasks = yaml.safe_load((SKSSO / "tasks/skai_blueprint.yml").read_text())
    assert next(t for t in tasks if "template" in t)["template"]["mode"] == "0644"


SKSSO_VAULT = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}


def _compose(**sksso):
    out = _jenv().from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env="prod", app="sksso", sksso={**SKSSO_VAULT, **sksso})
    return yaml.safe_load(out)["services"]


def _env_file(**sksso):
    base = {"postgres_user": "u", "postgres_password": "p", "authentik_secret_key": "k",
            "CLUSTERNAME": "c", "DOMAIN": "example.com"}
    return _jenv().from_string((SKSSO / "src/config/sksso/sksso.env.j2").read_text()).render(
        env="prod", sksso={**base, **sksso})


def test_off_renders_nothing_about_skai():
    for svc in ("server", "worker"):
        assert not any("/blueprints/" in v for v in _compose()[svc]["volumes"])
    assert "SKAI" not in _env_file()
    assert _env_file(SKAI_OIDC_CLIENT_SECRET=SECRET) == _env_file()
    assert _env_file(SKAI_OIDC_ENABLED=False, SKAI_OIDC_CLIENT_SECRET=SECRET) == _env_file()
    assert _compose(SKAI_OIDC_ENABLED=False, SKAI_OIDC_CLIENT_SECRET=SECRET) == _compose()


def test_on_mounts_blueprints_and_writes_the_secret_to_the_env_file_only():
    for svc in ("server", "worker"):
        vols = _compose(SKAI_OIDC_ENABLED=True, SKAI_OIDC_CLIENT_SECRET=SECRET)[svc]["volumes"]
        assert "/var/data/config/sksso-prod/blueprints:/blueprints/custom:ro" in vols
    assert f"\nSKAI_OIDC_CLIENT_SECRET={SECRET}\n" in _env_file(SKAI_OIDC_ENABLED=True, SKAI_OIDC_CLIENT_SECRET=SECRET)
    out = _jenv().from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env="prod", app="sksso", sksso={**SKSSO_VAULT, "SKAI_OIDC_ENABLED": True, "SKAI_OIDC_CLIENT_SECRET": SECRET})
    assert SECRET not in out


@pytest.mark.parametrize("env", ENVS)
def test_every_sksso_playbook_includes_the_task_only_when_enabled(env):
    doc = yaml.safe_load((SKSSO / f"deploy_sksso-{env}.yml").read_text())
    tasks = [t for play in doc if isinstance(play, dict) for t in play.get("tasks", [])]
    inc = next(t for t in tasks if "skai (Open WebUI) OAuth2 blueprint" in t.get("name", ""))
    assert inc["include_tasks"]["file"] == "tasks/skai_blueprint.yml"
    assert inc["when"] == "sksso.SKAI_OIDC_ENABLED | default(false) | bool"
    assert set(inc["tags"]) == {"templates", "deploy"}
