"""skpdf: Stirling-PDF updates stop-first.

It is a single replica whose state is files on shared storage (/configs,
pipeline, logs). start-first ran the new container next to the old one on the
same files and needs two memory reservations at once, so an update can stall
at 0/1.
"""
import pathlib

import jinja2
import pytest
import yaml

TEMPLATE = (pathlib.Path(__file__).resolve().parents[1]
            / "ansible/optional/skpdf/src/config/skpdf/skpdf.yml.j2")


def render(env, **skpdf):
    base = {"CLUSTERNAME": "c", "DOMAIN": "example.org"}
    base.update(skpdf)
    text = jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(
        TEMPLATE.read_text()).render(env=env, skpdf=base)
    return yaml.safe_load(text)["services"]["stirling-pdf"]


@pytest.mark.parametrize("env", ["prod", "dev", "staging"])
def test_update_order_is_stop_first(env):
    update = render(env)["deploy"]["update_config"]
    assert update["order"] == "stop-first"
    assert update["parallelism"] == 1


def test_stays_single_replica():
    deploy = render("prod")["deploy"]
    assert deploy["mode"] == "replicated" and deploy["replicas"] == 1


def test_order_not_overridable_back_to_start_first():
    # A vault knob must not be able to reintroduce the overlap.
    deploy = render("prod", UPDATE_ORDER="start-first")["deploy"]
    assert deploy["update_config"]["order"] == "stop-first"
