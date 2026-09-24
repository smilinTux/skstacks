"""Guard tests: each v1→v2 migration runbook stays actionable + safe."""
from __future__ import annotations

from pathlib import Path

import pytest

_MIG = Path(__file__).resolve().parents[1]
_RUNBOOKS = ["redis-to-valkey.md", "minio-to-garage.md", "pg-to-pg17-unified.md"]


@pytest.mark.parametrize("rb", _RUNBOOKS)
def test_runbook_has_safety_sections(rb):
    text = (_MIG / rb).read_text().lower()
    for section in ("precheck", "backup", "verify", "rollback"):
        assert section in text, f"{rb} missing a '{section}' section"


def test_readme_links_all_runbooks():
    readme = (_MIG / "README.md").read_text()
    for rb in _RUNBOOKS:
        assert rb in readme, f"README does not link {rb}"


def test_runbooks_target_ratified_tech():
    assert "valkey" in (_MIG / "redis-to-valkey.md").read_text().lower()
    assert "garage" in (_MIG / "minio-to-garage.md").read_text().lower()
    assert "pg17" in (_MIG / "pg-to-pg17-unified.md").read_text().lower()
