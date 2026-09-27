"""skdesk (RustDesk hbbs/hbbr) speaks its own binary relay protocol over raw
TCP/UDP ports (21115-21119 tcp, 21116 udp), never HTTP, so it is never
routed through Traefik. `network_mode: host` is compose-only and silently
unsupported by `docker stack deploy` (swarm mode); the only way to bind a
swarm service to host networking is `networks: - host-net` pointed at the
engine's built-in network literally named "host" (confirmed against prod's
own working template - this framework's first draft used network_mode and
would not actually have deployed). Host networking also means the container
must be pinned to one real node via a `node.hostname ==` placement
constraint, since the scheduler cannot be left to pick any manager."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKDESK = ANSIBLE / "optional/skdesk/src/config/skdesk/skdesk.yml.j2"


def render(env_name, **skdesk_overrides):
    skdesk = {"PLACEMENT_HOSTNAME": "swarm-manager-1"}
    skdesk.update(skdesk_overrides)
    tpl_env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    out = tpl_env.from_string(SKDESK.read_text()).render(app="skdesk", env=env_name, skdesk=skdesk)
    return yaml.safe_load(out)


@pytest.mark.parametrize("env_name", ["dev", "staging", "prod"])
def test_compose_is_valid_yaml_for_every_env(env_name):
    doc = render(env_name)
    for name in ("hbbs", "hbbr"):
        svc = doc["services"][name]
        assert "network_mode" not in svc  # unsupported by docker stack deploy
        assert svc["networks"] == ["host-net"]
        assert "deploy" in svc
    assert doc["networks"]["host-net"] == {"external": True, "name": "host"}


def test_pinned_to_a_real_node_via_placement_constraint():
    doc = render("prod", PLACEMENT_HOSTNAME="swarm-manager-1")
    for name in ("hbbs", "hbbr"):
        constraints = doc["services"][name]["deploy"]["placement"]["constraints"]
        assert "node.hostname == swarm-manager-1" in constraints


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


def test_relay_hostname_and_key_are_optional_cli_flags():
    # hbbr never takes -r (it is the relay itself, not the ID server); both
    # take -k only when a pre-shared key is set.
    bare = render("prod")
    assert bare["services"]["hbbs"]["command"] == "hbbs"
    assert bare["services"]["hbbr"]["command"] == "hbbr"

    full = render("prod", RELAY_HOSTNAME="skdesk.example.com", KEY="s3cr3t-not-a-real-key")
    assert full["services"]["hbbs"]["command"] == "hbbs -r skdesk.example.com -k s3cr3t-not-a-real-key"
    assert full["services"]["hbbr"]["command"] == "hbbr -k s3cr3t-not-a-real-key"


def test_image_tag_is_overridable_per_instance():
    """skdesk.IMAGE_TAG lets an instance pin its own tag (as the old
    private-repo source did) instead of the framework's digest default -
    caught by NAM's phase-4 render-proof: the prior image line read from a
    bare, unnamespaced `skdesk_image` var with no vault wiring at all, so
    an instance's skdesk.IMAGE_TAG was silently ignored."""
    doc = render("prod", IMAGE_TAG="1.1.14")
    for name in ("hbbs", "hbbr"):
        assert doc["services"][name]["image"] == "rustdesk/rustdesk-server:1.1.14"
