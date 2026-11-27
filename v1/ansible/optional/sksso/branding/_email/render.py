#!/usr/bin/env python3
"""Render the shared sksso email set for one brand.

Usage: render.py <pack>/email.yml <out_dir>

Writes <out_dir>/email/*.html|txt (Django templates for Authentik's
custom-templates dir, they override the stock email/*.html) and
<out_dir>/apply_email.py (run with `ak shell` to point the email stages,
email sign-in stages and notification transports at them).

The sources mix two template languages: brand values are Jinja with
[[ ]] / [% %] / [# #] delimiters and are filled in here; {{ }} / {% %} are
Django and are left for Authentik to render at send time. Needs jinja2 and
PyYAML (the Ansible controller has both).
"""
import sys
from pathlib import Path

import jinja2
import yaml

HERE = Path(__file__).resolve().parent
REQUIRED = ("name", "mark", "accent", "accent_text", "header_bg", "header_text", "page_bg",
            "card_border", "heading", "text", "muted", "link", "font", "subject_recovery",
            "subject_invite", "subject_confirm", "subject_otp", "notification_prefix")


def environment() -> jinja2.Environment:
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader([str(HERE / "email"), str(HERE)]),
        variable_start_string="[[", variable_end_string="]]",
        block_start_string="[%", block_end_string="%]",
        comment_start_string="[#", comment_end_string="#]",
        trim_blocks=True, keep_trailing_newline=True,
        undefined=jinja2.StrictUndefined, autoescape=False,
    )


def load(brand_file) -> dict:
    data = yaml.safe_load(Path(brand_file).read_text()) or {}
    e = data.get("e") or {}
    missing = [k for k in REQUIRED if not str(e.get(k, "")).strip()]
    if missing:
        raise SystemExit("%s: missing %s" % (brand_file, ", ".join(missing)))
    # the brand name lands inside Django {% trans '...' %} literals
    if "'" in e["name"] or '"' in e["name"]:
        raise SystemExit("%s: e.name must not contain quotes" % brand_file)
    return e


def render(brand_file, out_dir) -> list:
    e = load(brand_file)
    env = environment()
    out = Path(out_dir)
    (out / "email").mkdir(parents=True, exist_ok=True)
    written = []
    for src in sorted((HERE / "email").iterdir()):
        if src.name.startswith("_") or src.suffix not in (".html", ".txt"):
            continue
        dest = out / "email" / src.name
        dest.write_text(env.get_template(src.name).render(e=e))
        written.append(dest)
    dest = out / "apply_email.py"
    dest.write_text(env.get_template("apply_email.py.j2").render(e=e))
    written.append(dest)
    return written


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    for p in render(sys.argv[1], sys.argv[2]):
        print(p)
