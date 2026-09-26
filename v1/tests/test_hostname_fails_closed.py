"""A traefik Host() rule that resolves CLUSTERNAME/DOMAIN to a literal
placeholder when neither the instance override nor the inventory host var
is set renders a real, routable-looking hostname (e.g.
`skpeek.cluster1.example.com`) instead of failing the deploy. That is worse
than an obvious error: the stack comes up, Traefik registers the router, and
nothing points at it in DNS, so the failure only surfaces later as "why
can't anyone reach this" instead of at deploy time.

Every published service must fail closed instead: with no instance
override and no inventory fallback, rendering its router Host rule must
either raise (the value chain bottoms out on a genuinely undefined variable,
matching Ansible's default error_on_undefined_vars=True behavior) or the
service's env playbooks must assert the vars are set before deploying.
Falling back to the inventory host vars (cluster_name/domain - e.g. NAM's
sksec) is fine and must keep working; only the literal default is not.
"""
import pathlib
import re

import jinja2
import pytest

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"

_HOST_RULE_RE = re.compile(r"rule=Host\(.*(?:CLUSTERNAME|cluster_name).*(?:DOMAIN|domain)")


def _iter_host_rule_lines():
    """Yield (path, lineno, line, service) for every router Host rule that
    resolves through CLUSTERNAME/DOMAIN (directly or via cluster_name/domain)."""
    for path in sorted(ANSIBLE.rglob("*.j2")):
        parts = path.relative_to(ANSIBLE).parts
        if parts[0] not in ("core", "optional") or len(parts) < 2:
            continue
        service = parts[1]
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            if _HOST_RULE_RE.search(line):
                yield path, lineno, line.strip(), service, parts[0]


HOST_RULES = list(_iter_host_rule_lines())


def _has_assert_task_for_hostname_vars(service, base):
    """True if every env playbook (deploy_<service>-*.yml) for this service
    has an assert task that mentions CLUSTERNAME/DOMAIN or cluster_name/domain."""
    playbooks = sorted((ANSIBLE / base / service).glob(f"deploy_{service}-*.yml"))
    if not playbooks:
        return False
    for pb in playbooks:
        text = pb.read_text(errors="replace")
        if "assert:" not in text:
            return False
        if not re.search(r"CLUSTERNAME|cluster_name|DOMAIN|domain", text):
            return False
    return True


@pytest.mark.parametrize(
    "path,lineno,line,service,base",
    HOST_RULES,
    ids=[f"{p.relative_to(ANSIBLE)}:{n}" for p, n, _, _, _ in HOST_RULES],
)
def test_host_rule_fails_closed_with_no_override_and_no_inventory_fallback(path, lineno, line, service, base):
    assert HOST_RULES, "expected to find at least one CLUSTERNAME/DOMAIN router Host rule"
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    try:
        env.from_string(line).render(env="prod", **{service: {}})
    except jinja2.exceptions.UndefinedError:
        return  # fails closed: raises rather than falling back to a placeholder
    assert _has_assert_task_for_hostname_vars(service, base), (
        f"{path.relative_to(ANSIBLE)}:{lineno} renders a Host rule with neither "
        f"{service}.CLUSTERNAME/DOMAIN nor cluster_name/domain set, and doesn't "
        f"raise - and none of its env playbooks assert those vars are set "
        f"before deploying:\n{line}"
    )
