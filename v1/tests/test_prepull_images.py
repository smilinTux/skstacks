"""Pre-pull images before `docker stack deploy` (shared/tasks/prepull_images.yml).

Incident, nor estate, 2026-11-27: an sksso deploy moved Authentik to a new
ghcr.io digest and every task pull on the worker nodes was answered `denied`.
The image existed and a manual `docker pull` worked on every node right after,
but the stack deploy had already replaced the service spec, so server and worker
sat at 0/1 and `docker service rollback` restored nothing (PreviousSpec was the
new spec). Pulling first, with retries, and failing BEFORE `docker stack deploy`
turns that outage into a failed play with every running service untouched.

Three layers:
  1. the planner (shared/src/prepull_plan.py): which node needs which image,
     honouring placement constraints, drained nodes and unresolved references;
  2. the task file under ansible-playbook on localhost with a fake `docker`:
     retries with backoff, fail-closed before the deploy, digest-present skip,
     the off switch;
  3. every playbook that runs a stack deploy script includes the task right
     before it.
"""
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys

import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SHARED = ANSIBLE / "shared"
sys.path.insert(0, str(SHARED / "src"))
import prepull_plan as pp  # noqa: E402

needs_ansible = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")


def node(host, role="worker", labels=None, state="ready", availability="active", engine_labels=None, os_="linux", arch="x86_64"):
    return {
        "ID": f"id-{host}",
        "Description": {"Hostname": host, "Platform": {"OS": os_, "Architecture": arch}, "Engine": {"Labels": engine_labels or {}}},
        "Spec": {"Role": role, "Availability": availability, "Labels": labels or {}},
        "Status": {"State": state, "Addr": "192.0.2.1"},
    }


def svc(image, constraints=None, **deploy):
    d = dict(deploy)
    if constraints is not None:
        d["placement"] = {"constraints": constraints}
    return {"image": image, "deploy": d}


NODES = [node("m1", "manager"), node("w1", labels={"disk": "ssd"}), node("w2", labels={"disk": "hdd"})]


# 1. planner ---------------------------------------------------------------

def test_unconstrained_service_goes_to_every_ready_node():
    out = pp.plan({"services": {"a": svc("repo/a:1")}}, NODES)
    assert out["nodes"] == {"m1": ["repo/a:1"], "w1": ["repo/a:1"], "w2": ["repo/a:1"]}


def test_role_hostname_and_label_constraints_narrow_the_nodes():
    compose = {"services": {
        "mgr": svc("repo/mgr:1", ["node.role == manager"]),
        "ssd": svc("repo/ssd:1", ["node.labels.disk == ssd"]),
        "host": svc("repo/host:1", ["node.hostname == w2"]),
        "notmgr": svc("repo/notmgr:1", ["node.role != manager"]),
    }}
    out = pp.plan(compose, NODES)["nodes"]
    assert out["m1"] == ["repo/mgr:1"]
    assert out["w1"] == ["repo/ssd:1", "repo/notmgr:1"] or set(out["w1"]) == {"repo/ssd:1", "repo/notmgr:1"}
    assert set(out["w2"]) == {"repo/host:1", "repo/notmgr:1"}


def test_all_constraints_of_one_service_must_hold():
    compose = {"services": {"a": svc("repo/a:1", ["node.role == worker", "node.labels.disk == ssd"])}}
    assert pp.plan(compose, NODES)["nodes"] == {"w1": ["repo/a:1"]}


def test_an_unset_label_differs_from_every_value():
    compose = {"services": {"a": svc("repo/a:1", ["node.labels.gpu != yes"]), "b": svc("repo/b:1", ["node.labels.gpu == yes"])}}
    out = pp.plan(compose, NODES)
    assert set(out["nodes"]) == {"m1", "w1", "w2"} and out["unplaced"] == ["b"]


def test_engine_labels_and_platform_constraints():
    nodes = [node("a", engine_labels={"zone": "x"}, arch="aarch64"), node("b", engine_labels={"zone": "y"})]
    compose = {"services": {"s": svc("r/s:1", ["engine.labels.zone == x", "node.platform.arch == aarch64"])}}
    assert pp.plan(compose, nodes)["nodes"] == {"a": ["r/s:1"]}


def test_unknown_constraint_key_matches_so_it_can_only_over_pull():
    compose = {"services": {"a": svc("repo/a:1", ["something.new == x", "not even a constraint"])}}
    assert set(pp.plan(compose, NODES)["nodes"]) == {"m1", "w1", "w2"}


def test_drained_paused_and_down_nodes_get_nothing():
    nodes = [node("ok"), node("drain", availability="drain"), node("pause", availability="pause"), node("down", state="down")]
    out = pp.plan({"services": {"a": svc("repo/a:1")}}, nodes)
    assert out["nodes"] == {"ok": ["repo/a:1"]}


def test_service_with_no_eligible_node_is_reported_not_pulled():
    out = pp.plan({"services": {"a": svc("repo/a:1", ["node.hostname == nowhere"])}}, NODES)
    assert out["nodes"] == {} and out["unplaced"] == ["a"]


def test_placement_can_be_ignored_to_pull_everywhere():
    compose = {"services": {"a": svc("repo/a:1", ["node.hostname == w2"])}}
    out = pp.plan(compose, NODES, respect_placement=False)
    assert set(out["nodes"]) == {"m1", "w1", "w2"}


def test_unresolved_reference_zero_replicas_and_build_only_are_not_pulled():
    compose = {"services": {
        "var": {"image": "repo/${TAG}"},
        "off": svc("repo/off:1", replicas=0),
        "build": {"build": "."},
        "global": svc("repo/g:1", mode="global"),
    }}
    out = pp.plan(compose, NODES)
    assert {s["service"] for s in out["skipped"]} == {"var", "off"}
    assert all(imgs == ["repo/g:1"] for imgs in out["nodes"].values())


def test_same_image_in_two_services_is_pulled_once_per_node():
    compose = {"services": {"a": svc("repo/x:1"), "b": svc("repo/x:1")}}
    assert pp.plan(compose, NODES[:1])["nodes"] == {"m1": ["repo/x:1"]}


def test_cli_reads_stdin_and_prints_json():
    doc = {"compose": {"services": {"a": svc("repo/a:1")}}, "nodes": NODES[:1], "respect_placement": True}
    proc = subprocess.run([sys.executable, str(SHARED / "src/prepull_plan.py")], input=json.dumps(doc), capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["nodes"] == {"m1": ["repo/a:1"]}


# 2. the task file under ansible-playbook ---------------------------------

FAKE_DOCKER = r"""#!/bin/bash
echo "$*" >> "$FAKE_LOG"
case "$1 $2" in
  "node ls") echo id-fake ;;
  "node inspect") cat "$FAKE_NODES" ;;
  "image inspect")
    case " $FAKE_PRESENT " in *" ${@: -1} "*) exit 0 ;; esac
    exit 1 ;;
  *)
    if [ "$1" = "pull" ]; then
      img="${@: -1}"
      n=$(grep -c "^pull .*$img\$" "$FAKE_LOG")
      if [ -n "$FAKE_PULL_ALWAYS_FAIL" ] && [[ "$img" == *"$FAKE_PULL_ALWAYS_FAIL"* ]]; then
        echo "Error response from daemon: denied" >&2; exit 1
      fi
      if [ "$n" -le "${FAKE_PULL_FAIL_FIRST:-0}" ]; then
        echo "Error response from daemon: denied" >&2; exit 1
      fi
      exit 0
    fi ;;
esac
"""


@pytest.fixture
def lab(tmp_path):
    """A directory shaped like optional/<role>/ (so ../../shared resolves), a
    fake docker on PATH, and a stack file."""
    (tmp_path / "optional/demo").mkdir(parents=True)
    (tmp_path / "shared").symlink_to(SHARED)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text(FAKE_DOCKER)
    docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
    compose = tmp_path / "demo.yml"
    compose.write_text(yaml.safe_dump({"services": {
        "web": {"image": "ghcr.io/example/web:1.0@sha256:" + "a" * 64},
        "db": {"image": "docker.io/library/postgres:18"},
    }}))
    nodes = tmp_path / "nodes.json"
    nodes.write_text(json.dumps([node("localhost", "manager")]))
    (tmp_path / "fake.log").write_text("")
    return tmp_path


def run_play(lab, extra_vars=None, env_extra=None, inventory="localhost,"):
    vars_ = {"app": "demo", "env": "dev", "skstacks_prepull_compose": str(lab / "demo.yml"), "skstacks_prepull_delay": 0}
    vars_.update(extra_vars or {})
    play = lab / "optional/demo/play.yml"
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False, "vars": vars_,
        "tasks": [
            {"include_tasks": "../../shared/tasks/prepull_images.yml"},
            {"name": "Deploy marker", "copy": {"content": "deployed", "dest": str(lab / "deployed")}},
        ],
    }]))
    env = {**os.environ, "PATH": f"{lab / 'bin'}:{os.environ['PATH']}", "FAKE_LOG": str(lab / "fake.log"),
           "FAKE_NODES": str(lab / "nodes.json"), "ANSIBLE_NOCOLOR": "1", "ANSIBLE_RETRY_FILES_ENABLED": "0",
           "ANSIBLE_LOCALHOST_WARNING": "False", **(env_extra or {})}
    return subprocess.run(["ansible-playbook", "-i", inventory, "-c", "local", str(play)], capture_output=True, text=True, timeout=240, env=env)


def calls(lab):
    return (lab / "fake.log").read_text().splitlines()


@needs_ansible
def test_pulls_each_image_and_the_deploy_step_runs_after(lab):
    proc = run_play(lab)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    pulls = [c for c in calls(lab) if c.startswith("pull")]
    assert sorted(pulls) == sorted(["pull --quiet docker.io/library/postgres:18", "pull --quiet ghcr.io/example/web:1.0@sha256:" + "a" * 64])
    assert (lab / "deployed").exists()


@needs_ansible
def test_digest_pinned_image_already_present_is_not_pulled(lab):
    web = "ghcr.io/example/web:1.0@sha256:" + "a" * 64
    proc = run_play(lab, env_extra={"FAKE_PRESENT": web})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    pulls = [c for c in calls(lab) if c.startswith("pull")]
    assert pulls == ["pull --quiet docker.io/library/postgres:18"]  # a tag is always refreshed


@needs_ansible
def test_transient_refusal_is_retried_with_backoff_until_it_succeeds(lab):
    proc = run_play(lab, env_extra={"FAKE_PULL_FAIL_FIRST": "2"}, extra_vars={"skstacks_prepull_retries": 4})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    pg = [c for c in calls(lab) if c.endswith("postgres:18")]
    assert len(pg) == 3  # two refusals, then the pull
    assert (lab / "deployed").exists()


@needs_ansible
def test_refusal_that_outlasts_the_retries_stops_before_the_deploy(lab):
    proc = run_play(lab, env_extra={"FAKE_PULL_ALWAYS_FAIL": "postgres"}, extra_vars={"skstacks_prepull_retries": 3})
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0
    assert "stack deploy was NOT run" in out and "running" in out and "untouched" in out
    assert "docker.io/library/postgres:18" in out
    assert len([c for c in calls(lab) if c.endswith("postgres:18")]) == 3  # the budget, no more
    assert not (lab / "deployed").exists()


@needs_ansible
def test_off_switch_runs_nothing(lab):
    proc = run_play(lab, extra_vars={"skstacks_prepull_images": False})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert calls(lab) == []
    assert (lab / "deployed").exists()


@needs_ansible
def test_missing_stack_file_is_skipped_not_fatal(lab):
    (lab / "demo.yml").unlink()
    proc = run_play(lab)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "the pre-pull is skipped" in proc.stdout
    assert calls(lab) == []
    assert (lab / "deployed").exists()


@needs_ansible
def test_node_unknown_to_the_inventory_is_reported_and_left_alone(lab):
    (lab / "nodes.json").write_text(json.dumps([node("localhost", "manager"), node("ghost", "worker")]))
    proc = run_play(lab)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "not in the inventory" in proc.stdout and "ghost" in proc.stdout


@needs_ansible
def test_placement_constraints_decide_the_node(lab):
    (lab / "demo.yml").write_text(yaml.safe_dump({"services": {
        "web": {"image": "r/web:1", "deploy": {"placement": {"constraints": ["node.role == worker"]}}},
        "db": {"image": "r/db:1"},
    }}))
    proc = run_play(lab)  # the only node is a manager: web has nowhere to go
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert [c for c in calls(lab) if c.startswith("pull")] == ["pull --quiet r/db:1"]
    assert "No eligible node for service(s): web" in proc.stdout


# 3. every stack playbook runs it, right before the deploy -------------------

DEPLOY_CMD = re.compile(r"(\./deploy\b|/var/data/deploy_\{\{ ?app ?\}\}-\{\{ ?env ?\}\}|/var/data/\{\{ ?app ?\}\}-\{\{ ?env ?\}\}/deploy\b)")
INCLUDE_SUFFIX = "shared/tasks/prepull_images.yml"


def _stack_roles():
    return sorted({p.parts[-4] for p in ANSIBLE.glob("*/*/src/*/deploy.j2") if "docker stack deploy" in p.read_text()})


def _task_lists(tasks):
    """Every list of tasks, nested blocks included."""
    yield tasks
    for t in tasks:
        if isinstance(t, dict):
            for k in ("block", "rescue", "always"):
                if isinstance(t.get(k), list):
                    yield from _task_lists(t[k])


def _runs_deploy_script(task):
    for m in ("shell", "command", "ansible.builtin.shell", "ansible.builtin.command"):
        body = task.get(m)
        if isinstance(body, dict):
            body = body.get("cmd", "")
        if isinstance(body, str) and DEPLOY_CMD.search(body):
            return True
    return False


def _include_file(task):
    inc = task.get("include_tasks") or task.get("ansible.builtin.include_tasks")
    return (inc.get("file") if isinstance(inc, dict) else inc) or ""


def _files_of(role):
    out = []
    for kind in ("core", "optional"):
        base = ANSIBLE / kind / role
        out += sorted(base.glob("deploy_*.yml")) + sorted((base / "tasks").glob("*.yml"))
    return out


def _task_files_with_deploy(role):
    for p in _files_of(role):
        data = yaml.safe_load(p.read_text())
        if not isinstance(data, list):
            continue
        lists = []
        if any(isinstance(i, dict) and ("tasks" in i or "pre_tasks" in i) for i in data):  # a playbook
            for item in data:
                if isinstance(item, dict):
                    lists += list(_task_lists(item.get("pre_tasks") or [])) + list(_task_lists(item.get("tasks") or []))
        else:  # a tasks file
            lists += list(_task_lists(data))
        for tl in lists:
            for i, t in enumerate(tl):
                if isinstance(t, dict) and _runs_deploy_script(t):
                    yield p, tl, i


def test_the_stack_deploying_roles_are_found():
    assert len(_stack_roles()) >= 40 and "sksso" in _stack_roles() and "skfence" in _stack_roles()


@pytest.mark.parametrize("role", _stack_roles())
def test_every_stack_role_pre_pulls_right_before_its_deploy(role):
    found = list(_task_files_with_deploy(role))
    assert found, f"{role}: no task runs the deploy script"
    problems = []
    for path, tasks, i in found:
        prev = tasks[i - 1] if i else {}
        inc = _include_file(prev) if isinstance(prev, dict) else ""
        if not inc.endswith(INCLUDE_SUFFIX):
            problems.append(f"{path.relative_to(ANSIBLE)}: the task before the deploy is not the image pre-pull")
            continue
        tags = prev.get("tags") or []
        if "deploy" not in tags:
            problems.append(f"{path.relative_to(ANSIBLE)}: the pre-pull must carry the deploy tag")
        # include paths resolve from the file that holds them
        target = (path.parent / inc).resolve()
        if not target.exists():
            problems.append(f"{path.relative_to(ANSIBLE)}: {inc} does not resolve")
    assert not problems, "\n".join(problems)


def test_the_compose_file_lands_where_the_pre_pull_looks():
    """The shared task looks for <app>.yml, then docker-compose.yml, in
    /var/data/config/<app>-<env>/. A role that renders its stack file elsewhere
    would silently skip the pre-pull."""
    wanted = ("/var/data/config/{{ app }}-{{ env }}/{{ app }}.yml", "/var/data/config/{{ app }}-{{ env }}/docker-compose.yml")
    missing = []
    for role in _stack_roles():
        texts = "".join(p.read_text() for p in _files_of(role))
        if not any(w in texts for w in wanted):
            missing.append(role)
    assert not missing, f"stack file location not covered by the pre-pull: {missing}"


def test_task_file_documents_its_knobs():
    text = (SHARED / "tasks/prepull_images.yml").read_text()
    for knob in ("skstacks_prepull_images", "skstacks_prepull_retries", "skstacks_prepull_delay", "skstacks_prepull_placement", "skstacks_prepull_compose"):
        assert knob in text
