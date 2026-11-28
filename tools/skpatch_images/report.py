#!/usr/bin/env python3
"""Helpers over Trivy JSON for patch_one.sh.

  report.py count <trivy.json>          -> {"critical": n, "high": n}
  report.py key <trivy.json>            -> 12 hex chars: what to patch, from what
  report.py tag <ref> <key>             -> tag inside ghcr.io/smilintux/skpatched
  report.py config <inspect.json> <inspect.json>  exit 1 if the run config differs
"""
import hashlib
import json
import re
import sys


def vulns(path):
    d = json.load(open(path))
    for r in d.get("Results") or []:
        for v in r.get("Vulnerabilities") or []:
            yield v


def count(path):
    c = {"critical": 0, "high": 0}
    for v in vulns(path):
        s = v.get("Severity", "").lower()
        if s in c and v.get("FixedVersion"):
            c[s] += 1
    return c


def key(path):
    d = json.load(open(path))
    meta = d.get("Metadata") or {}
    base = ",".join(sorted(meta.get("RepoDigests") or [])) or meta.get("ImageID", "")
    fixes = sorted({f'{v["PkgName"]}={v["FixedVersion"]}' for v in vulns(path) if v.get("FixedVersion")})
    return hashlib.sha256((base + "\n" + "\n".join(fixes)).encode()).hexdigest()[:12]


def tag(ref, k):
    name = ref.split("@", 1)[0]
    last = name.rsplit("/", 1)[-1]
    repo, t = (name.rsplit(":", 1) if ":" in last else (name, "latest"))
    for p in ("docker.io/library/", "docker.io/"):
        if repo.startswith(p):
            repo = repo[len(p):]
    s = re.sub(r"[^A-Za-z0-9_.-]", ".", f"{repo}-{t}").strip(".-")
    return f"{s[:110]}-skp{k}"


RUN_KEYS = ("Entrypoint", "Cmd", "User", "WorkingDir", "Env", "ExposedPorts", "Volumes", "StopSignal")


def config(a, b):
    ca = json.load(open(a))[0]["Config"]
    cb = json.load(open(b))[0]["Config"]
    diff = [k for k in RUN_KEYS if ca.get(k) != cb.get(k)]
    if diff:
        print("run config differs: " + ", ".join(diff))
        return 1
    return 0


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "count":
        print(json.dumps(count(sys.argv[2])))
    elif cmd == "key":
        print(key(sys.argv[2]))
    elif cmd == "tag":
        print(tag(sys.argv[2], sys.argv[3]))
    elif cmd == "config":
        sys.exit(config(sys.argv[2], sys.argv[3]))
    else:
        sys.exit(f"unknown command {cmd}")
