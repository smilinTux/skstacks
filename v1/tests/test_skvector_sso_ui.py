"""skvector (Qdrant): optional web UI host behind SSO, api-key injected by Traefik.

Knobs (README "Web UI behind SSO"):

  skvector.QDRANT_READ_ONLY_API_KEY  renders QDRANT__SERVICE__READ_ONLY_API_KEY
  skvector.UI_HOST_ENABLED           second router skvector-ui[-env].<domain>
  skvector.UI_KEY_MODE               read_only (default) or full
  skvector.UI_SSO_MIDDLEWARE         forward-auth middleware, default authentik@file

The Qdrant dashboard calls the REST API from the browser on its own origin, so
the UI router injects the key on EVERY request of that host, after forward
auth. The main skvector[-env] router (programmatic clients) must not move.

Off (unset) must render byte-identical to the templates before this change:
the baselines in fixtures/skvector_ui_baseline/ were rendered from them with
the render gate's example vars.
"""
import pathlib

import jinja2
import pytest
import yaml

from image_pins import normalize_images

V1 = pathlib.Path(__file__).resolve().parents[1]
ROLE = V1 / "ansible/optional/skvector"
SRC = ROLE / "src/config/skvector"
BASELINE = pathlib.Path(__file__).resolve().parent / "fixtures/skvector_ui_baseline"
EXAMPLE = yaml.safe_load((pathlib.Path(__file__).resolve().parent / "render/vars/skvector.example.yml").read_text())["skvector"]
ENVS = ("dev", "staging", "prod")
RO_KEY = "example-read-only-key-not-a-secret"


class MandatoryError(Exception):
    pass


def _mandatory(value, msg=None):
    # Same contract as Ansible's mandatory filter: raise msg on an undefined value.
    if isinstance(value, jinja2.Undefined):
        raise MandatoryError(msg or "Mandatory variable not defined.")
    return value


def _env():
    e = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    e.filters["mandatory"] = _mandatory
    e.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return e


def render(name, env="prod", **over):
    return _env().from_string((SRC / name).read_text()).render(
        app="skvector", env=env, skvector=dict(EXAMPLE, **over))


def labels(env="prod", **over):
    return yaml.safe_load(render("skvector.yml.j2", env, **over))["services"]["qdrant"]["deploy"]["labels"]


def label_map(env="prod", **over):
    return dict(l.split("=", 1) for l in labels(env, **over))


def sfx(env):
    return "" if env == "prod" else f"-{env}"


# ---- off: unchanged -----------------------------------------------------------

@pytest.mark.parametrize("env", ENVS)
def test_off_compose_byte_identical_to_before(env):
    assert normalize_images(render("skvector.yml.j2", env)) == normalize_images((BASELINE / f"skvector-{env}.yml").read_text())


@pytest.mark.parametrize("env", ENVS)
def test_off_env_byte_identical_to_before(env):
    assert render("skvector.env.j2", env) == (BASELINE / f"skvector-{env}.env").read_text()


@pytest.mark.parametrize("value", [False, "false", "", None])
def test_off_values_render_like_unset(value):
    assert render("skvector.yml.j2", UI_HOST_ENABLED=value) == render("skvector.yml.j2")


def test_off_ignores_ui_key_mode_and_missing_read_only_key():
    # UI_KEY_MODE alone (no UI host) neither fails nor renders anything.
    assert render("skvector.yml.j2", UI_KEY_MODE="read_only") == render("skvector.yml.j2")


# ---- read-only key in the env file --------------------------------------------

def test_read_only_key_renders_env_var():
    text = render("skvector.env.j2", QDRANT_READ_ONLY_API_KEY=RO_KEY)
    assert f"QDRANT__SERVICE__READ_ONLY_API_KEY={RO_KEY}\n" in text
    assert "QDRANT__SERVICE__API_KEY=example-qdrant-api-key-not-a-secret\n" in text


def test_empty_read_only_key_renders_nothing():
    assert render("skvector.env.j2", QDRANT_READ_ONLY_API_KEY="") == render("skvector.env.j2")


# ---- on, read_only (default) ----------------------------------------------------

@pytest.mark.parametrize("env", ENVS)
def test_on_read_only_renders_ui_router(env):
    s = sfx(env)
    m = label_map(env, UI_HOST_ENABLED=True, QDRANT_READ_ONLY_API_KEY=RO_KEY)
    r = f"traefik.http.routers.skvector-ui{s}"
    assert m[f"{r}.rule"] == f"Host(`skvector-ui{s}.render.example.test`)"
    assert m[f"{r}.entrypoints"] == "websecure"
    assert m[f"{r}.tls"] == "true"
    assert m[f"{r}.tls.certresolver"] == "main"
    assert f"{r}.tls.options" not in m  # Traefik default TLS options, as every router (#463)
    assert m[f"{r}.service"] == f"skvector{s}"
    # forward auth FIRST, then the key, then the existing body buffering and
    # the main router's no-error-pages chain (the dashboard reads JSON errors).
    assert m[f"{r}.middlewares"].split(",") == [
        "authentik@file", f"skvector{s}-ui-apikey", f"skvector{s}-buffering", "default-no-error-pages@file"]
    assert m[f"traefik.http.middlewares.skvector{s}-ui-apikey.headers.customRequestHeaders.api-key"] == RO_KEY


@pytest.mark.parametrize("env", ENVS)
def test_on_leaves_main_and_grpc_routers_untouched(env):
    off = labels(env)
    on = labels(env, UI_HOST_ENABLED=True, QDRANT_READ_ONLY_API_KEY=RO_KEY)
    added = [l for l in on if l not in off]
    assert [l for l in off if l not in on] == []
    assert all(f"skvector-ui{sfx(env)}." in l or f"skvector{sfx(env)}-ui-apikey." in l for l in added), added
    assert len(added) == 7


def test_on_read_only_never_carries_the_full_key():
    text = render("skvector.yml.j2", UI_HOST_ENABLED=True, QDRANT_READ_ONLY_API_KEY=RO_KEY)
    assert EXAMPLE["QDRANT_API_KEY"] not in text


@pytest.mark.parametrize("missing", [{}, {"QDRANT_READ_ONLY_API_KEY": ""}, {"QDRANT_READ_ONLY_API_KEY": "  "}])
def test_on_read_only_without_key_fails_clearly(missing):
    with pytest.raises(MandatoryError, match="UI_KEY_MODE read_only needs skvector.QDRANT_READ_ONLY_API_KEY"):
        render("skvector.yml.j2", UI_HOST_ENABLED=True, **missing)


# ---- on, full -------------------------------------------------------------------

def test_on_full_uses_main_key():
    m = label_map(UI_HOST_ENABLED=True, UI_KEY_MODE="full", QDRANT_READ_ONLY_API_KEY=RO_KEY)
    assert m["traefik.http.middlewares.skvector-ui-apikey.headers.customRequestHeaders.api-key"] == EXAMPLE["QDRANT_API_KEY"]


def test_on_full_without_main_key_fails_clearly():
    with pytest.raises(MandatoryError, match="UI_KEY_MODE full needs skvector.QDRANT_API_KEY"):
        render("skvector.yml.j2", UI_HOST_ENABLED=True, UI_KEY_MODE="full", QDRANT_API_KEY="")


def test_bad_key_mode_fails():
    with pytest.raises(MandatoryError, match="must be read_only or full"):
        render("skvector.yml.j2", UI_HOST_ENABLED=True, UI_KEY_MODE="admin", QDRANT_READ_ONLY_API_KEY=RO_KEY)


# ---- SSO middleware: overridable, never off -----------------------------------------

def test_sso_middleware_override():
    m = label_map(UI_HOST_ENABLED=True, QDRANT_READ_ONLY_API_KEY=RO_KEY, UI_SSO_MIDDLEWARE="sso-other@file")
    assert m["traefik.http.routers.skvector-ui.middlewares"].split(",")[0] == "sso-other@file"


@pytest.mark.parametrize("off", ["none", "false", "None"])
def test_sso_middleware_cannot_be_turned_off(off):
    with pytest.raises(MandatoryError, match="cannot be turned off"):
        render("skvector.yml.j2", UI_HOST_ENABLED=True, QDRANT_READ_ONLY_API_KEY=RO_KEY, UI_SSO_MIDDLEWARE=off)


def test_dollar_in_key_is_escaped_for_stack_deploy():
    m = label_map(UI_HOST_ENABLED=True, QDRANT_READ_ONLY_API_KEY="ab$cd")
    assert m["traefik.http.middlewares.skvector-ui-apikey.headers.customRequestHeaders.api-key"] == "ab$$cd"


# ---- portal tile launch_url -------------------------------------------------------

def _launch_url(env, **over):
    play = yaml.safe_load((ROLE / f"deploy_skvector-{env}.yml").read_text())
    task = next(t for p in play for t in p.get("tasks", []) if "portal tile" in t.get("name", ""))
    expr = task["vars"]["portal_tile"]["launch_url"]
    return _env().from_string(expr).render(env=env, skvector=dict(EXAMPLE, **over))


@pytest.mark.parametrize("env", ENVS)
def test_tile_off_opens_main_host(env):
    assert _launch_url(env) == f"https://skvector{sfx(env)}.render.example.test"


@pytest.mark.parametrize("env", ENVS)
def test_tile_on_opens_ui_dashboard(env):
    assert _launch_url(env, UI_HOST_ENABLED=True) == f"https://skvector-ui{sfx(env)}.render.example.test/dashboard#/collections"


@pytest.mark.parametrize("env", ENVS)
def test_tile_group_default_stays_skadmin(env):
    play = yaml.safe_load((ROLE / f"deploy_skvector-{env}.yml").read_text())
    task = next(t for p in play for t in p.get("tasks", []) if "portal tile" in t.get("name", ""))
    assert "default('skadmin', true)" in task["vars"]["portal_tile"]["group"]
