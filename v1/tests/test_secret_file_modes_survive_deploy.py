"""A secret-bearing file's mode must survive the rest of its own deploy.

test_secret_files_not_world_readable.py checks the mode each `template` task
writes. The v2.21.0 release test (skstack06, fresh cluster) then found three
of those files 0755 on the node, because a LATER task in the same playbook
re-moded them:

  * skreg: "Create skreg docker-compose.yaml symlink" is `file: state=link`
    with `mode: 0755`. `file` follows the link by default, so every deploy
    chmods the target skreg.yml (registry HTTP secret) back to 0755 right
    after the template set 0600.
  * skgit: the post-deploy "Fix Forgejo data permissions" shell ran
    `chmod -R 755` over the config dir, which holds app.ini (secret key,
    tokens, DB and mail passwords).

This test walks every v1 deploy playbook in task order (blocks and imported
task files included) and fails when, after a template task writes a
secret-bearing file, a later task
  * is a `file` task with `state: link` (following the link, the default)
    whose `src` is that file and that sets `mode`, or
  * is a `file` task on that file (or, with `recurse`, a parent dir) whose
    mode grants "other" read, or
  * is a shell/command that runs `chmod` granting "other" read on that file,
    or `chmod -R` on a parent directory.
"""
import re

import pytest
import yaml

from test_secret_files_not_world_readable import (
    ALLOWED,
    ANSIBLE,
    PLAYBOOKS,
    _render,
    _vars,
    _walk,
    secret_reason,
    world_readable,
)

pytestmark = pytest.mark.filterwarnings("ignore:.*invalid escape sequence")

FILE_KEYS = ("file", "ansible.builtin.file")
SHELL_KEYS = ("shell", "command", "ansible.builtin.shell", "ansible.builtin.command")
CHMOD = re.compile(r"\bchmod\s+((?:-[A-Za-z]+\s+)*)(\S+)\s+([^\s;|&]+)")


def _flatten(tasks, ctx, base, origin):
    """Yield (origin, task, ctx) in execution order, expanding blocks/imports."""
    for task in tasks or []:
        tctx = _vars(task.get("vars"), ctx)
        if any(k in task for k in ("block", "rescue", "always")):
            for key in ("block", "rescue", "always"):
                yield from _flatten(task.get(key), tctx, base, origin)
            continue
        inc = None
        for key in ("import_tasks", "include_tasks", "ansible.builtin.import_tasks", "ansible.builtin.include_tasks"):
            if key in task:
                inc = task[key]
                inc = inc.get("file") if isinstance(inc, dict) else inc
        if inc:
            path = (base / _render(inc, tctx)).resolve()
            yield from _flatten(yaml.safe_load(path.read_text()), tctx, base, path.relative_to(ANSIBLE))
            continue
        yield origin, task, tctx


def _safe_render(value, ctx):
    """Render like _render; on a filter/test this harness lacks, fall back to
    substituting the plain {{ var }} references it can resolve."""
    if isinstance(value, dict):
        return {k: _safe_render(v, ctx) for k, v in value.items()}
    try:
        return _render(value, ctx)
    except Exception:
        if not isinstance(value, str):
            return value
        return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(ctx.get(m.group(1), m.group(0))), value)


def _grants_other_read(mode):
    mode = str(mode).strip()
    if re.fullmatch(r"0?[0-7]{3,4}", mode):
        return world_readable(mode)
    if re.fullmatch(r"\d+", mode):  # YAML int such as 493 (0755)
        return int(mode) & 0o004 != 0
    # symbolic: o+r / a+r / +r / o=r...
    return any(re.search(r"(^|,)[ugoa]*[oa][+=][^,]*r|(^|,)[+=][^,]*r", part) for part in [mode])


def _parents(path):
    parts = path.rstrip("/").split("/")
    return {"/".join(parts[:i]) for i in range(2, len(parts))}


def remode_findings(playbook):
    base = playbook.parent
    out = []
    for play in yaml.safe_load(playbook.read_text()):
        if "hosts" not in play:
            continue
        ctx = _vars(play.get("vars"), {"playbook_dir": str(base), "env": "dev"})
        steps = []
        for section in ("pre_tasks", "tasks", "post_tasks"):
            steps.extend(_flatten(play.get(section), ctx, base, playbook.relative_to(ANSIBLE)))
        secrets = {}  # dest -> reason, for templates seen so far
        for origin, task, tctx in steps:
            for _, _, src, dest, mode in _walk([task], tctx, base, origin):
                reason = secret_reason(src, dest, ALLOWED)
                if reason:
                    secrets[dest] = reason
            if not secrets:
                continue
            name = task.get("name", "?")
            spec = next((task[k] for k in FILE_KEYS if isinstance(task.get(k), dict)), None)
            if spec is not None:
                spec = _safe_render(spec, tctx)
                state = str(spec.get("state", "file"))
                follow = spec.get("follow", True) not in (False, "no", "false", "False")
                mode = spec.get("mode")
                if state == "link" and follow and mode is not None and str(spec.get("src")) in secrets:
                    out.append(f"{origin}: '{name}' is a link task with mode {mode!r}, which follows the link "
                               f"and re-modes {spec.get('src')} [{secrets[str(spec.get('src'))]}]")
                path = str(spec.get("path", spec.get("dest", "")))
                if state != "link" and mode is not None and _grants_other_read(mode):
                    hit = path in secrets or (spec.get("recurse") and any(path in _parents(d) for d in secrets))
                    if hit:
                        out.append(f"{origin}: '{name}' sets mode {mode!r} on {path}, which holds a secret-bearing file")
                continue
            text = next((task[k] for k in SHELL_KEYS if k in task), None)
            if isinstance(text, dict):
                text = text.get("cmd", "")
            if not isinstance(text, str):
                continue
            text = _safe_render(text, tctx)
            for flags, mode, target in CHMOD.findall(text):
                target = target.rstrip("/")
                recursive = "R" in flags
                if not _grants_other_read(mode):
                    continue
                for dest, reason in secrets.items():
                    if target == dest or (recursive and target in _parents(dest)):
                        out.append(f"{origin}: '{name}' runs chmod {flags}{mode} {target}, re-moding {dest} [{reason}]")
    return out


def test_walker_sees_link_and_shell_tasks():
    seen = 0
    for pb in PLAYBOOKS:
        for play in yaml.safe_load(pb.read_text()):
            if "hosts" not in play:
                continue
            ctx = _vars(play.get("vars"), {"playbook_dir": str(pb.parent), "env": "dev"})
            for section in ("pre_tasks", "tasks", "post_tasks"):
                seen += sum(1 for _ in _flatten(play.get(section), ctx, pb.parent, pb.relative_to(ANSIBLE)))
    assert seen > 2000


def test_detector_catches_remodes(tmp_path):
    """Sabotage: the three shapes found on the release test must be caught."""
    svc = tmp_path / "optional" / "svc"
    (svc / "templates").mkdir(parents=True)
    (svc / "templates" / "svc.yml.j2").write_text("secret: {{ svc.REGISTRY_HTTP_SECRET }}\n")
    tasks = [
        {"name": "compose", "template": {"src": "svc.yml.j2", "dest": "/var/data/config/svc-dev/svc.yml", "mode": "0600"}},
        {"name": "link", "file": {"src": "/var/data/config/svc-dev/svc.yml",
                                  "dest": "/var/data/config/svc-dev/docker-compose.yaml",
                                  "state": "link", "mode": "0755"}},
        {"name": "fix perms", "shell": "chmod -R 755 /var/data/config/svc-dev || true\n"},
        {"name": "ok link", "file": {"src": "/var/data/config/svc-dev/svc.yml",
                                     "dest": "/var/data/config/svc-dev/other.yaml", "state": "link"}},
        {"name": "ok chmod", "shell": "chmod 755 /var/data/config/svc-dev\n"},
    ]
    pb = svc / "deploy_svc-dev.yml"
    pb.write_text(yaml.safe_dump([{"hosts": "x", "vars": {"app": "svc"}, "tasks": tasks}]))
    import test_secret_file_modes_survive_deploy as mod
    real = mod.ANSIBLE
    mod.ANSIBLE = tmp_path
    try:
        found = remode_findings(pb)
    finally:
        mod.ANSIBLE = real
    assert any("'link'" in f for f in found), found
    assert any("'fix perms'" in f for f in found), found
    assert not any("'ok link'" in f or "'ok chmod'" in f for f in found), found
    assert _grants_other_read(493) and _grants_other_read("0755") and not _grants_other_read("0600")


def test_secret_file_modes_survive_the_rest_of_the_deploy():
    bad = []
    for pb in PLAYBOOKS:
        bad.extend(remode_findings(pb))
    assert not bad, f"{len(bad)} later task(s) re-mode a secret-bearing file:\n" + "\n".join(sorted(set(bad)))
