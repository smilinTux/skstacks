"""sksso v2.26.4: postgres healthcheck user, and unset knobs byte-identical.

1. The postgres healthcheck ran `pg_isready -U postgres`. pg_isready only
   needs the server to answer, so the check passed, but on an instance whose
   database user is not `postgres` every probe made the server log
   `FATAL: role "postgres" does not exist` (every 10s, forever). It now uses
   the configured user and database (the container's own POSTGRES_USER and
   POSTGRES_DB, `$$` so the stack deploy does not interpolate them). With
   `postgres_user: postgres` the old check was already correct and the
   render stays exactly as it was.

2. The local-disk knobs (DATA_NODE, POSTGRES_DATA_PATH, REDIS_DATA_PATH)
   unset or empty render byte-identical to skstacks-v2.26.3: the baselines
   in fixtures/sksso_baseline/ were rendered from the v2.26.3 template
   before this change.
"""
import pathlib

import jinja2
import pytest
import yaml

HERE = pathlib.Path(__file__).resolve().parent
APP = HERE.parents[0] / "ansible/optional/sksso"
T = APP / "src/config/sksso/sksso.yml.j2"
BASELINE = HERE / "fixtures/sksso_baseline"
ENVS = ("dev", "staging", "prod")
KNOBS = ("DATA_NODE", "POSTGRES_DATA_PATH", "REDIS_DATA_PATH")


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def render(env="prod", **over):
    sksso = {"CLUSTERNAME": "demo", "DOMAIN": "example.test", "postgres_user": "postgres",
             "postgres_password": "p", "authentik_secret_key": "k"}
    sksso.update(over)
    sksso = {k: v for k, v in sksso.items() if v is not None}
    return _env().from_string(T.read_text()).render(app="sksso", env=env, sksso=sksso)


def healthcheck(text):
    return yaml.safe_load(text)["services"]["postgres"]["healthcheck"]["test"]


# ---- byte-identical defaults ---------------------------------------------------

@pytest.mark.parametrize("env", ENVS)
def test_unset_knobs_render_byte_identical_to_v2263(env):
    assert render(env) == (BASELINE / f"sksso-{env}.yml").read_text()


@pytest.mark.parametrize("env", ENVS)
def test_empty_knobs_render_byte_identical_to_unset(env):
    assert render(env, **{k: "" for k in KNOBS}) == render(env)


@pytest.mark.parametrize("env", ENVS)
def test_other_db_user_changes_only_the_healthcheck_line(env):
    old = (BASELINE / f"sksso-{env}.yml").read_text().splitlines()
    new = render(env, postgres_user="authentik").splitlines()
    assert len(old) == len(new)
    diff = [(a, b) for a, b in zip(old, new) if a != b]
    assert len(diff) == 1
    assert "pg_isready -U postgres" in diff[0][0]
    assert "pg_isready" in diff[0][1]


# ---- healthcheck user ------------------------------------------------------------

def test_default_user_keeps_the_old_check():
    assert healthcheck(render()) == ["CMD-SHELL", "pg_isready -U postgres"]


def test_unset_user_keeps_the_old_check():
    sksso_text = render(postgres_user=None)
    assert healthcheck(sksso_text) == ["CMD-SHELL", "pg_isready -U postgres"]


@pytest.mark.parametrize("user", ["authentik", "sksso", "u"])
def test_other_user_probes_with_the_container_user_and_db(user):
    test = healthcheck(render(postgres_user=user))
    assert test == ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
    assert "-U postgres" not in test[1]


def test_the_check_never_renders_the_user_or_password_literally():
    text = render(postgres_user="someuser", postgres_password="somepass")
    test = healthcheck(text)[1]
    assert "someuser" not in test and "somepass" not in test


def test_env_file_defines_the_vars_the_check_reads():
    env_t = (APP / "src/config/sksso/sksso.env.j2").read_text()
    assert "POSTGRES_USER=" in env_t and "POSTGRES_DB=" in env_t


# ---- README migration procedure --------------------------------------------------

def test_readme_migration_section_verifies_regular_files_and_never_deletes():
    r = (APP / "README.md").read_text()
    sec = r.split("### Migrating a running instance", 1)
    assert len(sec) == 2, "README needs a 'Migrating a running instance' section"
    body = sec[1].split("\n## ", 1)[0]
    for word in ("rsync -aHAX --numeric-ids", "find", "-type f", "%s",
                 "STALE-moved-to-", "--apparent-size", "scale"):
        assert word in body, word
    assert "never delete" in body.lower()
    assert "remove the renamed dirs" not in r


# ---- playbooks: empty knobs behave like unset ------------------------------------

def _plays(env):
    return [p for p in yaml.safe_load((APP / f"deploy_sksso-{env}.yml").read_text()) if isinstance(p, dict)]


@pytest.mark.parametrize("env", ENVS)
def test_empty_knobs_pass_the_guard_and_keep_the_nfs_paths(env):
    sksso = {k: "" for k in KNOBS}
    guard = [t for p in _plays(env) for t in (p.get("pre_tasks") or []) + (p.get("tasks") or [])
             if isinstance(t, dict) and "assert" in t and "DATA_NODE" in str(t)]
    assert len(guard) == 1
    assert all(_env().compile_expression(c)(sksso=sksso) for c in guard[0]["assert"]["that"])
    pv = [p["vars"] for p in _plays(env) if "sksso_postgres_data_path" in (p.get("vars") or {})][0]
    ctx = {"app": "sksso", "env": env, "sksso": sksso}
    assert _env().from_string(pv["sksso_postgres_data_path"]).render(**ctx) == f"/var/data/runtime/sksso-{env}/postgres"
    assert _env().from_string(pv["sksso_redis_data_path"]).render(**ctx) == f"/var/data/runtime/sksso-{env}/redis"
