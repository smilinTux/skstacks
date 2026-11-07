"""No task may shell out to a hardcoded controller venv (coord card b38ffa32).

sksec-web ran its config patcher with ~/.skenv/bin/python3, so any
controller without that venv (or without ruamel.yaml in it) failed the
deploy with ModuleNotFoundError. Controller-side scripts run with
{{ ansible_playbook_python }}, and their packages are listed in
v1/ansible/requirements-controller.txt.
"""
import pathlib
import re

import pytest

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
VENV = re.compile(r"(~|\$HOME|/home/[^/\s]+)/\.skenv/bin/python")


def _task_files():
    return sorted(p for p in ANSIBLE.rglob("*.yml") if "/src/" not in p.as_posix())


def _code_lines(path):
    for n, line in enumerate(path.read_text(errors="replace").splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield n, line


def test_no_task_runs_a_hardcoded_skenv_python():
    hits = [f"{p.relative_to(ANSIBLE)}:{n}: {l.strip()}"
            for p in _task_files() for n, l in _code_lines(p) if VENV.search(l)]
    assert hits == []


def test_sksec_web_patcher_runs_with_ansible_playbook_python():
    text = (ANSIBLE / "optional/sksec-web/tasks/patch_app_config_yaml.yml").read_text()
    assert '"{{ ansible_playbook_python }}" "{{ playbook_dir }}/tasks/patch_sksw_config_yaml.py"' in text


def test_ruamel_is_a_pinned_controller_requirement():
    reqs = (ANSIBLE / "requirements-controller.txt").read_text().splitlines()
    assert any(re.fullmatch(r"ruamel\.yaml==\d+\.\d+\.\d+", r.strip()) for r in reqs)


@pytest.mark.parametrize("line", ["shell: ~/.skenv/bin/python3 x.py",
                                  "cmd: $HOME/.skenv/bin/python x.py",
                                  "shell: /home/someone/.skenv/bin/python3 -c 1"])
def test_pattern_catches_every_spelling(line):
    assert VENV.search(line)
