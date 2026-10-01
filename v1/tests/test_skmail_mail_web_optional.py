"""skmail's mail-web sidecar is optional (skmail.MAIL_WEB_ENABLED).

mail-web is a traefik/whoami container behind a Traefik router for the mail
hostname. Its only job is to make Traefik's ACME resolver obtain a
certificate for that hostname on an instance that does not already hold one.
It is not needed to RENEW anything: Traefik's ACME provider renews every
certificate stored in acme.json on its own timer
(pkg/provider/acme/provider.go renewCertificates, v3.6.2), whether or not a
router still references it, and a router whose domain is already covered by
a stored certificate (for example a wildcard) never obtains a new one
(getUncheckedDomains). On such an instance mail-web does nothing for TLS and
whoami publishes each visitor's request headers, client IP included, to the
internet.

skmail.MAIL_WEB_ENABLED (default true, unchanged behaviour) set to false
drops the service and its router from the compose file, and the deploy
script removes a mail-web service left over from an earlier deploy (docker
stack deploy never removes a service that disappeared from the file).
"""
import pathlib
import subprocess

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKMAIL = ANSIBLE / "optional/skmail"
COMPOSE = SKMAIL / "src/config/skmail/skmail.yml.j2"
DEPLOY = SKMAIL / "src/skmail/deploy.j2"
CERT_SETUP = SKMAIL / "src/skmail/cert_setup.sh.j2"
README = SKMAIL / "README.md"
BASE = {"CLUSTERNAME": "demo", "DOMAIN": "example.test"}


def _render(path, env="prod", **overrides):
    j2 = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return j2.from_string(path.read_text()).render(
        env=env, skmail=dict(BASE, **overrides), fence_service_name="skfenceha")


def _services(**overrides):
    return yaml.safe_load(_render(COMPOSE, **overrides))["services"]


def test_default_keeps_mail_web():
    services = _services()
    assert "mail-web" in services
    labels = services["mail-web"]["deploy"]["labels"]
    assert any(".tls.certresolver=main" in label for label in labels)


@pytest.mark.parametrize("value", [False, "false", "no", 0])
def test_disabled_drops_mail_web_and_its_router(value):
    out = _render(COMPOSE, MAIL_WEB_ENABLED=value)
    services = yaml.safe_load(out)["services"]
    assert "mail-web" not in services
    assert "whoami" not in out
    assert "traefik.http.routers." not in out


def test_disabled_leaves_the_mail_server_service_identical():
    assert _services(MAIL_WEB_ENABLED=False)["skmail"] == _services()["skmail"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_deploy_script_removes_a_leftover_mail_web_only_when_disabled(env):
    disabled = _render(DEPLOY, env=env, MAIL_WEB_ENABLED=False)
    enabled = _render(DEPLOY, env=env)
    assert 'docker service rm "${STACK_NAME}_mail-web"' in disabled
    assert 'docker service inspect "${STACK_NAME}_mail-web"' in disabled
    assert "_mail-web" not in enabled
    for script in (disabled, enabled):
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)


def test_cert_setup_troubleshooting_does_not_point_at_a_disabled_mail_web():
    assert "mail-web" not in _render(CERT_SETUP, MAIL_WEB_ENABLED=False)
    assert "mail-web" in _render(CERT_SETUP)


def test_readme_documents_the_knob_and_the_renewal_mechanism():
    text = README.read_text()
    assert "MAIL_WEB_ENABLED" in text
    assert "renewCertificates" in text
