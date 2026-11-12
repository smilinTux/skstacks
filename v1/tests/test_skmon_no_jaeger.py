"""Jaeger is gone from the skmon role (it had no trace producers)."""
from skmon_support import ANSIBLE, CFG, ROLE, services

# Files that later tasks add and that must name Jaeger as retired (operator cleanup notes).
RETIREMENT_REFERENCES = {"tasks/guards.yml", "tasks/tiles.yml", "tasks/authentik.yml", "src/skmon/authentik_provision.py", "README.md"}


def test_no_jaeger_anywhere_in_role():
    hits = [
        p for p in ROLE.rglob("*")
        if p.is_file()
        and p.relative_to(ROLE).as_posix() not in RETIREMENT_REFERENCES
        and b"jaeger" in p.read_bytes().lower()
    ]
    assert hits == []


def test_jaeger_not_a_service():
    assert "jaeger" not in services()


def test_jaeger_icon_and_notice_gone():
    assert not (CFG / "icons/jaeger.png").exists()
    assert "jaeger" not in (ANSIBLE / "shared/third_party/dashboard-icons/NOTICE.md").read_text().lower()
