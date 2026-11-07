"""The portal tile must find sksso's vault from core roles too.

sksso is an optional role, so its vault lives in optional/group_vars. Core
roles (skfence, skfenceha) used to look in core/group_vars and failed with
"Could not find or access .../core/group_vars/<env>/sksso-..." (v2.26.17
gate preflight). The expression is rendered with real Ansible here.
"""
from pathlib import Path

import pytest
import yaml

pytest.importorskip("ansible")
from ansible.parsing.dataloader import DataLoader  # noqa: E402
from ansible.template import Templar  # noqa: E402

try:  # ansible-core >= 2.19 only templates strings marked trusted
    from ansible.template import trust_as_template
except ImportError:  # older ansible-core templates any string
    def trust_as_template(s):
        return s

ROOT = Path(__file__).resolve().parents[1] / "ansible"
TASKS = ROOT / "shared" / "tasks" / "authentik_portal_tile.yml"


def _expr() -> str:
    tasks = yaml.safe_load(TASKS.read_text())
    for t in tasks:
        sf = t.get("set_fact", {})
        if "portal_tile_sksso_vault_file" in sf:
            return sf["portal_tile_sksso_vault_file"]
    raise AssertionError("locate task not found")


def _render(app: str, vault_file_path: str, portal_tile: dict | None = None) -> str:
    templar = Templar(
        loader=DataLoader(),
        variables={"app": app, "vault_file_path": vault_file_path, "portal_tile": portal_tile or {}},
    )
    return str(templar.template(trust_as_template(_expr()))).strip()


def test_core_role_points_at_optional_sksso_vault():
    got = _render("skfence", "/x/v1/ansible/core/group_vars/prod/skfence-prod-example-com-c1_vault.yml")
    assert got == "/x/v1/ansible/optional/group_vars/prod/sksso-prod-example-com-c1_vault.yml"


def test_optional_role_path_is_unchanged():
    got = _render("skpulse", "/x/v1/ansible/optional/group_vars/dev/skpulse-dev-example-com-c1_vault.yml")
    assert got == "/x/v1/ansible/optional/group_vars/dev/sksso-dev-example-com-c1_vault.yml"


def test_explicit_override_wins():
    got = _render(
        "skfenceha",
        "/x/v1/ansible/core/group_vars/prod/skfenceha-prod_vault.yml",
        {"authentik_vault_file": "/elsewhere/sksso.yml"},
    )
    assert got == "/elsewhere/sksso.yml"
