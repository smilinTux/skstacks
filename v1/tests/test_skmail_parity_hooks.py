"""Live-parity hooks for skmail (live_parity run, framework vs skstack01
prod). Each hook defaults to the value previously hardcoded in the template,
so an instance that sets none of them renders unchanged:

- skmail.RESOURCES_LIMITS_CPUS / RESOURCES_LIMITS_MEMORY /
  RESOURCES_RESERVATIONS_CPUS / RESOURCES_RESERVATIONS_MEMORY (skdash's
  naming) on the DMS service; an empty reservation value is omitted, and
  both empty drops the reservations block (live has no reservation).
- skmail.SSL_DOMAIN: `SSL_DOMAIN=` in skmail.env only when set.
- skmail.TLS_DOMAINS: `tls.domains[N].main` (and `.sans`) labels on the
  mail-web-<env> router.
- skmail.MAIL_WEB_IMAGE: mail-web image (default traefik/whoami:v1.12.0).
- skmail.MAIL_WEB_PLACEMENT_CONSTRAINTS: mail-web placement (default
  ["node.role == manager"])."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKMAIL_ENV = ANSIBLE / "optional/skmail/src/config/skmail/skmail.env.j2"
SKMAIL_COMPOSE = ANSIBLE / "optional/skmail/src/config/skmail/skmail.yml.j2"
BASE_VARS = {"CLUSTERNAME": "skstack01", "DOMAIN": "example.com"}


def _tpl_env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)


def render_env(**overrides):
    return _tpl_env().from_string(SKMAIL_ENV.read_text()).render(
        app="skmail", env="prod", skmail=dict(BASE_VARS, **overrides))


def render_compose(env_name="prod", **overrides):
    out = _tpl_env().from_string(SKMAIL_COMPOSE.read_text()).render(
        env=env_name, skmail=dict(BASE_VARS, **overrides), fence_service_name="skfenceha")
    return yaml.safe_load(out)["services"]


def dms_resources(**overrides):
    return render_compose(**overrides)["skmail"]["deploy"]["resources"]


# --- resources -------------------------------------------------------------

def test_resources_default_unchanged():
    res = dms_resources()
    assert res["limits"] == {"cpus": "1.5", "memory": "3G"}
    assert res["reservations"] == {"cpus": "0.5", "memory": "1G"}


def test_resources_overridden_to_live_values_drops_reservations():
    res = dms_resources(RESOURCES_LIMITS_CPUS="1", RESOURCES_LIMITS_MEMORY="1G",
                        RESOURCES_RESERVATIONS_CPUS="", RESOURCES_RESERVATIONS_MEMORY="")
    assert res["limits"] == {"cpus": "1", "memory": "1G"}
    assert "reservations" not in res


def test_one_empty_reservation_is_omitted_the_other_kept():
    res = dms_resources(RESOURCES_RESERVATIONS_CPUS="")
    assert res["reservations"] == {"memory": "1G"}


# --- SSL_DOMAIN ------------------------------------------------------------

def test_ssl_domain_absent_by_default():
    assert "SSL_DOMAIN" not in render_env()


def test_ssl_domain_rendered_when_set():
    assert "SSL_DOMAIN=example.com\n" in render_env(SSL_DOMAIN="example.com")


def test_ssl_domain_empty_string_is_not_rendered():
    assert "SSL_DOMAIN" not in render_env(SSL_DOMAIN="")


# --- mail-web router TLS domains ------------------------------------------

def mail_web_labels(env_name="prod", **overrides):
    return render_compose(env_name, **overrides)["mail-web"]["deploy"]["labels"]


def test_no_tls_domain_labels_by_default():
    assert not any(".tls.domains[" in l for l in mail_web_labels())


@pytest.mark.parametrize("env_name", ["dev", "prod"])
def test_tls_domains_render_on_the_mail_web_router(env_name):
    labels = mail_web_labels(env_name, TLS_DOMAINS=[
        "example.com", {"main": "example.org", "sans": ["*.example.org", "mail.example.org"]}])
    r = f"traefik.http.routers.mail-web-{env_name}"
    assert f"{r}.tls.domains[0].main=example.com" in labels
    assert not any(l.startswith(f"{r}.tls.domains[0].sans") for l in labels)
    assert f"{r}.tls.domains[1].main=example.org" in labels
    assert f"{r}.tls.domains[1].sans=*.example.org,mail.example.org" in labels


# --- mail-web image --------------------------------------------------------

def test_mail_web_image_default_unchanged():
    assert render_compose()["mail-web"]["image"] == "traefik/whoami:v1.12.0"


def test_mail_web_image_overridable():
    img = "traefik/whoami:v1.11.0"
    assert render_compose(MAIL_WEB_IMAGE=img)["mail-web"]["image"] == img


# --- mail-web placement ----------------------------------------------------

def mail_web_constraints(**overrides):
    return render_compose(**overrides)["mail-web"]["deploy"]["placement"]["constraints"]


def test_mail_web_placement_default_unchanged():
    assert mail_web_constraints() == ["node.role == manager"]


def test_mail_web_placement_overridable_to_a_hostname_pin():
    want = ["node.hostname == mgr1", "node.role == manager"]
    assert mail_web_constraints(MAIL_WEB_PLACEMENT_CONSTRAINTS=want) == want


def test_dms_placement_is_untouched_by_the_mail_web_hook():
    svc = render_compose(MAIL_WEB_PLACEMENT_CONSTRAINTS=[])["skmail"]
    assert svc["deploy"]["placement"]["constraints"] == [
        "node.role == manager", "node.labels.mail-vip == true"]
