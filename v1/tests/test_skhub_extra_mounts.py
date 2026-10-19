"""skhub.EXTRA_MOUNTS and the bind-mount drift guard.

An instance had three host bind mounts (Nextcloud external storages) added by
hand to the nextcloud and cron services with `docker service update
--mount-add` plus a hand edit of the rendered compose file. The next framework
redeploy re-rendered the compose file and replaced the stack: the mounts were
silently dropped and the three external storages broke for their users.

Two parts:

* ``skhub.EXTRA_MOUNTS`` (default empty): a list of
  ``{source, target, readonly?, propagation?}`` bind mounts rendered into BOTH
  nextcloud and cron (cron runs background jobs and scans on the same storages,
  so it must see the same paths). Items are validated before anything is
  written: absolute paths, no duplicate targets, nothing on or under the
  Nextcloud code tree or the other fixed mounts, known keys only.
* the drift guard: before the compose file is rewritten, the play reads the
  live service bind mounts and refuses to continue when one of them would be
  dropped by the new render, naming the service and target, unless
  ``skhub.ALLOW_MOUNT_DROP: true``.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
README = SKHUB / "README.md"
DRIFT = SKHUB / "files/skhub_mount_drift.py"
ENVS = ("dev", "staging", "prod")
PLAYBOOKS = {e: SKHUB / f"deploy_skhub-{e}.yml" for e in ENVS}
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "enable_talk_hpb": True, "enable_collabora": True}
INCIDENT = [
    {"source": "/mnt/cloud/onedrive-nc", "target": "/mnt/onedrive", "propagation": "rslave"},
    {"source": "/mnt/nfs-nc/cloud", "target": "/mnt/cloud", "propagation": "rslave"},
    {"source": "/mnt/nfs-nc/scratch", "target": "/mnt/scratch", "propagation": "rslave"},
]


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


def long_mounts(svc):
    return [v for v in svc.get("volumes", []) if isinstance(v, dict)]


# ---- compose ------------------------------------------------------------------

@pytest.mark.parametrize("env_name", ENVS)
@pytest.mark.parametrize("empty", [[], None, ""])
def test_empty_extra_mounts_render_byte_identical_to_unset(env_name, empty):
    assert render(env_name) == render(env_name, EXTRA_MOUNTS=empty)


@pytest.mark.parametrize("env_name", ENVS)
def test_default_has_no_long_syntax_mounts(env_name):
    for name, svc in services(env_name).items():
        assert long_mounts(svc) == [], name


@pytest.mark.parametrize("env_name", ENVS)
def test_incident_mounts_land_in_nextcloud_and_cron(env_name):
    s = services(env_name, EXTRA_MOUNTS=INCIDENT)
    want = [{"type": "bind", "source": m["source"], "target": m["target"], "bind": {"propagation": "rslave"}}
            for m in INCIDENT]
    for name in ("nextcloud", "cron"):
        assert long_mounts(s[name]) == want, name


def test_only_nextcloud_and_cron_change():
    a, b = services(), services(EXTRA_MOUNTS=INCIDENT)
    assert {n for n in a if a[n] != b[n]} == {"nextcloud", "cron", "webhook-worker"}
    for name in ("nextcloud", "cron"):
        assert b[name]["volumes"][: len(a[name]["volumes"])] == a[name]["volumes"], "fixed mounts unchanged, extras appended"


def test_readonly_and_no_propagation():
    s = services(EXTRA_MOUNTS=[{"source": "/srv/a", "target": "/mnt/a", "readonly": True},
                               {"source": "/srv/b", "target": "/mnt/b", "readonly": False}])
    assert long_mounts(s["nextcloud"]) == [
        {"type": "bind", "source": "/srv/a", "target": "/mnt/a", "read_only": True},
        {"type": "bind", "source": "/srv/b", "target": "/mnt/b"},
    ]


def test_extra_mounts_with_a_local_code_tree():
    s = services(EXTRA_MOUNTS=INCIDENT[:1], WEBROOT_LOCAL_PATH="/var/lib/skhub-prod", APP_NODE="n1")
    for name in ("nextcloud", "cron"):
        assert [m["target"] for m in long_mounts(s[name])] == ["/mnt/onedrive"]


# ---- validation (pre_tasks assert, before anything is written) ----------------

def tasks(env_name):
    out = []
    for play in yaml.safe_load(PLAYBOOKS[env_name].read_text()):
        if isinstance(play, dict):
            for sec in ("pre_tasks", "tasks"):
                out += [dict(t, _section=sec) for t in (play.get(sec) or [])]
    return out


def _validators(env_name):
    hits = [t for t in tasks(env_name) if "assert" in t and "EXTRA_MOUNTS" in json.dumps(t)]
    assert len(hits) == 2, env_name  # one per item, one for the whole list
    assert all(t["_section"] == "pre_tasks" for t in hits), "validation must run before anything is written"
    return hits


def _expr(e, text, ctx):
    """Evaluate an Ansible var/loop value: a bare "{{ x }}" keeps its native type, like Ansible."""
    m = re.fullmatch(r"\s*\{\{(.*)\}\}\s*", text)
    return e.compile_expression(m.group(1))(**ctx) if m else text


def valid(env_name="prod", **over):
    e = _env()
    ctx = {"app": "skhub", "env": env_name, "skhub": dict(BASE, **over)}
    for t in _validators(env_name):
        tctx = dict(ctx)
        for k, v in (t.get("vars") or {}).items():
            tctx[k] = _expr(e, v, tctx)
        items = _expr(e, t["loop"], tctx) if "loop" in t else [None]
        for item in items:
            ictx = dict(tctx, item=item)
            for c in t["assert"]["that"]:
                try:
                    if not e.compile_expression(c)(**ictx):
                        return False
                except Exception:  # Ansible fails the task on an evaluation error too
                    return False
    return True


@pytest.mark.parametrize("env_name", ENVS)
@pytest.mark.parametrize("mounts", [
    None, [], "", INCIDENT,
    [{"source": "/srv/a", "target": "/mnt/a", "readonly": True}],
    [{"source": "/srv/a", "target": "/mnt/a", "readonly": False, "propagation": "rprivate"}],
    [{"source": "/srv/a", "target": "/srv/a"}],
    [{"source": "/srv/a", "target": "/var/www/htmlx"}],
])
def test_valid_extra_mounts_pass(env_name, mounts):
    assert valid(env_name, EXTRA_MOUNTS=mounts)


@pytest.mark.parametrize("env_name", ENVS)
@pytest.mark.parametrize("mounts", [
    "/srv/a:/mnt/a",                                                    # not a list
    {"source": "/srv/a", "target": "/mnt/a"},                           # a mapping, not a list
    ["/srv/a:/mnt/a"],                                                  # short syntax item
    [{"target": "/mnt/a"}],                                             # no source
    [{"source": "/srv/a"}],                                             # no target
    [{"source": "srv/a", "target": "/mnt/a"}],                          # relative source
    [{"source": "/srv/a", "target": "mnt/a"}],                          # relative target
    [{"source": "/srv/a", "target": "/mnt/a/"}],                        # trailing slash
    [{"source": "/srv/a b", "target": "/mnt/a"}],                       # whitespace
    [{"source": "/srv/a", "target": "/mnt/a:ro"}],                      # colon
    [{"source": "/srv/a", "target": "/mnt/a,b"}],                       # comma (mount spec csv)
    [{"source": "/srv/a", "target": "/"}],
    [{"source": "/srv/a", "target": "/var/www/html"}],                  # the code tree
    [{"source": "/srv/a", "target": "/var/www/html/data"}],             # under it
    [{"source": "/srv/a", "target": "/var/www"}],                       # above it
    [{"source": "/srv/a", "target": "/var"}],
    [{"source": "/srv/a", "target": "/docker-entrypoint-hooks.d/x"}],
    [{"source": "/srv/a", "target": "/etc/localtime"}],
    [{"source": "/srv/a", "target": "/etc"}],
    [{"source": "/srv/a", "target": "/usr/local/etc/php/conf.d"}],
    [{"source": "/srv/a", "target": "/mnt/a"}, {"source": "/srv/b", "target": "/mnt/a"}],  # duplicate target
    [{"source": "/srv/a", "target": "/mnt/a", "readonly": "yes"}],      # not a boolean
    [{"source": "/srv/a", "target": "/mnt/a", "propagation": "bogus"}],
    [{"source": "/srv/a", "target": "/mnt/a", "read_only": True}],     # unknown key (typo)
    [{"source": 5, "target": "/mnt/a"}],
])
def test_bad_extra_mounts_are_refused(env_name, mounts):
    assert not valid(env_name, EXTRA_MOUNTS=mounts)


# ---- drift guard: playbook wiring ---------------------------------------------

def _guard_tasks(env_name):
    ts = tasks(env_name)
    names = [t.get("name", "") for t in ts]
    live = [i for i, t in enumerate(ts) if "skhub_live_mounts" == t.get("register")]
    cmp_ = [i for i, t in enumerate(ts) if "skhub_mount_drift.py" in json.dumps(t)]
    compose = [i for i, n in enumerate(names) if n.startswith("Process {{ app }} Docker Compose template")]
    deploy = [i for i, n in enumerate(names) if n == "Deploy {{ app }} stack"]
    assert len(live) == 1 and len(cmp_) == 1 and len(compose) == 1 and len(deploy) == 1, env_name
    return ts, live[0], cmp_[0], compose[0], deploy[0]


@pytest.mark.parametrize("env_name", ENVS)
def test_guard_runs_before_the_compose_file_is_rewritten(env_name):
    ts, live, cmp_, compose, deploy = _guard_tasks(env_name)
    assert live < cmp_ < compose < deploy
    for i in (live, cmp_):
        assert ts[i].get("check_mode") is False, "must also run (read-only) in a --check dry run"
        assert ts[i].get("changed_when") is False


@pytest.mark.parametrize("env_name", ENVS)
def test_live_read_prints_mounts_only_never_env(env_name):
    ts, live, *_ = _guard_tasks(env_name)
    body = ts[live]["shell"]
    assert ".Spec.TaskTemplate.ContainerSpec.Mounts" in body
    assert "Env" not in body
    assert "com.docker.stack.namespace={{ app }}-{{ env }}" in body


@pytest.mark.parametrize("env_name", ENVS)
def test_compare_renders_the_same_template_and_honours_the_override(env_name):
    ts, _, cmp_, compose, _ = _guard_tasks(env_name)
    t = ts[cmp_]
    blob = json.dumps(t)
    assert ts[compose]["template"]["src"] == "{{ playbook_dir }}/src/config/{{ app }}/{{ app }}.yml.j2"
    assert "lookup('template', playbook_dir ~ '/src/config/' ~ app ~ '/' ~ app ~ '.yml.j2')" in blob
    assert "ALLOW_MOUNT_DROP" in blob
    assert t["delegate_to"] == "localhost" and t["become"] is False


# ---- drift guard: the comparison itself ---------------------------------------

def live_line(name, mounts):
    return json.dumps(name) + " " + json.dumps(mounts)


def bind(src, dst, **kw):
    return dict({"Type": "bind", "Source": src, "Target": dst}, **kw)


def run_drift(rendered, live_lines, allow=False, stack="skhub-prod"):
    payload = {"rendered": rendered, "live": "\n".join(live_lines), "allow_drop": allow}
    return subprocess.run([sys.executable, str(DRIFT), "--stack", stack], input=json.dumps(payload),
                          capture_output=True, text=True)


def live_from_render(s, stack="skhub-prod"):
    """What swarm reports for a deployed render (short syntax -> Mounts)."""
    out = []
    for name, svc in s.items():
        ms = []
        for v in svc.get("volumes", []):
            if isinstance(v, dict):
                ms.append(bind(v["source"], v["target"]))
            else:
                p = v.split(":")
                ms.append(bind(p[0], p[1], ReadOnly=True) if p[2:] == ["ro"] else bind(p[0], p[1]))
        out.append(live_line(f"{stack}_{name}", ms or None))
    return out


def test_unchanged_render_passes():
    s = services(EXTRA_MOUNTS=INCIDENT)
    r = run_drift({"services": s}, live_from_render(s))
    assert r.returncode == 0, r.stdout + r.stderr


def test_the_incident_is_refused_naming_service_and_target():
    """Live has the three mounts; the new render (vault without them) drops them."""
    live = live_from_render(services(EXTRA_MOUNTS=INCIDENT))
    r = run_drift({"services": services()}, live)
    assert r.returncode != 0
    out = r.stdout + r.stderr
    for svc in ("skhub-prod_nextcloud", "skhub-prod_cron"):
        for t in ("/mnt/onedrive", "/mnt/cloud", "/mnt/scratch"):
            assert re.search(rf"{svc}: {re.escape(t)}\b", out), (svc, t, out)
    assert "ALLOW_MOUNT_DROP" in out


def test_hand_added_mount_is_refused():
    s = services()
    live = live_from_render(s)
    live = [l if not l.startswith('"skhub-prod_cron"') else
            live_line("skhub-prod_cron", json.loads(l.split(" ", 1)[1]) + [bind("/srv/x", "/mnt/x")]) for l in live]
    r = run_drift({"services": s}, live)
    assert r.returncode != 0 and "skhub-prod_cron: /mnt/x" in r.stdout


def test_allow_mount_drop_passes_but_still_names_the_targets():
    live = live_from_render(services(EXTRA_MOUNTS=INCIDENT))
    r = run_drift({"services": services()}, live, allow=True)
    assert r.returncode == 0
    assert "skhub-prod_nextcloud: /mnt/cloud" in r.stdout


def test_first_deploy_has_no_live_services():
    r = run_drift({"services": services()}, [])
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_service_absent_from_the_render_is_not_judged():
    """talk-recording off now: swarm does not remove it on deploy, nothing is dropped from a rendered service."""
    s = services()
    live = live_from_render(s) + [live_line("skhub-prod_talk-recording", [bind("/var/data/x", "/y")])]
    assert run_drift({"services": s}, live).returncode == 0


def test_source_change_is_not_a_drop():
    """WEBROOT_LOCAL_PATH moves html from /var/data to local disk: same target, new source."""
    live = live_from_render(services())
    s = services(WEBROOT_LOCAL_PATH="/var/lib/skhub-prod", APP_NODE="n1")
    assert run_drift({"services": s}, live).returncode == 0


def test_volumes_and_tmpfs_are_ignored():
    s = services()
    live = live_from_render(s)
    live = [l if not l.startswith('"skhub-prod_redis"') else
            live_line("skhub-prod_redis", json.loads(l.split(" ", 1)[1]) + [
                {"Type": "volume", "Source": "v", "Target": "/v"}, {"Type": "tmpfs", "Target": "/t"}]) for l in live]
    assert run_drift({"services": s}, live).returncode == 0


def test_other_stack_names_are_not_matched():
    live = [live_line("skhub-dev_nextcloud", [bind("/srv/x", "/mnt/x")])]
    assert run_drift({"services": services()}, live, stack="skhub-prod").returncode == 0


@pytest.mark.parametrize("garbage", ["not json", '"skhub-prod_cron" {not json}'])
def test_unparseable_live_output_fails_closed(garbage):
    r = run_drift({"services": services()}, [garbage])
    assert r.returncode != 0


# ---- docs ---------------------------------------------------------------------

def test_readme_documents_both_knobs():
    text = README.read_text()
    assert "## Extra bind mounts" in text
    for k in ("EXTRA_MOUNTS", "ALLOW_MOUNT_DROP", "propagation", "readonly", "cron"):
        assert k in text, k


# ---- drift guard through real Ansible ------------------------------------------
# The two guard tasks exactly as the prod playbook has them, run against
# localhost with a fake `docker` on PATH: proves the template lookup, the stdin
# payload and the interpreter path work in Ansible itself, and that --check
# still runs the guard.

ANSIBLE_PLAYBOOK = shutil.which("ansible-playbook")


def _real_guard(tmp_path, live_lines, skhub_vars, check=False):
    ts, live, cmp_, _, _ = _guard_tasks("prod")
    play_tasks = []
    for t in (ts[live], ts[cmp_]):
        t = {k: v for k, v in t.items() if k != "_section"}
        play_tasks.append(t)
    for d in ("src", "files"):
        (tmp_path / d).symlink_to(SKHUB / d)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (tmp_path / "live.txt").write_text("\n".join(live_lines) + ("\n" if live_lines else ""))
    docker = bindir / "docker"
    docker.write_text("#!/bin/sh\n"
                      "case \"$1 $2\" in\n"
                      f"  'service ls') if [ -s {tmp_path}/live.txt ]; then echo id1; fi ;;\n"
                      f"  'service inspect') cat {tmp_path}/live.txt ;;\n"
                      "  *) exit 9 ;;\n"
                      "esac\n")
    docker.chmod(0o755)
    pb = tmp_path / "guard.yml"
    pb.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"app": "skhub", "env": "prod", "skhub": dict(BASE, **skhub_vars)},
        "tasks": play_tasks}]))
    env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", ANSIBLE_NOCOLOR="1",
               ANSIBLE_LOCALHOST_WARNING="0", ANSIBLE_INVENTORY_UNPARSED_WARNING="0")
    cmd = [ANSIBLE_PLAYBOOK, "-i", "localhost,", str(pb)] + (["--check"] if check else [])
    return subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=tmp_path)


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("check", [False, True])
def test_real_ansible_guard_refuses_the_incident(tmp_path, check):
    live = live_from_render(services(EXTRA_MOUNTS=INCIDENT))
    r = _real_guard(tmp_path, live, {}, check=check)
    assert r.returncode != 0, r.stdout[-3000:]
    assert "skhub-prod_nextcloud: /mnt/cloud" in r.stdout, r.stdout[-3000:]


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("skhub_vars", [{"EXTRA_MOUNTS": INCIDENT}, {"ALLOW_MOUNT_DROP": True}])
def test_real_ansible_guard_passes_with_the_mounts_or_the_override(tmp_path, skhub_vars):
    live = live_from_render(services(EXTRA_MOUNTS=INCIDENT))
    r = _real_guard(tmp_path, live, skhub_vars)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


@pytest.mark.skipif(ANSIBLE_PLAYBOOK is None, reason="ansible-playbook not installed")
def test_real_ansible_guard_first_deploy(tmp_path):
    r = _real_guard(tmp_path, [], {})
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
