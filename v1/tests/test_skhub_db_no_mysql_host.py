"""skhub's db must not see MYSQL_HOST (issue #438).

The db service reads the shared skhub.env, which sets MYSQL_HOST=db for the
Nextcloud containers. On a datadir from an older MariaDB major, the image
entrypoint (MARIADB_AUTO_UPGRADE=1) starts a socket-only temporary server and runs
mariadb-upgrade, whose client honours MYSQL_HOST and connects over TCP to `db`,
which nothing answers yet: "Can't connect to server on 'db'", "Upgrade failed",
crash loop. The db wrapper unsets MYSQL_HOST before exec'ing the entrypoint.

These tests render the template, apply docker stack deploy's `$$` interpolation and
run the wrapper with /bin/sh against a fake docker-entrypoint.sh.
"""
import os
import pathlib
import subprocess

import jinja2
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub/src/config/skhub"


def _render(name, **skhub):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters.setdefault("bool", lambda v: str(v).lower() in ("1", "true", "yes"))
    ctx = {"CLUSTERNAME": "c", "DOMAIN": "e.com", **skhub}
    return env.from_string((SKHUB / name).read_text()).render(app="skhub", env="prod", skhub=ctx,
                                                              fence_service_name="x")


def _services():
    return yaml.safe_load(_render("skhub.yml.j2"))["services"]


def _run_db_wrapper(tmp_path, extra_env):
    db = _services()["db"]
    entry = db["entrypoint"]
    assert entry[:2] == ["/bin/sh", "-c"]
    script = entry[2].replace("$$", "$").replace("/var/lib/mysql/", f"{tmp_path}/data/")
    (tmp_path / "data").mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker-entrypoint.sh"
    fake.write_text('#!/bin/sh\necho "HOST=${MYSQL_HOST-<unset>} AUTO=${MARIADB_AUTO_UPGRADE-<unset>} ARGS=$*"\n')
    fake.chmod(0o755)
    base = {k: v for k, v in os.environ.items() if k != "MYSQL_HOST"}
    env = dict(base, PATH=f"{bindir}:{os.environ['PATH']}", **extra_env)
    args = [str(a) for a in db["command"]]
    out = subprocess.run(["/bin/sh", "-c", script, *entry[3:], *args], env=env,
                         capture_output=True, text=True, check=True)
    return out.stdout


def test_shared_env_still_sets_mysql_host_for_nextcloud():
    # The Nextcloud containers need it; the fix belongs in the db service only.
    env = _render("skhub.env.j2", mysql_root_password="r", mysql_user="u", mysql_password="p")
    assert "MYSQL_HOST=db" in env.splitlines()
    assert _services()["db"]["env_file"].endswith("/skhub.env")


def test_db_wrapper_unsets_mysql_host(tmp_path):
    out = _run_db_wrapper(tmp_path, {"MYSQL_HOST": "db"})
    assert "HOST=<unset>" in out


def test_db_wrapper_keeps_the_rest_of_the_env_and_args(tmp_path):
    out = _run_db_wrapper(tmp_path, {"MYSQL_HOST": "db", "MARIADB_AUTO_UPGRADE": "1"})
    assert "AUTO=1" in out
    assert "--max_connections=500" in out


def test_db_wrapper_without_mysql_host_is_fine(tmp_path):
    out = _run_db_wrapper(tmp_path, {})
    assert "HOST=<unset>" in out
