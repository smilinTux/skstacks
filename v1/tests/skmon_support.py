"""Shared helpers for the skmon role tests (render a template, load its compose, read a play)."""
import ast
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
    **{f"rules-{c}": f"rules/{c}.yml.j2" for c in ("node", "storage", "http", "cert", "capacity", "container", "meta")},
    "loki-rules": "loki-rules/storage.yml.j2",
}

# vmalert rule classes (one rules/<class>.yml.j2 each, mounted at /etc/vmalert/rules/<class>.yml).
RULE_CLASSES = tuple(k.removeprefix("rules-") for k in CONFIG_TEMPLATES if k.startswith("rules-"))
RULES = CFG / "rules"
# The DATA_PATH mountpoint the plays read with findmnt on DATA_NODE (a fake default for tests).
DATA_MOUNTPOINT = "/srv/local"


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


# A fake two-node inventory (RFC 5737 addresses) and the targets the plays derive from it.
FAKE_HOSTVARS = {
    "node-1.render.example.test": {"ansible_host": "192.0.2.10"},
    "node-2.render.example.test": {"ansible_host": "192.0.2.11"},
}


def agent_targets(hostvars: dict = FAKE_HOSTVARS, **skmon) -> list[dict]:
    """skmon_agent_targets as the plays derive it: evaluate tasks/agent_targets.yml over a fake inventory."""
    task = next(t for t in load_tasks(AGENT_TARGETS) if t.get("name") == "Derive the skmon agent scrape targets")
    groups = {"swarm_managers": list(hostvars)}
    facts = guard_facts(groups=groups, **skmon)
    jenv = ansible_environment()
    ctx = {**facts, "hostvars": hostvars, "groups": groups}
    return ast.literal_eval(jenv.from_string(task["set_fact"]["skmon_agent_targets"]).render(**ctx))  # as Ansible does


def ctx_with_configs(env: str = "prod", **skmon) -> dict:
    """Template context carrying `skmon_configs`: every config template rendered, as the plays do."""
    extra = {"skmon_agent_targets": agent_targets(**skmon), "skmon_data_mountpoint": DATA_MOUNTPOINT}
    configs = {key: render(CFG / name, env=env, extra_ctx=extra, **skmon) for key, name in CONFIG_TEMPLATES.items()}
    return {"skmon_configs": configs, **extra}


def render_rules(cls: str, env: str = "prod", mountpoint: str = DATA_MOUNTPOINT, **skmon) -> str:
    """One rendered vmalert rule file (rules/<cls>.yml.j2), as the plays render it."""
    return render(RULES / f"{cls}.yml.j2", env=env, extra_ctx={"skmon_data_mountpoint": mountpoint}, **skmon)


def all_rules(**skmon) -> list[dict]:
    """Every vmalert rule of every class file, rendered."""
    return [r for c in RULE_CLASSES for g in yaml.safe_load(render_rules(c, **skmon))["groups"] for r in g["rules"]]


def rules_named(name: str, **skmon) -> list[dict]:
    """Every rendered rule with this alert name (CertExpiringSoon and DiskFilling have two)."""
    return [r for r in all_rules(**skmon) if r.get("alert") == name]


def rule(name: str, **skmon) -> dict:
    """The first rendered rule with this alert name."""
    found = rules_named(name, **skmon)
    if not found:
        raise LookupError(f"no rule named {name!r}")
    return found[0]


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
AGENT_GUARDS = ROLE / "tasks/agent_guards.yml"

# Override keys that are top-level facts rather than vault keys. Anything else
# passed to guard_facts() is a vault key and lands in the `skmon` mapping.
_TOP_LEVEL_FACTS = {"swarm_nodes", "env", "groups"}
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
    ctx = {"app": "skmon", "env": "prod", "skmon": vault, "skmon_swarm_nodes": swarm, "groups": {}, **top}
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


# --- the Alloy agent (agent.yml) ----------------------------------------------

ALLOY = CFG / "alloy"
AGENT_CONFIG = ALLOY / "agent.alloy.j2"
AGENT_RUN = ALLOY / "skmon-alloy-run.sh.j2"
AGENT_UNIT = ALLOY / "skmon-alloy.service.j2"
SMART_SCRIPT = SRC / "skmon/smart_textfile.sh"
AGENT_PLAY = ROLE / "agent.yml"
AGENT_TARGETS = ROLE / "tasks/agent_targets.yml"

# The agent template context agent.yml builds for one host (fake values only).
AGENT_CTX = {
    "inventory_hostname": "node-2.render.example.test",
    "agent_mode": "container",
    "agent_light": False,
    "agent_is_data_node": False,
    "agent_is_storage": False,
    "agent_profiling": False,
    "loki_push_url": "http://192.0.2.1:3100/loki/api/v1/push",
    "probes": [],
}
# What the run script additionally reads (agent.yml sets these facts).
RUN_CTX = {
    "skmon_agent_listen_addr": "192.0.2.10",
    "skmon_agent_config_hash": "0" * 64,
}


def _split(kw: dict) -> tuple[dict, dict]:
    """Split keyword overrides into (top-level template context, vault keys)."""
    known = set(AGENT_CTX) | set(RUN_CTX) | {"skmon_parked", "skmon_data_node", "skmon_data_path"}
    top = {k: kw.pop(k) for k in list(kw) if k in known}
    return top, kw


def cfg(env: str = "prod", **kw) -> str:
    """agent.alloy rendered for one host; overrides: AGENT_CTX keys or vault keys."""
    top, vault = _split(dict(kw))
    return render(AGENT_CONFIG, env=env, extra_ctx={**AGENT_CTX, **top}, **vault)


def render_run_script(env: str = "prod", **kw) -> str:
    """The rendered skmon-<env>-alloy-run script; overrides: context facts or vault keys."""
    top, vault = _split(dict(kw))
    return render(AGENT_RUN, env=env, extra_ctx={**AGENT_CTX, **RUN_CTX, **top}, **vault)


def unit(env: str = "prod", **kw) -> str:
    """The systemd unit of the Alloy agent for one host; overrides as for cfg()."""
    top, vault = _split(dict(kw))
    return render(AGENT_UNIT, env=env, extra_ctx={**AGENT_CTX, **RUN_CTX, "agent_mode": "systemd", **top}, **vault)


def task_named(path: pathlib.Path, name: str) -> dict:
    """The one task of a play (or task file) with this exact name."""
    found = [t for t in (play_tasks(path) if path in (AGENT_PLAY, *PLAYS) else load_tasks(path)) if t.get("name") == name]
    if len(found) != 1:
        raise LookupError(f"expected one task named {name!r} in {path.name}, found {len(found)}")
    return found[0]


class RunArgs:
    """The options of a `docker run` command line: `a[flag]` is the last value,
    `flag in a` tests presence, `a.getall(flag)` lists every value. A flag given
    without a value (`--rm`) has the value True. Options after the image are the
    container command's own (`--server.http.listen-addr=...`) and land here too."""

    def __init__(self, pairs: list[tuple[str, object]], positional: list[str]):
        self.pairs, self.positional = pairs, positional

    def __getitem__(self, flag):
        vals = self.getall(flag)
        if not vals:
            raise KeyError(flag)
        return vals[-1]

    def __contains__(self, flag) -> bool:
        return any(f == flag for f, _ in self.pairs)

    def getall(self, flag) -> list:
        return [v for f, v in self.pairs if f == flag]


# docker run options that take no value (everything else in the run line takes one).
_DOCKER_BOOL_FLAGS = {"-d", "--detach", "--rm", "--privileged", "--init", "-i", "-t", "--read-only"}


def _docker_run_tokens(script: str) -> list[str]:
    import shlex
    joined = script.replace("\\\n", " ")
    lines = [l for l in joined.splitlines() if re.match(r"\s*docker run\b", l)]
    if len(lines) != 1:
        raise LookupError(f"expected exactly one `docker run` line, found {len(lines)}")
    return shlex.split(lines[0])[2:]


def run_script_args(**kw) -> RunArgs:
    """Parse the rendered run script's `docker run` line (see RunArgs)."""
    toks = _docker_run_tokens(render_run_script(**kw))
    pairs, positional, i = [], [], 0
    while i < len(toks):
        t = toks[i]
        if t.startswith("-") and "=" in t:
            f, v = t.split("=", 1)
            pairs.append((f, v))
        elif t in _DOCKER_BOOL_FLAGS or not t.startswith("-"):
            if t.startswith("-"):
                pairs.append((t, True))
            else:
                positional.append(t)
        else:
            pairs.append((t, toks[i + 1]))
            i += 1
        i += 1
    return RunArgs(pairs, positional)


def mounts(**kw) -> list[str]:
    """Every -v / --volume value of the rendered `docker run` line."""
    a = run_script_args(**kw)
    return [*a.getall("-v"), *a.getall("--volume")]


# --- the port allowlist (tasks/port_allowlist.yml, R36) -------------------------

PORT_ALLOWLIST = ROLE / "tasks/port_allowlist.yml"
FW_SCRIPT = CFG / "firewall/skmon-fw.sh.j2"
FW_UNIT = CFG / "firewall/skmon-fw.service.j2"


def fw_context(node: str | None = None, docker: bool = True, ports: dict | None = None, **skmon) -> dict:
    """Template context of the allowlist script: the set_fact tasks of port_allowlist.yml evaluated.

    node defaults to DATA_NODE (the host the env plays delegate to); docker stands for the
    install probe; ports, when given, replaces the derived protected-ports dict.
    """
    node = node or BASE["DATA_NODE"]
    facts = guard_facts(**skmon)
    ctx = {**facts, "skmon_agent_targets": agent_targets(**skmon), "skmon_fw_node": node,
           "skmon_fw_docker_probe": {"stdout": "yes" if docker else "no"}, "inventory_hostname": node}
    jenv = ansible_environment()
    for t in load_tasks(PORT_ALLOWLIST):
        for name, value in (t.get("set_fact") or {}).items():
            val = _eval_value(jenv, value, ctx)
            if isinstance(val, str) and val.startswith(("{", "[")):
                val = ast.literal_eval(val)  # a multi-statement block renders to a literal, as in Ansible
            ctx[name] = val
    if ports is not None:
        ctx["skmon_fw_ports"] = ports
    return ctx


def shell_body(env: str = "prod", **kw) -> str:
    """The rendered port allowlist script (see fw_context for the overrides)."""
    vault = {k: v for k, v in kw.items() if k not in ("node", "docker", "ports")}
    ctx = fw_context(env=env, **kw)
    return render(FW_SCRIPT, env=env, extra_ctx=ctx, **vault)


def rules(env: str = "prod", **kw) -> str:
    """The iptables ruleset text of the allowlist: the whole rendered script."""
    return shell_body(env=env, **kw)


def fw_unit(env: str = "prod", **kw) -> str:
    """The rendered skmon-<env>-fw.service unit."""
    vault = {k: v for k, v in kw.items() if k not in ("node", "docker", "ports")}
    return render(FW_UNIT, env=env, extra_ctx=fw_context(env=env, **kw), **vault)


# --- the deploy script and probe discovery (Task 9) -----------------------------

DEPLOY = SRC / "skmon/deploy.j2"
PROBES_PLUGIN = ROLE / "filter_plugins/skmon_probes.py"
V2_SERVICES = ("victoriametrics", "loki", "grafana", "alertmanager")


def render_deploy(env: str = "prod", **skmon) -> str:
    """The rendered deploy script; vault overrides as for render() (replicas=0 parks)."""
    return render(DEPLOY, env=env, **skmon)


def _load_probes_plugin():
    import importlib.util
    spec = importlib.util.spec_from_file_location("skmon_probes", PROBES_PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def probe_env() -> jinja2.Environment:
    """ansible_environment() plus what the probe tasks use: from_json and the skmon_probes filters."""
    import json
    jenv = ansible_environment()
    jenv.filters["from_json"] = json.loads
    jenv.filters.update(_load_probes_plugin().FilterModule().filters())
    return jenv


def probes_from(inspect: list, forward_auth_middleware: str = "authentik@file") -> list[dict]:
    """The probes the deploy plays derive from `docker service inspect` output (the plugin's filter)."""
    return _load_probes_plugin().skmon_probes_from_services(inspect, forward_auth_middleware)


def eval_play_fact(task_name: str, fact: str, play: pathlib.Path = PLAYS[0], **ctx):
    """Evaluate one set_fact of a deploy play over a context (R9: the play computes the fact)."""
    task = task_named(play, task_name)
    return _eval_value(probe_env(), task["set_fact"][fact], ctx)


def _svc(name: str, labels: dict) -> dict:
    """One `docker service inspect` entry (labels live under Spec.Labels)."""
    return {"ID": f"id-{name}", "Spec": {"Name": name, "Labels": labels}}


# What `docker service inspect` returns for a fake estate (example.test only).
FAKE_SERVICE_INSPECT = [
    _svc("app_web", {
        "traefik.http.routers.app-secure.rule": "Host(`app.render.example.test`)",
        "traefik.http.routers.app-secure.middlewares": "default-security-headers@file,gzip@file,authentik@file",
    }),
    _svc("public_site", {
        "traefik.http.routers.site-secure.rule": "Host(`www.render.example.test`) || Host(`render.example.test`)",
        "traefik.http.routers.site-secure.middlewares": "default-security-headers@file",
    }),
    _svc("api_gw", {
        "traefik.http.routers.api-secure.rule": "Host(`api.render.example.test`) && PathPrefix(`/v1`)",
    }),
    _svc("db", {"com.docker.stack.namespace": "db"}),
]
