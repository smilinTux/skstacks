"""sksso server router: `router_middlewares` and `SERVER_BACKEND_SCHEME` hooks.

A production Authentik router carries `middlewares=default@file` (security headers, error
pages, gzip) and reaches the server over https on 9443. The framework template had no
middleware hook and always used http on 9000, so the first framework deploy of sksso would
have silently dropped the security headers from the SSO login page and changed the backend.
Both are now knobs; unset renders byte-identical to before.
"""
import pathlib

import jinja2
import pytest
import yaml

APP = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso"
T = APP / "src/config/sksso/sksso.yml.j2"
README = APP / "README.md"
PLAYBOOKS = {e: APP / f"deploy_sksso-{e}.yml" for e in ("dev", "staging", "prod")}


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def text(env="prod", **over):
    sk = {"CLUSTERNAME": "demo", "DOMAIN": "example.test", "postgres_user": "u",
          "postgres_password": "p", "authentik_secret_key": "k", **over}
    return _env().from_string(T.read_text()).render(app="sksso", env=env, sksso=sk)


def labels(env="prod", **over):
    return yaml.safe_load(text(env, **over))["services"]["server"]["deploy"]["labels"]


def router(env):
    return "sksso" + ("" if env == "prod" else "-" + env) + "-server"


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_unset_renders_byte_identical(env):
    assert text(env) == text(env, router_middlewares=[], SERVER_BACKEND_SCHEME="http")


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_default_has_no_middlewares_and_http_9000(env):
    ls, r = labels(env), router(env)
    assert not any(f"routers.{r}.middlewares=" in l for l in ls)
    assert f"traefik.http.services.{r}.loadbalancer.server.scheme=http" in ls
    assert f"traefik.http.services.{r}.loadbalancer.server.port=9000" in ls


def test_router_middlewares_label():
    ls = labels(router_middlewares=["default@file", "extra@file"])
    assert "traefik.http.routers.sksso-server.middlewares=default@file,extra@file" in ls


def test_https_backend_uses_9443():
    ls = labels(SERVER_BACKEND_SCHEME="https")
    assert "traefik.http.services.sksso-server.loadbalancer.server.scheme=https" in ls
    assert "traefik.http.services.sksso-server.loadbalancer.server.port=9443" in ls
    assert not any("sksso-server.loadbalancer.server.port=9000" in l for l in ls)


def test_knobs_touch_only_the_server_service():
    a = yaml.safe_load(text())["services"]
    b = yaml.safe_load(text(router_middlewares=["default@file"], SERVER_BACKEND_SCHEME="https"))["services"]
    for name in a:
        if name != "server":
            assert a[name] == b[name], name


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbook_rejects_an_unknown_scheme(env):
    assert "SERVER_BACKEND_SCHEME" in PLAYBOOKS[env].read_text()


def test_readme_documents_the_knobs():
    r = README.read_text()
    assert "router_middlewares" in r and "SERVER_BACKEND_SCHEME" in r
