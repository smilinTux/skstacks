#!/usr/bin/env python3
"""Daily fleet image-update and runtime-security monitor. See README.md.

Usage:
    check_fleet_images.py --config config.yaml [--dry-run] [--floors floors.yaml]

Exit code is always 0 on a completed run (even with findings - this is a
report, not a gate); non-zero only on a usage/config error.
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

import notify  # noqa: E402
import php_floor  # noqa: E402
import php_probe  # noqa: E402
import registry  # noqa: E402
import state as state_mod  # noqa: E402
from imageref import parse_image_ref  # noqa: E402
from inventory import collect_cluster_inventory  # noqa: E402

log = logging.getLogger("image-monitor")


def _expand(path: str) -> Path:
    return Path(path).expanduser()


def gather_findings(config: dict, floors: dict, *, dry_run: bool) -> tuple[list[dict], list[dict]]:
    """Returns (digest_findings, urgent_findings): each a list of dicts with
    cluster/service/image/text/meta, ready for GTD capture and Talk text."""
    cache = registry.load_cache(_expand(config["cache_file"]), config.get("cache_ttl_hours", 20))
    reg_cfg = config.get("registry", {})
    store = state_mod.StateStore.load(_expand(config["state_file"])) if not dry_run else state_mod.StateStore.load(
        "/dev/null")

    digest_findings: list[dict] = []
    urgent_findings: list[dict] = []

    for cluster_name, cluster_cfg in config["clusters"].items():
        inv = collect_cluster_inventory(
            cluster_name, cluster_cfg["manager_hosts"], jump=cluster_cfg.get("jump"),
            user=cluster_cfg.get("ssh_user"), identity=cluster_cfg.get("ssh_identity"),
        )
        if inv.unreachable:
            log.warning("cluster %s: unreachable (%s)", cluster_name, inv.error)
            urgent_findings.append({
                "cluster": cluster_name, "service": "(cluster)", "image": "",
                "text": f"Cluster {cluster_name} was unreachable for today's image check: {inv.error}",
                "severity": "warn",
            })
            continue

        for svc in inv.services:
            changed = state_mod.image_changed(store, cluster_name, svc.service, svc.image)
            if not dry_run:
                store.record(cluster_name, svc.service, svc.image)

            try:
                ref = parse_image_ref(svc.image)
            except ValueError as exc:
                log.warning("%s/%s: unparsable image %r (%s)", cluster_name, svc.service, svc.image, exc)
                continue

            upstream = registry.check_upstream(
                ref, cache, timeout=reg_cfg.get("request_timeout_seconds", 15),
                max_retries=reg_cfg.get("max_retries", 4), backoff_base=reg_cfg.get("backoff_base_seconds", 5),
            )
            if upstream.digest_changed:
                digest_findings.append({
                    "cluster": cluster_name, "service": svc.service, "image": svc.image,
                    "text": f"[{cluster_name}] {svc.service} ({ref.full_repo}:{ref.tag}) has a new digest upstream: a "
                            "rebuild (base-image or security refresh) is available for the same tag.",
                    "severity": "info",
                })
            if changed:
                digest_findings.append({
                    "cluster": cluster_name, "service": svc.service, "image": svc.image,
                    "text": f"[{cluster_name}] {svc.service} is now running a different image than last check: {svc.image}",
                    "severity": "info",
                })

            for runtime_name in php_floor.runtimes_for_image(floors, ref.path):
                probe_cfg = floors[runtime_name]
                manager = inv.manager_used
                probe = php_probe.probe_runtime(
                    svc.service, probe_cfg["probe"], manager=manager, jump=cluster_cfg.get("jump"),
                    user=cluster_cfg.get("ssh_user"), identity=cluster_cfg.get("ssh_identity"),
                )
                check = php_floor.evaluate(runtime_name, floors, probe.output, probe.error)
                if check.below_floor:
                    urgent_findings.append({
                        "cluster": cluster_name, "service": svc.service, "image": svc.image,
                        "text": f"[{cluster_name}] {svc.service} is running {runtime_name} {check.version}, below the "
                                f"configured floor of {check.floor} for the {check.version.rsplit('.', 1)[0]} "
                                "line. Patch it.",
                        "severity": "error",
                    })

    return digest_findings, urgent_findings


def send_findings(config: dict, digest_findings: list[dict], urgent_findings: list[dict], *, dry_run: bool) -> None:
    notify_cfg = config.get("notify", {})
    gtd_cfg = config.get("gtd", {})

    for finding in digest_findings + urgent_findings:
        if not finding.get("image"):
            continue  # cluster-level findings have no (cluster, service, image) key to upsert on
        if dry_run:
            continue
        try:
            notify.gtd_upsert_finding(
                cluster=finding["cluster"], service=finding["service"], image=finding["image"],
                text=finding["text"], context=gtd_cfg.get("context", "@fleet"), status="next",
            )
        except Exception:  # noqa: BLE001 - a GTD write failure must not break Talk notify or the run
            log.exception("GTD capture failed for %s", finding.get("image"))

    if not digest_findings and not urgent_findings:
        log.info("no changes and nothing below floor; staying silent")
        return

    if dry_run:
        print("--- would notify ---")
        for f in urgent_findings:
            print(f"[URGENT] {f['text']}")
        for f in digest_findings:
            print(f"[DIGEST] {f['text']}")
        return

    try:
        creds = notify.load_credentials(notify_cfg.get("credentials_file", ""))
    except notify.NotConfigured as exc:
        log.warning("Talk notify skipped: %s", exc)
        return

    if urgent_findings:
        summary = "\n".join(f"- {f['text']}" for f in urgent_findings)
        notify.post_talk(creds, title="Fleet image monitor: below security floor", summary=summary,
                         severity="error", room=notify_cfg.get("urgent_room", "NAM Urgent"),
                         ref_id="image-monitor-urgent")
    if digest_findings:
        summary = "\n".join(f"- {f['text']}" for f in digest_findings)
        notify.post_talk(creds, title="Fleet image monitor: daily digest", summary=summary, severity="info",
                         room=notify_cfg.get("digest_room", "NAM Admin"), ref_id="image-monitor-digest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(SCRIPT_DIR / "config.yaml"))
    parser.add_argument("--floors", default=None, help="override floors file path from config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    config = yaml.safe_load(Path(args.config).read_text())
    floors_path = args.floors or (Path(args.config).parent / config.get("floors_file", "floors.yaml"))
    floors = php_floor.load_floors(floors_path)

    digest_findings, urgent_findings = gather_findings(config, floors, dry_run=args.dry_run)
    send_findings(config, digest_findings, urgent_findings, dry_run=args.dry_run)

    if not digest_findings and not urgent_findings:
        print("image-monitor: no changes, nothing below floor")
    else:
        print(f"image-monitor: {len(digest_findings)} digest finding(s), {len(urgent_findings)} urgent finding(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
