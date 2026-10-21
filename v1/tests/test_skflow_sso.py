"""skflow (Windmill) single sign-on through sksso (Authentik), as code.

Authentik side: sksso renders a blueprint for the skflow-windmill OAuth2
provider, application and group binding when sksso.SKFLOW_OIDC_CLIENT_SECRET
is set (and nothing changes when it is not); the client secret is never in the
blueprint, only the !Env reference. Windmill side: skflow's provisioner sets
the `oauths` global setting and the workspace admins, idempotently, against a
fake Windmill API; the Ansible task keeps the secrets on stdin."""
import http.server
import json
import pathlib
import subprocess
import sys
import threading

import jinja2
import pytest
import yaml

OPT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
SKSSO = OPT / "sksso"
SKFLOW = OPT / "skflow"
PROVISIONER = SKFLOW / "src/skflow/sso_provision.py"
SECRET = "skflow-oidc-client-secret-0123456789abcdef"
ADMIN_SECRET = "windmill-superadmin-secret-0123456789abcdef"


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
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    return j


def _blueprint(**over):
    ctx = dict(sksso_skflow_client_id="skflow-windmill", sksso_skflow_group="skflow-operators",
               sksso_skflow_host="skflow.example.com", sksso_skflow_app_name="Windmill (skflow)")
    ctx.update(over)
    text = (SKSSO / "src/config/sksso/blueprints/skflow-windmill.yaml.j2").read_text()
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

def test_blueprint_describes_the_provider_application_and_binding():
    text, bp = _blueprint()
    entries = {e["model"]: e for e in bp["entries"]}
    prov = entries["authentik_providers_oauth2.oauth2provider"]
    assert prov["identifiers"] == {"name": "skflow-windmill"}
    a = prov["attrs"]
    assert a["client_type"] == "confidential" and a["client_id"] == "skflow-windmill"
    assert a["sub_mode"] == "hashed_user_id"
    assert a["redirect_uris"] == [{"matching_mode": "strict",
                                   "url": "https://skflow.example.com/user/login_callback/authentik"}]
    assert a["authorization_flow"][1] == ["authentik_flows.flow", ["slug", "default-provider-authorization-implicit-consent"]]
    assert a["invalidation_flow"][1][1] == ["slug", "default-provider-invalidation-flow"]
    assert a["signing_key"][1][1] == ["name", "authentik Self-signed Certificate"]
    scopes = sorted(m[1][1][1].rsplit("-", 1)[1] for m in a["property_mappings"])
    assert scopes == ["email", "offline_access", "openid", "profile"]
    app = entries["authentik_core.application"]
    assert app["identifiers"] == {"slug": "skflow-windmill"}
    assert app["attrs"]["name"] == "Windmill (skflow)"
    assert app["attrs"]["meta_launch_url"] == "https://skflow.example.com/user/login"
    bind = entries["authentik_policies.policybinding"]
    assert bind["identifiers"]["target"][0] == "!KeyOf" and bind["identifiers"]["group"][1] == "operators-group"
    assert entries["authentik_core.group"]["identifiers"] == {"name": "skflow-operators"}


def test_blueprint_never_carries_the_secret_only_an_env_reference():
    text, bp = _blueprint()
    prov = next(e for e in bp["entries"] if e["model"].endswith("oauth2provider"))
    assert prov["attrs"]["client_secret"] == ("!Env", "SKFLOW_OIDC_CLIENT_SECRET")
    assert SECRET not in text


def test_blueprint_follows_the_vault_overrides():
    _, bp = _blueprint(sksso_skflow_client_id="wm", sksso_skflow_group="ops",
                       sksso_skflow_host="flow.example.org", sksso_skflow_app_name="Flow")
    prov = next(e for e in bp["entries"] if e["model"].endswith("oauth2provider"))
    assert prov["attrs"]["redirect_uris"][0]["url"] == "https://flow.example.org/user/login_callback/authentik"


def test_compose_mounts_blueprints_only_when_enabled():
    for svc in ("server", "worker"):
        off = _compose()[svc]["volumes"]
        assert not any("/blueprints/" in v for v in off)
        on = _compose(SKFLOW_OIDC_CLIENT_SECRET=SECRET)[svc]["volumes"]
        assert "/var/data/config/sksso-prod/blueprints:/blueprints/custom:ro" in on


def test_env_file_carries_the_secret_only_when_enabled():
    tpl = (SKSSO / "src/config/sksso/sksso.env.j2").read_text()
    base = {"postgres_user": "u", "postgres_password": "p", "authentik_secret_key": "k",
            "CLUSTERNAME": "c", "DOMAIN": "example.com"}
    off = _jenv().from_string(tpl).render(env="prod", sksso=base)
    assert "SKFLOW_OIDC_CLIENT_SECRET=" not in off
    on = _jenv().from_string(tpl).render(env="prod", sksso=dict(base, SKFLOW_OIDC_CLIENT_SECRET=SECRET))
    assert f"\nSKFLOW_OIDC_CLIENT_SECRET={SECRET}\n" in on


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_sksso_playbooks_render_the_blueprint_before_the_compose(env):
    tasks = [t for p in yaml.safe_load((SKSSO / f"deploy_sksso-{env}.yml").read_text()) for t in (p.get("tasks") or [])]
    names = [t.get("name", "") for t in tasks]
    i = next(i for i, t in enumerate(tasks) if _inc(t) == "tasks/skflow_blueprint.yml")
    assert tasks[i]["when"] == "sksso.SKFLOW_OIDC_CLIENT_SECRET | default('', true) | string | length > 0"
    assert i < names.index("Process {{ app }} Docker Compose template") < names.index("Deploy {{ app }} stack")


def test_blueprint_task_refuses_a_short_secret():
    assert_task = next(t for t in _tasks("sksso", "skflow_blueprint.yml") if "assert" in t)
    assert any(">= 32" in c for c in assert_task["assert"]["that"])


# ------------------------------------------------------------------ Windmill

class FakeWindmill(http.server.BaseHTTPRequestHandler):
    state = None

    def log_message(self, *a):
        pass

    def _send(self, code, body=None):
        raw = b"" if body is None else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _auth(self):
        if self.headers.get("Authorization") != "Bearer " + ADMIN_SECRET:
            self._send(401, "no")
            return False
        return True

    def do_GET(self):
        st = FakeWindmill.state
        if not self._auth():
            return
        if self.path == "/api/settings/global/oauths":
            return self._send(200, st["oauths"])
        if self.path.startswith("/api/w/") and self.path.endswith("/users/list"):
            ws = self.path.split("/")[3]
            if ws not in st["users"]:
                return self._send(404, "no workspace")
            return self._send(200, st["users"][ws])
        self._send(404)

    def do_POST(self):
        st = FakeWindmill.state
        if not self._auth():
            return
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        st["posts"].append((self.path, body))
        if self.path == "/api/settings/global/oauths":
            st["oauths"] = body["value"]
        elif self.path.endswith("/workspaces/add_user"):
            ws = self.path.split("/")[3]
            st["users"][ws].append({"email": body["email"], "username": body["email"].split("@")[0],
                                    "is_admin": body["is_admin"], "operator": body["operator"]})
        elif "/users/update/" in self.path:
            ws, name = self.path.split("/")[3], self.path.rsplit("/", 1)[1]
            for u in st["users"][ws]:
                if u["username"] == name:
                    u.update(body)
        self._send(200, "ok")


@pytest.fixture
def windmill():
    FakeWindmill.state = {"oauths": None, "users": {"fwe": [
        {"email": "old@example.com", "username": "old", "is_admin": False, "operator": True}]}, "posts": []}
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeWindmill)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", FakeWindmill.state
    srv.shutdown()


OAUTHS = {"authentik": {"id": "skflow-windmill", "secret": SECRET, "display_name": "SKWorld SSO",
                        "connect_config": {"auth_url": "https://sso.example.com/application/o/authorize/",
                                           "token_url": "https://sso.example.com/application/o/token/",
                                           "scopes": ["openid", "offline_access"]},
                        "login_config": {"auth_url": "https://sso.example.com/application/o/authorize/",
                                         "token_url": "https://sso.example.com/application/o/token/",
                                         "userinfo_url": "https://sso.example.com/application/o/userinfo/",
                                         "scopes": ["openid", "offline_access", "email", "profile"]}}}


def _run(url, admins=None, secret=ADMIN_SECRET):
    cfg = {"url": url, "superadmin_secret": secret, "oauths": OAUTHS,
           "workspace_admins": admins if admins is not None else {"fwe": ["new@example.com", "old@example.com"]}}
    return subprocess.run([sys.executable, "-I", str(PROVISIONER)], input=json.dumps(cfg),
                          capture_output=True, text=True, timeout=60)


def test_creates_the_setting_and_admins_then_is_idempotent(windmill):
    url, st = windmill
    r = _run(url)
    assert r.returncode == 0, r.stderr
    assert st["oauths"] == OAUTHS
    assert ("/api/w/fwe/workspaces/add_user", {"email": "new@example.com", "is_admin": True, "operator": False}) in st["posts"]
    assert ("/api/w/fwe/users/update/old", {"is_admin": True, "operator": False}) in st["posts"]
    assert "oauths: created" in r.stdout
    n = len(st["posts"])
    r2 = _run(url)
    assert r2.returncode == 0 and len(st["posts"]) == n
    assert "oauths: unchanged" in r2.stdout and "added" not in r2.stdout


def test_updates_a_drifted_setting(windmill):
    url, st = windmill
    st["oauths"] = {"authentik": {"id": "other"}}
    assert _run(url, admins={}).returncode == 0
    assert st["oauths"] == OAUTHS


def test_missing_workspace_is_an_error_after_the_setting_is_applied(windmill):
    url, st = windmill
    r = _run(url, admins={"nope": ["a@example.com"]})
    assert r.returncode == 1 and "nope" in r.stderr
    assert st["oauths"] == OAUTHS


def test_wrong_superadmin_secret_fails_and_input_errors_exit_2(windmill):
    url, _ = windmill
    assert _run(url, secret="wrong").returncode == 1
    bad = subprocess.run([sys.executable, "-I", str(PROVISIONER)], input="{}", capture_output=True, text=True)
    assert bad.returncode == 2


def test_never_prints_a_secret(windmill):
    url, _ = windmill
    for r in (_run(url), _run(url, secret="wrong")):
        assert SECRET not in r.stdout + r.stderr and ADMIN_SECRET not in r.stdout + r.stderr


def test_task_keeps_secrets_on_stdin_and_out_of_logs():
    tasks = {t["name"]: t for t in _tasks("skflow", "sso_provision.yml")}
    run = tasks["Set the Windmill Authentik login and the workspace admins"]
    assert run["no_log"] is True and run["command"]["stdin"] == "{{ sf_sso_input | to_json }}"
    assert "secret" not in " ".join(run["command"]["argv"])
    assert "@sha256:" in " ".join(run["command"]["argv"])
    assert tasks["Build the SSO provisioner input"]["no_log"] is True


def test_task_builds_the_oauths_value_windmill_expects():
    facts = next(t for t in _tasks("skflow", "sso_provision.yml") if t.get("name") == "Build the SSO provisioner input")
    raw = facts["set_fact"]["sf_sso_input"]["oauths"]["authentik"]
    ctx = {"skflow": {"SSO_CLIENT_SECRET": SECRET, "SUPERADMIN_SECRET": ADMIN_SECRET}, "sf_sso_host": "sso.example.com"}

    # the vault supplies defaults through default(), which the strict env handles
    j = _jenv(False)
    ctx_render = lambda s: j.from_string(s).render(**ctx)  # noqa: E731
    got = json.loads(json.dumps(raw), object_hook=lambda d: {k: (ctx_render(x) if isinstance(x, str) else x) for k, x in d.items()})
    assert got == OAUTHS["authentik"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_skflow_playbooks_provision_after_the_deploy_and_resolve_the_sso_host(env):
    plays = yaml.safe_load((SKFLOW / f"deploy_skflow-{env}.yml").read_text())
    tasks = [t for p in plays for t in (p.get("tasks") or [])]
    names = [t.get("name", "") for t in tasks]
    i = next(i for i, t in enumerate(tasks) if _inc(t) == "tasks/sso_provision.yml")
    assert i > names.index("Deploy {{ app }} stack")
    assert tasks[i]["when"] == "skflow.SSO_CLIENT_SECRET | default('', true) | string | length > 0"
    pre = [t for p in plays for t in (p.get("pre_tasks") or [])]
    facts = next(t["set_fact"] for t in pre if "sf_sso_host" in (t.get("set_fact") or {}))
    assert "'sso' + ('' if env == 'prod' else '-' + env) + '.' + sf_base_domain" in facts["sf_sso_host"]
