"""A long-running Swarm service must never run out of restart attempts.

`deploy.restart_policy.max_attempts` is a lifetime budget per task slot, not
a crash-loop brake: once it is spent Swarm stops rescheduling the slot for
good. Combined with a single-node placement pin, one outage of that node
spends it (every restart lands on a node that is down or cannot satisfy the
constraint), and the service then sits at 0/1 until a human forces an
update. That happened twice on a production instance, each time for about
four months: skboard (vikunja, `max_attempts: 3`) and skmail's `mail-web`
(`max_attempts: 3`, pinned to one node).

So every compose/stack template, rendered for every env and every flag
combination the schema test discovers, must leave `max_attempts` unset on
every service. `0` is Swarm's "unlimited" and is not an exception either:
the key has no business in a long-running service, and a literal `0` next to
a knob is one edit away from the bug. A true one-shot job (it is meant to
exit and stay exited) may be listed in ONE_SHOT with a reason.

A raw text scan backs this up for anything the render does not see: a
`max_attempts` key or a `--restart-max-attempts` flag anywhere under
v1/ansible outside ONE_SHOT fails too.
"""
import pathlib
import re

import yaml

from test_compose_templates_schema import build_cases, discover_compose_templates, render

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
ENVS = ["dev", "staging", "prod"]

# (template dir name, compose service name) -> why a finite restart budget is
# correct for it. Only for jobs that are meant to run once and stay exited.
ONE_SHOT: dict[tuple[str, str], str] = {}

# Vault knobs that used to set max_attempts. They are gone; a playbook must
# refuse a vault that still sets one rather than silently ignore it.
RETIRED_KNOBS = {
    "skboard": "RESTART_POLICY_MAX_ATTEMPTS",
    "skdash": "restart_policy_max_attempts",
}


def _violations(template):
    out = []
    for label, ctx in build_cases(template):
        for env_name in ENVS:
            doc = yaml.safe_load(render(template, env_name, ctx)) or {}
            for name, svc in (doc.get("services") or {}).items():
                policy = ((svc or {}).get("deploy") or {}).get("restart_policy") or {}
                if "max_attempts" in policy and (template.parent.name, name) not in ONE_SHOT:
                    out.append(f"{name} ({env_name}, {label}): max_attempts={policy['max_attempts']!r}")
    return sorted(set(out))


def test_no_long_running_service_has_a_restart_budget():
    failures = {}
    for template in discover_compose_templates():
        bad = _violations(template)
        if bad:
            failures[str(template.relative_to(ANSIBLE))] = bad[:3] + ([f"... {len(bad) - 3} more"] if len(bad) > 3 else [])
    assert not failures, (
        "restart_policy.max_attempts set on a long-running service (Swarm gives up for good "
        "after a single-node outage; drop it, use condition: any):\n"
        + "\n".join(f"  {t}: {v}" for t, v in failures.items()))


def test_raw_scan_finds_no_restart_budget_anywhere():
    exempt_dirs = {svc for svc, _ in ONE_SHOT}
    pattern = re.compile(r"max_attempts\s*:|--restart-max-attempts")
    hits = []
    for f in sorted(ANSIBLE.rglob("*")):
        if not f.is_file() or f.suffix == ".md" or exempt_dirs & set(f.relative_to(ANSIBLE).parts):
            continue
        try:
            text = f.read_text()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line.split("#", 1)[0]):
                hits.append(f"{f.relative_to(ANSIBLE)}:{n}: {line.strip()}")
    assert not hits, "max_attempts outside a one-shot job:\n" + "\n".join(hits)


def test_every_one_shot_exemption_has_a_reason():
    for key, reason in ONE_SHOT.items():
        assert isinstance(reason, str) and len(reason.split()) >= 5, key


def test_long_running_services_restart_on_any_exit_where_the_budget_was():
    """The services that carried a budget restart on ANY exit, not only a
    non-zero one: when Swarm kills a task for failing its healthcheck, the
    process usually exits 0 on the SIGTERM (busybox httpd, gunicorn), and
    `on-failure` never restarts a clean exit, so the slot would sit at 0/N."""
    for rel, service in [
        ("core/sksec/src/config/sksec/sksec.yml.j2", "crowdsec"),
        ("core/sksec/src/config/sksec/sksec.yml.j2", "bouncer-traefik"),
        ("optional/skboard/src/config/skboard/skboard.yml.j2", "vikunja"),
        ("optional/skbook/src/config/skbook/skbook.yml.j2", "bookstack"),
        ("optional/skdash/src/config/skdash/skdash.yml.j2", "dashy"),
        ("optional/skform/src/config/skform/skform.yml.j2", "opentofu"),
        ("optional/skgallery/src/config/skgallery/skgallery.yml.j2", "immich-server"),
        ("optional/skgallery/src/config/skgallery/skgallery.yml.j2", "immich-machine-learning"),
        ("optional/skgallery/src/config/skgallery/skgallery.yml.j2", "redis"),
        ("optional/skgallery/src/config/skgallery/skgallery.yml.j2", "database"),
        ("optional/skgraph/src/config/skgraph/skgraph.yml.j2", "falkordb"),
        ("optional/skmail/src/config/skmail/skmail.yml.j2", "mail-web"),
        ("optional/skmail/src/config/skmail/skmail.yml.j2", "skmail"),
        ("optional/skorch/src/config/skorch/skorch.yml.j2", "postgres"),
        ("optional/skorch/src/config/skorch/skorch.yml.j2", "redis"),
        ("optional/skport/src/config/skport/skport.yml.j2", "portainer"),
        ("optional/skvector/src/config/skvector/skvector.yml.j2", "qdrant"),
    ]:
        template = ANSIBLE / rel
        ctx = build_cases(template)[0][1]
        doc = yaml.safe_load(render(template, "prod", ctx))
        policy = doc["services"][service]["deploy"]["restart_policy"]
        assert policy.get("condition") == "any", (rel, service, policy)


def test_retired_max_attempts_knobs_are_refused_by_every_env_playbook():
    for svc, knob in RETIRED_KNOBS.items():
        template = next(ANSIBLE.glob(f"*/{svc}/src/config/{svc}/{svc}.yml.j2"))
        assert knob not in template.read_text(), f"{svc} template still reads retired {knob}"
        for env_name in ENVS:
            playbook = next(ANSIBLE.glob(f"*/{svc}/deploy_{svc}-{env_name}.yml"))
            text = playbook.read_text()
            assert re.search(rf"{svc}\.{knob} is not defined", text), (
                f"{playbook.name} must fail when the vault still sets {svc}.{knob}")
