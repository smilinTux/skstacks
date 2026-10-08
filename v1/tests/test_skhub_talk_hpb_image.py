"""skhub.TALK_HPB_IMAGE: a knob for the Talk HPB (aio-talk) image.

The compose template hardcoded `ghcr.io/nextcloud-releases/aio-talk:20260122_105751`, so an
instance that moved talk-hpb to a newer aio-talk (for a newer signaling server, which a newer
Talk app asks for in its setup checks) could not keep it: the next skhub deploy put talk-hpb
back on the older image. The knob sets talk-hpb's image only.

v2.26.3: the default moved from aio-talk:20260122_105751 to a digest-pinned current aio-talk.
The old build's /start.sh ignores TURN_DOMAIN and writes no STUN/TURN into janus.jcfg, so
Janus offered only its private container address and calls outside the LAN failed ICE (an
instance that fell back to the default lost call media). The new image is dinit-based, so
when TALK_HPB_IMAGE is unset the TURN relay wrapper now execs that image's own CMD; a set
TALK_HPB_IMAGE keeps the old supervisord default (byte-identical for pinned instances), except
an image pinned to that same new digest, which gets dinit: the CMD follows the image.
"""
import pathlib

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
README = SKHUB / "README.md"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "enable_talk_hpb": True}
DEFAULT = ("ghcr.io/nextcloud-releases/aio-talk:latest"
           "@sha256:831a08f9772d188e65aee2fb59ef3785d3567a1fe2fb65f0e6fd2c17a16ffc87")
# The default image's own Config.Cmd (docker image inspect), what the relay wrapper must exec.
DEFAULT_CMD = ["dinit", "--system", "--container", "nats-server", "eturnal", "janus", "signaling"]
SUPERVISORD = ["supervisord", "-c", "/supervisord.conf"]
RELAY = {"TALK_TURN_PUBLISH_MODE": "host", "TALK_HPB_PLACEMENT_CONSTRAINTS": ["node.hostname == w1"],
         "TALK_TURN_RELAY_MIN_PORT": 62000, "TALK_TURN_RELAY_MAX_PORT": 62001,
         "TALK_TURN_RELAY_IPV4": "192.0.2.10"}
PINNED = "ghcr.io/nextcloud-releases/aio-talk:latest@sha256:" + "b" * 64


def render(env_name="prod", **over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(COMPOSE.read_text()).render(app="skhub", env=env_name, skhub=dict(BASE, **over),
                                                       fence_service_name="skfenceha")


def services(**over):
    return yaml.safe_load(render(**over))["services"]


def test_default_is_the_pinned_current_aio_talk():
    assert services()["talk-hpb"]["image"] == DEFAULT


def test_default_image_is_digest_pinned():
    import re
    assert re.fullmatch(r"ghcr\.io/nextcloud-releases/aio-talk:[\w.-]+@sha256:[0-9a-f]{64}", DEFAULT)


def test_default_image_with_turn_relay_execs_its_own_dinit_cmd():
    svc = services(**RELAY)["talk-hpb"]
    assert svc["image"] == DEFAULT
    assert svc["command"][-len(DEFAULT_CMD):] == DEFAULT_CMD
    assert "supervisord" not in svc["command"]


def test_pinned_image_without_cmd_keeps_the_old_supervisord_default():
    old = "ghcr.io/nextcloud-releases/aio-talk:20260122_105751"
    svc = services(TALK_HPB_IMAGE=old, **RELAY)["talk-hpb"]
    assert svc["command"][-3:] == SUPERVISORD


def test_pinned_to_the_new_digest_without_cmd_execs_dinit():
    """The CMD follows the image, not whether it is pinned: an instance that pins
    TALK_HPB_IMAGE to the (dinit-based) default digest must not get supervisord."""
    for image in (DEFAULT, "ghcr.io/nextcloud-releases/aio-talk@sha256:" + DEFAULT.rsplit(":", 1)[1]):
        svc = services(TALK_HPB_IMAGE=image, **RELAY)["talk-hpb"]
        assert svc["command"][-len(DEFAULT_CMD):] == DEFAULT_CMD, image
        assert "supervisord" not in svc["command"], image


def test_empty_image_key_is_the_default_image_and_dinit():
    svc = services(TALK_HPB_IMAGE="", TALK_HPB_CMD="", **RELAY)["talk-hpb"]
    assert svc["command"][-len(DEFAULT_CMD):] == DEFAULT_CMD


@pytest.mark.parametrize("image", [None, PINNED])
def test_talk_hpb_cmd_knob_wins(image):
    over = dict(RELAY, TALK_HPB_CMD=["custom", "--flag"])
    if image:
        over["TALK_HPB_IMAGE"] = image
    assert services(**over)["talk-hpb"]["command"][-2:] == ["custom", "--flag"]


def test_no_relay_no_command():
    assert "command" not in services()["talk-hpb"]


def test_knob_sets_talk_hpb_image():
    assert services(TALK_HPB_IMAGE=PINNED)["talk-hpb"]["image"] == PINNED


def test_knob_touches_no_other_service():
    a, b = services(), services(TALK_HPB_IMAGE=PINNED)
    for name in a:
        if name != "talk-hpb":
            assert a[name] == b[name], name


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_unset_renders_the_default_image_line(env_name):
    """One unquoted image line, the pinned default."""
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
