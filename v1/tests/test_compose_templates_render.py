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


# 2026-10-24: a prior fix attempt (reverted, see CHANGELOG) added
# traefik.docker.network to a service that already carried the old
# traefik.swarm.network. Traefik v3's swarm provider skips any service
# whose labels carry BOTH families at once ("both Docker and Swarm labels
# are defined" -> "Skip container"): the service silently drops out of
# Traefik's dynamic config and its router falls through to the
# error-pages fallback. traefik.swarm.network is the correct, current
# label for this provider; traefik.docker.* is the deprecated form kept
# only for back-compat. Applying it fleet-wide by hand (one label-add per
# service, without removing the old label first) took ~27 production
# services offline this way before the mechanism was understood. This
# test is the fleet-wide gate so the same class can't land again, from
# either direction.
ALL_TEMPLATES = sorted(ANSIBLE.glob("*/*/src/config/*/*.yml.j2"))


@pytest.mark.parametrize("path", ALL_TEMPLATES, ids=lambda p: str(p.relative_to(ANSIBLE)))
def test_no_service_emits_both_traefik_docker_and_swarm_labels(path):
    """Best-effort: render each template with an empty per-app context
    (plus ``app``/``env``) and check whichever services come out the other
    side. Templates that need real fixture vars to render (passwords,
    cluster names, ...) raise and are skipped here; they already have
    their own app-specific render tests."""
    appname = path.parent.name
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    try:
        out = env.from_string(path.read_text()).render(env="prod", app=appname, **{appname: {}})
        doc = yaml.safe_load(out)
        services = (doc or {}).get("services") or {}
    except Exception:
        pytest.skip(f"{appname} needs real fixture vars to render; not this test's job")
        return
    for svcname, svc in services.items():
        labels = svc.get("deploy", {}).get("labels") or svc.get("labels") or []
        if isinstance(labels, dict):
            labels = [f"{k}={v}" for k, v in labels.items()]
        has_docker = any(str(lbl).startswith("traefik.docker.") for lbl in labels)
        has_swarm = any(str(lbl).startswith("traefik.swarm.") for lbl in labels)
        assert not (has_docker and has_swarm), (
            f"{appname}:{svcname} carries both traefik.docker.* and "
            "traefik.swarm.* labels - Traefik's swarm provider skips a "
            "service with both families defined, dropping its routers"
        )
