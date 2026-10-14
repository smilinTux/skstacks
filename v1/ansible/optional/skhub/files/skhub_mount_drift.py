#!/usr/bin/env python3
"""Refuse a skhub redeploy that would drop a live bind mount.

The deploy replaces the stack from the freshly rendered compose file. A bind
mount that exists on a running service but not in the new render (added by
hand with `docker service update --mount-add`, or from a vault key since
removed) is silently dropped by that replace. This compares the two before
the compose file is rewritten.

Reads one JSON object on stdin:

    {"rendered": <the rendered compose file, parsed>,
     "live": "<one line per running service of the stack:
               JSON service name, a space, JSON .Spec...ContainerSpec.Mounts>",
     "allow_drop": false}

Only services present in BOTH are judged (a service the render no longer has
is not removed by `docker stack deploy`). Only live mounts of Type bind count;
a dropped one is one whose Target no rendered volume of that service has (a
changed source for the same target is not a drop). Prints one
"<service>: <target> (source <source>)" line per drop. Exit 1 when there is a
drop and allow_drop is false, 2 on unreadable input (fail closed), 0 otherwise.
"""
import argparse
import json
import sys


def rendered_targets(svc):
    out = set()
    for v in (svc or {}).get("volumes") or []:
        if isinstance(v, dict):
            if v.get("target"):
                out.add(v["target"])
        else:
            parts = str(v).split(":")
            out.add(parts[1] if len(parts) > 1 else parts[0])
    return out


def live_mounts(text):
    out = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        name, sep, mounts = line.strip().partition(" ")
        if not sep:
            raise ValueError(f"unexpected live line: {line!r}")
        out[json.loads(name)] = json.loads(mounts) or []
    return out


def drops(rendered, live, stack):
    found = []
    services = (rendered or {}).get("services") or {}
    for full, mounts in sorted(live.items()):
        prefix = stack + "_"
        if not full.startswith(prefix) or full[len(prefix):] not in services:
            continue
        have = rendered_targets(services[full[len(prefix):]])
        for m in mounts:
            if m.get("Type") == "bind" and m.get("Target") not in have:
                found.append((full, m.get("Target"), m.get("Source")))
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stack", required=True, help="stack namespace, e.g. skhub-prod")
    args = ap.parse_args(argv)
    try:
        data = json.load(sys.stdin)
        found = drops(data["rendered"], live_mounts(data.get("live") or ""), args.stack)
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        print(f"skhub mount drift guard: cannot read the live or rendered mounts ({e}); refusing", file=sys.stderr)
        return 2
    if not found:
        print("skhub mount drift guard: no live bind mount is dropped by this render")
        return 0
    lines = "\n".join(f"  {svc}: {tgt} (source {src})" for svc, tgt, src in found)
    if data.get("allow_drop"):
        print("skhub mount drift guard: ALLOW_MOUNT_DROP is set, these live bind mounts WILL BE DROPPED:\n" + lines)
        return 0
    msg = ("skhub mount drift guard: REFUSING to redeploy, these live bind mounts are not in the new render "
           "and would be dropped:\n" + lines + "\n"
           "Add them to skhub.EXTRA_MOUNTS (README \"Extra bind mounts\"), or set skhub.ALLOW_MOUNT_DROP: true "
           "for one deploy if dropping them is intended.")
    print(msg)
    print(msg, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
