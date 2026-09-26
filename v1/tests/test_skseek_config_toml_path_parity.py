"""Live-parity hook: skseek's config.toml lives at
/var/data/skseek-prod/config/config.toml on skstack01 (verified via
`docker service inspect skseek-prod_skseek`), not
/var/data/config/skseek-prod/config.toml the framework hardcodes.
skseek.CONFIG_TOML_PATH makes the path instance-settable, wired through both
the compose mount and the ansible template destination so they can never
drift apart, with the framework default unchanged."""
import pathlib
import re

import jinja2
import yaml

SKSEEK = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skseek"

ENVS = ["dev", "staging", "prod"]


def render_mount(env="dev", **skseek):
    p = SKSEEK / "src/config/skseek/skseek.yml.j2"
    j2 = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = j2.from_string(p.read_text()).render(env=env, app="skseek", skseek=skseek, skseek_image="x")
    vols = yaml.safe_load(out)["services"]["skseek"]["volumes"]
    return [str(v) for v in vols if "config.toml" in str(v)][0]


def test_default_mount_source_is_unchanged():
    assert render_mount().startswith("/var/data/config/skseek-dev/config.toml:")


def test_instance_can_pin_the_live_path():
    mount = render_mount(CONFIG_TOML_PATH="/var/data/skseek-prod/config/config.toml", env="prod")
    assert mount.startswith("/var/data/skseek-prod/config/config.toml:")


def test_every_env_playbook_renders_config_toml_to_the_same_configurable_path():
    for e in ENVS:
        pb = (SKSEEK / f"deploy_skseek-{e}.yml").read_text()
        assert "skseek.CONFIG_TOML_PATH | default('/var/data/config/' ~ app ~ '-' ~ env ~ '/config.toml')" in pb, e
        # parent-directory creation task must use the same expression, so an
        # overridden path's directory always exists before the template runs
        assert pb.count("skseek.CONFIG_TOML_PATH | default('/var/data/config/' ~ app ~ '-' ~ env ~ '/config.toml')") == 2, e
