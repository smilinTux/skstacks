"""Provisioning the tiny model on a fresh box — every rung of the ladder."""
from __future__ import annotations

import pytest

from skwire.model import ensure_model, GGUF_REPO


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("SKWIRE_LLM_URL", "SKWIRE_MODEL"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_configured_endpoint_needs_no_provisioning(monkeypatch):
    monkeypatch.setenv("SKWIRE_LLM_URL", "http://box:8082")
    s = ensure_model(run=lambda *a, **k: None, have=lambda x: None, probe=lambda *a, **k: False)
    assert s.provider == "configured" and s.ready


def test_ollama_present_pulls_the_tiny_model():
    calls = []
    s = ensure_model(run=lambda cmd, **k: calls.append(cmd),
                     have=lambda x: "/usr/bin/ollama" if x == "ollama" else None,
                     probe=lambda *a, **k: False)
    assert s.provider == "ollama" and s.ready
    assert calls and calls[0][:2] == ["ollama", "pull"]      # actually pulled


def test_no_ollama_downloads_gguf_via_skhf():
    calls = []
    s = ensure_model(run=lambda cmd, **k: calls.append(cmd),
                     have=lambda x: "/usr/bin/llama-server" if x in ("llama-server", "llama-cli") else None,
                     probe=lambda *a, **k: False)
    assert s.provider == "llama.cpp"
    # used our own skhf downloader to fetch the GGUF
    assert any(GGUF_REPO in c and "--gguf" in c for c in calls)


def test_nothing_available_gives_actionable_guidance():
    s = ensure_model(run=lambda *a, **k: None, have=lambda x: None,
                     probe=lambda *a, **k: False)
    # skhf is bundled, so even here it downloads the GGUF and tells you to install llama.cpp
    assert s.provider in ("llama.cpp", "none")
    assert s.detail and not s.ready
