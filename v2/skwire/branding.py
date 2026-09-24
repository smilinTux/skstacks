"""
skwire vanity layer — white-label it as your own installer.

A project sets its name/logo/tagline (or ships them in its Pack) and the whole
experience — CLI banner, chat window, prompts — adopts it. "Powered by skwire"
underneath. So anyone can make it their own.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Branding:
    name: str = "skwire"
    tagline: str = "the universal bootstrapper"
    logo: str = ""                 # ASCII art, an emoji, or a path/URL the UI renders
    version: str = ""
    url: str = ""
    accent: str = "#22d3ee"        # UI accent color (cyan) — themes the chat window


_state: dict[str, Optional[Branding]] = {"current": None}


def set_branding(b: Branding) -> None:
    _state["current"] = b


def get_branding() -> Branding:
    return _state["current"] or Branding()


def active_branding() -> Branding:
    """A registered pack's branding wins; else the global/default."""
    from .pack import _REGISTRY
    for p in _REGISTRY.values():
        if getattr(p, "branding", None):
            return p.branding
    return get_branding()


def banner(b: Optional[Branding] = None) -> str:
    b = b or get_branding()
    lines = []
    if b.logo:
        lines.append(b.logo)
    lines.append(f"{b.name} — {b.tagline}" if b.tagline else b.name)
    if b.name.lower() != "skwire":
        lines.append("(powered by skwire)")
    if b.url:
        lines.append(b.url)
    return "\n".join(lines)
