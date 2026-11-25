"""skmail can route its mail ports through the skfenceha Traefik workers
(skmail.VIA_TRAEFIK + skfenceha.MAIL_ENTRYPOINTS) instead of host-mode ports
pinned to the mail-vip node, so mail fails over with Traefik.

Through a TCP proxy every connection would appear to come from the worker's
overlay IP, which breaks Postfix's client checks (SPF, RBLs, postscreen) and
Dovecot's auth logging and penalties. The routers therefore use a PROXY
protocol v2 serversTransport (TCP loadBalancer.proxyProtocol is deprecated
since Traefik v3.5.2) to separate PROXY-only docker-mailserver listeners,
the layout docker-mailserver's "mail server behind a proxy" tutorial
recommends, so the standard ports stay plain for overlay clients.

Both knobs default off and then render exactly what they did before.
"""
import pathlib
import subprocess

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKMAIL = ANSIBLE / "optional/skmail/src/config/skmail"
FENCE = ANSIBLE / "core/skfenceha/src/config/skfenceha"
MAIL_PORTS = [25, 465, 587, 143, 993]
# entrypoint -> docker-mailserver PROXY-only listener
PROXY_PORTS = {"smtp": 12525, "submissions": 10465, "submission": 10587, "imap": 10143, "imaps": 10993}
ON = [True, "true", "yes", 1]


def _j2():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)


def _skmail(name, env="prod", **overrides):
    skmail = {"CLUSTERNAME": "demo", "DOMAIN": "example.test", **overrides}
    return _j2().from_string((SKMAIL / name).read_text()).render(
        env=env, skmail=skmail, fence_service_name="skfenceha")


def _fence(name, **overrides):
    return _j2().from_string((FENCE / name).read_text()).render(
        app="skfenceha", env="prod", skfenceha={"ACME_ENABLED": False, **overrides},
        cluster_name="test", domain="example.test")


def _mail_service(**overrides):
    return yaml.safe_load(_skmail("skmail.yml.j2", **overrides))["services"]["skmail"]


# --- skmail compose ---------------------------------------------------------

def test_default_keeps_host_ports_and_mail_vip_pin():
    svc = _mail_service()
    assert sorted(p["published"] for p in svc["ports"]) == sorted(MAIL_PORTS)
    assert all(p["mode"] == "host" for p in svc["ports"])
    assert "node.labels.mail-vip == true" in svc["deploy"]["placement"]["constraints"]
    assert "networks" not in svc
    assert svc["deploy"]["labels"] == ["traefik.enable=false"]


@pytest.mark.parametrize("value", ON)
def test_via_traefik_drops_host_ports_and_mail_vip_keeps_manager(value):
    svc = _mail_service(VIA_TRAEFIK=value)
    assert "ports" not in svc
    constraints = svc["deploy"]["placement"]["constraints"]
    assert constraints == ["node.role == manager"]
    assert svc["networks"] == ["cloud-public-prod"]
    assert svc["deploy"]["replicas"] == 1


def test_via_traefik_labels_one_proxied_tcp_router_per_port():
    labels = _mail_service(VIA_TRAEFIK=True)["deploy"]["labels"]
    assert "traefik.enable=true" in labels
    assert "traefik.swarm.network=cloud-public-prod" in labels
    assert not any(label.startswith("traefik.http.") for label in labels)
    assert not any(".tls" in label for label in labels)  # passthrough, DMS terminates TLS
    for ep, port in PROXY_PORTS.items():
        name = f"skmail-prod-{ep}"
        assert f"traefik.tcp.routers.{name}.rule=HostSNI(`*`)" in labels
        assert f"traefik.tcp.routers.{name}.entrypoints={ep}" in labels
        assert f"traefik.tcp.routers.{name}.service={name}" in labels
        assert f"traefik.tcp.services.{name}.loadbalancer.server.port={port}" in labels
        assert f"traefik.tcp.services.{name}.loadbalancer.serverstransport=skmail-proxy-v2@file" in labels
    # the deprecated per-service label is not used
    assert not any("proxyprotocol" in label.lower() for label in labels)


# --- skmail user-patches.sh ---------------------------------------------------

def test_default_user_patches_has_no_proxy_listeners():
    out = _skmail("user-patches.sh.j2")
    assert "upstream_proxy_protocol" not in out
    assert "haproxy" not in out


def test_via_traefik_user_patches_adds_the_proxy_listeners():
    out = _skmail("user-patches.sh.j2", VIA_TRAEFIK=True)
    assert "12525/inet/postscreen_upstream_proxy_protocol=haproxy" in out
    assert "10587/inet/smtpd_upstream_proxy_protocol=haproxy" in out
    assert "10465/inet/smtpd_upstream_proxy_protocol=haproxy" in out
    assert "postscreen_cache_map = proxy:btree:" in out
    assert "port = 10143" in out and "port = 10993" in out
    assert out.count("haproxy = yes") == 2
    # the standard listeners are left alone (no PROXY on them)
    assert "smtp/inet/postscreen_upstream_proxy_protocol" not in out
    # a conf.d file, never the instance-owned dovecot.cf
    assert "/etc/dovecot/conf.d/95-skmail-proxy-protocol.conf" in out
    assert "/tmp/docker-mailserver/dovecot.cf" not in out


@pytest.mark.parametrize("env,subnet", [("prod", "172.16.200.0/24"),
                                        ("staging", "172.16.201.0/24"),
                                        ("dev", "172.16.202.0/24")])
def test_trusted_network_defaults_to_the_cloud_public_subnet(env, subnet):
    out = _skmail("user-patches.sh.j2", env=env, VIA_TRAEFIK=True)
    assert f"haproxy_trusted_networks = {subnet}\n" in out
    assert f"grep -qF '{subnet}'" in out  # mynetworks guard


def test_trusted_networks_override():
    out = _skmail("user-patches.sh.j2", VIA_TRAEFIK=True,
                  PROXY_TRUSTED_NETWORKS=["192.0.2.0/24", "198.51.100.0/24"])
    assert "haproxy_trusted_networks = 192.0.2.0/24 198.51.100.0/24\n" in out
    out = _skmail("user-patches.sh.j2", VIA_TRAEFIK=True, PROXY_TRUSTED_NETWORKS="192.0.2.0/24")
    assert "haproxy_trusted_networks = 192.0.2.0/24\n" in out


@pytest.mark.parametrize("value", [False, True])
def test_user_patches_parses(value, tmp_path):
    script = tmp_path / "user-patches.sh"
    script.write_text(_skmail("user-patches.sh.j2", VIA_TRAEFIK=value))
    subprocess.run(["bash", "-n", str(script)], check=True)


# --- skfenceha ----------------------------------------------------------------

def _worker_ports(**overrides):
    svc = yaml.safe_load(_fence("skfenceha.yml.j2", **overrides))["services"]["traefik-worker"]
    return {p["published"]: p["mode"] for p in svc["ports"]}


def test_fence_default_has_no_mail_ports_or_transport():
    ports = _worker_ports()
    assert not set(MAIL_PORTS) & set(ports)
    entrypoints = yaml.safe_load(_fence("traefik-worker.yml.j2"))["entryPoints"]
    assert not set(PROXY_PORTS) & set(entrypoints)
    assert "tcp" not in yaml.safe_load(_fence("services.yml.j2"))


@pytest.mark.parametrize("value", ON)
def test_fence_mail_entrypoints_publish_host_ports_and_define_transport(value):
    ports = _worker_ports(MAIL_ENTRYPOINTS=value)
    for port in MAIL_PORTS + [80, 443]:
        assert ports[port] == "host"
    entrypoints = yaml.safe_load(_fence("traefik-worker.yml.j2", MAIL_ENTRYPOINTS=value))["entryPoints"]
    expected = {"smtp": ":25", "submissions": ":465", "submission": ":587", "imap": ":143", "imaps": ":993"}
    for name, address in expected.items():
        assert entrypoints[name] == {"address": address}  # plain TCP, no TLS or http block
    transports = yaml.safe_load(_fence("services.yml.j2", MAIL_ENTRYPOINTS=value))["tcp"]["serversTransports"]
    assert transports == {"skmail-proxy-v2": {"proxyProtocol": {"version": 2}}}


def test_proxied_smtp_never_trusts_a_claimed_address():
    """A forged PROXY header could claim 127.0.0.1; the proxied listeners
    must relay only after SASL auth (empty mynetworks), and postscreen on
    12525 must hand off to its own pass service, since an -o on the
    postscreen line does not reach the shared smtpd pass service."""
    src = (pathlib.Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skmail"
           / "src" / "config" / "skmail" / "user-patches.sh.j2").read_text()
    for needle in ("12525/inet/smtpd_service_name=smtpd-proxyprotocol",
                   "smtpd-proxyprotocol/pass/mynetworks=",
                   "10587/inet/mynetworks=",
                   "10465/inet/mynetworks="):
        assert needle in src, needle
