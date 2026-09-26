"""skstor's deploy script must wait for Garage to converge instead of checking
replicas once 10s after `docker stack deploy`: an update that restarts the task
(e.g. a network alias change) shows 0/1 for a while, and the single check
failed the deploy on the skstack06 v2.18.0 run."""
import os
import pathlib
import stat
import subprocess

import jinja2

DEPLOY = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skstor/src/skstor/deploy.j2"

FAKE = r"""#!/bin/bash
case "$*" in
  "service ls"*)
    n=$(cat "$STATE" 2>/dev/null || echo 0); echo $((n+1)) > "$STATE"
    if [ "$n" -ge 3 ]; then echo "1/1"; else echo "0/1"; fi ;;
  *) : ;;
esac
"""


def health_block():
    text = jinja2.Environment(undefined=jinja2.ChainableUndefined).from_string(
        DEPLOY.read_text()).render(env="dev", app="skstor", skstor={})
    start = text.index('echo -e "${YELLOW}Checking service health')
    end = text.index('echo -e "${GREEN}Deployment complete!')
    return 'GREEN=""; YELLOW=""; RED=""; NC=""; STACK_NAME=skstor-dev\nsleep() { :; }\n' + text[start:end]


def test_health_check_waits_for_convergence(tmp_path):
    fake = tmp_path / "docker"
    fake.write_text(FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", STATE=str(tmp_path / "n"))
    r = subprocess.run(["bash", "-e", "-c", health_block()], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
