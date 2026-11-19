"""The default TLS options keep sniStrict opt-in (skfence and skfenceha).

v2.26.29 moved every router onto the default TLS options (#463) and first gave
default `sniStrict: true`. That refused the handshake for any host with no
matching certificate (ACME still pending, or only Traefik's self-signed default
cert), which broke the skstack06 gate's whoami probe. sniStrict is now an
instance knob, DEFAULT_SNI_STRICT, off unless set.
"""
import pathlib

import jinja2
import pytest
import yaml

V1 = pathlib.Path(__file__).resolve().parents[1]
ROLES = ("skfence", "skfenceha")


def _render(svc, vars_):
    src = V1 / "ansible" / "core" / svc / "src" / "config" / svc / "dynamic-tls.yml.j2"
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.filters["bool"] = lambda v: str(v).strip().lower() in ("1", "true", "yes", "on") if not isinstance(v, bool) else v
    return yaml.safe_load(env.from_string(src.read_text()).render(**vars_))["tls"]["options"]


@pytest.mark.parametrize("svc", ROLES)
def test_default_is_not_sni_strict_when_unset(svc):
    assert _render(svc, {svc: {}})["default"]["sniStrict"] is False


@pytest.mark.parametrize("svc", ROLES)
@pytest.mark.parametrize("value", [True, "true", "yes"])
def test_knob_turns_sni_strict_on(svc, value):
    assert _render(svc, {svc: {"DEFAULT_SNI_STRICT": value}})["default"]["sniStrict"] is True


@pytest.mark.parametrize("svc", ROLES)
@pytest.mark.parametrize("value", [False, "false", "no"])
def test_knob_false_keeps_it_off(svc, value):
    assert _render(svc, {svc: {"DEFAULT_SNI_STRICT": value}})["default"]["sniStrict"] is False


@pytest.mark.parametrize("svc", ROLES)
def test_named_options_stay_strict(svc):
    opts = _render(svc, {svc: {}})
    assert opts["intermediate"]["sniStrict"] is True and opts["modern"]["sniStrict"] is True
