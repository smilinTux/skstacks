"""Files rendered at RUNTIME by a service (not by Ansible) must be shipped with
`copy`, not `template`: Ansible would try to fill the service's own per-request
placeholders and fail (skfence error-pages: 'status_code' is undefined)."""
import pathlib

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
RUNTIME_TEMPLATES = {"src/error-pages/error.html.j2"}


def _tasks(playbook):
    for play in yaml.safe_load(playbook.read_text()) or []:
        for key in ("pre_tasks", "tasks", "post_tasks"):
            yield from play.get(key) or []


def test_runtime_templates_are_copied_not_templated():
    hits = []
    for pb in ANSIBLE.glob("*/*/deploy_*.yml"):
        for task in _tasks(pb):
            src = str((task.get("template") or {}).get("src", ""))
            if any(src.endswith(r) for r in RUNTIME_TEMPLATES):
                hits.append(f"{pb.relative_to(ANSIBLE)}: {task.get('name')}")
    assert not hits, "\n".join(hits)
