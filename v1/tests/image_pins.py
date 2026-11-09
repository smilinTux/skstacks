"""Shared helpers so tests follow the image defaults Renovate maintains.

Renovate rewrites every `image:` default in the compose templates. A test that
compares against a literal digest breaks on every such bump, so tests either:
- compare renders through `normalize_images()` (baselines guard the knob wiring,
  not the image version), or
- check a reference with `is_pinned()` (repo, version line, @sha256 digest), or
- read the shipped default with `template_default()`.
"""
import re

_DIGEST = r"@sha256:[0-9a-f]{64}"
_IMAGE_LINE = re.compile(r"""^(\s*-?\s*image:\s*["']?)[^\s"']+(["']?)\s*$""", re.M)


def normalize_images(text):
    """Replace every rendered `image:` value with a placeholder."""
    return _IMAGE_LINE.sub(r"\1<image>\2", text)


def is_pinned(ref, repo, tag_prefix=""):
    """True when ref is `repo:<tag starting with tag_prefix>@sha256:<64 hex>`."""
    return re.fullmatch(re.escape(repo) + ":" + re.escape(tag_prefix) + r"[\w.-]*" + _DIGEST, ref or "") is not None


def template_default(text, knob):
    """The literal default of `{{ <ctx>.<knob> | default('<ref>') }}` in a template."""
    m = re.search(r"\." + re.escape(knob) + r"\s*\|\s*default\(\s*['\"]([^'\"]+)['\"]", text)
    assert m, f"no literal default for {knob}"
    return m.group(1)
