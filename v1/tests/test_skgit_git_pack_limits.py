"""skgit renders bounded git pack limits into app.ini [git.config].

nor skgit was OOM-killed on 2026-11-11 by concurrent repacks. The fix was a
hand edit to the live app.ini, which the next re-render dropped, so the
limits must come from the template. skgit.GIT_CONFIG overrides or adds keys.
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

DEFAULTS = {
    "pack.threads": "1",
    "pack.windowMemory": "256m",
    "pack.deltaCacheSize": "64m",
    "core.deltaBaseCacheLimit": "64m",
    "gc.auto": "0",
}


def _render(**extra):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    tpl_env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    return tpl_env.from_string(APP_INI.read_text()).render(app="skgit", env="prod", skgit={**SKGIT, **extra})


def _git_config(text):
    cfg = configparser.RawConfigParser(strict=True, interpolation=None)
    cfg.optionxform = str
    cfg.read_string("[__root__]\n" + text)
    return dict(cfg.items("git.config"))


def test_defaults_render():
    assert _git_config(_render()) == DEFAULTS


def test_override_replaces_a_default_once():
    got = _git_config(_render(GIT_CONFIG={"pack.windowMemory": "512m"}))
    assert got["pack.windowMemory"] == "512m"
    assert got["pack.threads"] == "1"
    assert len(got) == len(DEFAULTS)


def test_override_adds_a_key():
    got = _git_config(_render(GIT_CONFIG={"uploadpack.allowFilter": "true"}))
    assert got["uploadpack.allowFilter"] == "true"
    assert len(got) == len(DEFAULTS) + 1


def test_empty_override_keeps_defaults():
    assert _git_config(_render(GIT_CONFIG=None)) == DEFAULTS


def test_git_section_unchanged():
    cfg = configparser.RawConfigParser(strict=True, interpolation=None)
    cfg.optionxform = str
    cfg.read_string("[__root__]\n" + _render())
    assert cfg.get("git", "MAX_GIT_DIFF_LINES") == "1000000"
    assert cfg.get("git.timeout", "GC") == "3600"
