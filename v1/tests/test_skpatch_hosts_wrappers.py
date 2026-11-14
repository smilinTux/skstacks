"""The root-owned selected-package upgrade wrappers of the skpatch-hosts role
(src/skpatch-hosts/skpatch-apt-only-upgrade.sh and skpatch-snap-refresh.sh).

Why wrappers: the dashboard's "upgrade selected packages" action runs
`apt-get ... install --only-upgrade -y <pkgs>` (or `snap refresh <names>`),
and a sudoers wildcard for either lets the caller pass an option such as
`-o APT::Update::Pre-Invoke::=<cmd>` and become root. Sudoers grants only the
wrappers, and the wrappers validate every argument before running anything.

How the scripts are executed here: the installed scripts hard-code
`APT_GET=/usr/bin/apt-get` / `SNAP=/usr/bin/snap` and read no override from
argv or the environment (a test below pins that), so nothing can redirect them
under sudo. The tests therefore run a COPY of each script in which that one
assignment line is rewritten to point at a stub that records its argv and
environment. The validation and exec logic under test is byte for byte the
shipped file.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROLE = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skpatch-hosts"
SRC = ROLE / "src" / "skpatch-hosts"

APT = ("skpatch-apt-only-upgrade", "APT_GET", "/usr/bin/apt-get")
SNAP = ("skpatch-snap-refresh", "SNAP", "/usr/bin/snap")

SHELLS = [s for s in ("/bin/sh", shutil.which("dash"), shutil.which("bash")) if s and Path(s).exists()]

# The wrapper clears the environment (env -i), so the stub cannot be told where
# to write through a variable: its output path is baked into the stub itself.
STUB = """#!/bin/sh
{
  printf 'ARGV'
  for a in "$@"; do printf '\\0%s' "$a"; done
  printf '\\n'
  env
} > "@OUT@"
"""

# invalid for the apt wrapper: must exit non-zero and never reach the stub
APT_BAD = [
    [],
    ["-o"],
    ["--allow-downgrades"],
    ["foo;rm"],
    ["Foo"],
    [""],
    ["openssl", "-o"],            # one bad argument poisons the whole call
    ["openssl", ""],
    ["openssl", "Foo"],
    ["foo bar"],
    ["foo\nbar"],
    ["foo=1.2"],
    ["foo/jammy"],
    ["foo*"],
    ["$(id)"],
    ["`id`"],
    ["-"],
    [".hidden"],
    [":amd64"],
    ["+x"],
    ["-o", "APT::Update::Pre-Invoke::=id"],
]
APT_GOOD = ["openssl", "libc6:amd64", "g++-13", "linux-image-6.8.0-45-generic", "libsdl1.2debian", "0ad"]

SNAP_BAD = [
    [],
    ["-x"],
    ["--classic"],
    ["--channel=edge"],
    ["foo;rm"],
    ["Foo"],
    [""],
    ["core22", "--amend"],
    ["foo_bar"],          # parallel-install instance names are deliberately refused
    ["foo.bar"],
    ["foo bar"],
    ["foo\nbar"],
    ["-"],
    ["$(id)"],
]
SNAP_GOOD = ["core22", "lxd", "snapd", "hello-world", "chromium", "gtk-common-themes"]


@pytest.fixture(params=SHELLS, ids=lambda s: Path(s).name if Path(s).name != "sh" else "sh")
def shell(request):
    return request.param


def build(tmp: Path, spec) -> tuple[Path, Path]:
    """Copy the shipped script with its one binary-path line pointed at a stub."""
    name, var, real = spec
    text = (SRC / f"{name}.sh").read_text()
    line = f"{var}={real}\n"
    assert text.count(line) == 1, f"{name}.sh must carry exactly one `{line.strip()}` line"
    stub = tmp / "stub"
    stub.write_text(STUB.replace("@OUT@", str(tmp / "stub.out")))
    stub.chmod(0o755)
    script = tmp / name
    script.write_text(text.replace(line, f"{var}={stub}\n"))
    return script, tmp / "stub.out"


def run(shell, script, out, args, env=None):
    base = {"HOME": "/nonexistent"}
    base.update(env or {})
    return subprocess.run([shell, str(script), *args], env=base, capture_output=True, text=True)


def recorded(out: Path):
    raw = out.read_text().split("\n", 1)
    argv = raw[0].split("\0")[1:]
    envs = dict(l.split("=", 1) for l in raw[1].splitlines() if "=" in l)
    return argv, envs


# --- apt wrapper ----------------------------------------------------------

@pytest.mark.parametrize("args", APT_BAD, ids=lambda a: repr(a))
def test_apt_wrapper_rejects_before_running_anything(tmp_path, shell, args):
    script, out = build(tmp_path, APT)
    proc = run(shell, script, out, args)
    assert proc.returncode != 0
    assert not out.exists(), "apt-get stub was reached although an argument was invalid"
    assert "skpatch-apt-only-upgrade:" in proc.stderr
    assert proc.stdout == ""


@pytest.mark.parametrize("name", APT_GOOD)
def test_apt_wrapper_accepts_real_package_names(tmp_path, shell, name):
    script, out = build(tmp_path, APT)
    proc = run(shell, script, out, [name])
    assert proc.returncode == 0, proc.stderr
    argv, _ = recorded(out)
    assert argv == ["-o", "DPkg::Lock::Timeout=60", "install", "--only-upgrade", "-y", "--", name]


def test_apt_wrapper_passes_several_names_in_order(tmp_path, shell):
    script, out = build(tmp_path, APT)
    assert run(shell, script, out, ["curl", "libc6:amd64", "g++-13"]).returncode == 0
    argv, _ = recorded(out)
    assert argv[-4:] == ["--", "curl", "libc6:amd64", "g++-13"]


def test_apt_wrapper_runs_with_a_fixed_environment(tmp_path, shell):
    script, out = build(tmp_path, APT)
    proc = run(shell, script, out, ["curl"],
               env={"APT_CONFIG": "/tmp/evil.conf", "LD_PRELOAD": "/tmp/evil.so",
                    "PATH": "/tmp/evil-bin", "BASH_ENV": "/tmp/evil.sh", "ENV": "/tmp/evil.sh"})
    assert proc.returncode == 0, proc.stderr
    _, envs = recorded(out)
    for leaked in ("APT_CONFIG", "LD_PRELOAD", "BASH_ENV", "ENV", "HOME"):
        assert leaked not in envs, f"{leaked} reached apt-get"
    assert envs["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"
    assert envs["DEBIAN_FRONTEND"] == "noninteractive"
    assert envs["LC_ALL"] == "C"


# --- snap wrapper ---------------------------------------------------------

@pytest.mark.parametrize("args", SNAP_BAD, ids=lambda a: repr(a))
def test_snap_wrapper_rejects_before_running_anything(tmp_path, shell, args):
    script, out = build(tmp_path, SNAP)
    proc = run(shell, script, out, args)
    assert proc.returncode != 0
    assert not out.exists(), "snap stub was reached although an argument was invalid"
    assert "skpatch-snap-refresh:" in proc.stderr


@pytest.mark.parametrize("name", SNAP_GOOD)
def test_snap_wrapper_accepts_real_snap_names(tmp_path, shell, name):
    script, out = build(tmp_path, SNAP)
    proc = run(shell, script, out, [name])
    assert proc.returncode == 0, proc.stderr
    argv, envs = recorded(out)
    assert argv == ["refresh", "--", name]
    assert envs["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"


def test_snap_wrapper_runs_with_a_fixed_environment(tmp_path, shell):
    script, out = build(tmp_path, SNAP)
    assert run(shell, script, out, ["lxd"], env={"SNAPD_DEBUG": "1", "PATH": "/tmp/evil-bin"}).returncode == 0
    _, envs = recorded(out)
    assert "SNAPD_DEBUG" not in envs and "HOME" not in envs
    assert envs["PATH"] == "/usr/sbin:/usr/bin:/sbin:/bin"


# --- the shipped files themselves -------------------------------------------

@pytest.mark.parametrize("spec", [APT, SNAP], ids=["apt", "snap"])
def test_shipped_wrapper_has_a_fixed_binary_path_and_no_override_hooks(spec):
    name, var, real = spec
    text = (SRC / f"{name}.sh").read_text()
    # an absolute, literal path: no ${VAR:-default}, no `command -v`, no argv or env lookup
    assert re.search(rf"^{var}={re.escape(real)}$", text, re.M)
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    assert "${" not in code, "parameter expansion with a default would allow an override"
    assert not re.search(r"\beval\b", code)
    assert "command -v" not in code
    assert text.startswith("#!/bin/sh\n")
    assert "set -eu" in text
    assert 'exec "$ENV_BIN" -i' in text
    assert "PATH=/usr/sbin:/usr/bin:/sbin:/bin" in text


def test_shipped_wrappers_have_no_dashes_in_prose():
    for name in (APT[0], SNAP[0]):
        text = (SRC / f"{name}.sh").read_text()
        assert chr(0x2014) not in text and chr(0x2013) not in text
