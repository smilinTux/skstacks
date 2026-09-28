"""skgit must not `chmod -R 755` Forgejo's data dir, nor walk all of it.

The post-deploy "Fix Forgejo data permissions and start runners" task ran
`chmod -R 755 /var/data/skgit-<env>/data` on every deploy. That bind mount is
Forgejo's /data, which holds its private keys: the image's OpenSSH host keys
(/data/ssh/ssh_host_*_key, written by ssh-keygen 0600), the built-in SSH
server's host key (APP_DATA_PATH/ssh/gitea.rsa, Forgejo's GenKeyPair, 0600),
the OAuth2/JWT signing key (APP_DATA_PATH/jwt/private.pem) and every
repository. After a deploy they were all world-readable (and executable) on
the shared /var/data. #87 fixed the same shape for the config dir (app.ini).

#89 replaced the mode with `chmod -R u+rwX,g+rX,g-w,o-rwx`, still a full walk
(plus `chown -R`): on a 60G data dir on NFS that was 40+ minutes of one
SETATTR per file, all no-ops, saturating NFS for every other service. The
task is now bounded (the top two levels of data/, the key dirs, config/;
never the repositories, LFS or attachments) and incremental (only entries
whose owner or mode is wrong). data/ itself is 0750, which is what keeps
"other" away from everything below it.

The behaviour tests run the task's own shell body against a scratch tree and
check both the result and that correct entries (and bulk data) keep their
ctime, i.e. were not written at all.
"""
import os
import pathlib
import re
import stat
import subprocess
import time

import jinja2
import pytest
import yaml

from test_secret_file_modes_survive_deploy import CHMOD, _grants_other_read

SKGIT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgit"
PLAYBOOKS = sorted(SKGIT.glob("deploy_skgit-*.yml"))
TASK = "Fix Forgejo data permissions and start runners"


def _tasks(pb):
    out = []
    for play in yaml.safe_load(pb.read_text()):
        for section in ("pre_tasks", "tasks", "post_tasks"):
            out.extend(play.get(section) or [])
    return out


def _env(pb):
    for play in yaml.safe_load(pb.read_text()):
        if "vars" in play and "env" in (play["vars"] or {}):
            return play["vars"]["env"]
    raise AssertionError(f"{pb.name}: no env var")


def fix_script(pb):
    task = next(t for t in _tasks(pb) if t.get("name") == TASK)
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return env.from_string(task["shell"]).render(app="skgit", env=_env(pb))


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_no_chmod_grants_other_read_on_the_data_dir(pb):
    data = f"/var/data/skgit-{_env(pb)}/data"
    bad = []
    for flags, mode, target in CHMOD.findall(fix_script(pb)):
        target = target.strip("\"'").rstrip("/")
        if target.startswith("$"):
            target = data if target in ("$D", "${D}") else target
        if _grants_other_read(mode) and (target == data or target.startswith(data + "/") or ("R" in flags and data.startswith(target + "/"))):
            bad.append(f"chmod {flags}{mode} {target}")
    assert not bad, f"{pb.name}: {bad}"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_data_dir_is_created_0750(pb):
    modes = [
        str(item.get("mode"))
        for t in _tasks(pb)
        for item in (t.get("loop") or [])
        if isinstance(item, dict) and item.get("path") == "/var/data/{{ app }}-{{ env }}/data"
    ]
    assert modes == ["0750"], f"{pb.name}: data dir create modes {modes}"


def _mkfile(path, mode, body="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(mode)


def _mode(p):
    return stat.S_IMODE(p.lstat().st_mode)


KEYS = [
    "ssh/ssh_host_ed25519_key",
    "ssh/ssh_host_rsa_key",
    "forgejo/ssh/gitea.rsa",
    "forgejo/jwt/private.pem",
    "git/.ssh/authorized_keys",
]
# bulk stores: the deploy must never walk into these by default
BULK = [
    "forgejo/forgejo-repositories/u/r.git/objects/ab/cdef",
    "forgejo/forgejo-repositories/u/r.git/HEAD",
    "lfs/aa/bb/aabbccdd",
    "forgejo/attachments/1/2/uuid",
]
HOOK = "forgejo/forgejo-repositories/u/r.git/hooks/pre-receive"


def _scratch_forgejo(root):
    """A Forgejo /data after a first start, plus the damage an earlier
    `chmod -R 755` left behind (keys and repo files world-readable)."""
    data = root / "data"
    for k in KEYS:
        _mkfile(data / k, 0o755)
    _mkfile(data / "ssh/ssh_host_ed25519_key.pub", 0o755)
    _mkfile(data / HOOK, 0o755, "#!/bin/sh\n")
    for b in BULK:
        _mkfile(data / b, 0o644)
    _mkfile(data / "git/.gitconfig", 0o644)
    (data / "log").mkdir()
    _mkfile(root / "config/app.ini", 0o600)
    for d in [p for p in data.rglob("*") if p.is_dir()] + [data]:
        d.chmod(0o755)
    return data


def _run(pb, root, uid=None, gid=None, full_walk="", stub_chown=None):
    """Run the task's own shell body against the scratch tree. The target
    owner is swapped for one this unprivileged test can actually hold."""
    env = _env(pb)
    script = fix_script(pb).replace(f"/var/data/skgit-{env}", str(root))
    uid = os.getuid() if uid is None else uid
    gid = os.getgid() if gid is None else gid
    script, n = re.subn(r"\bU=1000; G=1000\b", f"U={uid}; G={gid}", script)
    assert n == 1, "the task must name its owner once, as U=1000; G=1000"
    path = os.environ["PATH"]
    if stub_chown:
        stub_chown.mkdir(exist_ok=True)
        tool = stub_chown / "chown"
        tool.write_text(f'#!/bin/sh\nfor a in "$@"; do echo "$a"; done >> {stub_chown}/log\n')
        tool.chmod(0o755)
        path = f"{stub_chown}:{path}"
    r = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True,
        env={**os.environ, "PATH": path, "SKGIT_DIND_NODE": "", "SKGIT_PERMS_FULL_WALK": full_walk},
    )
    assert r.returncode == 0, r.stderr
    return r


def _ctimes(root):
    return {p: p.lstat().st_ctime_ns for p in [root, *root.rglob("*")]}


def _reachable_by_other(p, top):
    """Other can open p only if it can search every dir from top down to p."""
    d = p.parent
    while _mode(d) & 0o001:
        if d == top:
            return True
        d = d.parent
    return False


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_fix_script_secures_keys_and_the_data_dir(pb, tmp_path):
    root = tmp_path / f"skgit-{_env(pb)}"
    data = _scratch_forgejo(root)
    _run(pb, root)

    for k in KEYS:
        assert _mode(data / k) == 0o600, f"{k} is {oct(_mode(data / k))}"
    for kd in ("ssh", "forgejo/ssh", "forgejo/jwt", "git/.ssh"):
        assert _mode(data / kd) == 0o700, kd
    assert _mode(data / "ssh/ssh_host_ed25519_key.pub") & 0o027 == 0
    assert _mode(data) == 0o750
    for p in data.iterdir():
        assert _mode(p) & 0o007 == 0, f"{p.name} is {oct(_mode(p))}"
    assert _mode(data / "forgejo/forgejo-repositories") == 0o750
    assert _mode(root / "config/app.ini") == 0o600
    assert _mode(root / "config") == 0o755
    # nothing under data/ is reachable by "other", whatever its own mode
    leaks = [str(p.relative_to(root)) for p in data.rglob("*") if p.is_file() and _mode(p) & 0o004 and _reachable_by_other(p, data)]
    assert not leaks, leaks
    assert _mode(data / HOOK) & 0o100, "git hook lost its execute bit"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_fix_script_never_touches_bulk_data_or_correct_entries(pb, tmp_path):
    root = tmp_path / f"skgit-{_env(pb)}"
    data = _scratch_forgejo(root)
    before = _ctimes(root)
    time.sleep(0.05)
    _run(pb, root)
    after = _ctimes(root)
    # the repositories, LFS and attachments are not walked: an old 0644 on a
    # blob is left alone (data/ 0750 already keeps "other" out)
    for rel in BULK + [HOOK]:
        assert after[data / rel] == before[data / rel], f"{rel} was rewritten"
        assert _mode(data / rel) in (0o644, 0o755)
    # app.ini is already right: untouched
    assert after[root / "config/app.ini"] == before[root / "config/app.ini"]

    # a second deploy over a correct tree writes nothing at all
    time.sleep(0.05)
    _run(pb, root)
    again = _ctimes(root)
    changed = [str(p.relative_to(tmp_path)) for p in after if again[p] != after[p]]
    assert not changed, f"rewritten although already correct: {changed}"


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_ownership_fix_is_bounded_and_incremental(pb, tmp_path):
    root = tmp_path / f"skgit-{_env(pb)}"
    data = _scratch_forgejo(root)
    stub = tmp_path / "stub"
    # already the right owner: chown never runs
    _run(pb, root, stub_chown=stub)
    assert not (stub / "log").exists()
    # every entry has the wrong owner: chown runs, but only on the top of
    # data/, the key dirs and config/, never inside the bulk stores
    _run(pb, root, uid=os.getuid() + 1, stub_chown=stub)
    touched = {pathlib.Path(a) for a in (stub / "log").read_text().split() if a.startswith("/")}
    assert data in touched and data / "forgejo/forgejo-repositories" in touched
    assert data / "forgejo/jwt/private.pem" in touched and root / "config/app.ini" in touched
    for rel in BULK + [HOOK]:
        assert data / rel not in touched, rel
    assert all(len(p.relative_to(data).parts) <= 3 for p in touched if data in p.parents), touched


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_full_walk_knob_strips_old_world_readable_bits(pb, tmp_path):
    root = tmp_path / f"skgit-{_env(pb)}"
    data = _scratch_forgejo(root)
    _run(pb, root, full_walk="1")
    for p in data.rglob("*"):
        assert _mode(p) & 0o007 == 0, f"{p.relative_to(root)} is {oct(_mode(p))}"
    assert _mode(data / HOOK) & 0o100
    task = next(t for t in _tasks(pb) if t.get("name") == TASK)
    assert "skgit.PERMS_FULL_WALK | default(false)" in task["environment"]["SKGIT_PERMS_FULL_WALK"]
