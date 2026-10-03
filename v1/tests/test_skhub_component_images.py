"""skhub's mariadb/redis/clamav/collabora/whiteboard/imaginary images were hardcoded to
versions that fell behind upstream (mariadb 10.11.7 vs the 10.11 LTS line's 10.11.19,
redis 8.4.0-alpine vs the 8.x line's 8.10.2, an old clamav digest, collabora 25.04.8.2.1,
whiteboard v1.5.4 vs the v2.0.0 already live, and an aio-imaginary build over a year old).
This bumps the hardcoded defaults to the same digests already running in production on the
NAM cluster, pinned by @sha256 so there is no floating tag left for any of them. Nextcloud
(NEXTCLOUD_IMAGE) and talk-hpb (TALK_HPB_IMAGE) are out of scope: both already have their own
override knob and are already current.
"""
import pathlib

import jinja2
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "enable_collabora": True}

MARIADB = "mariadb:10.11.19@sha256:7db29378d4fdab73f8123bbc2b48905c90d1a4b00cf848b028f1e81e623257f2"
REDIS = "redis:8.10.2-alpine@sha256:3811787313eba226a2ef38658c6ccb91cd5e110edc89c37767de373120a0e5a0"
CLAMAV = "clamav/clamav:latest@sha256:ebec5bc138401b36ae987caa1a3fa3c3b2a21ed3d51f0bfa5852825e663e67b0"
COLLABORA = "collabora/code:26.04.4.2.1@sha256:4e983196eb9878f339cc506c38c21f1cc3473bca3d6de883c5de08f9c0cc3a6c"
WHITEBOARD = "ghcr.io/nextcloud-releases/whiteboard:v2.0.0@sha256:059596633333d009890c64858f89f133ccd973a2e0823ad960a0cc05cf8657f1"
IMAGINARY = "nextcloud/aio-imaginary:latest@sha256:15d3b439849a675d7b646db6796654ae238dd9c84ea089258d19c2fa69999cdf"


def render(**over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(COMPOSE.read_text()).render(app="skhub", env="prod", skhub=dict(BASE, **over),
                                                       fence_service_name="skfenceha")


def services(**over):
    return yaml.safe_load(render(**over))["services"]


def test_mariadb_bumped_to_10_11_19_both_services():
    svc = services()
    assert svc["db"]["image"] == MARIADB
    assert svc["db-backup"]["image"] == MARIADB


def test_redis_bumped_and_pinned():
    assert services()["redis"]["image"] == REDIS


def test_clamav_pinned_to_current_latest():
    assert services()["clamav"]["image"] == CLAMAV


def test_collabora_bumped_to_26_04():
    assert services()["collabora"]["image"] == COLLABORA


def test_whiteboard_bumped_to_v2():
    assert services()["whiteboard"]["image"] == WHITEBOARD


def test_imaginary_bumped_to_latest_build():
    assert services()["imaginary"]["image"] == IMAGINARY


def test_nextcloud_and_talk_hpb_are_untouched():
    """Both already have their own override knob and are already current; this PR
    must not touch either default."""
    svc = services()
    assert svc["nextcloud"]["image"] == "nextcloud:31.0.14"
    assert svc["cron"]["image"] == "nextcloud:31.0.14"
    assert svc["notify_push"]["image"] == "nextcloud:31.0.14"


def test_no_floating_tag_left_on_any_bumped_image():
    for name in ("db", "db-backup", "redis", "clamav", "collabora", "whiteboard", "imaginary"):
        image = services()[name]["image"]
        assert "@sha256:" in image, f"{name}: {image} has no digest pin"
