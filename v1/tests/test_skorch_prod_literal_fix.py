"""skorch.yml.j2's rule= label compared env to a bare `prod` Jinja variable
instead of the string literal 'prod' (every other {% if env != 'prod' %} in
the same file quotes it correctly). Undefined in real ansible rendering
(strict undefined), so a prod deploy failed with "'prod' is undefined" -
found running tools/live_parity.sh against the skorch prod vault PR (#29).
The lenient jinja2.ChainableUndefined used by other unit tests never caught
this since it tolerates the undefined lookup silently."""
import pathlib

import jinja2

SKORCH = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skorch/src/config/skorch/skorch.yml.j2"


def test_no_bare_prod_variable_comparison():
    text = SKORCH.read_text()
    assert "env != prod " not in text and "env != prod}" not in text and "env != prod%" not in text


def test_renders_under_strict_undefined_for_both_prod_and_non_prod():
    # StrictUndefined raises on any undefined lookup, exactly like ansible's
    # default templating - this is what the old bare `prod` reference broke.
    j2 = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True)
    tmpl = j2.from_string(SKORCH.read_text())
    for env in ("prod", "dev"):
        out = tmpl.render(
            env=env, app="skorch", skorch_cfg={}, cluster_name="cluster1", domain="example.com",
        )
        assert "traefik.http.routers.skorch" in out
