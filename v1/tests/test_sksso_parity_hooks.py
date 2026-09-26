"""Live-parity hooks for sksso: prod runs a custom Authentik build
(<private registry>/authentik-capauth:2025.12.3, upstream Authentik + a
CapAuth PGP login stage) and needs CAPAUTH_* env vars the framework has no
place for. Both must be settable per-instance without touching the
framework, default = current framework behaviour."""
import pathlib

import jinja2
import yaml

SKSSO = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso"


def render_yml(**sksso):
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    base.update(sksso)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env="dev", app="sksso", sksso=base
    )
    return yaml.safe_load(out)["services"]


def render_env(**sksso):
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "postgres_user": "u", "postgres_password": "p", "authentik_secret_key": "k"}
    base.update(sksso)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string((SKSSO / "src/config/sksso/sksso.env.j2").read_text()).render(env="dev", app="sksso", sksso=base)


def test_image_defaults_to_authentik_version_when_unset():
    svcs = render_yml()
    assert svcs["server"]["image"] == "ghcr.io/goauthentik/server:2024.4.2"
    assert svcs["worker"]["image"] == "ghcr.io/goauthentik/server:2024.4.2"


def test_image_override_wins_on_server_and_worker():
    svcs = render_yml(IMAGE="registry.internal/authentik-capauth:2025.12.3")
    assert svcs["server"]["image"] == "registry.internal/authentik-capauth:2025.12.3"
    assert svcs["worker"]["image"] == "registry.internal/authentik-capauth:2025.12.3"


def test_extra_env_absent_by_default():
    out = render_env()
    assert "CAPAUTH_REQUIRE_APPROVAL" not in out
    assert "CAPAUTH_SERVICE_ID" not in out


def test_extra_env_passthrough_renders_verbatim():
    out = render_env(extra_env={"CAPAUTH_REQUIRE_APPROVAL": "true", "CAPAUTH_SERVICE_ID": "sksso-prod"})
    assert "CAPAUTH_REQUIRE_APPROVAL=true" in out
    assert "CAPAUTH_SERVICE_ID=sksso-prod" in out
