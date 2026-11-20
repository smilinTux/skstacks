"""skfence Traefik timeout knobs (#460), mirroring skfenceha's worker knobs.

skfence hardcoded serversTransport.forwardingTimeouts.responseHeaderTimeout
30s. Nextcloud Talk internal signaling long-polls for 30 s, so every idle poll
got a 504 at 30.2 s; the same limit cuts multi-GB skhub uploads during chunk
assembly.

- skfence.FORWARDING_TIMEOUTS: map that replaces forwardingTimeouts entirely
  (unset or {}: the previous 30s/10s/90s, which is also skfenceha's default).
- skfence.RESPONDING_TIMEOUTS: map rendered verbatim as
  entryPoints.websecure.transport.respondingTimeouts (unset: no block).

Unset, the render is byte-identical to the previous template."""
import pathlib
import subprocess

import jinja2
import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
TPL = "v1/ansible/core/skfence/src/config/skfence/traefik.yml.j2"
HA_WORKER = ROOT / "v1/ansible/core/skfenceha/src/config/skfenceha/traefik-worker.yml.j2"
DEFAULTS = {"responseHeaderTimeout": "30s", "dialTimeout": "10s", "idleConnTimeout": "90s"}
TALK = {"responseHeaderTimeout": "7200s", "dialTimeout": "30s", "idleConnTimeout": "1800s"}
RESPONDING = {"readTimeout": "7200s", "writeTimeout": "7200s", "idleTimeout": "1800s"}


def _text(src, **skfence):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string(src).render(env="dev", app="skfence", NODE_HOSTNAME="n1", skfence=skfence)


def render_text(**skfence):
    return _text((ROOT / TPL).read_text(), **skfence)


def render(**skfence):
    return yaml.safe_load(render_text(**skfence))


def _git_show(rev):
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{rev}:{TPL}"], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


def test_default_forwarding_timeouts_unchanged():
    assert render()["serversTransport"]["forwardingTimeouts"] == DEFAULTS


def test_default_matches_skfenceha_worker_default():
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    ha = yaml.safe_load(env.from_string(HA_WORKER.read_text()).render(
        env="dev", app="skfenceha", skfenceha={"ACME_ENABLED": False}))
    assert ha["serversTransport"]["forwardingTimeouts"] == render()["serversTransport"]["forwardingTimeouts"]


@pytest.mark.parametrize("unset", [None, {}])
def test_unset_renders_byte_identical_to_before(unset):
    before = _git_show("skstacks-v2.26.27")
    if before is None:
        pytest.skip("tag skstacks-v2.26.27 not available in this checkout")
    over = {} if unset is None else {"FORWARDING_TIMEOUTS": unset, "RESPONDING_TIMEOUTS": unset}
    assert render_text(**over) == _text(before, **over)


def test_forwarding_timeouts_replace_the_defaults_entirely():
    assert render(FORWARDING_TIMEOUTS=TALK)["serversTransport"]["forwardingTimeouts"] == TALK


def test_partial_map_does_not_merge_defaults():
    ft = render(FORWARDING_TIMEOUTS={"responseHeaderTimeout": "60s"})["serversTransport"]["forwardingTimeouts"]
    assert ft == {"responseHeaderTimeout": "60s"}


def test_no_transport_block_by_default():
    eps = render()["entryPoints"]
    assert "transport" not in eps["websecure"] and "transport" not in eps["web"]


def test_responding_timeouts_on_websecure_only():
    eps = render(RESPONDING_TIMEOUTS=RESPONDING)["entryPoints"]
    assert eps["websecure"]["transport"] == {"respondingTimeouts": RESPONDING}
    assert "transport" not in eps["web"]
    assert eps["websecure"]["address"] == ":443"
    assert eps["websecure"]["http"]["encodedCharacters"]["allowEncodedSlash"] is True


def test_both_knobs_together_keep_the_rest_of_the_config():
    base, both = render(), render(FORWARDING_TIMEOUTS=TALK, RESPONDING_TIMEOUTS=RESPONDING)
    for key in base:
        if key not in ("serversTransport", "entryPoints"):
            assert both[key] == base[key], key
    assert both["entryPoints"]["metrics"] == base["entryPoints"]["metrics"]


def test_talk_long_poll_needs_more_than_30s():
    # The value an estate with Talk internal signaling sets must exceed the 30 s poll.
    rht = render(FORWARDING_TIMEOUTS=TALK)["serversTransport"]["forwardingTimeouts"]["responseHeaderTimeout"]
    assert int(rht.rstrip("s")) > 30
