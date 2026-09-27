"""skboard's deploy must not force-restart Vikunja on a first install.

`deploy.j2` ran `docker stack deploy`, slept ~5s, then
`docker service update --force --detach=false <stack>_vikunja`. On a FRESH
install the first Vikunja task is still running its initial SQLite migrations
(slow on NFS); the forced update (stop-first by default) kills it mid-migration
and leaves a half-migrated `vikunja.db`, so every later start fails with
"Migration failed: migration 20200515172220 failed: no such column:
done_at_unix" (or "table tasks_dg_tmp already exists"). Seen on the skstack06
v2.19.0 release-tag run.

Fix: record whether `<stack>_vikunja` exists (`docker service inspect` rc)
BEFORE `docker stack deploy`, and only force the update on a redeploy (where it
still picks up changed bind-mounted config). On a first install it is skipped.

Harness: render deploy.j2 and run it against a fake `docker` that logs every
call; `SERVICE_EXISTS=1|0` controls the `service inspect` exit status.
"""
import os
import pathlib
import stat
import subprocess

import jinja2
import pytest

V1_DIR = pathlib.Path(__file__).resolve().parents[1]
SRC = V1_DIR / "ansible/optional/skboard/src/skboard/deploy.j2"

FAKE_DOCKER = r"""#!/bin/bash
echo "$*" >> "$CALLLOG"
case "$*" in
  "service inspect"*)
    [ "$SERVICE_EXISTS" = "1" ] && exit 0 || exit 1 ;;
  "service ls"*"--format"*)
    echo "1/1" ;;
  "service ls"*)
    echo "abc skboard-dev_vikunja replicated 1/1" ;;
  *) : ;;
esac
"""


def _run(tmp_path, exists: bool):
    tmpl = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(SRC.read_text())
    script = tmpl.render(
        env="dev", app="skboard", skboard={}, cluster_name="c", domain="d.test"
    )
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "docker-compose.yml").write_text("services: {}\n")
    script = script.replace(
        'CONFIG_DIR="/var/data/config/${STACK_NAME}"', f'CONFIG_DIR="{config_dir}"'
    )
    assert str(config_dir) in script
    deploy = tmp_path / "deploy.sh"
    deploy.write_text(script)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("docker", FAKE_DOCKER), ("sleep", "#!/bin/sh\nexit 0\n")):
        f = bindir / name
        f.write_text(body)
        f.chmod(f.stat().st_mode | stat.S_IEXEC)

    calllog = tmp_path / "calls"
    env = dict(
        os.environ,
        PATH=f"{bindir}:{os.environ['PATH']}",
        CALLLOG=str(calllog),
        SERVICE_EXISTS="1" if exists else "0",
    )
    r = subprocess.run(
        ["bash", str(deploy)], env=env, capture_output=True, text=True, timeout=60
    )
    assert r.returncode == 0, r.stdout + r.stderr
    calls = calllog.read_text().splitlines() if calllog.exists() else []
    return r, calls


def _idx(calls, prefix):
    return [i for i, c in enumerate(calls) if c.startswith(prefix)]


def _forced_updates(calls):
    return [c for c in calls if c.startswith("service update") and "--force" in c]


def test_first_install_skips_forced_update(tmp_path):
    r, calls = _run(tmp_path, exists=False)
    assert _idx(calls, "stack deploy"), calls
    assert not _forced_updates(calls), (
        "first install must not force-restart vikunja (kills its initial "
        f"SQLite migration): {calls}\n{r.stdout}"
    )


def test_redeploy_still_forces_update(tmp_path):
    r, calls = _run(tmp_path, exists=True)
    forced = _forced_updates(calls)
    assert forced and all("skboard-dev_vikunja" in c for c in forced), (
        f"redeploy must still force-update vikunja: {calls}\n{r.stdout}"
    )


@pytest.mark.parametrize("exists", [False, True], ids=["first-install", "redeploy"])
def test_existence_checked_before_stack_deploy(tmp_path, exists):
    _, calls = _run(tmp_path, exists=exists)
    inspects = [
        i for i in _idx(calls, "service inspect") if "skboard-dev_vikunja" in calls[i]
    ]
    deploys = _idx(calls, "stack deploy")
    assert inspects and deploys, calls
    assert inspects[0] < deploys[0], (
        f"vikunja existence must be checked before `docker stack deploy`: {calls}"
    )
