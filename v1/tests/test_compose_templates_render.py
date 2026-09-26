"""Compose templates must render to valid YAML whichever way their feature
flags are set (skform produced invalid YAML with SKSTOR_ENABLED false: a
whitespace-control tag ate the newline before `deploy:`)."""
import pathlib

import jinja2
import pytest
import yaml

ANSIBLE = pathlib.Path(__file__).resolve().parents[1] / "ansible"
SKFORM = ANSIBLE / "optional/skform/src/config/skform/skform.yml.j2"


@pytest.mark.parametrize("skstor", [False, True])
def test_skform_compose_is_valid_yaml(skstor):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)  # ansible's template default
    out = env.from_string(SKFORM.read_text()).render(
        app="skform", env="dev", skform={"SKSTOR_ENABLED": skstor, "TOFU_VERSION": "1.8.0"})
    doc = yaml.safe_load(out)
    svc = next(iter(doc["services"].values()))
    assert "deploy" in svc and "networks" in svc
    assert ("cloud-public-dev" in svc["networks"]) == skstor
    assert "traefik.enable=false" in svc.get("labels", [])  # CLI only: never routed
