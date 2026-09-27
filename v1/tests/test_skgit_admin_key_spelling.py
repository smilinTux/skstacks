"""skgit's playbook header documents the admin vault keys as ADMIN_EMAIL /
ADMIN_PASSWORD (uppercase, like every other skgit secret), but the admin tasks
read skgit.admin_password / skgit.admin_email. An instance following the docs
never got its admin user (silently skipped), and after #75 made that loud the
skstack06 v2.20.0 run failed with admin_enabled true and only the uppercase
keys set. Both spellings must work (lowercase wins), resolved once, no_log."""
import pathlib

import pytest
import yaml

SKGIT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgit"
PLAYBOOKS = sorted(SKGIT.glob("deploy_skgit-*.yml"))


def _tasks(pb):
    return [t for play in yaml.safe_load(pb.read_text()) for key in ("pre_tasks", "tasks", "post_tasks") for t in (play.get(key) or [])]


def test_three_envs():
    assert len(PLAYBOOKS) == 3


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_admin_credentials_resolved_once_from_either_spelling(pb):
    resolve = [t for t in _tasks(pb) if "skgit_admin_password" in str(t.get("set_fact", ""))]
    assert len(resolve) == 1, "one set_fact resolving the admin credentials"
    t = resolve[0]
    assert t.get("no_log") is True
    facts = t["set_fact"]
    assert "skgit.admin_password" in facts["skgit_admin_password"] and "skgit.ADMIN_PASSWORD" in facts["skgit_admin_password"]
    assert "skgit.admin_email" in facts["skgit_admin_email"] and "skgit.ADMIN_EMAIL" in facts["skgit_admin_email"]
    names = [x.get("name", "") for x in _tasks(pb)]
    assert names.index(t["name"]) < next(i for i, n in enumerate(names) if n.startswith("Refuse an admin bootstrap"))


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_no_task_reads_the_raw_lowercase_keys_directly(pb):
    for t in _tasks(pb):
        if "skgit_admin_password" in str(t.get("set_fact", "")):
            continue
        t = {k: ({kk: vv for kk, vv in v.items() if kk not in ("fail_msg", "msg")} if isinstance(v, dict) else v)
             for k, v in t.items() if k != "name"}  # user-facing text may name the keys
        body = yaml.safe_dump(t)
        assert "skgit.admin_password" not in body and "skgit.admin_email" not in body, body[:120]
