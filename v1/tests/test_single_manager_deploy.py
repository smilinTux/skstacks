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


def _is_per_host(text):
    return PER_HOST_MARKER in text


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
        if not _is_per_host(text):
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
