"""imageref.parse_image_ref must correctly split every image-reference shape
`docker service inspect` hands back, so the registry lookup asks the right
question of the right host."""
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

from imageref import parse_image_ref, is_docker_hub  # noqa: E402


def test_docker_hub_unqualified_image():
    ref = parse_image_ref("redis:7-alpine")
    assert ref.registry == "docker.io"
    assert ref.namespace == "library"
    assert ref.repo == "redis"
    assert ref.tag == "7-alpine"
    assert ref.digest is None
    assert is_docker_hub(ref)


def test_pinned_tag_and_digest_on_third_party_registry():
    ref = parse_image_ref("ghcr.io/org/repo:v1.2.3@sha256:" + "a" * 64)
    assert ref.registry == "ghcr.io"
    assert ref.namespace == "org"
    assert ref.repo == "repo"
    assert ref.tag == "v1.2.3"
    assert ref.digest == "sha256:" + "a" * 64
    assert not is_docker_hub(ref)


def test_lscr_io_namespaced_image():
    ref = parse_image_ref("lscr.io/linuxserver/bookstack:v26.09-ls285")
    assert ref.registry == "lscr.io"
    assert ref.namespace == "linuxserver"
    assert ref.repo == "bookstack"
    assert ref.tag == "v26.09-ls285"


def test_docker_hub_namespaced_image():
    ref = parse_image_ref("nextcloud/aio-imaginary:latest")
    assert ref.registry == "docker.io"
    assert ref.namespace == "nextcloud"
    assert ref.repo == "aio-imaginary"


def test_empty_reference_is_rejected():
    with pytest.raises(ValueError):
        parse_image_ref("")


def test_reference_with_no_tag_or_digest_is_rejected():
    with pytest.raises(ValueError):
        parse_image_ref("just-a-name")
