"""skgallery postgres + redis on local disk on one pinned node.

Their data paths were hardcoded under /var/data/skgallery-<env>/ (NFS on a
typical instance). On an instance (2026-11-09) NFS write stalls of 30 to 120 s
froze Immich's database and its bullmq queue; the worker lost its job locks
and the API stopped answering. The knobs:

  skgallery.DATA_NODE        pin database + redis to this node (hostname)
  skgallery.DB_DATA_PATH     default /var/data/skgallery-<env>/db-data
  skgallery.REDIS_DATA_PATH  default /var/data/skgallery-<env>/redis-data

A path outside /var/data is local disk: the playbooks create it on DATA_NODE
(owner 999) and refuse to run without DATA_NODE.
"""
import pathlib

import pytest
import yaml

from test_router_middlewares_knob import render

APP = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgallery"
LOCAL = {"DATA_NODE": "node-db", "DB_DATA_PATH": "/var/lib/skgallery-prod/db",
         "REDIS_DATA_PATH": "/var/lib/skgallery-prod/redis"}


def _svc(name, **over):
    return yaml.safe_load(render("skgallery", **over))["services"][name]


def test_unset_keeps_the_nfs_paths_and_no_pin():
    db, rd = _svc("database"), _svc("redis")
    assert "/var/data/skgallery-prod/db-data:/var/lib/postgresql/data" in db["volumes"]
    assert "/var/data/skgallery-prod/redis-data:/data" in rd["volumes"]
    for s in (db, rd):
        assert s["deploy"]["placement"]["constraints"] == ["node.role == worker"]


def test_local_paths_and_pin_applied():
    db, rd = _svc("database", **LOCAL), _svc("redis", **LOCAL)
    assert "/var/lib/skgallery-prod/db:/var/lib/postgresql/data" in db["volumes"]
    assert "/var/lib/skgallery-prod/redis:/data" in rd["volumes"]
    for s in (db, rd):
        assert s["deploy"]["placement"]["constraints"] == ["node.role == worker", "node.hostname == node-db"]


def test_pin_does_not_touch_the_app_services():
    srv = _svc("immich-server", **LOCAL)
    assert "node.hostname == node-db" not in srv["deploy"]["placement"]["constraints"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_playbook_guards_and_creates_local_dirs(env):
    plays = yaml.safe_load((APP / f"deploy_skgallery-{env}.yml").read_text())
    tasks = [t for p in plays for t in (p.get("tasks") or [])]
    names = [t.get("name", "") for t in tasks]
    guard = next(t for t in tasks if t.get("name", "").startswith("Fail closed on local database paths"))
    assert "DATA_NODE" in guard["assert"]["that"][0]
    local = next(t for t in tasks if "local database directories on the data node" in t.get("name", ""))
    assert "DATA_NODE" in local["delegate_to"]
    assert local["file"]["owner"] == "999"
    # The guard runs before any directory is created.
    assert names.index(guard["name"]) < names.index(local["name"])
