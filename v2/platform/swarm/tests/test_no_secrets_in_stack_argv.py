"""Every Swarm stack's command/entrypoint/healthcheck.test must never carry a
secret-looking value: `docker service inspect` ContainerSpec.Args and
/proc/<pid>/cmdline are both readable by anyone with node or API read access.
Found live on prod: skcache's valkey started as
`valkey-server --requirepass <password>`, with the same password again on
`valkey-cli -a <password>` in its healthcheck.

Simulates docker compose's own ${VAR} interpolation with a sentinel for every
var whose name looks like a secret, then checks the rendered command/
entrypoint/healthcheck.test strings for the sentinel. `$$VAR` (compose's
escape for a literal `$VAR` left for the container's own shell, used
elsewhere in this framework, e.g. skbus/skmem-pg backup scripts) is left
untouched, matching real `docker stack deploy` interpolation.
"""
import re
from pathlib import Path

import pytest
import yaml

STACKS = Path(__file__).resolve().parents[1] / "stacks"
SENTINEL = "SENTINEL_SECRET_123"
SECRET_VAR = re.compile(r"(PASSWORD|PASSWD|SECRET|TOKEN|API_?KEY)", re.IGNORECASE)


def _stack_files():
    return sorted(STACKS.glob("*/docker-compose.yml"))


def _secret_vars(text):
    # ${VAR} or ${VAR:-default}, never $$VAR (compose's escape for a literal $).
    names = set(re.findall(r"(?<!\$)\$\{([A-Za-z_][A-Za-z0-9_]*)", text))
    return {n for n in names if SECRET_VAR.search(n)}


def _render(text):
    for name in _secret_vars(text):
        text = re.sub(r"(?<!\$)\$\{" + re.escape(name) + r"(:-[^}]*)?\}", SENTINEL, text)
    return text


def _argv_strings(doc):
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


STACKS_WITH_SECRETS = [f for f in _stack_files() if _secret_vars(f.read_text())]


@pytest.mark.parametrize("stack_file", STACKS_WITH_SECRETS, ids=lambda f: f.parent.name)
def test_no_secret_in_argv(stack_file):
    doc = yaml.safe_load(_render(stack_file.read_text()))
    leaked = [s for s in _argv_strings(doc) if SENTINEL in s]
    assert not leaked, f"{stack_file}: secret leaked into command/entrypoint/healthcheck: {leaked}"


def test_discovery_finds_a_secret_bearing_stack():
    """Guards the discovery+detection step itself: if it ever found zero
    stacks with a secret-looking var, the case above would vacuously pass."""
    assert STACKS_WITH_SECRETS, "no stack files with a secret-looking var were found"
