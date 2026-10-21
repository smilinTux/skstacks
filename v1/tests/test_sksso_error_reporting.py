"""sksso.error_reporting_enabled: AUTHENTIK_ERROR_REPORTING__ENABLED now
defaults to false (was Authentik's own upstream default of true). This is a
sovereign stack: Authentik should not phone error reports home to its
Sentry instance unless an instance opts back in. `sksso.error_reporting_enabled`
still overrides either way (unchanged behavior, only the unset default moved).
"""
import pathlib

import jinja2
import pytest

T = (pathlib.Path(__file__).resolve().parents[1]
     / "ansible/optional/sksso/src/config/sksso/sksso.env.j2").read_text()


def render(**sksso):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    return env.from_string(T).render(sksso=sksso)


def line(out):
    hits = [ln for ln in out.splitlines() if ln.startswith("AUTHENTIK_ERROR_REPORTING__ENABLED=")]
    assert len(hits) == 1, out
    return hits[0]


def test_unset_defaults_to_false():
    assert line(render()) == "AUTHENTIK_ERROR_REPORTING__ENABLED=false"


@pytest.mark.parametrize("value", ["true", "false"])
def test_explicit_value_is_honored(value):
    assert line(render(error_reporting_enabled=value)) == f"AUTHENTIK_ERROR_REPORTING__ENABLED={value}"
