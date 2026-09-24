"""The bilingual interpreter: Plan → plain-English the AI concierge speaks."""
from __future__ import annotations

from skwire.resolver import build_plan
from skwire.explain import explain


def _media_plan():
    return build_plan([
        {"name": "qbittorrent", "provides": {"url": "http://qbit:8080"}},
        {"name": "sonarr", "provides": {"url": "http://sonarr:8989"},
         "needs": [{"service": "qbittorrent", "secret": "qbit_pw"}]},
        {"name": "seerr", "needs": [{"service": "sonarr", "secret": "sonarr_api_key"}]},
    ])


def test_explain_names_the_services_and_count():
    text = explain(_media_plan())
    assert "3" in text
    for svc in ("qbittorrent", "sonarr", "seerr"):
        assert svc in text


def test_explain_states_connections_and_secret_count():
    text = explain(_media_plan()).lower()
    assert "2 secret" in text                 # 2 secrets to mint
    assert "connect" in text or "wire" in text
    assert "sonarr" in text and "qbittorrent" in text


def test_explain_ends_with_the_do_it_prompt():
    text = explain(_media_plan()).lower()
    assert "want me to" in text or "do it" in text


def test_explain_empty_plan_is_graceful():
    text = explain(build_plan([{"name": "solo", "provides": {"url": "x"}}]))
    assert "solo" in text
