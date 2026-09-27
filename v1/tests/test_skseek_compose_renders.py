"""skseek (Perplexica) must render to valid YAML, stay pinned by digest, and
reach its SearXNG backend (skpeek) over the shared skpeek-<env> overlay
network rather than a public URL."""
import pathlib
import re

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKSEEK = ANSIBLE / "optional/skseek/src/config/skseek/skseek.yml.j2"
CONFIG_TOML = ANSIBLE / "optional/skseek/src/config/skseek/config.toml.j2"

SKSEEK_VARS = {
    "PORT": "3000",
    "CLUSTERNAME": "cluster1",
    "DOMAIN": "example.com",
    "NEXT_PUBLIC_API_URL": "https://skseek.cluster1.example.com/api",
    "NEXT_PUBLIC_WS_URL": "wss://skseek.cluster1.example.com/ws",
    "SEARXNG_API_URL": "http://skpeek-prod_skpeek:8080",
}


def render(env_name):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    out = tpl_env.from_string(SKSEEK.read_text()).render(
        app="skseek", env=env_name, cluster_name="cluster1", domain="example.com", skseek=SKSEEK_VARS)
    return yaml.safe_load(out)


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_compose_is_valid_yaml_for_every_env(env_name):
    doc = render(env_name)
    svc = doc["services"]["skseek"]
    assert "deploy" in svc
    assert f"skseek-{env_name}" in svc["networks"]
    assert f"cloud-public-{env_name}" in svc["networks"]
    assert f"skpeek-{env_name}" in svc["networks"]


def test_image_is_pinned_by_digest():
    doc = render("prod")
    image = doc["services"]["skseek"]["image"]
    assert image.startswith("itzcrazykns1337/perplexica:")
    assert "@sha256:" in image


def test_data_is_a_shared_bind_mount_not_a_named_volume():
    doc = render("prod")
    volumes = doc["services"]["skseek"]["volumes"]
    assert "/var/data/skseek-prod/data:/home/perplexica/data:rw" in volumes
    assert "volumes" not in doc


def test_config_toml_mount_is_read_only():
    doc = render("prod")
    volumes = doc["services"]["skseek"]["volumes"]
    assert any(v.endswith("config.toml:/home/perplexica/config.toml:ro") for v in volumes)


SECRET_DEFAULT = re.compile(
    r"\{\{\s*[\w.]*(secret|password|passwd|token|api_?key|private_?key)[\w.]*\s*\|\s*default\(\s*['\"]([^'\"]+)['\"]",
    re.IGNORECASE,
)


def test_config_toml_renders_and_has_no_literal_secret_defaults():
    # v1/tests/test_no_default_secrets.py already scans the whole ansible
    # tree for this; this is a template-level tripwire on the one file here
    # that actually carries provider API keys.
    text = CONFIG_TOML.read_text()
    assert not SECRET_DEFAULT.search(text)

    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = tpl_env.from_string(text).render(env="prod", skseek=SKSEEK_VARS)
    assert "API_KEY" in out
    assert "SEARXNG" in out
