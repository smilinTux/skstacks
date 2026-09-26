"""Every remote image a published v1 service pulls must be pinned: either an
explicit non-`latest` tag, or a `@sha256:...` digest. An unpinned `:latest`
(or an unresolved instance var with no default) means a routine redeploy
silently pulls whatever upstream shipped since the last deploy, which is
exactly the class of incident test_skmail_dms_version_pinned.py caught for
docker-mailserver (prod three majors behind what `:latest` resolves to).

Images the framework builds itself (the per-app `-error-pages` sidecar) are
exempt: they are never pulled from a registry, so there is no upstream
`:latest` to drift out from under a deploy.
"""
import pathlib
import subprocess
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "list_images.py"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


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
