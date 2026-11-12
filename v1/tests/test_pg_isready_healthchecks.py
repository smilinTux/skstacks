"""pg_isready healthchecks must name a real user once docker stack deploy has run.

docker stack deploy interpolates `$VAR` and `${VAR}` in the compose file from the
deployer's shell before the spec reaches Swarm. Only `$$` survives as a literal `$`
for the container shell. skorch's postgres healthcheck used `$POSTGRES_USER`, which
the deploy shell never sets, so Swarm stored `pg_isready -U  -d ` and the server
logged `FATAL: role "-d" does not exist` every 10 s. pg_isready still exited 0, so
nothing went unhealthy and it went unnoticed from v2.18.0 to v2.26.20.
"""
import pathlib
import re
import shlex

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKORCH_T = ANSIBLE / "optional/skorch/src/config/skorch/skorch.yml.j2"

_DEPLOY_VAR = re.compile(r"\$\$|\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")
_SHELL_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def stack_deploy_interpolate(text, deploy_env=None):
    """What docker stack deploy does to a compose string: `$$` becomes `$`, any other
    variable is replaced from the deployer's environment (empty when unset)."""
    env = deploy_env or {}

    def sub(m):
        if m.group(0) == "$$":
            return "$"
        return env.get(m.group(1) or m.group(2), "")

    return _DEPLOY_VAR.sub(sub, text)


def container_expand(text, container_env):
    """The container's sh -c expansion of the CMD-SHELL string."""
    return _SHELL_VAR.sub(lambda m: container_env.get(m.group(1) or m.group(2), ""), text)


def pg_isready_user(cmd):
    """The -U argument of a pg_isready command, or None when it is missing or empty."""
    argv = shlex.split(cmd)
    for i, a in enumerate(argv):
        if a == "-U":
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            return None if not nxt or nxt.startswith("-") else nxt
        if a.startswith("-U") and len(a) > 2:
            return a[2:]
        if a.startswith("--username="):
            return a.split("=", 1)[1] or None
    return None


def render_skorch(env="prod", **over):
    cfg = {"CLUSTERNAME": "demo", "DOMAIN": "example.test",
           "POSTGRES_USER": "skorch_user", "POSTGRES_DB": "skorch_db"}
    cfg.update(over)
    j = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return j.from_string(SKORCH_T.read_text()).render(
        env=env, skorch_cfg=cfg, cluster_name="demo", domain="example.test")


def skorch_pg_probe(env="prod"):
    return yaml.safe_load(render_skorch(env))["services"]["postgres"]["healthcheck"]["test"]


# ---- the helpers behave like docker ---------------------------------------------

def test_interpolation_drops_single_dollar_and_keeps_double():
    assert stack_deploy_interpolate("-U $A -U ${B} -U $$C -U $${D}") == "-U  -U  -U $C -U ${D}"


def test_empty_user_is_detected():
    assert pg_isready_user("pg_isready -U  -d ") is None
    assert pg_isready_user("pg_isready -U -d db") is None
    assert pg_isready_user("pg_isready -d db") is None
    assert pg_isready_user("pg_isready -U skorch_user -d db") == "skorch_user"


# ---- skorch ---------------------------------------------------------------------

@pytest.mark.parametrize("env", ("dev", "staging", "prod"))
def test_skorch_pg_probe_survives_stack_deploy_as_the_configured_user(env):
    kind, cmd = skorch_pg_probe(env)
    assert kind == "CMD-SHELL"
    stored = stack_deploy_interpolate(cmd)  # the deployer's shell has no POSTGRES_USER
    ran = container_expand(stored, {"POSTGRES_USER": "skorch_user", "POSTGRES_DB": "skorch_db"})
    assert pg_isready_user(ran) == "skorch_user"
    assert "-d skorch_db" in ran


def test_skorch_pg_probe_does_not_depend_on_the_deployer_env():
    _, cmd = skorch_pg_probe()
    leaked = stack_deploy_interpolate(cmd, {"POSTGRES_USER": "laptop_user", "POSTGRES_DB": "x"})
    assert stack_deploy_interpolate(cmd) == leaked


def test_the_pre_fix_single_dollar_probe_fails_the_check():
    """The v2.18.0 to v2.26.20 line, so the check above is known to catch it."""
    stored = stack_deploy_interpolate("pg_isready -U $POSTGRES_USER -d $POSTGRES_DB")
    assert pg_isready_user(container_expand(stored, {"POSTGRES_USER": "u"})) is None


# ---- every stack template -------------------------------------------------------

_PROBES = sorted(
    (str(p.relative_to(ANSIBLE)), n, line.strip())
    for p in ANSIBLE.rglob("*.yml.j2")
    for n, line in enumerate(p.read_text().splitlines(), 1)
    if "pg_isready" in line and "test:" in line
)


def test_some_pg_isready_probes_were_found():
    assert len(_PROBES) >= 5


@pytest.mark.parametrize("path,lineno,line", _PROBES, ids=[f"{p}:{n}" for p, n, _ in _PROBES])
def test_no_pg_isready_probe_uses_a_deploy_time_variable(path, lineno, line):
    """A single-$ variable in a healthcheck is filled from the deployer's shell, not the
    container. Jinja expressions are fine (ansible renders them), `$$` is fine."""
    no_jinja = re.sub(r"\{\{.*?\}\}", "", line)
    single = [m.group(0) for m in _DEPLOY_VAR.finditer(no_jinja) if m.group(0) != "$$"]
    assert not single, f"{path}:{lineno} uses {single}: write $$ so the container expands it"
