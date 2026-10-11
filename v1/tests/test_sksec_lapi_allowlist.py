"""sksec syncs sksec.ALLOWLIST_CIDRS into a CrowdSec LAPI allowlist.

The s02-enrich whitelist parser (sksec-allowlist.yaml) only drops LOCAL
events before the scenarios see them. Decisions that do not come from a
local detection, the community blocklist (CAPI), console blocklists and a
manual `cscli decisions add`, still reached the bouncer and could ban the
operator's own ranges. CrowdSec 1.6.8+ has centralized LAPI allowlists that
LAPI applies to every decision, CAPI included. A production instance applied
one by hand (allowlist `sksec-operator`); after that `cscli decisions add`
on an allowlisted IP is refused and the bouncer answers 200. Hand state is
lost on a new cluster, so the playbook now owns it:

* after the stack deploy, resolve the node running the crowdsec task
  (`docker service ps`, TRAEFIK_HA_MODE pins it to the ACME master, which is
  often not the deploy node) and delegate every `docker exec` there;
* wait (bounded) until `cscli lapi status` answers;
* make allowlist `sksec.LAPI_ALLOWLIST_NAME` (default `sksec-operator`)
  hold exactly ALLOWLIST_CIDRS: create it, add what is missing, remove what
  is no longer listed; an empty list empties it, other allowlists are never
  touched; report changed only when something changed;
* `sksec.LAPI_ALLOWLIST_ENABLED: false` skips the sync.

The behavioural tests run the real task file through `ansible-playbook`
against a `docker` test double that models swarm task placement and the
cscli allowlists commands (checked against crowdsecurity/crowdsec:v1.6.11:
values are stored verbatim, `create` on an existing name fails, `list -o
json` returns every allowlist with its items).
"""
import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SVC = ROOT / "v1/ansible/core/sksec"
TASKS = SVC / "tasks/lapi_allowlist.yml"
PLAYBOOKS = [SVC / f"deploy_sksec-{e}.yml" for e in ("dev", "staging", "prod")]
README = SVC / "README.md"
DESC = "sksec.ALLOWLIST_CIDRS (operator ranges, applies to CAPI/blocklist/cscli decisions)"
COMMENT = "sksec.ALLOWLIST_CIDRS"
CIDRS = ["192.0.2.0/24", "198.51.100.7", "2001:db8::/32"]

FAKE_DOCKER = textwrap.dedent('''\
    import json, os, sys
    path = os.environ["FAKE_DOCKER_STATE"]
    st = json.load(open(path))
    a = sys.argv[1:]
    st["calls"].append(a)
    def save():
        json.dump(st, open(path, "w"))
    def done(code=0, out=None, err=None):
        save()
        if out is not None:
            print(out)
        if err is not None:
            print(err, file=sys.stderr)
        sys.exit(code)
    if a[:2] == ["service", "ps"]:
        assert a[2] == st["service"], a
        done(0, f"{st['node']} Running 9 seconds ago" if st["node"] else "")
    if a[:1] == ["ps"]:
        assert "label=com.docker.swarm.service.name=" + st["service"] in a, a
        done(0, "fakecid")
    if a[:1] != ["exec"] or a[1] != "fakecid" or a[2] != "cscli":
        done(2, err="fake docker: unexpected call %r" % a)
    c = a[3:]
    lists = st["lists"]
    by = {l["name"]: l for l in lists}
    if c[:2] == ["lapi", "status"]:
        st["lapi_calls"] += 1
        if st["lapi_calls"] > st["lapi_fail_first"]:
            done(0, "You can successfully interact with Local API (LAPI)")
        done(1, err="Error: dial tcp: connection refused")
    if c[:1] != ["allowlists"]:
        done(2, err="fake cscli: unexpected %r" % c)
    sub = c[1]
    if sub == "list":
        assert c[2:] == ["-o", "json"], c
        done(0, json.dumps(lists, indent=2))
    name = c[2]
    rest = c[3:]
    desc = None
    if "-d" in rest:
        i = rest.index("-d")
        desc = rest[i + 1]
        rest = rest[:i] + rest[i + 2:]
    st["mutations"].append(c)
    if sub == "create":
        if name in by:
            done(1, err=f"Error: allowlist '{name}' already exists")
        lists.append({"name": name, "description": desc, "items": []})
        done(0, f"allowlist '{name}' created successfully")
    if name not in by:
        done(1, err=f"Error: allowlist '{name}' not found")
    items = by[name]["items"]
    have = [i["value"] for i in items]
    if sub == "add":
        new = [v for v in rest if v not in have]
        items.extend({"value": v, "description": desc, "expiration": "0001-01-01T00:00:00.000Z"} for v in new)
        done(0, f"added {len(new)} values to allowlist {name}" if new else "no new values for allowlist")
    if sub == "remove":
        by[name]["items"] = [i for i in items if i["value"] not in rest]
        done(0, "removed values")
    done(2, err="fake cscli: unexpected %r" % c)
''')

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")


def _state(tmp_path, lists=None, node="localhost", lapi_fail_first=0, env="prod"):
    st = {"service": f"sksec-{env}_crowdsec", "node": node, "lapi_fail_first": lapi_fail_first,
          "lapi_calls": 0, "lists": lists or [], "calls": [], "mutations": []}
    p = tmp_path / "state.json"
    p.write_text(json.dumps(st))
    return p


def _run(tmp_path, sksec, env="prod", state=None):
    state = state or _state(tmp_path, env=env)
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "fake_docker.py").write_text(FAKE_DOCKER)
    docker = bindir / "docker"
    docker.write_text(f"#!/bin/sh\nexec {sys.executable} {bindir / 'fake_docker.py'} \"$@\"\n")
    docker.chmod(0o755)
    inv = tmp_path / "inv.ini"
    inv.write_text(f"localhost ansible_connection=local ansible_python_interpreter={sys.executable}\n")
    play = [{"hosts": "localhost", "gather_facts": False,
             "vars": {"app": "sksec", "env": env, "sksec": sksec},
             "tasks": [{"name": "sync", "include_tasks": str(TASKS)}]}]
    pb = tmp_path / "pb.yml"
    pb.write_text(yaml.safe_dump(play, sort_keys=False))
    envv = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_DOCKER_STATE=str(state),
                HOME=str(tmp_path), ANSIBLE_NOCOLOR="1", ANSIBLE_LOCALHOST_WARNING="False",
                ANSIBLE_RETRY_FILES_ENABLED="False")
    r = subprocess.run(["ansible-playbook", "-i", str(inv), str(pb)], capture_output=True, text=True, env=envv)
    return r, json.loads(state.read_text())


def _recap_changed(r):
    line = next(l for l in r.stdout.splitlines() if l.startswith("localhost") and "changed=" in l)
    return int(line.split("changed=")[1].split()[0])


def _values(st, name="sksec-operator"):
    for l in st["lists"]:
        if l["name"] == name:
            return [i["value"] for i in l["items"]]
    return None


def _log(r):
    return r.stdout[-3000:] + r.stderr[-2000:]


# ---- behaviour -------------------------------------------------------------

def test_fresh_cluster_creates_the_allowlist_with_every_cidr(tmp_path):
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS})
    assert r.returncode == 0, _log(r)
    assert sorted(_values(st)) == sorted(CIDRS)
    lst = next(l for l in st["lists"] if l["name"] == "sksec-operator")
    assert lst["description"] == DESC
    assert {i["description"] for i in lst["items"]} == {COMMENT}
    assert _recap_changed(r) >= 1


def test_second_run_changes_nothing(tmp_path):
    r1, _ = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS})
    assert r1.returncode == 0, _log(r1)
    state = tmp_path / "state.json"
    st = json.loads(state.read_text())
    st["mutations"] = []
    state.write_text(json.dumps(st))
    r2, st2 = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS}, state=state)
    assert r2.returncode == 0, _log(r2)
    assert st2["mutations"] == []
    assert _recap_changed(r2) == 0


def test_drift_is_converged_and_other_allowlists_are_untouched(tmp_path):
    other = {"name": "console-list", "description": "x", "items": [{"value": "203.0.113.5", "description": "y"}]}
    ours = {"name": "sksec-operator", "description": DESC,
            "items": [{"value": "192.0.2.0/24", "description": COMMENT},
                      {"value": "203.0.113.0/24", "description": COMMENT}]}
    state = _state(tmp_path, lists=[other, ours])
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS}, state=state)
    assert r.returncode == 0, _log(r)
    assert sorted(_values(st)) == sorted(CIDRS)
    assert next(l for l in st["lists"] if l["name"] == "console-list") == other
    assert not any(m[1] == "create" for m in st["mutations"])
    assert all(m[2] == "sksec-operator" for m in st["mutations"])
    assert _recap_changed(r) >= 1


def test_empty_list_empties_the_allowlist_but_keeps_it(tmp_path):
    other = {"name": "console-list", "description": "x", "items": [{"value": "203.0.113.5", "description": "y"}]}
    ours = {"name": "sksec-operator", "description": DESC,
            "items": [{"value": v, "description": COMMENT} for v in CIDRS]}
    state = _state(tmp_path, lists=[other, ours])
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": []}, state=state)
    assert r.returncode == 0, _log(r)
    assert _values(st) == []
    assert next(l for l in st["lists"] if l["name"] == "console-list") == other


@pytest.mark.parametrize("sksec", [{"ALLOWLIST_CIDRS": []}, {}], ids=["empty", "unset"])
def test_empty_list_and_no_allowlist_creates_nothing(tmp_path, sksec):
    r, st = _run(tmp_path, sksec)
    assert r.returncode == 0, _log(r)
    assert st["mutations"] == []
    assert st["lists"] == []
    assert _recap_changed(r) == 0


def test_allowlist_name_is_overridable(tmp_path):
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS[:1], "LAPI_ALLOWLIST_NAME": "site-ops"})
    assert r.returncode == 0, _log(r)
    assert _values(st, "site-ops") == CIDRS[:1]
    assert _values(st) is None


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_targets_the_envs_crowdsec_service(tmp_path, env):
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS[:1]}, env=env, state=_state(tmp_path, env=env))
    assert r.returncode == 0, _log(r)
    assert _values(st) == CIDRS[:1]


def test_waits_for_lapi_before_touching_it(tmp_path):
    state = _state(tmp_path, lapi_fail_first=1)
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS}, state=state)
    assert r.returncode == 0, _log(r)
    assert st["lapi_calls"] == 2
    first_mutation = next(i for i, c in enumerate(st["calls"]) if "allowlists" in c)
    last_lapi = max(i for i, c in enumerate(st["calls"]) if "lapi" in c)
    assert last_lapi < first_mutation


def test_lapi_never_ready_fails_the_play_without_mutating(tmp_path):
    state = _state(tmp_path, lapi_fail_first=10**6)
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS, "LAPI_WAIT_SECONDS": 1}, state=state)
    assert r.returncode != 0
    assert st["mutations"] == []
    assert 1 <= st["lapi_calls"] <= 3, "the wait must be bounded by LAPI_WAIT_SECONDS"


def test_no_running_crowdsec_task_fails_the_play(tmp_path):
    state = _state(tmp_path, node="")
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": CIDRS, "LAPI_WAIT_SECONDS": 1}, state=state)
    assert r.returncode != 0
    assert st["mutations"] == []
    assert not any(c[:1] == ["exec"] for c in st["calls"])


def test_a_string_instead_of_a_list_fails_closed(tmp_path):
    r, st = _run(tmp_path, {"ALLOWLIST_CIDRS": "192.0.2.0/24"})
    assert r.returncode != 0
    assert "ALLOWLIST_CIDRS" in r.stdout
    assert st["mutations"] == []


# ---- wiring ----------------------------------------------------------------

def _tasks(path):
    data = yaml.safe_load(path.read_text())
    if any(isinstance(d, dict) and ("hosts" in d or "import_playbook" in d) for d in data or []):
        out = []
        for play in data:
            out += play.get("tasks") or []
        return out
    return data


def _cmd(t):
    for k in ("shell", "command", "ansible.builtin.shell", "ansible.builtin.command"):
        if k in t:
            return json.dumps(t[k])
    return ""


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_playbooks_sync_after_the_stack_deploy_behind_the_knob(pb):
    tasks = _tasks(pb)
    names = [t.get("name", "") for t in tasks]
    deploy = names.index("Deploy {{ app }} stack")
    inc = [i for i, t in enumerate(tasks)
           if "lapi_allowlist.yml" in json.dumps(t.get("include_tasks") or t.get("ansible.builtin.include_tasks") or "")]
    assert len(inc) == 1, pb.name
    assert inc[0] > deploy
    when = json.dumps(tasks[inc[0]].get("when"))
    assert "LAPI_ALLOWLIST_ENABLED" in when and "default(true)" in when.replace(" ", "")


def test_every_docker_exec_runs_on_the_crowdsec_node():
    for t in _tasks(TASKS):
        if "exec" in _cmd(t) or "docker ps" in _cmd(t):
            assert "sksec_crowdsec_node" in str(t.get("delegate_to", "")), t.get("name")


def test_mutations_use_argv_not_a_shell():
    for t in _tasks(TASKS):
        c = _cmd(t)
        if any(s in c for s in ('"create"', '"add"', '"remove"', "allowlists create", "allowlists add", "allowlists remove")):
            assert "command" in t or "ansible.builtin.command" in t, t.get("name")
            assert "argv" in json.dumps(t.get("command") or t.get("ansible.builtin.command")), t.get("name")


def test_nothing_ignores_errors():
    for t in _tasks(TASKS):
        assert not t.get("ignore_errors"), t.get("name")


def test_readme_documents_the_lapi_allowlist():
    text = README.read_text()
    for needle in ("LAPI_ALLOWLIST_ENABLED", "LAPI_ALLOWLIST_NAME", "LAPI_WAIT_SECONDS",
                   "cscli allowlists check", "cscli decisions delete --ip", "CAPI"):
        assert needle in text, needle
