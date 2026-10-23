"""Exposure classification from a Swarm service's Traefik labels.

Read-only: the only remote call is `docker service inspect --format
'{{json .Spec.Labels}}'`. Classifies into exactly the three buckets the
vuln register cares about - see README.md "Exposure" for the operator
-facing explanation of each:

- "internet-behind-sso-gate": a public router's middleware chain names a
  forward-auth middleware (Authentik's `authentik@file`, or anything with
  "authentik"/"forward-auth" in the name). Unauthenticated requests never
  reach the app.
- "internet-unauthenticated": a public router exists (a Host()/HostRegexp()
  rule) but no forward-auth middleware sits in front of it. True whether
  the app has its own native OIDC/local login or only CrowdSec (a WAF/IDS,
  not an identity gate) - either way an unauthenticated HTTP request
  reaches application code before any login check happens, which is the
  exposure an unauthenticated-RCE CVE cares about.
- "internal-only": no public Traefik router was found for this service at
  all.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from remote import run_remote

_LABELS_CMD_TMPL = "sudo docker service inspect {svc} --format '{{{{json .Spec.Labels}}}}'"

_FORWARD_AUTH_HINTS = ("authentik", "forward-auth", "forwardauth")


@dataclass
class ExposureResult:
    exposure: str                      # "internet-behind-sso-gate" | "internet-unauthenticated" | "internal-only" | "unknown"
    mechanism: str = ""                 # e.g. "forward-auth:authentik@file", "no-public-router"
    routers: list[str] = field(default_factory=list)
    error: str = ""


def fetch_service_labels(service: str, *, manager: str, jump: str | None = None, user: str | None = None,
                         identity: str | None = None, runner=None) -> dict[str, str]:
    import subprocess
    runner = runner or subprocess.run
    result = run_remote(manager, _LABELS_CMD_TMPL.format(svc=service), jump=jump, user=user, identity=identity,
                        runner=runner)
    if not result.ok or not result.stdout.strip():
        return {}
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def classify_exposure(labels: dict[str, str]) -> ExposureResult:
    traefik_labels = {k: v for k, v in labels.items() if k.startswith("traefik.")}
    if not traefik_labels:
        return ExposureResult(exposure="internal-only", mechanism="no-traefik-labels")

    routers: set[str] = set()
    for key in traefik_labels:
        parts = key.split(".")
        if len(parts) >= 4 and parts[0] == "traefik" and parts[1] == "http" and parts[2] == "routers":
            routers.add(parts[3])

    has_public_rule = False
    middlewares: list[str] = []
    for router in routers:
        rule = traefik_labels.get(f"traefik.http.routers.{router}.rule", "")
        if "Host(" in rule or "HostRegexp(" in rule:
            has_public_rule = True
        mw_val = traefik_labels.get(f"traefik.http.routers.{router}.middlewares", "")
        if mw_val:
            middlewares.extend(m.strip() for m in mw_val.split(",") if m.strip())

    if not has_public_rule:
        return ExposureResult(exposure="internal-only", mechanism="no-public-router", routers=sorted(routers))

    forward_auth = [m for m in middlewares if any(hint in m.lower() for hint in _FORWARD_AUTH_HINTS)]
    if forward_auth:
        return ExposureResult(exposure="internet-behind-sso-gate", mechanism=f"forward-auth:{forward_auth[0]}",
                              routers=sorted(routers))

    return ExposureResult(exposure="internet-unauthenticated",
                          mechanism="native-app-auth-or-crowdsec-only-or-none", routers=sorted(routers))
