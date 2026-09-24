"""
skwire packs — "pack n ship". A project bundles its skwire knowledge as a Pack:
the descriptors it contributes (provides/needs), its project-specific injectors,
extra probes, and its Socratic questions. skwire merges all registered packs into
one graph and configures everything conversationally — no hand-edited TUI.

Discovery: a project declares an entry point so installing it registers its pack:

    # in the project's pyproject.toml
    [project.entry-points."skwire.packs"]
    hermes = "hermes.skwire_pack:pack"     # a callable returning a Pack

Then `skwire` auto-discovers it via load_packs_from_entrypoints().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .branding import Branding


@dataclass
class Pack:
    name: str
    nodes: list[dict] = field(default_factory=list)          # provides/needs descriptors
    injectors: dict = field(default_factory=dict)            # name -> Injector (project-specific)
    probes: list = field(default_factory=list)               # extra env probes
    options: list = field(default_factory=list)              # declared flags/params → auto questions
    questions: list = field(default_factory=list)            # bespoke Socratic questions (merged after)
    branding: Optional[Branding] = None                      # white-label the experience
    post_install: str = ""                                   # friendly "what now" shown after wiring


_REGISTRY: dict[str, Pack] = {}


def register_pack(pack: Pack) -> Pack:
    if pack.name in _REGISTRY:
        raise ValueError(f"pack {pack.name!r} is already registered")
    _REGISTRY[pack.name] = pack
    return pack


def get_pack(name: str) -> Pack:
    return _REGISTRY[name]


def list_packs() -> list[str]:
    return list(_REGISTRY)


def clear_registry() -> None:
    _REGISTRY.clear()


def all_nodes() -> list[dict]:
    """Every descriptor node across all registered packs (the merged graph)."""
    nodes: list[dict] = []
    for p in _REGISTRY.values():
        nodes.extend(p.nodes)
    return nodes


def load_packs_from_entrypoints(entry_points=None) -> list[str]:
    """
    Discover + register packs from the `skwire.packs` entry-point group.
    `entry_points` is injectable for testing; in prod it defaults to the installed
    distribution's entry points (so `pip install <project>` makes its pack available).
    Each entry point loads to a Pack or a zero-arg callable returning a Pack.
    """
    if entry_points is None:
        from importlib.metadata import entry_points as _eps
        entry_points = _eps(group="skwire.packs")
    loaded: list[str] = []
    for ep in entry_points:
        obj = ep.load()
        pack = obj() if callable(obj) else obj
        if pack.name not in _REGISTRY:
            register_pack(pack)
        loaded.append(pack.name)
    return loaded
