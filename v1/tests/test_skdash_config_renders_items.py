"""skdash's Dashy config must render the items of every section. Sections are
plain dicts (from discovery or from skdash.sections), so `section.items` in
Jinja resolves to dict.items (a method) and the render crashes with
"'method' object is not iterable" (found on the skstack06 v2.17.0 run)."""
import pathlib

import jinja2
import yaml

CONFIG = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skdash/src/config/skdash/config.yml.j2"


def test_sections_render_their_items():
    sections = [{"name": "Discovered Services", "icon": "fas fa-cube",
                 "items": [{"title": "whoami", "url": "https://whoami.example.com", "icon": "fas fa-globe"}]}]
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(CONFIG.read_text()).render(
        env="dev", app="skdash", skdash={}, skdash_effective_sections=sections)
    doc = yaml.safe_load(out)
    titles = [i.get("title") for s in doc.get("sections") or [] for i in s.get("items") or []]
    assert "whoami" in titles, out[:400]
