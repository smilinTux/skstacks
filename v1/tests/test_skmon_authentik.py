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
FOREIGN_PROVIDER_PK = 4242


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


def test_authentik_host_required_whenever_unparked():
    for prov in (None, True, False, "false"):
        kw = {} if prov is None else {"AUTHENTIK_PROVISION": prov}
        assert any("AUTHENTIK_HOST" in m for m in eval_asserts(GUARDS, guard_facts(AUTHENTIK_HOST="", **kw))), prov
        assert not any("AUTHENTIK_HOST" in m for m in eval_asserts(GUARDS, guard_facts(**kw))), prov


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
    """Enough of the Authentik admin API for the provisioner, behaving like the real one where it
    matters: a query parameter the viewset does not declare is IGNORED (everything is returned),
    and lists are paginated (page size 2). Records what it was asked to write."""

    PAGE = 2
    pages = ap.Api.pages  # the real client paging logic runs against this fake
    # Filters the real viewsets declare (upstream filterset_fields), per list endpoint.
    FILTERS = {
        "/providers/proxy/": {"name__iexact"},
        "/providers/oauth2/": {"name"},
        "/flows/instances/": {"slug"},
        "/propertymappings/provider/scope/": {"managed", "scope_name"},
        "/crypto/certificatekeypairs/": {"name", "has_key"},
        "/core/groups/": {"name"},
        "/policies/bindings/": {"target"},
        "/outposts/instances/": {"managed__iexact", "managed__icontains"},
    }

    def __init__(self, version="2026.8.5"):
        self.version = version
        self.writes = 0
        self.deleted = []
        self.apps, self.providers, self.groups = {}, {}, {"skadmin": "grp-skadmin"}
        self._app_pk, self._rows = {}, []
        self._next = 100
        self.outposts = [{"pk": "outpost-1", "managed": ap.EMBEDDED_OUTPOST, "type": "proxy",
                          "providers": [FOREIGN_PROVIDER_PK]}]

    @property
    def outpost(self):
        return next(o for o in self.outposts if o["managed"] == ap.EMBEDDED_OUTPOST)

    def add_provider(self, kind, name, key=None, **fields):
        self._next += 1
        self.providers[key or name] = {"pk": self._next, "type": kind, "name": name, **fields}
        return self._next

    # views the tests assert on
    @property
    def bindings(self):
        by_pk = {v: k for k, v in self.groups.items()}
        slug = {pk: s for s, pk in self._app_pk.items()}
        return {slug[r["target"]]: by_pk[r["group"]] for r in self._rows if r["target"] in slug}

    @property
    def outpost_providers(self):
        return list(self.outpost["providers"])

    def get(self, path, **q):
        if path == "/admin/version/":
            return {"version_current": self.version}
        page = int(q.pop("page", 1))
        supported = self.FILTERS.get(path, set())
        rows = self._rows_for(path, {k: v for k, v in q.items() if k in supported})
        chunk = rows[(page - 1) * self.PAGE: page * self.PAGE]
        more = page * self.PAGE < len(rows)
        return {"pagination": {"next": page + 1 if more else 0, "current": page}, "results": chunk}

    def _rows_for(self, path, q):
        if path == "/flows/instances/":
            return [{"pk": "flow-" + q["slug"], "slug": q["slug"]}]
        if path.startswith("/propertymappings/"):
            key, val = next(iter(q.items()))
            return [{"pk": "pm-" + val.rsplit("-", 1)[-1], "managed": val if key == "managed" else None, "scope_name": val}]
        if path == "/crypto/certificatekeypairs/":
            return [{"pk": "keypair-1", "name": "authentik Self-signed Certificate"}]
        if path in ("/providers/oauth2/", "/providers/proxy/"):
            kind = path.split("/")[2]
            rows = sorted((p for p in self.providers.values() if p["type"] == kind), key=lambda p: p["name"])
            if "name" in q:
                rows = [p for p in rows if p["name"] == q["name"]]
            if "name__iexact" in q:
                rows = [p for p in rows if p["name"].lower() == q["name__iexact"].lower()]
            return [dict(p) for p in rows]
        if path == "/core/groups/":
            rows = [{"pk": v, "name": k} for k, v in sorted(self.groups.items())]
            return [r for r in rows if r["name"] == q["name"]] if "name" in q else rows
        if path == "/policies/bindings/":
            rows = [dict(r) for r in self._rows]
            return [r for r in rows if r["target"] == q["target"]] if "target" in q else rows
        if path == "/outposts/instances/":
            rows = sorted(self.outposts, key=lambda o: o["managed"] or "")
            if "managed__iexact" in q:
                rows = [o for o in rows if (o["managed"] or "").lower() == q["managed__iexact"].lower()]
            return [dict(o) for o in rows]
        raise AssertionError(f"unexpected GET {path} {q}")

    def call(self, method, path, body=None, ok=None):
        if method == "GET":
            slug = path.removeprefix("/core/applications/").rstrip("/")
            if slug not in self.apps:
                raise ap.ApiError(f"GET {path}: HTTP 404")
            return dict(self.apps[slug])
        self.writes += 1
        if path in ("/providers/oauth2/", "/providers/proxy/") and method == "POST":
            assert not any(p["type"] == path.split("/")[2] and p["name"] == body["name"] for p in self.providers.values()), "duplicate provider"
            self.add_provider(path.split("/")[2], **body)
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
            self._rows.append({"pk": f"b{len(self._rows) + len(self.deleted) + 1}", **body})
            return dict(body)
        if path.startswith("/policies/bindings/") and method == "PATCH":
            pk = path.rstrip("/").rsplit("/", 1)[1]
            next(r for r in self._rows if r["pk"] == pk).update(body)
            return {}
        if path.startswith("/policies/bindings/") and method == "DELETE":
            pk = path.rstrip("/").rsplit("/", 1)[1]
            self._rows = [r for r in self._rows if r["pk"] != pk]
            self.deleted.append(pk)
            return None
        if path.startswith("/outposts/instances/") and method == "PATCH":
            pk = path.rstrip("/").rsplit("/", 1)[1]
            next(o for o in self.outposts if o["pk"] == pk).update(body)
            return {}
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
    assert set(fake.outpost_providers) == proxies | {FOREIGN_PROVIDER_PK}
    assert fake.providers["skmon"]["pk"] not in fake.outpost_providers
    before = fake.outpost_providers
    writes = fake.writes
    run_provisioner(fake, input_for())
    assert fake.outpost_providers == before and fake.writes == writes


def test_outpost_attach_is_incremental():
    fake = FakeAuthentik()
    run_provisioner(fake, input_for(profiling=False))
    run_provisioner(fake, input_for())
    assert fake.providers["pyroscope"]["pk"] in fake.outpost_providers and FOREIGN_PROVIDER_PK in fake.outpost_providers
    assert len(fake.outpost_providers) == 4


def test_each_ui_gets_its_own_provider_beside_a_catch_all_and_foreign_providers():
    fake = FakeAuthentik()
    # Sorts before vmui and alertmanager: a first-result lookup would pick these up and rewrite them.
    catch_all = fake.add_provider("proxy", "aaa-domain-catch-all", mode="forward_domain",
                                  external_host="https://auth.example.test", cookie_domain="example.test")
    other = fake.add_provider("proxy", "another-role", mode="forward_single", external_host="https://other.example.test")
    oidc = fake.add_provider("oauth2", "aaa-other-oidc", client_id="other")
    snapshot = {k: dict(v) for k, v in fake.providers.items()}
    run_provisioner(fake, input_for())
    for k, v in snapshot.items():
        assert fake.providers[k] == v, k
    pks = {fake.providers[s]["pk"] for s in ("vmui", "alertmanager", "pyroscope")}
    assert len(pks) == 3 and not pks & {catch_all, other, oidc}
    assert fake.providers["vmui"]["external_host"] == "https://vmui.c1.example.test"
    assert fake.providers["alertmanager"]["external_host"] == "https://alertmanager.c1.example.test"
    assert fake.apps["vmui"]["provider"] == fake.providers["vmui"]["pk"] != fake.apps["alertmanager"]["provider"]
    assert not {catch_all, other} & set(fake.outpost_providers)


def test_lookup_follows_every_page():
    fake = FakeAuthentik()
    for i in range(5):
        fake.add_provider("proxy", f"aa-filler-{i}", mode="forward_single")
    fake.add_provider("proxy", "zzz-last", mode="forward_single")
    run_provisioner(fake, input_for())
    n = fake.writes
    run_provisioner(fake, input_for())
    assert fake.writes == n


def test_duplicate_exact_match_fails_nonzero_and_names_kind_and_pk(capsys, monkeypatch):
    fake = FakeAuthentik()
    first = fake.add_provider("proxy", "vmui", key="vmui")
    second = fake.add_provider("proxy", "vmui", key="vmui#2")
    monkeypatch.setattr(ap, "Api", lambda url, host, token: fake)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(input_for())))
    assert ap.main() == 1
    err = capsys.readouterr().err
    assert "proxy provider 'vmui'" in err and str(first) in err and str(second) in err


def test_embedded_outpost_is_picked_by_managed_among_several():
    fake = FakeAuthentik()
    fake.outposts.insert(0, {"pk": "outpost-0", "managed": None, "type": "ldap", "providers": []})
    fake.outposts.append({"pk": "outpost-2", "managed": "other/managed", "type": "proxy", "providers": []})
    run_provisioner(fake, input_for())
    assert len(fake.outpost_providers) == 4
    assert all(o["providers"] == [] for o in fake.outposts if o["pk"] != "outpost-1")


def test_missing_embedded_outpost_is_an_error():
    fake = FakeAuthentik()
    fake.outposts[0]["managed"] = "something-else"
    with pytest.raises(ap.ApiError, match="embedded outpost"):
        run_provisioner(fake, input_for())


def test_several_embedded_outposts_are_an_error():
    fake = FakeAuthentik()
    fake.outposts.append({"pk": "outpost-9", "managed": ap.EMBEDDED_OUTPOST, "type": "proxy", "providers": []})
    with pytest.raises(ap.ApiError, match="2 embedded outposts"):
        run_provisioner(fake, input_for())


def test_missing_group_is_an_error_and_never_created():
    fake = FakeAuthentik()
    fake.groups.clear()
    with pytest.raises(ap.ApiError, match="skadmin"):
        run_provisioner(fake, input_for())
    assert fake.groups == {}


def test_extra_bindings_on_owned_apps_are_removed_and_others_untouched(capsys):
    fake = FakeAuthentik()
    run_provisioner(fake, input_for())
    fake.apps["unrelated"] = {"pk": "app-unrelated", "slug": "unrelated"}
    fake._app_pk["unrelated"] = "app-unrelated"
    fake._rows += [{"pk": "x1", "target": "app-vmui", "group": "grp-other", "order": 5},
                   {"pk": "x2", "target": "app-skmon", "user": 7, "order": 1},
                   {"pk": "keep1", "target": "app-unrelated", "group": "grp-other", "order": 0}]
    capsys.readouterr()
    run_provisioner(fake, input_for())
    out = capsys.readouterr().out
    assert sorted(fake.deleted) == ["x1", "x2"]
    assert "deleted extra binding x1" in out and "grp-other" not in out
    assert [r["pk"] for r in fake._rows if r["target"] == "app-unrelated"] == ["keep1"]
    owned = [r for r in fake._rows if r["target"] != "app-unrelated"]
    assert len(owned) == 4 and all(r["group"] == "grp-skadmin" for r in owned)
    n = fake.writes
    run_provisioner(fake, input_for())
    assert fake.writes == n


@pytest.mark.parametrize("field,bad", [("enabled", False), ("negate", True)])
def test_unhealthy_skadmin_binding_is_repaired_then_stable(field, bad, capsys):
    fake = FakeAuthentik()
    run_provisioner(fake, input_for())
    row = next(r for r in fake._rows if r["target"] == "app-vmui")
    row[field] = bad
    capsys.readouterr()
    run_provisioner(fake, input_for())
    out = capsys.readouterr().out
    assert row["enabled"] is True and row["negate"] is False
    assert "binding vmui -> skadmin: updated (enabled, negate)" in out
    assert fake.deleted == [] and len([r for r in fake._rows if r["target"] == "app-vmui"]) == 1
    n = fake.writes
    run_provisioner(fake, input_for())
    assert fake.writes == n


def test_binding_exists_before_the_provider_is_attached_to_the_outpost():
    fake = FakeAuthentik()
    order = []
    orig = fake.call

    def spy(method, path, body=None, ok=None):
        if method in ("POST", "PATCH", "DELETE"):
            order.append((method, path))
        return orig(method, path, body, ok)
    fake.call = spy
    run_provisioner(fake, input_for())
    last_binding = max(i for i, (m, p) in enumerate(order) if p == "/policies/bindings/")
    first_attach = min(i for i, (m, p) in enumerate(order) if p.startswith("/outposts/instances/"))
    assert last_binding < first_attach


def test_tile_loop_skips_the_provider_backed_slugs():
    jenv = ansible_environment()
    for p in PLAYS:
        t = task_named(p, "Create or update the skmon portal tiles in Authentik")
        kept = [i["key"] for i in t["loop"] if jenv.compile_expression(t["when"])(item=i)]
        assert not {"skmon", "vmui", "alertmanager", "pyroscope"} & set(kept), p


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
