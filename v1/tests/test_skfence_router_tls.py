"""Every skfence router on the websecure entrypoint must carry a tls section
Traefik recognises. An empty `tls:` renders as YAML null, which Traefik reads
as no TLS, so the router never matches HTTPS and returns 404 (found when
skdash discovery hit skfence's /api on the skstack06 test instance)."""
import pathlib

import jinja2
import pytest
import yaml

ROUTERS = pathlib.Path(__file__).resolve().parents[1] / "ansible/core/skfence/src/config/skfence/routers.yml.j2"


def render(acme):
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined, trim_blocks=True)
    out = env.from_string(ROUTERS.read_text()).render(
        env="dev", domain="example.com", cluster_name="cluster1",
        skfence={"ACME_ENABLED": acme})
    return yaml.safe_load(out)["http"]["routers"]


@pytest.mark.parametrize("acme", [False, True])
def test_websecure_routers_have_a_tls_mapping(acme):
    bad = [name for name, r in render(acme).items()
           if "websecure" in r.get("entryPoints", []) and not isinstance(r.get("tls"), dict)]
    assert not bad, f"websecure routers without a tls mapping: {bad}"


def test_acme_routers_use_the_resolver():
    for name, r in render(True).items():
        if "websecure" in r.get("entryPoints", []):
            assert r["tls"].get("certResolver") == "main", name
