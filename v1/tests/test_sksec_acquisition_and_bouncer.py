"""sksec (CrowdSec + Traefik bouncer) as shipped could not work against the
framework's own skfenceha, found while preparing a first production deploy:

1. Log acquisition read nothing. With skfenceha the crowdsec service mounted
   `<log_dir>/acme-node` and `<log_dir>/worker-base` and acquis.yaml globbed
   `acme-node/node-*/traefik-access.log` and `worker-base/worker-*/...`, but
   skfenceha's entrypoint.sh writes `<log_dir>/{worker,acme}/access-<node>.log`
   (the same names its logrotate.conf.j2 lists). The playbook even created the
   empty `acme-node`/`worker-base` dirs, so the mounts succeeded and CrowdSec
   silently tailed nothing.
2. The acquis label was `type: traefik-v3`, meant for a custom parser the
   playbook never deployed. It now uses `type: traefik`, which the stock
   `crowdsecurity/traefik-logs` parser (installed by the traefik collection)
   accepts, JSON format included.
3. The logs live on the shared filesystem and every Traefik node writes its
   own file, so CrowdSec on one node never gets an inotify event for another
   node's writes: acquisition must poll (`poll_without_inotify: true`).
4. The bouncer key never reached anything when the vault left
   `CROWDSEC_BOUNCER_API_KEY` unset: the playbook generated and persisted one
   (`computed_bouncer_key`), but sksec.env rendered the empty vault value.
5. The bouncer was registered by a `cscli bouncers add --key <key>` task that
   ran BEFORE the stack deploy (so on a first install it found no container
   and skipped), only looked on the deploy node, and put the key on a process
   argv. CrowdSec's image registers bouncers from `BOUNCER_KEY_<name>` env
   vars at start, which fixes all three.
6. With `TRAEFIK_HA_MODE` the sksec playbook removed `traefik.acme.master`
   from every manager and re-added it on its own deploy node, which moves
   skfenceha's ACME master (and Swarm stops the constrained skfenceha tasks
   in between). skfenceha owns that label; sksec now only reads it.

New: `sksec.ALLOWLIST_CIDRS` renders a CrowdSec whitelist parser so the
operator's own ranges never become alerts or bans, and `sksec.CROWDSEC_IMAGE`
/ `sksec.BOUNCER_IMAGE` let an instance pin digests.
"""
import fnmatch
import pathlib
import re

import jinja2
import pytest
import yaml

SVC = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/sksec"
SKFENCEHA = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha"
COMPOSE = SVC / "src/config/sksec/sksec.yml.j2"
ENV = SVC / "src/config/sksec/sksec.env.j2"
ACQUIS = SVC / "src/sksec/crowdsec/config/acquis.yaml.j2"
ALLOWLIST = SVC / "src/sksec/crowdsec/config/parsers/s02-enrich/sksec-allowlist.yaml.j2"
PLAYBOOKS = [SVC / f"deploy_sksec-{e}.yml" for e in ("dev", "staging", "prod")]

BASE = {
    "APP_ENV": "prod",
    "GID": "1000",
    "COLLECTIONS": "crowdsecurity/traefik",
    "LOG_LEVEL": "info",
    "LOG_FORMAT": "json",
    "CROWDSEC_CPU_LIMIT": "0.5",
    "CROWDSEC_MEMORY_LIMIT": "512M",
    "CROWDSEC_CPU_RESERVATION": "0.1",
    "CROWDSEC_MEMORY_RESERVATION": "128M",
    "BOUNCER_CPU_LIMIT": "0.25",
    "BOUNCER_MEMORY_LIMIT": "256M",
    "BOUNCER_CPU_RESERVATION": "0.05",
    "BOUNCER_MEMORY_RESERVATION": "64M",
    "HEALTH_CHECK_INTERVAL": "30s",
    "HEALTH_CHECK_TIMEOUT": "5s",
    "HEALTH_CHECK_RETRIES": "3",
    "HEALTH_CHECK_START_PERIOD": "10s",
    "LOG_MAX_SIZE": "10m",
    "LOG_MAX_FILES": "3",
}


def _render(path, traefik="skfenceha", env="prod", **overrides):
    sksec = dict(BASE, **overrides)
    j = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, lstrip_blocks=False)
    j.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "on", "1")
    return j.from_string(path.read_text()).render(
        env=env,
        app="sksec",
        sksec=sksec,
        traefik_service_name=traefik,
        domain="example.test",
        cluster_name="example",
        computed_bouncer_key="COMPUTED-KEY-SENTINEL",
    )


def _crowdsec(traefik, **o):
    return yaml.safe_load(_render(COMPOSE, traefik, **o))["services"]["crowdsec"]


def _mounts(svc):
    out = {}
    for v in svc["volumes"]:
        src, dst = v.split(":")[:2]
        out[dst] = src
    return out


def _host_path(container_path, mounts):
    for dst, src in sorted(mounts.items(), key=lambda kv: -len(kv[0])):
        if container_path == dst or container_path.startswith(dst.rstrip("/") + "/"):
            return src + container_path[len(dst.rstrip("/")):]
    return None


def _glob_match(pattern, path):
    """Path-segment-aware glob ('*' never crosses '/'), as CrowdSec's
    filepath.Glob does."""
    p, s = pattern.split("/"), path.split("/")
    return len(p) == len(s) and all(fnmatch.fnmatchcase(b, a) for a, b in zip(p, s))


def _acquis(traefik, **o):
    return yaml.safe_load(_render(ACQUIS, traefik, **o))


# The files the Traefik side actually writes, per the framework's own
# templates (entrypoint.sh / traefik.yml.j2 filePath and logrotate.conf.j2).
WRITTEN = {
    "skfenceha": [
        "/var/data/logs/skfenceha-prod/traefik/worker/access-node1.log",
        "/var/data/logs/skfenceha-prod/traefik/worker/access-node2.log",
        "/var/data/logs/skfenceha-prod/traefik/acme/access-node1.log",
    ],
    "skfence": ["/var/data/logs/skfence-prod/traefik/node1/access.log"],
}


def test_skfenceha_writes_the_layout_this_test_assumes():
    ep = (SKFENCEHA / "src/config/skfenceha/entrypoint.sh").read_text()
    assert 'ACCESS_LOG="${LOG_DIR}/access-${NODE_HOSTNAME}.log"' in ep
    lr = (SKFENCEHA / "src/config/skfenceha/logrotate.conf.j2").read_text()
    assert "{{ log_dir }}/worker/access-{{ logrotate_node }}.log" in lr
    assert "{{ log_dir }}/acme/access-{{ logrotate_node }}.log" in lr


@pytest.mark.parametrize("traefik", ["skfenceha", "skfence"])
def test_every_traefik_access_log_is_acquired(traefik):
    mounts = _mounts(_crowdsec(traefik))
    globs = [g for g in _acquis(traefik)["filenames"]]
    host_globs = [_host_path(g, mounts) for g in globs]
    assert None not in host_globs, f"acquis glob outside every mount: {globs} vs {mounts}"
    for f in WRITTEN[traefik]:
        assert any(_glob_match(g, f) for g in host_globs), f"{f} not acquired by {host_globs}"


def test_ha_mode_flag_alone_uses_the_skfenceha_layout():
    mounts = _mounts(_crowdsec("skfence", TRAEFIK_HA_MODE=True))
    assert any(src.endswith("/traefik") for src in mounts.values())


@pytest.mark.parametrize("traefik", ["skfenceha", "skfence"])
def test_log_mounts_are_read_only(traefik):
    for v in _crowdsec(traefik)["volumes"]:
        if v.split(":")[1].startswith("/var/log/traefik"):
            assert v.endswith(":ro"), v


@pytest.mark.parametrize("traefik", ["skfenceha", "skfence"])
def test_acquisition_polls_because_logs_are_on_a_shared_filesystem(traefik):
    assert _acquis(traefik)["poll_without_inotify"] is True


@pytest.mark.parametrize("traefik", ["skfenceha", "skfence"])
def test_acquis_label_matches_the_stock_traefik_parser(traefik):
    # crowdsecurity/traefik-logs: filter "evt.Parsed.program startsWith 'traefik'"
    assert _acquis(traefik)["labels"]["type"] == "traefik"


def test_undeployed_custom_parser_is_gone():
    assert not list(SVC.rglob("traefik-v3-logs.yaml*"))


def test_playbooks_do_not_create_the_dead_log_dirs():
    for pb in PLAYBOOKS:
        text = pb.read_text()
        assert "acme-node" not in text and "worker-base" not in text, pb.name


def _env_lines(**o):
    return dict(
        line.split("=", 1)
        for line in _render(ENV, **o).splitlines()
        if line and not line.startswith("#") and "=" in line
    )


def test_env_carries_the_computed_key_when_vault_leaves_it_unset():
    env = _env_lines()
    assert env["CROWDSEC_BOUNCER_API_KEY"] == "COMPUTED-KEY-SENTINEL"


def test_env_registers_the_bouncer_with_crowdsec_via_bouncer_key_env():
    # docker_start.sh: for BOUNCER in $(compgen -A variable | grep -i BOUNCER_KEY)
    # -> NAME = cut -d_ -f3-, so the name must be a valid shell identifier tail.
    env = _env_lines()
    keys = [k for k in env if re.search("BOUNCER_KEY", k, re.I)]
    assert keys == ["BOUNCER_KEY_traefik"], keys
    assert env["BOUNCER_KEY_traefik"] == "COMPUTED-KEY-SENTINEL"


def _tasks(pb):
    out = []

    def walk(ts):
        for t in ts or []:
            out.append(t)
            for k in ("block", "rescue", "always"):
                walk(t.get(k))

    for play in yaml.safe_load(pb.read_text()):
        for k in ("pre_tasks", "tasks", "post_tasks"):
            walk(play.get(k))
    return out


def _shell(t):
    for k in ("shell", "command", "ansible.builtin.shell", "ansible.builtin.command"):
        if k in t:
            v = t[k]
            return v if isinstance(v, str) else str(v)
    return ""


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_bouncer_key_never_on_a_command_line(pb):
    for t in _tasks(pb):
        assert "bouncer_key" not in _shell(t).lower() or "openssl rand" in _shell(t), t.get("name")


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_sksec_never_moves_the_skfenceha_acme_label(pb):
    for t in _tasks(pb):
        assert not re.search(r"docker node update[^\n]*--label-(rm|add)", _shell(t)), t.get("name")


@pytest.mark.parametrize("pb", PLAYBOOKS, ids=lambda p: p.name)
def test_allowlist_parser_is_deployed_into_the_runtime_config(pb):
    tasks = _tasks(pb)
    rendered = [
        t for t in tasks
        if "sksec-allowlist.yaml.j2" in str(t.get("ansible.builtin.template", t.get("template", "")))
    ]
    assert rendered, "no task renders the allowlist parser"
    dest = str(rendered[0].get("ansible.builtin.template", rendered[0].get("template"))["dest"])
    assert dest.endswith("/crowdsec-config/parsers/s02-enrich/sksec-allowlist.yaml"), dest
    assert "runtime" in dest
    absent = [
        t for t in tasks
        if "sksec-allowlist.yaml" in str(t.get("ansible.builtin.file", t.get("file", "")))
        and "absent" in str(t.get("ansible.builtin.file", t.get("file", "")))
    ]
    assert absent, "an emptied ALLOWLIST_CIDRS must remove the parser"


def test_allowlist_renders_cidrs_and_ips():
    doc = yaml.safe_load(_render(ALLOWLIST, ALLOWLIST_CIDRS=["192.0.2.0/24", "198.51.100.7", "2001:db8::/32"]))
    assert doc["name"] == "sksec/allowlist"
    wl = doc["whitelist"]
    assert set(wl["cidr"]) == {"192.0.2.0/24", "2001:db8::/32"}
    assert wl["ip"] == ["198.51.100.7"]
    assert wl["reason"]


def test_images_default_pinned_and_overridable():
    svc = yaml.safe_load(_render(COMPOSE))["services"]
    assert svc["crowdsec"]["image"] == "crowdsecurity/crowdsec:v1.6.11"
    assert svc["bouncer-traefik"]["image"] == "docker.io/fbonalair/traefik-crowdsec-bouncer:0.5.0"
    svc = yaml.safe_load(_render(
        COMPOSE,
        CROWDSEC_IMAGE="crowdsecurity/crowdsec:v1.6.11@sha256:" + "a" * 64,
        BOUNCER_IMAGE="docker.io/fbonalair/traefik-crowdsec-bouncer:0.5.0@sha256:" + "b" * 64,
    ))["services"]
    assert svc["crowdsec"]["image"].endswith("a" * 64)
    assert svc["bouncer-traefik"]["image"].endswith("b" * 64)
