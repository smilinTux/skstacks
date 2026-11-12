# Copyright (C) 2025 S&K Holding QT (Quantum Technologies)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# skmon default probe discovery: the output of `docker service inspect` for every
# Swarm service becomes one blackbox probe per distinct Traefik router host.
# No Ansible dependency; the deploy plays and v1/tests/skmon_support.py both call
# skmon_probes_from_services, so the parsing lives in exactly one place.

import re

_HOST_RE = re.compile(r"Host\(\s*`([^`]+)`\s*\)")
_RULE_RE = re.compile(r"^traefik\.http\.routers\.([^.]+)\.rule$")


def skmon_probes_from_services(inspect, forward_auth_middleware):
    """One probe per distinct Host() in the Traefik router rules of the inspected services.

    Args:
        inspect: parsed `docker service inspect` output, a list of service objects
            (labels under Spec.Labels).
        forward_auth_middleware: middleware name that marks a router as behind
            forward auth (skmon.FORWARD_AUTH_MIDDLEWARE).

    Returns:
        List of {name, url, severity, expect, forward_auth} sorted by host. `name` is the
        host. A host served by several routers is forward_auth when any of them is.
    """
    hosts = {}
    for svc in inspect or []:
        labels = ((svc or {}).get("Spec") or {}).get("Labels") or {}
        for key, rule in labels.items():
            m = _RULE_RE.match(key)
            if not m:
                continue
            middlewares = [x.strip() for x in str(labels.get(f"traefik.http.routers.{m.group(1)}.middlewares", "")).split(",")]
            fwd = bool(forward_auth_middleware) and forward_auth_middleware in middlewares
            for host in _HOST_RE.findall(str(rule)):
                hosts[host] = hosts.get(host, False) or fwd
    return [
        {"name": host, "url": f"https://{host}", "severity": "critical", "expect": "2xx|3xx", "forward_auth": hosts[host]}
        for host in sorted(hosts)
    ]


def skmon_with_authentik_probe(probes, authentik_host):
    """The probe list plus the fixed Authentik health probe.

    Nothing is added when authentik_host is empty (skmon could not determine it) or a probe
    named authentik is already in the list (an operator entry wins).
    """
    probes = list(probes or [])
    if not authentik_host or any(p.get("name") == "authentik" for p in probes):
        return probes
    return probes + [{"name": "authentik", "url": f"https://{authentik_host}/-/health/live/",
                      "severity": "critical", "expect": 200, "forward_auth": False}]


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "skmon_probes_from_services": skmon_probes_from_services,
            "skmon_with_authentik_probe": skmon_with_authentik_probe,
        }
