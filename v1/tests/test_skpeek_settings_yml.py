"""skpeek.SETTINGS_YML: an instance-owned SearXNG settings.yml, written verbatim.

The framework renders settings.yml from a full upstream-style template where
only debug, instance_name, theme and secret_key are hookable. An instance that
runs a hand-written minimal settings.yml (no use_default_settings, its own
engine list, timeouts, resolvers, redis url) could not express it without
forking the framework.

skpeek.SETTINGS_YML (string, raw YAML) now replaces the rendered file:
- unset or empty: settings.yml is rendered from the framework template
  exactly as before (byte-identical content).
- set: it must parse as a YAML mapping, and is then written as-is to the same
  path. A value that does not parse, is not a mapping, or is not a string
  stops the deploy with a message naming skpeek.SETTINGS_YML, before anything
  is written, without echoing the value (it can carry server.secret_key).

Either way settings.yml is root:root 0640, not world-readable: the rendered
file carries server.secret_key too. The SearXNG entrypoint runs as root (and
chowns /etc/searxng to searxng:searxng), so the container can still read it.
"""
import json
import pathlib
import shutil
import stat
import subprocess

import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
SKPEEK = V1 / "ansible/optional/skpeek"
TASKS = SKPEEK / "tasks/settings_yml.yml"
TEMPLATE = SKPEEK / "src/skpeek/etc/settings.yml.j2"
PLAYBOOKS = sorted(SKPEEK.glob("deploy_skpeek-*.yml"))
DEST = "/var/data/{{ app }}-{{ env }}/etc/settings.yml"

FAKE_SECRET = "FAKE-skpeek-secret-SENTINEL-4b1e"

CUSTOM = f"""# hand-written minimal settings (no use_default_settings)
general:
  debug: false
  instance_name: "Custom Peek"
server:
  secret_key: "{FAKE_SECRET}"
  limiter: false
redis:
  url: valkey://redis:6379/0
outgoing:
  request_timeout: 4.0
  max_request_timeout: 8.0
engines:
  - name: duckduckgo
    engine: duckduckgo
    shortcut: ddg
  - name: grep
    engine: command
    command: ['grep', '{{{{QUERY}}}}']
    shortcut: gr
"""


def _tasks(path):
    plays = yaml.safe_load(path.read_text())
    return [t for p in plays if isinstance(p, dict) for t in p.get("tasks", [])]


def _import_index(tasks):
    hits = [i for i, t in enumerate(tasks)
            if "tasks/settings_yml.yml" in str(t.get("import_tasks", t.get("include_tasks", "")))]
    assert len(hits) == 1, f"expected exactly one settings_yml.yml import, got {hits}"
    return hits[0]


def _config_loop_index(tasks):
    hits = [i for i, t in enumerate(tasks) if "uwsgi.ini.j2" in (t.get("loop") or [])]
    assert len(hits) == 1, hits
    return hits[0]


def _write_tasks(tasks=None):
    tasks = tasks if tasks is not None else yaml.safe_load(TASKS.read_text())
    out = []
    for t in tasks:
        for k in ("block", "rescue", "always"):
            out.extend(_write_tasks(t.get(k) or []))
        for mod in ("template", "ansible.builtin.template", "copy", "ansible.builtin.copy"):
            if mod in t:
                out.append((t, t[mod]))
    return out


# --- static wiring ----------------------------------------------------------

@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_settings_yml_no_longer_rendered_by_the_config_loop(path):
    tasks = _tasks(path)
    loop = tasks[_config_loop_index(tasks)]["loop"]
    assert "settings.yml.j2" not in loop
    assert loop == ["uwsgi.ini.j2", "limiter.toml.j2"]


@pytest.mark.parametrize("path", PLAYBOOKS, ids=lambda p: p.name)
def test_settings_yml_import_follows_the_config_loop_with_the_same_dest(path):
    tasks = _tasks(path)
    i = _import_index(tasks)
    assert i == _config_loop_index(tasks) + 1
    t = tasks[i]
    assert "import_tasks" in t
    assert t["vars"]["skpeek_settings_dest"] == DEST
    assert {"templates", "config"} <= set(t.get("tags", []))


def test_settings_yml_import_identical_across_envs():
    bodies = set()
    for path in PLAYBOOKS:
        tasks = _tasks(path)
        bodies.add(yaml.safe_dump(tasks[_import_index(tasks)], sort_keys=True))
    assert len(PLAYBOOKS) == 3 and len(bodies) == 1


def test_both_write_paths_are_root_0640_and_never_log_the_value():
    writes = _write_tasks()
    mods = sorted(m for t, _ in writes for m in t if m in (
        "template", "ansible.builtin.template", "copy", "ansible.builtin.copy"))
    assert len(writes) == 2, mods
    for t, args in writes:
        assert args["dest"] == "{{ skpeek_settings_dest }}"
        assert (args["owner"], args["group"], str(args["mode"])) == ("root", "root", "0640")
        if "copy" in t or "ansible.builtin.copy" in t:
            assert t.get("no_log") is True
            assert "SETTINGS_YML" in args["content"]
        else:
            assert args["src"].endswith("/src/{{ app }}/etc/settings.yml.j2")


# --- real ansible runs of the task file -------------------------------------

PROBE = """
- hosts: localhost
  gather_facts: false
  vars_files: [vars.yml]
  vars:
    app: skpeek
    env: dev
  tasks:
    - import_tasks: tasks/settings_yml.yml
      vars:
        skpeek_settings_dest: "{{ probe_dest }}"
"""

PLAIN = """
- hosts: localhost
  gather_facts: false
  vars_files: [vars.yml]
  vars:
    app: skpeek
    env: dev
  tasks:
    - template:
        src: "{{ playbook_dir }}/src/skpeek/etc/settings.yml.j2"
        dest: "{{ probe_dest }}"
"""


def _strip_ownership(node):
    """Drop owner/group (the probe runs unprivileged); modes are kept."""
    if isinstance(node, list):
        return [_strip_ownership(n) for n in node]
    if isinstance(node, dict):
        return {k: _strip_ownership(v) for k, v in node.items() if k not in ("owner", "group")}
    return node


def _vars_yaml(settings_yml):
    doc = ("cluster_name: cluster1\ndomain: example.test\n"
           "skpeek:\n"
           "  CLUSTERNAME: cluster1\n"
           "  DOMAIN: example.test\n"
           f"  SECRET_KEY: \"{FAKE_SECRET}\"\n")
    if settings_yml is not None:
        doc += "  SETTINGS_YML: " + settings_yml
    return doc


def _run(tmp_path, settings_yml=None, playbook=PROBE):
    if not shutil.which("ansible-playbook"):
        pytest.skip("ansible-playbook not installed")
    work = tmp_path / "work"
    (work / "tasks").mkdir(parents=True)
    (work / "src").symlink_to(SKPEEK / "src")
    if TASKS.exists():
        (work / "tasks/settings_yml.yml").write_text(
            yaml.safe_dump(_strip_ownership(yaml.safe_load(TASKS.read_text())), sort_keys=False))
    (work / "vars.yml").write_text(_vars_yaml(settings_yml))
    (work / "probe.yml").write_text(playbook)
    dest = tmp_path / "etc" / "settings.yml"
    dest.parent.mkdir()
    r = subprocess.run(
        ["ansible-playbook", "-i", "localhost,", "-c", "local", str(work / "probe.yml"),
         "-e", json.dumps({"probe_dest": str(dest)})],
        capture_output=True, text=True, cwd=work,
    )
    return r, dest


def _block(text):
    """A YAML literal block scalar holding text exactly (keep trailing newlines)."""
    return "|+\n" + "".join("    " + line + "\n" if line else "\n" for line in text.split("\n")[:-1]) \
        if text.endswith("\n") else "|-\n" + "".join("    " + line + "\n" for line in text.split("\n"))


def test_default_renders_the_framework_template_byte_identical(tmp_path):
    r, dest = _run(tmp_path / "new")
    assert r.returncode == 0, r.stdout + r.stderr
    p, plain = _run(tmp_path / "plain", playbook=PLAIN)
    assert p.returncode == 0, p.stdout + p.stderr
    assert dest.read_bytes() == plain.read_bytes()
    doc = yaml.safe_load(dest.read_text())
    assert doc["server"]["secret_key"] == FAKE_SECRET
    assert doc["general"]["instance_name"] == "SKPeek"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o640


def test_empty_value_is_the_default(tmp_path):
    r, dest = _run(tmp_path / "new", settings_yml='""\n')
    assert r.returncode == 0, r.stdout + r.stderr
    p, plain = _run(tmp_path / "plain", playbook=PLAIN)
    assert dest.read_bytes() == plain.read_bytes()


def test_set_writes_the_value_verbatim(tmp_path):
    # !unsafe keeps Ansible from templating '{{QUERY}}' in the value.
    r, dest = _run(tmp_path, settings_yml="!unsafe " + _block(CUSTOM))
    assert r.returncode == 0, r.stdout + r.stderr
    assert dest.read_text() == CUSTOM
    assert "{{QUERY}}" in dest.read_text()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o640
    assert FAKE_SECRET not in r.stdout + r.stderr


def test_set_without_trailing_newline_is_kept_as_is(tmp_path):
    body = "general:\n  instance_name: x\nserver:\n  secret_key: k"
    r, dest = _run(tmp_path, settings_yml=_block(body))
    assert r.returncode == 0, r.stdout + r.stderr
    assert dest.read_text() == body


@pytest.mark.parametrize("value,why", [
    (_block(f"server:\n  secret_key: \"{FAKE_SECRET}\n  limiter: [false\n"), "does not parse as YAML"),
    (_block("general: {debug: false\n"), "does not parse as YAML"),
    (_block("just a scalar string\n"), "must be a YAML mapping"),
    (_block("- a\n- list\n"), "must be a YAML mapping"),
    ("{general: {debug: false}}\n", "must be a string"),
], ids=["unterminated-quote", "unclosed-flow-map", "scalar", "list", "dict-not-string"])
def test_bad_value_fails_clearly_before_writing(tmp_path, value, why):
    r, dest = _run(tmp_path, settings_yml=value)
    out = r.stdout + r.stderr
    assert r.returncode != 0
    assert "skpeek.SETTINGS_YML" in out and why in out, out
    assert FAKE_SECRET not in out, "the value (it can hold server.secret_key) must not be echoed"
    assert not dest.exists(), "validation must run before anything is written"


def test_readme_documents_the_key():
    readme = SKPEEK / "README.md"
    assert readme.exists()
    text = readme.read_text()
    assert "skpeek.SETTINGS_YML" in text and "!unsafe" in text and "0640" in text
