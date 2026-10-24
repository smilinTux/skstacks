"""Lint: every role that exposes a web UI through Traefik must either wire
the shared Authentik portal-tile mechanism (or an equivalent role-owned
one, like optional/skrac and optional/skmesh's own provider-provisioning
tasks), or carry an explicit `# portal-tile: opt-out (...)` marker with a
reason.

Why this exists: skmon, skfenceha, skport (and, until this change,
skpulse) all render and forward-auth correctly behind the cluster's
domain-wide Traefik ForwardAuth catch-all while having NO Authentik
dashboard tile declared anywhere in code - every one of those tiles was
hand-created through the Authentik UI/API, so a brand new role with a web
UI can ship, deploy and work perfectly while remaining invisible on the
portal, with nothing catching that at review time. This test is that
catch: see docs/ROLE_AUTHORING_CHECKLIST.md "Portal tile (Authentik)".

A role "has a web UI" when any of its rendered templates contains a
Traefik `Host(` router rule (whether as a Docker Swarm label, as this
framework's own file-provider dynamic config for skfence/skfenceha, or in
a per-service loop like skfetch's). A role with only
`traefik.enable=false` (no UI, API/CLI-only: skdesk, skform) never
matches and needs nothing.

Three happy-path role checks are parametrized (a wired generic-mechanism
role, a role-owned-mechanism role, and the one explicit opt-out), one edge
case covers multi-sub-service roles whose tile is inside an Ansible
`loop:` rather than a single `vars:` block, and the failure case plants a
throwaway role with a router and no tile/opt-out and asserts the scanner
catches exactly it.
"""
import shutil
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "ansible"
ROLE_DIRS = [("core", d) for d in ("skfence", "skfenceha", "skha", "sksec")] + [
    ("optional", d.name) for d in sorted((ROOT / "optional").iterdir()) if d.is_dir()
]

TILE_MARKERS = (
    "authentik_portal_tile.yml",  # the shared generic mechanism
)
OWN_MECHANISM_MARKERS = (
    "tasks/authentik.yml",  # optional/skrac, optional/skmesh: provider + application
)
OPT_OUT_MARKER = "portal-tile: opt-out"


def has_web_ui(kind: str, role: str) -> bool:
    role_dir = ROOT / kind / role
    for j2 in role_dir.rglob("*.j2"):
        if "Host(" in j2.read_text(errors="ignore"):
            return True
    return False


def prod_playbook_text(kind: str, role: str) -> str:
    path = ROOT / kind / role / f"deploy_{role}-prod.yml"
    if not path.exists():
        return ""
    return path.read_text()


def is_covered(kind: str, role: str) -> bool:
    text = prod_playbook_text(kind, role)
    if any(m in text for m in TILE_MARKERS):
        return True
    if all(m in text for m in OWN_MECHANISM_MARKERS):
        return True
    if OPT_OUT_MARKER in text:
        return True
    return False


def web_ui_roles():
    return [(kind, role) for kind, role in ROLE_DIRS if has_web_ui(kind, role)]


# --- detection sanity (not the lint itself, but pins the heuristic) -------

def test_detects_known_web_ui_roles():
    found = {role for _, role in web_ui_roles()}
    for expected in ("skpulse", "sksec-web", "skpatch", "skmon", "skport", "skfenceha"):
        assert expected in found


def test_does_not_flag_api_only_roles():
    found = {role for _, role in web_ui_roles()}
    for cli_only in ("skdesk", "skform", "skbackup", "skgraph", "skreg"):
        assert cli_only not in found


# --- happy path: a role wired with each accepted mechanism ---------------

@pytest.mark.parametrize("kind,role", [("optional", "skpulse"), ("optional", "sksec-web"), ("optional", "skpatch")])
def test_generic_mechanism_role_is_covered(kind, role):
    assert is_covered(kind, role)


@pytest.mark.parametrize("kind,role", [("optional", "skrac"), ("optional", "skmesh")])
def test_own_mechanism_role_is_covered(kind, role):
    assert is_covered(kind, role)


def test_explicit_opt_out_role_is_covered():
    assert is_covered("optional", "sksso")


# --- edge case: a multi-sub-service tile declared inside a loop: ---------

def test_multi_service_role_loop_tile_is_covered():
    text = prod_playbook_text("optional", "skmon")
    assert "authentik_portal_tile.yml" in text
    assert "loop:" in text
    assert is_covered("optional", "skmon")


# --- the actual fleet-wide lint: every detected web-UI role is covered ---

def test_every_web_ui_role_has_a_tile_or_an_opt_out():
    uncovered = [f"{kind}/{role}" for kind, role in web_ui_roles() if not is_covered(kind, role)]
    assert not uncovered, (
        "these roles expose a Traefik web UI but declare no Authentik portal "
        "tile and no explicit opt-out: " + ", ".join(sorted(uncovered)) +
        ". Wire shared/tasks/authentik_portal_tile.yml (see "
        "docs/ROLE_AUTHORING_CHECKLIST.md), or add a "
        "'# portal-tile: opt-out (<reason>)' comment to the role's prod "
        "deploy playbook."
    )


# --- failure case: a planted role with a router and nothing else ---------

def test_scanner_catches_an_unwired_role():
    with tempfile.TemporaryDirectory() as tmp:
        fake_root = Path(tmp) / "optional" / "skfake"
        (fake_root / "src" / "config" / "skfake").mkdir(parents=True)
        (fake_root / "src" / "config" / "skfake" / "skfake.yml.j2").write_text(
            '- "traefik.http.routers.skfake.rule=Host(`skfake.example.com`)"\n'
        )
        (fake_root / "deploy_skfake-prod.yml").write_text("- hosts: all\n  tasks: []\n")

        global ROOT
        real_root = ROOT
        try:
            ROOT = Path(tmp)
            assert has_web_ui("optional", "skfake")
            assert not is_covered("optional", "skfake")
        finally:
            ROOT = real_root
