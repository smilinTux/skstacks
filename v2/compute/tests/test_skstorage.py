"""Guard tests for the skstorage family descriptors (skblock / skfile / skobject)."""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_COMPUTE = Path(__file__).resolve().parents[1]


def _app(port):
    return yaml.safe_load((_COMPUTE / port / "app.yaml").read_text())


def test_three_storage_ports_exist():
    for port in ("skblock", "skfile", "skobject"):
        assert (_COMPUTE / port / "app.yaml").is_file()


def test_skblock_is_longhorn_with_object_backup_target():
    d = _app("skblock")
    assert d["name"] == "skblock"
    assert "Longhorn" in d["provider"]
    assert "skobject" in d["config"]["BACKUP_TARGET"].lower() or "s3://" in d["config"]["BACKUP_TARGET"]


def test_skfile_juicefs_composes_object_plus_metadata():
    d = _app("skfile")
    assert d["name"] == "skfile"
    assert "JuiceFS" in d["provider"]
    # the sovereign move: data=skobject, metadata=skdata
    assert set(d["depends_on"]) >= {"skobject", "skdata"}
    assert d["config"]["META_ENGINE"] == "postgres"


def test_skobject_is_garage_not_minio():
    d = _app("skobject")
    assert "Garage" in d["provider"]
    # MinIO is archived — it must not be a SELECTABLE adapter (provider/alternates),
    # though a historical "replaces MinIO" note in the description is fine.
    assert "minio" not in d["provider"].lower()
    assert not any("minio" in str(a).lower() for a in d.get("alternates", []))
