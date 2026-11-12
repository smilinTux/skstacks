"""Shared helpers for the skmon role tests (render a template, load its compose, read a play)."""
import pathlib
import re

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
    "GRAFANA_ROOT_URL": "https://skmon.c1.example.test",
}

# Config templates delivered as hash-named Swarm configs: key -> template file.
CONFIG_TEMPLATES = {
    "vm-scrape": "vm-scrape.yml.j2",
    "loki": "loki-config.yml.j2",
    "alertmanager": "alertmanager-config.yml.j2",
}


def render(tpl: pathlib.Path, env: str = "prod", extra_ctx: dict | None = None, **skmon) -> str:
    """Render a role template with the framework's filters and StrictUndefined.

    The data facts the plays set (skmon_data_node, skmon_data_path, skmon_parked)
    are derived from the vault the same way tasks/facts.yml does, unless
    extra_ctx overrides them.
    """
    jenv = _schema.make_environment()
    jenv.undefined = jinja2.StrictUndefined
    facts = guard_facts(env=env, **dict(skmon))
    derived = {k: facts[k] for k in ("skmon_data_node", "skmon_data_path", "skmon_parked")}
    ctx = {"app": "skmon", "env": env, "skmon": {**BASE, **skmon}, **derived, **(extra_ctx or {})}
    return jenv.from_string(tpl.read_text()).render(**ctx)


def ctx_with_configs(env: str = "prod", **skmon) -> dict:
    """Template context carrying `skmon_configs`: every config template rendered, as the plays do."""
    configs = {key: render(CFG / name, env=env, **skmon) for key, name in CONFIG_TEMPLATES.items()}
    return {"skmon_configs": configs}


def compose(**skmon) -> dict:
    """The whole rendered compose document (configs rendered the way the plays do)."""
    return yaml.safe_load(render(CFG / "skmon.yml.j2", extra_ctx=ctx_with_configs(**skmon), **skmon))


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


# --- guard evaluation (a small Ansible stand-in, not a full emulator) ---------

FACTS_FILE = ROLE / "tasks/facts.yml"
GUARDS = ROLE / "tasks/guards.yml"
DATA_DIRS = ROLE / "tasks/data_dirs.yml"

# Override keys that are top-level facts rather than vault keys. Anything else
# passed to guard_facts() is a vault key and lands in the `skmon` mapping.
_TOP_LEVEL_FACTS = {"swarm_nodes", "env"}
_TOP_LEVEL_PREFIXES = ("skmon_", "skalert_")


def load_tasks(path: pathlib.Path) -> list[dict]:
    """Flat task list of a role task file."""
    return list(_flatten(yaml.safe_load(path.read_text())))


def ansible_environment() -> jinja2.Environment:
    """The framework's jinja environment plus the Ansible filters/tests the skmon task files use."""
    jenv = _schema.make_environment()
    jenv.filters["select"] = lambda seq, test=None, *a: [
        x for x in seq if (jenv.tests[test](x, *a) if test else x)
    ]
    jenv.filters["int"] = lambda v, default=0: int(v) if str(v).lstrip("-").isdigit() else default
    jenv.tests["match"] = lambda v, pattern: re.match(pattern, str(v)) is not None
    jenv.tests["regex"] = lambda v, pattern: re.search(pattern, str(v)) is not None
    return jenv


def _eval_value(jenv: jinja2.Environment, value, ctx: dict):
    """Template a set_fact value; a lone `{{ expr }}` keeps its native type (as Ansible does)."""
    if not isinstance(value, str):
        return value
    m = re.fullmatch(r"\s*\{\{(.*)\}\}\s*", value, re.S)
    if m and "{{" not in m.group(1):
        return jenv.compile_expression(m.group(1).strip(), undefined_to_none=False)(**ctx)
    return jenv.from_string(value).render(**ctx)


def guard_facts(**overrides) -> dict:
    """Facts the guard tasks see, derived by evaluating tasks/facts.yml over BASE plus overrides.

    Vault keys (DATA_NODE, DATA_PATH, replicas, HEALTHZ_ALLOW, retired keys, ...)
    land in the `skmon` mapping. Non-vault facts land at the top level: `env`,
    `swarm_nodes` (which also becomes `skmon_swarm_nodes`, the fact guards.yml
    derives from the registered `docker node ls`, defaulting to just DATA_NODE),
    and any `skmon_*` / `skalert_*` key (e.g. `skalert_actual_sha256`).
    """
    top = {k: overrides.pop(k) for k in list(overrides)
           if k in _TOP_LEVEL_FACTS or k.startswith(_TOP_LEVEL_PREFIXES)}
    vault = {**BASE, **overrides}
    swarm = top.pop("swarm_nodes", [vault.get("DATA_NODE", "")])
    ctx = {"app": "skmon", "env": "prod", "skmon": vault, "skmon_swarm_nodes": swarm, **top}
    jenv = ansible_environment()
    for t in load_tasks(FACTS_FILE):
        for name, value in (t.get("set_fact") or {}).items():
            ctx[name] = _eval_value(jenv, value, ctx)
    return ctx


def _holds(jenv, cond, ctx) -> bool:
    conds = cond if isinstance(cond, list) else [cond]
    return all(bool(jenv.compile_expression(str(c))(**ctx)) for c in conds)


def eval_asserts(task_file: pathlib.Path, facts: dict) -> list[str]:
    """Rendered fail_msg/msg of every failing `assert`, and of every `fail` whose `when` holds.

    Include/import tasks and everything that is not assert/fail are skipped; a
    `when` on an assert is honoured.
    """
    jenv = ansible_environment()
    out = []
    for t in load_tasks(task_file):
        if "when" in t and not _holds(jenv, t["when"], facts):
            continue
        if "assert" in t:
            spec = t["assert"]
            if _holds(jenv, spec["that"], facts):
                continue
            msg = spec.get("fail_msg") or spec.get("msg") or "Assertion failed"
        elif "fail" in t and "when" in t:
            msg = (t["fail"] or {}).get("msg", "Failed as requested")
        else:
            continue
        out.append(jenv.from_string(msg).render(**facts))
    return out


def data_dir_items() -> list[dict]:
    """The {sub, owner, mode} loop items of tasks/data_dirs.yml, rendered against BASE."""
    jenv = ansible_environment()
    ctx = {"skmon": BASE}
    items = []
    for t in load_tasks(DATA_DIRS):
        for item in t.get("loop") or []:
            items.append({k: _eval_value(jenv, v, ctx) for k, v in item.items()})
    return items
