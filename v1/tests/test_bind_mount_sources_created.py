"""Every /var/data bind-mount source in a v1 compose template must be created
by that service's deploy.

Docker Swarm never creates a bind-mount source. A missing one rejects the
task outright ("invalid mount config for type "bind": bind source path does
not exist") and the service sits at 0 replicas. skstack06 (v2.19.0 rc4) hit
exactly this: skbook's compose bind-mounts
/var/data/{{ app }}-{{ env }}/bookstack/config, but deploy_skbook-*.yml only
created /var/data/{{ app }}-{{ env }}/bookstack. A long-lived instance never
notices, because the directory was created by hand (or by an older
framework) years ago; only a fresh cluster exposes it.

How the check works, per service and per env (dev/staging/prod):

* Bind sources come from every compose template in
  ``src/config/<svc>/*.yml.j2`` that has a top-level ``services:`` key.
  Three shapes are read: short syntax ``- /var/data/src:/target[:mode]``,
  long syntax ``source: /var/data/src`` and a local-driver named volume's
  ``device: /var/data/src`` (``o: bind``, which Docker also refuses to
  create). An ``env_file:`` list entry has no ``:target`` and is ignored.
* Paths the deploy creates come from the playbook
  ``deploy_<svc>-<env>.yml``: ``file:`` with ``state: directory`` (``path``),
  and ``template:``/``copy:`` ``dest`` (a file bind source is a rendered
  file), including ``loop``/``with_items``/``with_fileglob`` items (a
  ``copy`` into ``dir/`` lands at ``dir/<basename>``; a ``when`` on a
  fileglob loop is evaluated per item) and ``{{ item.x }}``, plus
  literal ``mkdir -p /var/data/...`` in shell/command tasks. The service's
  own ``src/<svc>/deploy.j2`` counts too when it ``mkdir -p``s a path
  (simple ``VAR=...`` assignments and ``for X in "${ARR[@]}"`` arrays are
  expanded). Every ancestor of a created path exists as well; a rendered
  file's parent must exist or the template task would already have failed.
* Both sides are rendered with real Jinja2 against the play's ``vars``
  (``app``, ``env``, ``service_name``, ...) and any ``{% set %}`` in the
  compose template, so ``{{ app }}``, ``{{app}}``, ``default('x')`` and
  sksec's ``env_suffix`` all normalise the same way. A variable the play
  does not define (a ``set_fact`` or vault value) renders as a one-segment
  wildcard, matched as ``[^/]+`` on either side. That stays precise: a
  wildcard never spans a ``/``, so it cannot make a missing nested
  directory look created.

Sources that legitimately come from elsewhere are in ALLOWED below, each
with its reason.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

# skpeek's deploy.j2 holds shell regexes like "\." that Jinja's lexer warns
# about while rendering it; the render result is still correct.
pytestmark = pytest.mark.filterwarnings(
    "ignore:.*invalid escape sequence:DeprecationWarning"
)

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
ENVS = ("dev", "staging", "prod")
WILD = "\x00"  # rendered in place of an undefined variable

# (service, regex on the rendered source, reason). The regex sees WILD as
# a literal \x00 for sources with an unresolved variable.
ALLOWED = [
    (
        "skmail",
        r"/var/data/runtime/\x00-(dev|staging|prod)/acme/acme\.json",
        "Traefik's ACME store, written by skfence/skfenceha (the detected "
        "fence_service_name), not by skmail. skmail requires the fence.",
    ),
]


class _Undefined(jinja2.ChainableUndefined):
    def __str__(self):
        return WILD

    def __iter__(self):
        return iter(())


_JINJA = jinja2.Environment(undefined=_Undefined)
_JINJA.filters.setdefault("basename", lambda p: str(p).rstrip("/").rsplit("/", 1)[-1])
_JINJA.filters.setdefault("dirname", lambda p: str(p).rsplit("/", 1)[0])
_JINJA.filters.setdefault("bool", lambda v: str(v).lower() in ("1", "true", "yes", "on"))


def _render(value, ctx):
    if isinstance(value, dict):
        return {k: _render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [_render(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    try:
        return _JINJA.from_string(value).render(ctx)
    except jinja2.TemplateError:
        return re.sub(r"\{%.*?%\}", "", re.sub(r"\{\{.*?\}\}", WILD, value))


def _norm(path):
    path = re.sub(r"/+", "/", path.strip().strip("\"'"))
    return path.rstrip("/") or "/"


# --- compose side -----------------------------------------------------------

_PATH = r"(/var/data/(?:\{\{.*?\}\}|\{%.*?%\}|[^\s:\"'{#])+)"
_SHORT_RE = re.compile(r"^\s*-\s*[\"']?" + _PATH + r":", re.M)
_LONG_RE = re.compile(r"^\s*(?:source|device):\s*[\"']?" + _PATH, re.M)
_SET_RE = re.compile(r"\{%-?\s*set\s.*?-?%\}", re.S)


def _compose_templates(svc_dir):
    config = svc_dir / "src" / "config" / svc_dir.name
    for tpl in sorted(config.glob("*.yml.j2")):
        text = tpl.read_text()
        if re.search(r"^services:\s*$", text, re.M):
            yield tpl, text


def bind_sources(svc_dir, ctx):
    """Rendered /var/data bind sources of every compose template, as
    {source: template name}."""
    found = {}
    for tpl, text in _compose_templates(svc_dir):
        sets = "".join(_SET_RE.findall(text))
        body = "\n".join(
            "" if line.lstrip().startswith("#") else line for line in text.splitlines()
        )
        for raw in _SHORT_RE.findall(body) + _LONG_RE.findall(body):
            found.setdefault(_norm(_render(sets + raw, ctx)), tpl.name)
    return found


# --- deploy side ------------------------------------------------------------

_FILE = ("file", "ansible.builtin.file")
_DEST = ("template", "ansible.builtin.template", "copy", "ansible.builtin.copy")
_SHELL = ("shell", "command", "ansible.builtin.shell", "ansible.builtin.command")
_MKDIR_RE = re.compile(r"mkdir\s+-p\s+([^;&|\n]+)")


def _play_ctx(play):
    ctx = dict(play.get("vars") or {})
    for _ in range(3):  # vars that reference other vars
        ctx = {k: _render(v, ctx) for k, v in ctx.items()}
    return ctx


def _iter_tasks(tasks, base=None):
    """Tasks, depth first, including block/rescue/always and, when `base`
    (the playbook dir) is given, the tasks of a static include_tasks /
    import_tasks file (a path with no Jinja in it)."""
    for task in tasks or []:
        if not isinstance(task, dict):
            continue
        yield task
        for key in ("block", "rescue", "always"):
            yield from _iter_tasks(task.get(key), base)
        if base is None:
            continue
        for key in ("include_tasks", "import_tasks", "ansible.builtin.include_tasks",
                    "ansible.builtin.import_tasks"):
            inc = task.get(key)
            inc = inc.get("file") if isinstance(inc, dict) else inc
            if isinstance(inc, str) and "{{" not in inc and (base / inc).is_file():
                yield from _iter_tasks(yaml.safe_load((base / inc).read_text()), base)


def _args(task, modules):
    for mod in modules:
        if mod in task:
            args = task[mod]
            if isinstance(args, str):  # free-form k=v
                args = dict(
                    kv.split("=", 1) for kv in args.split() if "=" in kv
                ) or {"_raw": args}
            return args
    return None


def _mkdir_paths(text):
    for args in _MKDIR_RE.findall(text):
        for tok in args.split():
            tok = tok.strip("\"'")
            if tok.startswith("/var/data"):
                yield tok


def _fileglob(playbook_dir, task, ctx):
    """Expand with_fileglob the way Ansible does (relative patterns resolve
    against the playbook's files/ dir, then the playbook dir), keeping only
    items the task's ``when`` does not filter out, when that ``when`` can be
    evaluated."""
    items = []
    for pattern in task["with_fileglob"]:
        pattern = _render(pattern, ctx)
        for base in (playbook_dir / "files", playbook_dir):
            hits = sorted(str(p) for p in base.glob(pattern) if p.is_file())
            if hits:
                items.extend(hits)
                break
    when = task.get("when")
    if isinstance(when, str):
        kept = []
        for item in items:
            try:
                cond = _JINJA.from_string("{% if " + when + " %}1{% endif %}")
                if cond.render({**ctx, "item": item}) != "1":
                    continue
            except jinja2.TemplateError:
                pass
            kept.append(item)
        items = kept
    return items


def _playbook_created(path):
    created, ctx_out = set(), {}
    for play in yaml.safe_load(path.read_text()) or []:
        if not isinstance(play, dict):
            continue
        ctx = _play_ctx(play)
        if "app" in ctx:
            ctx_out = ctx
        for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
            for task in _iter_tasks(play.get(section), path.parent):
                tctx = {**ctx, **_render(task.get("vars") or {}, ctx)}
                items = task.get("loop", task.get("with_items"))
                if isinstance(items, str):
                    items = [WILD]  # a loop over an expression: unknowable
                items = items if items is not None else [None]
                if task.get("with_fileglob"):
                    items = _fileglob(path.parent, task, tctx)
                for item in items:
                    ictx = {**tctx, "item": _render(item, tctx)}
                    fargs = _args(task, _FILE)
                    if fargs and str(fargs.get("state", "")) == "directory":
                        created.add(_norm(_render(str(fargs.get("path", "")), ictx)))
                    dargs = _args(task, _DEST)
                    if dargs and dargs.get("dest"):
                        dest = _render(str(dargs["dest"]), ictx)
                        created.add(_norm(dest))
                        src = _render(str(dargs.get("src", "")), ictx)
                        if dest.endswith("/") and src and not src.endswith("/"):
                            # copy src=<file or dir> dest=<dir>/ lands at
                            # <dir>/<basename of src>
                            created.add(_norm(dest + "/" + src.rsplit("/", 1)[-1]))
                    sargs = _args(task, _SHELL)
                    if sargs is not None:
                        cmd = sargs if isinstance(sargs, str) else sargs.get("cmd", sargs.get("_raw", ""))
                        for p in _mkdir_paths(str(cmd)):
                            created.add(_norm(_render(p, ictx)))
    return created, ctx_out


_ASSIGN_RE = re.compile(r"^\s*(?:export\s+|local\s+)?([A-Za-z_]\w*)=([\"']?)([^\"'\n(]*)\2\s*(?:#.*)?$", re.M)
_ARRAY_RE = re.compile(r"^\s*([A-Za-z_]\w*)=\((.*?)^\s*\)", re.M | re.S)
_FOR_RE = re.compile(r"for\s+([A-Za-z_]\w*)\s+in\s+\"?\$\{([A-Za-z_]\w*)\[@\]\}\"?")


def _deploy_script_created(svc_dir, ctx):
    script = svc_dir / "src" / svc_dir.name / "deploy.j2"
    if not script.exists():
        return set()
    text = _render(script.read_text(), ctx)
    if "mkdir" not in text:
        return set()
    shvars = {}
    for name, _, value in _ASSIGN_RE.findall(text):
        shvars.setdefault(name, value)

    def expand(s):
        for _ in range(5):
            s = re.sub(
                r"\$\{?([A-Za-z_]\w*)\}?",
                lambda m: shvars.get(m.group(1), m.group(0)),
                s,
            )
        return s

    arrays = {
        name: [
            tok.strip("\"'")
            for line in body.splitlines()
            for tok in line.split("#", 1)[0].split()
        ]
        for name, body in _ARRAY_RE.findall(text)
    }
    loops = dict(_FOR_RE.findall(text))
    created = set()
    for args in _MKDIR_RE.findall(text):
        for tok in args.split():
            tok = tok.strip("\"'")
            var = re.fullmatch(r"\$\{?([A-Za-z_]\w*)\}?", tok)
            values = arrays.get(loops.get(var.group(1)), []) if var and var.group(1) in loops else [tok]
            for value in values:
                value = expand(value)
                if value.startswith("/var/data"):
                    created.add(_norm(value))
    return created


def _with_ancestors(paths):
    out = set()
    for p in paths:
        parts = p.split("/")
        for i in range(2, len(parts) + 1):
            out.add("/".join(parts[:i]))
    return out


def _pattern(path):
    return re.compile("".join("[^/]+" if c == WILD else re.escape(c) for c in path))


def _is_created(source, created):
    if source in created:
        return True
    if WILD in source:
        pat = _pattern(source)
        return any(pat.fullmatch(c) for c in created)
    return any(WILD in c and _pattern(c).fullmatch(source) for c in created)


def _is_allowed(svc, source):
    return any(s == svc and re.fullmatch(rx, source) for s, rx, _ in ALLOWED)


# --- tests ------------------------------------------------------------------

def _cases():
    for playbook in sorted(ANSIBLE.glob("*/*/deploy_*-prod.yml")):
        svc_dir = playbook.parent
        if not any(True for _ in _compose_templates(svc_dir)):
            continue
        for env in ENVS:
            p = svc_dir / f"deploy_{svc_dir.name}-{env}.yml"
            if p.exists():
                yield pytest.param(svc_dir, p, id=f"{svc_dir.name}-{env}")


def missing_sources(svc_dir, playbook):
    created, ctx = _playbook_created(playbook)
    created |= _deploy_script_created(svc_dir, ctx)
    created = _with_ancestors(created)
    return sorted(
        f"{src} (from {tpl})"
        for src, tpl in bind_sources(svc_dir, ctx).items()
        if not _is_created(src, created) and not _is_allowed(svc_dir.name, src)
    )


@pytest.mark.parametrize("svc_dir,playbook", list(_cases()))
def test_every_bind_mount_source_is_created_by_the_deploy(svc_dir, playbook):
    missing = missing_sources(svc_dir, playbook)
    assert not missing, (
        f"{playbook.name} never creates these bind-mount sources; Docker "
        "Swarm will reject the task (bind source path does not exist):\n  "
        + "\n  ".join(missing)
    )


def test_extractors_see_real_data():
    """Guard against a parser that silently finds nothing and passes."""
    skbook = ANSIBLE / "optional" / "skbook"
    created, ctx = _playbook_created(skbook / "deploy_skbook-dev.yml")
    assert ctx.get("app") == "skbook" and ctx.get("env") == "dev"
    assert "/var/data/skbook-dev/bookstack" in created
    assert "/var/data/config/skbook-dev/bookstack.env" in created  # template dest
    sources = bind_sources(skbook, ctx)
    assert "/var/data/skbook-dev/bookstack/config" in sources
    assert "/var/data/runtime/skbook-dev/db" in sources  # named-volume device
    assert "/var/data/config/skbook-dev/skbook.env" not in sources  # env_file
    sksync = ANSIBLE / "optional" / "sksync"
    _, sctx = _playbook_created(sksync / "deploy_sksync-dev.yml")
    assert "/var/data/sksync-dev/sync-data" in _deploy_script_created(sksync, sctx)
    skhub = ANSIBLE / "optional" / "skhub"
    hub_created, _ = _playbook_created(skhub / "deploy_skhub-dev.yml")
    # with_fileglob copy into a dir, with the `when` filter applied
    assert "/var/data/config/skhub-dev/php-opcache.ini" in hub_created
    assert "/var/data/config/skhub-dev/skhub.env.j2" not in hub_created
    n = sum(1 for _ in _cases())
    assert n >= 60, f"only {n} service/env cases collected"


def test_wildcard_never_spans_a_path_segment():
    assert _is_created(f"/var/data/{WILD}-dev/x", {"/var/data/skfence-dev/x"})
    assert not _is_created(f"/var/data/{WILD}-dev/x", {"/var/data/a/b-dev/x"})
    assert not _is_created("/var/data/skbook-dev/bookstack/config", _with_ancestors({"/var/data/skbook-dev/bookstack"}))
