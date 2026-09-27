"""skfenceha live-parity hooks, second batch. An existing HA Traefik edge
differs from the framework render in a handful of places that are policy,
not bugs: which security headers the default middleware sets, the dashboard
basicAuth realm, whether the error-page/redirect routers ask the ACME
resolver for a certificate, a docker provider on the ACME master, and the
worker's static log/accessLog/global/tls blocks. Every hook defaults to the
previous render; these tests pin both the default and an override shaped
like such a live deploy."""
import pathlib

import jinja2
import yaml

SRC = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/config/skfenceha"


def render(name, **skfenceha_vars):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string((SRC / name).read_text()).render(
        env="prod", app="skfenceha", domain="example.com", cluster_name="cluster1",
        skfenceha=skfenceha_vars)
    return yaml.safe_load(out)


def middlewares(**kw):
    return render("dynamic-middlewares.yml.j2", **kw)["http"]["middlewares"]


def routers(**kw):
    return render("routers.yml.j2", **kw)["http"]["routers"]


AUTH = "admin:$2y$05$abcdefghijklmnopqrstuv"
ROUTERS_WITH_HOOK = ("catch-all", "root-https", "dashboard-redirect-https", "wildcard-domain", "wildcard-cluster")


# -- 2. default-security-headers + dashboard realm ---------------------------

def test_security_headers_default_unchanged():
    h = middlewares()["default-security-headers"]["headers"]
    assert h["permissionsPolicy"] == "camera=(), microphone=(), geolocation=(), payment=(), usb=(), vr=()"
    assert h["customResponseHeaders"] == {"X-Permitted-Cross-Domain-Policies": "none", "X-Robots-Tag": "none", "Server": ""}
    assert h["customFrameOptionsValue"] == "DENY"


def test_security_headers_override_replaces_the_set():
    live = {
        "browserXssFilter": True, "contentTypeNosniff": True, "forceSTSHeader": True,
        "stsIncludeSubdomains": True, "stsPreload": True, "stsSeconds": 63072000,
        "customFrameOptionsValue": "DENY", "referrerPolicy": "strict-origin-when-cross-origin",
    }
    h = middlewares(SECURITY_HEADERS=live)["default-security-headers"]["headers"]
    assert h == live


def test_security_headers_override_keeps_cors_block():
    h = middlewares(SECURITY_HEADERS={"browserXssFilter": True},
                    CORS_ALLOWED_ORIGINS=["https://app.example.com"])["default-security-headers"]["headers"]
    assert h["browserXssFilter"] is True
    assert h["accessControlAllowOriginList"] == ["https://app.example.com"]
    assert "permissionsPolicy" not in h


def test_dashboard_realm_default_unchanged():
    assert middlewares(DASHBOARD_AUTH_HASH=AUTH)["traefikAuth"]["basicAuth"]["realm"] == "TraefikDashboard"


def test_dashboard_realm_empty_omits_realm():
    ba = middlewares(DASHBOARD_AUTH_HASH=AUTH, DASHBOARD_AUTH_REALM="")["traefikAuth"]["basicAuth"]
    assert "realm" not in ba
    assert ba["users"] == [AUTH]


def test_dashboard_realm_custom():
    ba = middlewares(DASHBOARD_AUTH_HASH=AUTH, DASHBOARD_AUTH_REALM="Edge")["traefikAuth"]["basicAuth"]
    assert ba["realm"] == "Edge"


def test_auth_hash_rendered_verbatim():
    # The value goes through untouched: no $$ escaping, no quoting damage.
    assert middlewares(DASHBOARD_AUTH_HASH=AUTH)["traefikAuth"]["basicAuth"]["users"] == [AUTH]


# -- 3. router cert resolver -------------------------------------------------

def test_router_cert_resolver_default_main():
    r = routers(ACME_ENABLED=True)
    for name in ROUTERS_WITH_HOOK + ("dashboard",):
        assert r[name]["tls"] == {"certResolver": "main"}, name


def test_router_cert_resolver_empty_renders_bare_tls_but_dashboard_keeps_main():
    r = routers(ACME_ENABLED=True, ROUTER_CERT_RESOLVER="")
    for name in ROUTERS_WITH_HOOK:
        assert r[name]["tls"] == {}, name
    assert r["dashboard"]["tls"] == {"certResolver": "main"}


def test_router_cert_resolver_named():
    r = routers(ACME_ENABLED=True, ROUTER_CERT_RESOLVER="other")
    assert r["catch-all"]["tls"] == {"certResolver": "other"}


def test_router_cert_resolver_ignored_without_acme():
    r = routers(ROUTER_CERT_RESOLVER="other")
    for name in ROUTERS_WITH_HOOK:
        assert r[name]["tls"] == {}, name


# -- 4. docker provider on the ACME master -----------------------------------

def test_acme_docker_provider_default_absent():
    assert "docker" not in render("traefik.yml.j2")["providers"]


def test_acme_docker_provider_enabled():
    p = render("traefik.yml.j2", ACME_DOCKER_PROVIDER_ENABLED=True)["providers"]
    assert p["docker"] == {
        "endpoint": "tcp://tasks.skfenceha-prod_socket-proxy:2375",
        "network": "cloud-socket-proxy-prod",
        "exposedByDefault": False,
        "watch": True,
    }
    assert p["docker"] == p["swarm"]


# -- 5. worker static config -------------------------------------------------

def worker(**kw):
    return render("traefik-worker.yml.j2", **kw)


def test_worker_defaults_unchanged():
    w = worker()
    assert "log" not in w
    assert "filters" not in w["accessLog"]
    assert set(w["accessLog"]["fields"]) == {"headers"}
    assert w["global"] == {"checknewversion": False, "sendanonymoususage": False}
    assert "tls" not in w


def test_worker_log_block():
    text = (SRC / "traefik-worker.yml.j2").read_text()
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(text).render(env="prod", app="skfenceha",
                                       skfenceha={"WORKER_LOG": {"level": "INFO", "format": "json"}})
    assert yaml.safe_load(out)["log"] == {"level": "INFO", "format": "json"}
    # entrypoint.sh's awk needs a bare `log:` line and a block-style `  level:`.
    assert "\nlog:\n  level: " in out


def test_worker_access_log_filters():
    f = {"statusCodes": ["200-299", "400-599"], "retryAttempts": True, "minDuration": "10ms"}
    assert worker(ACCESS_LOG_FILTERS=f)["accessLog"]["filters"] == f


def test_worker_access_log_fields_replaces_the_block():
    fields = {
        "defaultMode": "keep",
        "names": {"headers": {"Authorization": "redact"}},
        "headers": {"defaultMode": "keep", "names": {"Authorization": "redact", "X-Auth-Token": "redact"}},
    }
    assert worker(ACCESS_LOG_FIELDS=fields)["accessLog"]["fields"] == fields


def test_worker_access_log_key_stays_bare_for_entrypoint():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string((SRC / "traefik-worker.yml.j2").read_text()).render(
        env="prod", app="skfenceha", skfenceha={"ACCESS_LOG_FIELDS": {"defaultMode": "keep"}})
    assert "\naccessLog:\n" in out


def test_worker_global_empty_renders_null():
    assert worker(WORKER_GLOBAL={})["global"] is None


def test_worker_global_custom():
    assert worker(WORKER_GLOBAL={"checknewversion": True})["global"] == {"checknewversion": True}


def test_worker_tls_block():
    tls = {"stores": {"default": {"acmeStorage": "/acme/acme.json"}}}
    assert worker(WORKER_TLS=tls)["tls"] == tls
