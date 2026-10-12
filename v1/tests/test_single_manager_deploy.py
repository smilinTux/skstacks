"""/var/data is shared by every node, so a deploy must run on ONE manager.
Every v1 deploy playbook selects one via select_manager_node.yml and does its
work in a play on `selected_manager_group`; a play aimed at a whole manager
group races itself on the shared storage (skstack06 caught skreg doing it).

A narrow, documented exception exists for host-level `core/` services whose
state is per-node identity rather than shared storage - keepalived VRRP
config (priority, state, unicast peers, interface) legitimately differs on
every manager and must be applied to all of them, not one selected manager.
Such a playbook opts out with a `# skstacks: per-host` marker that carries
its reason; see test_per_host_exemption_is_documented_and_scoped_to_core
below for what keeps that marker from becoming a way to dodge the rule
above."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
PER_HOST_MARKER = "# skstacks: per-host"
# A host-level optional app that runs on the storage host serving /var/data
# (skbackup), not on the swarm: no manager, no shared-storage race.
STORAGE_HOST_MARKER = "# skstacks: storage-host"
# A host-level agent installed on every node (skhealth): node-local files
# only (/etc, /usr/local, /var/lib), never /var/data, never the swarm.
EVERY_NODE_MARKER = "# skstacks: every-node"


def _is_per_host(text):
    return PER_HOST_MARKER in text or STORAGE_HOST_MARKER in text or EVERY_NODE_MARKER in text


def test_deploy_playbooks_run_on_one_selected_manager():
    problems = []
    for p in sorted(ANSIBLE.glob("*/*/deploy_*.yml")):
        text = p.read_text()
        if _is_per_host(text):
            continue
        plays = yaml.safe_load(text)
        work = [pl for pl in plays if isinstance(pl, dict) and (pl.get("tasks") or pl.get("pre_tasks"))
                and pl.get("hosts") not in ("localhost", "all")]
        if "select_manager_node.yml" not in text:
            problems.append(f"{p.relative_to(ANSIBLE)}: does not select a manager")
        for pl in work:
            if pl.get("hosts") != "selected_manager_group":
                problems.append(f"{p.relative_to(ANSIBLE)}: play on {pl.get('hosts')!r}")
    assert not problems, "\n".join(problems)


def test_per_host_exemption_is_documented_and_scoped_to_core():
    """The `# skstacks: per-host` marker must (1) only appear under `core/`,
    where a host-level service like skha lives - `optional/` services are
    Swarm stacks writing to shared `/var/data`, exactly the race this rule
    guards against, so the marker never belongs there - and (2) carry an
    actual reason on the same line, not sit bare."""
    problems = []
    for p in sorted(ANSIBLE.glob("*/*/deploy_*.yml")):
        text = p.read_text()
        if PER_HOST_MARKER not in text:
            continue
        rel = p.relative_to(ANSIBLE)
        if rel.parts[0] != "core":
            problems.append(f"{rel}: per-host exemption is only for host-level core/ services")
        reasons = [
            line.split(PER_HOST_MARKER, 1)[1].strip(" -")
            for line in text.splitlines()
            if PER_HOST_MARKER in line
        ]
        if not any(len(r) >= 10 for r in reasons):
            problems.append(f"{rel}: per-host marker has no documented reason")
    assert not problems, "\n".join(problems)


def test_storage_host_exemption_is_documented_and_never_touches_the_swarm():
    """`# skstacks: storage-host` must carry a reason, and such a playbook
    must not aim a play at a manager group or drive Docker: it exists for
    apps that run beside the data, on the storage host."""
    problems = []
    for p in sorted(ANSIBLE.glob("*/*/deploy_*.yml")):
        text = p.read_text()
        if STORAGE_HOST_MARKER not in text:
            continue
        rel = p.relative_to(ANSIBLE)
        reasons = [line.split(STORAGE_HOST_MARKER, 1)[1].strip(" -")
                   for line in text.splitlines() if STORAGE_HOST_MARKER in line]
        if not any(reasons):
            problems.append(f"{rel}: storage-host marker without a reason")
        for pl in yaml.safe_load(text):
            hosts = str(pl.get("hosts", "")) if isinstance(pl, dict) else ""
            if "manager" in hosts:
                problems.append(f"{rel}: play on {hosts!r}")
        if "docker stack" in text or "docker_swarm" in text:
            problems.append(f"{rel}: drives the swarm")
    assert not problems, "\n".join(problems)


def test_every_node_exemption_is_documented_and_never_touches_shared_storage():
    """`# skstacks: every-node` must carry a reason, and such a playbook must
    stay node-local: it may not write under /var/data (the shared storage
    the one-manager rule protects) nor drive the swarm, so running it on
    every node at once cannot race."""
    problems = []
    found = 0
    for p in sorted(ANSIBLE.glob("*/*/deploy_*.yml")):
        text = p.read_text()
        if EVERY_NODE_MARKER not in text:
            continue
        found += 1
        rel = p.relative_to(ANSIBLE)
        reasons = [line.split(EVERY_NODE_MARKER, 1)[1].strip(" -")
                   for line in text.splitlines() if EVERY_NODE_MARKER in line]
        if not any(len(r) >= 10 for r in reasons):
            problems.append(f"{rel}: every-node marker without a reason")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        if "/var/data" in body:
            problems.append(f"{rel}: an every-node play touches /var/data")
        if "docker stack" in body or "docker service" in body or "docker_swarm" in body:
            problems.append(f"{rel}: drives the swarm")
    assert found >= 3, "skhealth's dev/staging/prod playbooks carry the marker"
    assert not problems, "\n".join(problems)
