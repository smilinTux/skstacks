"""The skbackup engine is plain bash shipped verbatim to storage hosts: it
must parse, and pass shellcheck where shellcheck is installed (CI's
ubuntu-latest image ships it)."""
import shutil
import subprocess

import pytest

from skbackup_support import ENGINE

SCRIPTS = sorted([ENGINE / "backup", *ENGINE.glob("lib/*.sh")])


def test_engine_files_exist():
    assert (ENGINE / "backup").exists()
    assert len(SCRIPTS) >= 4


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_bash_parses(script):
    proc = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck not installed")
def test_shellcheck_clean():
    proc = subprocess.run(["shellcheck", "-x", "-s", "bash", "-P", str(ENGINE / "lib"), *map(str, SCRIPTS)],
                          capture_output=True, text=True, cwd=ENGINE)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_engine_is_strict_mode_and_has_no_eval():
    main = (ENGINE / "backup").read_text()
    assert "set -euo pipefail" in main
    for script in SCRIPTS:
        for n, line in enumerate(script.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            assert "eval " not in code, f"{script.name}:{n}"
