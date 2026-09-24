"""
The catalog — a browsable list of offerings (packs) a user can one-click add.
First-party offerings ship built-in; anyone can register more, host their own
catalog (a JSON file/URL), or just pip-install a pack. Take the engine + roll
your own, or add to ours.
"""
from __future__ import annotations

import json

import pytest

from skwire import (
    Offering, register_offering, list_offerings, install_offering,
    load_builtin_catalog, load_catalog_file, clear_catalog,
    list_packs, all_nodes, clear_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_catalog(); clear_registry(); yield; clear_catalog(); clear_registry()


def test_builtin_catalog_has_offerings_with_descriptions():
    load_builtin_catalog()
    offs = list_offerings()
    assert offs and all(o.title and o.description and o.category for o in offs)
    names = {o.name for o in offs}
    assert {"skmedia", "skhome"} <= names                 # first-party offerings


def test_list_offerings_filters_by_category():
    load_builtin_catalog()
    media = list_offerings(category="media")
    assert media and all(o.category == "media" for o in media)


def test_install_builtin_offering_registers_its_pack():
    load_builtin_catalog()
    res = install_offering("skmedia")
    assert res["installed"] is True
    assert "skmedia" in list_packs()                      # pack registered
    assert any(n["name"] for n in all_nodes())            # its nodes are now in the graph


def test_pip_offering_returns_install_hint_not_auto_install():
    register_offering(Offering(name="acme", title="ACME", description="x",
                               category="dev", source="pip:acme-skwire-pack"))
    res = install_offering("acme")
    assert res["installed"] is False and "pip install acme-skwire-pack" in res["hint"]


def test_recommend_matches_user_intent_to_offerings():
    from skwire import recommend
    load_builtin_catalog()
    assert recommend("I want to watch movies and tv")[0].name == "skmedia"
    assert recommend("home automation with cameras")[0].name == "skhome"
    assert recommend("install claude code and an ai agent")[0].name == "skdev"


def test_recommend_lists_everything_when_no_strong_match():
    from skwire import recommend
    load_builtin_catalog()
    out = recommend("what can I install?")
    assert {o.name for o in out} >= {"skmedia", "skhome", "skdev", "skobserve"}


def test_anyone_can_host_a_catalog_file(tmp_path):
    cat = tmp_path / "catalog.json"
    cat.write_text(json.dumps([
        {"name": "myapp", "title": "My App", "description": "cool", "category": "dev",
         "source": "pip:myapp-pack", "icon": "🧩"},
    ]))
    load_catalog_file(str(cat))
    assert any(o.name == "myapp" and o.icon == "🧩" for o in list_offerings())


def test_skhf_offering_is_present_and_installs():
    load_builtin_catalog()
    assert any(o.name == "skhf" and o.category == "data" for o in list_offerings())
    res = install_offering("skhf")
    assert res["installed"] is True


def test_recommend_surfaces_skhf_for_download_intent():
    from skwire import recommend
    load_builtin_catalog()
    top = [o.name for o in recommend("download a huggingface dataset")[:2]]
    assert "skhf" in top
