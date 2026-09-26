"""Every remote image a published v1 service pulls must be pinned: either an
explicit non-`latest` tag, or a `@sha256:...` digest. An unpinned `:latest`
(or an unresolved instance var with no default) means a routine redeploy
silently pulls whatever upstream shipped since the last deploy, which is
exactly the class of incident test_skmail_dms_version_pinned.py caught for
docker-mailserver (prod three majors behind what `:latest` resolves to).

A tag can also look pinned while still floating: a variant-only tag like
`redis:alpine` or a major-only tag like `registry:2` or `postgres:16` moves
every time upstream repoints it, the same as `:latest` does, it just moves
less often. Both are rejected below unless the reference also carries a
`@sha256:...` digest, which is the only thing that actually stops moving.

Images the framework builds itself (the per-app `-error-pages` sidecar) are
exempt: they are never pulled from a registry, so there is no upstream
`:latest` to drift out from under a deploy.
"""
import pathlib
import re
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "list_images.py"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# A tag that is nothing but a major version, optionally with a non-numeric
# variant suffix ("2", "16", "pg16", "16-alpine", "27-cli", ...). None of
# these commit to a minor/patch, so upstream can and does repoint them.
_MAJOR_ONLY_RE = re.compile(r"^\D*\d+(-\w+)?$")


def run_list_images(root):
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(root)],
        capture_output=True, text=True, check=True,
    )
    rows = {}
    for line in result.stdout.splitlines():
        services, image = line.split("\t")
        rows[image] = services
    return rows


def _is_locally_built(image):
    # e.g. "skfence-<unresolved>-error-pages:latest" / "<unresolved>-<unresolved>-error-pages:latest"
    return "-error-pages:" in image


def _is_pinned(image):
    if "@sha256:" in image:
        return True
    tag = image.rsplit(":", 1)[-1]
    if tag in ("latest", "lts", "stable", "release", "edge", "main", "<unresolved>"):
        return False
    if not any(ch.isdigit() for ch in tag):
        return False
    if _MAJOR_ONLY_RE.match(tag):
        return False
    return True


def test_every_remote_image_is_pinned():
    rows = run_list_images(REPO_ROOT)
    remote_images = [image for image in rows if not _is_locally_built(image)]
    assert remote_images, "expected at least one remote image from list_images.py"
    unpinned = sorted(
        f"{image} (services: {rows[image]})"
        for image in remote_images
        if not _is_pinned(image)
    )
    assert not unpinned, "unpinned remote image(s) found (need an explicit tag or a digest):\n" + "\n".join(unpinned)


def test_locally_built_images_are_still_detected_as_such():
    """Guards the exemption itself: if the compose templates ever stop
    emitting an `-error-pages` sidecar, this must fail loudly rather than
    let the exemption silently stop applying to anything."""
    rows = run_list_images(REPO_ROOT)
    locally_built = [image for image in rows if _is_locally_built(image)]
    assert locally_built, "expected at least one locally-built -error-pages image"


def test_is_pinned_rejects_variant_only_tags_with_no_digit():
    for image in ("redis:alpine", "debian:bookworm", "node:slim"):
        assert not _is_pinned(image), image


def test_is_pinned_rejects_major_only_numeric_tags():
    for image in (
        "registry:2",
        "postgres:16",
        "postgres:16-alpine",
        "pgvector/pgvector:pg16",
        "docker:27-cli",
    ):
        assert not _is_pinned(image), image


def test_is_pinned_accepts_full_version_tags():
    for image in (
        "postgres:16.2",
        "postgres:16.14",
        "vikunja/vikunja:0.24.6",
        "redis:7.4.9-alpine",
        "traefik:v3.6.2",
    ):
        assert _is_pinned(image), image


def test_is_pinned_accepts_any_digest_pinned_reference_regardless_of_tag():
    for image in (
        "rustdesk/rustdesk-server:latest@sha256:10818ec05b179039c6660f4d8e74b303f0db2858bbad2b18e24992ea22d54cd6",
        "pgvector/pgvector:pg16@sha256:0a07c4114ba6d1d04effcce3385e9f5ce305eb02e56a3d35948a415a52f193ec",
    ):
        assert _is_pinned(image), image
