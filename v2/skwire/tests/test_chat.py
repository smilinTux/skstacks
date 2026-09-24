"""Free-text chat: interpret what the user types into an actionable intent.

The user must be able to TYPE (not just click) — "just install the hf downloader"
adds only skhf, "install everything" adds the whole stack, etc."""
from __future__ import annotations

import pytest

from skwire import interpret, Intent, clear_catalog, load_catalog


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setenv("SKWIRE_CATALOG_URL", "http://127.0.0.1:9/none.json")  # built-ins only
    clear_catalog(); load_catalog(); yield; clear_catalog()


def test_just_install_one_adds_only_that_offering():
    i = interpret("just install the hf downloader")
    assert i.kind == "add_one" and i.target == "skhf"


def test_typing_the_exact_app_name_wins_over_fuzzy_match():
    # regression: "i wanna install skhf" matched GIMP (word "install" in its blurb)
    # instead of the app literally named in the message.
    i = interpret("i wanna install skhf")
    assert i.kind == "add_one" and i.target == "skhf"


def test_named_app_beats_a_generic_keyword():
    i = interpret("install skmedia")
    assert i.kind == "add_one" and i.target == "skmedia"


def test_only_keyword_scopes_to_a_single_app():
    i = interpret("only the media server please")
    assert i.kind == "add_one" and i.target == "skmedia"


def test_clear_intent_routes_without_an_action_word():
    # regression: "watch movies" (no install/want/just) was classed unknown and sent
    # to the weak model, which guessed skhf. A clear catalog match must route directly.
    i = interpret("watch movies")
    assert i.kind == "add_one" and i.target == "skmedia"


def test_pure_chitchat_stays_unknown():
    assert interpret("hello there friend").kind == "unknown"


def test_everything_adds_the_whole_stack():
    for phrase in ("install everything", "set up the whole stack", "give me the works"):
        assert interpret(phrase).kind == "add_all"


def test_search_intent_extracts_the_query():
    i = interpret("search for llama 3 8b")
    assert i.kind == "search" and "llama 3 8b" in i.target


def test_proceed_words_mean_show_the_plan():
    for phrase in ("ok do it", "go ahead", "what's the plan?"):
        assert interpret(phrase).kind in ("plan", "proceed")


def test_unknown_returns_suggestions_not_a_dead_end():
    i = interpret("hmmmm idk")
    assert i.kind == "unknown"
    assert i.suggestions                       # offers options instead of failing


def test_a_single_pick_never_silently_adds_everything():
    # the bug Chef hit: asking for one must NOT default to the whole stack
    i = interpret("I just want huggingface downloads")
    assert i.kind == "add_one" and i.target == "skhf"
