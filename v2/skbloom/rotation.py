"""
skbloom rotation — one view of everything that rotates in an install, creds AND certs.

- **Secrets**: each descriptor secret declares `rotation_days`. The flow is automatic:
  the backend (vault-file / OpenBao / capauth) re-mints on cadence (skwire's rotate()),
  ESO's refreshInterval re-syncs the new value into the cluster Secret, and consumers
  pick it up. The cadence is the descriptor's rotation_days.
- **Certs**: when TLS is on, cert-manager auto-renews leaf + CA certs (duration/
  renewBefore in skbloom.tls) and rolls the key each time — fully automatic, no cron.

`rotation_plan()` surfaces all of it for `skbloom rotation` / the web UI.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SECRET_DAYS = 90          # if a descriptor secret omits rotation_days
CERT_RENEW_DAYS = 90              # cert-manager leaf renewal cadence (see tls.LEAF_*)


@dataclass(frozen=True)
class Rotation:
    service: str          # the deployed service name
    target: str           # secret key, or "tls-cert"
    kind: str             # "secret" | "cert"
    every_days: int       # rotation/renewal cadence
    automatic: bool       # True = self-rotates (backend+ESO, or cert-manager)
    how: str = ""         # one-line description of the mechanism


def _base(path: str) -> str:
    return path.rstrip("/").split("/")[-1].split("=")[0] if "=" in path else path.rstrip("/").split("/")[-1]


def rotation_plan(v2_root: str, service_paths, *, tls: bool = False) -> list:
    """Every rotating credential + cert for the given services."""
    import yaml
    out = []
    for entry in service_paths:
        path = entry.split("=", 1)[0]
        name = entry.split("=", 1)[1] if "=" in entry else _base(path)
        f = os.path.join(v2_root, path, "app.yaml")
        try:
            desc = yaml.safe_load(open(f)) or {}
        except Exception:
            continue
        for sec in (desc.get("secrets") or []):
            days = int(sec.get("rotation_days") or DEFAULT_SECRET_DAYS)
            out.append(Rotation(service=name, target=sec["key"], kind="secret",
                                every_days=days, automatic=True,
                                how="backend re-mints (skwire rotate) → ESO re-syncs → consumers update"))
        if tls:
            out.append(Rotation(service=name, target="tls-cert", kind="cert",
                                every_days=CERT_RENEW_DAYS, automatic=True,
                                how="cert-manager auto-renews + rolls the key (renewBefore)"))
    return out
