"""A compose/stack template must never put a secret-looking value on a
process's own command line: `docker service inspect` ContainerSpec.Args and
/proc/<pid>/cmdline are both readable by anyone with node or API read access,
unlike an environment variable or a mode-0640 mounted config file. Found live
on prod: skorch's redis started as `redis-server --requirepass <password>`.

Reuses test_compose_templates_schema.py's own template discovery and jinja
plumbing. Renders each template with a sentinel value standing in for every
var whose name looks like a secret (password/secret/token/key/...), then
asserts the sentinel never shows up in any service's command, entrypoint, or
healthcheck.test.
"""
import re

import pytest
import yaml

from test_compose_templates_schema import (
    discover_compose_templates,
    _jinja_blocks,
    _local_set_vars,
    _namespaces,
    _set_path,
    render,
)

SENTINEL = "SENTINEL_SECRET_123"
SECRET_NAME = re.compile(r"(password|passwd|secret|token|apikey|api_key|_pw$)", re.IGNORECASE)


def _secret_paths(template):
    """Every dotted (namespace.attr) var path referenced in the template
    whose last segment looks like a secret."""
    text = template.read_text()
    blocks = _jinja_blocks(text)
    local = _local_set_vars(blocks)
    namespaces = _namespaces(blocks, local) | {template.parent.name}
    paths = set()
    for expr, stmt in blocks:
        body = expr if expr else stmt
        if not body:
            continue
        for m in re.finditer(r"[a-zA-Z_][a-zA-Z0-9_]*(?:\.[a-zA-Z_][a-zA-Z0-9_]*)*", body):
            path = m.group(0)
            last = path.rsplit(".", 1)[-1]
            if not SECRET_NAME.search(last):
                continue
            if "." not in path:
                continue  # a bare name with no namespace: not one of ours to inject
            head = path.split(".", 1)[0]
            if head not in namespaces:
                continue
            paths.add(path)
    return sorted(paths)


def _base_ctx(template):
    blocks = _jinja_blocks(template.read_text())
    local = _local_set_vars(blocks)
    namespaces = _namespaces(blocks, local) | {template.parent.name}
    ctx = {"app": template.parent.name, "cluster_name": "cluster1", "domain": "example.com"}
    for ns in namespaces:
        ctx[ns] = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    return ctx


def _argv_strings(doc):
    """Every command/entrypoint/healthcheck.test value in the rendered
    document, flattened to strings."""
    out = []
    for svc in (doc.get("services") or {}).values():
        if not isinstance(svc, dict):
            continue
        for key in ("command", "entrypoint"):
            val = svc.get(key)
            if isinstance(val, str):
                out.append(val)
            elif isinstance(val, list):
                out.extend(str(v) for v in val)
        hc = svc.get("healthcheck")
        if isinstance(hc, dict):
            test = hc.get("test")
            if isinstance(test, str):
                out.append(test)
            elif isinstance(test, list):
                out.extend(str(v) for v in test)
    return out


TEMPLATES_WITH_SECRETS = [t for t in discover_compose_templates() if _secret_paths(t)]


@pytest.mark.parametrize("template", TEMPLATES_WITH_SECRETS, ids=lambda t: t.parent.name)
def test_no_secret_in_argv(template):
    ctx = _base_ctx(template)
    for path in _secret_paths(template):
        _set_path(ctx, path, SENTINEL)
    rendered = render(template, "dev", ctx)
    doc = yaml.safe_load(rendered)
    leaked = [s for s in _argv_strings(doc) if SENTINEL in s]
    assert not leaked, f"{template}: secret leaked into command/entrypoint/healthcheck: {leaked}"


def test_discovery_finds_a_secret_bearing_template():
    """Guards the discovery+detection step itself: if it ever found zero
    templates with a secret-looking var, every case above would vacuously
    pass and this file would stop meaning anything."""
    assert TEMPLATES_WITH_SECRETS, "no compose templates with a secret-looking var were found"
