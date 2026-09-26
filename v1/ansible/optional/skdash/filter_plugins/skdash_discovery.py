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
# Generic Traefik-API -> Dashy sections transform for skdash.
#
# This module has NO Ansible dependency and NO estate-specific service
# knowledge (no hardcoded hostnames, service-name patterns or domains). It
# takes whatever Traefik's `/api/http/routers` endpoint returns plus a small
# set of instance-supplied rules (exclude patterns, a group map) and produces
# Dashy `sections`. Instance-specific categorization lives entirely in the
# `group_map` vault var, not in this code, so this file is safe to publish.
#
# See v1/tests/test_skdash_discovery.py for the pure-Python unit tests and
# v1/docs/skdash-discovery.md for the design.

import re

DEFAULT_ICON = "https://cdn.jsdelivr.net/gh/homarr-labs/dashboard-icons/svg/default.svg"
DEFAULT_GROUP_NAME = "Discovered Services"

_HOST_RE = re.compile(r"Host\(`([^`]+)`\)")


def _extract_host(rule):
    """Pull the hostname out of a Traefik router rule string, e.g.
    'Host(`grafana.example.test`) && PathPrefix(`/api`)' -> grafana.example.test.
    Rules with no Host() matcher (catch-alls, PathPrefix-only routers, etc.)
    return None and are excluded from discovery."""
    match = _HOST_RE.search(rule or "")
    return match.group(1) if match else None


def _match_group(name, host, group_map):
    """First matching group_map entry wins; an entry matches if its regex
    pattern is found in either the router name or its discovered host."""
    for entry in group_map or []:
        pattern = entry.get("pattern")
        if not pattern:
            continue
        regex = re.compile(pattern, re.IGNORECASE)
        if regex.search(name) or (host and regex.search(host)):
            return entry.get("group"), entry.get("icon")
    return None, None


def _title_from_router_name(name):
    # Traefik router names carry a "@provider" suffix (e.g. "@docker",
    # "@internal"); it is implementation detail, not part of the service name.
    base = name.split("@", 1)[0]
    return re.sub(r"[-_]+", " ", base).strip().title()


def routers_to_sections(routers, exclude_patterns=None, group_map=None,
                         default_group=DEFAULT_GROUP_NAME, default_icon=DEFAULT_ICON):
    """Turn a Traefik `/api/http/routers` response into Dashy sections.

    Args:
        routers: list of router dicts as returned by the Traefik API
            (each with at least "name" and "rule"; "entryPoints" is used to
            guess http vs https). Anything that is not a list (an error
            payload, None, etc.) yields no sections.
        exclude_patterns: list of regex strings. A router is dropped
            entirely if any pattern matches its name or its discovered host.
        group_map: list of {"pattern": regex, "group": name, "icon": icon}
            dicts, evaluated in order; the first match assigns a router to
            that Dashy section (and, the first time, that section's icon).
            Routers matching nothing land in `default_group`.
        default_group: section name used for routers that match no
            group_map entry.
        default_icon: icon URL applied to every generated item. Instance
            icon mapping is intentionally out of scope here; keep it simple
            and let instances layer `discovery.extra_sections` / vault
            overrides on top if they want per-item icons.

    Returns:
        list of Dashy section dicts, in first-seen order.
    """
    if not isinstance(routers, list):
        return []

    exclude_res = [re.compile(p, re.IGNORECASE) for p in (exclude_patterns or [])]
    groups = {}
    order = []

    for router in routers:
        if not isinstance(router, dict):
            continue
        name = router.get("name") or ""
        host = _extract_host(router.get("rule"))
        if not host:
            continue
        if any(regex.search(name) or regex.search(host) for regex in exclude_res):
            continue

        entry_points = router.get("entryPoints") or []
        secure = any("secure" in str(ep).lower() for ep in entry_points)
        url = "{0}://{1}".format("https" if secure else "http", host)

        group_name, group_icon = _match_group(name, host, group_map)
        group_name = group_name or default_group

        if group_name not in groups:
            groups[group_name] = {"icon": group_icon, "items": []}
            order.append(group_name)
        elif group_icon and not groups[group_name]["icon"]:
            groups[group_name]["icon"] = group_icon

        groups[group_name]["items"].append({
            "title": _title_from_router_name(name),
            "description": host,
            "url": url,
            "icon": default_icon,
        })

    sections = []
    for group_name in order:
        data = groups[group_name]
        section = {
            "name": group_name,
            "displayData": {
                "sortBy": "default",
                "rows": 1,
                "cols": 1,
                "collapsed": False,
                "hideForGuests": False,
            },
            "items": sorted(data["items"], key=lambda item: item["title"]),
        }
        if data["icon"]:
            section["icon"] = data["icon"]
        sections.append(section)
    return sections


def select_sections(discovery_enabled, discovery_available, discovered_sections,
                     static_sections, extra_sections=None):
    """Choose what a deploy renders: discovered sections (plus any pinned
    `extra_sections`) when discovery is on and the API answered, otherwise
    the instance's static `skdash.sections`. This is the one place the
    on/off + fallback decision is made, so both the Ansible task and this
    test suite share the exact same logic."""
    if discovery_enabled and discovery_available:
        return list(discovered_sections or []) + list(extra_sections or [])
    return list(static_sections or [])


class FilterModule:
    """Ansible filter plugin entry point."""

    def filters(self):
        return {
            "skdash_routers_to_sections": routers_to_sections,
            "skdash_select_sections": select_sections,
        }
