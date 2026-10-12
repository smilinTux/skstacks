"""The forced-command wrapper (src/skhealth/skhealth-remote) that an operator
installs on a remote host: it must run exactly the four allowlisted read-only
commands, on the devices listed as its arguments, and deny everything else.
The real script runs; only its BIN and SUDO constants are pointed at stub
binaries that record their argv."""
import json
import os
import subprocess
import sys

import pytest

from skhealth_support import WRAPPER

STUB = f"""#!{sys.executable}
import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "calls.jsonl"), "a") as fh:
    fh.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + "\\n")
"""
SUDO_STUB = f"""#!/bin/bash
echo "[\\"sudo\\", $(printf '"%s", ' "$@" | sed 's/, $//')]" >> "$(dirname "$0")/calls.jsonl"
[ "$1" = -n ] || exit 99
shift
exec "$@"
"""


@pytest.fixture
def wrapper(tmp_path):
    text = WRAPPER.read_text()
    assert text.count("\nBIN=/usr/sbin\n") == 1 and text.count("\nSUDO=/usr/bin/sudo\n") == 1
    bindir = tmp_path / "sbin"
    bindir.mkdir()
    for name in ("zpool", "pvesm", "smartctl"):
        (bindir / name).write_text(STUB)
        (bindir / name).chmod(0o755)
    (bindir / "sudo").write_text(SUDO_STUB)
    (bindir / "sudo").chmod(0o755)
    w = tmp_path / "skhealth-remote"
    w.write_text(text.replace("\nBIN=/usr/sbin\n", f"\nBIN={bindir}\n")
                     .replace("\nSUDO=/usr/bin/sudo\n", f"\nSUDO={bindir}/sudo\n"))
    w.chmod(0o755)
    return w, bindir


def call(wrapper, command, devices=("/dev/nvme0n1", "/dev/sda")):
    w, bindir = wrapper
    log = bindir / "calls.jsonl"
    if log.exists():
        log.unlink()
    env = {"PATH": "/usr/bin:/bin"}
    if command is not None:
        env["SSH_ORIGINAL_COMMAND"] = command
    proc = subprocess.run([str(w), *devices], env=env, capture_output=True, text=True, timeout=30)
    calls = [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []
    tools = [c for c in calls if c[0] != "sudo"]
    return proc, tools, calls


@pytest.mark.parametrize("command,expected", [
    ("zpool status -x", ["zpool", "status", "-x"]),
    ("pvesm status", ["pvesm", "status"]),
    ("smartctl -H /dev/nvme0n1", ["smartctl", "-H", "/dev/nvme0n1"]),
    ("smartctl -A /dev/nvme0n1", ["smartctl", "-A", "/dev/nvme0n1"]),
    ("smartctl -H /dev/sda", ["smartctl", "-H", "/dev/sda"]),
])
def test_allowlisted_commands_run_with_exact_argv(wrapper, command, expected):
    proc, tools, calls = call(wrapper, command)
    assert proc.returncode == 0, proc.stderr
    assert tools == [expected]
    if os.geteuid() != 0:
        assert calls[0][:2] == ["sudo", "-n"], "a non-root forced command goes through sudo -n"


@pytest.mark.parametrize("command", [
    None,                                        # interactive login
    "",
    "zpool status",
    "zpool status -v",
    "zpool status -x ",
    "zpool status -x; rm -rf /",
    "zpool status -x && id",
    "zpool list",
    "pvesm status --verbose",
    "pvesm set local --disable 1",
    "smartctl -H /dev/sdb",                      # not a listed device
    "smartctl -x /dev/sda",
    "smartctl -t long /dev/sda",
    "smartctl -H -A /dev/sda",
    "smartctl -H /dev/sda -d sat",
    "smartctl -H /dev/../dev/sda",
    "smartctl -H /dev/sda\nid",
    "smartctl -H /dev/sda$(id)",
    "smartctl -H /dev/sda `id`",
    "/usr/sbin/smartctl -H /dev/sda",
    "sh -c 'zpool status -x'",
    "bash",
])
def test_everything_else_is_denied(wrapper, command):
    proc, tools, calls = call(wrapper, command)
    assert proc.returncode == 126
    assert "skhealth-remote: denied" in proc.stderr
    assert tools == [] and calls == []


def test_no_listed_devices_means_no_smartctl_at_all(wrapper):
    proc, tools, _ = call(wrapper, "smartctl -H /dev/sda", devices=())
    assert proc.returncode == 126 and tools == []
    proc, tools, _ = call(wrapper, "zpool status -x", devices=())
    assert proc.returncode == 0 and tools == [["zpool", "status", "-x"]]


def test_wrapper_never_evaluates_input_through_a_shell():
    text = WRAPPER.read_text()
    assert "eval" not in text and "bash -c" not in text and "sh -c" not in text
    assert 'exec "$@"' in text and 'exec "$SUDO" -n "$@"' in text
