"""
skwire catalog — the browsable "app store" of offerings (packs) a user can one-click
add during or after an install.

Three sources, all open:
  • built-in   — first-party offerings we curate (skmedia, skhome, …)
  • pip:<pkg>  — a published pack (`pip install <pkg>` exposes a skwire.packs entry point)
  • a catalog file/URL — anyone can host their own JSON catalog and point skwire at it

So: take the engine and roll your own, add a pack to our list, or host your own list.
Offerings are plain data (JSON-serializable) for remote catalogs + the web API.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Callable, Optional

from .pack import Pack, register_pack, list_packs


@dataclass(frozen=True)
class Offering:
    name: str
    title: str
    description: str
    category: str               # media | home | dev | infra | creative | …
    source: str = "builtin"     # "builtin" | "pip:<pkg>" | "pkg:<name>" | "url:<…>"
    icon: str = "📦"
    license: str = "OSS"        # everything here is best-of-breed OPEN SOURCE
    repo: str = ""              # source repo → it's forkable: roll your own / contribute back

    def to_dict(self) -> dict:
        return asdict(self)


# The community catalog lives in its own repo so PRs to add an app never touch the
# engine. skwire auto-pulls from its raw URL (override with SKWIRE_CATALOG_URL).
DEFAULT_CATALOG_URL = "https://raw.githubusercontent.com/smilinTux/skwire-catalog/main/catalog.json"

_OFFERINGS: dict[str, Offering] = {}
_BUILTIN_PACKS: dict[str, Callable[[], Pack]] = {}


def register_offering(o: Offering, pack_factory: Optional[Callable[[], Pack]] = None) -> None:
    _OFFERINGS[o.name] = o
    if pack_factory:
        _BUILTIN_PACKS[o.name] = pack_factory


def list_offerings(category: Optional[str] = None) -> list[Offering]:
    offs = sorted(_OFFERINGS.values(), key=lambda o: (o.category, o.name))
    return [o for o in offs if category is None or o.category == category]


def clear_catalog() -> None:
    _OFFERINGS.clear()
    _BUILTIN_PACKS.clear()


def install_offering(name: str) -> dict:
    """Add an offering. Built-ins register their pack; pip/url return a hint."""
    o = _OFFERINGS[name]
    if o.source == "builtin" and name in _BUILTIN_PACKS:
        if o.name not in list_packs():
            register_pack(_BUILTIN_PACKS[name]())
        return {"installed": True, "via": "builtin", "name": name}
    if o.source.startswith("pip:"):
        return {"installed": False, "via": "pip", "hint": f"pip install {o.source[4:]}"}
    if o.source.startswith("pkg:"):
        return {"installed": False, "via": "pkg", "hint": f"flatpak/apt install {o.source[4:]}"}
    if o.source.startswith("url:"):
        return {"installed": False, "via": "url", "hint": f"load_catalog_file('{o.source[4:]}')"}
    return {"installed": False, "reason": "unknown source"}


# ── the AI "what should I install?" recommender ───────────────────────────────
_HINTS = {
    "media":    {"movie", "movies", "tv", "watch", "stream", "streaming", "jellyfin",
                 "plex", "music", "media", "download", "downloads", "shows"},
    "home":     {"home", "automation", "automate", "camera", "cameras", "smart",
                 "zigbee", "frigate", "sensor", "sensors", "lights", "doorbell"},
    "dev":      {"code", "coding", "ai", "agent", "agents", "claude", "codex",
                 "opencode", "dev", "develop", "model", "llm", "contribute", "fork"},
    "infra":    {"monitor", "monitoring", "metrics", "logs", "observability",
                 "grafana", "prometheus", "uptime", "dashboard"},
    "creative": {"image", "images", "photo", "photos", "edit", "editing", "draw",
                 "paint", "gimp", "design", "art", "graphics"},
    "data":     {"download", "dataset", "datasets", "huggingface", "hf", "model",
                 "models", "training", "weights", "checkpoint", "resume", "1tb"},
}


def _match_score(o: "Offering", words: set) -> int:
    import re
    s = 3 * len(words & _HINTS.get(o.category, set()))
    own = set(re.findall(r"[a-z]+", f"{o.title} {o.description} {o.name}".lower()))
    return s + len(words & own)


def recommend(text: str, offerings: Optional[list] = None) -> list:
    """Rank offerings by how well they match what the user said. No match → list all."""
    import re
    offs = offerings if offerings is not None else list_offerings()
    words = set(re.findall(r"[a-z]+", text.lower()))
    ranked = sorted(offs, key=lambda o: _match_score(o, words), reverse=True)
    if not ranked or _match_score(ranked[0], words) == 0:
        return list(offs)          # "what can I install?" → show everything
    return ranked


def best_offering(text: str, offerings: Optional[list] = None):
    """The single clearly-matching offering, or None when nothing really matches.

    Stricter than recommend(): scores ONLY on the curated category hints + the exact
    app name — never on description prose — so chit-chat ('hello', 'do it') matches
    nothing and isn't force-routed to a random app."""
    import re
    offs = offerings if offerings is not None else list_offerings()
    words = set(re.findall(r"[a-z]+", text.lower()))

    def strict(o: "Offering") -> int:
        hits = len(words & _HINTS.get(o.category, set()))
        return 3 * hits + (1 if o.name.lower() in words else 0)

    ranked = sorted(offs, key=strict, reverse=True)
    return ranked[0] if ranked and strict(ranked[0]) > 0 else None


def load_catalog_file(path_or_url: str) -> int:
    """Register offerings from a JSON catalog (a list of offering dicts). Extensible."""
    if path_or_url.startswith(("http://", "https://")):
        import urllib.request
        raw = urllib.request.urlopen(path_or_url, timeout=10).read().decode()
    else:
        with open(path_or_url) as f:
            raw = f.read()
    n = 0
    for d in json.loads(raw):
        register_offering(Offering(**d))
        n += 1
    return n


def load_remote_catalog(url: Optional[str] = None) -> int:
    """
    Pull the community catalog from the skwire-catalog repo (or SKWIRE_CATALOG_URL,
    or a passed url). Best-effort + OFFLINE-SAFE — never raises if it can't reach it.
    Returns how many offerings were registered (0 on failure / no network).
    """
    import os
    src = url or os.environ.get("SKWIRE_CATALOG_URL") or DEFAULT_CATALOG_URL
    try:
        return load_catalog_file(src)
    except Exception:
        return 0


def load_catalog(remote: bool = True) -> None:
    """Convenience: first-party built-ins + (best-effort) the remote community catalog."""
    load_builtin_catalog()
    if remote:
        load_remote_catalog()


# ── the curated first-party catalog ───────────────────────────────────────────
def _demo_pack(name: str, node: str) -> Callable[[], Pack]:
    return lambda: Pack(name=name, nodes=[{"name": node, "provides": {"url": f"http://{node}"}}])


def load_builtin_catalog() -> None:
    """Register the first-party offerings (each is a clickable standalone deployment)."""
    def _skmedia():
        from .packs.skmedia import pack
        return pack()
    register_offering(Offering(
        name="skmedia", title="Media Server", category="media", icon="🎬",
        description="Jellyfin + the *arr stack — your own streaming + automatic downloads, fully wired.",
        license="GPL/Apache (Jellyfin/Servarr)", repo="https://github.com/jellyfin/jellyfin"),
        _skmedia)
    register_offering(Offering(
        name="skhome", title="Home Automation", category="home", icon="🏠",
        description="Home Assistant + Frigate NVR (AI cameras) + Zigbee/Z-Wave, MQTT-wired.",
        license="Apache-2.0 (HA/Frigate)", repo="https://github.com/home-assistant/core"),
        lambda: __import__("skwire.packs.skhome", fromlist=["pack"]).pack())
    register_offering(Offering(
        name="skdev", title="AI Dev Toolchain", category="dev", icon="🤖",
        description="Claude Code / Codex / OpenCode + your model endpoints (OpenRouter/Ollama), keys wired. "
                    "Use it to contribute back or fork your own.",
        license="OSS", repo="https://github.com/sst/opencode"),
        lambda: __import__("skwire.packs.skdev", fromlist=["pack"]).pack())
    register_offering(Offering(
        name="skobserve", title="Observability", category="infra", icon="📈",
        description="Prometheus + Grafana + VictoriaLogs + Gatus — metrics, logs, uptime, pre-wired.",
        license="Apache/AGPL", repo="https://github.com/grafana/grafana"),
        _demo_pack("skobserve", "grafana"))
    register_offering(Offering(
        name="skhf", title="HF Download Assistant", category="data", icon="⬇️",
        description="Foolproof Hugging Face downloads — resumable 1TB datasets/models on the right disk "
                    "(no more filling ~/ or re-downloading at 900GB). Chat it the repo + disk.",
        license="Apache-2.0 (huggingface_hub)", repo="https://github.com/huggingface/huggingface_hub"),
        lambda: __import__("skwire.packs.skhf.skhf", fromlist=["pack"]).pack())
    # Best-of-breed OSS isn't just servers — desktop apps too. Install GIMP, use it,
    # then have the AI help you contribute or roll your own fork.
    register_offering(Offering(
        name="gimp", title="GIMP (image editor)", category="creative", icon="🎨",
        description="The open-source image editor. Install it, use it — then use the AI dev toolchain "
                    "to contribute upstream or fork your own.",
        source="pkg:gimp", license="GPL-3.0", repo="https://gitlab.gnome.org/GNOME/gimp"))
