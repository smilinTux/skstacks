"""Every free-form shell/command task body must survive Ansible's own
argument splitter. A lone apostrophe in a comment (Forgejo's) made
`split_args` raise "unbalanced jinja2 block or quotes" and every skgit
playbook failed to LOAD (dev, staging and prod), yet the render gate and the
unit tests passed: they read the YAML, never Ansible's task parser."""
import pathlib

import pytest
import yaml
from ansible.parsing.splitter import split_args

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
MODULES = {"shell", "command", "ansible.builtin.shell", "ansible.builtin.command"}


def _tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from _tasks(item)
    elif isinstance(node, dict):
        if any(m in node for m in MODULES):
            yield node
        for key in ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always"):
            if key in node:
                yield from _tasks(node[key])


def _bodies():
    files = sorted(ANSIBLE.rglob("deploy_*.yml")) + sorted(ANSIBLE.rglob("tasks/*.yml"))
    for path in files:
        try:
            doc = yaml.safe_load(path.read_text())
        except yaml.YAMLError:
            continue
        for task in _tasks(doc):
            mod = next(m for m in MODULES if m in task)
            if isinstance(task[mod], str):
                yield pytest.param(task[mod], id=f"{path.relative_to(ANSIBLE)}:{task.get('name', mod)}")


@pytest.mark.parametrize("body", list(_bodies()))
def test_free_form_body_splits(body):
    split_args(body)
