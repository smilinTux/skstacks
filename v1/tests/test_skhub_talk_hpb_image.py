"""skhub.TALK_HPB_IMAGE: a knob for the Talk HPB (aio-talk) image.

The compose template hardcoded `ghcr.io/nextcloud-releases/aio-talk:20260122_105751`, so an
instance that moved talk-hpb to a newer aio-talk (for a newer signaling server, which a newer
Talk app asks for in its setup checks) could not keep it: the next skhub deploy put talk-hpb
back on the older image. The knob sets talk-hpb's image only; unset renders byte-identical.
"""
import pathlib

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
README = SKHUB / "README.md"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "enable_talk_hpb": True}
DEFAULT = "ghcr.io/nextcloud-releases/aio-talk:20260122_105751"
PINNED = "ghcr.io/nextcloud-releases/aio-talk:latest@sha256:" + "b" * 64


def render(env_name="prod", **over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(COMPOSE.read_text()).render(app="skhub", env=env_name, skhub=dict(BASE, **over),
                                                       fence_service_name="skfenceha")


def services(**over):
    return yaml.safe_load(render(**over))["services"]


def test_default_is_unchanged():
    assert services()["talk-hpb"]["image"] == DEFAULT


def test_knob_sets_talk_hpb_image():
    assert services(TALK_HPB_IMAGE=PINNED)["talk-hpb"]["image"] == PINNED


def test_knob_touches_no_other_service():
    a, b = services(), services(TALK_HPB_IMAGE=PINNED)
    for name in a:
        if name != "talk-hpb":
            assert a[name] == b[name], name


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_unset_renders_byte_identical_image_line(env_name):
    """Unset must keep the exact unquoted line the hardcoded template had, so a
    rendered compose file and its live-parity comparison do not change."""
    lines = [l for l in render(env_name).splitlines() if "aio-talk" in l]
    assert lines == ["    image: " + DEFAULT], lines


def test_only_the_image_line_differs():
    a = render().splitlines()
    b = render(TALK_HPB_IMAGE=PINNED).splitlines()
    assert len(a) == len(b)
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    assert diff == [("    image: " + DEFAULT, "    image: " + PINNED)]


def test_knob_with_turn_host_mode_keeps_the_relay_command():
    """The relay-address command execs the image's own supervisord; the knob
    must not drop it when TURN host mode is on."""
    svc = services(TALK_HPB_IMAGE=PINNED, TALK_TURN_PUBLISH_MODE="host",
                   TALK_HPB_PLACEMENT_CONSTRAINTS=["node.hostname == w1"],
                   TALK_TURN_RELAY_MIN_PORT=62000, TALK_TURN_RELAY_MAX_PORT=62001,
                   TALK_TURN_RELAY_IPV4="192.0.2.10")["talk-hpb"]
    assert svc["image"] == PINNED
    assert svc["command"][-3:] == ["supervisord", "-c", "/supervisord.conf"]


def test_no_hardcoded_aio_talk_image_left():
    # "aio-talk:" (with the colon), not "aio-talk-recording:" (talk-recording's
    # own image, its own knob TALK_RECORDING_IMAGE -- see test_skhub_recording_ai.py).
    lines = [l.strip() for l in COMPOSE.read_text().splitlines() if l.strip().startswith("image:")]
    talk = [l for l in lines if "aio-talk:" in l]
    assert talk and all("skhub.TALK_HPB_IMAGE" in l for l in talk), talk


def test_readme_documents_the_knob():
    assert "TALK_HPB_IMAGE" in README.read_text()
