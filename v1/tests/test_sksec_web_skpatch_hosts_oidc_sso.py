"""sksec-web and skpatch-hosts single sign-on through sksso (Authentik), as code
(AUTH_MODE: oidc on either app's own deploy).

Authentik side: sksso renders a blueprint for the app's own OAuth2/OIDC
provider + application when sksso.SKSEC_WEB_OIDC_CLIENT_SECRET /
sksso.SKPATCH_HOSTS_OIDC_CLIENT_SECRET is set (and nothing changes when it is
not); the client secret is never in the blueprint, only the !Env reference.
Unlike skflow (Windmill), neither app needs its own provisioner script:
both read their OIDC config from plain env vars (verified against the
pinned crowdsec-web-ui / linux-update-dashboard source, see the two roles'
own README "Sign-in..." sections), which the apps' own deploy playbooks
render (covered by test_sksec_web_render.py / test_skpatch_hosts_render.py).

This file covers only the sksso side, and the one design constraint that
spans both roles: the blueprint's Application slug defaults to the SAME
value each app's own portal tile defaults to, so
shared/tasks/authentik_portal_tile.yml ends up managing ONE Authentik
Application (the OAuth2-backed one), never a second provider-less one
(shared/src/authentik_portal_tile.py "keeping its existing provider",
#242)."""
import pathlib

import jinja2
import pytest
import yaml

OPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
SKSSO = OPT / "sksso"
SKSW = OPT / "sksec-web"
SKPATCH_HOSTS = OPT / "skpatch-hosts"
SKSW_SECRET = "sksec-web-oidc-client-secret-0123456789abcdef"
SKPATCH_HOSTS_SECRET = "skpatch-hosts-oidc-client-secret-0123456789abcdef"


class _Loader(yaml.SafeLoader):
    pass


for _tag in ("Find", "KeyOf", "Env"):
    _Loader.add_constructor("!" + _tag, lambda loader, node: (node.tag, loader.construct_sequence(node, deep=True)
                                                              if isinstance(node, yaml.SequenceNode)
                                                              else loader.construct_scalar(node)))


def _jenv(strict=False):
    j = jinja2.Environment(undefined=jinja2.StrictUndefined if strict else jinja2.ChainableUndefined,
                           trim_blocks=True)
    j.filters["to_json"] = __import__("json").dumps
    return j


def _blueprint(name, **over):
    if name == "sksec-web":
        ctx = dict(sksso_sksec_web_client_id="sksec-web", sksso_sksec_web_host="sksec-web.example.com",
                   sksso_sksec_web_app_name="SKSec Web (CrowdSec)")
        path = SKSSO / "src/config/sksso/blueprints/sksec-web-oidc.yaml.j2"
    else:
        ctx = dict(sksso_skpatch_hosts_client_id="skpatch-hosts", sksso_skpatch_hosts_host="skpatch-hosts.example.com",
                   sksso_skpatch_hosts_app_name="SKPatch Hosts")
        path = SKSSO / "src/config/sksso/blueprints/skpatch-hosts-oidc.yaml.j2"
    ctx.update(over)
    text = path.read_text()
    return text, yaml.load(_jenv(True).from_string(text).render(**ctx), _Loader)


def _compose(**sksso):
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    base.update(sksso)
    out = _jenv().from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env="prod", app="sksso", sksso=base)
    return yaml.safe_load(out)["services"]


def _inc(task):
    inc = task.get("include_tasks")
    return inc.get("file") if isinstance(inc, dict) else inc


def _tasks(role, name):
    return yaml.safe_load((OPT / role / "tasks" / name).read_text())


# ---------------------------------------------------------------- Authentik

# role -> the app's own env var prefix. A dict, not a list of string pairs:
# secret scanners read ("name", "PREFIX") pairs as username/password tuples.
ENV_PREFIX = {"sksec-web": "AUTH", "skpatch-hosts": "LUDASH"}


@pytest.mark.parametrize("name", sorted(ENV_PREFIX))
def test_blueprint_describes_the_provider_and_application(name):
    env_prefix = ENV_PREFIX[name]
    text, bp = _blueprint(name)
    entries = {e["model"]: e for e in bp["entries"]}
    scope = entries["authentik_providers_oauth2.scopemapping"]
    assert scope["attrs"]["scope_name"] == "groups"
    assert "ak_groups" in scope["attrs"]["expression"]
    prov = entries["authentik_providers_oauth2.oauth2provider"]
    assert prov["identifiers"] == {"name": name}
    a = prov["attrs"]
    assert a["client_type"] == "confidential" and a["client_id"] == name
    assert a["sub_mode"] == "hashed_user_id"
    assert a["issuer_mode"] == "per_provider"
    assert a["redirect_uris"] == [{"matching_mode": "strict",
                                   "url": f"https://{name}.example.com/api/auth/oidc/callback"}]
    app = entries["authentik_core.application"]
    assert app["identifiers"] == {"slug": name}
    assert app["attrs"]["provider"][0] == "!KeyOf"
    # No policy binding in the blueprint: the portal tile (same slug) owns
    # visibility/group binding instead, see the module docstring.
    assert "authentik_policies.policybinding" not in entries


@pytest.mark.parametrize("name", ["sksec-web", "skpatch-hosts"])
def test_blueprint_never_carries_the_secret_only_an_env_reference(name):
    secret_env = "SKSEC_WEB_OIDC_CLIENT_SECRET" if name == "sksec-web" else "SKPATCH_HOSTS_OIDC_CLIENT_SECRET"
    secret = SKSW_SECRET if name == "sksec-web" else SKPATCH_HOSTS_SECRET
    text, bp = _blueprint(name)
    prov = next(e for e in bp["entries"] if e["model"].endswith("oauth2provider"))
    assert prov["attrs"]["client_secret"] == ("!Env", secret_env)
    assert secret not in text


def test_compose_mounts_blueprints_when_either_app_is_enabled():
    for svc in ("server", "worker"):
        off = _compose()[svc]["volumes"]
        assert not any("/blueprints/" in v for v in off)
        on_sksw = _compose(SKSEC_WEB_OIDC_CLIENT_SECRET=SKSW_SECRET)[svc]["volumes"]
        assert "/var/data/config/sksso-prod/blueprints:/blueprints/custom:ro" in on_sksw
        on_skp = _compose(SKPATCH_HOSTS_OIDC_CLIENT_SECRET=SKPATCH_HOSTS_SECRET)[svc]["volumes"]
        assert "/var/data/config/sksso-prod/blueprints:/blueprints/custom:ro" in on_skp


def test_env_file_carries_each_secret_only_when_enabled():
    tpl = (SKSSO / "src/config/sksso/sksso.env.j2").read_text()
    base = {"postgres_user": "u", "postgres_password": "p", "authentik_secret_key": "k",
            "CLUSTERNAME": "c", "DOMAIN": "example.com"}
    off = _jenv().from_string(tpl).render(env="prod", sksso=base)
    assert "SKSEC_WEB_OIDC_CLIENT_SECRET=" not in off
    assert "SKPATCH_HOSTS_OIDC_CLIENT_SECRET=" not in off
    on = _jenv().from_string(tpl).render(env="prod", sksso=dict(
        base, SKSEC_WEB_OIDC_CLIENT_SECRET=SKSW_SECRET, SKPATCH_HOSTS_OIDC_CLIENT_SECRET=SKPATCH_HOSTS_SECRET))
    assert f"\nSKSEC_WEB_OIDC_CLIENT_SECRET={SKSW_SECRET}\n" in on
    assert f"\nSKPATCH_HOSTS_OIDC_CLIENT_SECRET={SKPATCH_HOSTS_SECRET}\n" in on


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_sksso_playbooks_render_both_blueprints_before_the_compose(env):
    tasks = [t for p in yaml.safe_load((SKSSO / f"deploy_sksso-{env}.yml").read_text()) for t in (p.get("tasks") or [])]
    names = [t.get("name", "") for t in tasks]
    for task_file, when_key in (
        ("tasks/sksec_web_blueprint.yml", "SKSEC_WEB_OIDC_CLIENT_SECRET"),
        ("tasks/skpatch_hosts_blueprint.yml", "SKPATCH_HOSTS_OIDC_CLIENT_SECRET"),
    ):
        i = next(i for i, t in enumerate(tasks) if _inc(t) == task_file)
        assert tasks[i]["when"] == f"sksso.{when_key} | default('', true) | string | length > 0"
        assert i < names.index("Process {{ app }} Docker Compose template") < names.index("Deploy {{ app }} stack")


@pytest.mark.parametrize("role,task_file", [("sksso", "sksec_web_blueprint.yml"), ("sksso", "skpatch_hosts_blueprint.yml")])
def test_blueprint_task_refuses_a_short_secret(role, task_file):
    assert_task = next(t for t in _tasks(role, task_file) if "assert" in t)
    assert any(">= 32" in c for c in assert_task["assert"]["that"])


# ----------------------------------------------------- same-slug constraint

def test_sksec_web_oidc_client_id_defaults_to_the_router_name_like_the_portal_tile():
    pb = (SKSW / "deploy_sksec-web-prod.yml").read_text()
    assert 'sksw_oidc_client_id: "{{ sksec_web.OIDC_CLIENT_ID | default(sksw_router, true) }}"' in pb
    assert 'slug: "{{ sksec_web.AUTHENTIK_TILE_SLUG | default(sksw_router, true) }}"' in pb
    blueprint_tasks = _tasks("sksso", "sksec_web_blueprint.yml")
    facts = next(t["set_fact"] for t in blueprint_tasks if "sksso_sksec_web_client_id" in (t.get("set_fact") or {}))
    assert "default('sksec-web'" in facts["sksso_sksec_web_client_id"]


def test_skpatch_hosts_oidc_client_id_defaults_to_the_router_name_like_the_portal_tile():
    pb = (SKPATCH_HOSTS / "deploy_skpatch-hosts-prod.yml").read_text()
    assert 'skp_oidc_client_id: "{{ skpatch_hosts.OIDC_CLIENT_ID | default(skp_router, true) }}"' in pb
    assert 'slug: "{{ skpatch_hosts.AUTHENTIK_TILE_SLUG | default(skp_router, true) }}"' in pb
    blueprint_tasks = _tasks("sksso", "skpatch_hosts_blueprint.yml")
    facts = next(t["set_fact"] for t in blueprint_tasks if "sksso_skpatch_hosts_client_id" in (t.get("set_fact") or {}))
    assert "default('skpatch-hosts'" in facts["sksso_skpatch_hosts_client_id"]
