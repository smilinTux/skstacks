"""sksso.CAPAUTH_ENABLED: a CapAuth OIDC identity provider next to Authentik,
registered in Authentik as an OAuth source, with its own login flow that an
application opts into (skmesh.AUTHENTIK_AUTHENTICATION_FLOW).

Render side: off by default (nothing changes for an existing instance); when
on, the capauth service, its env file and its client registry agree with the
Authentik side on the issuer, the callback URL and the client. Provisioner
side: runs against a fake Authentik API that behaves like 2025.12's source
serializer (a PATCH without provider type and URLs is refused, the client
secret is never read back)."""
import http.server
import json
import pathlib
import subprocess
import sys
import threading

import jinja2
import pytest
import yaml

SKSSO = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso"
PROVISIONER = SKSSO / "src/sksso/capauth_provision.py"
SECRET = "capauth-client-secret-0123456789abcdef0123"
ADMIN = "capauth-admin-token-0123456789abcdef012345"
ENABLED = {"CAPAUTH_ENABLED": True, "CAPAUTH_IMAGE": "registry.example.test/capauth:1.2.3@sha256:" + "a" * 64,
           "CAPAUTH_CLIENT_SECRET": SECRET, "CAPAUTH_ADMIN_TOKEN": ADMIN}


def _render(name, env="dev", **sksso):
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    base.update(sksso)
    jenv = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    jenv.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    jenv.filters["to_json"] = json.dumps
    return jenv.from_string((SKSSO / "src/config/sksso" / name).read_text()).render(env=env, app="sksso", sksso=base)


def _compose(env="dev", **sksso):
    # the compose template also reads optional keys through default(); the
    # strict renderer above is for the files this feature adds
    jenv = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    jenv.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    base.update(sksso)
    out = jenv.from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(env=env, app="sksso", sksso=base)
    return yaml.safe_load(out)["services"]


def _envfile(env="dev", **sksso):
    out = _render("capauth.env.j2", env=env, **sksso)
    pairs = [line.split("=", 1) for line in out.splitlines() if line and not line.startswith("#")]
    return dict(pairs)


def test_off_by_default_nothing_changes():
    assert "capauth" not in _compose()
    assert "capauth" not in _compose(CAPAUTH_ENABLED=False)


def test_capauth_service_when_enabled():
    svc = _compose(**ENABLED)["capauth"]
    assert svc["image"] == ENABLED["CAPAUTH_IMAGE"]
    assert svc["env_file"] == "/var/data/config/sksso-dev/capauth.env"
    assert svc["deploy"]["replicas"] == 1  # SQLite state: exactly one writer
    assert svc["deploy"]["restart_policy"]["condition"] == "any"
    assert set(svc["networks"]) == {"cloud-public-dev", "sksso-dev"}
    assert "/var/data/sksso-dev/capauth:/data" in svc["volumes"]
    assert "/var/data/config/sksso-dev/capauth-oidc-clients.json:/config/oidc-clients.json:ro" in svc["volumes"]
    labels = svc["deploy"]["labels"]
    assert "traefik.http.routers.sksso-dev-capauth.rule=Host(`capauth-dev.${BASE_DOMAIN}`)" in labels
    assert "traefik.http.services.sksso-dev-capauth.loadbalancer.server.port=8420" in labels
    assert "traefik.swarm.network=cloud-public-dev" in labels
    assert "8420" in " ".join(svc["healthcheck"]["test"])


def test_capauth_prod_host_has_no_env_suffix_and_placement_follows_server():
    svcs = _compose(env="prod", placement_exclude_nodes=["n2"], **ENABLED)
    labels = svcs["capauth"]["deploy"]["labels"]
    assert "traefik.http.routers.sksso-capauth.rule=Host(`capauth.${BASE_DOMAIN}`)" in labels
    assert svcs["capauth"]["deploy"]["placement"] == svcs["server"]["deploy"]["placement"]


def test_capauth_env_names_the_public_https_issuer_and_keeps_state_in_data():
    e = _envfile(**ENABLED)
    assert e["CAPAUTH_OIDC_ISSUER"] == "https://capauth-dev.cluster1.example.com"
    assert e["CAPAUTH_BASE_URL"] == e["CAPAUTH_OIDC_ISSUER"]
    assert e["CAPAUTH_SERVICE_ID"] == "capauth-dev.cluster1.example.com"
    assert e["CAPAUTH_REQUIRE_APPROVAL"] == "true"
    assert e["CAPAUTH_ADMIN_TOKEN"] == ADMIN
    assert e["CAPAUTH_OIDC_CLIENTS_FILE"] == "/config/oidc-clients.json"
    for k in ("CAPAUTH_DB_PATH", "CAPAUTH_OIDC_STATE_DB", "CAPAUTH_OIDC_SIGNING_KEY_PATH"):
        assert e[k].startswith("/data/"), k
    assert e["CAPAUTH_HOME"] == e["CAPAUTH_DATA_DIR"] == "/data"


def test_capauth_env_knobs():
    e = _envfile(env="prod", CLOUDFLARED=True, CAPAUTH_HOST="id.example.org", CAPAUTH_REQUIRE_APPROVAL=False,
                 **ENABLED)
    assert e["CAPAUTH_OIDC_ISSUER"] == "https://id.example.org"
    assert e["CAPAUTH_REQUIRE_APPROVAL"] == "false"


def test_client_registry_matches_the_authentik_source_callback():
    clients = json.loads(_render("capauth-oidc-clients.json.j2", **ENABLED))
    assert clients == [{
        "client_id": "authentik",
        "client_secret": SECRET,
        "name": "Authentik (sksso)",
        "redirect_uris": ["https://sso-dev.cluster1.example.com/source/oauth/callback/capauth/"],
        "scopes": ["openid", "profile", "email"],
        "require_nonce": False,
    }]
    prod = json.loads(_render("capauth-oidc-clients.json.j2", env="prod", CLOUDFLARED=True,
                              CAPAUTH_SOURCE_SLUG="pgp", **ENABLED))
    assert prod[0]["redirect_uris"] == ["https://sso.example.com/source/oauth/callback/pgp/"]


def _plays(env):
    return yaml.safe_load((SKSSO / f"deploy_sksso-{env}.yml").read_text())


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbooks_wire_config_before_the_stack_and_provisioning_after(env):
    tasks = [t for p in _plays(env) for t in (p.get("tasks") or [])]
    names = [t.get("name", "") for t in tasks]

    def included(t):
        inc = t.get("include_tasks")
        return inc.get("file") if isinstance(inc, dict) else inc

    cfg = next(i for i, t in enumerate(tasks) if included(t) == "tasks/capauth_config.yml")
    prov = next(i for i, t in enumerate(tasks) if included(t) == "tasks/capauth_provision.yml")
    compose = names.index("Process {{ app }} Docker Compose template")
    deploy = names.index("Deploy {{ app }} stack")
    assert cfg < compose < deploy < prov
    for i in (cfg, prov):
        assert tasks[i]["when"] == "sksso.CAPAUTH_ENABLED | default(false) | bool"


def _tasks(name):
    return yaml.safe_load((SKSSO / "tasks" / name).read_text())


def _eval_assert(sksso):
    task = next(t for t in _tasks("capauth_config.yml") if "assert" in t)
    jenv = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    return all(jenv.from_string("{{ " + c + " }}").render(sksso=sksso) == "True" for c in task["assert"]["that"])


def test_config_refuses_missing_or_weak_keys():
    good = dict(ENABLED, api_token="t" * 40)
    assert _eval_assert(good)
    for key in ("CAPAUTH_IMAGE", "CAPAUTH_CLIENT_SECRET", "CAPAUTH_ADMIN_TOKEN", "api_token"):
        assert not _eval_assert({k: v for k, v in good.items() if k != key}), key
    assert not _eval_assert(dict(good, CAPAUTH_CLIENT_SECRET="short"))
    assert not _eval_assert(dict(good, CAPAUTH_IMAGE="capauth:latest"))  # must be pinned by digest


def test_secret_files_are_owner_only_and_mounted_read_only():
    tpl = {t.get("name"): t for t in _tasks("capauth_config.yml")}
    loop = tpl["Render the CapAuth env file and OIDC client registry"]["loop"]
    assert {i["mode"] for i in loop} == {"0600"}
    assert tpl["Render the CapAuth env file and OIDC client registry"].get("no_log") is True


def test_provision_task_keeps_secrets_off_argv_and_logs():
    tasks = {t["name"]: t for t in _tasks("capauth_provision.yml")}
    run = tasks["Create or update the CapAuth source, login flow and user links in Authentik"]
    assert run.get("no_log") is True
    assert run["command"]["stdin"] == "{{ sksso_capauth_input | to_json }}"
    argv = " ".join(run["command"]["argv"])
    assert "token" not in argv and "secret" not in argv
    assert "@sha256:" in argv
    assert tasks["Build the CapAuth provisioner input"].get("no_log") is True


def test_provisioner_input_matches_the_rendered_service():
    tasks = {t["name"]: t for t in _tasks("capauth_provision.yml")}
    facts = tasks["Build the CapAuth provisioner input"]["set_fact"]["sksso_capauth_input"]
    assert facts["token_url"] == "http://{{ app }}-{{ env }}_capauth:8420/oidc/token"
    assert facts["userinfo_url"] == "http://{{ app }}-{{ env }}_capauth:8420/oidc/userinfo"
    assert facts["authorization_url"] == "https://{{ sksso_capauth_host }}/oidc/authorize"
    assert facts["client_secret"] == "{{ sksso.CAPAUTH_CLIENT_SECRET }}"


def test_deploy_script_restarts_capauth_on_redeploy_only_when_enabled():
    jenv = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    src = (SKSSO / "src/sksso/deploy.j2").read_text()
    on = jenv.from_string(src).render(env="dev", app="sksso", sksso=ENABLED)
    off = jenv.from_string(src).render(env="dev", app="sksso", sksso={})
    assert "preexisted ${STACK_NAME}_capauth &&" in on and '"${STACK_NAME}_capauth"' in on
    assert "_capauth" not in off


# --- the provisioner, against a fake Authentik API ------------------------------

class FakeAuthentik:
    FLOWS = {"default-source-authentication": "f-src-auth"}

    def __init__(self, version="2025.12.3"):
        self.version = version
        self.flows = [{"pk": pk, "slug": s} for s, pk in self.FLOWS.items()]
        self.sources, self.ident, self.login, self.bindings, self.conns = [], [], [], [], []
        self.users = [{"pk": 7, "username": "alice"}]
        self.secret = None
        self.log = []
        self.n = 100

    def _pk(self):
        self.n += 1
        return f"pk{self.n}"

    def handle(self, method, path, query, body, headers):
        self.log.append((method, path, headers.get("Host"), body))
        if headers.get("Authorization") != "Bearer admin-token":
            return 403, {"detail": "no"}
        q = {k: v[0] for k, v in query.items()}
        p = path[len("/api/v3"):]
        if p == "/admin/version/":
            return 200, {"version_current": self.version}
        if p.startswith("/sources/oauth/"):
            rest = p[len("/sources/oauth/"):].strip("/")
            if method in ("POST", "PATCH"):
                for k in ("provider_type", "authorization_url", "access_token_url", "profile_url"):
                    if k not in body:  # 2025.12 OAuthSourceSerializer.validate()
                        return 400, {"non_field_errors": [f"{k} is required"]}
                if "consumer_secret" in body:
                    self.secret = body["consumer_secret"]
            if method == "POST":
                obj = {k: v for k, v in body.items() if k != "consumer_secret"}
                obj["pk"] = self._pk()
                self.sources.append(obj)
                return 201, obj
            hit = [s for s in self.sources if s["slug"] == rest]
            if not hit:
                return 404, {"detail": "Not found."}
            if method == "PATCH":
                hit[0].update({k: v for k, v in body.items() if k != "consumer_secret"})
            return 200, hit[0]
        if p == "/core/users/":
            return 200, {"results": [u for u in self.users if u["username"] == q.get("username")]}
        if p.startswith("/flows/instances/"):
            rest = p[len("/flows/instances/"):].strip("/")
            if method == "GET" and not rest:
                return 200, {"results": [f for f in self.flows if f["slug"] == q.get("slug")]}
            if method == "GET":
                hit = [f for f in self.flows if f["slug"] == rest]
                return (200, hit[0]) if hit else (404, {"detail": "Not found."})
            if method == "POST":
                obj = dict(body, pk=self._pk())
                self.flows.append(obj)
                return 201, obj
            obj = next(f for f in self.flows if f["slug"] == rest)
            obj.update(body)
            return 200, obj
        stores = {"/stages/identification/": self.ident, "/stages/user_login/": self.login,
                  "/flows/bindings/": self.bindings, "/sources/user_connections/oauth/": self.conns}
        for prefix, store in stores.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):].strip("/")
            if method == "GET":
                if prefix == "/sources/user_connections/oauth/":
                    return 200, {"results": list(store)}
                keys = {k: v for k, v in q.items() if k in ("name", "target", "stage")}
                return 200, {"results": [o for o in store if all(str(o.get(k)) == v for k, v in keys.items())]}
            if method == "POST":
                obj = dict(body, pk=self._pk())
                store.append(obj)
                return 201, obj
            obj = next(o for o in store if str(o["pk"]) == rest)
            obj.update(body)
            return 200, obj
        return 404, {"detail": f"unhandled {method} {p}"}


@pytest.fixture
def fake_authentik():
    servers = []

    def start(version="2025.12.3"):
        fake = FakeAuthentik(version)

        class Handler(http.server.BaseHTTPRequestHandler):
            def _do(self):
                from urllib.parse import parse_qs, urlsplit
                u = urlsplit(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                status, payload = fake.handle(self.command, u.path, parse_qs(u.query), body, self.headers)
                raw = b"" if payload is None else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = do_PATCH = _do

            def log_message(self, *a):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
        fake.url = f"http://127.0.0.1:{srv.server_address[1]}"
        return fake

    yield start
    for srv in servers:
        srv.shutdown()


FP = "0123456789ABCDEF0123456789ABCDEF01234567"


def _cfg(fake, **over):
    cfg = {
        "url": fake.url, "host": "sso.demo.example.test", "token": "admin-token",
        "source_name": "CapAuth", "source_slug": "capauth", "client_id": "authentik", "client_secret": SECRET,
        "authorization_url": "https://capauth.demo.example.test/oidc/authorize",
        "token_url": "http://sksso-prod_capauth:8420/oidc/token",
        "userinfo_url": "http://sksso-prod_capauth:8420/oidc/userinfo",
        "source_authentication_flow": "default-source-authentication",
        "identification_stage": "capauth-identification", "login_stage": "capauth-authentication-login",
        "flow_name": "CapAuth login", "flow_slug": "capauth-authentication", "flow_title": "Sign in with CapAuth",
        "users": [],
    }
    cfg.update(over)
    return cfg


def _provision(cfg):
    return subprocess.run([sys.executable, str(PROVISIONER)], input=json.dumps(cfg),
                          capture_output=True, text=True, timeout=60)


def test_provisioner_creates_everything_then_is_idempotent(fake_authentik):
    fake = fake_authentik()
    first = _provision(_cfg(fake))
    assert first.returncode == 0, first.stderr
    for line in ("source: created", "source client secret: set", "identification stage: created",
                 "user login stage: created", "flow: created", "flow binding identification: created",
                 "flow binding user login: created"):
        assert line in first.stdout, line
    src = fake.sources[0]
    assert src["provider_type"] == "openidconnect" and src["pkce"] == "S256"
    assert src["user_matching_mode"] == "identifier" and src["enrollment_flow"] is None
    assert src["authentication_flow"] == "f-src-auth"
    assert src["authorization_code_auth_method"] == "basic_auth"
    assert src["consumer_key"] == "authentik" and fake.secret == SECRET
    assert "consumer_secret" not in src
    ident = fake.ident[0]
    assert ident["user_fields"] == [] and ident["sources"] == [src["pk"]] and ident["password_stage"] is None
    flow = next(f for f in fake.flows if f["slug"] == "capauth-authentication")
    assert flow["designation"] == "authentication"
    assert sorted((b["stage"], b["order"]) for b in fake.bindings) == sorted(
        [(ident["pk"], 10), (fake.login[0]["pk"], 100)])
    assert all(b["target"] == flow["pk"] for b in fake.bindings)
    assert {h for _, _, h, _ in fake.log} == {"sso.demo.example.test"}

    second = _provision(_cfg(fake))
    assert second.returncode == 0, second.stderr
    assert "created" not in second.stdout and "updated (" not in second.stdout
    assert "source client secret: set" in second.stdout  # write-only: set every run
    assert len(fake.sources) == len(fake.ident) == len(fake.login) == 1 and len(fake.bindings) == 2


def test_provisioner_repairs_drift_with_a_full_source_body(fake_authentik):
    fake = fake_authentik()
    assert _provision(_cfg(fake)).returncode == 0
    fake.sources[0]["pkce"] = "none"
    fake.sources[0]["enrollment_flow"] = "someone-added-enrollment"
    out = _provision(_cfg(fake))
    assert out.returncode == 0, out.stderr
    assert "source: updated (enrollment_flow, pkce)" in out.stdout
    assert fake.sources[0]["pkce"] == "S256" and fake.sources[0]["enrollment_flow"] is None


def test_provisioner_rotates_the_client_secret(fake_authentik):
    fake = fake_authentik()
    assert _provision(_cfg(fake)).returncode == 0
    assert _provision(_cfg(fake, client_secret="rotated-" + SECRET)).returncode == 0
    assert fake.secret == "rotated-" + SECRET


def test_provisioner_links_users_by_fingerprint_only(fake_authentik):
    fake = fake_authentik()
    out = _provision(_cfg(fake, users=[{"username": "alice", "fingerprint": FP.lower()}]))
    assert out.returncode == 0, out.stderr
    assert fake.conns == [{"user": 7, "source": fake.sources[0]["pk"], "identifier": FP, "pk": fake.conns[0]["pk"]}]
    again = _provision(_cfg(fake, users=[{"username": "alice", "fingerprint": FP}]))
    assert "created" not in again.stdout and len(fake.conns) == 1


def test_provisioner_refuses_unknown_user_and_bad_fingerprint(fake_authentik):
    fake = fake_authentik()
    nouser = _provision(_cfg(fake, users=[{"username": "mallory", "fingerprint": FP}]))
    assert nouser.returncode == 1 and "'mallory' does not exist" in nouser.stderr
    badfp = _provision(_cfg(fake, users=[{"username": "alice", "fingerprint": "not-a-fingerprint"}]))
    assert badfp.returncode == 1 and "fingerprint" in badfp.stderr
    assert fake.users == [{"pk": 7, "username": "alice"}] and fake.conns == []


def test_provisioner_refuses_authentik_without_source_pkce(fake_authentik):
    fake = fake_authentik("2024.4.2")
    out = _provision(_cfg(fake))
    assert out.returncode == 1 and "2025.2" in out.stderr
    assert fake.sources == []


def test_provisioner_never_prints_a_secret(fake_authentik):
    fake = fake_authentik()
    for cfg in (_cfg(fake), _cfg(fake, token="wrong-admin-token")):
        out = _provision(cfg)
        for secret in ("admin-token", "wrong-admin-token", SECRET):
            assert secret not in out.stdout + out.stderr


def test_provisioner_fails_loudly(fake_authentik):
    fake = fake_authentik()
    bad = _provision(_cfg(fake, token="wrong-admin-token"))
    assert bad.returncode == 1 and "HTTP 403" in bad.stderr
    flow = _provision(_cfg(fake, source_authentication_flow="no-such-flow"))
    assert flow.returncode == 1 and "no-such-flow" in flow.stderr
    missing = _provision(_cfg(fake, client_secret=""))
    assert missing.returncode == 2 and "client_secret" in missing.stderr
