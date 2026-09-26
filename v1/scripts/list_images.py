#!/usr/bin/env python3
"""List every container image the published v1 services reference.

Walks the Jinja compose templates under ``v1/ansible/*/*/src/**/*.j2``
(and, if a playbook ever copies a raw, non-templated compose file instead of
rendering one, that file too) looking for ``image:`` lines. Each image
reference is resolved as its own tiny Jinja template using a lenient
undefined, so a tag pulled from a ``default(...)`` filter (e.g.
``{{ x.TAG | default('1.2') }}``) resolves to that default. A reference with
no default has nothing to resolve to, so it is reported as
``<name>:<unresolved>`` and left for the caller to skip.

No Ansible install is required: this only needs jinja2 and a regex over the
one line that matters, not a real playbook render.

Usage: list_images.py [ROOT]
    ROOT defaults to the repo root (two levels up from this script).

Prints one "<service>\t<image>:<tag>" line per unique image, sorted.
"""
import sys
from pathlib import Path
import re

import jinja2

# Matches the value of a compose "image:" line, e.g. "  image: foo/bar:1.2"
IMAGE_LINE_RE = re.compile(r"^\s*image:\s*(.+?)\s*$", re.MULTILINE)

# Any real image tag/digest containing NUL bytes would already be invalid,
# so this is safe to use as an out-of-band marker for "still undefined".
_UNRESOLVED = "\x00UNRESOLVED\x00"


class _LenientUndefined(jinja2.ChainableUndefined):
    """Undefined that renders to a marker instead of raising or going blank.

    ChainableUndefined lets "{{ x.TAG }}" resolve when x itself is
    undefined (attribute access on it just yields more Undefined), which
    matches how these templates get called for real via Ansible with only
    some vars set. Overriding __str__ means a reference with a `default()`
    filter still gets its default (the filter intercepts before __str__
    is ever called), while one with no default renders to a value we can
    detect and flag as unresolved, rather than silently becoming "".
    """

    def __str__(self):
        return _UNRESOLVED


_JINJA_ENV = jinja2.Environment(undefined=_LenientUndefined)


def resolve_image_ref(raw_expr):
    """Render one image: value through Jinja using only its own defaults."""
    try:
        rendered = _JINJA_ENV.from_string(raw_expr).render()
    except jinja2.TemplateError:
        # Not valid standalone Jinja (shouldn't happen for a real image
        # line) - fall back to the literal text rather than crashing.
        return raw_expr
    if _UNRESOLVED in rendered:
        rendered = rendered.replace(":" + _UNRESOLVED, ":<unresolved>")
        rendered = rendered.replace(_UNRESOLVED, "<unresolved>")
    return rendered


def find_template_files(root):
    """Yield every Jinja compose template plus any raw compose file a
    playbook copies verbatim (none exist today, but a future service might
    ship one instead of rendering it)."""
    ansible_root = root / "v1" / "ansible"
    yield from sorted(ansible_root.glob("*/*/src/**/*.j2"))
    for playbook in sorted(ansible_root.glob("*/*/deploy_*.yml")):
        text = playbook.read_text()
        for m in re.finditer(r"copy:\s*\n\s*src:\s*\"?\{\{\s*playbook_dir\s*\}\}/(\S+?\.yml)\"?", text):
            candidate = playbook.parent / m.group(1)
            if candidate.is_file():
                yield candidate


def service_name_for(template_path, root):
    """The service is the directory right after v1/ansible/<bucket>/."""
    parts = template_path.relative_to(root / "v1" / "ansible").parts
    return parts[1]


def list_images(root):
    """Return sorted (services, image) pairs, one per unique image.

    services is a comma-joined, sorted list of every service that
    references that exact image, so the same image pulled by two services
    is checked (and reported) once.
    """
    images_to_services = {}
    for template_path in find_template_files(root):
        service = service_name_for(template_path, root)
        text = template_path.read_text()
        for raw_expr in IMAGE_LINE_RE.findall(text):
            image = resolve_image_ref(raw_expr)
            images_to_services.setdefault(image, set()).add(service)
    return sorted(
        (",".join(sorted(services)), image)
        for image, services in images_to_services.items()
    )


def main(argv):
    if len(argv) > 1:
        root = Path(argv[1]).resolve()
    else:
        root = Path(__file__).resolve().parents[2]
    for services, image in list_images(root):
        print(f"{services}\t{image}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
