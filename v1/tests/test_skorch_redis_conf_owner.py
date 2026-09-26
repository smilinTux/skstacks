"""skorch's redis reads its password from a bind-mounted redis.conf. The
official redis image starts as root, then its entrypoint drops to the redis
user (uid 999) before exec'ing redis-server. A root:root 0640 redis.conf is
unreadable to that user: "Fatal error, can't open config file ... Permission
denied", exit 1, service stuck 0/1 (skstack06 v2.19.0 rc1). The file must be
readable by uid 999 while staying unreadable to other users."""
import pathlib

import yaml

SKORCH = pathlib.Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skorch"
PLAYBOOKS = sorted(SKORCH.glob("deploy_skorch-*.yml"))
REDIS_UID = "999"


def _tasks(playbook):
    for play in yaml.safe_load(playbook.read_text()):
        for key in ("pre_tasks", "tasks", "post_tasks"):
            yield from play.get(key) or []


def _redis_conf_tasks(playbook):
    found = []
    for task in _tasks(playbook):
        mod = task.get("template") or task.get("ansible.builtin.template")
        if not mod:
            continue
        dests = [str(mod.get("dest", ""))] + [str(i.get("dest", "")) for i in task.get("loop") or [] if isinstance(i, dict)]
        if any(d.endswith("/redis.conf") for d in dests):
            found.append((task, mod))
    return found


def test_every_env_playbook_renders_redis_conf():
    assert len(PLAYBOOKS) == 3
    for pb in PLAYBOOKS:
        assert _redis_conf_tasks(pb), pb.name


def test_redis_conf_is_readable_by_the_redis_user_and_no_one_else():
    for pb in PLAYBOOKS:
        for task, mod in _redis_conf_tasks(pb):
            owner, group, mode = str(mod.get("owner")), str(mod.get("group")), str(mod.get("mode"))
            assert REDIS_UID in (owner, group), f"{pb.name}: redis.conf owner/group {owner}:{group} is not uid {REDIS_UID}"
            assert mode.lstrip("0")[-1:] == "0", f"{pb.name}: redis.conf mode {mode} is world-readable"
