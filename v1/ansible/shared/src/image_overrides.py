#!/usr/bin/env python3
# Copyright (C) 2026 S&K Holding QT (Quantum Technologies)
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
"""Swap upstream images in a rendered stack file for their skpatch builds.

skpatch (.github/workflows/skpatch-images.yml) rebuilds an upstream image
with its fixable OS-package CVEs patched (Copacetic) and records the result
in v1/ansible/shared/vars/skpatch_patched_images.yml, keyed by the exact
upstream reference the templates pin. This script reads that mapping and a
rendered stack file and replaces every `image:` line whose reference is a
key with the patched reference.

Only an exact match is replaced. When Renovate moves the template to a new
tag or digest, the key no longer matches, the new upstream image deploys
as is, and the next skpatch run builds a patched copy of it. A patched
image is never applied on top of a different upstream build than the one
it was made from.

stdin: JSON {"content": str, "overrides": {upstream: {"image": patched, ...}},
             "skip": [repository, ...]}
stdout: JSON {"content": str, "replaced": [{"line", "from", "to"}]}
"""
import json
import re
import sys

IMAGE_LINE = re.compile(r'^(?P<lead>\s*(?:-\s+)?image:\s*)(?P<q>["\']?)(?P<ref>[^\s"\'#]+)(?P=q)(?P<tail>\s*(?:#.*)?)$')


def normalize(ref):
    """Compare references the way the Docker daemon resolves them."""
    for prefix in ("docker.io/library/", "docker.io/", "index.docker.io/library/", "index.docker.io/"):
        if ref.startswith(prefix):
            ref = ref[len(prefix):]
            break
    if ref.startswith("library/"):
        ref = ref[len("library/"):]
    return ref


def repository(ref):
    name = ref.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        name = name[: name.rfind(":")]
    return normalize(name)


def apply(content, overrides, skip=()):
    table = {normalize(k): v["image"] if isinstance(v, dict) else v for k, v in (overrides or {}).items()}
    skip = {normalize(s) for s in skip or ()}
    out, replaced = [], []
    for n, line in enumerate(content.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        end = line[len(body):]
        m = IMAGE_LINE.match(body)
        if m:
            ref = normalize(m.group("ref"))
            new = table.get(ref)
            if new and repository(ref) not in skip:
                body = f'{m.group("lead")}{m.group("q")}{new}{m.group("q")}{m.group("tail")}'
                replaced.append({"line": n, "from": m.group("ref"), "to": new})
        out.append(body + end)
    return "".join(out), replaced


def main():
    req = json.load(sys.stdin)
    content, replaced = apply(req["content"], req.get("overrides"), req.get("skip"))
    json.dump({"content": content, "replaced": replaced}, sys.stdout)


if __name__ == "__main__":
    main()
