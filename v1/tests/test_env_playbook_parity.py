"""A service's dev and staging playbooks must run the same tasks as prod.
Three skstack06 failures in a row came from dev-only drift that prod never
exercised; this keeps the env playbooks honest.

Comparing task *names* alone (the original check below) missed skvector's
(and, it turns out, skgraph's) dev/staging drift: the "Define network
variables from vault" task kept the same name in all three env files, but
its body had drifted - dev/staging referenced a different override variable
(`skvector_dev_networks` / `skvector_staging_networks` instead of prod's
`skvector.networks`) and their network dicts were missing the
`driver`/`attachable` keys prod's had, plus dev/staging were missing the
`config` tag prod carried. Same name, silently different behavior.

A blanket "every task must be byte-identical after normalizing env tokens"
check is too broad: several services deliberately vary other tasks across
envs (skfenceha's log retention_days, skpdf's SYSTEM_SHOWUPDATE default,
skgallery's human-readable "DEVELOPMENT/STAGING/PRODUCTION" banner, skorch's
prod-has-no-subdomain-suffix N8N_HOST). Those are real, intended
differences, not drift.

test_network_fact_tasks_structurally_match_prod_across_envs is scoped
instead to just the task(s) that `set_fact` a `*network*` var - the actual
class of bug this test exists to catch - after normalizing away the
per-env differences those tasks ARE expected to carry (the literal env
token, and CIDR subnet literals). Anything that still differs after that
normalization is real, unintended drift: an override variable name, a
missing dict key, or a missing tag.
"""
import pathlib
import re

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"

_ENV_TOKEN_RE = re.compile(r"(?<![A-Za-z])(dev|staging|prod)(?![A-Za-z])")
_CIDR_RE = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}/\d{1,2}")


def _task_names(path):
    names = []
    for play in yaml.safe_load(path.read_text()):
        for section in ("pre_tasks", "tasks"):
            for task in play.get(section) or []:
                names.append(task.get("name") or next(iter(task)))
    return names


def test_dev_and_staging_run_the_same_tasks_as_prod():
    problems = []
    for prod in ANSIBLE.glob("*/*/deploy_*-prod.yml"):
        want = _task_names(prod)
        for env in ("dev", "staging"):
            other = prod.with_name(prod.name.replace("-prod.yml", f"-{env}.yml"))
            if other.exists() and _task_names(other) != want:
                problems.append(f"{other.relative_to(ANSIBLE)} differs from {prod.name}")
    assert not problems, "\n".join(problems)


def _tasks(path):
    tasks = []
    for play in yaml.safe_load(path.read_text()):
        for section in ("pre_tasks", "tasks"):
            tasks.extend(play.get(section) or [])
    return tasks


def _network_fact_tasks(path):
    """The `set_fact` task(s) that define a network list from a vault
    override (almost always `app_networks`), for direct dev/staging/prod
    comparison."""
    tasks = []
    for task in _tasks(path):
        fact = task.get("set_fact") or {}
        if any("network" in str(key).lower() for key in fact):
            tasks.append(task)
    return tasks


def _normalize(obj):
    """Strip the per-env differences a network-defining task is expected to
    carry (the env name itself, and subnet CIDR literals) so what's left can
    be compared directly across dev/staging/prod."""
    if isinstance(obj, dict):
        return {k: _normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, str):
        s = _CIDR_RE.sub("CIDR", obj)
        s = _ENV_TOKEN_RE.sub("ENV", s)
        return s
    return obj


def test_network_fact_tasks_exist_for_at_least_one_service():
    found = [p for p in ANSIBLE.glob("*/*/deploy_*-prod.yml") if _network_fact_tasks(p)]
    assert found, "expected at least one deploy_*-prod.yml with a network-defining set_fact task"


def test_network_fact_tasks_structurally_match_prod_across_envs():
    problems = []
    for prod in ANSIBLE.glob("*/*/deploy_*-prod.yml"):
        prod_net_tasks = [_normalize(t) for t in _network_fact_tasks(prod)]
        if not prod_net_tasks:
            continue
        for env in ("dev", "staging"):
            other = prod.with_name(prod.name.replace("-prod.yml", f"-{env}.yml"))
            if not other.exists():
                continue
            other_net_tasks = [_normalize(t) for t in _network_fact_tasks(other)]
            if other_net_tasks != prod_net_tasks:
                problems.append(
                    f"{other.relative_to(ANSIBLE)}: network-defining task(s) differ from "
                    f"{prod.name} after normalizing env tokens/CIDRs\n"
                    f"  {env}:  {other_net_tasks}\n"
                    f"  prod:  {prod_net_tasks}"
                )
    assert not problems, "\n\n".join(problems)


def test_normalize_treats_env_tokens_and_cidrs_as_equivalent():
    a = {"name": "x", "set_fact": {"app_networks": "skvector-dev 172.16.100.0/24"}}
    b = {"name": "x", "set_fact": {"app_networks": "skvector-prod 172.16.98.0/24"}}
    assert _normalize(a) == _normalize(b)


def test_normalize_treats_underscore_embedded_env_tokens_as_equivalent():
    """The suffixed-override convention (`skport_dev_networks`,
    `skport_staging_networks`, `skport_prod_networks`) is itself consistent
    across a service's three env files and must not be flagged - only a
    service that MIXES the suffixed convention with the bare
    `<svc>.networks` convention between its own env files is real drift."""
    a = "skport_dev_networks"
    b = "skport_staging_networks"
    c = "skport_prod_networks"
    assert _normalize(a) == _normalize(b) == _normalize(c)


def test_normalize_still_catches_a_different_override_var_convention():
    a = {"set_fact": {"app_networks": "{{ skvector_dev_networks | default([]) }}"}}
    b = {"set_fact": {"app_networks": "{{ skvector.networks | default([]) }}"}}
    assert _normalize(a) != _normalize(b)


def test_normalize_still_catches_a_missing_dict_key():
    a = [{"name": "skvector-dev", "subnet": "172.16.100.0/24"}]
    b = [{"name": "skvector-prod", "subnet": "172.16.98.0/24", "driver": "overlay", "attachable": True}]
    assert _normalize(a) != _normalize(b)


def test_normalize_still_catches_a_missing_tag():
    a = {"tags": ["networks"]}
    b = {"tags": ["networks", "config"]}
    assert _normalize(a) != _normalize(b)
