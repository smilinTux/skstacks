"""
Guard tests for the RKE2 ansible roles.

The headline guard: every `notify:` resolves to a real handler in the same role
(the rke2-server role previously notified a handler that didn't exist).
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_ANSIBLE = Path(__file__).resolve().parents[1]
_ROLES = _ANSIBLE / "roles"
_ROLE_NAMES = ["rke2-common", "rke2-server", "rke2-agent"]


def _notifies(role: Path) -> set[str]:
    out: set[str] = set()
    for tf in (role / "tasks").glob("*.yml"):
        for task in yaml.safe_load(tf.read_text()) or []:
            n = task.get("notify") if isinstance(task, dict) else None
            if isinstance(n, str):
                out.add(n)
            elif isinstance(n, list):
                out.update(n)
    return out


def _handler_names(role: Path) -> set[str]:
    hf = role / "handlers" / "main.yml"
    if not hf.exists():
        return set()
    return {h["name"] for h in (yaml.safe_load(hf.read_text()) or []) if isinstance(h, dict) and "name" in h}


@pytest.mark.parametrize("role", _ROLE_NAMES)
def test_every_notify_has_a_handler(role):
    rp = _ROLES / role
    missing = _notifies(rp) - _handler_names(rp)
    assert not missing, f"{role}: notify(s) with no matching handler: {missing}"


def test_agent_role_complete():
    agent = _ROLES / "rke2-agent"
    assert (agent / "tasks" / "main.yml").is_file()
    assert (agent / "handlers" / "main.yml").is_file()
    assert (agent / "templates" / "config.yaml.j2").is_file()


def test_server_role_has_molecule_scenario():
    mol = _ROLES / "rke2-server" / "molecule" / "default"
    for f in ("molecule.yml", "converge.yml", "verify.yml", "prepare.yml"):
        assert (mol / f).is_file(), f"rke2-server molecule missing {f}"


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="ansible-playbook not installed")
@pytest.mark.parametrize("pb", ["install-rke2-server.yml", "install-rke2-agent.yml"])
def test_playbook_syntax(pb):
    r = subprocess.run(
        ["ansible-playbook", "--syntax-check", str(_ANSIBLE / pb)],
        capture_output=True, text=True, cwd=str(_ANSIBLE),
    )
    out = (r.stderr or "") + (r.stdout or "")
    if "couldn't resolve module" in out or "ansible_collections" in out:
        pytest.skip("ansible galaxy collections not installed (run: ansible-galaxy install -r requirements.yml)")
    assert r.returncode == 0, out
