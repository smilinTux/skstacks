"""Every `docker service update --detach=false` in a deploy.j2 must be bounded.

`--detach=false` blocks until the service converges and has no timeout of its
own. When a task cannot be scheduled (e.g. "no suitable node (insufficient
resources)") the update never converges, so the deploy script, and the Ansible
play that runs it, hangs forever. Observed on a v2.19.0 rc2 test instance:
skbook's deploy sat 27 min on `docker service update --force --detach=false
skbook-dev_bookstack` with bookstack Pending.

Fix: each such call is wrapped in `timeout "${SERVICE_UPDATE_TIMEOUT:-300}"`,
keeping its existing failure semantics (`|| true` stays), so the health poll
that follows (test_deploy_health_poll.py) runs and reports the service as
unhealthy instead of the deploy hanging.
"""
import os
import pathlib
import re
import shutil
import signal
import stat
import subprocess

import jinja2
import pytest

V1_DIR = pathlib.Path(__file__).resolve().parents[1]
DEPLOY_FILES = sorted((V1_DIR / "ansible").glob("*/*/src/*/deploy.j2"))
TIMEOUT_RE = re.compile(r'\btimeout "\$\{SERVICE_UPDATE_TIMEOUT:-300\}" docker service update\b')


def _detach_false_lines():
    out = []
    for path in DEPLOY_FILES:
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if "--detach=false" in line and not line.lstrip().startswith("#"):
                out.append(pytest.param(line, id=f"{path.relative_to(V1_DIR)}:{n}"))
    return out


LINES = _detach_false_lines()


def test_discovered_detach_false_calls():
    # Sanity floor so a future scan change cannot silently match nothing.
    assert len(LINES) >= 41


@pytest.mark.parametrize("line", LINES)
def test_detach_false_update_is_bounded(line):
    assert TIMEOUT_RE.search(line), (
        "blocking `docker service update --detach=false` must be wrapped in "
        '`timeout "${SERVICE_UPDATE_TIMEOUT:-300}"`: ' + line.strip()
    )


REAL_SLEEP = shutil.which("sleep")

FAKE_DOCKER = r"""#!/bin/bash
case "$*" in
  "service update"*)
    echo "$*" >> "$UPDATELOG"
    exec "$REAL_SLEEP" 3600 ;;
  "service ls"*"--format"*)
    echo "0/1" ;;
  "service ls"*)
    echo "abc skbook-dev_bookstack replicated 0/1" ;;
  "service logs"*)
    echo "$*" >> "$FAILLOG" ;;
  *) : ;;
esac
"""


def test_stuck_service_update_times_out_and_reports_unhealthy(tmp_path):
    src = V1_DIR / "ansible/optional/skbook/src/skbook/deploy.j2"
    tmpl = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(src.read_text())
    script = tmpl.render(env="dev", app="skbook", skbook={})
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

    env = dict(
        os.environ,
        PATH=f"{bindir}:{os.environ['PATH']}",
        REAL_SLEEP=REAL_SLEEP,
        SERVICE_UPDATE_TIMEOUT="1",
        UPDATELOG=str(tmp_path / "updates"),
        FAILLOG=str(tmp_path / "faillog"),
    )
    proc = subprocess.Popen(
        ["bash", str(deploy)], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        out, _ = proc.communicate()
        pytest.fail("deploy hung on a never-converging `docker service update`:\n" + out)

    assert (tmp_path / "updates").exists(), "fake `docker service update` never ran:\n" + out
    unhealthy = "health check: 0/1" in out and (tmp_path / "faillog").exists()
    assert proc.returncode != 0 or unhealthy, (
        "deploy terminated but neither failed nor reported the service unhealthy:\n" + out
    )
    if unhealthy:
        assert "skbook-dev_bookstack" in (tmp_path / "faillog").read_text()
