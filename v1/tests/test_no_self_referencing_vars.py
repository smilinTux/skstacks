"""`vars: {x: "{{ x }}"}` recurses forever in Ansible (select_vault_file.yml
warns about it). No published playbook may contain it."""
import pathlib
import re

import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SELF = re.compile(r"^\{\{\s*(\w+)\s*\}\}$")


def _walk(node, path, hits):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == "vars" and isinstance(v, dict):
                for name, val in v.items():
                    m = SELF.match(str(val).strip())
                    if m and m.group(1) == name:
                        hits.append(f"{path}: vars.{name} = {val}")
            _walk(v, path, hits)
    elif isinstance(node, list):
        for item in node:
            _walk(item, path, hits)


def test_no_playbook_sets_a_var_to_itself():
    hits = []
    for p in ANSIBLE.rglob("*.yml"):
        _walk(yaml.safe_load(p.read_text()), p.relative_to(ANSIBLE), hits)
    assert not hits, "\n".join(hits)
