"""MariaDB 12.x tc.log guard for the skhub and skbook db services.

On a clean shutdown MariaDB's TC_LOG_MMAP::close() overwrites the first magic byte
of tc.log with 'A' and then deletes the file. The 'A' marks a log that was closed
cleanly, so that a delete that fails is not taken for a crashed one. The v2.26.20
gate saw a clean, sub-second shutdown on an NFS datadir leave the closed log behind
(header 41 23 05 74), and every later start aborted with "Bad magic header in tc
log". The db entrypoint now moves aside only a log carrying that closed marker and
leaves a real crashed log (magic fe 23 05 74) for MariaDB's own recovery.

These tests render the template, apply docker stack deploy's `$` interpolation, and
run the resulting wrapper with /bin/sh against real files.
"""
import os
import pathlib
import subprocess

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
TEMPLATES = {
    "skhub": (ANSIBLE / "skhub/src/config/skhub/skhub.yml.j2", {"fence_service_name": "x"}),
    "skbook": (ANSIBLE / "skbook/src/config/skbook/skbook.yml.j2", {}),
}
CLOSED = bytes([0x41, 0x23, 0x05, 0x74]) + b"\0" * 60
CRASHED = bytes([0xFE, 0x23, 0x05, 0x74]) + b"\0" * 60


def _db(app):
    path, extra = TEMPLATES[app]
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters.setdefault("bool", lambda v: str(v).lower() in ("1", "true", "yes"))
    out = env.from_string(path.read_text()).render(
        app=app, env="prod", **{app: {"CLUSTERNAME": "c", "DOMAIN": "e.com"}}, **extra)
    return yaml.safe_load(out)["services"]["db"]


def _stack_deploy(s):
    """Only `$$` survives docker stack deploy, as a literal `$`. The guard has no
    single-$ variable (asserted below), so this is the whole interpolation."""
    return s.replace("$$", "$")


def _run(app, tmp_path, tc=None, **extra_env):
    entry = _db(app)["entrypoint"]
    assert entry[:2] == ["/bin/sh", "-c"]
    script = _stack_deploy(entry[2]).replace("/var/lib/mysql/", f"{tmp_path}/data/")
    data = tmp_path / "data"
    data.mkdir()
    if tc is not None:
        (data / "tc.log").write_bytes(tc)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "docker-entrypoint.sh"
    fake.write_text('#!/bin/sh\necho "HOST=${MYSQL_HOST-unset} MARIADB_HOST=${MARIADB_HOST-unset}"\n'
                    'echo "ENTRY $*"\n')
    fake.chmod(0o755)
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", **extra_env)
    args = [str(a) for a in _db(app)["command"]]
    r = subprocess.run(["/bin/sh", "-c", script, *entry[3:], *args],
                       env=env, capture_output=True, text=True, timeout=20)
    return r, data


@pytest.mark.parametrize("app", TEMPLATES)
def test_closed_tclog_is_moved_aside_and_mariadb_starts(app, tmp_path):
    r, data = _run(app, tmp_path, CLOSED)
    assert r.returncode == 0, r.stderr
    assert not (data / "tc.log").exists()
    moved = list(data.glob("tc.log.closed-*"))
    assert len(moved) == 1 and moved[0].read_bytes() == CLOSED
    assert "moved aside a tc.log" in r.stdout


@pytest.mark.parametrize("app", TEMPLATES)
def test_crashed_tclog_is_left_for_mariadb_recovery(app, tmp_path):
    r, data = _run(app, tmp_path, CRASHED)
    assert r.returncode == 0, r.stderr
    assert (data / "tc.log").read_bytes() == CRASHED
    assert not list(data.glob("tc.log.closed-*"))


@pytest.mark.parametrize("app", TEMPLATES)
def test_no_tclog_is_a_no_op(app, tmp_path):
    r, data = _run(app, tmp_path, None)
    assert r.returncode == 0, r.stderr
    assert list(data.iterdir()) == []


@pytest.mark.parametrize("app", TEMPLATES)
def test_the_entrypoint_execs_with_the_service_args(app, tmp_path):
    r, _ = _run(app, tmp_path, None)
    want = " ".join(str(a) for a in _db(app)["command"])
    assert r.stdout.strip().splitlines()[-1] == f"ENTRY {want}"


@pytest.mark.parametrize("app", TEMPLATES)
def test_the_guard_has_no_deploy_time_variable(app):
    """Every `$` in the wrapper is `$$`, so docker stack deploy never fills one in
    from the deployer's shell (see test_pg_isready_healthchecks.py)."""
    script = _db(app)["entrypoint"][2]
    assert "$" in script
    assert script.replace("$$", "").count("$") == 0


@pytest.mark.parametrize("app", TEMPLATES)
def test_the_entrypoint_does_not_see_mysql_host(app, tmp_path):
    """skhub's db reads the env file Nextcloud uses (MYSQL_HOST=db). The mariadb
    client honours MYSQL_HOST, so MARIADB_AUTO_UPGRADE's mariadb-upgrade against
    the socket-only temporary server failed with "Unknown server host 'db'" on
    the first 10.11 to 12.3 start (nor, v2.26.20). The db never needs it."""
    r, _ = _run(app, tmp_path, None, MYSQL_HOST="db", MARIADB_HOST="db", MYSQL_TCP_PORT="3306")
    assert r.returncode == 0, r.stderr
    assert "HOST=unset MARIADB_HOST=unset" in r.stdout


def test_skhub_env_still_gives_nextcloud_its_db_host():
    """The fix is in the db wrapper, not the shared env file: Nextcloud still needs it."""
    text = (ANSIBLE / "skhub/src/config/skhub/skhub.env.j2").read_text()
    assert "MYSQL_HOST=db" in text


def test_both_stacks_carry_the_same_guard():
    assert _db("skhub")["entrypoint"] == _db("skbook")["entrypoint"]
