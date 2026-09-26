"""skdesk (RustDesk hbbs/hbbr) speaks its own binary relay protocol over raw
TCP/UDP ports (21115-21119 tcp, 21116 udp), never HTTP, so it cannot go
through Traefik or a published-port overlay network the way every other v1
service does. It runs in network_mode: host instead, matching prod's live
topology exactly (network driver=host, EndpointSpec.Ports=null)."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKDESK = ANSIBLE / "optional/skdesk/src/config/skdesk/skdesk.yml.j2"


def render(env_name):
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    out = tpl_env.from_string(SKDESK.read_text()).render(
        app="skdesk",
        env=env_name,
        cluster_name="cluster1",
        domain="example.com",
        skdesk={"RELAY_HOST": f"skdesk.cluster1.example.com"},
    )
    return yaml.safe_load(out)


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_compose_is_valid_yaml_for_every_env(env_name):
    doc = render(env_name)
    for name in ("hbbs", "hbbr"):
        svc = doc["services"][name]
        assert svc["network_mode"] == "host"
        assert "networks" not in svc
        assert "deploy" in svc


def test_never_routed_through_traefik():
    doc = render("prod")
    for name in ("hbbs", "hbbr"):
        labels = doc["services"][name]["deploy"]["labels"]
        assert "traefik.enable=false" in labels


def test_data_dir_is_a_shared_bind_mount_not_a_named_volume():
    doc = render("prod")
    for name in ("hbbs", "hbbr"):
        volumes = doc["services"][name]["volumes"]
        assert "/var/data/skdesk-prod/data:/root" in volumes
    assert "volumes" not in doc  # no top-level named-volume declarations


def test_image_is_pinned_by_digest():
    doc = render("prod")
    for name in ("hbbs", "hbbr"):
        image = doc["services"][name]["image"]
        assert image.startswith("rustdesk/rustdesk-server:")
        assert "@sha256:" in image


def test_hbbr_takes_no_relay_host_argument():
    # -r <relay-host> only makes sense for hbbs (the ID/rendezvous server);
    # hbbr is the relay itself and takes no arguments.
    doc = render("prod")
    assert doc["services"]["hbbs"]["command"][:2] == ["hbbs", "-r"]
    assert doc["services"]["hbbr"]["command"] == ["hbbr"]
