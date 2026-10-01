"""skgit's app.ini must set WORK_PATH in the root (unnamed) section.

Forgejo reads WORK_PATH only from the root section (modules/setting/path.go,
``CfgProvider.Section("")``); a WORK_PATH under ``[server]`` is ignored. When
the root key is absent, ``forgejo web`` writes the effective work path back
into the root section of app.ini at startup (cmd/web.go), so every deploy
left a file that no longer matched what the playbook rendered, and the
template's ``[server] WORK_PATH`` only looked like it pinned the path. The
effective value comes from the image wrapper's ``GITEA_WORK_DIR`` today;
setting it where Forgejo reads it makes the rendered file the real config.
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


def _parse(env="prod"):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    tpl_env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("yes", "true", "1")
    text = tpl_env.from_string(APP_INI.read_text()).render(app="skgit", env=env, skgit=dict(SKGIT))
    cfg = configparser.RawConfigParser(strict=True, interpolation=None)
    cfg.optionxform = str
    cfg.read_string("[__root__]\n" + text)
    return cfg


def test_work_path_is_in_the_root_section():
    for env in ("dev", "staging", "prod"):
        cfg = _parse(env)
        assert cfg.get("__root__", "WORK_PATH", fallback=None) == "/data/gitea", env


def test_work_path_is_not_under_server():
    for env in ("dev", "staging", "prod"):
        cfg = _parse(env)
        assert not cfg.has_option("server", "WORK_PATH"), env


def test_root_section_holds_only_work_path():
    # Anything else placed before the first [section] would also land in the
    # root section, where Forgejo does not look for it.
    cfg = _parse()
    assert cfg.options("__root__") == ["WORK_PATH"]
