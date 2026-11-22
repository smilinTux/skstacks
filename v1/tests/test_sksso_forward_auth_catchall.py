"""The domain-wide forward-auth catch-all behind `authentik@file`, as code in sksso.

The blueprint (src/config/sksso/blueprints/forward-auth-catchall.yaml.j2) creates ONE
`forward_domain` Proxy Provider, its Application and a policy binding to the admin group
ONLY; the provider is attached to the embedded outpost additively by
src/sksso/forward_auth_attach.py (a blueprint outpost entry would replace the outpost's
provider list). ON by default; FORWARD_AUTH_ENABLED: false opts out; the name knobs adopt a
hand-made catch-all.

Three layers, as for the skgit OIDC tests:
- the blueprint template, rendered and parsed (shape, the one binding, adoption identifiers);
- the whole sksso deploy through REAL Ansible (render/render_playbooks.py) with a given vault:
  default on, opt-out, adoption names, the cookie domain derivation and override, refusals;
- the attach script, run against a fake Authentik ORM (additive, idempotent, fail closed).
"""
import json
import re
import runpy
import shutil
import sys
import types
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent / "render"))
import render_playbooks as rp  # noqa: E402
from test_compose_templates_schema import make_environment  # noqa: E402

SKSSO = rp.ANSIBLE / "optional" / "sksso"
BLUEPRINT = SKSSO / "src/config/sksso/blueprints/forward-auth-catchall.yaml.j2"
ENVS = ("dev", "staging", "prod")
BASE = yaml.safe_load((rp.VARS / "sksso.example.yml").read_text())["sksso"]
KNOBS = ["FORWARD_AUTH_ENABLED", "FORWARD_AUTH_PROVIDER_NAME", "FORWARD_AUTH_APP_SLUG", "FORWARD_AUTH_APP_NAME",
         "FORWARD_AUTH_SSO_HOST", "FORWARD_AUTH_COOKIE_DOMAIN", "FORWARD_AUTH_ADMIN_GROUP", "FORWARD_AUTH_OUTPOST"]

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


class _Loader(yaml.SafeLoader):
    pass


for _tag in ("Find", "KeyOf", "Env"):
    _Loader.add_constructor("!" + _tag, lambda loader, node: (node.tag, loader.construct_sequence(node, deep=True)
                                                              if isinstance(node, yaml.SequenceNode)
                                                              else loader.construct_scalar(node)))


# ----------------------------------------------------------- the blueprint template

CTX = dict(sksso_fa_provider_name="SKStacks forward auth (domain)", sksso_fa_app_slug="skstacks-forward-auth",
           sksso_fa_app_name="SKStacks forward auth (admin-only apps)", sksso_fa_admin_group="skadmin",
           sksso_fa_external_host="https://sso.example.test", sksso_fa_cookie_domain="example.test",
           sksso_fa_invalidation_flow=True)


def _blueprint(**over):
    text = BLUEPRINT.read_text()
    out = make_environment().from_string(text).render(**{**CTX, **over})
    return text, yaml.load(out, _Loader)


def _by_model(bp):
    out = {}
    for e in bp["entries"]:
        out.setdefault(e["model"], []).append(e)
    return out


def test_blueprint_is_one_provider_one_application_one_binding_and_the_group():
    _, bp = _blueprint()
    m = _by_model(bp)
    assert sorted(m) == ["authentik_core.application", "authentik_core.group",
                         "authentik_policies.policybinding", "authentik_providers_proxy.proxyprovider"]
    assert all(len(v) == 1 for v in m.values())
    prov = m["authentik_providers_proxy.proxyprovider"][0]
    assert prov["identifiers"] == {"name": "SKStacks forward auth (domain)"}
    a = prov["attrs"]
    assert a["mode"] == "forward_domain"
    assert a["external_host"] == "https://sso.example.test"
    assert a["cookie_domain"] == "example.test"
    assert a["authorization_flow"] == ("!Find", ["authentik_flows.flow", ["slug", "default-provider-authorization-implicit-consent"]])
    assert a["invalidation_flow"] == ("!Find", ["authentik_flows.flow", ["slug", "default-provider-invalidation-flow"]])
    app = m["authentik_core.application"][0]
    assert app["identifiers"] == {"slug": "skstacks-forward-auth"}
    assert app["attrs"]["provider"] == ("!KeyOf", "provider")
    assert app["attrs"]["name"] == "SKStacks forward auth (admin-only apps)"


def test_the_binding_is_to_the_admin_group_only_and_never_widens():
    _, bp = _blueprint()
    m = _by_model(bp)
    # the only binding, on the application, to the group entry (a KeyOf of the admin group)
    (binding,) = m["authentik_policies.policybinding"]
    assert binding["identifiers"] == {"target": ("!KeyOf", "application"), "group": ("!KeyOf", "admin-group"), "order": 0}
    assert binding["attrs"] == {"enabled": True, "negate": False, "timeout": 30}
    (group,) = m["authentik_core.group"]
    assert group["id"] == "admin-group" and group["identifiers"] == {"name": "skadmin"}
    # existence only: no attrs, so an existing group's members and flags are never touched
    assert "attrs" not in group
    # no user or policy binding, no "any authenticated user" path
    assert "user" not in binding["identifiers"] and "policy" not in binding["identifiers"]
    assert "sso-allow-any-account" not in BLUEPRINT.read_text() and "sso-allow-public" not in BLUEPRINT.read_text()
    # adoption never widens: policy_engine_mode (and anything unlisted) is left as it is
    assert "policy_engine_mode" not in m["authentik_core.application"][0]["attrs"]


def test_a_custom_admin_group_is_the_only_one_bound():
    _, bp = _blueprint(sksso_fa_admin_group="ops-admins")
    m = _by_model(bp)
    assert m["authentik_core.group"][0]["identifiers"] == {"name": "ops-admins"}
    assert len(m["authentik_policies.policybinding"]) == 1


def test_adoption_names_become_the_identifiers_so_nothing_is_duplicated():
    _, bp = _blueprint(sksso_fa_provider_name="Domain wide (made by hand)", sksso_fa_app_slug="domain-gate")
    m = _by_model(bp)
    assert m["authentik_providers_proxy.proxyprovider"][0]["identifiers"] == {"name": "Domain wide (made by hand)"}
    assert m["authentik_core.application"][0]["identifiers"] == {"slug": "domain-gate"}


def test_names_with_quotes_stay_valid_yaml():
    _, bp = _blueprint(sksso_fa_provider_name='Dom "main" gate', sksso_fa_app_name="O'Brien: gate")
    m = _by_model(bp)
    assert m["authentik_providers_proxy.proxyprovider"][0]["identifiers"]["name"] == 'Dom "main" gate'
    assert m["authentik_core.application"][0]["attrs"]["name"] == "O'Brien: gate"


def test_invalidation_flow_is_left_out_for_an_authentik_that_predates_it():
    _, bp = _blueprint(sksso_fa_invalidation_flow=False)
    assert "invalidation_flow" not in _by_model(bp)["authentik_providers_proxy.proxyprovider"][0]["attrs"]
    _, bp = _blueprint(sksso_fa_invalidation_flow=True)
    assert "invalidation_flow" in _by_model(bp)["authentik_providers_proxy.proxyprovider"][0]["attrs"]


def test_the_invalidation_flow_gate_is_authentik_2024_10_not_2024_8():
    # invalidation_flow first exists in Authentik 2024.10 (authentik/core/models.py at
    # version/2024.8.0 has none, at version/2024.10.0 it does)
    task = next(t for t in yaml.safe_load((SKSSO / "tasks/forward_auth_blueprint.yml").read_text())
                if t.get("name") == "Decide whether the provider carries an invalidation flow")
    expr = task["set_fact"]["sksso_fa_invalidation_flow"]
    assert "version('2024.10', '>=')" in expr and "2024.8" not in expr


def test_the_outpost_is_not_a_blueprint_entry_because_a_blueprint_replaces_its_providers():
    text, bp = _blueprint()
    assert not any(e["model"].startswith("authentik_outposts") for e in bp["entries"])
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    assert "outpost" not in code.lower()


def test_the_blueprint_carries_no_secret():
    code = re.sub(r"(?m)^\s*#.*$", "", BLUEPRINT.read_text())
    assert "!Env" not in code and "secret" not in code.lower() and "token" not in code.lower()


# --------------------------------------------------------- the deploy, real Ansible

def render(tmp: Path, vault: dict, env: str = "prod") -> tuple[dict, Path]:
    # one private copy of the playbook tree per module (the copy and the task
    # rewrite are the slow part); every render gets its own run dir inside it
    work = tmp / "work"
    if not (work / "ansible").exists():
        shutil.copytree(rp.ANSIBLE, work / "ansible")
        for task_file in (work / "ansible").glob("**/tasks/*.yml"):
            if task_file.name != "select_vault_file.yml":
                rp.rewrite_task_file(task_file)
    vars_dir = tmp / "vars"
    vars_dir.mkdir(exist_ok=True)
    (vars_dir / "sksso.example.yml").write_text(yaml.safe_dump({"sksso": vault}))
    run_dir = work / f"sksso-{env}-base"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    saved, saved_contract = rp.VARS, rp.contract_findings
    rp.VARS = vars_dir
    # the README vault-key contract is the render gate's job (and is asserted for
    # these knobs below); these vaults carry other roles' keys on purpose
    rp.contract_findings = lambda *a, **k: []
    try:
        pb = work / "ansible" / "optional" / "sksso" / f"deploy_sksso-{env}.yml"
        res = rp.run_one(work, "optional", "sksso", env, pb, "base")
    finally:
        rp.VARS, rp.contract_findings = saved, saved_contract
    res["log"] = (run_dir / "ansible.log").read_text() if (run_dir / "ansible.log").exists() else ""
    # keep the output after the next render reuses the run dir name
    keep = tmp / f"out{next(_n)}"
    if (run_dir / "out").exists():
        shutil.copytree(run_dir / "out", keep)
    return res, keep


@pytest.fixture(scope="module")
def tmp_mod(tmp_path_factory):
    return tmp_path_factory.mktemp("sksso-fa")


_n = iter(range(10_000))


def ok(tmp_mod, over=None, env="prod", drop=()):
    vault = {k: v for k, v in (BASE | (over or {})).items() if k not in drop}
    res, out = render(tmp_mod, vault, env)
    assert res["status"] == "PASS", (res["findings"], res["log"][-3000:])
    return out


def fails(tmp_mod, over, env="prod"):
    res, _ = render(tmp_mod, BASE | over, env)
    assert res["status"] == "FAIL", "the deploy rendered although it must refuse this vault"
    return res["log"]


def bp_path(out, env="prod"):
    return out / f"var/data/config/sksso-{env}/blueprints/forward-auth-catchall.yaml"


def provider(out, env="prod"):
    bp = yaml.load(bp_path(out, env).read_text(), _Loader)
    return _by_model(bp)["authentik_providers_proxy.proxyprovider"][0]


def application(out, env="prod"):
    bp = yaml.load(bp_path(out, env).read_text(), _Loader)
    return _by_model(bp)["authentik_core.application"][0]


def server_volumes(out, env="prod"):
    doc = yaml.safe_load((out / f"var/data/config/sksso-{env}/sksso.yml").read_text())
    return {svc: doc["services"][svc]["volumes"] for svc in ("server", "worker")}


MOUNT = "/var/data/config/sksso-prod/blueprints:/blueprints/custom:ro"


@needs_ansible
def test_a_fresh_install_gets_the_catch_all_without_setting_anything(tmp_mod):
    # the example vault sets none of the knobs: that is a fresh install
    assert not any(k in BASE for k in KNOBS)
    out = ok(tmp_mod)
    assert bp_path(out).exists()
    p = provider(out)
    assert p["identifiers"] == {"name": "SKStacks forward auth (domain)"}
    assert p["attrs"]["mode"] == "forward_domain"
    assert p["attrs"]["external_host"] == "https://sso.render.example.test"
    assert application(out)["identifiers"] == {"slug": "skstacks-forward-auth"}
    for vols in server_volumes(out).values():
        assert MOUNT in vols
    bp = yaml.load(bp_path(out).read_text(), _Loader)
    m = _by_model(bp)
    assert m["authentik_core.group"][0]["identifiers"] == {"name": "skadmin"}
    assert len(m["authentik_policies.policybinding"]) == 1


@needs_ansible
@pytest.mark.parametrize("env", ENVS)
def test_every_env_renders_it_with_its_own_sso_host(tmp_mod, env):
    out = ok(tmp_mod, env=env)
    host = "sso.render.example.test" if env == "prod" else f"sso-{env}.render.example.test"
    assert provider(out, env)["attrs"]["external_host"] == f"https://{host}"
    # the cookie domain is the cluster domain, a parent of the sso host and of the app hosts
    assert provider(out, env)["attrs"]["cookie_domain"] == "render.example.test"


@needs_ansible
@pytest.mark.parametrize("flag", [False, "no", 0])
def test_opt_out_renders_nothing_and_mounts_nothing(tmp_mod, flag):
    out = ok(tmp_mod, {"FORWARD_AUTH_ENABLED": flag})
    assert not bp_path(out).exists()
    for vols in server_volumes(out).values():
        assert not any("/blueprints/" in v for v in vols)
    # the rest of the role is unaffected: still a full render
    assert (out / "var/data/config/sksso-prod/sksso.env").exists()


def _compose_volumes(**sksso):
    out = make_environment().from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env="prod", app="sksso", sksso={"CLUSTERNAME": "c", "DOMAIN": "example.test", **sksso})
    doc = yaml.safe_load(out)["services"]
    return {svc: doc[svc]["volumes"] for svc in ("server", "worker")}


def test_the_blueprints_mount_follows_the_flag_and_stays_for_another_roles_blueprint():
    for vols in _compose_volumes().values():  # default: on
        assert MOUNT in vols
    for vols in _compose_volumes(FORWARD_AUTH_ENABLED="no").values():
        assert not any("/blueprints/" in v for v in vols)
    for vols in _compose_volumes(FORWARD_AUTH_ENABLED=False, SKGIT_OIDC_ENABLED=True).values():
        assert MOUNT in vols


@needs_ansible
def test_the_blueprints_directory_is_created_whatever_else_is_on(tmp_mod):
    # the mount is on by default, so the playbook must create its source up front
    for env in ENVS:
        assert f"/var/data/config/{{{{ app }}}}-{{{{ env }}}}/blueprints" in (SKSSO / f"deploy_sksso-{env}.yml").read_text()


@needs_ansible
@pytest.mark.parametrize("flag", [True, "yes"])
def test_explicit_on_is_the_same_as_the_default(tmp_mod, flag):
    out = ok(tmp_mod, {"FORWARD_AUTH_ENABLED": flag})
    assert bp_path(out).exists()


@needs_ansible
def test_adoption_names_set_the_identifiers_of_the_hand_made_objects(tmp_mod):
    out = ok(tmp_mod, {"FORWARD_AUTH_PROVIDER_NAME": "Domain-level forward auth",
                       "FORWARD_AUTH_APP_SLUG": "domain-catch-all",
                       "FORWARD_AUTH_APP_NAME": "Domain catch-all"})
    assert provider(out)["identifiers"] == {"name": "Domain-level forward auth"}
    assert application(out)["identifiers"] == {"slug": "domain-catch-all"}
    assert application(out)["attrs"]["name"] == "Domain catch-all"


@needs_ansible
def test_cookie_domain_is_the_cluster_domain_or_the_bare_domain_behind_cloudflared(tmp_mod):
    assert provider(ok(tmp_mod))["attrs"]["cookie_domain"] == "render.example.test"
    cf = ok(tmp_mod, {"CLOUDFLARED": True})
    assert provider(cf)["attrs"]["cookie_domain"] == "example.test"
    assert provider(cf)["attrs"]["external_host"] == "https://sso.example.test"


@needs_ansible
def test_cookie_domain_and_host_can_be_overridden(tmp_mod):
    out = ok(tmp_mod, {"FORWARD_AUTH_COOKIE_DOMAIN": "Example.Test", "FORWARD_AUTH_SSO_HOST": "login.example.test"})
    assert provider(out)["attrs"]["cookie_domain"] == "example.test"  # lowercased
    assert provider(out)["attrs"]["external_host"] == "https://login.example.test"


@needs_ansible
def test_the_capauth_sso_host_is_honoured_when_no_forward_auth_host_is_set(tmp_mod):
    out = ok(tmp_mod, {"CAPAUTH_SSO_HOST": "id.render.example.test"})
    assert provider(out)["attrs"]["external_host"] == "https://id.render.example.test"
    out = ok(tmp_mod, {"CAPAUTH_SSO_HOST": "id.render.example.test", "FORWARD_AUTH_SSO_HOST": "sso2.render.example.test"})
    assert provider(out)["attrs"]["external_host"] == "https://sso2.render.example.test"


@needs_ansible
def test_the_admin_group_is_skadmin_unless_a_vault_knob_says_otherwise(tmp_mod):
    bp = yaml.load(bp_path(ok(tmp_mod)).read_text(), _Loader)
    assert _by_model(bp)["authentik_core.group"][0]["identifiers"] == {"name": "skadmin"}
    bp = yaml.load(bp_path(ok(tmp_mod, {"FORWARD_AUTH_ADMIN_GROUP": "ops-admins"})).read_text(), _Loader)
    m = _by_model(bp)
    assert m["authentik_core.group"][0]["identifiers"] == {"name": "ops-admins"}
    assert len(m["authentik_policies.policybinding"]) == 1


@needs_ansible
def test_invalidation_flow_follows_the_authentik_version_the_role_pins(tmp_mod):
    # the role's default image is 2024.4.2, which has no invalidation_flow field,
    # and neither has any 2024.8.x (it first exists in 2024.10)
    assert "invalidation_flow" not in provider(ok(tmp_mod))["attrs"]
    for old in ("2024.6.3", "2024.8.0", "2024.8.4", "2024.9.1"):
        assert "invalidation_flow" not in provider(ok(tmp_mod, {"authentik_version": old}))["attrs"], old
    assert "invalidation_flow" not in provider(ok(tmp_mod, {"IMAGE": "registry.example.test/authentik/server:2024.8.2"}))["attrs"]
    for over in ({"authentik_version": "2024.10.0"}, {"authentik_version": "2025.10.1"},
                 {"IMAGE": "registry.example.test/authentik/server:2024.10.0"},
                 {"IMAGE": "registry.example.test/authentik/server:2026.2.1"},
                 {"IMAGE": "registry.example.test/authentik/server@sha256:" + "0" * 64}):
        assert "invalidation_flow" in provider(ok(tmp_mod, over))["attrs"], over
    assert "invalidation_flow" not in provider(ok(tmp_mod, {"IMAGE": "registry.example.test/authentik/server:2024.4.2"}))["attrs"]


@needs_ansible
def test_the_rendered_blueprint_file_is_not_secret_bearing(tmp_mod):
    out = ok(tmp_mod, {"api_token": "t" * 40})
    assert "t" * 40 not in bp_path(out).read_text()


# ---- refusals ------------------------------------------------------------------

@needs_ansible
@pytest.mark.parametrize("knob,value", [
    ("FORWARD_AUTH_PROVIDER_NAME", "Hand made"),
    ("FORWARD_AUTH_APP_SLUG", "hand-made"),
    ("FORWARD_AUTH_APP_NAME", "Hand made"),
    ("FORWARD_AUTH_SSO_HOST", "sso.example.test"),
    ("FORWARD_AUTH_COOKIE_DOMAIN", "example.test"),
    ("FORWARD_AUTH_ADMIN_GROUP", "skadmin"),
    ("FORWARD_AUTH_OUTPOST", "authentik Embedded Outpost"),
])
def test_opting_out_while_adopting_or_overriding_is_contradictory(tmp_mod, knob, value):
    log = fails(tmp_mod, {"FORWARD_AUTH_ENABLED": False, knob: value})
    assert "contradicts the adopt/override" in log and knob in log


@needs_ansible
@pytest.mark.parametrize("junk", ["maybe", "disabled", "2", "tru"])
def test_an_unreadable_enabled_flag_is_refused_not_read_as_off(tmp_mod, junk):
    log = fails(tmp_mod, {"FORWARD_AUTH_ENABLED": junk})
    assert "FORWARD_AUTH_ENABLED must be true or false" in log


@needs_ansible
@pytest.mark.parametrize("over", [
    {"FORWARD_AUTH_COOKIE_DOMAIN": "other.example.test"},          # the sso host is not under it
    {"FORWARD_AUTH_COOKIE_DOMAIN": "localhost"},                    # a single label
    {"FORWARD_AUTH_COOKIE_DOMAIN": ".render.example.test"},         # a leading dot
    {"FORWARD_AUTH_COOKIE_DOMAIN": "sub.sso.render.example.test"},  # deeper than the sso host
    {"FORWARD_AUTH_SSO_HOST": "sso.elsewhere.test"},                # host outside the derived cookie domain
    {"FORWARD_AUTH_SSO_HOST": "sso.render.example.test/x"},         # not a host
    {"FORWARD_AUTH_APP_SLUG": "Not A Slug"},
    {"FORWARD_AUTH_PROVIDER_NAME": " leading space"},
    {"FORWARD_AUTH_APP_NAME": "bad\nname"},
    {"FORWARD_AUTH_ADMIN_GROUP": "skuser"},                         # the every-user baseline group
    {"FORWARD_AUTH_ADMIN_GROUP": "SkUser"},                         # case does not get around it
    {"FORWARD_AUTH_ADMIN_GROUP": "bad/group"},
])
def test_unusable_settings_fail_the_play_before_anything_is_rendered(tmp_mod, over):
    log = fails(tmp_mod, over)
    assert "forward-auth catch-all settings are unusable" in log


@needs_ansible
def test_the_app_slug_may_not_collide_with_the_skgit_sign_in_application(tmp_mod):
    skgit = {"SKGIT_OIDC_ENABLED": True, "SKGIT_OIDC_CLIENT_SECRET": "s" * 40}
    log = fails(tmp_mod, {**skgit, "FORWARD_AUTH_APP_SLUG": "skgit"})
    assert "unusable" in log
    ok(tmp_mod, {**skgit, "FORWARD_AUTH_APP_SLUG": "skgit-gate"})
    ok(tmp_mod, {"FORWARD_AUTH_APP_SLUG": "skgit"})  # skgit sign-in off: no collision


# --------------------------------------------------------- the playbooks, the three envs

def _play(env):
    doc = yaml.safe_load((SKSSO / f"deploy_sksso-{env}.yml").read_text())
    return next(p for p in doc if isinstance(p, dict) and p.get("hosts") == "selected_manager_group")


@pytest.mark.parametrize("env", ENVS)
def test_the_blueprint_task_always_runs_and_the_attach_runs_only_when_on(env):
    tasks = _play(env)["tasks"]
    names = [t["name"] for t in tasks]
    bp = next(t for t in tasks if t["name"].startswith("Forward-auth catch-all blueprint"))
    assert bp["include_tasks"]["file"] == "tasks/forward_auth_blueprint.yml"
    assert "when" not in bp  # it must run on opt-out too: the refusals and the stale-file cleanup live in it
    assert set(bp["tags"]) == {"templates", "deploy"}
    att = next(t for t in tasks if t["name"].startswith("Attach the forward-auth catch-all provider"))
    assert att["include_tasks"]["file"] == "tasks/forward_auth_attach.yml"
    assert att["when"] == "sksso_fa_on | bool"
    # blueprint rendered before the stack deploy, attach after it (the worker must be up)
    deploy = names.index("Deploy {{ app }} stack")
    assert names.index(bp["name"]) < deploy < names.index(att["name"])


def test_the_three_envs_carry_identical_forward_auth_tasks():
    def pick(env):
        return json.loads(json.dumps([t for t in _play(env)["tasks"] if "forward-auth" in t["name"].lower()])
                          .replace(f"-{env}", "-ENV"))
    ref = pick("prod")
    assert len(ref) == 2 and pick("dev") == ref and pick("staging") == ref


def test_the_attach_does_not_need_an_api_token_so_it_works_on_a_new_cluster():
    text = (SKSSO / "tasks/forward_auth_attach.yml").read_text()
    code = re.sub(r"(?m)^\s*#.*$", "", text)
    assert "api_token" not in code and "Bearer" not in code
    assert "ak shell" in code and "forward_auth_attach.py" in code


def test_the_attach_stops_waiting_on_a_refusal_and_fails_the_deploy_unless_ok():
    tasks = yaml.safe_load((SKSSO / "tasks/forward_auth_attach.yml").read_text())
    run = next(t for t in tasks if t["name"].startswith("Attach the forward-auth provider"))
    assert run["until"] == "'attach: waiting' not in sksso_fa_attach.stdout"
    fail = next(t for t in tasks if "fail" in t)
    assert fail["when"] == "'attach: ok' not in (sksso_fa_attach.stdout | default(''))"


# ------------------------------------------------ the preflight: before anything is rendered

BP_TASKS = SKSSO / "tasks/forward_auth_blueprint.yml"
PREFLIGHT = SKSSO / "src/sksso/forward_auth_preflight.py"


def _bp_tasks():
    return yaml.safe_load(BP_TASKS.read_text())


def _idx(tasks, prefix):
    return next(i for i, t in enumerate(tasks) if t["name"].startswith(prefix))


def test_the_preflight_runs_before_the_blueprint_directory_and_the_render():
    tasks = _bp_tasks()
    names = [t["name"] for t in tasks]
    render_i = _idx(tasks, "Render the forward-auth-catchall blueprint")
    mkdir_i = _idx(tasks, "Create the Authentik blueprints directory")
    pf = [i for i, n in enumerate(names) if "preflight" in n.lower()]
    assert len(pf) >= 5
    # every preflight task, the stale-file removal and the fail sit ahead of the render
    assert max(pf) < mkdir_i < render_i
    # and after the settings were checked, so the provider name it reads is a valid one
    assert _idx(tasks, "Check the forward-auth catch-all settings") < min(pf)


def test_the_preflight_is_skipped_when_the_catch_all_is_off():
    tasks = _bp_tasks()
    for prefix in ("Find the node running this stack's Authentik server (forward-auth preflight)",
                   "Name the preflight node", "Refuse an existing domain-wide provider"):
        assert "sksso_fa_on | bool" in tasks[_idx(tasks, prefix)]["when"], prefix
    decide = tasks[_idx(tasks, "Decide whether the preflight lets the blueprint through")]
    assert decide["set_fact"]["sksso_fa_pf_blocked"].startswith("{{ (sksso_fa_on | bool) and")


def test_the_preflight_reads_through_ak_shell_in_the_server_container_with_no_token():
    tasks = _bp_tasks()
    run = tasks[_idx(tasks, "Refuse an existing domain-wide provider")]
    body = run["shell"]
    assert "_server" in body and "ak shell" in body and "docker exec" in body
    assert run["args"]["stdin"].count("forward_auth_preflight.py") == 1
    assert run["until"] == "'preflight: waiting' not in sksso_fa_pf.stdout"  # a refusal is never retried
    code = re.sub(r"(?m)^\s*#.*$", "", BP_TASKS.read_text() + PREFLIGHT.read_text())
    assert "api_token" not in code and "Bearer" not in code


def test_no_server_running_yet_skips_the_preflight_and_a_refusal_takes_the_stale_blueprint_away():
    tasks = _bp_tasks()
    # first install: the service does not exist (docker rc != 0) or has no running task: empty host,
    # the run is skipped and nothing blocks
    name = tasks[_idx(tasks, "Name the preflight node")]
    assert "else ''" in name["set_fact"]["sksso_fa_pf_host"]
    run = tasks[_idx(tasks, "Refuse an existing domain-wide provider")]
    assert "length) > 0" in run["when"]
    assert tasks[_idx(tasks, "Find the node running this stack's Authentik server (forward-auth preflight)")]["failed_when"] is False
    # a refusal removes the rendered blueprint BEFORE it fails, so the worker cannot apply a stale one
    rm = tasks[_idx(tasks, "Take an earlier rendering of the blueprint away when the preflight refuses")]
    assert rm["file"]["state"] == "absent" and rm["file"]["path"].endswith("/blueprints/forward-auth-catchall.yaml")
    assert rm["when"] == "sksso_fa_pf_blocked | bool"
    fail = tasks[_idx(tasks, "Fail before rendering when the preflight did not pass")]
    assert fail["when"] == "sksso_fa_pf_blocked | bool"
    assert _idx(tasks, "Take an earlier rendering") < _idx(tasks, "Fail before rendering")


def _blocked(host, stdout, on=True):
    """Evaluate the `sksso_fa_pf_blocked` expression the way Ansible would (its `bool` filter)."""
    import jinja2
    env = jinja2.Environment()
    env.filters["bool"] = lambda v: str(v).lower() in ("true", "yes", "on", "1")
    expr = _bp_tasks()[_idx(_bp_tasks(), "Decide whether the preflight lets the blueprint through")]["set_fact"]["sksso_fa_pf_blocked"]
    ctx = {"sksso_fa_on": on, "sksso_fa_pf": {"stdout": stdout} if stdout is not None else jinja2.Undefined()}
    if host is not None:
        ctx["sksso_fa_pf_host"] = host
    return env.from_string(expr).render(**ctx).strip() == "True"


@pytest.mark.parametrize("host,stdout,blocked", [
    ("", None, False),                                   # first install: no server, skipped
    ("node-1", "preflight: ok (no other domain-wide provider overlaps cookie domain 'x.test')", False),
    ("node-1", "preflight: skipped (no sksso-prod_server container on this node)", False),
    ("node-1", "preflight: refused (Authentik already has a domain-wide provider ...)", True),
    ("node-1", "Traceback (most recent call last):\nValueError: boom", True),   # unreadable: fail closed
    ("node-1", "preflight: waiting (ak shell printed no status yet)", True),    # never answered in time
])
def test_the_preflight_verdict(host, stdout, blocked):
    assert _blocked(host, stdout) is blocked
    assert _blocked(host, stdout, on=False) is False  # opted out: never blocks


@needs_ansible
def test_the_preflight_command_is_rendered_only_when_the_catch_all_is_on(tmp_mod):
    # the render gate stands in a running server on a node (FAKE_STDOUT) that answers ok
    def pf_shells(out):
        return [p for p in (out / "_shell").glob("*.sh") if "preflight: " in p.read_text()] \
            if (out / "_shell").exists() else []
    on = ok(tmp_mod)
    assert bp_path(on).exists()
    (script,) = pf_shells(on) if len(pf_shells(on)) == 1 else (None,)
    assert script is not None, "the preflight command was not rendered with the catch-all on"
    text = script.read_text()
    assert "sksso-prod_server" in text and "ak shell" in text
    off = ok(tmp_mod, {"FORWARD_AUTH_ENABLED": False})
    assert not bp_path(off).exists() and pf_shells(off) == []


def test_every_knob_is_documented_in_the_readme_and_the_sop():
    readme, sop = (SKSSO / "README.md").read_text(), (SKSSO / "SOP.md").read_text()
    used = set()
    for f in [*SKSSO.glob("tasks/forward_auth_*.yml"), BLUEPRINT, SKSSO / "src/config/sksso/sksso.yml.j2"]:
        used |= set(re.findall(r"sksso\.(FORWARD_AUTH_[A-Z_]+)", f.read_text()))
    # the knobs set into the override scan are quoted strings, not sksso.X attribute reads
    used |= set(re.findall(r"'(FORWARD_AUTH_[A-Z_]+)'", (SKSSO / "tasks/forward_auth_blueprint.yml").read_text()))
    assert used == set(KNOBS), used ^ set(KNOBS)
    for knob in KNOBS:
        assert knob in readme, f"{knob} not in the README"
        assert knob in sop, f"{knob} not in the SOP"


def test_the_render_gate_finds_every_knob_documented_in_the_readme_vault_block():
    assert rp.contract_findings("sksso", "optional", {"sksso": {k: "x" for k in KNOBS}}) == []
    assert rp.contract_findings("sksso", "optional", {"sksso": {"FORWARD_AUTH_NOT_A_KNOB": "x"}}) != []


def test_the_set_pass_vault_exercises_the_knobs_the_readme_documents():
    set_vars = yaml.safe_load((rp.VARS / "sksso.set.yml").read_text())["sksso"]
    assert {k for k in set_vars if k.startswith("FORWARD_AUTH_")} >= {"FORWARD_AUTH_PROVIDER_NAME", "FORWARD_AUTH_COOKIE_DOMAIN"}


# --------------------------------------------------------- the attach script, fake ORM

ATTACH = SKSSO / "src/sksso/forward_auth_attach.py"


class QS(list):
    """The slice of a Django queryset the script uses."""

    def first(self):
        return self[0] if self else None

    def exists(self):
        return bool(self)

    def count(self):
        return len(self)

    def exclude(self, **kw):
        return QS(o for o in self if not _match(o, kw))

    def filter(self, **kw):
        return QS(o for o in self if _match(o, kw))


def _match(obj, kw):
    for key, val in kw.items():
        if key.endswith("__iexact"):
            if str(getattr(obj, key[:-8]) or "").lower() != str(val).lower():
                return False
        elif getattr(obj, key) != val:
            return False
    return True


class Manager:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, **kw):
        return QS(o for o in self.rows if _match(o, kw))


class Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def __repr__(self):
        return f"<{self.__dict__.get('name') or self.__dict__.get('slug')}>"


class Outpost(Obj):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.provs = list(kw.get("provs", []))
        self.saved = 0

    @property
    def providers(self):
        outer = self

        class M:
            def filter(self, **kw):
                return QS(p for p in outer.provs if _match(p, kw))

            def add(self, p):
                if p not in outer.provs:
                    outer.provs.append(p)

            def count(self):
                return len(outer.provs)
        return M()

    def save(self):
        self.saved += 1


def make_world(*, provider_kw=None, bindings=None, twins=(), outpost_kw=None, with_app=True, with_provider=True):
    group = lambda n: Obj(name=n)
    prov = Obj(**{"pk": 1, "name": "SKStacks forward auth (domain)", "mode": "forward_domain",
                  "cookie_domain": "example.test", **(provider_kw or {})})
    app = Obj(pk=10, slug="skstacks-forward-auth", provider=prov)
    admin = Obj(name="skadmin")
    default_bindings = [Obj(target=app, enabled=True, negate=False, group=admin, user=None, policy=None)]
    if bindings is not None:
        default_bindings = [Obj(target=app, enabled=b.get("enabled", True), negate=b.get("negate", False),
                                group=group(b["group"]) if b.get("group") else None,
                                user=Obj(username=b["user"]) if b.get("user") else None,
                                policy=Obj(name=b["policy"]) if b.get("policy") else None) for b in bindings]
    other = Obj(pk=2, name="skpages single", mode="forward_single", cookie_domain="")
    outpost = Outpost(**{"name": "authentik Embedded Outpost", "type": "proxy", "provs": [other], **(outpost_kw or {})})
    return {
        "providers": ([prov] if with_provider else []) + [other] + list(twins),
        "apps": [app] if with_app else [],
        "bindings": default_bindings,
        "outposts": [outpost],
        "prov": prov, "other": other, "outpost": outpost,
    }


def _fake_authentik(world, monkeypatch):
    def module(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        monkeypatch.setitem(sys.modules, name, m)
        return m

    class ProxyProvider:
        objects = Manager(world["providers"])

    class Application:
        objects = Manager(world["apps"])

    class PolicyBinding:
        objects = Manager(world["bindings"])

    class OutpostM:
        objects = Manager(world["outposts"])

    for name in ("authentik", "authentik.core", "authentik.outposts", "authentik.policies", "authentik.providers",
                 "authentik.providers.proxy"):
        module(name)
    module("authentik.core.models", Application=Application)
    module("authentik.outposts.models", Outpost=OutpostM)
    module("authentik.policies.models", PolicyBinding=PolicyBinding)
    module("authentik.providers.proxy.models", ProxyProvider=ProxyProvider)


def run_attach(world, monkeypatch, capsys, **cfg):
    _fake_authentik(world, monkeypatch)
    conf = {"provider": "SKStacks forward auth (domain)", "outpost": "authentik Embedded Outpost",
            "admin_group": "skadmin", "cookie_domain": "example.test", **cfg}
    monkeypatch.setenv("SKSSO_FORWARD_AUTH", json.dumps(conf))
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(ATTACH))
    assert exc.value.code == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("attach:")]
    assert len(lines) == 1, lines
    return lines[0]


def test_attach_adds_the_provider_and_keeps_the_ones_already_on_the_outpost(monkeypatch, capsys):
    w = make_world()
    line = run_attach(w, monkeypatch, capsys)
    assert line.startswith("attach: ok (attached to outpost") and "2 provider(s)" in line
    assert w["outpost"].provs == [w["other"], w["prov"]]  # additive: the per-app provider is still there
    assert w["outpost"].saved == 1  # the running outpost is told to reload


def test_attach_is_idempotent(monkeypatch, capsys):
    w = make_world()
    run_attach(w, monkeypatch, capsys)
    line = run_attach(w, monkeypatch, capsys)
    assert line.startswith("attach: ok (already on outpost")
    assert w["outpost"].provs == [w["other"], w["prov"]] and w["outpost"].saved == 1


@pytest.mark.parametrize("kw", [
    {"with_provider": False},
    {"provider_kw": {"mode": "forward_single"}},
    {"provider_kw": {"cookie_domain": "other.test"}},
    {"with_app": False},
    {"bindings": []},
    {"bindings": [{"group": "skuser"}]},
])
def test_attach_waits_for_the_blueprint_to_land(monkeypatch, capsys, kw):
    w = make_world(**kw)
    assert run_attach(w, monkeypatch, capsys).startswith("attach: waiting")
    assert w["outpost"].provs == [w["other"]]


@pytest.mark.parametrize("bindings,what", [
    ([{"group": "skadmin"}, {"group": "community"}], "group 'community'"),
    ([{"group": "skadmin"}, {"user": "alice"}], "user 'alice'"),
    ([{"group": "skadmin"}, {"policy": "sso-allow-any-account"}], "policy 'sso-allow-any-account'"),
    ([{"group": "skadmin"}, {"group": "authentik Admins"}], "group 'authentik Admins'"),
    ([{"group": "skadmin"}, {"group": "skuser", "negate": True}], "group 'skuser' (negated)"),
])
def test_attach_refuses_a_catch_all_gated_by_more_than_the_admin_group(monkeypatch, capsys, bindings, what):
    w = make_world(bindings=bindings)
    line = run_attach(w, monkeypatch, capsys)
    assert line.startswith("attach: refused") and "more than the admin group" in line and what in line
    assert w["outpost"].provs == [w["other"]] and w["outpost"].saved == 0


def test_attach_ignores_a_disabled_extra_binding(monkeypatch, capsys):
    w = make_world(bindings=[{"group": "skadmin"}, {"group": "community", "enabled": False}])
    assert run_attach(w, monkeypatch, capsys).startswith("attach: ok")


def test_attach_refuses_a_second_domain_wide_provider_for_the_same_cookie_domain(monkeypatch, capsys):
    twin = Obj(pk=3, name="hand made catch-all", mode="forward_domain", cookie_domain="Example.Test")
    w = make_world(twins=[twin])
    line = run_attach(w, monkeypatch, capsys)
    assert line.startswith("attach: refused") and "another domain-wide provider" in line and "hand made catch-all" in line
    assert "FORWARD_AUTH_PROVIDER_NAME" in line and "FORWARD_AUTH_ENABLED: false" in line
    assert w["outpost"].provs == [w["other"]]


def test_attach_allows_a_domain_wide_provider_for_a_different_cookie_domain(monkeypatch, capsys):
    twin = Obj(pk=3, name="other estate", mode="forward_domain", cookie_domain="elsewhere.test")
    assert run_attach(make_world(twins=[twin]), monkeypatch, capsys).startswith("attach: ok")


def test_attach_refuses_an_unknown_or_non_proxy_outpost(monkeypatch, capsys):
    w = make_world()
    assert run_attach(w, monkeypatch, capsys, outpost="no such outpost").startswith("attach: refused (no outpost named")
    w = make_world(outpost_kw={"type": "ldap"})
    assert run_attach(w, monkeypatch, capsys).startswith("attach: refused (outpost")


def test_attach_reads_no_token_and_no_secret():
    code = re.sub(r"(?m)^\s*#.*$", "", ATTACH.read_text())
    assert "api_token" not in code and "password" not in code.lower() and "Bearer" not in code


# --------------------------------------------------------- the preflight script, fake ORM

def run_preflight(world, monkeypatch, capsys, **cfg):
    _fake_authentik(world, monkeypatch)
    conf = {"provider": "SKStacks forward auth (domain)", "cookie_domain": "example.test", **cfg}
    monkeypatch.setenv("SKSSO_FORWARD_AUTH", json.dumps(conf))
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(str(PREFLIGHT))
    assert exc.value.code == 0
    lines = [l for l in capsys.readouterr().out.splitlines() if l.startswith("preflight:")]
    assert len(lines) == 1, lines
    return lines[0]


def _hand_made(world, name="hand made catch-all", cookie="example.test", slug="hand-made-gate", pk=30):
    prov = Obj(pk=pk, name=name, mode="forward_domain", cookie_domain=cookie)
    world["providers"].append(prov)
    if slug:
        world["apps"].append(Obj(pk=pk + 100, slug=slug, provider=prov))
    return prov


def test_preflight_refuses_an_existing_catch_all_under_another_name(monkeypatch, capsys):
    w = make_world(with_provider=False)  # nothing under the configured name yet: the blueprint would create it
    _hand_made(w)
    line = run_preflight(w, monkeypatch, capsys)
    assert line.startswith("preflight: refused")
    assert "'hand made catch-all'" in line and "'hand-made-gate'" in line  # names the provider and its application
    assert "FORWARD_AUTH_PROVIDER_NAME" in line and "FORWARD_AUTH_APP_SLUG" in line and "FORWARD_AUTH_ENABLED: false" in line
    assert "'SKStacks forward auth (domain)'" in line  # the configured name that would have been created


def test_preflight_passes_when_the_only_catch_all_is_the_configured_one(monkeypatch, capsys):
    assert run_preflight(make_world(), monkeypatch, capsys).startswith("preflight: ok")
    # adoption: the vault names the hand-made one, so it is the configured one
    w = make_world(with_provider=False)
    _hand_made(w)
    assert run_preflight(w, monkeypatch, capsys, provider="hand made catch-all").startswith("preflight: ok")


def test_preflight_passes_when_there_is_no_catch_all_at_all(monkeypatch, capsys):
    w = make_world(with_provider=False)  # only a forward_single provider exists
    assert run_preflight(w, monkeypatch, capsys).startswith("preflight: ok")


def test_preflight_refuses_a_twin_beside_the_configured_provider_too(monkeypatch, capsys):
    w = make_world()
    _hand_made(w, cookie="Example.Test")
    assert run_preflight(w, monkeypatch, capsys).startswith("preflight: refused")


@pytest.mark.parametrize("cookie,refused", [
    ("example.test", True), ("EXAMPLE.TEST", True),
    ("test", True), ("sub.example.test", True),         # a parent or a subdomain of the configured domain overlaps
    ("", True), (None, True),                            # unknown cookie domain: cannot be told from a duplicate
    ("elsewhere.test", False), ("badexample.test", False),   # unrelated domains are not duplicates
])
def test_preflight_judges_the_cookie_domain_overlap(monkeypatch, capsys, cookie, refused):
    w = make_world(with_provider=False)
    _hand_made(w, cookie=cookie)
    line = run_preflight(w, monkeypatch, capsys)
    assert line.startswith("preflight: refused" if refused else "preflight: ok"), line


def test_preflight_names_a_provider_without_an_application(monkeypatch, capsys):
    w = make_world(with_provider=False)
    _hand_made(w, slug=None)
    assert "no application" in run_preflight(w, monkeypatch, capsys)


def test_preflight_is_read_only(monkeypatch, capsys):
    w = make_world(with_provider=False)
    twin = _hand_made(w)
    before = ([dict(o.__dict__) for o in w["providers"]], [dict(o.__dict__) for o in w["apps"]],
              list(w["outpost"].provs), w["outpost"].saved)
    run_preflight(w, monkeypatch, capsys)
    assert before == ([dict(o.__dict__) for o in w["providers"]], [dict(o.__dict__) for o in w["apps"]],
                      list(w["outpost"].provs), w["outpost"].saved) and twin in w["providers"]
    code = re.sub(r"(?m)^\s*#.*$", "", PREFLIGHT.read_text())
    assert not re.search(r"\.(save|delete|add|create|update|remove|set)\(|get_or_create|update_or_create", code)
    assert "api_token" not in code and "password" not in code.lower() and "Bearer" not in code
