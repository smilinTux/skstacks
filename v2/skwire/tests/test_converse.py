"""Multi-turn conversation engine — driven by an injected LLM client, so we can
unit-test a real back-and-forth offline with a scripted fake model."""
from __future__ import annotations

import pytest

from skwire import converse, ConverseResult, build_system_prompt, clear_catalog, load_catalog, list_offerings


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    monkeypatch.setenv("SKWIRE_CATALOG_URL", "http://127.0.0.1:9/none.json")
    clear_catalog(); load_catalog(); yield; clear_catalog()


class ScriptedLLM:
    """A fake LLM: returns the next canned response and records what it was asked."""
    name = "fake"

    def __init__(self, responses):
        self._responses = list(responses)
        self.seen = []                       # the messages list for each call

    def chat(self, messages):
        self.seen.append(messages)
        return self._responses.pop(0)


def test_plain_chat_turn_has_no_action():
    llm = ScriptedLLM(['{"reply":"Hey! What do you want to set up?","action":{"kind":"none"}}'])
    r = converse([], "hi there", llm, offerings=list_offerings())
    assert isinstance(r, ConverseResult)
    assert r.reply == "Hey! What do you want to set up?"
    assert r.action["kind"] == "none"
    assert len(r.history) == 2 and r.history[-1]["role"] == "assistant"


def test_a_real_back_and_forth_threads_history():
    llm = ScriptedLLM([
        '{"reply":"Sure — which repo, or want me to search?","action":{"kind":"none"}}',
        '{"reply":"Adding the HF downloader for you.","action":{"kind":"add_one","target":"skhf"}}',
    ])
    r1 = converse([], "i want to grab some models off huggingface", llm, offerings=list_offerings())
    assert r1.action["kind"] == "none"                       # it asked a question first
    r2 = converse(r1.history, "just set up the downloader", llm, offerings=list_offerings())
    assert r2.action == {"kind": "add_one", "target": "skhf"}
    # the 2nd call must include the prior turns (real memory, not stateless)
    second_call = llm.seen[1]
    roles = [m["role"] for m in second_call]
    assert roles.count("user") == 2 and "assistant" in roles


def test_system_prompt_lists_the_catalog_so_the_model_knows_options():
    sp = build_system_prompt(list_offerings())
    assert "skhf" in sp and "skmedia" in sp
    assert "json" in sp.lower()                              # instructed to return structured output


def test_tolerates_fenced_json_and_extra_prose():
    llm = ScriptedLLM(['Sure thing!\n```json\n{"reply":"On it.","action":{"kind":"add_one","target":"skmedia"}}\n```'])
    r = converse([], "media server", llm, offerings=list_offerings())
    assert r.action == {"kind": "add_one", "target": "skmedia"}
    assert r.reply == "On it."


def test_malformed_output_degrades_gracefully():
    llm = ScriptedLLM(["the model rambled and forgot the json"])
    r = converse([], "hello", llm, offerings=list_offerings())
    assert r.action["kind"] == "none"                        # no crash
    assert r.reply                                            # still says something
    assert r.ok is False                                     # signal: caller should fall back


def test_template_echo_is_rejected_not_shown():
    # a weak model that parrots the schema placeholders must NOT leak "<...>" to the user
    llm = ScriptedLLM(['{"reply":"<friendly answer, 1-2 sentences>",'
                       '"action":{"kind":"<add_one|add_all|none>","target":""}}'])
    r = converse([], "hi", llm, offerings=list_offerings())
    assert "<" not in r.reply                                 # placeholder never surfaced
    assert r.ok is False                                     # treated as degenerate → fall back


def test_good_structured_output_is_marked_ok():
    llm = ScriptedLLM(['{"reply":"On it.","action":{"kind":"add_one","target":"skhf"}}'])
    r = converse([], "hf", llm, offerings=list_offerings())
    assert r.ok is True and r.action["kind"] == "add_one"
