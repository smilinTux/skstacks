"""Compose templates must render to valid YAML whichever way their feature
flags are set (skform produced invalid YAML with SKSTOR_ENABLED false: a
whitespace-control tag ate the newline before `deploy:`)."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKFORM = ANSIBLE / "optional/skform/src/config/skform/skform.yml.j2"


@pytest.mark.parametrize("skstor", [False, True])
def test_skform_compose_is_valid_yaml(skstor):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    out = env.from_string(SKFORM.read_text()).render(
        app="skform", env="dev", skform={"SKSTOR_ENABLED": skstor, "TOFU_VERSION": "1.8.0"})
    doc = yaml.safe_load(out)
    svc = next(iter(doc["services"].values()))
    assert "deploy" in svc and "networks" in svc
    assert ("cloud-public-dev" in svc["networks"]) == skstor
    assert "traefik.enable=false" in svc.get("labels", [])  # CLI only: never routed
    # the official image lives on ghcr (Docker Hub has none) and its entrypoint
    # is `tofu`, so idling needs the entrypoint replaced, not the command
    assert svc["image"].startswith("ghcr.io/opentofu/opentofu:")
    assert svc.get("entrypoint") == ["sleep", "infinity"]

SKMESH = ANSIBLE / "optional/skmesh/src/config/skmesh/skmesh.yml.j2"

_SKMESH_BASE_VARS = {
    "CLUSTERNAME": "demo",
    "DOMAIN": "example.com",
    "SSO_HOST_IP": "10.0.0.5",
    "POSTGRES_PASSWORD": "x",
    "TURN_SECRET": "y",
}


def test_skmesh_dashboard_defaults_to_the_upstream_digest_pin():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(SKMESH.read_text()).render(env="prod", skmesh=dict(_SKMESH_BASE_VARS))
    doc = yaml.safe_load(out)
    image = doc["services"]["dashboard"]["image"]
    assert image.startswith("netbirdio/dashboard:v") and "@sha256:" in image


def test_skmesh_dashboard_image_is_overridable_per_instance():
    """NAM runs a private, unpublished ghcr.io/smilintux/skmesh fork as its
    dashboard. The framework default must not silently swap that out from
    under an instance that depends on it - DASHBOARD_IMAGE lets an instance
    vault pin its own image (including a private one) instead."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    custom = dict(_SKMESH_BASE_VARS, DASHBOARD_IMAGE="ghcr.io/smilintux/skmesh:latest")
    out = env.from_string(SKMESH.read_text()).render(env="prod", skmesh=custom)
    doc = yaml.safe_load(out)
    assert doc["services"]["dashboard"]["image"] == "ghcr.io/smilintux/skmesh:latest"


# 2026-10-24: a production skmesh instance returned intermittent
# curl 000s. Root cause: dashboard is attached to two overlay networks
# (skmesh-{env} + cloud-public-{env}) with no traefik.docker.network label,
# so Traefik's docker provider could non-deterministically resolve the
# backend to the skmesh-{env} IP, which Traefik has no interface on - the
# connection then hung until the client's own timeout fired.
# management/signal/relay had already been hand-patched live on NAM to
# traefik.docker.network (and resolved correctly on every sampled request);
# dashboard still carried only the old traefik.swarm.network label, which
# is not a label any Traefik version reads, so it was no better than
# having no network label at all. Every multi-network skmesh service must
# pin traefik.docker.network so this can't recur on any of them.
@pytest.mark.parametrize("service", ["management", "signal", "relay", "dashboard"])
@pytest.mark.parametrize("auth_mode", ["standalone", "authentik"])
def test_skmesh_multi_network_services_pin_traefik_docker_network(service, auth_mode):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    vars_ = dict(_SKMESH_BASE_VARS, AUTH_MODE=auth_mode)
    out = env.from_string(SKMESH.read_text()).render(env="prod", skmesh=vars_)
    doc = yaml.safe_load(out)
    svc = doc["services"][service]
    assert set(svc["networks"]) == {"skmesh-prod", "cloud-public-prod"}, (
        f"{service} is on {svc['networks']!r}: if it ever drops to one "
        "network the docker.network label below becomes unnecessary, not "
        "wrong, but this test documents the precondition for the bug."
    )
    labels = svc["deploy"]["labels"]
    assert "traefik.docker.network=cloud-public-prod" in labels
    # the stale, non-functional label must not come back either.
    assert not any(lbl.startswith("traefik.swarm.network") for lbl in labels)


def test_skmesh_traefik_docker_network_label_follows_env():
    """A non-prod instance's label must point at ITS own cloud-public
    network, not prod's - a copy-paste of the literal prod string would
    silently cross-wire two instances' edges."""
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(SKMESH.read_text()).render(env="dev", skmesh=dict(_SKMESH_BASE_VARS))
    doc = yaml.safe_load(out)
    labels = doc["services"]["dashboard"]["deploy"]["labels"]
    assert "traefik.docker.network=cloud-public-dev" in labels
    assert "traefik.docker.network=cloud-public-prod" not in labels
