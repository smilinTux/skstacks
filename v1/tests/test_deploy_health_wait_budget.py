"""The heavy services' deploy.j2 health polls must fit a slow first boot.

skgit (Forgejo, healthcheck start_period 300s) and skgallery (Immich server
plus machine learning, both pulling large images and migrating on first
start) polled replicas 36 x 5s (3 minutes) before printing a warning, so a
normal first install on a busy host ended in a misleading "unhealthy"
report. Their polls now run for HEALTH_WAIT_SECONDS (env var, default 900,
same style as SERVICE_UPDATE_TIMEOUT). Semantics stay warning-only: the
script never exits non-zero because a service is still converging.
"""
import os
import pathlib
import re
import stat
import subprocess

import pytest

from test_deploy_health_poll import _extract_blocks, _render

V1_DIR = pathlib.Path(__file__).resolve().parents[1]
HEAVY = {
    "skgit": V1_DIR / "ansible/optional/skgit/src/skgit/deploy.j2",
    "skgallery": V1_DIR / "ansible/optional/skgallery/src/skgallery/deploy.j2",
}
DEFAULT_SECONDS = 900
INTERVAL = 5

# Never converges: every `service ls` reports 0/1.
FAKE_DOCKER = r"""#!/bin/bash
case "$*" in
  "service ls"*) echo polled >> "$POLLS"; echo "0/1" ;;
  *) : ;;
esac
"""


def _blocks(name):
    blocks = _extract_blocks(_render(HEAVY[name]))
    assert blocks, f"{name}: no health poll block found"
    return blocks


def _poll_count(tmp_path, block, seconds=None):
    fake = tmp_path / "docker"
    fake.write_text(FAKE_DOCKER)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    polls = tmp_path / "polls"
    if polls.exists():
        polls.unlink()
    script = (
        'GREEN=""; YELLOW=""; RED=""; NC=""; BLUE=""; CYAN=""\n'
        "STACK_NAME=svc-dev; EXPECTED=1\n"
        "sleep() { :; }\n"
        "_test_block() {\n" + block + "\n}\n"
        "_test_block || true\n"
    )
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", POLLS=str(polls))
    env.pop("HEALTH_WAIT_SECONDS", None)
    if seconds is not None:
        env["HEALTH_WAIT_SECONDS"] = str(seconds)
    r = subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    return len(polls.read_text().splitlines())


@pytest.mark.parametrize("name", sorted(HEAVY))
def test_default_budget_is_fifteen_minutes(tmp_path, name):
    for block in _blocks(name):
        assert _poll_count(tmp_path, block) * INTERVAL >= DEFAULT_SECONDS


@pytest.mark.parametrize("name", sorted(HEAVY))
@pytest.mark.parametrize("seconds", [60, 1800])
def test_budget_driven_by_env_var(tmp_path, name, seconds):
    for block in _blocks(name):
        budget = _poll_count(tmp_path, block, seconds) * INTERVAL
        assert seconds <= budget < seconds + INTERVAL, (name, seconds, budget)


@pytest.mark.parametrize("name", sorted(HEAVY))
def test_health_poll_stays_warning_only(name):
    for block in _blocks(name):
        assert not re.search(r"^\s*exit\b", block, re.MULTILINE), block
