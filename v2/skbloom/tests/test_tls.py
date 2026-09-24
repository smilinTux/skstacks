"""Local TLS — instant HTTPS for a skbloom install via a cert-manager self-signed CA
(the design's mkcert/skca 'big UX win'). Deterministic manifest builders, unit-tested."""
from __future__ import annotations

from skbloom.tls import ca_bootstrap_manifests, cert_for


def test_ca_bootstrap_is_the_selfsigned_to_ca_chain():
    ms = ca_bootstrap_manifests(name="skbloom-ca")
    kinds = [m["kind"] for m in ms]
    # the standard cert-manager chain: a self-signed bootstrap issuer → a CA cert → the CA issuer
    assert kinds.count("ClusterIssuer") == 2 and "Certificate" in kinds
    boot = next(m for m in ms if m["kind"] == "ClusterIssuer" and "selfSigned" in str(m["spec"]))
    ca_cert = next(m for m in ms if m["kind"] == "Certificate")
    ca_issuer = next(m for m in ms if m["kind"] == "ClusterIssuer" and "ca" in m["spec"])
    assert ca_cert["spec"]["isCA"] is True
    assert ca_cert["spec"]["issuerRef"]["name"] == boot["metadata"]["name"]
    # the CA issuer signs from the cert's secret → a chain of trust
    assert ca_issuer["spec"]["ca"]["secretName"] == ca_cert["spec"]["secretName"]
    assert ca_issuer["metadata"]["name"] == "skbloom-ca"


def test_cert_for_a_service_requests_from_the_ca_issuer():
    c = cert_for("login.sk.local", namespace="login", issuer="skbloom-ca")
    assert c["kind"] == "Certificate"
    assert "login.sk.local" in c["spec"]["dnsNames"]
    assert c["spec"]["issuerRef"]["name"] == "skbloom-ca"
    assert c["spec"]["issuerRef"]["kind"] == "ClusterIssuer"
    assert c["metadata"]["namespace"] == "login"
    assert c["spec"]["secretName"]                       # a TLS secret the ingress mounts


def test_cert_for_supports_a_wildcard():
    c = cert_for("*.sk.local", namespace="default")
    assert "*.sk.local" in c["spec"]["dnsNames"]
