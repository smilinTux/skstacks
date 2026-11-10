"""Wave 2 data-store majors: redis 8 (skgallery, skorch, sksso), valkey 9
(skpeek) and registry 3 (skreg).

None of these needs a pre-deploy guard. Redis, valkey and registry open the
previous major's data in place (an RDB or AOF file written by an older major
loads in a newer one; the registry filesystem layout is unchanged). What they
need is an escape hatch: once a newer major has written its RDB (redis 8.10
writes RDB version 15), the older major can no longer open it, so an operator
who wants to stay on or return to the old major pins the image var in the vault.
"""
import pathlib

import jinja2
import pytest
import yaml

from test_router_middlewares_knob import render

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
DIGEST = "@sha256:"


def _svc(app, name, env="prod", **over):
    return yaml.safe_load(render(app, env, **over))["services"][name]


def _render_plain(app, **ctx):
    tpl = (ANSIBLE / app / "src/config" / app / f"{app}.yml.j2").read_text()
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return yaml.safe_load(env.from_string(tpl).render(app=app, env="prod", **ctx))["services"]


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_default_images_are_the_new_majors_and_digest_pinned(env):
    assert _svc("skgallery", "redis", env)["image"].startswith("redis:8.10.3-alpine" + DIGEST)
    assert _svc("skorch", "redis", env)["image"].startswith("redis:8.10.3-alpine" + DIGEST)
    assert _svc("skpeek", "redis", env)["image"].startswith("docker.io/valkey/valkey:9.1.3-alpine" + DIGEST)


def test_sksso_and_skreg_default_images():
    sksso = _render_plain("sksso", sksso={"DOMAIN": "example.test", "CLUSTERNAME": "demo"})
    assert sksso["redis"]["image"].startswith("docker.io/library/redis:8.10.3" + DIGEST)
    skreg = _render_plain("skreg", vault_skreg_registry_http_secret="x")
    assert skreg["registry"]["image"].startswith("registry:3.1.2" + DIGEST)


def test_operator_can_stay_on_the_old_major_with_a_vault_pin():
    # skgallery: REDIS_VERSION is the tag; a plain tag drops the digest.
    assert _svc("skgallery", "redis", REDIS_VERSION="7.4.12-alpine")["image"] == "redis:7.4.12-alpine"
    assert _svc("skorch", "redis", REDIS_IMAGE="redis:7.4.12-alpine")["image"] == "redis:7.4.12-alpine"
    # skpeek: `redis_image` is a top-level var, not under skpeek.*
    peek = _render_plain("skpeek", skpeek={}, redis_image="docker.io/valkey/valkey:8.1.11-alpine")
    assert peek["redis"]["image"] == "docker.io/valkey/valkey:8.1.11-alpine"
    sksso = _render_plain("sksso", sksso={"REDIS_IMAGE": "docker.io/library/redis:7.4.12"})
    assert sksso["redis"]["image"] == "docker.io/library/redis:7.4.12"
    skreg = _render_plain("skreg", skreg_image="registry:2.8.3", vault_skreg_registry_http_secret="x")
    assert skreg["registry"]["image"] == "registry:2.8.3"


def test_redis_commands_use_flags_that_redis_8_still_accepts():
    gallery = _svc("skgallery", "redis")["command"]
    assert "--appendonly yes" in gallery and "--maxmemory-policy noeviction" in gallery
    assert _svc("skorch", "redis")["command"] == "redis-server /usr/local/etc/redis/redis.conf"
    assert _svc("skpeek", "redis")["command"].startswith("valkey-server --save")


def test_skreg_is_env_only_and_uses_nothing_registry_3_removed():
    """registry 3 dropped the oss and swift storage drivers and moved the image's
    default config to /etc/distribution/config.yml. The template mounts no config
    file and sets no storage driver, so the image default (filesystem driver on
    /var/lib/registry) applies, and only REGISTRY_HTTP_* env is passed."""
    text = (ANSIBLE / "skreg/src/config/skreg/skreg.yml.j2").read_text()
    for removed in ("REGISTRY_STORAGE_OSS", "REGISTRY_STORAGE_SWIFT", "/etc/docker/registry", "schema1"):
        assert removed not in text, removed
    svc = _render_plain("skreg", vault_skreg_registry_http_secret="x")["registry"]
    for item in svc["environment"]:
        assert item.startswith(("REGISTRY_HTTP_SECRET=", "REGISTRY_HTTP_ADDR=")), item
    assert "/var/data/skreg-prod/data:/var/lib/registry" in svc["volumes"]
