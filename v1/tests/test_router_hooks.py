"""Instance hooks for the skhub-secure and skdash-secure Traefik routers.

Two published services (skhub, skdash) have no vault hook to add router
middlewares or a `tls.options` reference, and skdash's compose has no
`restart_policy`/`rollback_config` and no configurable resource limits or
sticky-cookie `sameSite`. This blocked an instance (prod) from reproducing
its existing routing/resource behavior after moving onto the framework.

Middleware names and other instance data (e.g. `large-upload-buffering@file`)
never appear as framework defaults -- only as generic fixtures here.
"""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKHUB = ANSIBLE / "optional/skhub/src/config/skhub/skhub.yml.j2"
SKDASH = ANSIBLE / "optional/skdash/src/config/skdash/skdash.yml.j2"


def render(path, **ctx):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    return env.from_string(path.read_text()).render(**ctx)


def _skhub_doc(**skhub_overrides):
    skhub = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com"}
    skhub.update(skhub_overrides)
    out = render(SKHUB, env="prod", skhub=skhub)
    return yaml.safe_load(out)


def _skdash_doc(**skdash_overrides):
    skdash = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com"}
    skdash.update(skdash_overrides)
    out = render(SKDASH, app="skdash", env="prod", skdash=skdash)
    return yaml.safe_load(out)


# --- skhub-secure router -----------------------------------------------

def test_skhub_default_router_has_no_extra_middlewares_or_tls_options():
    doc = _skhub_doc()
    labels = doc["services"]["nextcloud"]["deploy"]["labels"]
    assert "traefik.http.routers.skhub-secure.middlewares=skhub,skhub-dav" in labels
    assert not any("skhub-secure.tls.options" in l for l in labels)


def test_skhub_router_middlewares_appended_after_framework_ones():
    doc = _skhub_doc(router_middlewares=["upload-buffering@file"])
    labels = doc["services"]["nextcloud"]["deploy"]["labels"]
    assert "traefik.http.routers.skhub-secure.middlewares=skhub,skhub-dav,upload-buffering@file" in labels


def test_skhub_tls_options_label_set_when_configured():
    doc = _skhub_doc(tls_options="generic-tls@file")
    labels = doc["services"]["nextcloud"]["deploy"]["labels"]
    assert "traefik.http.routers.skhub-secure.tls.options=generic-tls@file" in labels


# --- skdash-secure router -----------------------------------------------

def test_skdash_default_router_has_no_middlewares_or_tls_options():
    doc = _skdash_doc()
    labels = doc["services"]["dashy"]["deploy"]["labels"]
    assert not any("-secure.middlewares=" in l for l in labels)
    assert not any("-secure.tls.options" in l for l in labels)
    assert not any("sameSite=" in l for l in labels)


def test_skdash_router_middlewares_set_when_configured():
    doc = _skdash_doc(router_middlewares=["default-no-crowdsec@file"])
    labels = doc["services"]["dashy"]["deploy"]["labels"]
    assert "traefik.http.routers.skdash-secure.middlewares=default-no-crowdsec@file" in labels


def test_skdash_tls_options_label_set_when_configured():
    doc = _skdash_doc(tls_options="generic-tls@file")
    labels = doc["services"]["dashy"]["deploy"]["labels"]
    assert "traefik.http.routers.skdash-secure.tls.options=generic-tls@file" in labels


def test_skdash_sticky_samesite_set_when_configured():
    doc = _skdash_doc(sticky_samesite="strict")
    labels = doc["services"]["dashy"]["deploy"]["labels"]
    assert "traefik.http.services.skdash.loadbalancer.sticky.cookie.sameSite=strict" in labels


# --- skdash resources / restart / rollback -------------------------------

def test_skdash_default_resources_match_framework_current_values():
    doc = _skdash_doc()
    resources = doc["services"]["dashy"]["deploy"]["resources"]
    assert resources["limits"]["cpus"] == "0.5"
    assert resources["limits"]["memory"] == "512M"
    assert resources["reservations"]["cpus"] == "0.1"
    assert resources["reservations"]["memory"] == "128M"


def test_skdash_resources_vars_override_defaults():
    doc = _skdash_doc(
        RESOURCES_LIMITS_CPUS="0.5",
        RESOURCES_LIMITS_MEMORY="2G",
        RESOURCES_RESERVATIONS_CPUS="0.25",
        RESOURCES_RESERVATIONS_MEMORY="512M",
    )
    resources = doc["services"]["dashy"]["deploy"]["resources"]
    assert resources["limits"]["cpus"] == "0.5"
    assert resources["limits"]["memory"] == "2G"
    assert resources["reservations"]["cpus"] == "0.25"
    assert resources["reservations"]["memory"] == "512M"


def test_skdash_has_restart_policy_and_rollback_config():
    doc = _skdash_doc()
    deploy = doc["services"]["dashy"]["deploy"]
    assert deploy["restart_policy"]["condition"] == "on-failure"
    assert deploy["rollback_config"]["parallelism"] == 1


# --- both templates keep rendering valid, well-formed compose YAML ------

@pytest.mark.parametrize("router_middlewares,tls_options", [([], None), (["mw@file"], "opt@file")])
def test_skhub_still_renders_valid_yaml(router_middlewares, tls_options):
    overrides = {"router_middlewares": router_middlewares}
    if tls_options:
        overrides["tls_options"] = tls_options
    doc = _skhub_doc(**overrides)
    assert "deploy" in doc["services"]["nextcloud"]


@pytest.mark.parametrize("router_middlewares,tls_options", [([], None), (["mw@file"], "opt@file")])
def test_skdash_still_renders_valid_yaml(router_middlewares, tls_options):
    overrides = {"router_middlewares": router_middlewares}
    if tls_options:
        overrides["tls_options"] = tls_options
    doc = _skdash_doc(**overrides)
    assert "deploy" in doc["services"]["dashy"]
