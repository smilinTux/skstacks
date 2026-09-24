"""
skred config — the default scope for OUR infrastructure, and config loading.

The defaults encode the smilinTux / SKWorld estate so `skred scan` is safe out of the
box. Override via a skred.yaml (or SKRED_SCOPE_* env) when the estate changes.
"""
from __future__ import annotations

import os

from .scope import ScopeGuard

# Default scope is fail-closed: loopback only. skred is offensive tooling, so a
# shipped default must never pre-authorize anyone's real estate. Declare yours
# per deployment, e.g.
#   SKRED_SCOPE_DOMAINS=example.com,cluster.example.com
#   SKRED_SCOPE_CIDRS=192.168.100.0/24
# Repo scans are unaffected: they are scoped by the checkout root, not by these.
OWN_DOMAINS = ["localhost"]
OWN_CIDRS = ["127.0.0.0/8"]


def default_guard(repo_root: str | None = None) -> ScopeGuard:
    """A ScopeGuard scoped to our estate + the current repo checkout."""
    root = os.path.realpath(repo_root or os.getcwd())
    domains = list(OWN_DOMAINS)
    cidrs = list(OWN_CIDRS)
    # allow env extension (comma-separated), e.g. SKRED_SCOPE_DOMAINS=foo.io,bar.io
    domains += [d for d in os.environ.get("SKRED_SCOPE_DOMAINS", "").split(",") if d.strip()]
    cidrs += [c for c in os.environ.get("SKRED_SCOPE_CIDRS", "").split(",") if c.strip()]
    return ScopeGuard(domains=domains, cidrs=cidrs, roots=[root])
