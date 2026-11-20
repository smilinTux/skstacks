#!/usr/bin/env python3
"""skpatch image vulnerabilities: scan the images one estate's swarm runs.

Lists the services on a swarm manager over ssh (read-only `docker service
ls`), scans each unique image with Trivy run as a container on this
controller (never on a swarm node), and writes one JSON summary that the
daily digest (skpatch_watch.py --digest --image-vulns <summary>) turns into
its "Image vulnerabilities" lines.

Only HIGH and CRITICAL findings that have a fixed version are kept (Trivy's
--ignore-unfixed): what an image rebuild or bump would actually fix.

Results are cached per image digest for --ttl-hours (24): a rerun the same
day scans nothing new. A scan that fails for one image is recorded in the
summary as that image's error, never fatal, and is not cached (the next run
tries again).

Summary (--out):
    {"estate", "generated_at", "trivy", "manager", "error",
     "totals": {"images", "scanned", "failed", "critical", "high", "ignored"},
     "images": [{"image", "services", "digest", "status": "ok"|"failed",
                 "error", "scanned_at", "cached", "ignored",
                 "critical", "high",
                 "vulns": [{"id", "severity", "package", "installed",
                            "fixed", "target", "title"}]}]}

Ignore file (--ignore, optional): one rule per line, "#" starts a comment,
    <vulnerability id> [<image glob>]
e.g. "CVE-2024-1234 ghcr.io/example/app:*  # not reachable, see ticket 12".
A rule without a glob silences that id in every image. The glob is matched
(fnmatch) against the image reference without its @sha256 digest. Ignored
findings are counted ("ignored"), never listed.

Usage:
    skpatch_image_vulns.py --ssh <swarm manager> --estate <estate> \\
        --out ~/.local/state/skpatch-hosts/image-vulns/<estate>.json \\
        --cache-dir ~/.cache/skpatch-hosts/image-vulns/<estate> [--ignore <file>]
Exit 0: every image scanned; 1: the summary was written but at least one
image failed (listed in it); 2: the service list could not be read (the
summary carries the error, so the digest says so).
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

# aquasec/trivy 0.76.2 (2026-11-05), the multi-arch index digest as served by
# registry-1.docker.io (GET /v2/aquasec/trivy/manifests/0.76.2,
# Docker-Content-Digest). Bump both together.
TRIVY_IMAGE = "aquasec/trivy:0.76.2@sha256:8f6bb947dbc528902e11f2f5be52f7b519f6a39d5d148f41166f2040499768fb"
SEVERITIES = ("CRITICAL", "HIGH")
LIST_CMD = ("set -eu; D=docker; $D info >/dev/null 2>&1 || D='sudo -n docker'; "
            "$D service ls --format '{{.Name}}\t{{.Image}}'")


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def list_services(ssh: list[str], timeout: int = 120) -> dict[str, list[str]]:
    """{image reference: [service names]} from the manager's service list."""
    proc = subprocess.run([*ssh, LIST_CMD], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"docker service ls failed (exit {proc.returncode}): {proc.stderr.strip()[-400:]}")
    images: dict[str, list[str]] = {}
    for line in proc.stdout.splitlines():
        name, sep, image = line.strip().partition("\t")
        if not sep or not image.strip():
            continue
        images.setdefault(image.strip(), []).append(name.strip())
    return {k: sorted(v) for k, v in sorted(images.items())}


def image_digest(ref: str) -> str | None:
    _, sep, digest = ref.partition("@")
    return digest if sep and digest.startswith("sha256:") else None


def display_name(ref: str) -> str:
    """The reference without its digest: what a person reads."""
    return ref.partition("@")[0]


def cache_key(ref: str) -> str:
    d = image_digest(ref)
    if d:
        return d.split(":", 1)[1]
    # an unpinned tag: cache by the reference itself (it can move, so the
    # 24 hour TTL is the only freshness there)
    return "ref-" + hashlib.sha256(ref.encode()).hexdigest()


def parse_trivy(doc: dict) -> list[dict]:
    """Fixable HIGH/CRITICAL findings from one `trivy image --format json`
    report, one row per (id, package, installed version), severest first."""
    rows: dict[tuple, dict] = {}
    for result in doc.get("Results") or []:
        for v in result.get("Vulnerabilities") or []:
            sev = str(v.get("Severity") or "").upper()
            if sev not in SEVERITIES or not v.get("FixedVersion"):
                continue
            key = (v.get("VulnerabilityID"), v.get("PkgName"), v.get("InstalledVersion"))
            rows.setdefault(key, {
                "id": v.get("VulnerabilityID"), "severity": sev, "package": v.get("PkgName"),
                "installed": v.get("InstalledVersion"), "fixed": v.get("FixedVersion"),
                "target": result.get("Target"), "title": (v.get("Title") or "")[:160] or None,
            })
    order = {s: i for i, s in enumerate(SEVERITIES)}
    return sorted(rows.values(), key=lambda r: (order[r["severity"]], str(r["id"]), str(r["package"])))


def trivy_command(ref: str, trivy_image: str, db_dir: Path, docker: str = "docker",
                  registry_auth: Path | None = None) -> list[str]:
    cmd = [*shlex.split(docker), "run", "--rm", "-v", f"{db_dir}:/root/.cache"]
    if registry_auth:
        cmd += ["-v", f"{registry_auth}:/root/.docker/config.json:ro"]
    return cmd + [trivy_image, "image", "--quiet", "--format", "json", "--severity", ",".join(reversed(SEVERITIES)),
                  "--ignore-unfixed", ref]


def trivy_error(stderr: str) -> str:
    """The useful part of a failed scan: Trivy lists every image source it
    tried (docker, containerd, podman, remote); with no daemon mounted only
    the registry's answer matters."""
    text = " ".join(l.strip() for l in stderr.splitlines() if l.strip())
    _, sep, remote = text.partition("remote error:")
    return (remote.strip() if sep else text)[-300:] or "no error output"


def scan(ref: str, trivy_image: str, db_dir: Path, docker: str = "docker", timeout: int = 900,
         registry_auth: Path | None = None) -> list[dict]:
    db_dir.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(trivy_command(ref, trivy_image, db_dir, docker, registry_auth),
                          capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise RuntimeError(f"trivy exit {proc.returncode}: {trivy_error(proc.stderr)}")
    try:
        return parse_trivy(json.loads(proc.stdout))
    except ValueError as e:
        raise RuntimeError(f"trivy printed no JSON report: {e}") from None


def read_cache(path: Path, trivy_image: str, ttl: dt.timedelta, now: dt.datetime) -> dict | None:
    try:
        c = json.loads(path.read_text())
        at = dt.datetime.fromisoformat(c["scanned_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if c.get("trivy") != trivy_image or now - at > ttl or now < at:
        return None
    return c


def load_ignores(path: Path | None) -> list[tuple[str, str]]:
    if not path:
        return []
    path = path.expanduser()
    if not path.exists():
        print(f"ignore file {path} does not exist: nothing ignored", file=sys.stderr)
        return []
    rules = []
    for line in path.read_text().splitlines():
        words = line.split("#", 1)[0].split()
        if words:
            rules.append((words[0], words[1] if len(words) > 1 else "*"))
    return rules


def ignored(vuln_id: str, ref: str, rules: list[tuple[str, str]]) -> bool:
    name = display_name(ref)
    return any(vuln_id == rid and fnmatch.fnmatchcase(name, glob) for rid, glob in rules)


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def build_summary(estate: str, manager: str, services: dict[str, list[str]], cache_dir: Path,
                  trivy_image: str, ttl: dt.timedelta, rules: list[tuple[str, str]], scanner=None,
                  now: dt.datetime | None = None) -> dict:
    """Scan (or reuse the cache for) each image. scanner(ref) -> vulns, for tests."""
    now = now or now_utc()
    results = cache_dir / "results"
    scanner = scanner or (lambda ref: scan(ref, trivy_image, cache_dir / "trivy"))
    images = []
    for ref, names in services.items():
        entry = {"image": ref, "services": names, "digest": image_digest(ref), "status": "ok", "error": None,
                 "scanned_at": None, "cached": False, "ignored": 0, "critical": 0, "high": 0, "vulns": []}
        cpath = results / f"{cache_key(ref)}.json"
        cached = read_cache(cpath, trivy_image, ttl, now)
        if cached is not None:
            vulns, entry["scanned_at"], entry["cached"] = cached["vulns"], cached["scanned_at"], True
        else:
            try:
                vulns = scanner(ref)
            except (RuntimeError, subprocess.SubprocessError, OSError) as err:
                entry.update(status="failed", error=str(err)[:500])
                images.append(entry)
                continue
            entry["scanned_at"] = now.isoformat(timespec="seconds")
            write_json(cpath, {"image": ref, "scanned_at": entry["scanned_at"], "trivy": trivy_image, "vulns": vulns})
        keep = [v for v in vulns if not ignored(v["id"], ref, rules)]
        entry["ignored"] = len(vulns) - len(keep)
        entry["vulns"] = keep
        entry["critical"] = sum(v["severity"] == "CRITICAL" for v in keep)
        entry["high"] = sum(v["severity"] == "HIGH" for v in keep)
        images.append(entry)
    ok = [i for i in images if i["status"] == "ok"]
    return {
        "estate": estate, "manager": manager, "generated_at": now.isoformat(timespec="seconds"),
        "trivy": trivy_image, "error": None,
        "totals": {"images": len(images), "scanned": len(ok), "failed": len(images) - len(ok),
                   "critical": sum(i["critical"] for i in ok), "high": sum(i["high"] for i in ok),
                   "ignored": sum(i["ignored"] for i in ok)},
        "images": images,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ssh", default=os.environ.get("SKPW_SWARM_SSH"),
                    help="ssh destination of a swarm manager (default $SKPW_SWARM_SSH)")
    ap.add_argument("--ssh-option", action="append", default=[], help="extra ssh argument (repeatable)")
    ap.add_argument("--estate", required=True)
    ap.add_argument("--out", type=Path, required=True, help="summary JSON the digest reads")
    ap.add_argument("--cache-dir", type=Path, required=True, help="Trivy DB (trivy/) and per-digest results (results/)")
    ap.add_argument("--ttl-hours", type=float, default=24)
    ap.add_argument("--ignore", type=Path, default=os.environ.get("SKPW_IMAGE_IGNORE") or None,
                    help="ignore file (default $SKPW_IMAGE_IGNORE)")
    ap.add_argument("--trivy-image", default=TRIVY_IMAGE, help="must carry @sha256:")
    ap.add_argument("--docker", default="docker", help="local docker command")
    ap.add_argument("--registry-auth", type=Path, help="docker config.json for private registries, mounted read-only")
    ap.add_argument("--scan-timeout", type=int, default=900, help="seconds per image")
    args = ap.parse_args()
    if not args.ssh:
        ap.error("--ssh (or SKPW_SWARM_SSH) is required")
    if "@sha256:" not in args.trivy_image:
        ap.error("--trivy-image must be pinned by digest (@sha256:)")

    now = now_utc()
    ssh = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", *args.ssh_option, args.ssh]
    try:
        services = list_services(ssh)
    except (RuntimeError, subprocess.SubprocessError, OSError) as err:
        print(f"image scan failed: {err}", file=sys.stderr)
        write_json(args.out, {"estate": args.estate, "manager": args.ssh, "generated_at": now.isoformat(timespec="seconds"),
                              "trivy": args.trivy_image, "error": str(err)[:500], "totals": {}, "images": []})
        return 2
    rules = load_ignores(args.ignore)
    summary = build_summary(
        args.estate, args.ssh, services, args.cache_dir, args.trivy_image, dt.timedelta(hours=args.ttl_hours), rules,
        scanner=lambda ref: scan(ref, args.trivy_image, args.cache_dir / "trivy", args.docker, args.scan_timeout,
                                 args.registry_auth), now=now)
    write_json(args.out, summary)
    t = summary["totals"]
    for i in summary["images"]:
        state = (f"failed: {i['error']}" if i["status"] == "failed" else
                 f"{i['critical']} critical, {i['high']} high" + (" (cached)" if i["cached"] else ""))
        print(f"{display_name(i['image'])}: {state}")
    print(f"{t['images']} image(s), {t['failed']} failed; {t['critical']} critical, {t['high']} high, "
          f"{t['ignored']} ignored -> {args.out}")
    return 1 if t["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
