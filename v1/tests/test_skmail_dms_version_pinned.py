"""skmail's docker-mailserver image tag must be an overridable instance var
(skmail.DMS_VERSION), not a literal in the compose template - a prod
instance pinned to an older major must be able to stay there without
editing the framework. The framework's own default must itself be a pinned
semver, never `latest`: a `latest` default would silently major-upgrade
any instance that never set DMS_VERSION on its next redeploy (caught
2026-09-26 when prod turned out to be running 15.1.0, three majors behind
what :latest resolves to today)."""
import pathlib
import re

TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[1]
    / "ansible" / "optional" / "skmail" / "src" / "config" / "skmail" / "skmail.yml.j2"
)

IMAGE_LINE = re.compile(
    r"image:\s*[\"']?ghcr\.io/docker-mailserver/docker-mailserver:"
    r"\{\{\s*skmail\.DMS_VERSION\s*\|\s*default\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\}\}[\"']?"
)

SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def test_dms_version_is_an_overridable_vault_var_with_a_pinned_default():
    text = TEMPLATE.read_text()
    m = IMAGE_LINE.search(text)
    assert m, "docker-mailserver image line must read skmail.DMS_VERSION | default('X.Y.Z')"
    default = m.group(1)
    assert default != "latest", "the framework default must not be latest"
    assert SEMVER.match(default), f"default {default!r} must be a pinned semver (X.Y.Z)"
