"""skmesh (NetBird) authentication: two modes, one knob.

skmesh.AUTH_MODE:

* ``standalone`` (default): NetBird's embedded IdP (Dex, local
  email/password users), served by management at https://<skmesh host>/oauth2.
  No external IdP, nothing else in the cluster needed.
* ``authentik``: the cluster's sksso is the IdP. management.json points
  HttpConfig at the Authentik application's issuer, NetBird's Authentik IdP
  manager resolves users through a service account, the CLI gets a PKCE
  flow (device flow optional), and the deploy creates or updates the
  Authentik side itself (provider, application, redirect URIs, service
  account + app password) BEFORE the stack starts, because management 0.79
  exits at startup when the issuer's discovery document 404s.

Before this, management.json was hardwired to an Authentik application
nobody created (the deploy could never come up on its own), with an empty
AuthAudience, no IdP manager and no PKCE/device flow for the CLI; the
dashboard asked for an ``api`` scope Authentik does not have; the hostnames
ignored env (a dev deploy pointed at the prod names); and the routers named
their network with ``traefik.docker.network``, a label the swarm provider
never reads.
"""
import http.server
import json
import pathlib
import subprocess
import sys
import threading

import jinja2
import pytest
import yaml

SKMESH = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmesh"
SKSSO = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso"
SRC = SKMESH / "src/config/skmesh"
PROVISIONER = SKMESH / "src/skmesh/authentik_provision.py"
ENSURE_TOKEN = SKSSO / "src/sksso/ensure-api-token.sh"

BASE = {
    "CLUSTERNAME": "demo",
    "DOMAIN": "example.test",
    "POSTGRES_PASSWORD": "pw",
    "NETBIRD_DATASTORE_ENC_KEY": "enc",
    "NETBIRD_RELAY_AUTH_SECRET": "relaysecret",
    "NETBIRD_RELAY_ENDPOINT": "rels://skmesh.demo.example.test:443",
    "TURN_SECRET": "turnsecret",
    "TURN_USER": "u",
    "TURN_PASSWORD": "p",
}
AUTHENTIK = dict(BASE, AUTH_MODE="authentik", NETBIRD_AUTH_CLIENT_ID="skmesh",
                 IDP_MANAGER_PASSWORD="app-password-0123456789abcdef0123456789",
                 SSO_HOST_IP="192.0.2.5")


def _render(name, skmesh, env="prod"):
    jenv = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    return jenv.from_string((SRC / name).read_text()).render(env=env, skmesh=skmesh, inventory_hostname="n1")


def _mgmt(skmesh, env="prod"):
    return json.loads(_render("management.json.j2", skmesh, env))


def _envfile(skmesh, env="prod"):
    out = {}
    for line in _render("skmesh.env.j2", skmesh, env).splitlines():
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


def _compose(skmesh, env="prod"):
    return yaml.safe_load(_render("skmesh.yml.j2", skmesh, env))


def _labels(svc):
    return (svc.get("deploy") or {}).get("labels") or []


def _rule(svc, router):
    return next(lbl.split("=", 1)[1] for lbl in _labels(svc) if lbl.startswith(f"traefik.http.routers.{router}.rule="))


# --- standalone (default) -----------------------------------------------------

def test_default_is_standalone_embedded_idp():
    m = _mgmt(dict(BASE))
    idp = m["EmbeddedIdP"]
    assert idp["Enabled"] is True
    assert idp["Issuer"] == "https://skmesh.demo.example.test/oauth2"
    assert idp["DashboardRedirectURIs"] == ["https://skmesh.demo.example.test/nb-auth",
                                            "https://skmesh.demo.example.test/nb-silent-auth"]
    assert idp["CLIRedirectURIs"] == ["http://localhost:53000/"]
    # management derives HttpConfig from EmbeddedIdP; an external IdP config
    # here would be ignored at best
    for key in ("HttpConfig", "IdpManagerConfig", "PKCEAuthorizationFlow", "DeviceAuthorizationFlow"):
        assert key not in m


def test_standalone_dashboard_uses_the_embedded_idp_clients():
    env = _envfile(dict(BASE))
    assert env["AUTH_AUTHORITY"] == _mgmt(dict(BASE))["EmbeddedIdP"]["Issuer"]
    assert env["AUTH_CLIENT_ID"] == env["AUTH_AUDIENCE"] == "netbird-dashboard"
    assert env["AUTH_REDIRECT_URI"] == "/nb-auth"
    assert env["AUTH_SILENT_REDIRECT_URI"] == "/nb-silent-auth"
    assert env["USE_AUTH0"] == "false"
    assert env["NETBIRD_MGMT_API_ENDPOINT"] == env["NETBIRD_MGMT_GRPC_API_ENDPOINT"] == "https://skmesh.demo.example.test"
    assert env["AUTH_SUPPORTED_SCOPES"] == "openid profile email groups"


def test_standalone_routes_oauth2_to_management_not_the_dashboard():
    svcs = _compose(dict(BASE))["services"]
    labels = _labels(svcs["management"])
    rule = _rule(svcs["management"], "skmesh-mgmt-oauth2")
    assert "Host(`skmesh.demo.example.test`)" in rule and "PathPrefix(`/oauth2`)" in rule
    assert "traefik.http.routers.skmesh-mgmt-oauth2.service=skmesh-mgmt-api-svc" in labels
    assert "!PathPrefix(`/oauth2`)" in _rule(svcs["dashboard"], "skmesh-dash")


def test_standalone_needs_no_sso():
    mgmt = _compose(dict(BASE))["services"]["management"]
    assert "extra_hosts" not in mgmt
    assert "SSO_HOST_IP" not in BASE  # rendered with StrictUndefined above


# --- authentik ------------------------------------------------------------------

def test_authentik_points_management_at_the_application_issuer():
    m = _mgmt(AUTHENTIK)
    issuer = "https://sso.demo.example.test/application/o/skmesh/"
    http = m["HttpConfig"]
    assert http["AuthIssuer"] == issuer
    assert http["OIDCConfigEndpoint"] == issuer + ".well-known/openid-configuration"
    assert http["AuthKeysLocation"] == issuer + "jwks/"
    assert http["AuthAudience"] == "skmesh"  # was "": no audience to validate against
    assert http["AuthUserIDClaim"] == "sub"
    assert "EmbeddedIdP" not in m


def test_authentik_idp_manager_uses_the_service_account():
    idp = _mgmt(AUTHENTIK)["IdpManagerConfig"]
    assert idp["ManagerType"] == "authentik"
    cc = idp["ClientConfig"]
    assert cc["Issuer"] == "https://sso.demo.example.test/application/o/skmesh"
    assert cc["TokenEndpoint"] == "https://sso.demo.example.test/application/o/token/"
    assert cc["ClientID"] == "skmesh" and cc["GrantType"] == "client_credentials"
    assert idp["ExtraConfig"] == {"Username": "skmesh-idp-manager",
                                  "Password": AUTHENTIK["IDP_MANAGER_PASSWORD"]}


def test_authentik_cli_login_is_pkce_and_device_flow_is_opt_in():
    m = _mgmt(AUTHENTIK)
    pkce = m["PKCEAuthorizationFlow"]["ProviderConfig"]
    assert pkce["ClientID"] == pkce["Audience"] == "skmesh"
    assert pkce["RedirectURLs"] == ["http://localhost:53000"]
    assert "offline_access" in pkce["Scope"].split()
    assert "DeviceAuthorizationFlow" not in m
    dev = _mgmt(dict(AUTHENTIK, DEVICE_AUTH_FLOW=True))["DeviceAuthorizationFlow"]
    assert dev["Provider"] == "hosted" and dev["ProviderConfig"]["ClientID"] == "skmesh"


def test_authentik_dashboard_matches_management():
    m, env = _mgmt(AUTHENTIK), _envfile(AUTHENTIK)
    assert env["AUTH_AUTHORITY"] == m["HttpConfig"]["AuthIssuer"]
    assert env["AUTH_CLIENT_ID"] == env["AUTH_AUDIENCE"] == m["HttpConfig"]["AuthAudience"]
    scopes = env["AUTH_SUPPORTED_SCOPES"].split()
    assert {"openid", "profile", "email", "offline_access"} <= set(scopes)
    assert "api" not in scopes  # not an Authentik scope; the API scope is the IdP manager's alone


def test_authentik_management_reaches_sso_by_extra_hosts():
    svcs = _compose(AUTHENTIK)["services"]
    assert svcs["management"]["extra_hosts"] == ["sso.demo.example.test:192.0.2.5"]
    assert not any("skmesh-mgmt-oauth2" in lbl for lbl in _labels(svcs["management"]))
    assert "/oauth2" not in _rule(svcs["dashboard"], "skmesh-dash")


def test_authentik_knobs_override_client_slug_and_sso_host():
    sk = dict(AUTHENTIK, NETBIRD_AUTH_CLIENT_ID="nb", AUTHENTIK_APP_SLUG="mesh",
              SSO_HOST="login.example.test", IDP_MANAGER_USERNAME="nb-idp")
    m, env = _mgmt(sk), _envfile(sk)
    assert m["HttpConfig"]["AuthIssuer"] == "https://login.example.test/application/o/mesh/"
    assert m["HttpConfig"]["AuthAudience"] == "nb"
    assert m["IdpManagerConfig"]["ExtraConfig"]["Username"] == "nb-idp"
    assert env["AUTH_AUTHORITY"] == "https://login.example.test/application/o/mesh/"
    assert _compose(sk)["services"]["management"]["extra_hosts"] == ["login.example.test:192.0.2.5"]


def test_extra_ca_cert_is_mounted_for_management_only_when_set():
    mounts = _compose(dict(AUTHENTIK, EXTRA_CA_CERT="PEM"))["services"]["management"]["volumes"]
    assert "/var/data/config/skmesh-prod/extra-ca.pem:/etc/ssl/certs/skmesh-extra-ca.pem:ro" in mounts
    assert not any("extra-ca" in v for v in _compose(AUTHENTIK)["services"]["management"]["volumes"])


# --- hostnames follow env, like sksso's own router ------------------------------

@pytest.mark.parametrize("mode", [BASE, AUTHENTIK])
def test_dev_hostnames_are_env_suffixed_everywhere(mode):
    m, env, svcs = _mgmt(mode, "dev"), _envfile(mode, "dev"), _compose(mode, "dev")["services"]
    host = "skmesh-dev.demo.example.test"
    assert m["Signal"]["URI"] == f"{host}:443"
    assert env["NETBIRD_MGMT_API_ENDPOINT"] == f"https://{host}"
    assert f"Host(`{host}`)" in _rule(svcs["dashboard"], "skmesh-dash")
    if mode is AUTHENTIK:
        assert m["HttpConfig"]["AuthIssuer"] == "https://sso-dev.demo.example.test/application/o/skmesh/"
        assert svcs["management"]["extra_hosts"] == ["sso-dev.demo.example.test:192.0.2.5"]
    else:
        assert m["EmbeddedIdP"]["Issuer"] == f"https://{host}/oauth2"


def test_sso_hostname_matches_sksso_router():
    compose = (SKSSO / "src/config/sksso/sksso.yml.j2").read_text()
    assert "Host(`sso{% if env != 'prod' %}-{{ env }}{% endif %}.${BASE_DOMAIN}`)" in compose


@pytest.mark.parametrize("mode", [BASE, AUTHENTIK])
def test_routed_services_name_their_network_for_the_swarm_provider(mode):
    for name, svc in _compose(mode)["services"].items():
        labels = _labels(svc)
        if "traefik.enable=true" in labels:
            assert "traefik.swarm.network=cloud-public-prod" in labels, name
            assert not any(lbl.startswith("traefik.docker.network") for lbl in labels), name


# --- playbooks --------------------------------------------------------------------

def _plays(path):
    return yaml.safe_load(path.read_text())


def _all_tasks(path):
    out = []
    for play in _plays(path):
        for section in ("pre_tasks", "tasks", "post_tasks"):
            out.extend(play.get(section) or [])
    return out


@pytest.mark.parametrize("pb", ["deploy_skmesh-prod.yml", "deploy_skmesh-dev.yml"])
def test_authentik_side_is_provisioned_before_the_stack_starts(pb):
    names = [t.get("name", "") for t in _all_tasks(SKMESH / pb)]
    inc = next(i for i, t in enumerate(_all_tasks(SKMESH / pb))
               if isinstance(t.get("include_tasks"), dict))
    deploy = names.index("Deploy {{ app }} stack")
    assert inc < deploy
    task = _all_tasks(SKMESH / pb)[inc]
    assert task["include_tasks"]["file"] == "tasks/authentik.yml"
    assert task["when"] == "skmesh_auth_mode == 'authentik'"


@pytest.mark.parametrize("pb", ["deploy_skmesh-prod.yml", "deploy_skmesh-dev.yml"])
def test_management_json_is_owner_only(pb):
    t = next(t for t in _all_tasks(SKMESH / pb) if t.get("name") == "Process {{ app }} management.json template")
    assert str(t["template"]["mode"]) == "0600"


def test_dev_and_prod_playbooks_differ_only_in_env_and_networks():
    prod = (SKMESH / "deploy_skmesh-prod.yml").read_text().splitlines()
    dev = (SKMESH / "deploy_skmesh-dev.yml").read_text().splitlines()
    diff = [(a, b) for a, b in zip(prod, dev) if a != b]
    assert len(prod) == len(dev)
    assert len(diff) == 2, diff
    assert "env: dev" in diff[0][1] and "skmesh_dev_networks" in diff[1][1]


def _assert_task():
    return next(t for t in _all_tasks(SKMESH / "deploy_skmesh-prod.yml")
                if t.get("name") == "Check skmesh.AUTH_MODE and the vault keys it needs")


def _eval_assert(skmesh):
    """Evaluate the playbook's assert conditions with plain Jinja."""
    jenv = jinja2.Environment()
    mode = str(skmesh.get("AUTH_MODE", "standalone")).lower()
    return all(jenv.compile_expression(cond)(skmesh=skmesh, skmesh_auth_mode=mode)
               for cond in _assert_task()["assert"]["that"])


def test_assert_accepts_both_modes():
    assert _eval_assert(dict(BASE))
    assert _eval_assert(AUTHENTIK)


def test_assert_refuses_an_authentik_era_vault_without_an_explicit_mode():
    assert not _eval_assert(dict(BASE, NETBIRD_AUTH_CLIENT_ID="skmesh"))
    assert _eval_assert(dict(BASE, NETBIRD_AUTH_CLIENT_ID="skmesh", AUTH_MODE="standalone"))


@pytest.mark.parametrize("drop", ["IDP_MANAGER_PASSWORD", "SSO_HOST_IP"])
def test_assert_refuses_authentik_without_its_keys(drop):
    assert not _eval_assert({k: v for k, v in AUTHENTIK.items() if k != drop})


def test_assert_refuses_an_unknown_mode():
    assert not _eval_assert(dict(BASE, AUTH_MODE="keycloak"))


def test_authentik_tasks_keep_secrets_out_of_logs():
    tasks = yaml.safe_load((SKMESH / "tasks/authentik.yml").read_text())
    by_name = {t["name"]: t for t in tasks}
    for name in ("Load sksso's vault", "Build the Authentik provisioner input",
                 "Create or update the skmesh provider, application and IdP manager in Authentik"):
        assert by_name[name].get("no_log") is True, name
    run = by_name["Create or update the skmesh provider, application and IdP manager in Authentik"]
    assert "token" not in " ".join(run["command"]["argv"])  # secrets on stdin, not argv
    assert run["command"]["stdin"] == "{{ skmesh_authentik_input | to_json }}"
    assert "@sha256:" in run["command"]["argv"][8]


# --- the Authentik provisioner, against a fake Authentik API --------------------

class FakeAuthentik:
    """Just enough of the Authentik v3 API for the provisioner."""

    def __init__(self, version="2025.12.3"):
        self.version = version
        self.modern = version >= "2024.8"
        self.objects = {"provider": [], "application": [], "role": [], "group": [], "user": [], "token": []}
        self.log = []
        self.token_keys = {}
        self.brand = {"brand_uuid": "b1", "default": True, "flow_device_code": None}
        self.next_pk = 100
        self.created_mappings = []

    def handle(self, method, path, query, body, headers):
        self.log.append((method, path, headers.get("Host"), headers.get("Authorization")))
        if headers.get("Authorization") != "Bearer admin-token":
            return 403, {"detail": "no"}
        q = {k: v[0] for k, v in query.items()}
        p = path[len("/api/v3"):]
        if p == "/admin/version/":
            return 200, {"version_current": self.version}
        if p == "/flows/instances/":
            known = {"default-provider-authorization-implicit-consent": "f-auth",
                     "capauth-authentication": "f-capauth"}
            if self.modern:
                known["default-provider-invalidation-flow"] = "f-inval"
            return 200, {"results": [{"pk": known[q["slug"]], "slug": q["slug"]}] if q["slug"] in known else []}
        if p == "/crypto/certificatekeypairs/":
            return 200, {"results": [{"pk": "kp1", "name": "authentik Self-signed Certificate"}]}
        if p in ("/propertymappings/provider/scope/", "/propertymappings/scope/"):
            if (p == "/propertymappings/provider/scope/") != self.modern:
                return 404, {}
            if method == "POST":  # 2024.4 has no managed API-access mapping
                self.created_mappings.append(body)
                return 201, {"pk": "pm-custom-api"}
            if "managed" in q:
                if q["managed"].endswith("authentik_api") and not self.modern:
                    return 400, {"managed": ["Select a valid choice."]}
                return 200, {"results": [{"pk": "pm-" + q["managed"].rsplit("-", 1)[1]}]}
            hit = [m for m in self.created_mappings if m["scope_name"] == q.get("scope_name")]
            return 200, {"results": [{"pk": "pm-custom-api"}] if hit else []}
        if p.startswith("/rbac/permissions/assigned_by_roles/"):
            return 200, {}
        if p.startswith("/core/groups/") and p.endswith("/add_user/"):
            return 204, None
        if p.startswith("/core/tokens/") and p.endswith("/set_key/"):
            self.token_keys[p.split("/")[3]] = body["key"]
            return 204, None
        if p == "/core/brands/":
            return 200, {"results": [self.brand]}
        if p.startswith("/core/brands/") and method == "PATCH":
            self.brand.update(body)
            return 200, self.brand
        kinds = {"/providers/oauth2/": ("provider", "name", "pk"), "/core/applications/": ("application", "slug", "slug"),
                 "/rbac/roles/": ("role", "name", "pk"), "/core/groups/": ("group", "name", "pk"),
                 "/core/users/": ("user", "username", "pk"), "/core/tokens/": ("token", "identifier", "identifier")}
        for prefix, (kind, field, key) in kinds.items():
            if not p.startswith(prefix):
                continue
            rest = p[len(prefix):].strip("/")
            store = self.objects[kind]
            if method == "GET" and not rest:
                return 200, {"results": [o for o in store if all(o.get(k) == v for k, v in q.items())]}
            if method == "GET":
                hit = [o for o in store if str(o[key]) == rest]
                return (200, hit[0]) if hit else (404, {"detail": "Not found."})
            if method == "POST" and not rest:
                obj = dict(body)
                if key == "pk":
                    self.next_pk += 1
                    obj["pk"] = self.next_pk
                store.append(obj)
                return 201, obj
            if method == "PATCH":
                obj = next(o for o in store if str(o[key]) == rest)
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


def _cfg(fake, **over):
    cfg = {
        "url": fake.url, "host": "sso.demo.example.test", "token": "admin-token",
        "client_id": "skmesh", "provider_name": "skmesh", "app_name": "SKMesh", "app_slug": "skmesh",
        "launch_url": "https://skmesh.demo.example.test/",
        "redirect_uris": ["https://skmesh.demo.example.test/nb-auth",
                          "https://skmesh.demo.example.test/nb-silent-auth", "http://localhost:53000"],
        "authorization_flow": "default-provider-authorization-implicit-consent",
        "invalidation_flow": "default-provider-invalidation-flow",
        "signing_key": "authentik Self-signed Certificate",
        "sa_username": "skmesh-idp-manager", "sa_password": "sa-app-password-0123456789abcdef",
        "sa_group": "skmesh-idp-manager", "sa_role": "skmesh-idp-manager",
        "sa_token_identifier": "skmesh-idp-manager", "device_flow": False,
        "device_code_flow": "default-provider-authorization-implicit-consent",
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
    for line in ("provider: created", "application: created", "role: created", "group: created",
                 "service account: created", "service account token: created"):
        assert line in first.stdout
    prov = fake.objects["provider"][0]
    assert prov["client_type"] == "public" and prov["sub_mode"] == "user_id"
    assert prov["client_id"] == "skmesh" and prov["signing_key"] == "kp1"
    assert prov["invalidation_flow"] == "f-inval"
    assert [u["url"] for u in prov["redirect_uris"]] == _cfg(fake)["redirect_uris"]
    assert all(u["matching_mode"] == "strict" for u in prov["redirect_uris"])
    assert sorted(prov["property_mappings"]) == sorted(
        ["pm-openid", "pm-email", "pm-profile", "pm-offline_access", "pm-authentik_api"])
    assert fake.objects["application"][0]["provider"] == prov["pk"]
    assert fake.objects["user"][0]["type"] == "service_account"
    assert fake.objects["token"][0]["intent"] == "app_password"
    assert fake.token_keys == {"skmesh-idp-manager": "sa-app-password-0123456789abcdef"}
    assert fake.objects["group"][0]["roles"] == [fake.objects["role"][0]["pk"]]
    assert fake.objects["group"][0]["is_superuser"] is False
    # every call carries the public SSO host (Authentik 404s an unknown Host)
    assert {h for _, _, h, _ in fake.log} == {"sso.demo.example.test"}

    second = _provision(_cfg(fake))
    assert second.returncode == 0, second.stderr
    assert "created" not in second.stdout and "updated (" not in second.stdout
    assert len(fake.objects["provider"]) == len(fake.objects["user"]) == 1


def test_provisioner_repairs_drift(fake_authentik):
    fake = fake_authentik()
    assert _provision(_cfg(fake)).returncode == 0
    fake.objects["provider"][0]["client_type"] = "confidential"
    out = _provision(_cfg(fake))
    assert out.returncode == 0 and "provider: updated (client_type)" in out.stdout
    assert fake.objects["provider"][0]["client_type"] == "public"


def test_provisioner_speaks_the_2024_4_api(fake_authentik):
    fake = fake_authentik("2024.4.2")
    out = _provision(_cfg(fake))
    assert out.returncode == 0, out.stderr
    prov = fake.objects["provider"][0]
    assert prov["redirect_uris"] == "\n".join(_cfg(fake)["redirect_uris"])
    assert "invalidation_flow" not in prov
    assert "scope mapping goauthentik.io/api: created" in out.stdout
    assert fake.created_mappings[0]["scope_name"] == "goauthentik.io/api"
    assert "pm-custom-api" in prov["property_mappings"]
    again = _provision(_cfg(fake))
    assert again.returncode == 0 and "created" not in again.stdout
    assert len(fake.created_mappings) == 1


def test_provisioner_sets_the_device_flow_only_when_asked_and_never_overwrites(fake_authentik):
    fake = fake_authentik()
    assert _provision(_cfg(fake)).returncode == 0
    assert fake.brand["flow_device_code"] is None
    assert _provision(_cfg(fake, device_flow=True)).returncode == 0
    assert fake.brand["flow_device_code"] == "f-auth"
    fake.brand["flow_device_code"] = "someone-elses"
    out = _provision(_cfg(fake, device_flow=True))
    assert "left as is" in out.stdout and fake.brand["flow_device_code"] == "someone-elses"


def test_provisioner_never_prints_a_secret(fake_authentik):
    fake = fake_authentik()
    for cfg in (_cfg(fake), _cfg(fake, token="wrong-admin-token")):
        out = _provision(cfg)
        for secret in ("admin-token", "wrong-admin-token", "sa-app-password-0123456789abcdef"):
            assert secret not in out.stdout + out.stderr


def test_provisioner_fails_loudly(fake_authentik):
    fake = fake_authentik()
    bad = _provision(_cfg(fake, token="wrong-admin-token"))
    assert bad.returncode == 1 and "HTTP 403" in bad.stderr
    flow = _provision(_cfg(fake, authorization_flow="no-such-flow"))
    assert flow.returncode == 1 and "no-such-flow" in flow.stderr
    missing = _provision(_cfg(fake, sa_password=""))
    assert missing.returncode == 2 and "sa_password" in missing.stderr


# --- sksso: the one-time automation token bootstrap -----------------------------

def test_ensure_api_token_is_valid_bash_and_takes_the_token_on_stdin():
    assert subprocess.run(["bash", "-n", str(ENSURE_TOKEN)]).returncode == 0
    text = ENSURE_TOKEN.read_text()
    assert "docker exec -i" in text and "sys.stdin.read()" in text
    assert "$2" not in text  # the token is never an argument


def test_ensure_api_token_refuses_without_a_container(tmp_path):
    stub = tmp_path / "docker"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    r = subprocess.run(["bash", str(ENSURE_TOKEN), "prod"], input="x" * 40, capture_output=True, text=True,
                       env={"PATH": f"{tmp_path}:/usr/bin:/bin"})
    assert r.returncode == 2 and "no sksso-prod worker/server container" in r.stderr


# --- skmesh.AUTHENTIK_AUTHENTICATION_FLOW: bind a login flow to skmesh only ------

def test_authentication_flow_knob_reaches_the_provisioner():
    tasks = yaml.safe_load((SKMESH / "tasks/authentik.yml").read_text())
    facts = next(t for t in tasks if t["name"] == "Build the Authentik provisioner input")
    value = facts["set_fact"]["skmesh_authentik_input"]["authentication_flow"]
    assert value == "{{ skmesh.AUTHENTIK_AUTHENTICATION_FLOW | default('') }}"


def test_provider_keeps_the_brand_login_by_default(fake_authentik):
    fake = fake_authentik()
    out = _provision(_cfg(fake))
    assert out.returncode == 0, out.stderr
    assert fake.objects["provider"][0]["authentication_flow"] is None


def test_provider_binds_the_authentication_flow_then_unbinds_it(fake_authentik):
    fake = fake_authentik()
    assert _provision(_cfg(fake)).returncode == 0
    bind = _provision(_cfg(fake, authentication_flow="capauth-authentication"))
    assert bind.returncode == 0, bind.stderr
    assert "provider: updated (authentication_flow)" in bind.stdout
    assert fake.objects["provider"][0]["authentication_flow"] == "f-capauth"
    again = _provision(_cfg(fake, authentication_flow="capauth-authentication"))
    assert "provider: unchanged" in again.stdout
    unbind = _provision(_cfg(fake))  # knob removed: back to the brand's login (rollback)
    assert "provider: updated (authentication_flow)" in unbind.stdout
    assert fake.objects["provider"][0]["authentication_flow"] is None


def test_unknown_authentication_flow_fails_loudly(fake_authentik):
    fake = fake_authentik()
    out = _provision(_cfg(fake, authentication_flow="no-such-login-flow"))
    assert out.returncode == 1 and "no-such-login-flow" in out.stderr
    assert fake.objects["provider"] == []
