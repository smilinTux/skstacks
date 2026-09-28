"""skmail's user-patches.sh (run by docker-mailserver at startup) waited up to
300s for /etc/dms/tls/certificate.crt and rewrote Dovecot's ssl_cert/ssl_key to
that directory. Only SSL_TYPE=manual (the traefik-certs-dumper bind) ever puts
certs there. With letsencrypt or self-signed, startup blocked past the
healthcheck window and Swarm killed the container as unhealthy in a loop
(skstack06 v2.19.0 rc4), and the Dovecot rewrite would point at missing files.
Both belong to SSL_TYPE=manual only."""
import pathlib

import jinja2
import pytest

TPL = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmail/src/config/skmail/user-patches.sh.j2"


def render(ssl_type=None):
    skmail = {"DOMAIN": "example.test"}
    if ssl_type:
        skmail["SSL_TYPE"] = ssl_type
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string(TPL.read_text()).render(skmail=skmail, env="dev", fence_service_name="skfenceha")


@pytest.mark.parametrize("ssl_type", [None, "letsencrypt", "self-signed"])
def test_no_cert_wait_or_dovecot_rewrite_unless_manual(ssl_type):
    out = render(ssl_type)
    assert "Waiting for SSL certificates" not in out
    assert "/etc/dms/tls/certificate.crt" not in out


def test_manual_keeps_the_wait_and_the_dovecot_paths():
    out = render("manual")
    assert "Waiting for SSL certificates" in out
    assert "ssl_cert = </etc/dms/tls/certificate.crt" in out


@pytest.mark.parametrize("ssl_type", [None, "letsencrypt", "self-signed", "manual"])
def test_common_permission_fixes_always_run(ssl_type):
    assert "-exec chown -h amavis:clamav {} +" in render(ssl_type)
