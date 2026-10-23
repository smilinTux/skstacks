"""skfence.TRAEFIK_IMAGE / skfenceha.TRAEFIK_IMAGE: pin the Traefik image.

Both skfence's single traefik service and skfenceha's traefik-acme and
traefik-worker services previously hardcoded the image literal with no
override. This knob lets an instance stage a tactical version (ahead of or
instead of a full framework release) by pinning a digest, same convention
as every other <SVC>_IMAGE knob in this framework. Unset renders the
current framework default (traefik:v3.7.16@sha256:...) in every service
that carries it; skfenceha's two Traefik services always move together
(one knob, both services), since they are the same binary in the same HA
deployment.
"""
import pathlib

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
SKFENCE = V1 / "ansible/core/skfence/src/config/skfence/skfence.yml.j2"
SKFENCEHA = V1 / "ansible/core/skfenceha/src/config/skfenceha/skfenceha.yml.j2"
DEFAULT_IMAGE = "traefik:v3.7.16@sha256:abb2b9c1b132b5b0be40d09c85bf8d3dab2a59fa0eb3aa955ab8be5efbb73de6"


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


def test_skfence_default_is_the_pinned_digest():
    assert skfence_services()["traefik"]["image"] == DEFAULT_IMAGE


def test_skfence_override_replaces_it():
    s = skfence_services(TRAEFIK_IMAGE="traefik:v3.7.17@sha256:" + "0" * 64)
    assert s["traefik"]["image"] == "traefik:v3.7.17@sha256:" + "0" * 64


def test_skfenceha_default_is_the_pinned_digest_on_both_services():
    s = skfenceha_services()
    assert s["traefik-acme"]["image"] == DEFAULT_IMAGE
    assert s["traefik-worker"]["image"] == DEFAULT_IMAGE


def test_skfenceha_override_moves_both_services_together():
    pin = "traefik:v3.7.17@sha256:" + "1" * 64
    s = skfenceha_services(TRAEFIK_IMAGE=pin)
    assert s["traefik-acme"]["image"] == pin
    assert s["traefik-worker"]["image"] == pin


def test_skfence_and_skfenceha_knobs_are_independent():
    """Setting one role's TRAEFIK_IMAGE must not affect the other's default
    (different top-level vault dicts, even though an instance runs only one
    of the two roles at a time)."""
    s = skfence_services(TRAEFIK_IMAGE="traefik:v3.7.17@sha256:" + "2" * 64)
    assert s["traefik"]["image"] == "traefik:v3.7.17@sha256:" + "2" * 64
    h = skfenceha_services()
    assert h["traefik-acme"]["image"] == DEFAULT_IMAGE
    assert h["traefik-worker"]["image"] == DEFAULT_IMAGE
