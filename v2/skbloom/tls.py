"""
skbloom local TLS — instant HTTPS for an install (the design's mkcert/skca UX win).

Uses cert-manager's standard self-signed→CA chain: a self-signed bootstrap ClusterIssuer
mints an in-cluster CA, and a CA ClusterIssuer signs per-service leaf certs from it. The
CA cert can be exported and trusted on the host (mkcert-style) for green-lock `*.sk.local`
in the browser. Pure manifest builders — deterministic + unit-tested; the flow step that
installs cert-manager + applies these is in flow.py (gated on profile.tls).
"""
from __future__ import annotations

_CM = "cert-manager.io/v1"


# Default rotation cadence. cert-manager AUTO-RENEWS at `renewBefore` before expiry —
# so setting duration + renewBefore makes certs self-rotating, no cron needed.
LEAF_DURATION = "2160h"      # 90 days
LEAF_RENEW_BEFORE = "720h"   # renew 30 days early
CA_DURATION = "8760h"        # 1 year
CA_RENEW_BEFORE = "2160h"    # renew 90 days early


def ca_bootstrap_manifests(name: str = "skbloom-ca", namespace: str = "cert-manager") -> list:
    """The self-signed → CA → CA-issuer chain. Apply once per cluster. The CA cert
    carries duration/renewBefore so cert-manager auto-rotates the CA too."""
    boot = {
        "apiVersion": _CM, "kind": "ClusterIssuer",
        "metadata": {"name": f"{name}-selfsigned"},
        "spec": {"selfSigned": {}},
    }
    ca_cert = {
        "apiVersion": _CM, "kind": "Certificate",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "isCA": True, "commonName": name, "secretName": f"{name}-tls",
            "duration": CA_DURATION, "renewBefore": CA_RENEW_BEFORE,
            "privateKey": {"algorithm": "ECDSA", "size": 256, "rotationPolicy": "Always"},
            "issuerRef": {"name": f"{name}-selfsigned", "kind": "ClusterIssuer", "group": "cert-manager.io"},
        },
    }
    ca_issuer = {
        "apiVersion": _CM, "kind": "ClusterIssuer",
        "metadata": {"name": name},
        "spec": {"ca": {"secretName": f"{name}-tls"}},
    }
    return [boot, ca_cert, ca_issuer]


def cert_for(host: str, *, namespace: str, issuer: str = "skbloom-ca", name: str | None = None,
             duration: str = LEAF_DURATION, renew_before: str = LEAF_RENEW_BEFORE) -> dict:
    """A leaf Certificate for one service host, signed by the CA issuer. duration +
    renewBefore make cert-manager AUTO-ROTATE it; `rotationPolicy: Always` rolls the key
    on every renewal too. The TLS Secret is what the ingress (skfence/Traefik) mounts."""
    safe = (name or host).replace("*", "wildcard").replace(".", "-").strip("-")
    return {
        "apiVersion": _CM, "kind": "Certificate",
        "metadata": {"name": safe, "namespace": namespace},
        "spec": {
            "secretName": f"{safe}-tls", "dnsNames": [host],
            "duration": duration, "renewBefore": renew_before,
            "privateKey": {"rotationPolicy": "Always"},
            "issuerRef": {"name": issuer, "kind": "ClusterIssuer", "group": "cert-manager.io"},
        },
    }
