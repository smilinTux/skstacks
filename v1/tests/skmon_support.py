"""Shared helpers for the skmon role tests (render a template, load its compose, read a play)."""
import pathlib

import jinja2
import yaml

import test_compose_templates_schema as _schema

ROLE = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmon"
ANSIBLE = ROLE.parents[1]
SRC = ROLE / "src"
CFG = SRC / "config/skmon"
PLAYS = [ROLE / f"deploy_skmon-{e}.yml" for e in ("dev", "staging", "prod")]

# Minimal valid vault, fake values only.
BASE = {
    "CLUSTERNAME": "c1",
    "DOMAIN": "example.test",
    "ESTATE": "example",
    "DATA_NODE": "node-1.render.example.test",
    "GRAFANA_ADMIN_USER": "admin",
    "GRAFANA_ADMIN_PASSWORD": "fake-admin-password",
    "GRAFANA_SECRET_KEY": "fake-secret-key",
}


def render(tpl: pathlib.Path, env: str = "prod", extra_ctx: dict | None = None, **skmon) -> str:
    """Render a role template with the framework's filters and StrictUndefined."""
    jenv = _schema.make_environment()
    jenv.undefined = jinja2.StrictUndefined
    ctx = {"app": "skmon", "env": env, "skmon": {**BASE, **skmon}, **(extra_ctx or {})}
    return jenv.from_string(tpl.read_text()).render(**ctx)


def compose(**skmon) -> dict:
    """The whole rendered compose document."""
    return yaml.safe_load(render(CFG / "skmon.yml.j2", **skmon))


def services(**skmon) -> dict[str, dict]:
    """The rendered compose `services` mapping."""
    return compose(**skmon)["services"]


def _flatten(tasks):
    for t in tasks or []:
        if not isinstance(t, dict):
            continue
        yield t
        for key in ("block", "rescue", "always"):
            yield from _flatten(t.get(key))


def play_tasks(path: pathlib.Path) -> list[dict]:
    """Flat task list of the play whose vars.app is skmon."""
    for play in yaml.safe_load(path.read_text()):
        if isinstance(play, dict) and (play.get("vars") or {}).get("app") == "skmon":
            return list(_flatten([*(play.get("pre_tasks") or []), *(play.get("tasks") or []),
                                  *(play.get("post_tasks") or [])]))
    raise LookupError(f"no skmon play in {path}")
