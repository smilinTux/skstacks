"""skhub.NEXTCLOUD_IMAGE: one knob for the Nextcloud image (nextcloud, cron, notify_push).

The compose template hardcoded `nextcloud:31.0.14` three times, so an instance could not
move to a newer Nextcloud without a framework release, and an instance that had already
upgraded (Nextcloud upgrades are one-way) would be put back on the older image by the next
deploy, where the image's entrypoint refuses to start ("downgrading is not supported") and
the service crash-loops. The three services share `/var/www/html`, so they must always run
the same image: the knob sets all three. Unset renders byte-identical to before.
"""
import pathlib

import jinja2
import pytest
import yaml

SKHUB = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skhub/src/config/skhub"
COMPOSE = SKHUB / "skhub.yml.j2"
BASE = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
DEFAULT = "nextcloud:31.0.14"
NC_SERVICES = ("nextcloud", "cron", "notify_push")


def render(**over):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    return env.from_string(COMPOSE.read_text()).render(app="skhub", env="prod", skhub=dict(BASE, **over),
                                                       fence_service_name="skfenceha")


def services(**over):
    return yaml.safe_load(render(**over))["services"]


@pytest.mark.parametrize("svc", NC_SERVICES)
def test_default_is_unchanged(svc):
    assert services()[svc]["image"] == DEFAULT


@pytest.mark.parametrize("svc", NC_SERVICES)
def test_knob_sets_all_three(svc):
    img = "nextcloud:35.0.1@sha256:" + "a" * 64
    assert services(NEXTCLOUD_IMAGE=img)[svc]["image"] == img


def test_other_services_untouched_by_knob():
    a, b = services(), services(NEXTCLOUD_IMAGE="nextcloud:35.0.1")
    for name in a:
        if name not in NC_SERVICES:
            assert a[name] == b[name], name


def test_no_hardcoded_nextcloud_image_left():
    lines = [l.strip() for l in COMPOSE.read_text().splitlines() if l.strip().startswith("image:")]
    assert not [l for l in lines if l.startswith("image: nextcloud:")], "hardcoded nextcloud image"
    assert sum("skhub.NEXTCLOUD_IMAGE" in l for l in lines) == 3


def test_only_image_lines_differ_between_default_and_knob():
    a = render().splitlines()
    b = render(NEXTCLOUD_IMAGE="nextcloud:35.0.1").splitlines()
    assert len(a) == len(b)
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    assert len(diff) == 3 and all("image:" in x for x, _ in diff)


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_unset_renders_byte_identical_image_lines(env_name):
    """The docstring's promise, checked on the text: the default render must
    carry the exact line the hardcoded template had (`image: nextcloud:31.0.14`,
    unquoted), not a quoted equivalent, so an instance's rendered compose file
    and its live-parity comparison do not change on the upgrade."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True, keep_trailing_newline=True)
    text = env.from_string(COMPOSE.read_text()).render(app="skhub", env=env_name, skhub=dict(BASE),
                                                       fence_service_name="skfenceha")
    lines = [l for l in text.splitlines() if l.strip().startswith("image:") and "nextcloud:" in l]
    assert lines == ["    image: " + DEFAULT] * 3, lines
