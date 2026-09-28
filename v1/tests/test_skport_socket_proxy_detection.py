"""skport's socket-proxy detection must pick the right proxy, and fail loudly
when there is none.

The task piped `docker service ls` into `grep -q` under `set -o pipefail`.
With a long service list, grep matched and exited, `docker` took SIGPIPE on
its next write, the pipeline reported failure, and the task fell through to
the next branch: skfenceha present, "skfence" chosen. When neither proxy
existed it silently answered "skfence" anyway, and skport was deployed
pointing at a socket proxy that does not exist. These tests run the task's
own shell body, rendered for each env, against a fake `docker`.
"""
import os
import pathlib
import stat
import subprocess

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
ENVS = ["dev", "staging", "prod"]
FAKE_DOCKER = """#!/bin/bash
if [ "$1 $2" = "service ls" ]; then
  # like the real CLI, die of SIGPIPE if the reader goes away mid-write
  cat "$FAKE_SERVICES" || exit $?
  exit "${FAKE_RC:-0}"
fi
exit 0
"""


def _body(env_name):
    playbook = ANSIBLE / f"optional/skport/deploy_skport-{env_name}.yml"
    tasks = [t for play in yaml.safe_load(playbook.read_text())
             for t in (play.get("pre_tasks") or []) + (play.get("tasks") or [])]
    task = next(t for t in tasks if t.get("register") == "socket_proxy_detection")
    assert (task.get("args") or {}).get("executable") == "/bin/bash"
    return jinja2.Environment().from_string(task["shell"]).render(env=env_name)


def _run(tmp_path, env_name, names, rc=0):
    fake = tmp_path / "bin" / "docker"
    fake.parent.mkdir(exist_ok=True)
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    services = tmp_path / "services.txt"
    services.write_text("".join(f"{n}\n" for n in names))
    env = dict(os.environ, PATH=f"{fake.parent}:{os.environ['PATH']}",
               FAKE_SERVICES=str(services), FAKE_RC=str(rc))
    return subprocess.run(["/bin/bash", "-c", _body(env_name)], env=env,
                          capture_output=True, text=True, timeout=60)


def _busy(env_name, first):
    # The match comes first and ~1 MB follows it: grep -q exits long before
    # the writer is done, which is exactly when SIGPIPE hits the writer.
    return [first] + [f"otherstack-{env_name}_service-{i:06d}-padding-padding" for i in range(20000)]


@pytest.mark.parametrize("env_name", ENVS)
def test_finds_skfenceha_on_a_busy_cluster(tmp_path, env_name):
    r = _run(tmp_path, env_name, _busy(env_name, f"skfenceha-{env_name}_socket-proxy"))
    assert (r.returncode, r.stdout.strip()) == (0, "skfenceha"), r.stderr


@pytest.mark.parametrize("env_name", ENVS)
def test_finds_skfence_on_a_busy_cluster(tmp_path, env_name):
    r = _run(tmp_path, env_name, _busy(env_name, f"skfence-{env_name}_socket-proxy"))
    assert (r.returncode, r.stdout.strip()) == (0, "skfence"), r.stderr


@pytest.mark.parametrize("env_name", ENVS)
def test_prefers_skfenceha_when_both_exist(tmp_path, env_name):
    r = _run(tmp_path, env_name, [f"skfence-{env_name}_socket-proxy", f"skfenceha-{env_name}_socket-proxy"])
    assert (r.returncode, r.stdout.strip()) == (0, "skfenceha"), r.stderr


@pytest.mark.parametrize("env_name", ENVS)
def test_no_known_proxy_fails_loudly(tmp_path, env_name):
    r = _run(tmp_path, env_name, [f"skfenceha-{env_name}_traefik", f"skfence-{env_name}_socket-proxy-old",
                                  f"skfenceha-other_socket-proxy"])
    assert r.returncode != 0 and r.stdout.strip() == ""
    assert "socket-proxy" in r.stderr


@pytest.mark.parametrize("env_name", ENVS)
def test_docker_failure_is_not_read_as_no_match(tmp_path, env_name):
    r = _run(tmp_path, env_name, [f"skfenceha-{env_name}_socket-proxy"], rc=1)
    assert r.returncode != 0 and r.stdout.strip() == ""
    assert "docker service ls" in r.stderr
