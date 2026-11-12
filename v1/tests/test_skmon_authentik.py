"""skmon Authentik: Grafana OIDC (admin-only), forward auth on vmui and Alertmanager, the provisioner."""
import io
import json
import sys

import pytest

from skmon_support import (BASE, CFG, GUARDS, PLAYS, ROLE, ansible_environment, eval_asserts, guard_facts,
                           load_tasks, render, services, task_named)

sys.path.insert(0, str(ROLE / "src/skmon"))
import authentik_provision as ap  # noqa: E402

AUTHENTIK_TASKS = ROLE / "tasks/authentik.yml"
MARKER = "SECRET-MARKER"
PRIORITY = {"OTHER-PROVIDER": 4242}


def env_lines(**kw):
    return render(CFG / "skmon.env.j2", **kw).splitlines()


def middlewares(svc):
    (line,) = [l for l in svc["deploy"]["labels"] if ".middlewares=" in l]
    return line.split(".middlewares=")[1].split(",")


# --- Grafana env and secret ------------------------------------------------------------------

def test_role_mapping_admin_only():
    env = env_lines()
    assert "GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH=contains(groups[*], 'skadmin') && 'GrafanaAdmin' || ''" in env
    assert "GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_STRICT=true" in env
    assert "GF_AUTH_GENERIC_OAUTH_ALLOW_ASSIGN_GRAFANA_ADMIN=true" in env
    assert "GF_AUTH_GENERIC_OAUTH_ENABLED=true" in env
    assert "GF_AUTH_DISABLE_LOGIN_FORM=false" in env and "GF_USERS_ALLOW_SIGN_UP=false" in env


def test_oidc_endpoints_come_from_authentik_host():
    env = env_lines()
    assert "GF_AUTH_GENERIC_OAUTH_AUTH_URL=https://sso.c1.example.test/application/o/authorize/" in env
    assert "GF_AUTH_GENERIC_OAUTH_TOKEN_URL=https://sso.c1.example.test/application/o/token/" in env
    assert "GF_AUTH_GENERIC_OAUTH_API_URL=https://sso.c1.example.test/application/o/userinfo/" in env
    assert "GF_AUTH_GENERIC_OAUTH_SCOPES=openid profile email" in env
    assert f"GF_AUTH_GENERIC_OAUTH_CLIENT_ID={BASE['GRAFANA_OIDC_CLIENT_ID']}" in env
    assert "GF_SERVER_ROOT_URL=https://skmon.c1.example.test" in env


def test_root_url_defaults_to_the_router_host():
    kw = {"GRAFANA_ROOT_URL": ""}
    assert "GF_SERVER_ROOT_URL=https://skmon.c1.example.test" in env_lines(**kw)
    assert "GF_SERVER_ROOT_URL=https://skmon-dev.c1.example.test" in render(CFG / "skmon.env.j2", env="dev", **kw).splitlines()


def test_client_secret_is_a_file_not_env():
    env = env_lines()
    assert not any(l.startswith("GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET=") for l in env)
    assert "GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET__FILE=/run/secrets/grafana_oidc_client_secret" in env
    assert MARKER not in "\n".join(env)


def test_client_secret_docker_secret_is_hash_named_and_mounted():
    svc = services()["grafana"]
    (m,) = [s for s in svc["secrets"] if s["source"] == "grafana_oidc_client_secret"]
    assert m["target"] == "grafana_oidc_client_secret" and str(m["mode"]) in ("0o400", "256", "400")
    from skmon_support import compose
    sec = compose()["secrets"]["grafana_oidc_client_secret"]
    assert sec["name"].startswith("skmon_prod_grafana_oidc_client_secret_") and len(sec["name"].rsplit("_", 1)[1]) == 12
    assert sec["file"] == "/var/data/config/skmon-prod/secrets/grafana_oidc_client_secret"
    assert render(CFG / "grafana_oidc_client_secret.j2").strip() == BASE["GRAFANA_OIDC_CLIENT_SECRET"]


def test_provision_off_still_renders_oidc_env():
    assert "GF_AUTH_GENERIC_OAUTH_ENABLED=true" in env_lines(AUTHENTIK_PROVISION=False)


# --- routers ---------------------------------------------------------------------------------

def test_grafana_has_no_edge_auth_middleware():
    mws = middlewares(services()["grafana"])
    assert mws == ["default-security-headers@file", "gzip@file"]


def test_forward_auth_on_vmui_and_alertmanager_not_basic_auth():
    for n in ("victoriametrics", "alertmanager"):
        mws = middlewares(services()[n])
        assert "authentik@file" in mws and not any("traefikAuth" in m for m in mws), n
        custom = middlewares(services(FORWARD_AUTH_MIDDLEWARE="sso@file")[n])
        assert "sso@file" in custom and "authentik@file" not in custom, n


def test_no_basic_auth_anywhere_in_skmon():
    labels = [l for s in services().values() for l in s.get("deploy", {}).get("labels", [])]
    assert not any("traefikAuth" in l or "basicauth" in l.lower() for l in labels)
    assert "GF_AUTH_BASIC_ENABLED" not in render(CFG / "skmon.env.j2")


# --- guards ----------------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["GRAFANA_OIDC_CLIENT_ID", "GRAFANA_OIDC_CLIENT_SECRET"])
@pytest.mark.parametrize("val", ["short", "x" * 31, ""])
def test_short_oidc_credentials_fail_the_play(key, val):
    msgs = eval_asserts(GUARDS, guard_facts(**{key: val}))
    assert any("GRAFANA_OIDC" in m for m in msgs)
    assert val not in "".join(msgs) or val == ""


def test_32_char_credentials_pass():
    assert eval_asserts(GUARDS, guard_facts(GRAFANA_OIDC_CLIENT_SECRET="x" * 32, GRAFANA_OIDC_CLIENT_ID="y" * 32)) == []


def test_authentik_host_required_only_when_provisioning():
    assert any("AUTHENTIK_HOST" in m for m in eval_asserts(GUARDS, guard_facts(AUTHENTIK_HOST="")))
    assert any("AUTHENTIK_HOST" in m for m in eval_asserts(GUARDS, guard_facts(AUTHENTIK_HOST="", AUTHENTIK_PROVISION=True)))
    assert eval_asserts(GUARDS, guard_facts(AUTHENTIK_HOST="", AUTHENTIK_PROVISION=False)) == []
    assert eval_asserts(GUARDS, guard_facts(AUTHENTIK_HOST="", AUTHENTIK_PROVISION="false")) == []


def test_guards_skipped_when_parked():
    msgs = eval_asserts(GUARDS, guard_facts(replicas=0, GRAFANA_OIDC_CLIENT_SECRET="", GRAFANA_OIDC_CLIENT_ID="", AUTHENTIK_HOST=""))
    assert not any("GRAFANA_OIDC" in m or "AUTHENTIK_HOST" in m for m in msgs)


# --- plays ------------------------------------------------------------------------------------

def test_every_play_runs_the_provisioner_unless_parked_or_off():
    for p in PLAYS:
        t = task_named(p, "Create or update the skmon providers and applications in Authentik")
        assert t["include_tasks"].endswith("tasks/authentik.yml")
        assert "not skmon_parked" in t["when"] and "AUTHENTIK_PROVISION" in t["when"] and "default(true)" in t["when"]
        sec = task_named(p, "Render the {{ app }} Docker secret contents")
        assert "grafana_oidc_client_secret" in sec["set_fact"]["skmon_secrets"]


def _deep(jenv, value, ctx):
    from skmon_support import _eval_value
    if isinstance(value, dict):
        return {k: _deep(jenv, v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_deep(jenv, v, ctx) for v in value]
    return _eval_value(jenv, value, ctx)


def provisioner_input(env="prod", **skmon):
    ctx = {**guard_facts(env=env, **skmon), "vault_file_path": "/v/skmon-" + env + "_vault.yml", "app": "skmon", "env": env,
           "skmon_sksso_token_vault": {"sksso": {"api_token": "T" * 40}}}
    jenv = ansible_environment()
    for t in load_tasks(AUTHENTIK_TASKS):
        if "set_fact" in t and "skmon_authentik_" in next(iter(t["set_fact"])):
            for k, v in t["set_fact"].items():
                ctx[k] = _deep(jenv, v, ctx)
    return ctx["skmon_authentik_input"]


def test_provisioner_input_matches_the_router_hosts():
    inp = provisioner_input()
    assert inp["group"] == "skadmin" and inp["host"] == "sso.c1.example.test" and inp["token"] == "T" * 40
    assert inp["url"] == "http://sksso-prod_server:9000"
    apps = {a["slug"]: a for a in inp["apps"]}
    assert set(apps) == {"skmon", "vmui", "alertmanager"}
    assert apps["skmon"]["redirect_uris"] == ["https://skmon.c1.example.test/login/generic_oauth"]
    assert apps["skmon"]["client_secret"] == BASE["GRAFANA_OIDC_CLIENT_SECRET"]
    svcs = services()
    for slug, svc in (("vmui", "victoriametrics"), ("alertmanager", "alertmanager")):
        rule = [l for l in svcs[svc]["deploy"]["labels"] if ".rule=" in l][0]
        assert f"Host(`{apps[slug]['external_host'].removeprefix('https://')}`)" in rule
        assert apps[slug]["launch_url"] == apps[slug]["external_host"]


def test_provisioner_input_env_suffix_and_pyroscope():
    inp = provisioner_input(env="dev", PROFILING={"enabled": True})
    assert {a["slug"] for a in inp["apps"]} == {"skmon-dev", "vmui-dev", "alertmanager-dev", "pyroscope-dev"}
    assert inp["url"] == "http://sksso-dev_server:9000"
    assert not any(a["slug"].startswith("pyroscope") for a in provisioner_input(PROFILING={"enabled": False})["apps"])


def test_provisioner_task_keeps_secrets_off_argv_and_out_of_logs():
    tasks = load_tasks(AUTHENTIK_TASKS)
    run = next(t for t in tasks if "command" in t)
    assert "stdin" in run["command"] and run.get("no_log") is True
    assert not any("TOKEN" in a.upper() or "SECRET" in a.upper() for a in run["command"]["argv"] if isinstance(a, str) and "{{" in a)
    for t in tasks:
        text = json.dumps(t)
        if any(w in text for w in ("skmon_sksso_token_vault", "GRAFANA_OIDC_CLIENT_SECRET", "skmon_authentik_input")) and "assert" not in t:
            assert t.get("no_log") is True or "debug" in t or "fail" in t, t.get("name")
    image = next(a for a in run["command"]["argv"] if "python:" in a)
    import yaml
    pin = yaml.safe_load((CFG / "pins.yml").read_text())["images"]["python_slim"]
    assert f"{pin['repo']}:{pin['tag']}@{pin['digest']}" in image


# --- the provisioner, against an in-process fake Authentik --------------------------------------

class FakeAuthentik:
    """Enough of the Authentik admin API for the provisioner; records what it was asked to write."""

    def __init__(self, version="2026.8.5"):
        self.version = version
        self.writes = 0
        self.apps, self.providers, self.groups = {}, {}, {"skadmin": "grp-skadmin"}
        self._app_pk, self._rows = {}, []
        self._next = 100
        self.outpost = {"pk": "outpost-1", "managed": ap.EMBEDDED_OUTPOST, "type": "proxy",
                        "providers": [PRIORITY["OTHER-PROVIDER"]]}

    # views the tests assert on
    @property
    def bindings(self):
        by_pk = {v: k for k, v in self.groups.items()}
        slug = {pk: s for s, pk in self._app_pk.items()}
        return {slug[r["target"]]: by_pk[r["group"]] for r in self._rows}

    @property
    def outpost_providers(self):
        return list(self.outpost["providers"])

    def _provider(self, kind, name):
        return next((p for p in self.providers.values() if p["type"] == kind and p["name"] == name), None)

    def get(self, path, **q):
        if path == "/admin/version/":
            return {"version_current": self.version}
        return {"results": self._results(path, q)}

    def first(self, path, **q):
        res = self.get(path, **q)["results"]
        return res[0] if res else None

    def _results(self, path, q):
        if path == "/flows/instances/":
            return [{"pk": "flow-" + q["slug"]}]
        if path.startswith("/propertymappings/"):
            return [{"pk": "pm-" + q.get("scope_name", q.get("managed", "")).rsplit("-", 1)[-1]}]
        if path == "/crypto/certificatekeypairs/":
            return [{"pk": "keypair-1"}]
        if path in ("/providers/oauth2/", "/providers/proxy/"):
            p = self._provider(path.split("/")[2].replace("oauth2", "oauth2"), q["name"])
            return [dict(p)] if p else []
        if path == "/core/groups/":
            return [{"pk": self.groups[q["name"]], "name": q["name"]}] if q["name"] in self.groups else []
        if path == "/policies/bindings/":
            return [dict(r) for r in self._rows if r["target"] == q["target"]]
        if path == "/outposts/instances/":
            return [dict(self.outpost)] if q.get("managed") == self.outpost["managed"] else []
        raise AssertionError(f"unexpected GET {path} {q}")

    def call(self, method, path, body=None, ok=None):
        if method == "GET":
            slug = path.removeprefix("/core/applications/").rstrip("/")
            if slug not in self.apps:
                raise ap.ApiError(f"GET {path}: HTTP 404")
            return dict(self.apps[slug])
        self.writes += 1
        if path in ("/providers/oauth2/", "/providers/proxy/") and method == "POST":
            self._next += 1
            kind = path.split("/")[2]
            self.providers[body["name"]] = {"pk": self._next, "type": kind, **body}
            return dict(self.providers[body["name"]])
        if path.startswith("/providers/") and method == "PATCH":
            pk = int(path.rstrip("/").rsplit("/", 1)[1])
            p = next(p for p in self.providers.values() if p["pk"] == pk)
            p.update(body)
            return dict(p)
        if path == "/core/applications/" and method == "POST":
            self.apps[body["slug"]] = {"pk": "app-" + body["slug"], **body}
            self._app_pk[body["slug"]] = "app-" + body["slug"]
            return dict(self.apps[body["slug"]])
        if path.startswith("/core/applications/") and method == "PATCH":
            self.apps[path.rstrip("/").rsplit("/", 1)[1]].update(body)
            return {}
        if path == "/policies/bindings/" and method == "POST":
            assert not any(r["target"] == body["target"] and r["group"] == body["group"] for r in self._rows), "duplicate binding"
            self._rows.append(dict(body))
            return dict(body)
        if path.startswith("/outposts/instances/") and method == "PATCH":
            self.outpost.update(body)
            return dict(self.outpost)
        raise AssertionError(f"unexpected {method} {path}")


def input_for(profiling=True, **over):
    apps = [
        {"slug": "skmon", "name": "SKMon (Grafana)", "kind": "oidc", "launch_url": "https://skmon.c1.example.test",
         "redirect_uris": ["https://skmon.c1.example.test/login/generic_oauth"],
         "client_id": "c" * 32, "client_secret": MARKER + "-" + "s" * 20},
        {"slug": "vmui", "name": "VictoriaMetrics", "kind": "proxy", "launch_url": "https://vmui.c1.example.test",
         "external_host": "https://vmui.c1.example.test"},
        {"slug": "alertmanager", "name": "Alertmanager", "kind": "proxy", "launch_url": "https://alertmanager.c1.example.test",
         "external_host": "https://alertmanager.c1.example.test"},
    ]
    if profiling:
        apps.append({"slug": "pyroscope", "name": "Pyroscope", "kind": "proxy", "launch_url": "https://pyroscope.c1.example.test",
                     "external_host": "https://pyroscope.c1.example.test"})
    return {"host": "sso.c1.example.test", "token": "T" * 40, "group": "skadmin", "apps": apps, **over}


def run_provisioner(fake, cfg):
    ap.provision(fake, cfg)


def test_provisioner_objects():
    fake = FakeAuthentik()
    run_provisioner(fake, input_for(profiling=False))
    assert set(fake.apps) == {"skmon", "vmui", "alertmanager"}
    assert fake.bindings == {s: "skadmin" for s in fake.apps}
    assert fake.providers["skmon"]["type"] == "oauth2" and fake.providers["vmui"]["type"] == "proxy"
    assert fake.providers["vmui"]["mode"] == "forward_single"
    assert fake.providers["vmui"]["external_host"] == "https://vmui.c1.example.test"
    assert fake.providers["skmon"]["client_id"] == "c" * 32
    assert fake.apps["vmui"]["provider"] == fake.providers["vmui"]["pk"]
    assert fake.apps["vmui"]["policy_engine_mode"] == "any"


def test_pyroscope_only_when_given():
    fake = FakeAuthentik()
    run_provisioner(fake, input_for())
    assert "pyroscope" in fake.apps and fake.providers["pyroscope"]["type"] == "proxy"


def test_provisioner_is_idempotent():
    fake = FakeAuthentik()
    run_provisioner(fake, input_for())
    n = fake.writes
    assert n > 0
    run_provisioner(fake, input_for())
    assert fake.writes == n


def test_drift_is_repaired_with_a_patch():
    fake = FakeAuthentik()
    run_provisioner(fake, input_for())
    fake.providers["vmui"]["external_host"] = "https://wrong.example.test"
    n = fake.writes
    run_provisioner(fake, input_for())
    assert fake.writes == n + 1 and fake.providers["vmui"]["external_host"] == "https://vmui.c1.example.test"


def test_embedded_outpost_gains_proxy_providers_and_keeps_others():
    fake = FakeAuthentik()
    run_provisioner(fake, input_for())
    proxies = {fake.providers[s]["pk"] for s in ("vmui", "alertmanager", "pyroscope")}
    assert set(fake.outpost_providers) == proxies | {PRIORITY["OTHER-PROVIDER"]}
    assert fake.providers["skmon"]["pk"] not in fake.outpost_providers
    before = fake.outpost_providers
    writes = fake.writes
    run_provisioner(fake, input_for())
    assert fake.outpost_providers == before and fake.writes == writes


def test_outpost_attach_is_incremental():
    fake = FakeAuthentik()
    run_provisioner(fake, input_for(profiling=False))
    run_provisioner(fake, input_for())
    assert fake.providers["pyroscope"]["pk"] in fake.outpost_providers and PRIORITY["OTHER-PROVIDER"] in fake.outpost_providers
    assert len(fake.outpost_providers) == 4


def test_missing_embedded_outpost_is_an_error():
    fake = FakeAuthentik()
    fake.outpost["managed"] = "something-else"
    with pytest.raises(ap.ApiError, match="embedded outpost"):
        run_provisioner(fake, input_for())


def test_missing_group_is_an_error_and_never_created():
    fake = FakeAuthentik()
    fake.groups.clear()
    with pytest.raises(ap.ApiError, match="skadmin"):
        run_provisioner(fake, input_for())
    assert fake.groups == {}


def test_existing_bindings_are_left_alone():
    fake = FakeAuthentik()
    run_provisioner(fake, input_for())
    fake._rows.append({"target": "app-vmui", "group": "grp-other", "order": 5})
    n = fake.writes
    run_provisioner(fake, input_for())
    assert fake.writes == n and len(fake._rows) == len(fake.apps) + 1


def test_provisioner_never_prints_secret(capsys, monkeypatch):
    fake = FakeAuthentik()
    run_provisioner(fake, input_for())
    run_provisioner(fake, input_for())
    fake.providers["skmon"]["client_secret"] = "stale"
    run_provisioner(fake, input_for())
    out = capsys.readouterr()
    assert MARKER not in out.out + out.err and "T" * 40 not in out.out + out.err
    assert "client_secret (secret)" in out.out


def test_main_reads_stdin_and_exits_zero_without_leaking(capsys, monkeypatch):
    fake = FakeAuthentik()
    monkeypatch.setattr(ap, "Api", lambda url, host, token: fake)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(input_for())))
    assert ap.main() == 0
    out = capsys.readouterr()
    assert MARKER not in out.out + out.err
    assert len([l for l in out.out.splitlines() if l]) >= 8


def test_main_rejects_incomplete_input_by_name_only(capsys, monkeypatch):
    bad = input_for()
    del bad["apps"][0]["client_secret"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(bad)))
    assert ap.main() == 2
    err = capsys.readouterr().err
    assert "apps[0].client_secret" in err and MARKER not in err


def test_main_reports_api_errors_with_exit_one(capsys, monkeypatch):
    fake = FakeAuthentik()
    fake.groups.clear()
    monkeypatch.setattr(ap, "Api", lambda url, host, token: fake)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(input_for())))
    assert ap.main() == 1
    assert MARKER not in capsys.readouterr().err
