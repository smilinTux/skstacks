"""skhub talk-recording: resource limits and reservations are knobs.

The recorder runs a headless Firefox plus software ffmpeg, so its CPU limit
sets the recorded frame rate. On an older-generation Xeon without AVX2 a 1080p
recording at the historic 2.0 CPU limit records at about 2 fps, so operators
need to raise it. Defaults stay at the historic values (limits 2.0 CPUs / 4G,
reservations 1.0 CPU / 1G), so the default render is unchanged.
"""
import pathlib

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub"
COMPOSE = SKHUB / "src/config/skhub/skhub.yml.j2"
README = SKHUB / "README.md"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com", "TALK_RECORDING_ENABLED": True,
        "TALK_RECORDING_NODE": "w2", "TALK_RECORDING_SECRET": "x" * 32}
KNOBS = ("TALK_RECORDING_CPUS_LIMIT", "TALK_RECORDING_CPUS_RESERVATION",
         "TALK_RECORDING_MEMORY_LIMIT", "TALK_RECORDING_MEMORY_RESERVATION")


def _resources(**over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    text = env.from_string(COMPOSE.read_text()).render(app="skhub", env="prod", skhub=dict(BASE, **over),
                                                       fence_service_name="skfenceha")
    return yaml.safe_load(text)["services"]["talk-recording"]["deploy"]["resources"]


def test_defaults_are_the_historic_values():
    assert _resources() == {"reservations": {"memory": "1G", "cpus": "1.0"},
                            "limits": {"memory": "4G", "cpus": "2.0"}}


def test_default_block_text_is_unchanged():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    text = env.from_string(COMPOSE.read_text()).render(app="skhub", env="prod", skhub=BASE,
                                                       fence_service_name="skfenceha")
    block = text.split("  talk-recording:", 1)[1]
    assert ("      resources:\n        reservations:\n          memory: 1G\n          cpus: '1.0'\n"
            "        limits:\n          memory: 4G\n          cpus: '2.0'\n") in block


def test_all_four_knobs_reach_the_service():
    res = _resources(TALK_RECORDING_CPUS_LIMIT="6.0", TALK_RECORDING_CPUS_RESERVATION="3.5",
                     TALK_RECORDING_MEMORY_LIMIT="8G", TALK_RECORDING_MEMORY_RESERVATION="2G")
    assert res == {"reservations": {"memory": "2G", "cpus": "3.5"}, "limits": {"memory": "8G", "cpus": "6.0"}}


@pytest.mark.parametrize("value", [6, 4.5])
def test_numeric_cpu_values_stay_strings(value):
    """A vault value written as a bare number still renders as a quoted string,
    which is the form compose and the schema expect."""
    res = _resources(TALK_RECORDING_CPUS_LIMIT=value)
    assert res["limits"]["cpus"] == str(value)


def test_one_knob_leaves_the_others_at_default():
    res = _resources(TALK_RECORDING_CPUS_LIMIT="5.0")
    assert res == {"reservations": {"memory": "1G", "cpus": "1.0"}, "limits": {"memory": "4G", "cpus": "5.0"}}


def test_knobs_only_touch_talk_recording():
    for knob in KNOBS:
        assert COMPOSE.read_text().count(knob) == 1, knob
        assert COMPOSE.read_text().index(knob) > COMPOSE.read_text().index("  talk-recording:")


def test_readme_documents_every_knob_with_its_default():
    text = README.read_text()
    row = next(line for line in text.splitlines() if "`TALK_RECORDING_CPUS_LIMIT`" in line)
    for knob in KNOBS:
        assert f"`{knob}`" in row, knob
    assert "`2.0`, `1.0`, `4G`, `1G`" in row
