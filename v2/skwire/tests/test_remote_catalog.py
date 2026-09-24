"""Remote catalog — pulls from the skwire-catalog repo; offline-safe + overridable."""
from __future__ import annotations

import json

import pytest

from skwire import (
    load_remote_catalog, load_catalog, list_offerings, clear_catalog, clear_registry,
)
from skwire.catalog import DEFAULT_CATALOG_URL


@pytest.fixture(autouse=True)
def _clean():
    clear_catalog(); clear_registry(); yield; clear_catalog(); clear_registry()


def test_default_url_points_at_the_skwire_catalog_repo():
    assert "skwire-catalog" in DEFAULT_CATALOG_URL and DEFAULT_CATALOG_URL.endswith("catalog.json")


def test_remote_catalog_is_offline_safe():
    # an unreachable URL must NOT raise — returns 0
    assert load_remote_catalog("https://127.0.0.1:9/nope.json") == 0


def test_env_override_loads_a_local_catalog(tmp_path, monkeypatch):
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps([
        {"name": "extra", "title": "Extra", "description": "from the community repo",
         "category": "dev", "source": "pip:extra-pack", "icon": "🧩"},
    ]))
    monkeypatch.setenv("SKWIRE_CATALOG_URL", str(cat))
    assert load_remote_catalog() == 1
    assert any(o.name == "extra" for o in list_offerings())


def test_load_catalog_merges_builtin_plus_remote(tmp_path, monkeypatch):
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps([{"name": "extra", "title": "Extra", "description": "x",
                                "category": "dev", "source": "pip:extra-pack"}]))
    monkeypatch.setenv("SKWIRE_CATALOG_URL", str(cat))
    load_catalog()                                   # builtins + remote
    names = {o.name for o in list_offerings()}
    assert {"skmedia", "extra"} <= names             # first-party + community
