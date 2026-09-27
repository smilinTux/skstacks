"""<svc>.EXTRA_DYNAMIC_FILES: instance-owned Traefik dynamic config files.

skfence/skfenceha render a fixed list of dynamic files (routers, services,
middlewares, tls). An instance that needs its own routes (for example an
extra hypervisor UI) had no way to add a file to dynamic/ without forking the
framework. EXTRA_DYNAMIC_FILES is an optional list of {src, dest[, acme]}
entries rendered AFTER the fixed dynamic config task, in every env playbook.

- src: absolute path, or relative to the instance's inventory directory
  (``inventory_dir``; the framework playbook's own playbook_dir is the
  framework checkout, not the instance).
- dest: a plain file name (no '/', ends .yml/.yaml) that does not shadow a
  framework-owned dynamic file.
- acme (skfenceha only): also render into dynamic_acme/.

Default empty: nothing is rendered and nothing is asserted.
"""
import pathlib
import shutil
import subprocess

import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
ANSIBLE = V1 / "ansible"
SHARED = ANSIBLE / "shared/tasks/render_extra_dynamic_files.yml"
PLAYBOOKS = [
    (svc, env, ANSIBLE / f"core/{svc}/deploy_{svc}-{env}.yml")
    for svc in ("skfence", "skfenceha")
    for env in ("dev", "staging", "prod")
]


def _tasks(path):
    plays = yaml.safe_load(path.read_text())
    return [t for p in plays if isinstance(p, dict) for t in p.get("tasks", [])]


def _fixed_dynamic_index(tasks):
    for i, t in enumerate(tasks):
        if "template" in t and "dynamic" in str(t.get("tags", [])) and "routers.yml.j2" in str(t):
            return i
    raise AssertionError("fixed dynamic config task not found")


def _extra_index(tasks):
    hits = [i for i, t in enumerate(tasks)
            if "render_extra_dynamic_files.yml" in str(t.get("include_tasks", t.get("import_tasks", "")))]
    assert len(hits) == 1, f"expected exactly one EXTRA_DYNAMIC_FILES task, got {hits}"
    return hits[0]


@pytest.mark.parametrize("svc,env,path", PLAYBOOKS)
def test_extra_dynamic_task_runs_after_the_fixed_dynamic_task(svc, env, path):
    tasks = _tasks(path)
    assert _extra_index(tasks) == _fixed_dynamic_index(tasks) + 1


@pytest.mark.parametrize("svc,env,path", PLAYBOOKS)
def test_extra_dynamic_task_is_guarded_and_wired_to_the_service_var(svc, env, path):
    t = _tasks(path)[_extra_index(_tasks(path))]
    assert "include_tasks" in t, "must be a dynamic include so `when` skips it entirely"
    when = str(t.get("when", ""))
    assert "EXTRA_DYNAMIC_FILES" in when and "length > 0" in when
    v = t.get("vars", {})
    assert f"{svc}" in v["extra_dynamic_files"] and "EXTRA_DYNAMIC_FILES" in v["extra_dynamic_files"]
    assert v["extra_dynamic_acme_supported"] is (svc == "skfenceha")
    assert {"routers.yml", "services.yml", "middlewares.yml", "tls.yml"} <= set(v["extra_dynamic_reserved"])
    assert "dynamic" in set(t.get("tags", []))


def test_extra_dynamic_task_identical_across_envs():
    for svc in ("skfence", "skfenceha"):
        bodies = set()
        for env in ("dev", "staging", "prod"):
            tasks = _tasks(ANSIBLE / f"core/{svc}/deploy_{svc}-{env}.yml")
            bodies.add(yaml.safe_dump(tasks[_extra_index(tasks)], sort_keys=True))
        assert len(bodies) == 1, svc


# --- real ansible runs of the shared task file ------------------------------

PROBE = """
- hosts: localhost
  gather_facts: false
  vars:
    base: "{{ probe_base }}"
    svc: {EXTRA_DYNAMIC_FILES: "{{ probe_files | default([]) }}"}
  tasks:
    - name: Render extra dynamic files
      include_tasks: "{{ probe_shared }}"
      when: (svc.EXTRA_DYNAMIC_FILES | default([])) | length > 0
      vars:
        extra_dynamic_files: "{{ svc.EXTRA_DYNAMIC_FILES | default([]) }}"
        extra_dynamic_config_dir: "{{ base }}"
        extra_dynamic_acme_supported: "{{ probe_acme_supported | default(true) }}"
        extra_dynamic_reserved: [routers.yml, services.yml, middlewares.yml, tls.yml]
"""


def _run(tmp_path, files=None, acme_supported=True):
    if not shutil.which("ansible-playbook"):
        pytest.skip("ansible-playbook not installed")
    inst = tmp_path / "inst" / "envs" / "dev"
    inst.mkdir(parents=True, exist_ok=True)
    (tmp_path / "base" / "dynamic").mkdir(parents=True, exist_ok=True)
    (tmp_path / "base" / "dynamic_acme").mkdir(parents=True, exist_ok=True)
    probe = tmp_path / "probe.yml"
    probe.write_text(PROBE)
    inv = inst / "inventory.ini"
    inv.write_text("localhost ansible_connection=local\n")
    import json
    extra = {"probe_base": str(tmp_path / "base"), "probe_shared": str(SHARED),
             "probe_acme_supported": acme_supported}
    if files is not None:
        extra["probe_files"] = files
    return subprocess.run(
        ["ansible-playbook", "-i", str(inv), str(probe), "-e", json.dumps(extra)],
        capture_output=True, text=True,
    )


def _listing(tmp_path):
    return sorted(str(p.relative_to(tmp_path / "base")) for p in (tmp_path / "base").rglob("*") if p.is_file())


def test_default_is_a_noop(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _listing(tmp_path) == []
    r = _run(tmp_path, files=[])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _listing(tmp_path) == []


def test_relative_src_resolves_against_the_instance_inventory_dir(tmp_path):
    inst = tmp_path / "inst" / "envs" / "dev"
    (inst / "traefik").mkdir(parents=True)
    (inst / "traefik" / "pve.yml.j2").write_text(
        "http:\n  routers:\n    pve:\n      rule: Host(`pve.{{ 'example.test' }}`)\n")
    r = _run(tmp_path, files=[{"src": "traefik/pve.yml.j2", "dest": "pve.yml"}])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _listing(tmp_path) == ["dynamic/pve.yml"]
    doc = yaml.safe_load((tmp_path / "base/dynamic/pve.yml").read_text())
    assert doc["http"]["routers"]["pve"]["rule"] == "Host(`pve.example.test`)"


def test_absolute_src_and_acme_flag(tmp_path):
    src = tmp_path / "abs.yaml.j2"
    src.write_text("http: {}\n")
    r = _run(tmp_path, files=[{"src": str(src), "dest": "extra.yaml", "acme": True},
                              {"src": str(src), "dest": "worker-only.yml"}])
    assert r.returncode == 0, r.stdout + r.stderr
    assert _listing(tmp_path) == ["dynamic/extra.yaml", "dynamic/worker-only.yml",
                                  "dynamic_acme/extra.yaml"]


@pytest.mark.parametrize("dest", ["../evil.yml", "sub/x.yml", "/etc/x.yml", "x.j2",
                                  "x.yml.bak", "", ".yml", "routers.yml", "tls.yml"])
def test_bad_dest_fails_before_rendering(tmp_path, dest):
    src = tmp_path / "abs.yml.j2"
    src.write_text("http: {}\n")
    r = _run(tmp_path, files=[{"src": str(src), "dest": "good.yml"},
                              {"src": str(src), "dest": dest}])
    assert r.returncode != 0
    assert "EXTRA_DYNAMIC_FILES" in r.stdout, r.stdout
    assert _listing(tmp_path) == [], "validation must run before anything is rendered"


def test_missing_src_fails(tmp_path):
    r = _run(tmp_path, files=[{"dest": "x.yml"}])
    assert r.returncode != 0
    assert "EXTRA_DYNAMIC_FILES" in r.stdout, r.stdout
    assert _listing(tmp_path) == []


def test_acme_flag_rejected_where_unsupported(tmp_path):
    src = tmp_path / "abs.yml.j2"
    src.write_text("http: {}\n")
    r = _run(tmp_path, files=[{"src": str(src), "dest": "x.yml", "acme": True}], acme_supported=False)
    assert r.returncode != 0
    assert "EXTRA_DYNAMIC_FILES" in r.stdout, r.stdout
    assert _listing(tmp_path) == []


def test_readme_documents_the_option():
    for svc in ("skfence", "skfenceha"):
        readme = ANSIBLE / f"core/{svc}/README.md"
        assert readme.exists(), readme
        assert f"{svc}.EXTRA_DYNAMIC_FILES" in readme.read_text(), svc
