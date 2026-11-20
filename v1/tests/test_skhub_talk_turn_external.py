"""skhub TALK_TURN_EXTERNAL: the Talk HPB with an external TURN server.

The bundled skmesh coturn (#450) runs on the host network of the managers and
binds 3478 there. With enable_talk_hpb, talk-hpb publishes 3478 too (ingress
mode: on EVERY node through the routing mesh; host mode: on its own node) and
runs eturnal, so the HPB and the bundled coturn could not share a host.

TALK_TURN_EXTERNAL: true keeps the HPB (signaling, Janus) and hands TURN to
the server at TURN_DOMAIN: talk-hpb publishes no 3478, the default dinit
image does not start eturnal (signaling depends on nats-server and janus
only), Janus and Talk use TURN_DOMAIN:3478 with turn_secret (the external
server's shared secret, TURN REST credentials), and TALK_TURNS_PORT may add
the server's TLS port. Unset, everything renders as before.
"""
import pytest
import yaml

import test_skhub_internal_ingress_and_stun as stun
import test_skhub_talk_turn_modes as modes

EXT = {"TALK_TURN_EXTERNAL": True, "TURN_DOMAIN": "turn.example.com"}
DINIT_NO_ETURNAL = ["dinit", "--system", "--container", "nats-server", "janus", "signaling"]


def _talk(**over):
    return stun.services(**over)["talk-hpb"]


# --- render ------------------------------------------------------------------

def test_external_publishes_no_turn_port_and_runs_no_eturnal():
    t = _talk(**EXT)
    assert "ports" not in t
    assert t["command"] == DINIT_NO_ETURNAL


def test_default_still_publishes_3478_and_keeps_the_image_cmd():
    t = _talk()
    assert t["ports"] == ["3478:3478/tcp", "3478:3478/udp"]
    assert "command" not in t


@pytest.mark.parametrize("off", [False, "false", "no", None])
def test_external_off_renders_byte_identical(off):
    over = {} if off is None else {"TALK_TURN_EXTERNAL": off}
    assert stun.render(**over) == stun.render()


def test_external_with_an_explicit_cmd_uses_it():
    cmd = ["dinit", "--system", "--container", "nats-server", "janus", "signaling", "extra"]
    assert _talk(**EXT, TALK_HPB_CMD=cmd)["command"] == cmd


def test_external_with_a_pinned_supervisord_image_keeps_its_cmd_but_publishes_nothing():
    t = _talk(**EXT, TALK_HPB_IMAGE="ghcr.io/nextcloud-releases/aio-talk:20260122_105751")
    assert "ports" not in t and "command" not in t


def test_external_changes_only_talk_hpb():
    base, ext = stun.services(), stun.services(**EXT)
    assert set(base) == set(ext)
    for name in base:
        if name != "talk-hpb":
            assert ext[name] == base[name], name


def test_talk_env_points_janus_at_the_external_turn():
    lines = modes.env_lines(**EXT, turn_secret="x" * 32)
    assert "TURN_DOMAIN=turn.example.com" in lines and "TALK_PORT=3478" in lines


# --- healthcheck (#458) -------------------------------------------------------
# aio-talk's /healthcheck.sh also probes eturnal (TALK_PORT, eturnalctl status),
# so without eturnal the image's own check never passes and Swarm kills a healthy
# HPB every minute. External mode probes only signaling and Janus.

def _hc(**over):
    return _talk(**over).get("healthcheck")


def test_external_healthcheck_probes_signaling_and_janus_only():
    hc = _hc(**EXT)
    assert hc is not None and hc["test"][0] == "CMD-SHELL"
    probe = hc["test"][1]
    assert "8081" in probe and "/api/v1/stats" in probe and "8188" in probe
    assert "eturnal" not in probe and "3478" not in probe and "TALK_PORT" not in probe
    assert hc["start_period"] == "60s" and hc["retries"] == 3


def test_default_mode_keeps_the_image_healthcheck():
    assert _hc() is None


def test_external_with_eturnal_in_an_explicit_cmd_keeps_the_image_healthcheck():
    cmd = ["dinit", "--system", "--container", "nats-server", "eturnal", "janus", "signaling"]
    assert _hc(**EXT, TALK_HPB_CMD=cmd) is None


def test_external_with_a_pinned_supervisord_image_keeps_the_image_healthcheck():
    assert _hc(**EXT, TALK_HPB_IMAGE="ghcr.io/nextcloud-releases/aio-talk:20260122_105751") is None


@pytest.mark.parametrize("ext", [{}, EXT])
def test_healthcheck_test_override(ext):
    test = ["CMD", "/healthcheck.sh"]
    assert _hc(**ext, TALK_HPB_HEALTHCHECK_TEST=test)["test"] == test


# --- the guard -----------------------------------------------------------------

GOOD = [
    EXT,
    dict(EXT, TALK_TURNS_PORT=5349),
    dict(EXT, TALK_STUN_SERVERS=["turn.example.com:3478"]),
    dict(EXT, TALK_TURN_PUBLISH_MODE="ingress"),
]
BAD = {
    "external without the HPB": dict(EXT, enable_talk_hpb=False),
    "external without TURN_DOMAIN": {"TALK_TURN_EXTERNAL": True},
    "external with a bad TURN_DOMAIN": dict(EXT, TURN_DOMAIN="turn example"),
    "external with a short secret": dict(EXT, turn_secret="short"),
    "external with TLS 443 (routes to eturnal)": dict(EXT, TALK_TURN_TLS_443=True),
    "external with host mode": dict(EXT, TALK_TURN_PUBLISH_MODE="host", TALK_HPB_PLACEMENT_CONSTRAINTS=["node.hostname == w"]),
    "external with a relay range": dict(EXT, TALK_TURN_RELAY_MIN_PORT=62000, TALK_TURN_RELAY_MAX_PORT=62099),
    "external with a relay address": dict(EXT, TALK_TURN_RELAY_IPV4="203.0.113.10"),
    "TURNS port with the HPB's own eturnal": {"TURN_DOMAIN": "turn.example.com", "TALK_TURNS_PORT": 5349},
    "external and internal signaling": dict(EXT, TALK_SIGNALING="internal"),
}


@pytest.mark.parametrize("pb", stun.PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("over", GOOD, ids=range(len(GOOD)))
def test_guard_accepts_external_turn(pb, over):
    assert stun._guard_passes(stun._guard(pb), dict(stun.BASE, **over))


@pytest.mark.parametrize("pb", stun.PLAYBOOKS, ids=lambda p: p.name)
@pytest.mark.parametrize("case", sorted(BAD))
def test_guard_refuses_incoherent_external_turn(pb, case):
    assert not stun._guard_passes(stun._guard(pb), dict(stun.BASE, **BAD[case]))


@pytest.mark.parametrize("pb", stun.PLAYBOOKS, ids=lambda p: p.name)
def test_guard_still_accepts_the_defaults(pb):
    assert stun._guard_passes(stun._guard(pb), dict(stun.BASE))


# --- the post-deploy Talk task -------------------------------------------------

@pytest.mark.parametrize("pb", stun.PLAYBOOKS, ids=lambda p: p.name)
def test_external_turn_servers_use_turn_domain_and_add_the_turns_port(pb):
    ts = stun._turn_servers(stun._talk_shell(pb, **EXT, TALK_TURNS_PORT=5349))
    assert [(e["schemes"], e["server"], e["protocols"]) for e in ts] == [
        ("turn", "turn.example.com:3478", "udp,tcp"), ("turns", "turn.example.com:5349", "tcp")]
    assert all(e["secret"] == stun.BASE["turn_secret"] for e in ts)


@pytest.mark.parametrize("pb", stun.PLAYBOOKS, ids=lambda p: p.name)
def test_external_keeps_the_hpb_signaling_servers(pb):
    shell = stun._talk_shell(pb, **EXT)
    assert "spreed signaling_servers" in shell and "/standalone-signaling/" in shell


@pytest.mark.parametrize("pb", stun.PLAYBOOKS, ids=lambda p: p.name)
def test_no_turns_port_keeps_the_task_unchanged(pb):
    assert stun._talk_shell(pb, TURN_DOMAIN="turn.example.com") == stun._talk_shell(pb, **EXT)
