"""Live-parity hook: skpeek (SearXNG) on skstack01 sets INSTANCE_NAME,
SEARXNG_DEBUG, SEARXNG_PRIVACY, SEARXNG_SECRET_KEY, SEARXNG_THEME as
container env vars (verified via docker service inspect skpeek-prod_skpeek,
names only), while the framework moved instance_name/debug/theme into
file-based settings.yml with no vault hook, and had no file/env equivalent
for SEARXNG_PRIVACY at all. Confirms the settings.yml fields are now
vault-driven with unchanged defaults, and closes the SEARXNG_PRIVACY-style
gap generically via skpeek.extra_env."""
import pathlib

import jinja2
import yaml

SKPEEK = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skpeek"


def render_settings(**skpeek):
    p = SKPEEK / "src/skpeek/etc/settings.yml.j2"
    j2 = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = j2.from_string(p.read_text()).render(skpeek=skpeek)
    doc = yaml.safe_load(out)
    return {**doc["general"], "default_theme": doc["ui"]["default_theme"]}


def render_env(**skpeek):
    p = SKPEEK / "src/config/skpeek/skpeek.env.j2"
    j2 = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    base = {"CLUSTERNAME": "cluster1", "DOMAIN": "example.com"}
    base.update(skpeek)
    return j2.from_string(p.read_text()).render(env="dev", skpeek=base, cluster_name="cluster1", domain="example.com")


def test_settings_defaults_are_unchanged():
    g = render_settings()
    assert g["debug"] is False
    assert g["instance_name"] == "SKPeek"
    assert g["default_theme"] == "simple"


def test_instance_can_pin_instance_name_debug_and_theme_from_the_vault():
    g = render_settings(INSTANCE_NAME="SKPeek Prod", DEBUG=True, THEME="simple")
    assert g["instance_name"] == "SKPeek Prod"
    assert g["debug"] is True
    assert g["default_theme"] == "simple"


def test_extra_env_absent_by_default():
    out = render_env()
    assert "SEARXNG_PRIVACY=" not in out


def test_extra_env_closes_the_searxng_privacy_gap():
    out = render_env(extra_env={"SEARXNG_PRIVACY": "1"})
    assert "SEARXNG_PRIVACY=1" in out
