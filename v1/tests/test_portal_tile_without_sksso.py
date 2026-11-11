"""shared/tasks/authentik_portal_tile.yml when sksso is not deployed yet.

A fresh cluster (and the release lab) deploys some roles before sksso. The
tile provisioner joins sksso's network to reach its API, so before this
check every such role failed its deploy with "network sksso-<env> not
found" (v2.26.17 gate, skvector). Now the tile is skipped with a warning,
unless the caller sets portal_tile.require_sksso.

Runs the real task file under ansible-playbook on localhost with a fake
`docker` on PATH that records its argv and answers `network inspect` with
the exit code the test chooses. Three cases: network missing (skip, play
succeeds, provisioner never runs), network missing with require_sksso
(fails), network present (passes the gate and reaches the vault checks).
"""
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")

TASKS = Path(__file__).resolve().parents[1] / "ansible" / "shared" / "tasks" / "authentik_portal_tile.yml"


def run(tmp_path, net_rc, require=False):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    log = tmp_path / "docker.log"
    docker = bindir / "docker"
    docker.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "$*" >> {log}
        [ "$1 $2" = "network inspect" ] && exit {net_rc}
        exit 0
        """))
    docker.chmod(0o755)
    vault = tmp_path / "sksso-vault.yml"
    vault.write_text("sksso: {}\n")  # no api_token: the assert after the gate fails
    play = tmp_path / "play.yml"
    play.write_text(textwrap.dedent(f"""\
        - hosts: localhost
          gather_facts: false
          vars:
            app: skvector
            env: dev
            vault_file_path: {vault}
            portal_tile:
              enabled: true
              slug: skvector
              name: SKVector
              launch_url: https://skvector.example.com
              authentik_vault_file: {vault}
              require_sksso: {str(require).lower()}
          tasks:
            - include_tasks: {TASKS}
        """))
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCALHOST_WARNING="false", ANSIBLE_INVENTORY_UNPARSED_WARNING="false")
    proc = subprocess.run(["ansible-playbook", "-i", "localhost,", "-c", "local", str(play)],
                          capture_output=True, text=True, env=env, timeout=180)
    calls = log.read_text().splitlines() if log.exists() else []
    return proc, calls


def test_missing_network_skips_with_warning(tmp_path):
    proc, calls = run(tmp_path, net_rc=1)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "sksso is not deployed yet" in proc.stdout
    assert calls == ["network inspect sksso-dev"]  # the provisioner `docker run` never ran


def test_missing_network_fails_when_required(tmp_path):
    proc, _ = run(tmp_path, net_rc=1, require=True)
    assert proc.returncode != 0
    assert "require_sksso is set" in proc.stdout


def test_present_network_passes_the_gate(tmp_path):
    proc, _ = run(tmp_path, net_rc=0)
    assert proc.returncode != 0  # stops at the token assert, which proves the gate let it through
    assert "sksso.api_token (32+ chars) is missing" in proc.stdout
    assert "sksso is not deployed yet" not in proc.stdout
