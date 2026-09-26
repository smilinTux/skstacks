"""skmail hardcoded SSL_TYPE=letsencrypt (reading Traefik's acme.json), so an
instance without ACME -- a test instance, or one bringing its own certs --
had no clean path; the skstack06 test instance had to hack a self-signed
cert into Traefik's acme.json. skmail.SSL_TYPE (default letsencrypt,
unchanged behaviour) makes docker-mailserver's own `manual` and
`self-signed` TLS providers first-class: manual mounts
SSL_CERT_PATH/SSL_KEY_PATH read-only, self-signed relies on the framework
placing files under docker-data/dms/config/ssl/ (mounted whole at
/tmp/docker-mailserver already) -- and the Traefik-acme.json preflight
(which only makes sense for letsencrypt) becomes conditional.
Docs: https://docker-mailserver.github.io/docker-mailserver/latest/config/security/ssl/
(SSL_TYPE=letsencrypt/manual/self-signed, required files/paths per type)."""
import pathlib
import subprocess

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKMAIL_ENV = ANSIBLE / "optional/skmail/src/config/skmail/skmail.env.j2"
SKMAIL_COMPOSE = ANSIBLE / "optional/skmail/src/config/skmail/skmail.yml.j2"
PLAYBOOKS = {
    env_name: ANSIBLE / "optional/skmail" / f"deploy_skmail-{env_name}.yml"
    for env_name in ("dev", "staging", "prod")
}

BASE_VARS = {
    "CLUSTERNAME": "skstack01",
    "DOMAIN": "example.com",
}


def _tpl_env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)


def render_env(**skmail_overrides):
    skmail = dict(BASE_VARS, **skmail_overrides)
    return _tpl_env().from_string(SKMAIL_ENV.read_text()).render(app="skmail", env="prod", skmail=skmail)


def render_compose(env_name="prod", fence_service_name="skfenceha", **skmail_overrides):
    skmail = dict(BASE_VARS, **skmail_overrides)
    out = _tpl_env().from_string(SKMAIL_COMPOSE.read_text()).render(
        env=env_name, skmail=skmail, fence_service_name=fence_service_name)
    return yaml.safe_load(out), out


ORIGINAL_ENV_LETSENCRYPT_LINES = [
    "SSL_TYPE=letsencrypt",
    "# No SSL_CERT_PATH/SSL_KEY_PATH needed - DMS extracts from acme.json automatically",
    "ONE_DIR=1",
]


def test_default_env_is_letsencrypt_unchanged():
    text = render_env()
    for line in ORIGINAL_ENV_LETSENCRYPT_LINES:
        assert line in text
    assert "SSL_CERT_PATH=" not in text
    assert "SSL_KEY_PATH=" not in text


def test_default_compose_mounts_acme_json_unchanged():
    doc, _ = render_compose()
    svc = doc["services"]["skmail"]
    assert "/var/data/runtime/skfenceha-prod/acme/acme.json:/etc/letsencrypt/acme.json:ro" in svc["volumes"]
    assert not any("skmail-tls" in v for v in svc["volumes"])


def test_manual_env_sets_cert_and_key_path():
    text = render_env(SSL_TYPE="manual")
    assert "SSL_TYPE=manual" in text
    assert "SSL_CERT_PATH=/etc/skmail-tls/cert.pem" in text
    assert "SSL_KEY_PATH=/etc/skmail-tls/key.pem" in text


def test_manual_compose_mounts_host_paths_read_only_and_no_acme():
    doc, _ = render_compose(
        SSL_TYPE="manual",
        SSL_CERT_PATH="/srv/tls/mail/fullchain.pem",
        SSL_KEY_PATH="/srv/tls/mail/privkey.pem",
    )
    svc = doc["services"]["skmail"]
    assert "/srv/tls/mail/fullchain.pem:/etc/skmail-tls/cert.pem:ro" in svc["volumes"]
    assert "/srv/tls/mail/privkey.pem:/etc/skmail-tls/key.pem:ro" in svc["volumes"]
    assert not any("acme.json" in v for v in svc["volumes"])


def test_self_signed_env_has_no_manual_paths_and_no_acme_mount():
    text = render_env(SSL_TYPE="self-signed")
    assert "SSL_TYPE=self-signed" in text
    assert "SSL_CERT_PATH" not in text
    assert "SSL_KEY_PATH" not in text


def test_self_signed_compose_has_no_acme_mount_and_no_manual_mount():
    doc, _ = render_compose(SSL_TYPE="self-signed")
    svc = doc["services"]["skmail"]
    assert not any("acme.json" in v for v in svc["volumes"])
    assert not any("skmail-tls" in v for v in svc["volumes"])
    # already covered: the whole config dir (including ssl/) is bind-mounted
    assert any(v.endswith(":/tmp/docker-mailserver") for v in svc["volumes"])


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_cert_preflight_tasks_are_conditional_on_letsencrypt(env_name):
    doc = yaml.safe_load(PLAYBOOKS[env_name].read_text())
    tasks = {t["name"]: t for play in doc if "tasks" in play for t in play["tasks"]}
    for name in [
        "Find actual certificate directory (prefer mail.example.com, fallback to wildcard)",
        "Set actual certificate directory path",
        "Create certificate setup script",
        "Execute certificate setup script",
    ]:
        assert name in tasks, f"missing task {name!r}"
        when = str(tasks[name].get("when", ""))
        assert "letsencrypt" in when, f"{name!r} must be conditional on SSL_TYPE == letsencrypt, got when={when!r}"


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_self_signed_cert_generation_task_exists_and_is_conditional(env_name):
    doc = yaml.safe_load(PLAYBOOKS[env_name].read_text())
    tasks = {t["name"]: t for play in doc if "tasks" in play for t in play["tasks"]}
    matches = [t for n, t in tasks.items() if "self-signed" in n.lower() or "self signed" in n.lower()]
    assert matches, "expected a self-signed cert generation task in the deploy playbook"
    task = matches[0]
    when = str(task.get("when", ""))
    assert "self-signed" in when


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_playbook_syntax_check(env_name):
    r = subprocess.run(
        ["ansible-playbook", "--syntax-check", str(PLAYBOOKS[env_name])],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr
