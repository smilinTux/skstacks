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


# --- skdash / skgallery: CLUSTERNAME/DOMAIN must fall back to inventory vars ---
# PR #40 aligned most services on `<svc>.KEY | default(cluster_name)` /
# `<svc>.KEY | default(domain)` so a deploy with only the inventory host vars
# set (no per-service override) still resolves a real hostname instead of
# depending on a hard failure. skdash and skgallery were left out of that
# pass: every templated use of their CLUSTERNAME/DOMAIN values referenced the
# service dict directly, with no fallback to cluster_name/domain.

_FALLBACK_TARGETS = {
    "optional/skdash/src/config/skdash/skdash.yml.j2",
    "optional/skdash/src/config/skdash/skdash.env.j2",
    "optional/skdash/src/config/skdash/skdash.sh.env.j2",
    "optional/skdash/src/config/skdash/config.yml.j2",
    "optional/skgallery/src/config/skgallery/skgallery.sh.env.j2",
    "optional/skgallery/src/config/skgallery/skgallery.env.j2",
    "optional/skgallery/src/config/skgallery/skgallery.yml.j2",
    "optional/skgallery/src/skgallery/deploy.j2",
}


def _iter_unguarded_cluster_domain_refs():
    """Yield (relpath, lineno, line, service) for every non-comment line in
    the target files that reads `<service>.CLUSTERNAME` or `<service>.DOMAIN`
    without a `| default(cluster_name)` / `| default(domain)` fallback."""
    for relpath in sorted(_FALLBACK_TARGETS):
        path = ANSIBLE / relpath
        service = pathlib.Path(relpath).parts[1]
        unguarded_cluster = re.compile(rf"{service}\.CLUSTERNAME(?!\s*\|\s*default\(cluster_name\))")
        unguarded_domain = re.compile(rf"{service}\.DOMAIN(?!\s*\|\s*default\(domain\))")
        for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # informational comment headers are exempt repo-wide
            if unguarded_cluster.search(line) or unguarded_domain.search(line):
                yield relpath, lineno, line.strip(), service


UNGUARDED_CLUSTER_DOMAIN_REFS = list(_iter_unguarded_cluster_domain_refs())


@pytest.mark.parametrize(
    "relpath,lineno,line,service",
    UNGUARDED_CLUSTER_DOMAIN_REFS,
    ids=[f"{r}:{n}" for r, n, _, _ in UNGUARDED_CLUSTER_DOMAIN_REFS],
)
def test_skdash_and_skgallery_cluster_domain_use_inventory_fallback(relpath, lineno, line, service):
    pytest.fail(
        f"{relpath}:{lineno} reads {service}.CLUSTERNAME/DOMAIN without a "
        f"`| default(cluster_name)` / `| default(domain)` fallback, unlike the "
        f"other services aligned in PR #40:\n{line}"
    )


@pytest.mark.parametrize("relpath", sorted(_FALLBACK_TARGETS), ids=sorted(_FALLBACK_TARGETS))
def test_skdash_and_skgallery_fallback_lines_actually_render_from_inventory_vars(relpath):
    """A `| default(cluster_name)` fallback is only real if the line still
    renders when the service override is absent and only the inventory host
    vars are supplied - otherwise it's decorative and the deploy still fails
    at the same undefined variable it claims to guard against."""
    path = ANSIBLE / relpath
    service = pathlib.Path(relpath).parts[1]
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    context = {
        "env": "prod",
        "app": service,
        "cluster_name": "cluster1",
        "domain": "example.com",
        "item": {"url": "https://example.com"},
        service: {},
    }
    for lineno, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if f"{service}.CLUSTERNAME" not in line and f"{service}.DOMAIN" not in line:
            continue
        if line.strip().startswith("#"):
            continue
        try:
            env.from_string(line).render(**context)
        except jinja2.exceptions.UndefinedError as exc:
            pytest.fail(
                f"{relpath}:{lineno} still raises with only cluster_name/domain "
                f"set (no {service} override):\n{line}\n{exc}"
            )
