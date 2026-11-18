"""skmesh's bundled coturn must actually be reachable, and Talk must be able to use it.

Before: the coturn service set ``network_mode: host``, which ``docker stack
deploy`` ignores, and published no ports, so nothing listened on 3478/5349 on
the host and every NetBird client reported STUN and TURN Unavailable. It also
passed the shared secret on the command line (visible in the service spec),
handed clients static TURN_USER/TURN_PASSWORD that coturn's use-auth-secret
mode refuses, and its rendered turnserver.conf was never mounted.

Now: coturn joins Swarm's predefined ``host`` network, reads a mounted config
(secret off the command line), relays only to public peers, optionally serves
TLS/DTLS on 5349 with the Traefik certificate skfence's dumper writes, and
management hands out TURN REST credentials by default when coturn is bundled.
skhub gets ``TALK_SIGNALING: internal`` to point Talk at that server without
the HPB and to drop a stale ``signaling_servers``.
"""
import json
import pathlib
import subprocess

import jinja2
import pytest
import yaml

from image_pins import is_pinned

ROOT = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional"
SKMESH = ROOT / "skmesh"
SRC = SKMESH / "src/config/skmesh"
SKHUB = ROOT / "skhub"

BASE = {
    "CLUSTERNAME": "demo",
    "DOMAIN": "example.test",
    "SSO_HOST_IP": "192.0.2.5",
    "POSTGRES_PASSWORD": "pw",
    "NETBIRD_AUTH_CLIENT_ID": "netbird",
    "NETBIRD_DATASTORE_ENC_KEY": "enc",
    "NETBIRD_RELAY_AUTH_SECRET": "relaysecret",
    "NETBIRD_RELAY_ENDPOINT": "rels://skmesh.demo.example.test:443",
    "TURN_SECRET": "turn-shared-secret-0123456789abcdef0123",
    "TURN_USER": "u",
    "TURN_PASSWORD": "p",
}
COTURN = dict(BASE, DEPLOY_COTURN=True, COTURN_EXTERNAL_IP="203.0.113.7", COTURN_RELAY_IP="192.0.2.9",
              TURN_MIN_PORT=49152, TURN_MAX_PORT=49999)
TLS = dict(COTURN, TURN_TLS=True)


def _render(name, skmesh):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string((SRC / name).read_text()).render(env="prod", skmesh=skmesh, inventory_hostname="n1")


def _compose(skmesh):
    return yaml.safe_load(_render("skmesh.yml.j2", skmesh))


def _conf(skmesh):
    return [l for l in _render("turnserver.conf.j2", skmesh).splitlines() if l and not l.startswith("#")]


def _mgmt(skmesh):
    return json.loads(_render("management.json.j2", skmesh))


# --- the coturn service ------------------------------------------------------

def test_coturn_is_on_swarms_host_network_not_network_mode():
    c = _compose(COTURN)
    svc = c["services"]["coturn"]
    assert "network_mode" not in svc
    assert svc["networks"] == ["hostnet"]
    assert c["networks"]["hostnet"] == {"external": True, "name": "host"}
    assert "ports" not in svc  # host network: no per-port mapping for the relay range


def test_no_host_network_without_coturn():
    c = _compose(dict(BASE))
    assert "coturn" not in c["services"]
    assert "hostnet" not in c["networks"]


def test_secret_is_not_in_the_service_spec():
    svc = _compose(COTURN)["services"]["coturn"]
    assert COTURN["TURN_SECRET"] not in json.dumps(svc)
    assert "env_file" not in svc  # it used to get all of skmesh.env (relay secret, DB DSN, ...)
    assert "/var/data/config/skmesh-prod/turnserver.conf:/etc/coturn/turnserver.conf:ro" in svc["volumes"]
    assert svc["entrypoint"] == ["/bin/sh", "/coturn-run.sh"]


def test_coturn_image_is_pinned():
    assert is_pinned(_compose(COTURN)["services"]["coturn"]["image"], "coturn/coturn", "4.18.")


def test_tls_mounts_the_dumped_certificate_and_requests_it_from_traefik():
    c = _compose(TLS)
    assert "/var/data/runtime/skfence-prod/certs:/certs-dump:ro" in c["services"]["coturn"]["volumes"]
    labels = c["services"]["dashboard"]["deploy"]["labels"]
    assert "traefik.http.routers.skmesh-turn-cert.rule=Host(`turn.demo.example.test`)" in labels
    assert "traefik.http.routers.skmesh-turn-cert.service=noop@internal" in labels
    assert "traefik.http.routers.skmesh-turn-cert.tls.certresolver=main" in labels


def test_no_cert_router_or_mount_without_tls():
    c = _compose(COTURN)
    assert not any("skmesh-turn-cert" in l for l in c["services"]["dashboard"]["deploy"]["labels"])
    assert not any(v.endswith(":/certs-dump:ro") for v in c["services"]["coturn"]["volumes"])


def test_turn_domain_and_cert_dir_knobs():
    c = _compose(dict(TLS, TURN_DOMAIN="turn.example.org", TURN_TLS_DUMP_DIR="/srv/certs"))
    assert "/srv/certs:/certs-dump:ro" in c["services"]["coturn"]["volumes"]
    assert "cert=/certs-dump/turn.example.org/certificate.crt" in _conf(dict(TLS, TURN_DOMAIN="turn.example.org"))
    assert any("Host(`turn.example.org`)" in l for l in c["services"]["dashboard"]["deploy"]["labels"])


# --- turnserver.conf -------------------------------------------------------------

def test_conf_uses_the_shared_secret_scheme_only():
    conf = _conf(COTURN)
    assert "use-auth-secret" in conf
    assert f"static-auth-secret={COTURN['TURN_SECRET']}" in conf
    assert "lt-cred-mech" not in conf  # static long-term users never worked with use-auth-secret


def test_conf_maps_external_to_relay_address_and_range():
    conf = _conf(COTURN)
    # Never "<public>/<relay>": coturn would whitelist the relay address as a peer.
    assert "external-ip=203.0.113.7\n" in conf and "external-ip=203.0.113.7/" not in conf
    assert "relay-ip=192.0.2.9" in conf
    assert "min-port=49152" in conf and "max-port=49999" in conf
    assert "realm=turn.demo.example.test" in conf


def test_conf_keeps_an_explicit_external_mapping():
    conf = _conf(dict(COTURN, COTURN_EXTERNAL_IP="203.0.113.7/10.1.2.3"))
    assert "external-ip=203.0.113.7/10.1.2.3" in conf


@pytest.mark.parametrize("rng", ["10.0.0.0-10.255.255.255", "172.16.0.0-172.31.255.255",
                                 "192.168.0.0-192.168.255.255", "100.64.0.0-100.127.255.255",
                                 "127.0.0.0-127.255.255.255", "169.254.0.0-169.254.255.255"])
def test_conf_never_relays_into_private_ranges(rng):
    conf = _conf(COTURN)
    assert f"denied-peer-ip={rng}" in conf
    assert "allow-loopback-peers" not in conf  # coturn 4.18 denies loopback peers by default


def test_conf_drops_privileges():
    conf = _conf(COTURN)
    assert "proc-user=nobody" in conf and "proc-group=nogroup" in conf


def test_conf_tls_on_5349_only_with_tls():
    tls, plain = _conf(TLS), _conf(COTURN)
    assert "tls-listening-port=5349" in tls and "cert=/certs-dump/turn.demo.example.test/certificate.crt" in tls
    assert "pkey=/certs-dump/turn.demo.example.test/privatekey.key" in tls and "no-tls" not in tls
    assert "dtls" in tls  # coturn 4.18 starts DTLS only when asked
    assert "no-tls" in plain and "dtls" not in plain and "tls-listening-port=5349" not in plain


def test_conf_has_no_options_coturn_4_18_rejects():
    for conf in (_conf(COTURN), _conf(TLS)):
        for gone in ("no-cli", "no-tlsv1", "no-tlsv1_1", "no-dtls", "no-loopback-peers", "lt-cred-mech"):
            assert gone not in conf, gone


@pytest.mark.parametrize("skmesh", [COTURN, TLS], ids=["plain", "tls"])
def test_run_script_is_valid_sh(skmesh, tmp_path):
    script = tmp_path / "run.sh"
    script.write_text(_render("coturn-run.sh.j2", skmesh))
    assert subprocess.run(["sh", "-n", str(script)], capture_output=True).returncode == 0
    text = script.read_text()
    assert "turnserver -c /etc/coturn/turnserver.conf" in text
    assert ("certificate changed" in text) == (skmesh is TLS)


def test_run_script_restarts_on_a_changed_certificate(tmp_path):
    """Drive the TLS script with fake turnserver/sleep: a changed cert ends it with 0."""
    certs = tmp_path / "certs-dump" / "turn.demo.example.test"
    certs.mkdir(parents=True)
    (certs / "certificate.crt").write_text("old")
    (certs / "privatekey.key").write_text("key")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "turnserver").write_text("#!/bin/sh\nexec sleep 30\n")
    (bin_ / "turnserver").chmod(0o755)
    flag = tmp_path / "slept"
    (bin_ / "sleep").write_text(
        f"#!/bin/sh\nif [ \"$1\" = 1 ] && [ ! -e {flag} ]; then touch {flag}; "
        f"echo new > {certs}/certificate.crt; fi\nexec /bin/sleep 0.2\n")
    (bin_ / "sleep").chmod(0o755)
    script = _render("coturn-run.sh.j2", dict(TLS, COTURN_CERT_CHECK_S=1)).replace("/certs-dump/", f"{tmp_path}/certs-dump/")
    script = script.replace("turnserver -c", f"{bin_}/turnserver -c").replace("    sleep ", f"    {bin_}/sleep ")
    r = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0
    assert "certificate changed" in r.stdout


# --- management.json -------------------------------------------------------------

def test_bundled_coturn_defaults_to_time_based_credentials():
    m = _mgmt(COTURN)
    assert m["TURNConfig"]["TimeBasedCredentials"] is True
    assert m["TURNConfig"]["Secret"] == COTURN["TURN_SECRET"]
    assert m["TURNConfig"]["Turns"][0]["URI"] == "turn:turn.demo.example.test:3478"
    assert m["Stuns"][0]["URI"] == "stun:turn.demo.example.test:3478"


def test_explicit_false_is_respected_and_unbundled_default_unchanged():
    assert _mgmt(dict(COTURN, TURN_TIME_BASED_CREDENTIALS=False))["TURNConfig"]["TimeBasedCredentials"] is False
    assert _mgmt(dict(BASE))["TURNConfig"]["TimeBasedCredentials"] is False


def test_turn_domain_drives_the_default_uris():
    m = _mgmt(dict(COTURN, TURN_DOMAIN="turn.example.org"))
    assert m["TURNConfig"]["Turns"][0]["URI"] == "turn:turn.example.org:3478"
    assert m["Stuns"][0]["URI"] == "stun:turn.example.org:3478"


# --- playbooks ------------------------------------------------------------------

@pytest.mark.parametrize("env", ["prod", "dev"])
def test_skmesh_playbook_renders_the_script_and_checks_the_range(env):
    text = (SKMESH / f"deploy_skmesh-{env}.yml").read_text()
    assert '"coturn-run.sh.j2"' in text
    assert "Check the bundled coturn settings" in text


@pytest.mark.parametrize("env", ["prod", "dev", "staging"])
def test_skhub_internal_signaling_task(env):
    plays = yaml.safe_load((SKHUB / f"deploy_skhub-{env}.yml").read_text())
    tasks = [t for p in plays for t in p.get("tasks", [])]
    t = next(t for t in tasks if t.get("name") == "Configure Talk for internal signaling and an external TURN server")
    assert t["no_log"] is True
    assert "config:app:delete spreed signaling_servers" in t["shell"]
    assert '"schemes":"turns","server":"{{ skhub.TURN_DOMAIN }}:{{ skhub.TALK_TURNS_PORT }}"' in t["shell"]
    assert "TALK_SIGNALING" in t["when"] and "enable_talk_hpb" in t["when"]
    pre = [t for p in plays for t in p.get("pre_tasks", []) + p.get("tasks", [])]
    checks = " ".join(json.dumps(t.get("assert", {})) for t in pre)
    assert "_sig in ['', 'internal']" in checks
    assert "not (_internal and _stun_talk)" in checks


def test_playbooks_restart_coturn_when_its_config_changes():
    for env in ("prod", "dev"):
        pb = (SKMESH / f"deploy_skmesh-{env}.yml").read_text()
        assert "register: skmesh_env_templates" in pb
        assert "Restart coturn when its config changed" in pb
        assert "docker service update --force --detach {{ app }}-{{ env }}_coturn" in pb
        assert "'turnserver.conf.j2', 'coturn-run.sh.j2'" in pb
