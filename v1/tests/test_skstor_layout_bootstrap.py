"""skstor's deploy script bootstraps Garage's cluster layout exactly once.
Runs the script's bootstrap block against a fake `docker` that emulates the
garage CLI (v2.4.x output shapes). `garage status` lists a connected node even
before it has a role, and truncates IDs to 16 chars, so it cannot tell a fresh
node from a bootstrapped one; the current layout version can (0 = never applied)."""
import os
import pathlib
import stat
import subprocess

DEPLOY = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skstor/src/skstor/deploy.j2"
NODE = "a1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f9"

FAKE_DOCKER = r"""#!/bin/bash
echo "$*" >> "$CALLS"
case "$*" in
  "ps -q"*) echo c0ffee ;;
  *"node id -q"*) echo "${NODE}@10.0.0.5:3901" ;;
  *"garage status"*) printf '==== HEALTHY NODES ====\nID                Hostname  Address         Tags  Zone  Capacity\n%s  garage1   10.0.0.5:3901   NO ROLE ASSIGNED\n' "${NODE:0:16}" ;;
  *"layout show"*) printf '==== CURRENT CLUSTER LAYOUT ====\nCurrent cluster layout version: %s\n' "$LAYOUT_VERSION" ;;
esac
"""


def bootstrap_block():
    text = DEPLOY.read_text()
    start = text.index("CONTAINER_ID=")
    end = text.index('echo -e "${GREEN}Deployment complete!${NC}"')
    return 'GREEN=""; YELLOW=""; RED=""; NC=""; STACK_NAME=skstor-dev\n' + text[start:end]


def run(tmp_path, layout_version):
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    calls = tmp_path / "calls"
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", CALLS=str(calls),
               NODE=NODE, LAYOUT_VERSION=str(layout_version))
    subprocess.run(["bash", "-e", "-c", bootstrap_block()], env=env, check=True, capture_output=True)
    return calls.read_text()


def test_fresh_node_is_assigned_and_applied_at_version_1(tmp_path):
    calls = run(tmp_path, 0)
    assert f"layout assign -z dc1 -c 1 {NODE}" in calls
    assert "layout apply --version 1" in calls


def test_bootstrapped_node_is_left_alone(tmp_path):
    calls = run(tmp_path, 1)
    assert "layout assign" not in calls
    assert "layout apply" not in calls
