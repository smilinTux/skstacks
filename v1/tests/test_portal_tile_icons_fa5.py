"""Portal tile icons must be Font Awesome 5 names.

Authentik's portal draws `fa://fa-<name>` icons from its bundled Font Awesome
5 set. A Font Awesome 6 only name (fa-house, fa-shield-halved, ...) is accepted
by the API, answers 200, and renders as a blank tile. Four such tiles were
found blank on a production portal on 2026-11-14. This test fails the build when a
role default (or a docs example) goes back to one of those names."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

# FA6 renames of FA5 icons the portal cannot draw -> the FA5 name to use.
FA6_ONLY = {
    "fa-shield-halved": "fa-shield-alt",
    "fa-magnifying-glass": "fa-search",
    "fa-heart-pulse": "fa-heartbeat",
    "fa-list-check": "fa-tasks",
    "fa-house": "fa-home",
    "fa-gears": "fa-cogs",
    "fa-gauge": "fa-tachometer-alt",
    "fa-gauge-high": "fa-tachometer-alt",
    "fa-circle-info": "fa-info-circle",
    "fa-diagram-project": "fa-project-diagram",
    "fa-arrows-rotate": "fa-sync-alt",
    "fa-clapperboard": "fa-film",
    "fa-xmark": "fa-times",
}


def _icon_names():
    for path in (ROOT / "ansible").rglob("*"):
        if path.suffix not in {".yml", ".j2", ".md", ".py"} or not path.is_file():
            continue
        for match in re.finditer(r"fa://(fa-[a-z0-9-]+)", path.read_text(errors="ignore")):
            yield path.relative_to(ROOT), match.group(1)


def test_no_fa6_only_icon_names_in_roles():
    bad = [(str(p), n, FA6_ONLY[n]) for p, n in _icon_names() if n in FA6_ONLY]
    assert not bad, f"Font Awesome 6 only icon names render blank on the portal: {bad}"


def test_icon_scan_finds_the_role_defaults():
    names = {n for _, n in _icon_names()}
    assert {"fa-shield-alt", "fa-home", "fa-search"} <= names
