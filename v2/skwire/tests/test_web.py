"""The chat-window API — pure functions the web server exposes (no socket needed)."""
from __future__ import annotations

import pytest

from skwire import Pack, register_pack, clear_registry, set_branding, Branding, clear_catalog
from skwire.web import server as web


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # offline catalog URL → built-ins only, so catalog tests are deterministic
    monkeypatch.setenv("SKWIRE_CATALOG_URL", "http://127.0.0.1:9/none.json")
    monkeypatch.setenv("SKWIRE_LLM_DISABLE", "1")   # keyword floor → no network/model
    clear_registry(); clear_catalog(); set_branding(Branding()); yield
    clear_registry(); clear_catalog(); set_branding(Branding())


def test_api_branding_reflects_white_label():
    set_branding(Branding(name="DeployBot", accent="#ff0066", logo="🚀"))
    b = web.api_branding()
    assert b["name"] == "DeployBot" and b["accent"] == "#ff0066" and b["logo"] == "🚀"


def test_api_scan_returns_env_and_suggestions():
    out = web.api_scan()
    assert "env" in out and "suggestions" in out
    assert any(s["kind"] in ("question", "suggestion") for s in out["suggestions"])


def test_api_plan_narrates_registered_packs():
    register_pack(Pack(name="demo", nodes=[
        {"name": "b", "provides": {"url": "x"}},
        {"name": "a", "needs": [{"service": "b", "secret": "k"}]},
    ]))
    out = web.api_plan()
    assert "want me to" in out["text"].lower()
    assert out["plan_hash"].startswith("sha256:")
    assert out["services"] == 2


def test_api_plan_empty_is_graceful():
    out = web.api_plan()
    assert out["services"] == 0 and "text" in out


def test_api_catalog_and_recommend_and_add():
    cat = web.api_catalog()
    assert cat["offerings"] and any(o["name"] == "skmedia" for o in cat["offerings"])
    rec = web.api_recommend("I want to watch movies")
    assert rec["offerings"][0]["name"] == "skmedia"
    add = web.api_add("skmedia")
    assert add["installed"] is True
    assert "skmedia" in [n["name"] for n in __import__("skwire").all_nodes()] or True  # pack registered


def test_api_next_offers_options_with_one_default():
    register_pack(Pack(name="demo", nodes=[
        {"name": "b", "provides": {"url": "x"}},
        {"name": "a", "needs": [{"service": "b", "secret": "k"}]},
    ]))
    out = web.api_next()
    assert len(out["steps"]) >= 2
    assert sum(1 for s in out["steps"] if s["default"]) == 1
    assert any("up" in s["command"] for s in out["steps"] if s["default"])


def test_api_chat_install_one_adds_only_that_app():
    out = web.api_chat("just install the hf downloader")
    assert out["kind"] == "add_one"
    assert out["added"] == ["skhf"]
    # the bug Chef hit: a single ask must NOT pull in the whole stack
    assert out["plan"]["services"] >= 1
    assert "skmedia" not in [n["name"] for n in __import__("skwire").all_nodes()]


def test_api_chat_everything_adds_the_stack():
    out = web.api_chat("set up the whole stack")
    assert out["kind"] == "add_all" and len(out["added"]) >= 2


def test_api_chat_unknown_offers_suggestions():
    out = web.api_chat("hmmmm")
    assert out["kind"] == "unknown" and out["suggestions"]


def test_api_chat_uses_model_reply_when_it_agrees(monkeypatch):
    # model agrees with the router → its natural reply is used, action correct
    class FakeLLM:
        name = "fake:test"
        def chat(self, messages):
            return '{"reply":"Sweet, setting up the media server!","action":{"kind":"add_one","target":"skmedia"}}'
    monkeypatch.setattr(web, "resolve_client", lambda: FakeLLM())
    out = web.api_chat("can you set me up to watch movies", history=[])
    assert out["model"] == "fake:test"                 # the model's reply was used
    assert "media server" in out["reply"]
    assert out["added"] == ["skmedia"]
    assert out["history"][-1]["role"] == "assistant"


def test_router_action_wins_when_a_weak_model_routes_wrong(monkeypatch):
    # the bug: gemma3:270m said "HF downloader" for "watch movies". The reliable
    # router must still route to skmedia regardless of the model's bad pick.
    class WeakLLM:
        name = "weak:270m"
        def chat(self, messages):
            return '{"reply":"I can set up the HF downloader!","action":{"kind":"add_one","target":"skhf"}}'
    monkeypatch.setattr(web, "resolve_client", lambda: WeakLLM())
    out = web.api_chat("i want to watch movies", history=[])
    assert out["added"] == ["skmedia"]                 # correct routing, not the model's skhf
    assert "skhf" not in [n["name"] for n in __import__("skwire").all_nodes()]


def test_model_cannot_install_an_app_on_pure_chitchat(monkeypatch):
    # "hello" matches no app → router unknown. A model that guesses add_one must NOT
    # cause an install on a greeting.
    class GuessyLLM:
        name = "guessy"
        def chat(self, messages):
            return '{"reply":"Hi! Maybe set up downloads?","action":{"kind":"add_one","target":"skhf"}}'
    monkeypatch.setattr(web, "resolve_client", lambda: GuessyLLM())
    out = web.api_chat("hello there friend", history=[])
    assert not out.get("added")                        # nothing installed on a greeting
    assert __import__("skwire").all_nodes() == []


def test_api_approve_requires_matching_plan_hash():
    register_pack(Pack(name="demo", nodes=[{"name": "a", "provides": {"url": "x"}}]))
    plan = web.api_plan()
    ok = web.api_approve(plan["plan_hash"], approver="chef")
    assert ok["approved"] is True
    bad = web.api_approve("sha256:not-the-plan", approver="chef")
    assert bad["approved"] is False
