"""traefik-certs-watch: closes the "renewed cert never reaches a running
worker" gap (skfenceha only -- skfence is single-node, so the same Traefik
process that renews is the one serving, and this gap does not exist there).

traefik-worker reads acme.json once at container start and never again.
traefik-certs-dumper already dumps PEM files into a directory
traefik-worker mounts read-only but never reads from. traefik-certs-watch
is a global-mode sidecar, one per manager node (same placement as
traefik-worker), that turns that dumped output into a Traefik
file-provider `tls.certificates` dynamic file traefik-worker's own file
provider already watches -- no restart, no Docker socket, no network.

Covers the render shape only. The runtime mechanism (busybox script
correctly regenerating that file, and a real Traefik file provider picking
it up with zero restart) is proven separately against a live container in
this PR's description, since it needs an actual Traefik process and is not
practical to assert through a Jinja2 render.
"""
import pathlib

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
SKFENCEHA = V1 / "ansible/core/skfenceha/src/config/skfenceha/skfenceha.yml.j2"
SKFENCE = V1 / "ansible/core/skfence/src/config/skfence/skfence.yml.j2"
SCRIPT = V1 / "ansible/core/skfenceha/src/config/skfenceha/certs-watch.sh"
DEFAULT_WATCH_IMAGE = (
    "busybox:1.36.1-musl@sha256:"
    "3c6ae8008e2c2eedd141725c30b20d9c36b026eb796688f88205845ef17aa213"
)


def _env():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    env.filters["bool"] = lambda v: v if isinstance(v, bool) else str(v).lower() in ("yes", "true", "1", "on")
    return env


def skfenceha_services(**overrides):
    skfenceha = {"ACME_ENABLED": False}
    skfenceha.update(overrides)
    out = _env().from_string(SKFENCEHA.read_text()).render(
        app="skfenceha", env="prod", skfenceha=skfenceha, cluster_name="test", domain="example.com")
    return yaml.safe_load(out)["services"]


def skfence_services(**overrides):
    skfence = {"ACME_ENABLED": False}
    skfence.update(overrides)
    out = _env().from_string(SKFENCE.read_text()).render(
        app="skfence", env="prod", skfence=skfence, cluster_name="test", domain="example.com")
    return yaml.safe_load(out)["services"]


def test_skfence_has_no_certs_watch_sidecar():
    """skfence is single-node: the same Traefik process renews and serves,
    so there is no cross-process staleness for this sidecar to fix."""
    assert "traefik-certs-watch" not in skfence_services()


def test_certs_watch_service_exists_and_is_global():
    svc = skfenceha_services()["traefik-certs-watch"]
    assert svc["deploy"]["mode"] == "global"
    assert svc["deploy"]["placement"]["constraints"] == ["node.role == manager"]


def test_certs_watch_default_image_is_pinned_by_digest():
    svc = skfenceha_services()["traefik-certs-watch"]
    assert svc["image"] == DEFAULT_WATCH_IMAGE


def test_certs_watch_image_knob_overrides():
    pin = "busybox:1.37.0-musl@sha256:" + "d" * 64
    svc = skfenceha_services(CERTS_WATCH_IMAGE=pin)["traefik-certs-watch"]
    assert svc["image"] == pin


def test_certs_watch_interval_defaults_to_15_seconds():
    svc = skfenceha_services()["traefik-certs-watch"]
    assert "INTERVAL=15" in svc["environment"]


def test_certs_watch_interval_knob_overrides():
    svc = skfenceha_services(CERTS_WATCH_INTERVAL_SECONDS=5)["traefik-certs-watch"]
    assert "INTERVAL=5" in svc["environment"]


def test_certs_watch_mounts_certs_readonly_and_dynamic_readwrite():
    volumes = skfenceha_services()["traefik-certs-watch"]["volumes"]
    certs_mount = [v for v in volumes if "/certs:/certs" in v]
    dynamic_mount = [v for v in volumes if v.endswith(":/dynamic-out")]
    assert certs_mount and certs_mount[0].endswith(":ro"), volumes
    assert dynamic_mount, volumes
    # Read-write: must NOT carry a :ro suffix, unlike traefik-worker's own
    # mount of the exact same host directory.
    assert not dynamic_mount[0].endswith(":ro")


def test_certs_watch_has_no_docker_socket_and_no_network():
    svc = skfenceha_services()["traefik-certs-watch"]
    assert not any("docker.sock" in v for v in svc["volumes"])
    assert "networks" not in svc


def test_certs_watch_script_file_exists_and_is_posix_sh():
    assert SCRIPT.exists()
    text = SCRIPT.read_text()
    assert text.splitlines()[0] == "#!/bin/sh"
    # No bashisms that busybox ash can't parse (this bit us once already:
    # a `<(...)` process substitution during authoring).
    assert "<(" not in text


def test_certs_watch_script_defaults_match_the_compose_environment():
    text = SCRIPT.read_text()
    assert 'CERTS_DIR="${CERTS_DIR:-/certs}"' in text
    assert 'DEST_FILE="${DEST_FILE:-/dynamic-out/certs-generated.yml}"' in text
