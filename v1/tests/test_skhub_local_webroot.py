"""skhub Nextcloud code tree (webroot + custom_apps) on local disk on one pinned node.

/var/www/html (about 30k files) and custom_apps (about 60k) were bind mounts from
/var/data, which is NFS on a typical instance. Every PHP request and every occ
command stats thousands of small files there: `occ integrity:check-core` took
340 s on NFS and a Nextcloud image bump made the entrypoint rsync the whole code
tree over NFS (35 to 75 minutes per hop, the service unhealthy the whole time).

The knobs:

  skhub.WEBROOT_LOCAL_PATH   local dir holding html/ and custom_apps/ (default
                             empty: both stay on /var/data, unchanged render)
  skhub.APP_NODE             pins nextcloud, cron and notify_push to this node
                             (node.hostname); required with WEBROOT_LOCAL_PATH
  skhub.WEBROOT_NFS_SYNC     nightly local -> /var/data copy on APP_NODE as the
                             failover source (default true with a local path)
  skhub.WEBROOT_NFS_SYNC_ON_CALENDAR / WEBROOT_NFS_SYNC_BWLIMIT

config, data and themes stay on shared storage.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
README = SKHUB / "README.md"
SYNC_SH = SKHUB / "templates/skhub-webroot-sync.sh.j2"
SYNC_SERVICE = SKHUB / "templates/skhub-webroot-sync.service.j2"
SYNC_TIMER = SKHUB / "templates/skhub-webroot-sync.timer.j2"
ENVS = ("dev", "staging", "prod")
PLAYBOOKS = {e: SKHUB / f"deploy_skhub-{e}.yml" for e in ENVS}
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "enable_talk_hpb": True, "enable_collabora": True}
LOCAL = {"WEBROOT_LOCAL_PATH": "/var/lib/skhub-prod", "APP_NODE": "node-app"}
APP_SERVICES = ("nextcloud", "cron", "notify_push")


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    env.tests["match"] = lambda v, pattern: re.match(pattern, str(v)) is not None  # Ansible's match
    return env


def render(env_name="prod", **over):
    return _env().from_string(COMPOSE.read_text()).render(
        app="skhub", env=env_name, skhub=dict(BASE, **over), fence_service_name="skfenceha")


def services(env_name="prod", **over):
    return yaml.safe_load(render(env_name, **over))["services"]


def mounts(svc):
    return {v.split(":")[1]: v.split(":")[0] for v in svc.get("volumes", []) if isinstance(v, str)}


def constraints(svc):
    return svc["deploy"]["placement"]["constraints"]


# ---- compose ------------------------------------------------------------------

@pytest.mark.parametrize("env_name", ENVS)
def test_default_keeps_the_code_tree_on_shared_storage(env_name):
    s = services(env_name)
    for name in ("nextcloud", "cron"):
        m = mounts(s[name])
        assert m["/var/www/html"] == f"/var/data/skhub-{env_name}/html", name
        assert m["/var/www/html/custom_apps"] == f"/var/data/skhub-{env_name}/custom_apps", name
    assert mounts(s["notify_push"])["/var/www/html/custom_apps"] == f"/var/data/skhub-{env_name}/custom_apps"
    for name, svc in s.items():
        assert not any(c.startswith("node.hostname") for c in constraints(svc)), name


@pytest.mark.parametrize("env_name", ENVS)
def test_empty_knobs_render_byte_identical_to_unset(env_name):
    assert render(env_name) == render(env_name, WEBROOT_LOCAL_PATH="", APP_NODE="")


def test_local_path_moves_html_and_custom_apps_only():
    s = services(**LOCAL)
    for name in ("nextcloud", "cron"):
        m = mounts(s[name])
        assert m["/var/www/html"] == "/var/lib/skhub-prod/html"
        assert m["/var/www/html/custom_apps"] == "/var/lib/skhub-prod/custom_apps"
        assert m["/var/www/html/config"] == "/var/data/skhub-prod/config"
        assert m["/var/www/html/data"] == "/var/data/skhub-prod/data"
        assert m["/var/www/html/themes"] == "/var/data/skhub-prod/themes"
    np = s["notify_push"]["volumes"]
    assert "/var/lib/skhub-prod/custom_apps:/var/www/html/custom_apps:ro" in np
    assert "/var/data/skhub-prod/config:/var/www/html/config:ro" in np


def test_local_path_keeps_the_mount_order():
    """The code tree is mounted before the nested config/data/themes binds."""
    for name in ("nextcloud", "cron"):
        targets = [v.split(":")[1] for v in services(**LOCAL)[name]["volumes"]]
        assert targets.index("/var/www/html") < targets.index("/var/www/html/custom_apps") < targets.index(
            "/var/www/html/config")


def test_app_node_pins_nextcloud_cron_and_notify_push_only():
    s = services(placement_constraints=["node.role == worker"], **LOCAL)
    for name in APP_SERVICES:
        assert constraints(s[name]) == ["node.role == worker", "node.hostname == node-app"], name
    for name in set(s) - set(APP_SERVICES):
        assert "node.hostname == node-app" not in constraints(s[name]), name


def test_app_node_without_other_constraints():
    s = services(**LOCAL)
    for name in APP_SERVICES:
        assert constraints(s[name]) == ["node.hostname == node-app"], name


def test_only_the_three_app_services_change():
    a, b = services(), services(**LOCAL)
    assert {n for n in a if a[n] != b[n]} == set(APP_SERVICES)


# ---- playbooks ----------------------------------------------------------------

def tasks(env_name):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env_name].read_text()):
        if isinstance(play, dict):
            for sec in ("pre_tasks", "tasks"):
                out += play.get(sec) or []
    return out


def _guard(env_name):
    hits = [t for t in tasks(env_name) if "assert" in t and "WEBROOT_LOCAL_PATH" in json.dumps(t.get("vars", {}))
            and "APP_NODE" in json.dumps(t.get("vars", {}))]
    assert len(hits) == 1, env_name
    return hits[0]


def _passes(env_name, **over):
    e = _env()
    t = _guard(env_name)
    ctx = {"app": "skhub", "env": env_name, "skhub": dict(BASE, **over)}
    for k, v in (t.get("vars") or {}).items():
        ctx[k] = e.from_string(v).render(**ctx)
    return all(e.compile_expression(c)(**ctx) for c in t["assert"]["that"])


@pytest.mark.parametrize("env_name", ENVS)
@pytest.mark.parametrize("over,ok", [
    ({}, True),
    (LOCAL, True),
    ({"APP_NODE": "node-app"}, True),
    ({"WEBROOT_LOCAL_PATH": "/var/lib/skhub-prod"}, False),
    ({"WEBROOT_LOCAL_PATH": "/var/lib/skhub-prod", "APP_NODE": ""}, False),
    ({"WEBROOT_LOCAL_PATH": "/var/data/skhub-prod", "APP_NODE": "n"}, False),
    ({"WEBROOT_LOCAL_PATH": "/var/data/elsewhere", "APP_NODE": "n"}, False),
    ({"WEBROOT_LOCAL_PATH": "var/lib/skhub", "APP_NODE": "n"}, False),
    ({"WEBROOT_LOCAL_PATH": "/var/lib/skhub/", "APP_NODE": "n"}, False),
    ({"WEBROOT_LOCAL_PATH": "/var/lib/sk hub", "APP_NODE": "n"}, False),
    ({"WEBROOT_LOCAL_PATH": "/", "APP_NODE": "n"}, False),
])
def test_guard_fails_closed(env_name, over, ok):
    assert _passes(env_name, **over) is ok


def _local_dir_task(env_name):
    hits = [t for t in tasks(env_name) if "file" in t and "APP_NODE" in str(t.get("delegate_to", ""))]
    assert len(hits) == 1, env_name
    return hits[0]


@pytest.mark.parametrize("env_name", ENVS)
def test_local_dirs_are_created_on_the_app_node_not_recursive(env_name):
    t = _local_dir_task(env_name)
    assert t["delegate_to"] == "{{ skhub.APP_NODE | default(inventory_hostname, true) }}"
    assert "WEBROOT_LOCAL_PATH" in str(t["when"])
    assert not t["file"].get("recurse")
    assert t["file"]["state"] == "directory"


@pytest.mark.parametrize("env_name", ENVS)
def test_every_app_node_delegate_renders_with_strict_undefined_when_unset(env_name):
    """Ansible templates delegate_to before when (the sksso #125 bug)."""
    e = jinja2.Environment(undefined=jinja2.StrictUndefined)
    hits = [t for t in tasks(env_name) if "APP_NODE" in str(t.get("delegate_to", ""))]
    assert len(hits) >= 4, env_name
    for t in hits:
        tpl = e.from_string(t["delegate_to"])
        assert tpl.render(skhub={}, inventory_hostname="mgr-1") == "mgr-1"
        assert tpl.render(skhub={"APP_NODE": "node-app"}, inventory_hostname="mgr-1") == "node-app"


@pytest.mark.parametrize("env_name", ENVS)
def test_nfs_code_dirs_are_still_created(env_name):
    """The /var/data copy stays: it is the failover source and the rollback path."""
    text = PLAYBOOKS[env_name].read_text()
    assert '"/var/data/{{ app }}-{{ env }}/html"' in text
    assert '"/var/data/{{ app }}-{{ env }}/custom_apps"' in text


def _seed_guard(env_name):
    hits = [t for t in tasks(env_name) if "assert" in t and "version" in json.dumps(t)
            and "WEBROOT_LOCAL_PATH" in json.dumps(t)]
    assert len(hits) == 1, env_name
    return hits[0]


@pytest.mark.parametrize("env_name", ENVS)
@pytest.mark.parametrize("local_has,nfs_has,ok", [
    (True, True, True),     # seeded: fine
    (False, False, True),   # fresh install: the entrypoint fills the local dir
    (True, False, True),
    (False, True, False),   # existing instance, local dir never seeded: refuse
])
def test_seed_guard(env_name, local_has, nfs_has, ok):
    t = _seed_guard(env_name)
    ctx = {"skhub": dict(BASE, **LOCAL), "app": "skhub", "env": env_name,
           "skhub_webroot_local_version": {"stat": {"exists": local_has}},
           "skhub_webroot_nfs_version": {"stat": {"exists": nfs_has}}}
    e = _env()
    assert all(e.compile_expression(c)(**ctx) for c in t["assert"]["that"]) is ok


@pytest.mark.parametrize("env_name", ENVS)
def test_sync_units_are_installed_on_the_app_node_only_when_enabled(env_name):
    sync = [t for t in tasks(env_name) if "webroot-sync" in json.dumps(t)
            and any(k in t for k in ("template", "systemd", "file"))]
    assert sync, env_name
    for t in sync:
        assert "APP_NODE" in t["delegate_to"], t["name"]
        assert "WEBROOT_NFS_SYNC" in str(t["when"]), t["name"]
        assert "WEBROOT_LOCAL_PATH" in str(t["when"]), t["name"]


def test_timer_and_service_render():
    ctx = {"app": "skhub", "env": "prod", "skhub": dict(BASE, **LOCAL)}
    timer = _env().from_string(SYNC_TIMER.read_text()).render(**ctx)
    assert "OnCalendar=*-*-* 03:17:00 UTC" in timer and "Persistent=true" in timer
    timer2 = _env().from_string(SYNC_TIMER.read_text()).render(
        **dict(ctx, skhub=dict(BASE, WEBROOT_NFS_SYNC_ON_CALENDAR="*-*-* 04:00:00 UTC", **LOCAL)))
    assert "OnCalendar=*-*-* 04:00:00 UTC" in timer2
    svc = _env().from_string(SYNC_SERVICE.read_text()).render(**ctx)
    assert "ExecStart=/usr/local/sbin/skhub-prod-webroot-sync.sh" in svc and "Type=oneshot" in svc


# ---- the sync script itself ---------------------------------------------------

def _script(tmp_path, **over):
    ctx = {"app": "skhub", "env": "prod", "skhub": dict(BASE, **LOCAL, **over)}
    p = tmp_path / "sync.sh"
    p.write_text(_env().from_string(SYNC_SH.read_text()).render(**ctx))
    p.chmod(0o755)
    return p


def _run(script, src, dst):
    env = dict(os.environ, SKHUB_WEBROOT_SRC=str(src), SKHUB_WEBROOT_DST=str(dst))
    return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)


def _tree(root):
    (root / "html/lib").mkdir(parents=True)
    (root / "html/version.php").write_text("<?php $OC_Version = [34,0,4,1];\n")
    (root / "html/lib/base.php").write_text("base\n")
    for d in ("data", "config", "custom_apps", "themes"):
        (root / "html" / d).mkdir()
    (root / "custom_apps/notify_push").mkdir(parents=True)
    (root / "custom_apps/notify_push/appinfo.xml").write_text("np\n")


needs_rsync = pytest.mark.skipif(shutil.which("rsync") is None, reason="needs rsync")


def test_script_parses(tmp_path):
    assert subprocess.run(["bash", "-n", str(_script(tmp_path))]).returncode == 0
    text = _script(tmp_path).read_text()
    assert "/var/lib/skhub-prod" in text and "/var/data/skhub-prod" in text
    assert 'BWLIMIT="20000"' in text and '"--bwlimit=$BWLIMIT"' in text


@needs_rsync
def test_script_mirrors_and_keeps_the_nfs_mount_point_contents(tmp_path):
    src, dst = tmp_path / "local", tmp_path / "nfs"
    _tree(src)
    _tree(dst)
    (dst / "html/data/.ocdata").write_text("")          # under a mount point: must survive
    (dst / "html/stale.php").write_text("old\n")         # gone locally: must be removed
    (src / "custom_apps/newapp").mkdir()
    (src / "custom_apps/newapp/info.xml").write_text("n\n")
    r = _run(_script(tmp_path), src, dst)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (dst / "html/data/.ocdata").exists()
    assert not (dst / "html/stale.php").exists()
    assert (dst / "custom_apps/newapp/info.xml").read_text() == "n\n"
    assert (dst / "html/lib/base.php").read_text() == "base\n"


@needs_rsync
def test_script_refuses_an_unseeded_local_tree(tmp_path):
    src, dst = tmp_path / "local", tmp_path / "nfs"
    _tree(dst)
    (src / "html").mkdir(parents=True)
    (src / "custom_apps").mkdir()
    r = _run(_script(tmp_path), src, dst)
    assert r.returncode != 0
    assert (dst / "html/version.php").exists() and (dst / "custom_apps/notify_push/appinfo.xml").exists()


@needs_rsync
def test_script_refuses_an_empty_custom_apps(tmp_path):
    src, dst = tmp_path / "local", tmp_path / "nfs"
    _tree(src)
    _tree(dst)
    shutil.rmtree(src / "custom_apps")
    (src / "custom_apps").mkdir()
    r = _run(_script(tmp_path), src, dst)
    assert r.returncode != 0
    assert (dst / "custom_apps/notify_push/appinfo.xml").exists()


@needs_rsync
def test_script_refuses_when_shared_storage_is_missing(tmp_path):
    """/var/data not mounted: never write into the bare mount point."""
    src, dst = tmp_path / "local", tmp_path / "nfs"
    _tree(src)
    r = _run(_script(tmp_path), src, dst)
    assert r.returncode != 0
    assert not dst.exists()


# ---- docs ---------------------------------------------------------------------

def test_readme_documents_the_knobs_and_the_migration():
    text = README.read_text()
    for key in ("WEBROOT_LOCAL_PATH", "APP_NODE", "WEBROOT_NFS_SYNC", "WEBROOT_NFS_SYNC_ON_CALENDAR",
                "WEBROOT_NFS_SYNC_BWLIMIT"):
        assert key in text, key
    assert "Local code tree on a pinned node" in text
    assert "--mount-rm" in text


# ---- real Ansible: the delegated dir task with the knobs unset and set ----------

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _ansible_dirs(tmp_path, env_name, skhub):
    task = dict(_local_dir_task(env_name))
    task["file"] = {k: v for k, v in task["file"].items() if k not in ("owner", "group")}
    task["register"] = "local_dirs"
    out = tmp_path / "result.yml"
    play = [{"hosts": "localhost", "gather_facts": False, "connection": "local",
             "vars": {"app": "skhub", "env": env_name, "skhub": skhub},
             "tasks": [task, {"name": "record", "copy": {"dest": str(out), "content": "{{ local_dirs | to_yaml }}"}}]}]
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump(play, sort_keys=False))
    env_vars = dict(os.environ, HOME=str(tmp_path), ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="False",
                    ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
    r = subprocess.run(["ansible-playbook", "-i", "localhost,", str(pb)], capture_output=True, text=True, env=env_vars)
    return r, (yaml.safe_load(out.read_text()) if out.exists() else None)


@needs_ansible
@pytest.mark.parametrize("env_name", ENVS)
def test_ansible_unset_knobs_skip_the_local_dir_task(tmp_path, env_name):
    r, res = _ansible_dirs(tmp_path, env_name, dict(BASE))
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert res and all(i.get("skipped") for i in res["results"]), res


@needs_ansible
@pytest.mark.parametrize("env_name", ENVS)
def test_ansible_set_knobs_create_the_local_dirs(tmp_path, env_name):
    root = tmp_path / "local/skhub"
    r, _ = _ansible_dirs(tmp_path, env_name, dict(BASE, WEBROOT_LOCAL_PATH=str(root), APP_NODE="localhost"))
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert (root / "html").is_dir() and (root / "custom_apps").is_dir()
