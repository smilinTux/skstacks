"""The sksso worker healthcheck must be the image's own `ak healthcheck`.

It used to match process command lines for `manage` or `celery`. From Authentik
2026.5 the worker is `ak worker`, so that check failed on a working worker and
Swarm killed it every few minutes (seen upgrading nor on 2026-11-06). Matching
`ak` instead would always pass, because PID 1 is `dumb-init -- ak worker`."""
import pathlib

import jinja2
import pytest
import yaml

SKSSO = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/sksso"


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_worker_uses_the_images_healthcheck(env):
    jenv = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = jenv.from_string((SKSSO / "src/config/sksso/sksso.yml.j2").read_text()).render(
        env=env, app="sksso", sksso={"CLUSTERNAME": "c", "DOMAIN": "example.com"})
    hc = yaml.safe_load(out)["services"]["worker"]["healthcheck"]
    assert hc["test"] == ["CMD-SHELL", "ak healthcheck"]
