"""A service's dev and staging playbooks must run the same tasks as prod.
Three skstack06 failures in a row came from dev-only drift that prod never
exercised; this keeps the env playbooks honest."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"


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
