"""The vanity layer — white-label skwire as your own installer (name/logo/tagline)."""
from __future__ import annotations

import pytest

from skwire import (
    Branding, set_branding, get_branding, banner, active_branding,
    Pack, register_pack, clear_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    clear_registry(); set_branding(Branding()); yield
    clear_registry(); set_branding(Branding())


def test_default_branding_is_skwire():
    assert get_branding().name == "skwire"


def test_set_and_get_branding_roundtrip():
    set_branding(Branding(name="DeployBot", tagline="ship it sovereign"))
    assert get_branding().name == "DeployBot"


def test_banner_renders_name_tagline_and_logo():
    b = Branding(name="DeployBot", tagline="ship it", logo="<<DB>>", url="https://db.example")
    text = banner(b)
    assert "DeployBot" in text and "ship it" in text and "<<DB>>" in text


def test_banner_credits_skwire_when_white_labeled():
    text = banner(Branding(name="DeployBot"))
    assert "skwire" in text.lower()           # "powered by skwire"


def test_a_pack_can_brand_the_whole_experience():
    register_pack(Pack(name="myproj",
                       nodes=[{"name": "a", "provides": {"url": "x"}}],
                       branding=Branding(name="MyInstaller", tagline="one click")))
    assert active_branding().name == "MyInstaller"


def test_active_branding_falls_back_to_global_default():
    register_pack(Pack(name="plain", nodes=[{"name": "a", "provides": {"url": "x"}}]))
    assert active_branding().name == "skwire"
