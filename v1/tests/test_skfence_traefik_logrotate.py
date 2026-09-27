"""skfence/skfenceha Traefik logrotate: rotate what Traefik actually writes.

Found in a production deploy: the rendered /etc/logrotate.d/<app>-<env> file
never rotated anything, and one ingress node's access log grew to tens of GB.

1. The glob did not match Traefik's layout (an instance copy was one level
   too deep); `missingok` hid it and logrotate exited 0 every night.
2. The postrotate `pkill -USR1 -f "traefik"` (skfenceha) never matches the
   container's PID 1 (`traefik --configFile=...`) and would also signal
   unrelated processes (traefik-certs-dumper, any other Traefik on the
   host); skfence signalled a /var/run/traefik.pid that does not exist.
3. `*/*.log` also caught Traefik's MAIN log (log.filePath). Traefik v3
   rotates that one itself and does NOT reopen it on USR1, so an external
   rename strands its write fd on the rotated (then deleted) inode.
4. The log directory is shared storage: every node sees every node's files,
   but a node can only signal its own Traefik. Each node must rotate only
   its own access logs.

The fix lists this node's ACCESS logs only (depth 2 under the log dir,
exactly the filePath Traefik is configured with), signals only this node's
Traefik containers by Swarm service-name label with `docker kill -s USR1`,
and exposes size/rotate knobs.
"""
import pathlib
import re

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
CORE = V1 / "ansible/core"
LOG_DIR_EXPR = "/var/data/logs/{{ app }}-{{ env }}/traefik"
ENVS = ("dev", "staging", "prod")
NODE = "node-a"

SKFENCEHA_TMPL = CORE / "skfenceha/src/config/skfenceha/logrotate.conf.j2"
SKFENCE_TMPL = CORE / "skfence/src/config/skfence/logrotate.conf.j2"
ENTRYPOINT = CORE / "skfenceha/src/config/skfenceha/entrypoint.sh"
SKFENCE_TRAEFIK = CORE / "skfence/src/config/skfence/traefik.yml.j2"


def _render(tmpl, app, env="prod", knobs=None, rotate="7"):
    ctx = {
        "app": app,
        "env": env,
        "log_dir": f"/var/data/logs/{app}-{env}/traefik",
        "logrotate_node": NODE,
        "retention_days": rotate,
        app: knobs or {},
    }
    j = jinja2.Environment(undefined=jinja2.ChainableUndefined, keep_trailing_newline=True)
    return j.from_string(tmpl.read_text()).render(**ctx)


def _lines(text):
    return [l.strip() for l in text.splitlines() if l.strip() and not l.strip().startswith("#")]


def _paths(text):
    """Log paths: the lines up to the one that opens the block with '{'."""
    out = []
    for l in _lines(text):
        out.append(l.rstrip("{").strip())
        if l.endswith("{"):
            return [p for p in out if p]
    raise AssertionError(f"no logrotate block in:\n{text}")


def _directives(text):
    lines = _lines(text)
    start = next(i for i, l in enumerate(lines) if l.endswith("{"))
    return lines[start + 1:]


def _script(text, name):
    m = re.search(rf"^\s*{name}\s*$(.*?)^\s*endscript\s*$", text, re.M | re.S)
    assert m, f"no {name} block"
    return "\n".join(l for l in m.group(1).splitlines() if not l.strip().startswith("#"))


CASES = [
    ("skfenceha", SKFENCEHA_TMPL,
     {f"worker/access-{NODE}.log", f"acme/access-{NODE}.log"},
     ["skfenceha-prod_traefik-worker", "skfenceha-prod_traefik-acme"]),
    ("skfence", SKFENCE_TMPL,
     {f"{NODE}/access.log"},
     ["skfence-prod_traefik"]),
]


@pytest.mark.parametrize("app,tmpl,want,services", CASES, ids=[c[0] for c in CASES])
def test_paths_are_this_nodes_access_logs_two_levels_under_the_log_dir(app, tmpl, want, services):
    text = _render(tmpl, app)
    log_dir = f"/var/data/logs/{app}-prod/traefik/"
    paths = _paths(text)
    assert paths, text
    rel = set()
    for p in paths:
        assert p.startswith(log_dir), p
        assert "*" not in p and "?" not in p, f"no globs: the dir is shared by every node: {p}"
        r = p[len(log_dir):]
        assert len(r.split("/")) == 2, f"Traefik writes two levels under the log dir: {p}"
        rel.add(r)
    assert rel == want


@pytest.mark.parametrize("app,tmpl,want,services", CASES, ids=[c[0] for c in CASES])
def test_main_traefik_log_is_never_rotated(app, tmpl, want, services):
    for p in _paths(_render(tmpl, app)):
        name = p.rsplit("/", 1)[1]
        assert name.startswith("access"), f"only access logs; Traefik rotates its main log itself: {p}"
        assert "traefik" not in name, p


@pytest.mark.parametrize("app,tmpl,want,services", CASES, ids=[c[0] for c in CASES])
def test_postrotate_signals_local_traefik_by_service_label(app, tmpl, want, services):
    text = _render(tmpl, app)
    post = _script(text, "postrotate")
    assert "pkill" not in text and "pgrep" not in text and "traefik.pid" not in text
    assert "docker kill -s USR1" in post
    assert "restart" not in post and "docker service update" not in post
    for svc in services:
        assert f"label=com.docker.swarm.service.name={svc}" in post, post
    assert "label=com.docker.swarm.service.name=" in post
    # a node without a Traefik container must not fail the logrotate run
    assert "|| true" in post


@pytest.mark.parametrize("app,tmpl,want,services", CASES, ids=[c[0] for c in CASES])
def test_validated_settings(app, tmpl, want, services):
    d = _directives(_render(tmpl, app))
    for want_d in ("su root root", "daily", "maxsize 200M", "rotate 7", "dateext",
                   "dateformat -%Y%m%d-%s", "compress", "delaycompress", "missingok",
                   "notifempty", "create 0644 root root", "sharedscripts"):
        assert want_d in d, f"{want_d!r} missing from {d}"


@pytest.mark.parametrize("app,tmpl,want,services", CASES, ids=[c[0] for c in CASES])
def test_knobs_drive_size_and_rotate(app, tmpl, want, services):
    d = _directives(_render(tmpl, app, knobs={"LOGROTATE_MAXSIZE": "1G"}, rotate="14"))
    assert "maxsize 1G" in d and "rotate 14" in d
    assert "maxsize 200M" not in d and "rotate 7" not in d


def test_skfenceha_paths_match_the_entrypoint_filepaths():
    """entrypoint.sh builds the filePaths: /logs/<role>/access-<hostname>.log,
    with /logs bind-mounted from <log_dir>."""
    sh = ENTRYPOINT.read_text()
    assert 'ACCESS_LOG="${LOG_DIR}/access-${NODE_HOSTNAME}.log"' in sh
    assert 'LOG_DIR="${LOG_BASE}/${ROLE_DIR}"' in sh
    assert 'ROLE_DIR="worker"' in sh and 'ROLE_DIR="acme"' in sh
    assert 'ERROR_LOG="${LOG_DIR}/traefik-${NODE_HOSTNAME}.log"' in sh


def test_skfence_paths_match_the_traefik_filepaths():
    j = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    cfg = yaml.safe_load(j.from_string(SKFENCE_TRAEFIK.read_text()).render(
        env="prod", app="skfence", NODE_HOSTNAME=NODE, skfence={}))
    host_dir = "/var/data/logs/skfence-prod/traefik"
    to_host = lambda p: p.replace("/var/log/traefik", host_dir, 1)
    paths = _paths(_render(SKFENCE_TMPL, "skfence"))
    assert paths == [to_host(cfg["accessLog"]["filePath"])]
    assert to_host(cfg["log"]["filePath"]) not in paths


def _logrotate_tasks(path):
    plays = yaml.safe_load(path.read_text())
    tasks = [t for p in plays if isinstance(p, dict) for t in p.get("tasks", []) or []]
    return [t for t in tasks if "logrotate.conf.j2" in str(t.get("template", ""))], tasks


PLAYBOOKS = [(svc, env, CORE / f"{svc}/deploy_{svc}-{env}.yml")
             for svc in ("skfence", "skfenceha") for env in ENVS]


@pytest.mark.parametrize("svc,env,path", PLAYBOOKS, ids=[f"{s}-{e}" for s, e, _ in PLAYBOOKS])
def test_playbook_log_dir_matches_the_layout(svc, env, path):
    hits, _ = _logrotate_tasks(path)
    assert len(hits) == 1, path
    v = hits[0].get("vars", {})
    assert "log_path" not in v, "the old glob var is gone"
    assert v.get("log_dir") == LOG_DIR_EXPR
    assert "logrotate_node" in v
    assert "LOGROTATE_ROTATE" in v.get("retention_days", "")
    assert "*" not in str(v)


@pytest.mark.parametrize("svc,env,path", PLAYBOOKS, ids=[f"{s}-{e}" for s, e, _ in PLAYBOOKS])
def test_playbook_rotate_default_keeps_env_retention(svc, env, path):
    hits, _ = _logrotate_tasks(path)
    expr = hits[0]["vars"]["retention_days"]
    j = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    assert j.from_string(expr).render(env=env, **{svc: {}}) == ("7" if env == "prod" else "3")
    assert j.from_string(expr).render(env=env, **{svc: {"LOGROTATE_ROTATE": 30}}) == "30"


@pytest.mark.parametrize("env", ENVS)
def test_skfenceha_installs_on_every_manager_with_its_own_hostname(env):
    """traefik-worker is global over the managers: each one needs its own
    file naming its own access logs, by the hostname Swarm hands Traefik
    ({{.Node.Hostname}})."""
    hits, tasks = _logrotate_tasks(CORE / f"skfenceha/deploy_skfenceha-{env}.yml")
    t = hits[0]
    assert t.get("delegate_to"), "must run on each manager, not only the selected one"
    reg = [x for x in tasks if x.get("command") == "hostname" and x.get("register")]
    assert len(reg) == 1 and "groups[target_manager_group]" in str(reg[0].get("loop"))
    assert reg[0]["register"] in str(t.get("loop"))
    assert "stdout" in t["vars"]["logrotate_node"]


@pytest.mark.parametrize("env", ENVS)
def test_skfence_node_is_the_traefik_filepath_node(env):
    hits, _ = _logrotate_tasks(CORE / f"skfence/deploy_skfence-{env}.yml")
    assert hits[0]["vars"]["logrotate_node"] == "{{ NODE_HOSTNAME }}"
