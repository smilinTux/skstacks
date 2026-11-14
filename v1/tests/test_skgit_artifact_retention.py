"""skgit caps Forgejo Actions artifact retention.

Forgejo's default is 90 days. A single e2e pipeline uploading about 80 MB
per run grew the data dir by 21 GB in a week, and every backup of it with
it, until a backup pool filled. The rendered app.ini (the file Forgejo
reads) must set ARTIFACT_RETENTION_DAYS, default 7, overridable per vault.
"""
import configparser
import pathlib

import jinja2

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
APP_INI = ANSIBLE / "optional/skgit/src/config/skgit/app.ini.j2"
ENV = ANSIBLE / "optional/skgit/src/config/skgit/skgit.env.j2"

SKGIT = {"CLUSTERNAME": "c", "DOMAIN": "example.com", "POSTGRES_PASSWORD": "pw",
         "SECRET_KEY": "secret", "INTERNAL_TOKEN": "internal"}


def _render(path, **over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    return env.from_string(path.read_text()).render(app="skgit", env="prod", skgit=dict(SKGIT, **over))


def _actions(**over):
    cfg = configparser.RawConfigParser(strict=True, interpolation=None)
    cfg.optionxform = str
    cfg.read_string("[__root__]\n" + _render(APP_INI, **over))
    return cfg["actions"]


def test_default_retention_is_a_week():
    assert _actions()["ARTIFACT_RETENTION_DAYS"] == "7"


def test_retention_is_a_vault_knob():
    assert _actions(ARTIFACT_RETENTION_DAYS=3)["ARTIFACT_RETENTION_DAYS"] == "3"


def test_env_file_matches_the_ini():
    assert "FORGEJO__actions__ARTIFACT_RETENTION_DAYS=7\n" in _render(ENV)
