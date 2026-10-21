"""Parse a Docker/OCI image reference into its registry, repository, tag and digest.

Handles the shapes `docker service ls`/`inspect` actually hand back:

    redis:7-alpine
    redis@sha256:abc...
    redis:7-alpine@sha256:abc...
    ghcr.io/org/repo:tag@sha256:abc...
    lscr.io/linuxserver/bookstack:v26.09-ls285
    registry.example.org:5000/ns/repo:tag

No network access here, this is pure string parsing.
"""
from __future__ import annotations

from dataclasses import dataclass

# Registries that are not Docker Hub get the generic OCI v2 anonymous-token
# path; anything else (including a bare "library" image) is Docker Hub.
_KNOWN_NON_HUB_HOSTS = {"ghcr.io", "lscr.io", "quay.io"}


@dataclass(frozen=True)
class ImageRef:
    registry: str       # "docker.io", "ghcr.io", "lscr.io", "quay.io", or a custom host
    namespace: str       # "library" for an unqualified Docker Hub image
    repo: str            # repository name without namespace
    tag: str | None       # may be None if only a digest pin was given
    digest: str | None    # "sha256:..." or None

    @property
    def path(self) -> str:
        """The repository path a registry API expects, e.g. "library/redis"."""
        return f"{self.namespace}/{self.repo}"

    @property
    def full_repo(self) -> str:
        """registry-qualified repo, no tag/digest, e.g. "ghcr.io/org/repo"."""
        return f"{self.registry}/{self.path}" if self.namespace != "library" or self.registry != "docker.io" else self.repo


def _looks_like_host(segment: str) -> bool:
    """A leading path segment is a registry host if it has a dot, a colon
    (port), or is exactly "localhost" - the same heuristic the Docker CLI
    uses to tell "ghcr.io/org/repo" apart from "org/repo" on Docker Hub."""
    return "." in segment or ":" in segment or segment == "localhost"


def parse_image_ref(raw: str) -> ImageRef:
    """Parse one image reference string into an ImageRef.

    Raises ValueError on something that isn't a plausible image reference
    (e.g. empty string) so a bad inventory row fails loudly instead of
    silently checking the wrong thing.
    """
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty image reference")

    digest = None
    name_and_tag = raw
    if "@" in raw:
        name_and_tag, digest = raw.rsplit("@", 1)
        if not digest.startswith("sha256:"):
            raise ValueError(f"unsupported digest algorithm in {raw!r}")

    # Split registry/namespace/repo from an optional ":tag". A port in the
    # host (e.g. "registry.local:5000/repo:tag") means the FIRST colon isn't
    # necessarily the tag separator, so split on the last "/"-free colon.
    if "/" in name_and_tag:
        host_candidate, rest = name_and_tag.split("/", 1)
    else:
        host_candidate, rest = "", name_and_tag

    if host_candidate and _looks_like_host(host_candidate):
        registry = host_candidate
        remainder = rest
    else:
        registry = "docker.io"
        remainder = name_and_tag

    tag = None
    if ":" in remainder.rsplit("/", 1)[-1]:
        remainder, tag = remainder.rsplit(":", 1)

    if "/" in remainder:
        namespace, repo = remainder.rsplit("/", 1)
    else:
        namespace, repo = ("library", remainder) if registry == "docker.io" else ("", remainder)

    if not repo:
        raise ValueError(f"no repository name in {raw!r}")
    if tag is None and digest is None:
        raise ValueError(f"no tag or digest in {raw!r}")

    return ImageRef(registry=registry, namespace=namespace or "library", repo=repo, tag=tag, digest=digest)


def is_docker_hub(ref: ImageRef) -> bool:
    return ref.registry == "docker.io"
