"""
skwire conversation — auto-generate a module's Socratic questions from its declared
options (the CLI flags / params), so a pack author gets the chat flow for free.
Authors can still add bespoke `questions`; both are merged. The flow is particular
to each module because each declares its own options.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Option:
    name: str
    flag: str = ""                       # the CLI flag this maps to (e.g. --dest)
    type: str = "str"                    # "str" | "bool" | "choice" | "path"
    choices: tuple = ()                  # for type="choice"
    default: object = None
    required: bool = False
    prompt: Optional[str] = None         # explicit prompt overrides the auto-generated one


@dataclass(frozen=True)
class Choice:
    label: str                           # human label, e.g. "model"
    value: object                        # the value passed downstream
    is_default: bool = False              # exactly one is the default when a default exists


def option_choices(opt: Option) -> list:
    """The selectable options for a question, with the best default marked.

    Better than a single suggestion: give a few concrete choices the user can click,
    with exactly one pre-selected as the most-likely default."""
    if opt.type == "choice" and opt.choices:
        values = list(opt.choices)
    elif opt.type == "bool":
        values = ["yes", "no"]
    else:
        return []                        # free-text/path: no fixed choice set
    # normalise the default to a comparable value (bool default → yes/no)
    dflt = opt.default
    if opt.type == "bool" and isinstance(dflt, bool):
        dflt = "yes" if dflt else "no"
    if dflt is None and values:
        dflt = values[0]                 # first choice is the sensible default
    return [Choice(label=str(v), value=v, is_default=(v == dflt)) for v in values]


def auto_question(opt: Option) -> str:
    """Generate a Socratic question for a single option, surfacing the best default."""
    if opt.prompt:
        base = opt.prompt
    else:
        label = opt.name.replace("_", " ")
        if opt.type == "choice" and opt.choices:
            cs = option_choices(opt)
            rendered = " / ".join(
                f"{c.value} (default)" if c.is_default else str(c.value) for c in cs
            )
            base = f"{label.capitalize()}? ({rendered})"
        elif opt.type == "bool":
            base = f"Do you want {label}? ({opt.flag})" if opt.flag else f"Do you want {label}?"
        elif opt.type == "path":
            base = f"Which path for {label}?"
        else:
            base = f"What's the {label}?" + ("" if opt.required else " (optional)")
    # For non-choice options, still surface the best default inline so they can just accept.
    if opt.default is not None and opt.type != "choice":
        base += f"  [default: {opt.default}]"
    return base


def questions_for(pack) -> list:
    """The module's full conversation: auto-generated from its options, then any
    bespoke `questions` the author added."""
    auto = [auto_question(o) for o in getattr(pack, "options", []) or []]
    custom = list(getattr(pack, "questions", []) or [])
    return auto + custom
