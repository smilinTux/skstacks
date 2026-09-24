"""
skred scope guard — the safety rail.

skred is offensive tooling (scanners, fuzzers, exploit checks). It must NEVER touch
anything that isn't ours. Every target — a URL, host, IP, or file path — is checked
against an explicit allowlist of OUR domains, IP ranges, and repo roots. Anything not
explicitly allowed is refused. Fail closed: an empty allowlist permits nothing.
"""
from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, field
from urllib.parse import urlparse


class OutOfScopeError(Exception):
    """Raised when a red-team action targets something outside the allowlist."""


@dataclass
class ScopeGuard:
    domains: list = field(default_factory=list)   # own domains (incl. subdomains)
    cidrs: list = field(default_factory=list)     # own IP ranges (CIDR)
    roots: list = field(default_factory=list)     # own filesystem roots

    def __post_init__(self):
        self._nets = [ipaddress.ip_network(c, strict=False) for c in self.cidrs]
        self._roots = [os.path.realpath(r) for r in self.roots]
        self._domains = [d.lower().lstrip(".") for d in self.domains]

    # ── public API ────────────────────────────────────────────────────────────
    def is_allowed(self, target: str) -> bool:
        """True iff `target` (url / host / ip / path) is within our scope."""
        t = (target or "").strip()
        if not t:
            return False
        if t.startswith("/") or t.startswith("./") or t.startswith("../"):
            return self._path_ok(t)
        host = self._host_of(t)
        if not host:
            return False
        ip = self._as_ip(host)
        if ip is not None:
            return any(ip in net for net in self._nets)
        return self._domain_ok(host)

    def assert_in_scope(self, target: str) -> None:
        if not self.is_allowed(target):
            raise OutOfScopeError(
                f"refusing out-of-scope target {target!r} — skred only touches our own "
                f"domains/IPs/paths (fail-closed safety rail)")

    def filter_in_scope(self, targets) -> list:
        """Keep only the in-scope targets (for batch operations)."""
        return [t for t in targets if self.is_allowed(t)]

    # ── internals ──────────────────────────────────────────────────────────────
    @staticmethod
    def _host_of(t: str) -> str:
        if "://" in t:
            return (urlparse(t).hostname or "").lower()
        # strip any path/port from a bare host[:port]/path
        h = t.split("/", 1)[0]
        if h.count(":") == 1:                      # host:port (not IPv6)
            h = h.split(":", 1)[0]
        return h.lower()

    @staticmethod
    def _as_ip(host: str):
        try:
            return ipaddress.ip_address(host)
        except ValueError:
            return None

    def _domain_ok(self, host: str) -> bool:
        # exact match or a real subdomain (boundary-aware: blocks skworld.io.evil.com
        # and notskworld.io)
        return any(host == d or host.endswith("." + d) for d in self._domains)

    def _path_ok(self, path: str) -> bool:
        rp = os.path.realpath(path)
        return any(rp == root or rp.startswith(root + os.sep) for root in self._roots)
