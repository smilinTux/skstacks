"""The model fallback ladder: configured endpoint → local Ollama → none (keyword floor)."""
from __future__ import annotations

import pytest

from skwire import resolve_client, OpenAIClient, OllamaClient


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("SKWIRE_LLM_URL", "SKWIRE_LLM_KEY", "SKWIRE_MODEL", "OLLAMA_HOST"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_configured_endpoint_wins(monkeypatch):
    monkeypatch.setenv("SKWIRE_LLM_URL", "http://my-box:8082")
    monkeypatch.setenv("SKWIRE_MODEL", "qwen3.6-27b")
    c = resolve_client(probe=lambda *a, **k: True)
    assert isinstance(c, OpenAIClient) and c.model == "qwen3.6-27b"


def test_falls_back_to_local_ollama_when_reachable():
    c = resolve_client(probe=lambda url, *a, **k: "11434" in url)
    assert isinstance(c, OllamaClient)


def test_no_model_available_returns_none_for_keyword_floor():
    c = resolve_client(probe=lambda *a, **k: False)
    assert c is None
