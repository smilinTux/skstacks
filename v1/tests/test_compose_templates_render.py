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
    assert image.startswith("netbirdio/dashboard@sha256:")


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
