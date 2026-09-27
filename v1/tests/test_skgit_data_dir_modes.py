"""skgit must not `chmod -R 755` Forgejo's data dir.

The post-deploy "Fix Forgejo data permissions and start runners" task ran
`chmod -R 755 /var/data/skgit-<env>/data` on every deploy. That bind mount is
Forgejo's /data, which holds its private keys: the image's OpenSSH host keys
(/data/ssh/ssh_host_*_key, written by ssh-keygen 0600), the built-in SSH
server's host key (APP_DATA_PATH/ssh/gitea.rsa, Forgejo's GenKeyPair, 0600),
the OAuth2/JWT signing key (APP_DATA_PATH/jwt/private.pem) and every
repository. After a deploy they were all world-readable (and executable) on
the shared /var/data. #87 fixed the same shape for the config dir (app.ini).

Forgejo runs as the container user 1000:1000 (skgit.yml.j2 `user:`; the image
maps its `git` user to uid/gid 1000), so owner permissions are all it needs.
The task now keeps the chown, drops the blanket mode: owner rwX (existing
execute bits, e.g. git hooks, are kept), group read, other nothing, so dirs
end up 0750; private key files are 0600 and ~git/.ssh 0700. The data dir is
also created 0750, so the create task and the post-deploy fix agree.

The behaviour test runs the task's own shell body against a scratch tree.
"""
import os
import pathlib
import re
import stat
import subprocess

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


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_fix_script_leaves_keys_0600_and_nothing_world_readable(pb, tmp_path):
    env = _env(pb)
    root = tmp_path / f"skgit-{env}"
    data = root / "data"
    # what a Forgejo 15 /data looks like after a first start, plus the damage
    # an earlier `chmod -R 755` left behind
    keys = [
        data / "ssh/ssh_host_ed25519_key",
        data / "ssh/ssh_host_rsa_key",
        data / "forgejo/ssh/gitea.rsa",
        data / "forgejo/jwt/private.pem",
        data / "git/.ssh/authorized_keys",
    ]
    for k in keys:
        _mkfile(k, 0o755)
    _mkfile(data / "ssh/ssh_host_ed25519_key.pub", 0o755)
    hook = data / "git/repositories/u/r.git/hooks/pre-receive"
    _mkfile(hook, 0o755, "#!/bin/sh\n")
    blob = data / "git/repositories/u/r.git/objects/ab/cdef"
    _mkfile(blob, 0o644)
    (root / "config").mkdir(parents=True)
    for d in [p for p in data.rglob("*") if p.is_dir()] + [data]:
        d.chmod(0o755)

    script = fix_script(pb).replace(f"/var/data/skgit-{env}", str(root))
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env={**os.environ, "SKGIT_DIND_NODE": ""})
    assert r.returncode == 0, r.stderr

    def mode(p):
        return stat.S_IMODE(p.stat().st_mode)

    for k in keys:
        assert mode(k) == 0o600, f"{k.relative_to(root)} is {oct(mode(k))}"
    assert mode(data / "git/.ssh") == 0o700
    for p in [data, *data.rglob("*")]:
        assert mode(p) & 0o007 == 0, f"{p.relative_to(root)} is {oct(mode(p))}: other has access"
        assert mode(p) & 0o700 in (0o600, 0o700), f"{p.relative_to(root)} is {oct(mode(p))}: owner lost access"
    assert mode(hook) & 0o100, "git hook lost its execute bit"
    assert mode(data) == 0o750
