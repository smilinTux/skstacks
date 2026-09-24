"""Auto-generate the per-module conversation from its declared options (+ custom Qs)."""
from __future__ import annotations

from skwire import Option, auto_question, questions_for, option_choices, Choice, Pack


def test_choice_option_lists_the_choices():
    q = auto_question(Option("type", flag="--type", type="choice", choices=["model", "dataset"]))
    assert "model" in q and "dataset" in q and "?" in q


def test_option_choices_marks_the_default():
    opt = Option("type", flag="--type", type="choice", choices=["model", "dataset"], default="model")
    cs = option_choices(opt)
    assert [c.value for c in cs] == ["model", "dataset"]
    assert sum(1 for c in cs if c.is_default) == 1            # exactly one default
    assert next(c for c in cs if c.is_default).value == "model"


def test_choice_question_marks_the_default_inline():
    q = auto_question(Option("type", type="choice", choices=["model", "dataset"], default="model"))
    assert "model (default)" in q                            # the default is obvious


def test_bool_option_offers_yes_no_with_a_default():
    cs = option_choices(Option("safetensors", type="bool", default=True))
    assert {c.value for c in cs} == {"yes", "no"}
    assert next(c for c in cs if c.is_default).value == "yes"


def test_bool_option_asks_yes_no_with_flag():
    q = auto_question(Option("safetensors", flag="--safetensors", type="bool"))
    assert "safetensors" in q.lower() and "--safetensors" in q


def test_path_option_asks_where():
    q = auto_question(Option("dest", flag="--dest", type="path", required=True))
    assert "dest" in q.lower() or "path" in q.lower()


def test_explicit_prompt_overrides_autogen():
    q = auto_question(Option("repo", flag="<repo>", prompt="Which HF repo (or describe it)?"))
    assert q == "Which HF repo (or describe it)?"


def test_questions_for_merges_autogen_options_plus_custom():
    pack = Pack(
        name="m",
        options=[Option("type", flag="--type", type="choice", choices=["a", "b"])],
        questions=["Any custom note?"],
    )
    qs = questions_for(pack)
    assert any("a" in q and "b" in q for q in qs)     # from options
    assert "Any custom note?" in qs                   # custom preserved


def test_skhf_pack_generates_its_flow_from_options():
    from skwire.packs.skhf.skhf import pack
    qs = questions_for(pack())
    joined = " ".join(qs).lower()
    assert "repo" in joined and "dest" in joined.replace("disk", "dest") or "disk" in joined
    assert "model" in joined and "dataset" in joined   # the --type choice
