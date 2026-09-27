"""No v1 deploy playbook may write a secret-bearing file world-readable.

The deploy templates land on the shared /var/data filesystem (NFS across the
swarm), so a mode ending in a non-zero digit hands the file to every account on
every node that mounts the share. skmail.env was fixed to 0600 in PR #71;
skpeek.env (and the env files of most other services) were still 0644 while
carrying DB passwords, secret keys and free-form `extra_env` values.

This test walks every v1 deploy playbook (plus the task files it imports),
resolves each `template` task's src/dest/mode (loops included), and fails when
a secret-bearing file is rendered with the "other" permission bits set.

A file is secret-bearing when:
  * its dest is an env file (`*.env`): the service env_file, which the
    instance vault fills and `extra_env` may extend with anything, or
  * its source template interpolates a secret-looking variable
    (PASSWORD, SECRET, TOKEN, *_KEY, extra_env, ...).

Env files are read by the docker daemon (root) via compose `env_file`, or
sourced by the root-run deploy script, so 0600 root:root is the target. A file
bind-mounted into a container that runs as a non-root user keeps group read
(0640) with a group that user holds; that is still not world-readable.

Proven non-secret files are listed in ALLOWED with the reason.
"""
import glob
import pathlib
import re

import jinja2
import jinja2.nativetypes
import pytest
import yaml

# The playbooks' own regex_replace('\\.j2$') strings trip Python's escape warning
# when compiled as Jinja source here; Ansible compiles them the same way.
pytestmark = pytest.mark.filterwarnings("ignore:.*invalid escape sequence")

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
PLAYBOOKS = sorted(ANSIBLE.glob("*/*/deploy_*.yml"))

# A variable reference, inside {{ }} or {% %}, whose name looks like a secret.
SECRET_NAME = re.compile(
    r"(?i)(pass(word|wd)?|secret|token|credential|api_?key|private_?key|(^|_)key($|_)|extra_env)"
)
# Only what a template WRITES counts: {{ ... }} and {% set ... %}. A bare
# `{% if x.PASSWORD is defined %}` guard puts nothing secret in the file.
JINJA_EXPR = re.compile(r"\{\{(.*?)\}\}|\{%-?\s*set\b(.*?)%\}", re.S)
STRING = re.compile(r"'[^']*'|\"[^\"]*\"")
IDENT = re.compile(r"[A-Za-z_][\w.]*")

# Variable names that match SECRET_NAME but hold no secret.
NOT_SECRET = {
    "skmail.SSL_KEY_PATH": "a host path to the TLS key, bind-mounted; the path is not the key",
}

# Env files proven to carry no secret, keyed by template path relative to
# v1/ansible. This only exempts the "*.env dest" rule: if the template ever
# interpolates a secret-looking variable, the file fails again regardless.
ALLOWED = {
    "optional/skmail/src/config/skmail/skmail.sh.env.j2": (
        "sourced by the helper scripts for CLUSTERNAME/DOMAIN/INSTANCE/APP_ENV/HOSTNAME only; "
        "the relay credentials live in skmail.env (0600)"
    ),
    "optional/skpulse/src/config/skpulse/skpulse.env.j2": (
        "base URL, CLUSTERNAME, DOMAIN and PORT only; Uptime Kuma keeps its own credentials in its data volume"
    ),
    "optional/skwhoami/src/config/skwhoami/skwhoami.env.j2": (
        "DOMAIN, CLUSTERNAME and APP_ENV only; whoami has no credentials"
    ),
}


def _basename(value):
    return pathlib.PurePosixPath(str(value)).name


def _regex_replace(value, pattern, repl=""):
    return re.sub(pattern, repl, str(value))


def _product(a, b):
    return [[x, y] for x in a for y in b]


def _env(native=False):
    cls = jinja2.nativetypes.NativeEnvironment if native else jinja2.Environment
    env = cls(undefined=jinja2.ChainableUndefined)
    env.filters.update(basename=_basename, regex_replace=_regex_replace, product=_product)
    return env


TEXT, NATIVE = _env(), _env(native=True)


def _render(value, ctx, native=False):
    if isinstance(value, str) and ("{{" in value or "{%" in value):
        out = (NATIVE if native else TEXT).from_string(value).render(**ctx)
        return out
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    return value


def _vars(raw, ctx):
    ctx = dict(ctx)
    for k, v in (raw or {}).items():
        ctx[k] = _render(v, ctx)
    return ctx


class _RuntimeItem(str):
    """Stands in for an item only known at deploy time."""

    def __new__(cls):
        return super().__new__(cls, "RUNTIME_ITEM")

    def __getattr__(self, name):
        return _RuntimeItem()

    def __getitem__(self, key):
        return _RuntimeItem()


def _items(task, ctx, base):
    if "with_fileglob" in task:
        pats = task["with_fileglob"]
        pats = pats if isinstance(pats, list) else [pats]
        out = []
        for p in pats:
            p = _render(p, ctx)
            p = p if p.startswith("/") else str(base / "files" / p)
            out.extend(sorted(glob.glob(p)))
        return out
    loop = task.get("loop", task.get("with_items"))
    if loop is None:
        return [None]
    if isinstance(loop, str):
        loop = _render(loop, ctx, native=True)
    if not isinstance(loop, list):
        # A runtime loop (registered results, inventory groups, vault lists)
        # may only vary the dest name: src and mode must not depend on `item`,
        # so one placeholder item stands for every iteration.
        mod = task.get("template") or task.get("ansible.builtin.template") or {}
        if any("item" in str(mod.get(k, "")) for k in ("src", "mode")):
            raise AssertionError(f"unresolvable loop {task.get('loop')!r} in {task.get('name')}")
        return [_RuntimeItem()]
    return [_render(i, ctx) for i in loop]


def _walk(tasks, ctx, base, origin):
    """Yield (origin, task_name, src, dest, mode) for every template task."""
    for task in tasks or []:
        tctx = _vars(task.get("vars"), ctx)
        for key in ("block", "rescue", "always"):
            if key in task:
                yield from _walk(task[key], tctx, base, origin)
        inc = None
        for key in ("import_tasks", "include_tasks", "ansible.builtin.import_tasks", "ansible.builtin.include_tasks"):
            if key in task:
                inc = task[key]
                inc = inc.get("file") if isinstance(inc, dict) else inc
        if inc:
            path = (base / _render(inc, tctx)).resolve()
            yield from _walk(yaml.safe_load(path.read_text()), tctx, base, path.relative_to(ANSIBLE))
            continue
        mod = task.get("template") or task.get("ansible.builtin.template")
        if not mod:
            continue
        for item in _items(task, tctx, base):
            ictx = dict(tctx, item=item)
            src = _render(mod["src"], ictx)
            src = pathlib.Path(src if src.startswith("/") else base / "templates" / src)
            mode = mod.get("mode")
            mode = "" if mode is None else _render(str(mode) if isinstance(mode, str) else "%o" % mode, ictx)
            yield origin, task.get("name", "?"), src, _render(mod["dest"], ictx), mode


def rendered_files(playbook):
    base = playbook.parent
    for play in yaml.safe_load(playbook.read_text()):
        if "hosts" not in play:
            continue
        ctx = _vars(play.get("vars"), {"playbook_dir": str(base), "env": "dev"})
        for section in ("pre_tasks", "tasks", "post_tasks"):
            yield from _walk(play.get(section), ctx, base, playbook.relative_to(ANSIBLE))


def secret_names(src):
    names = set()
    for m in JINJA_EXPR.finditer(src.read_text(errors="replace")):
        expr = STRING.sub("", m.group(1) or m.group(2))
        for ident in IDENT.findall(expr):
            if ident in NOT_SECRET:
                continue
            if any(SECRET_NAME.search(part) and part != "key" for part in ident.split(".")):
                names.add(ident)
    return names


def _rel(src):
    src = src.resolve()
    return str(src.relative_to(ANSIBLE)) if src.is_relative_to(ANSIBLE) else str(src)


def secret_reason(src, dest, allowed=()):
    names = secret_names(src)
    if names:
        return ", ".join(sorted(names))
    if dest.endswith(".env") and _rel(src) not in allowed:
        return "env file"
    return None


def world_readable(mode):
    if not mode:
        return True  # no mode: the umask decides, which is 0644 on a stock host
    mode = mode.strip()
    if not re.fullmatch(r"0?[0-7]{3,4}", mode):
        raise AssertionError(f"cannot evaluate mode {mode!r}")
    return int(mode, 8) & 0o007 != 0


def test_playbooks_found():
    assert len(PLAYBOOKS) > 60


def test_every_playbook_resolves():
    """The walker must see every template task, or the gate is blind to it."""
    total = 0
    for pb in PLAYBOOKS:
        for _, _, src, dest, _ in rendered_files(pb):
            assert src.exists(), f"{pb.name}: template {src} missing"
            assert "{{" not in dest, f"{pb.name}: unresolved dest {dest}"
            total += 1
    assert total > 300


def test_allow_list_entries_exist_and_have_reasons():
    for rel, reason in ALLOWED.items():
        assert (ANSIBLE / rel).exists(), f"stale allow-list entry {rel}"
        assert rel.endswith(".env.j2"), f"{rel}: only env files may be allow-listed"
        assert not secret_names(ANSIBLE / rel), f"{rel} now interpolates a secret"
        assert len(reason) > 20, f"{rel}: give a real reason"


def test_detector_catches_and_ignores(tmp_path):
    """Sabotage check: the detector must see a secret and skip non-secrets."""
    hit = tmp_path / "a.yml.j2"
    hit.write_text("pw: {{ svc.POSTGRES_PASSWORD }}\n")
    assert secret_reason(hit, "/x/a.yml") == "svc.POSTGRES_PASSWORD"
    miss = tmp_path / "b.yml.j2"
    miss.write_text("{% if svc.API_TOKEN is defined %}on{% endif %}\nh: {{ {'X-Api-Key': 1} }}\n")
    assert secret_reason(miss, "/x/b.yml") is None
    assert secret_reason(miss, "/x/b.env") == "env file"
    assert world_readable("0644") and world_readable("") and not world_readable("0640")


def test_secret_bearing_files_not_world_readable():
    bad = []
    for pb in PLAYBOOKS:
        for origin, name, src, dest, mode in rendered_files(pb):
            reason = secret_reason(src, dest, ALLOWED)
            if not reason or not world_readable(mode):
                continue
            rel = _rel(src)
            bad.append(f"{origin}: {dest} mode={mode or '(none)'} [{reason}] <- {rel}")
    assert not bad, f"{len(bad)} secret-bearing files world-readable:\n" + "\n".join(sorted(set(bad)))
