"""skfence.DUMPER_IMAGE / skfenceha.DUMPER_IMAGE: pin the certs-dumper image.

Both skfence's and skfenceha's traefik-certs-dumper service previously
hardcoded ldez/traefik-certs-dumper:v2.10.0 with no override, a version that
panics ("unsupported key type: ''") on acme.json written by Traefik 3.7+
(the account KeyType field was dropped). Bumped to v2.11.4 by digest, with
the same per-role override knob convention as TRAEFIK_IMAGE: an instance can
stage a tactical version ahead of or instead of a full framework release.
"""
import pathlib

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
SKFENCE = V1 / "ansible/core/skfence/src/config/skfence/skfence.yml.j2"
SKFENCEHA = V1 / "ansible/core/skfenceha/src/config/skfenceha/skfenceha.yml.j2"
DEFAULT_IMAGE = (
    "ldez/traefik-certs-dumper:v2.11.4@sha256:"
    "a34b27edf8e5e96a215035a6bfba00d769d2638f8444a5dd6dc27299d75f43c5"
)


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def skfence_services(**overrides):
    skfence = {"ACME_ENABLED": False}
    skfence.update(overrides)
    out = _env().from_string(SKFENCE.read_text()).render(
        app="skfence", env="prod", skfence=skfence, cluster_name="test", domain="example.com")
    return yaml.safe_load(out)["services"]


def skfenceha_services(**overrides):
    skfenceha = {"ACME_ENABLED": False}
    skfenceha.update(overrides)
    out = _env().from_string(SKFENCEHA.read_text()).render(
        app="skfenceha", env="prod", skfenceha=skfenceha, cluster_name="test", domain="example.com")
    return yaml.safe_load(out)["services"]


def test_skfence_dumper_default_is_not_the_broken_v2_10_0():
    assert skfence_services()["traefik-certs-dumper"]["image"] == DEFAULT_IMAGE
    assert "v2.10.0" not in skfence_services()["traefik-certs-dumper"]["image"]


def test_skfence_dumper_override_replaces_it():
    pin = "ldez/traefik-certs-dumper:v2.12.0@sha256:" + "a" * 64
    s = skfence_services(DUMPER_IMAGE=pin)
    assert s["traefik-certs-dumper"]["image"] == pin


def test_skfenceha_dumper_default_is_not_the_broken_v2_10_0():
    assert skfenceha_services()["traefik-certs-dumper"]["image"] == DEFAULT_IMAGE
    assert "v2.10.0" not in skfenceha_services()["traefik-certs-dumper"]["image"]


def test_skfenceha_dumper_override_replaces_it():
    pin = "ldez/traefik-certs-dumper:v2.12.0@sha256:" + "b" * 64
    s = skfenceha_services(DUMPER_IMAGE=pin)
    assert s["traefik-certs-dumper"]["image"] == pin


def test_skfence_and_skfenceha_dumper_knobs_are_independent():
    """Same independence contract as TRAEFIK_IMAGE (test_traefik_image_knob.py):
    different top-level vault dicts, even though an instance runs only one
    of skfence or skfenceha at a time."""
    s = skfence_services(DUMPER_IMAGE="ldez/traefik-certs-dumper:v2.12.0@sha256:" + "c" * 64)
    assert s["traefik-certs-dumper"]["image"] == "ldez/traefik-certs-dumper:v2.12.0@sha256:" + "c" * 64
    h = skfenceha_services()
    assert h["traefik-certs-dumper"]["image"] == DEFAULT_IMAGE


def test_skfenceha_traefik_worker_still_mounts_certs_dir_readonly():
    """The worker's existing (previously unused) /certs:ro mount is what
    traefik-certs-watch now relies on; make sure this PR didn't drop it."""
    volumes = skfenceha_services()["traefik-worker"]["volumes"]
    assert any(v.endswith("/certs:/certs:ro") for v in volumes)
