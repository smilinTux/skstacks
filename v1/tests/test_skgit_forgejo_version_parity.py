"""Live-parity check: prod runs Forgejo on the floating codeberg.org/forgejo:12
tag while the framework pins 15.0.9. skgit.FORGEJO_VERSION already exists as
an instance-settable var (f69f146) - this locks in that an instance can pin
its own tag (or digest) without a framework change, so the parity vault PR
only needs to set the var, not patch the template."""
import pathlib

import jinja2
import yaml

SKGIT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skgit/src/config/skgit/skgit.yml.j2"


def render(**skgit):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(SKGIT.read_text()).render(env="dev", app="skgit", skgit=skgit, _base_domain="example.com")
    return yaml.safe_load(out)["services"]["forgejo"]["image"]


def test_forgejo_version_defaults_to_pinned_release():
    assert render() == "codeberg.org/forgejo/forgejo:15.0.9"


def test_instance_can_pin_the_live_floating_major_as_an_exact_tag():
    assert render(FORGEJO_VERSION="12.0.3") == "codeberg.org/forgejo/forgejo:12.0.3"
