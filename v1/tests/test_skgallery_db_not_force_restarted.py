"""skgallery: a redeploy never force-restarts the database.

The deploy wrapper used to run `docker service update --force` on every
pre-existing service after `docker stack deploy`, the database included.
That restart killed an in-flight db-backup sidecar dump (seen on a v3
upgrade: the sidecar's first run failed because the wrapper restarted
postgres mid-dump) and dropped every Immich DB connection, on every deploy,
whether anything changed or not.

The force is only useful for a service whose bind-mounted config files
changed (they do not change the service spec). The database has none: its
env_file values are copied into the service spec, and its image, resources
and healthcheck are spec fields, so `docker stack deploy` already rolls it
exactly when one of them changed. The force on it added nothing but an
unconditional restart. Now the database restarts only through the stack
deploy, i.e. only when its image or config changed.
"""
import pathlib
import re

import jinja2
import yaml

from test_no_forced_restart_on_first_install import FORCE_RE, _code_lines, _run

DEPLOY = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgallery/src/skgallery/deploy.j2"
COMPOSE = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgallery/src/config/skgallery/skgallery.yml.j2"
SERVICES = ["skgallery-dev_immich-server", "skgallery-dev_immich-machine-learning",
            "skgallery-dev_redis", "skgallery-dev_database", "skgallery-dev_postgres-db-backup"]


def test_redeploy_does_not_force_the_database(tmp_path):
    forced, out = _run(tmp_path, "skgallery", SERVICES, "skgallery.yml", present=True)
    targets = [line.split()[-1] for line in forced]
    assert "skgallery-dev_database" not in targets, "\n".join(forced) + "\n---\n" + out
    assert "skgallery-dev_postgres-db-backup" not in targets


def test_redeploy_still_forces_the_app_services(tmp_path):
    forced, _ = _run(tmp_path, "skgallery", SERVICES, "skgallery.yml", present=True)
    targets = [line.split()[-1] for line in forced]
    assert targets == ["skgallery-dev_immich-server", "skgallery-dev_immich-machine-learning", "skgallery-dev_redis"]


def test_first_install_still_forces_nothing(tmp_path):
    forced, _ = _run(tmp_path, "skgallery", SERVICES, "skgallery.yml", present=False)
    assert forced == []


def test_no_force_line_can_reach_the_database():
    """Static: no forced update in the wrapper names the database, directly
    or through the service loop."""
    for n, line in _code_lines(DEPLOY):
        if FORCE_RE.search(line):
            assert "database" not in line, f"line {n}: {line.strip()}"
    loops = [line for _, line in _code_lines(DEPLOY) if re.match(r"\s*for SERVICE in .*; do", line)]
    force_loop = loops[0]
    assert "database" not in force_loop.split(" in ", 1)[1], force_loop


def test_premise_the_database_has_no_bind_mounted_config():
    """The reason the force is safe to drop: the database mounts only its data
    dir and localtime, no config file whose change the spec would miss. If a
    config file is ever bind-mounted here, this test fails and the force has
    to come back for that case."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    doc = yaml.safe_load(env.from_string(COMPOSE.read_text()).render(env="prod", app="skgallery", skgallery={}))
    db = doc["services"]["database"]
    assert db["volumes"] == ["/var/data/skgallery-prod/db-data:/var/lib/postgresql/data", "/etc/localtime:/etc/localtime:ro"]
    assert "configs" not in db and db["env_file"] == "/var/data/config/skgallery-prod/skgallery-db.env"
