"""
Guard tests for the Gateway API + Traefik base (replaces ingress-nginx, EOL Mar 2026).

Validates the manifests parse and encode the ratified decision: in-cluster
ingress = Gateway API with Traefik v3 as the controller, the legacy Ingress
provider OFF, and no `kind: Ingress` left in the base.
"""
from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

_BASE = Path(__file__).resolve().parents[1] / "base"


def _docs(name):
    return [d for d in yaml.safe_load_all((_BASE / name).read_text()) if d]


def test_gatewayclass_uses_traefik_controller():
    docs = _docs("gateway.yaml")
    gc = next(d for d in docs if d["kind"] == "GatewayClass")
    assert gc["spec"]["controllerName"] == "traefik.io/gateway-controller"


def test_gateway_has_web_and_websecure_listeners():
    docs = _docs("gateway.yaml")
    gw = next(d for d in docs if d["kind"] == "Gateway")
    assert gw["spec"]["gatewayClassName"] == "traefik"
    names = {l["name"] for l in gw["spec"]["listeners"]}
    assert {"web", "websecure"} <= names
    websecure = next(l for l in gw["spec"]["listeners"] if l["name"] == "websecure")
    assert websecure["protocol"] == "HTTPS"
    assert websecure["tls"]["mode"] == "Terminate"


def test_traefik_helmchart_gateway_on_ingress_off():
    docs = _docs("traefik-helmchart.yaml")
    hc = next(d for d in docs if d["kind"] == "HelmChart")
    values = hc["spec"]["valuesContent"]
    # parse the embedded values YAML
    vals = yaml.safe_load(values)
    assert vals["providers"]["kubernetesGateway"]["enabled"] is True
    assert vals["providers"]["kubernetesIngress"]["enabled"] is False
    assert vals["service"]["type"] == "LoadBalancer"   # MetalLB VIP


def test_base_kustomization_includes_gateway_and_traefik():
    docs = _docs("kustomization.yaml")
    res = docs[0]["resources"]
    assert "gateway.yaml" in res
    assert "traefik-helmchart.yaml" in res


def test_no_legacy_ingress_in_base():
    for f in _BASE.glob("*.yaml"):
        for d in yaml.safe_load_all(f.read_text()):
            if d:
                assert d.get("kind") != "Ingress", f"legacy Ingress found in {f.name}"


_K8S = Path(__file__).resolve().parents[1]


def test_no_ingress_nginx_solver_anywhere():
    # ACME http01 must use the Gateway solver, never ingress-nginx, post-migration.
    for f in _K8S.rglob("*.yaml"):
        assert "ingressClassName: nginx" not in f.read_text(), f"nginx solver in {f}"


@pytest.mark.parametrize("issuer", list(_K8S.rglob("cluster-issuer.yaml")))
def test_cluster_issuers_use_gateway_http01_solver(issuer):
    doc = next(d for d in yaml.safe_load_all(issuer.read_text()) if d)
    solver = doc["spec"]["acme"]["solvers"][0]["http01"]
    assert "gatewayHTTPRoute" in solver, f"{issuer} not on the Gateway solver"
    parent = solver["gatewayHTTPRoute"]["parentRefs"][0]
    assert parent["name"] == "skstacks" and parent["kind"] == "Gateway"
