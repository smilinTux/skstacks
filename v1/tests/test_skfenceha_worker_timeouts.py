"""Live-parity hooks for skfenceha's worker-role Traefik (live_parity run,
framework vs skstack01 prod). skstack01 prod's worker Traefik carries long
transport timeouts so multi-GB skhub (Nextcloud) uploads and chunk assembly
survive; the framework rendered Traefik's short defaults
(responseHeaderTimeout 30s), which cuts those uploads off.

- skfenceha.WORKER_RESPONDING_TIMEOUTS: map rendered verbatim as
  entryPoints.websecure.transport.respondingTimeouts (unset: no transport
  block, as before).
- skfenceha.WORKER_FORWARDING_TIMEOUTS: map that replaces
  serversTransport.forwardingTimeouts entirely (unset: the previous
  hardcoded 30s/10s/90s).
- skfenceha.GZIP_COMPRESS: map that replaces the gzip middleware's compress
  options in dynamic-middlewares.yml.j2 (unset: the previous
  includedContentTypes/minResponseBodyBytes; live runs `compress: {}`).

Each default is byte-identical to the previous render."""
import pathlib

import jinja2
import yaml

CFG = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfenceha/src/config/skfenceha"
WORKER = CFG / "traefik-worker.yml.j2"
MIDDLEWARES = CFG / "dynamic-middlewares.yml.j2"

LIVE_RESPONDING = {"readTimeout": "7200s", "writeTimeout": "7200s", "idleTimeout": "1800s"}
LIVE_FORWARDING = {"responseHeaderTimeout": "7200s", "dialTimeout": "30s", "idleConnTimeout": "1800s"}


def _env():
    return jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)


def render_worker(**overrides):
    skfenceha = {"ACME_ENABLED": False}
    skfenceha.update(overrides)
    out = _env().from_string(WORKER.read_text()).render(env="prod", app="skfenceha", skfenceha=skfenceha)
    return yaml.safe_load(out)


def render_middlewares(**overrides):
    out = _env().from_string(MIDDLEWARES.read_text()).render(skfenceha=overrides)
    return yaml.safe_load(out)["http"]["middlewares"]


# --- entrypoint responding timeouts ---------------------------------------

def test_no_transport_block_by_default():
    eps = render_worker()["entryPoints"]
    assert "transport" not in eps["websecure"]
    assert "transport" not in eps["web"]


def test_responding_timeouts_rendered_on_websecure_only():
    eps = render_worker(WORKER_RESPONDING_TIMEOUTS=LIVE_RESPONDING)["entryPoints"]
    assert eps["websecure"]["transport"] == {"respondingTimeouts": LIVE_RESPONDING}
    assert "transport" not in eps["web"]
    assert eps["websecure"]["address"] == ":443"


def test_responding_timeouts_coexist_with_trusted_proxies():
    eps = render_worker(WORKER_RESPONDING_TIMEOUTS=LIVE_RESPONDING,
                        TRUSTED_PROXIES=["10.0.0.0/8"])["entryPoints"]
    assert eps["websecure"]["forwardedHeaders"]["trustedIPs"] == ["10.0.0.0/8"]
    assert eps["websecure"]["transport"]["respondingTimeouts"] == LIVE_RESPONDING


def test_empty_responding_timeouts_renders_no_transport_block():
    assert "transport" not in render_worker(WORKER_RESPONDING_TIMEOUTS={})["entryPoints"]["websecure"]


# --- serversTransport forwarding timeouts ---------------------------------

def test_forwarding_timeouts_default_unchanged():
    st = render_worker()["serversTransport"]
    assert st["insecureSkipVerify"] is True
    assert st["forwardingTimeouts"] == {"responseHeaderTimeout": "30s", "dialTimeout": "10s", "idleConnTimeout": "90s"}


def test_forwarding_timeouts_replaced_exactly_when_set():
    st = render_worker(WORKER_FORWARDING_TIMEOUTS=LIVE_FORWARDING)["serversTransport"]
    assert st["forwardingTimeouts"] == LIVE_FORWARDING
    assert st["insecureSkipVerify"] is True


def test_forwarding_timeouts_partial_map_does_not_merge_defaults():
    st = render_worker(WORKER_FORWARDING_TIMEOUTS={"responseHeaderTimeout": "7200s"})["serversTransport"]
    assert st["forwardingTimeouts"] == {"responseHeaderTimeout": "7200s"}


# --- gzip compress options -------------------------------------------------

def test_gzip_default_unchanged():
    compress = render_middlewares()["gzip"]["compress"]
    assert compress["minResponseBodyBytes"] == 1024
    assert "text/html" in compress["includedContentTypes"]
    assert len(compress["includedContentTypes"]) == 10


def test_gzip_empty_map_renders_traefik_defaults_like_live():
    assert render_middlewares(GZIP_COMPRESS={})["gzip"]["compress"] == {}


def test_gzip_custom_options_rendered():
    opts = {"excludedContentTypes": ["text/event-stream"], "minResponseBodyBytes": 2048}
    assert render_middlewares(GZIP_COMPRESS=opts)["gzip"]["compress"] == opts
