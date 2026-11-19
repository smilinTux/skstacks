#!/usr/bin/env python3
"""Generate or check v1/ansible/shared/networks, the overlay subnet registry.

Each deploy playbook (and a few deploy.j2 fallbacks and compose templates)
carries its own default subnets; those are what a deploy actually creates.
This script collects every one of them under v1/ansible into one sorted
table, so a person can eyeball the whole map and pick a free /24 without
grepping 200 files, and so the tests can check for collisions.

Formats it understands (all seen in the tree):
  { 'name': 'skpages-prod', 'subnet': '172.16.102.0/24' }
  { 'name': 'skform-' + env, 'subnet': '172.16.139.0/24' }      (also ~ env)
  - subnet: "172.16.62.0/24"                                    (name from the file)
  {'prod': '172.16.249.0/24', 'staging': ..., 'dev': ...}[env]  (internal subnets)
Lines that only REFERENCE a subnet (trusted proxies, comments) are skipped.

The playbooks stay the source of truth. ``--check`` (v1/tests/
test_networks_registry.py) fails when the table is stale, and
v1/tests/test_unique_subnets.py fails when two services overlap.

Usage:
  python v1/scripts/networks_registry.py           # rewrite the table
  python v1/scripts/networks_registry.py --check   # exit 1 if out of date
  python v1/scripts/networks_registry.py --free    # print free 172.16.x.0/24
"""
import ipaddress
import pathlib
import re
import sys
from collections import defaultdict

V1 = pathlib.Path(__file__).resolve().parents[1]
ANSIBLE = V1 / "ansible"
REGISTRY = ANSIBLE / "shared" / "networks"
ENVS = ("prod", "staging", "dev")

CIDR = r"(172\.16\.\d+\.\d+/\d+)"
NAMED = re.compile(
    r"""name['"]?\s*:\s*['"]([\w.-]+)['"]\s*(?:(?:\+|~)\s*env\s*)?,\s*['"]?subnet['"]?\s*:\s*['"]""" + CIDR
)
NAMED_ENV = re.compile(r"""name['"]?\s*:\s*['"]([\w.-]+)['"]\s*(\+|~)\s*env""")
BARE = re.compile(r"""^\s*-?\s*subnet\s*:\s*['"]""" + CIDR)
PER_ENV = re.compile(r"""['"](prod|staging|dev)['"]\s*:\s*['"]""" + CIDR)
REFERENCE = re.compile(r"trusted|TRUSTED|proxies|PROXY", re.I)
SCAN = ("*.yml", "*.yaml", "*.j2")

HEADER = """\
# SKStacks overlay network registry (GENERATED, do not edit by hand)
#
# Every overlay subnet the framework creates by default, sorted by subnet.
# The playbooks are the source of truth; regenerate with
#   python v1/scripts/networks_registry.py
# CI fails when this file is stale (test_networks_registry.py) and when two
# services overlap (test_unique_subnets.py).
#
# Adding a service: pick from "Free" at the bottom, put it in the playbook's
# app_networks default, regenerate, commit both. An instance that overrides
# <svc>_<env>_networks in its vault must also check its own overrides and the
# live `docker network inspect` before reusing a subnet.
#
# cloud-public-<env> is shared by every service on purpose.
"""


def _env_of(path):
    m = re.search(r"-(prod|staging|dev)\.ya?ml$", path.name)
    return m.group(1) if m else None


def _service_of(path):
    rel = path.relative_to(ANSIBLE).parts
    return f"{rel[0]}/{rel[1]}" if len(rel) > 2 else str(path.relative_to(ANSIBLE))


def _base(name):
    for env in ENVS:
        if name.endswith("-" + env):
            return name[: -len(env) - 1]
    return name.rstrip("-")


def collect():
    """Return [{subnet, name, base, service, where}] for every subnet a deploy creates."""
    rows = []
    for pattern in SCAN:
        for path in sorted(ANSIBLE.rglob(pattern)):
            if path == REGISTRY or "/files/" in str(path):
                continue
            env = _env_of(path)
            svc = _service_of(path)
            where = path.name
            for line in path.read_text(errors="replace").splitlines():
                code = line.split("#", 1)[0] if not line.lstrip().startswith("{%") else line
                if "172.16." not in code or REFERENCE.search(code):
                    continue
                found = False
                for m in NAMED.finditer(code):
                    name = m.group(1)
                    tail = code[m.start():m.end()]
                    if NAMED_ENV.search(tail):
                        name = f"{name}{env or '<env>'}"
                    rows.append(dict(subnet=m.group(2), name=name, where=where, svc=svc))
                    found = True
                if found:
                    continue
                m = BARE.search(code)
                if m:
                    name = f"{path.parent.name}-{env}" if env else f"{path.parent.name}-<env>"
                    rows.append(dict(subnet=m.group(1), name=name, where=where, svc=svc))
                    continue
                for m in PER_ENV.finditer(code):
                    rows.append(dict(subnet=m.group(2), name=f"{path.parent.name}-{m.group(1)} (internal)",
                                     where=where, svc=svc))
    for r in rows:
        r["base"] = _base(r["name"].replace(" (internal)", "").replace("<env>", "prod"))
        r["net"] = ipaddress.ip_network(r["subnet"], strict=False)
    return rows


def clashes(rows):
    """Subnets that overlap across two different networks (cloud-public excluded)."""
    owned = [r for r in rows if not r["base"].startswith("cloud-public")]
    out = defaultdict(set)
    for i, a in enumerate(owned):
        for b in owned[i + 1:]:
            if a["base"] != b["base"] and a["net"].overlaps(b["net"]):
                out[f'{a["subnet"]} / {b["subnet"]}'].update(
                    {f'{a["name"]} ({a["svc"]})', f'{b["name"]} ({b["svc"]})'})
    return {k: sorted(v) for k, v in out.items()}


def render(rows):
    table = defaultdict(set)
    named = {(r["subnet"], r["base"]): r["name"] for r in rows if "<env>" not in r["name"]}
    for r in rows:
        # A deploy.j2 fallback ("skbook-<env>") that repeats a playbook default
        # folds into that row; one with no playbook twin keeps its own row.
        name = named.get((r["subnet"], r["base"]), r["name"]) if "<env>" in r["name"] else r["name"]
        if r["base"].startswith("cloud-public"):
            table[(r["subnet"], name)] = {"(shared by every service)"}
        else:
            table[(r["subnet"], name)].add(r["svc"])
    out = [HEADER, f"{'SUBNET':<22}{'NETWORK':<34}DEFINED IN"]
    for (subnet, name) in sorted(table, key=lambda k: (ipaddress.ip_network(k[0], strict=False), k[1])):
        out.append(f"{subnet:<22}{name:<34}{', '.join(sorted(table[(subnet, name)]))}")
    nets = [r["net"] for r in rows]
    free = [str(n) for n in ipaddress.ip_network("172.16.0.0/16").subnets(new_prefix=24)
            if 60 <= n.network_address.packed[2] <= 199 and not any(n.overlaps(u) for u in nets)]
    out += ["", "# Free /24s in 172.16.60.0 to 172.16.199.0 (no framework default touches them):"]
    for i in range(0, len(free), 6):
        out.append("#   " + "  ".join(free[i:i + 6]))
    return "\n".join(out) + "\n", free


def main(argv):
    rows = collect()
    text, free = render(rows)
    if "--free" in argv:
        print("\n".join(free))
        return 0
    if "--check" in argv:
        current = REGISTRY.read_text() if REGISTRY.exists() else ""
        if current != text:
            print(f"{REGISTRY.relative_to(V1.parent)} is stale: run python v1/scripts/networks_registry.py",
                  file=sys.stderr)
            return 1
        return 0
    REGISTRY.write_text(text)
    print(f"wrote {REGISTRY.relative_to(V1.parent)} ({len(text.splitlines())} lines)")
    bad = clashes(rows)
    for k, v in bad.items():
        print(f"CLASH {k}: {', '.join(v)}", file=sys.stderr)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
