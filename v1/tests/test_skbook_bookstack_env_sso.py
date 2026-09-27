"""bookstack.env.j2's SSO block was missing four fields the live skbook-prod
instance actually has set (OIDC_DISPLAY_NAME_CLAIMS, OIDC_END_SESSION_ENDPOINT,
OIDC_SCOPES, AUTH_AUTO_INITIATE) and the mail block was missing MAIL_FROM_NAME:
a from-framework redeploy of that instance would silently narrow its SSO
behavior (no auto-initiate, wrong/default display claim, no configured
scopes) even though ENABLE_SSO and the core OIDC_CLIENT_* fields were already
supported. None of these five is a framework default: they render only when
the vault sets them, except AUTH_AUTO_INITIATE, which BookStack itself
defaults to false, so an explicit `false` here is not an estate-specific
guess.
"""
import pathlib

import jinja2
import pytest

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
BOOKSTACK_ENV = ANSIBLE / "optional/skbook/src/config/skbook/bookstack.env.j2"


def _ansible_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("yes", "true", "1", "on")


def render(**skbook_overrides):
    skbook = {
        "CLUSTERNAME": "skstack01",
        "DOMAIN": "example.com",
        "APP_KEY": "base64:test",
        "DB_DATABASE": "db",
        "DB_USER": "user",
        "DB_PASSWORD": "pass",
    }
    skbook.update(skbook_overrides)
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    env.filters["bool"] = _ansible_bool
    return env.from_string(BOOKSTACK_ENV.read_text()).render(app="skbook", env="prod", skbook=skbook)


SSO_BASE = dict(
    ENABLE_SSO=True,
    OIDC_CLIENT_ID="cid",
    OIDC_CLIENT_SECRET="secret",
    OIDC_ISSUER="https://idp.example.com",
)


def test_oidc_display_name_claims_rendered_when_set():
    out = render(**SSO_BASE, OIDC_DISPLAY_NAME_CLAIMS="name")
    assert "OIDC_DISPLAY_NAME_CLAIMS=name" in out


def test_oidc_display_name_claims_absent_when_unset():
    out = render(**SSO_BASE)
    assert "OIDC_DISPLAY_NAME_CLAIMS=" not in out


def test_oidc_end_session_endpoint_rendered_when_set():
    out = render(**SSO_BASE, OIDC_END_SESSION_ENDPOINT="true")
    assert "OIDC_END_SESSION_ENDPOINT=true" in out


def test_oidc_end_session_endpoint_absent_when_unset():
    out = render(**SSO_BASE)
    assert "OIDC_END_SESSION_ENDPOINT=" not in out


def test_oidc_scopes_rendered_when_set():
    out = render(**SSO_BASE, OIDC_SCOPES="openid email profile")
    assert "OIDC_SCOPES=openid email profile" in out


def test_oidc_scopes_absent_when_unset():
    out = render(**SSO_BASE)
    assert "OIDC_SCOPES=" not in out


def test_auth_auto_initiate_defaults_false_when_unset():
    out = render(**SSO_BASE)
    assert "AUTH_AUTO_INITIATE=false" in out


def test_auth_auto_initiate_rendered_when_set():
    out = render(**SSO_BASE, AUTH_AUTO_INITIATE="true")
    assert "AUTH_AUTO_INITIATE=true" in out


def test_mail_from_name_rendered_when_set():
    out = render(MAIL_HOST="smtp.example.com", MAIL_FROM_NAME="BookStack Wiki")
    assert "MAIL_FROM_NAME=BookStack Wiki" in out


def test_mail_from_name_absent_when_unset():
    out = render(MAIL_HOST="smtp.example.com")
    assert "MAIL_FROM_NAME" not in out


def test_sso_block_still_absent_when_disabled():
    out = render()
    assert "AUTH_METHOD=" not in out
    assert "OIDC_DISPLAY_NAME_CLAIMS=" not in out
    assert "AUTH_AUTO_INITIATE=" not in out


@pytest.mark.parametrize("extra", [{}, dict(OIDC_DISPLAY_NAME_CLAIMS="name", OIDC_SCOPES="openid",
                                             OIDC_END_SESSION_ENDPOINT="true", AUTH_AUTO_INITIATE="true")])
def test_still_renders_with_every_field_set_or_unset(extra):
    out = render(**SSO_BASE, **extra)
    assert "APP_KEY=" in out


def test_db_username_and_password_use_laravel_key_names():
    """BookStack's Laravel app reads standard Laravel .env keys (DB_USERNAME,
    DB_PASSWORD), confirmed against the live skbook-prod instance's own
    rendered bookstack.env. DB_USER/DB_PASS are not recognized and would
    leave BookStack unable to find its DB credentials at all."""
    out = render(**SSO_BASE)
    assert "DB_USERNAME=user" in out
    assert "DB_PASSWORD=pass" in out
    assert "DB_USER=" not in out
    assert "DB_PASS=" not in out


def test_cache_driver_rendered_when_set():
    out = render(CACHE_DRIVER="redis")
    assert "CACHE_DRIVER=redis" in out


def test_cache_driver_absent_when_unset():
    out = render()
    assert "CACHE_DRIVER=" not in out


def test_session_driver_rendered_when_set():
    out = render(SESSION_DRIVER="database")
    assert "SESSION_DRIVER=database" in out


def test_session_driver_absent_when_unset():
    out = render()
    assert "SESSION_DRIVER=" not in out


def test_storage_type_rendered_when_set():
    out = render(STORAGE_TYPE="s3")
    assert "STORAGE_TYPE=s3" in out


def test_storage_type_absent_when_unset():
    out = render()
    assert "STORAGE_TYPE=" not in out
