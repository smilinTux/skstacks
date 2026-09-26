"""Render every compose/stack template under v1/ansible/*/*/src/config/** across
many variable combinations (each boolean-looking flag flipped both ways, each
`is defined` check tried both ways, list/string defaults tried empty and
non-empty) for dev/staging/prod, then validate the rendered document against
the official Compose Specification JSON Schema plus a few Swarm-specific
checks that the schema itself doesn't cover.

This is the general framework version of four bugs a static check would have
caught in seconds, each of which instead cost a ~40 minute skstack06 VM run to
surface: skform rendering invalid YAML with SKSTOR_ENABLED false (a
whitespace-control tag ate the newline before `deploy:`), a Traefik dynamic
config with `tls:` null, a Jinja `section.items` resolving to dict.items
instead of the intended loop, and sksso's `deploy.placement` rendering null
("must be a mapping") when the worker constraint flag was false.

Discovery (which templates, which flags) is read straight from each .j2
source rather than hand-listed, so a new service or a new flag on an existing
one is covered with no edit to this file.
"""
import base64
import json
import pathlib
import re

import jinja2
import pytest
import yaml
from jsonschema import Draft202012Validator

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SCHEMA = json.loads((pathlib.Path(__file__).resolve().parent / "data" / "compose-spec.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA)

ENVS = ["dev", "staging", "prod"]
# names that show up as the left side of a dotted access in these templates
# but are never an external render var (loop vars, jinja builtins, or vars
# already supplied at the top level rather than as a namespace dict)
GENERIC_NAMES = {"env", "app", "item", "loop", "self", "range", "node", "cluster_name", "domain", "namespace"}
DUMMY = "test-value"


# ---------------------------------------------------------------------------
# Discovery: find the compose/stack templates, and the vars/flags each one
# references, by reading the raw .j2 source.
# ---------------------------------------------------------------------------

def discover_compose_templates():
    """A compose/stack template is any *.yml.j2 under an ansible role's
    src/config tree that defines a top-level `services:` key. Config-only
    templates (traefik dynamic config, prometheus.yml, grafana datasources,
    ...) live in the same directories but don't, and are out of scope here."""
    return sorted(
        f for f in ANSIBLE.glob("*/*/src/config/**/*.yml.j2")
        if re.search(r"(?m)^services:", f.read_text())
    )


def _jinja_blocks(text):
    """Every {{ ... }} and {% ... %} block's inner text, as (expr, stmt)
    pairs (exactly one side populated). Restricting the regexes below to
    these blocks keeps them from matching plain YAML/label text, such as a
    literal 'sslProxyHeaders.X-Forwarded-Proto' Traefik label."""
    return re.findall(r"\{\{-?(.*?)-?\}\}|\{%-?(.*?)-?%\}", text, re.DOTALL)


def _strip_string_literals(body):
    """Quoted literals like 'example.com' or "docker.io/..." must not be
    mistaken for a namespace.attr access ('example' + '.' + 'com')."""
    body = re.sub(r"'[^']*'", "''", body)
    return re.sub(r'"[^"]*"', '""', body)


def _local_set_vars(blocks):
    """Names bound by {% set NAME = ... %} (plain or namespace()): always
    local to the template, never something the caller needs to supply."""
    out = set()
    for _, stmt in blocks:
        m = re.match(r"\s*set\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*=", stmt)
        if m:
            out.add(m.group(1))
    return out


def _namespaces(blocks, local):
    ns = set()
    for expr, stmt in blocks:
        body = _strip_string_literals(expr if expr else stmt)
        for m in re.finditer(r"(?<![.\w])([a-zA-Z_][a-zA-Z0-9_-]*)\.[A-Za-z_]", body):
            ns.add(m.group(1))
    return ns - GENERIC_NAMES - local


def _bool_flags(blocks):
    """`KEY | default(true)` / `KEY | default(false)`: a real boolean flag
    with a known default, so both values are worth trying."""
    flags = {}
    for expr, stmt in blocks:
        body = expr if expr else stmt
        for m in re.finditer(r"([a-zA-Z_][a-zA-Z0-9_.-]*)\s*\|\s*default\((true|false)\)", body):
            flags[m.group(1)] = m.group(2) == "true"
    return flags


def _is_defined_flags(blocks):
    out = set()
    for expr, stmt in blocks:
        body = expr if expr else stmt
        out |= set(re.findall(r"([a-zA-Z_][a-zA-Z0-9_.-]*)\s+is\s+defined", body))
    return out


def _bare_if_flags(blocks, known):
    """`{% if ns.KEY %}` with no filter and no default: still a real branch
    point, just one whose default (falsy/undefined) is implicit."""
    out = set()
    for _, stmt in blocks:
        m = re.match(r"\s*if\s+([a-zA-Z_][a-zA-Z0-9_.]*)\s*$", stmt)
        if m and "." in m.group(1) and m.group(1) not in known:
            out.add(m.group(1))
    return out


def _list_default_flags(blocks):
    """`KEY | default([])`, almost always a `{% for %}` source: worth trying
    both empty and non-empty (sksso's placement_exclude_nodes)."""
    out = set()
    for expr, stmt in blocks:
        body = expr if expr else stmt
        out |= set(re.findall(r"([a-zA-Z_][a-zA-Z0-9_.-]*)\s*\|\s*default\(\[\]\)", body))
    return out


def _string_default_flags(blocks):
    """`KEY | default('')`, almost always gating an optional block (skdash's
    rollback/restart overrides, tls_options, sticky_samesite): worth trying
    both empty (the branch is skipped) and non-empty (the branch renders)."""
    out = set()
    for expr, stmt in blocks:
        body = expr if expr else stmt
        out |= set(re.findall(r"([a-zA-Z_][a-zA-Z0-9_.-]*)\s*\|\s*default\((?:''|\"\")\)", body))
    return out


def _bare_toplevel_vars(blocks, local):
    """A bare `{{ name }}` (no dot, no filter): if it's not a local {% set
    %} var, something outside the template has to provide it."""
    out = set()
    for expr, _ in blocks:
        if not expr:
            continue
        m = re.match(r"\s*([a-z_][a-z0-9_]*)\s*$", expr)
        if m:
            out.add(m.group(1))
    return out - GENERIC_NAMES - local


def _set_path(ctx, path, value):
    parts = path.split(".", 1)
    if len(parts) == 1:
        ctx[parts[0]] = value
    else:
        ns, key = parts
        ctx.setdefault(ns, {})[key] = value


def build_cases(template):
    """(label, context) pairs: the baseline (every flag at its template
    default) plus each discovered flag flipped away from its default, one
    flag at a time rather than the full power set."""
    text = template.read_text()
    blocks = _jinja_blocks(text)
    local = _local_set_vars(blocks)
    namespaces = _namespaces(blocks, local) | {template.parent.name}
    bool_flags = _bool_flags(blocks)
    is_defined = _is_defined_flags(blocks)
    bare_ifs = _bare_if_flags(blocks, {**bool_flags, **dict.fromkeys(is_defined)})
    list_flags = _list_default_flags(blocks)
    string_flags = _string_default_flags(blocks)
    toplevel_vars = _bare_toplevel_vars(blocks, local)

    def base():
        ctx = {"app": template.parent.name, "cluster_name": "cluster1", "domain": "example.com"}
        for ns in namespaces:
            ctx[ns] = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
        for name in toplevel_vars:
            ctx[name] = DUMMY
        return ctx

    cases = [("baseline", base())]
    for path, default in bool_flags.items():
        for value in (True, False):
            ctx = base()
            _set_path(ctx, path, value)
            cases.append((f"{path}={value}", ctx))
    for path in sorted(is_defined):
        ctx = base()
        _set_path(ctx, path, DUMMY)
        cases.append((f"{path}=defined", ctx))
        cases.append((f"{path}=undefined", base()))
    for path in sorted(bare_ifs):
        for value in (True, False):
            ctx = base()
            _set_path(ctx, path, value)
            cases.append((f"{path}={value}", ctx))
    for path in sorted(list_flags):
        ctx = base()
        _set_path(ctx, path, ["test-node"])
        cases.append((f"{path}=[test-node]", ctx))
    for path in sorted(string_flags):
        ctx = base()
        _set_path(ctx, path, DUMMY)
        cases.append((f"{path}=nonempty", ctx))
    return cases


# ---------------------------------------------------------------------------
# Rendering: ansible's own jinja2 defaults, plus a shim for the Ansible-only
# filters templates lean on that vanilla jinja2 doesn't ship (none of the 17
# compose templates currently use these, but a config template elsewhere in
# the tree does, and a future compose template reasonably could).
# ---------------------------------------------------------------------------

def _bool_filter(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("yes", "true", "1", "on")
    return bool(value)


def make_environment():
    env = jinja2.Environment(
        undefined=jinja2.ChainableUndefined,
        trim_blocks=True,
        extensions=["jinja2.ext.do"],
    )
    env.filters["bool"] = _bool_filter
    env.filters["to_json"] = lambda v: json.dumps(v)
    env.filters["to_yaml"] = lambda v: yaml.safe_dump(v)
    env.filters["to_nice_yaml"] = lambda v, indent=2, **_: yaml.safe_dump(v, indent=indent, default_flow_style=False)
    env.filters["regex_replace"] = lambda v, pattern, repl: re.sub(pattern, repl, str(v))
    env.filters["b64encode"] = lambda v: base64.b64encode(str(v).encode()).decode()
    return env


def render(template, env_name, ctx):
    jenv = make_environment()
    full_ctx = dict(ctx)
    full_ctx["env"] = env_name
    return jenv.from_string(template.read_text()).render(**full_ctx)


# ---------------------------------------------------------------------------
# Swarm-relevant checks the Compose Specification schema doesn't enforce
# (the schema describes the file format; these are the extra rules that
# matter specifically for `docker stack deploy`).
# ---------------------------------------------------------------------------

def check_swarm(doc, errors):
    # "if present" throughout means "if the key exists", not "if not None":
    # a template bug renders `placement:` with nothing after it just as
    # easily as it omits `placement:` altogether, and yaml.safe_load turns
    # the former into {"placement": None} -- a key that IS present, with a
    # value that is emphatically not a mapping. Checking `is not None`
    # instead of `in` would silently skip exactly that case.
    services = doc.get("services") or {}
    top_networks = set((doc.get("networks") or {}).keys())
    for name, svc in services.items():
        if not isinstance(svc, dict):
            errors.append(f"service {name}: is {svc!r}, not a mapping")
            continue
        if "build" in svc:
            errors.append(f"service {name}: has build: (swarm ignores it, so it's either dead or a mistake)")

        if "deploy" in svc:
            deploy = svc["deploy"]
            if not isinstance(deploy, dict):
                errors.append(f"service {name}: deploy is {deploy!r}, not a mapping")
            else:
                if "placement" in deploy:
                    placement = deploy["placement"]
                    if not isinstance(placement, dict):
                        errors.append(f"service {name}: deploy.placement is {placement!r}, not a mapping")
                    elif "constraints" in placement:
                        constraints = placement["constraints"]
                        if not isinstance(constraints, list) or not all(isinstance(c, str) for c in constraints):
                            errors.append(f"service {name}: deploy.placement.constraints is {constraints!r}")
                check_labels(name, "deploy", deploy, "labels", errors)

        net_field = svc.get("networks")
        if isinstance(net_field, list):
            referenced = net_field
        elif isinstance(net_field, dict):
            referenced = list(net_field.keys())
        else:
            referenced = []
        for net in referenced:
            if net not in top_networks:
                errors.append(f"service {name}: network {net!r} used but not declared at top level")

        check_labels(name, "service", svc, "labels", errors)


def check_labels(service, where, container, key, errors):
    if key not in container:
        return
    labels = container[key]
    where = f"{where}.{key}"
    if isinstance(labels, list):
        bad = [entry for entry in labels if not isinstance(entry, str)]
    elif isinstance(labels, dict):
        bad = [value for value in labels.values() if not isinstance(value, str)]
    else:
        errors.append(f"service {service}: {where} is {labels!r}, not a list or mapping")
        return
    if bad:
        errors.append(f"service {service}: {where} has non-string entries: {bad!r}")


# ---------------------------------------------------------------------------
# The test itself: one parametrized case per (template, env, flag combo).
# ---------------------------------------------------------------------------

def pytest_generate_tests(metafunc):
    if "case" not in metafunc.fixturenames:
        return
    argvalues = []
    ids = []
    for template in discover_compose_templates():
        for label, ctx in build_cases(template):
            for env_name in ENVS:
                argvalues.append((template, env_name, ctx))
                ids.append(f"{template.parent.name}/{env_name}/{label}")
    metafunc.parametrize("case", argvalues, ids=ids)


def test_compose_template_schema_and_swarm(case):
    template, env_name, ctx = case

    try:
        rendered = render(template, env_name, ctx)
    except Exception as exc:
        pytest.fail(f"{template}: render raised for env={env_name} ctx={ctx}: {exc}")

    try:
        doc = yaml.safe_load(rendered)
    except yaml.YAMLError as exc:
        pytest.fail(f"{template}: rendered output is not valid YAML for env={env_name} ctx={ctx}: {exc}")

    assert isinstance(doc, dict), f"{template}: rendered document is {doc!r}, not a mapping (env={env_name} ctx={ctx})"
    assert "services" in doc, f"{template}: rendered document has no services: key (env={env_name} ctx={ctx})"

    schema_errors = sorted(VALIDATOR.iter_errors(doc), key=lambda e: list(map(str, e.path)))
    if schema_errors:
        detail = "; ".join(f"{'/'.join(map(str, e.path)) or '<root>'}: {e.message}" for e in schema_errors)
        pytest.fail(f"{template} env={env_name} ctx={ctx}: compose-spec schema violations: {detail}")

    swarm_errors = []
    check_swarm(doc, swarm_errors)
    if swarm_errors:
        pytest.fail(f"{template} env={env_name} ctx={ctx}: swarm violations: {'; '.join(swarm_errors)}")


def test_discovery_finds_the_known_compose_templates():
    """Guards the discovery step itself: if the glob or the services: sniff
    ever silently found zero templates, every case above would vacuously
    pass and this whole file would stop meaning anything."""
    templates = discover_compose_templates()
    names = {t.parent.name for t in templates}
    assert len(templates) >= 15, templates
    assert {"skfence", "sksso", "skform", "skdash", "skhub"} <= names, names
