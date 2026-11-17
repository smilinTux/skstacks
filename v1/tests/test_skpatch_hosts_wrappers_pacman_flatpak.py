"""The pacman and flatpak selected-package wrappers of the skpatch-hosts role
(src/skpatch-hosts/skpatch-pacman-upgrade.sh and skpatch-flatpak-update.sh),
run the same way as the apt/snap ones in test_skpatch_hosts_wrappers.py: a
copy of the shipped script whose one binary-path line points at a stub.

The pacman wrapper also refuses a name that is not installed (`pacman -Q`
fails), so "upgrade selected" can never install a new package."""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_skpatch_hosts_wrappers import SHELLS, SRC, build, recorded, run  # noqa: E402

PACMAN = ("skpatch-pacman-upgrade", "PACMAN", "/usr/bin/pacman")
FLATPAK = ("skpatch-flatpak-update", "FLATPAK", "/usr/bin/flatpak")

# The pacman stub answers `-Q -- <name>` with success unless the name starts
# with "missing", and records every call (the last call is what ran last).
PACMAN_STUB = """#!/bin/sh
if [ "$1" = "-Q" ]; then
  case $3 in missing*) exit 1 ;; esac
  exit 0
fi
{
  printf 'ARGV'
  for a in "$@"; do printf '\\0%s' "$a"; done
  printf '\\n'
  env
} > "@OUT@"
"""

PACMAN_BAD = [[], ["-U"], ["--config"], ["-Syu"], ["foo;rm"], ["Foo"], [""], ["firefox", "--hookdir"],
              ["foo bar"], ["foo\nbar"], ["foo/bar"], ["core/firefox"], ["foo=1"], ["$(id)"], ["-"], [".x"], ["-x"]]
PACMAN_GOOD = ["firefox", "linux612", "lib32-glibc", "python-pip", "gtk3", "libc++", "@scope", "_x"]
FLATPAK_BAD = [[], ["-y"], ["--user"], ["--commit=abc"], ["app/org.mozilla.firefox/x86_64/stable"], ["foo;rm"],
               [""], ["org.mozilla.firefox", "--no-deps"], ["foo bar"], ["foo\nbar"], ["$(id)"], ["-"], [".x"], ["_x"]]
FLATPAK_GOOD = ["org.mozilla.firefox", "com.rustdesk.RustDesk", "org.freedesktop.Platform", "org.gtk.Gtk3theme.Adwaita-dark"]


@pytest.fixture(params=SHELLS, ids=lambda s: Path(s).name)
def shell(request):
    return request.param


def build_pacman(tmp):
    script, out = build(tmp, PACMAN)
    (tmp / "stub").write_text(PACMAN_STUB.replace("@OUT@", str(out)))
    return script, out


@pytest.mark.parametrize("args", PACMAN_BAD, ids=repr)
def test_pacman_wrapper_rejects_before_running_anything(tmp_path, shell, args):
    script, out = build_pacman(tmp_path)
    res = run(shell, script, out, args)
    assert res.returncode == 2, res
    assert not out.exists()


@pytest.mark.parametrize("name", PACMAN_GOOD)
def test_pacman_wrapper_upgrades_an_installed_package(tmp_path, shell, name):
    script, out = build_pacman(tmp_path)
    assert run(shell, script, out, [name]).returncode == 0
    argv, envs = recorded(out)
    assert argv == ["-S", "--noconfirm", "--", name]
    assert envs["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"


def test_pacman_wrapper_refuses_a_package_that_is_not_installed(tmp_path, shell):
    script, out = build_pacman(tmp_path)
    res = run(shell, script, out, ["firefox", "missing-pkg"])
    assert res.returncode == 2 and "not installed" in res.stderr
    assert not out.exists()


def test_pacman_wrapper_runs_with_a_fixed_environment(tmp_path, shell):
    script, out = build_pacman(tmp_path)
    assert run(shell, script, out, ["firefox", "gtk3"], env={"LD_PRELOAD": "/tmp/x.so", "PATH": "/tmp/evil"}).returncode == 0
    argv, envs = recorded(out)
    assert argv == ["-S", "--noconfirm", "--", "firefox", "gtk3"]
    assert "LD_PRELOAD" not in envs and "HOME" not in envs


@pytest.mark.parametrize("args", FLATPAK_BAD, ids=repr)
def test_flatpak_wrapper_rejects_before_running_anything(tmp_path, shell, args):
    script, out = build(tmp_path, FLATPAK)
    res = run(shell, script, out, args)
    assert res.returncode == 2, res
    assert not out.exists()


@pytest.mark.parametrize("name", FLATPAK_GOOD)
def test_flatpak_wrapper_updates_only(tmp_path, shell, name):
    script, out = build(tmp_path, FLATPAK)
    assert run(shell, script, out, [name], env={"FLATPAK_USER_DIR": "/tmp/x"}).returncode == 0
    argv, envs = recorded(out)
    assert argv == ["update", "-y", "--noninteractive", "--", name]
    assert "FLATPAK_USER_DIR" not in envs


@pytest.mark.parametrize("spec", [PACMAN, FLATPAK], ids=["pacman", "flatpak"])
def test_shipped_wrapper_has_a_fixed_binary_path_and_no_override_hooks(spec):
    name, var, real = spec
    text = (SRC / f"{name}.sh").read_text()
    assert re.search(rf"^{var}={re.escape(real)}$", text, re.M)
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "${" not in code and not re.search(r"\beval\b", code) and "command -v" not in code
    assert text.startswith("#!/bin/sh\n") and "set -eu" in text and 'exec "$ENV_BIN" -i' in text
    assert chr(0x2014) not in text and chr(0x2013) not in text
