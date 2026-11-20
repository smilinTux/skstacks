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
    for expected in ("skpulse", "sksec-web", "skpatch-hosts", "skmon", "skport", "skfenceha"):
        assert expected in found


def test_does_not_flag_api_only_roles():
    found = {role for _, role in web_ui_roles()}
    for cli_only in ("skdesk", "skform", "skbackup", "skgraph", "skreg"):
        assert cli_only not in found


# --- happy path: a role wired with each accepted mechanism ---------------

@pytest.mark.parametrize("kind,role", [("optional", "skpulse"), ("optional", "sksec-web"), ("optional", "skpatch-hosts")])
def test_generic_mechanism_role_is_covered(kind, role):
    assert is_covered(kind, role)


@pytest.mark.parametrize("kind,role", [("optional", "skrac"), ("optional", "skmesh")])
def test_own_mechanism_role_is_covered(kind, role):
    assert is_covered(kind, role)


def test_explicit_opt_out_role_is_covered():
    assert is_covered("optional", "sksso")


# --- edge case: a multi-sub-service tile declared inside a loop: ---------

def test_multi_service_role_loop_tile_is_covered():
    # skmon's tile loop lives in the role task file the play includes (tasks/tiles.yml).
    assert "tasks/tiles.yml" in prod_playbook_text("optional", "skmon")
    text = (ROOT / "optional" / "skmon" / "tasks" / "tiles.yml").read_text()
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


# ===========================================================================
# SSO access declaration (docs/design/sso-app-access-provisioning.md, 4.8, 7)
# ===========================================================================
#
# Phase 1 adds shared/tasks/sso_access_resolve.yml and makes the shared tile
# task resolve the access itself when a role still passes only `group:` (the
# compatibility shim). Phase 3 moves every role to resolving early and passing
# `access: "{{ sso_access }}"`. This is a ratchet: the roles that exist today
# are listed as pending and still pass, a NEW tile role must handle SSO access,
# and a pending role that gets migrated must be taken off the list.

import re  # noqa: E402

SHARED_TASKS = ROOT / "shared" / "tasks"
TILE_INCLUDE_RE = re.compile(r"""include_tasks:\s*["']?[^\n]*authentik_portal_tile\.yml""")
SSO_HANDLING_RE = re.compile(r"""sso_access_resolve\.yml|^\s+access:\s*["']?\{\{\s*sso_access\b""", re.M)

PHASE3_PENDING = frozenset({
    "core/skfence", "core/skfenceha",
    "optional/skaegis", "optional/skboard", "optional/skbom", "optional/skbook", "optional/skdash",
    "optional/skfetch", "optional/skflow", "optional/skgallery", "optional/skgit", "optional/skgym",
    "optional/skhome", "optional/skhub", "optional/skmail", "optional/skmon", "optional/skorch",
    "optional/skpatch-hosts", "optional/skpayment", "optional/skpdf", "optional/skpeek", "optional/skport",
    "optional/skpulse", "optional/sksec-web", "optional/skseek", "optional/skstor", "optional/skstream",
    "optional/sksync", "optional/skvector", "optional/skwhoami",
})


def role_task_texts(kind: str, role: str):
    role_dir = ROOT / kind / role
    paths = sorted(role_dir.glob("deploy_*.yml")) + sorted((role_dir / "tasks").glob("*.yml"))
    return [p.read_text(errors="ignore") for p in paths]


def uses_shared_tile(kind: str, role: str) -> bool:
    return any(TILE_INCLUDE_RE.search(t) for t in role_task_texts(kind, role))


def has_sso_access_handling(kind: str, role: str) -> bool:
    return any(SSO_HANDLING_RE.search(t) for t in role_task_texts(kind, role))


def tile_roles():
    return [f"{kind}/{role}" for kind, role in ROLE_DIRS if uses_shared_tile(kind, role)]


def test_shared_tile_task_resolves_access_and_passes_it():
    text = (SHARED_TASKS / "authentik_portal_tile.yml").read_text()
    assert "include_tasks: sso_access_resolve.yml" in text  # the compatibility shim
    assert 'access: "{{ portal_tile_access }}"' in text  # the provisioner gets the resolved object
    assert (SHARED_TASKS / "sso_access_resolve.yml").exists()


def test_tile_role_detection_sees_known_roles():
    found = set(tile_roles())
    for expected in ("optional/skpulse", "optional/skmon", "core/skfenceha", "optional/skfetch"):
        assert expected in found
    assert "optional/sksso" not in found  # it is the portal (opt-out)


def test_new_tile_roles_handle_sso_access():
    missing = sorted(r for r in tile_roles() if not has_sso_access_handling(*r.split("/")) and r not in PHASE3_PENDING)
    assert not missing, (
        "these roles wire an Authentik portal tile but never resolve their SSO access: "
        + ", ".join(missing)
        + '. Include shared/tasks/sso_access_resolve.yml early and pass access: "{{ sso_access }}" '
        "in portal_tile (docs/design/sso-app-access-provisioning.md, 4.8)."
    )


def test_phase3_pending_list_has_no_stale_entries():
    found = set(tile_roles())
    stale = sorted(r for r in PHASE3_PENDING if r not in found or has_sso_access_handling(*r.split("/")))
    assert not stale, "take these off PHASE3_PENDING (no longer a tile role, or now migrated): " + ", ".join(stale)


def test_sso_access_scanner_catches_an_unhandled_tile_role():
    global ROOT
    with tempfile.TemporaryDirectory() as tmp:
        role = Path(tmp) / "optional" / "skfake"
        (role / "tasks").mkdir(parents=True)
        play = role / "deploy_skfake-prod.yml"
        play.write_text(
            "- hosts: all\n  tasks:\n    - include_tasks: ../../shared/tasks/authentik_portal_tile.yml\n"
            "      vars:\n        portal_tile:\n          group: skadmin\n"
        )
        real_root = ROOT
        try:
            ROOT = Path(tmp)
            assert uses_shared_tile("optional", "skfake")
            assert not has_sso_access_handling("optional", "skfake")
            # migrated form: resolve early, pass the fact
            play.write_text(
                "- hosts: all\n  tasks:\n"
                "    - include_tasks: ../../shared/tasks/sso_access_resolve.yml\n"
                "      vars:\n        sso_access_slug: skfake\n"
                "    - include_tasks: \"{{ playbook_dir }}/../../shared/tasks/authentik_portal_tile.yml\"\n"
                "      vars:\n        portal_tile:\n          access: \"{{ sso_access }}\"\n"
            )
            assert uses_shared_tile("optional", "skfake")
            assert has_sso_access_handling("optional", "skfake")
        finally:
            ROOT = real_root
