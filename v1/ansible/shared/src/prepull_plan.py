#!/usr/bin/env python3
"""Plan which swarm nodes must hold which images before a `docker stack deploy`.

Used by shared/tasks/prepull_images.yml. Reads one JSON document on stdin:

    {"compose": <the rendered stack file, parsed>,
     "nodes":   <the output of `docker node inspect` for every node>,
     "respect_placement": true}

and prints one JSON document:

    {"nodes":   {"<swarm hostname>": ["<image>", ...]},   # what to pull where
     "skipped": [{"service": "...", "image": "...", "reason": "..."}],
     "unplaced": ["<service>", ...]}                        # no eligible node

Only Ready nodes with Availability "active" are candidates: a drained, paused
or down node gets no new task, so a pull there proves nothing and a refusal
there must not block the deploy. A service's `deploy.placement.constraints`
narrow the candidates when `respect_placement` is true (the default). A
constraint this planner does not understand is treated as satisfied, so an
unknown rule can only cause an extra pull, never a missed one.

Standard library only: it runs on the Ansible controller.
"""
import json
import sys

def _node_facts(node):
    """The values a constraint can compare, from one `docker node inspect` object."""
    spec = node.get("Spec") or {}
    desc = node.get("Description") or {}
    platform = desc.get("Platform") or {}
    engine = desc.get("Engine") or {}
    status = node.get("Status") or {}
    return {
        "id": node.get("ID", ""),
        "hostname": desc.get("Hostname", ""),
        "ip": status.get("Addr", ""),
        "role": spec.get("Role", ""),
        "os": platform.get("OS", ""),
        "arch": platform.get("Architecture", ""),
        "node_labels": spec.get("Labels") or {},
        "engine_labels": engine.get("Labels") or {},
        "state": status.get("State", ""),
        "availability": spec.get("Availability", ""),
    }


def eligible(node):
    """A node that can be handed a new task: Ready and Availability active."""
    f = _node_facts(node)
    return str(f["state"]).lower() == "ready" and str(f["availability"]).lower() == "active"


def _split_constraint(text):
    """('key', '==' or '!=', 'value') or None when it is not `key op value`."""
    for op in ("==", "!="):
        if op in text:
            key, _, value = text.partition(op)
            return key.strip(), op, value.strip().strip("'\"")
    return None


def _constraint_matches(constraint, facts):
    parsed = _split_constraint(str(constraint))
    if parsed is None:
        return True
    key, op, want = parsed
    have = None
    known = True
    if key == "node.id":
        have = facts["id"]
    elif key == "node.hostname":
        have = facts["hostname"]
    elif key == "node.ip":
        have = facts["ip"]
    elif key == "node.role":
        have = facts["role"]
    elif key == "node.platform.os":
        have = facts["os"]
    elif key == "node.platform.arch":
        have = facts["arch"]
    elif key.startswith("node.labels."):
        have = facts["node_labels"].get(key[len("node.labels."):])
    elif key.startswith("engine.labels."):
        have = facts["engine_labels"].get(key[len("engine.labels."):])
    else:
        known = False
    if not known:
        return True
    # Docker matches case-insensitively; a label that is not set equals "".
    same = str(have if have is not None else "").lower() == want.lower()
    return same if op == "==" else not same


def _constraints(service):
    placement = ((service.get("deploy") or {}).get("placement") or {})
    raw = placement.get("constraints") or []
    if isinstance(raw, str):
        raw = [raw]
    return [c for c in raw if isinstance(c, str)]


def _replicas_zero(service):
    deploy = service.get("deploy") or {}
    if str(deploy.get("mode", "replicated")).lower() == "global":
        return False
    replicas = deploy.get("replicas")
    return replicas is not None and str(replicas).strip() == "0"


def plan(compose, nodes, respect_placement=True):
    services = (compose or {}).get("services") or {}
    candidates = [n for n in nodes if eligible(n)]
    per_node = {}
    skipped = []
    unplaced = []
    for name in sorted(services):
        service = services[name] or {}
        image = service.get("image")
        if not image or not isinstance(image, str):
            continue  # build-only or malformed: nothing to pull
        image = image.strip()
        if "$" in image:
            skipped.append({"service": name, "image": image, "reason": "image reference is not resolved in the stack file"})
            continue
        if _replicas_zero(service):
            skipped.append({"service": name, "image": image, "reason": "replicas is 0"})
            continue
        wanted = _constraints(service) if respect_placement else []
        targets = [n for n in candidates if all(_constraint_matches(c, _node_facts(n)) for c in wanted)]
        if not targets:
            unplaced.append(name)
            continue
        for node in targets:
            host = _node_facts(node)["hostname"]
            images = per_node.setdefault(host, [])
            if image not in images:
                images.append(image)
    return {"nodes": per_node, "skipped": skipped, "unplaced": unplaced}


def main(argv=None):
    data = json.load(sys.stdin)
    result = plan(data.get("compose") or {}, data.get("nodes") or [], bool(data.get("respect_placement", True)))
    json.dump(result, sys.stdout, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
