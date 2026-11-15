"""skgit's optional [service] knobs: DISABLE_REGISTRATION, REQUIRE_SIGNIN_VIEW.

An internet-facing Forgejo defaults to open self-registration and anonymous
read of every public repo. These two vault keys let an instance close both
from the rendered app.ini (a live-only edit is reverted by the next deploy,
which re-renders the whole file). Unset, the template renders exactly as
before: no [service] section at all.
"""
import configparser
import pathlib

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
APP_INI = ANSIBLE / "optional/skgit/src/config/skgit/app.ini.j2"

SKGIT = {
    "CLUSTERNAME": "skstack01",
    "DOMAIN": "example.com",
    "POSTGRES_PASSWORD": "pw",
    "SECRET_KEY": "secret",
    "INTERNAL_TOKEN": "internal",
}


def _render(**extra):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    tpl_env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    return tpl_env.from_string(APP_INI.read_text()).render(app="skgit", env="prod", skgit={**SKGIT, **extra})


def _parse(text):
    cfg = configparser.RawConfigParser(strict=True, interpolation=None)
    cfg.optionxform = str
    cfg.read_string("[__root__]\n" + text)
    return cfg


def test_unset_renders_no_service_section():
    assert "[service]" not in _render()


def test_both_knobs_render_lowercase_booleans():
    cfg = _parse(_render(DISABLE_REGISTRATION=True, REQUIRE_SIGNIN_VIEW="yes"))
    assert cfg.get("service", "DISABLE_REGISTRATION") == "true"
    assert cfg.get("service", "REQUIRE_SIGNIN_VIEW") == "true"


def test_one_knob_alone():
    cfg = _parse(_render(DISABLE_REGISTRATION=False))
    assert cfg.get("service", "DISABLE_REGISTRATION") == "false"
    assert not cfg.has_option("service", "REQUIRE_SIGNIN_VIEW")


def test_service_section_does_not_swallow_the_mailer():
    text = _render(DISABLE_REGISTRATION=True, email_enabled=True, email_host="smtp.example.com",
                   email_from="a@example.com", email_username="u", email_password="p")
    cfg = _parse(text)
    assert cfg.get("mailer", "ENABLED") == "true"
    assert not cfg.has_option("service", "ENABLED")
