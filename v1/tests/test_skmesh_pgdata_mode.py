"""skmesh: the postgres runtime directory is created 0700.

With a pinned postgres older than 18 that directory is PGDATA itself
(mounted at /var/lib/postgresql/data), and PostgreSQL refuses to start on a
data directory that is group or world readable. The deploys created it 0755,
which the image entrypoint only repaired on the next container start (the
same defect fixed for skgit in test_skgit_pgdata_mode.py). With postgres 18+
the directory is the mount's parent, where 0700 is also fine.
"""
import pathlib

import pytest
import yaml

SKMESH = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skmesh"
PLAYBOOKS = sorted(SKMESH.glob("deploy_skmesh-*.yml"))


def _dir_items(playbook):
    plays = yaml.safe_load(playbook.read_text())
    for play in plays:
        for task in play.get("tasks", []) + play.get("pre_tasks", []):
            for item in task.get("loop", []) or task.get("with_items", []) or []:
                if isinstance(item, dict) and "path" in item and "mode" in item:
                    yield item


@pytest.mark.parametrize("playbook", PLAYBOOKS, ids=lambda p: p.name)
def test_postgres_runtime_dir_is_0700(playbook):
    items = [i for i in _dir_items(playbook) if i["path"].endswith("/postgres")
             and i["path"].startswith("/var/data/runtime/")]
    assert items, f"{playbook.name}: no postgres runtime directory item found"
    for item in items:
        assert item["mode"] == "0700", item
        assert (item["owner"], item["group"]) == ("999", "999"), item


def test_skmesh_playbooks_are_covered():
    assert {p.name for p in PLAYBOOKS} >= {"deploy_skmesh-prod.yml", "deploy_skmesh-dev.yml"}
