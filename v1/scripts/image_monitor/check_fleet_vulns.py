#!/usr/bin/env python3
"""Fleet vulnerability register: cross-references vulns.yml against live
cluster inventory, writes a private status JSON + human-readable HTML
page, and upserts actionable findings into the unified GTD.

Usage:
    check_fleet_vulns.py --config /path/to/your/config.yaml [--vulns vulns.yml]
                         [--floors floors.yaml] [--dry-run]

Shares --config with check_fleet_images.py (the same `clusters` block); run
both from the same systemd timer, this one right after the other, since
both need the same ssh reachability and inventory shape. No second timer
is created by this tool - see README.md "Running by hand".

Exit code is always 0 on a completed run (this is a report, not a gate);
non-zero only on a usage/config error.
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import yaml  # noqa: E402

import exposure  # noqa: E402
import notify  # noqa: E402
import php_floor  # noqa: E402
import php_probe  # noqa: E402
import vuln_register  # noqa: E402
from imageref import parse_image_ref  # noqa: E402
from inventory import collect_cluster_inventory  # noqa: E402
from render_register import render_html  # noqa: E402

log = logging.getLogger("vuln-register")


def _expand(path) -> Path:
    return Path(path).expanduser()


def gather_matches(config: dict, vulns: list[dict], floors: dict) -> list[vuln_register.VulnMatch]:
    matches: list[vuln_register.VulnMatch] = []
    fleet_wide = [e for e in vulns if not vuln_register.images_for_entry(e)]

    for cluster_name, cluster_cfg in config["clusters"].items():
        inv = collect_cluster_inventory(
            cluster_name, cluster_cfg["manager_hosts"], jump=cluster_cfg.get("jump"),
            user=cluster_cfg.get("ssh_user"), identity=cluster_cfg.get("ssh_identity"),
        )
        if inv.unreachable:
            log.warning("cluster %s unreachable: %s", cluster_name, inv.error)
            continue

        manager, jump = inv.manager_used, cluster_cfg.get("jump")
        user, identity = cluster_cfg.get("ssh_user"), cluster_cfg.get("ssh_identity")

        for entry in fleet_wide:
            # A fleet-wide entry (empty `images`) has no per-service image to
            # match against, so it's evaluated once per cluster. Use the
            # manual_findings' own service label (e.g. "(host kernel)") when
            # one was recorded for this cluster, else a generic placeholder
            # so an un-verified fleet-wide entry still shows up as a row.
            service = next(
                (f["service"] for f in entry.get("manual_findings", []) if f.get("cluster") == cluster_name),
                "(fleet-wide)",
            )
            matches.append(vuln_register.build_match(
                entry, cluster=cluster_name, service=service, image="", ref=None,
                exposure_result=exposure.ExposureResult(exposure="n/a"), probed_version=None,
                probe_error=None, floors=floors,
            ))

        for svc in inv.services:
            try:
                ref = parse_image_ref(svc.image)
            except ValueError:
                log.warning("%s/%s: unparsable image %r", cluster_name, svc.service, svc.image)
                continue

            labels = exposure.fetch_service_labels(svc.service, manager=manager, jump=jump, user=user,
                                                    identity=identity)
            exp = exposure.classify_exposure(labels)

            applicable = [e for e in vulns if vuln_register.matches_image(e, ref.path)]
            if not applicable:
                # Nothing in vulns.yml mentions this image at all - surface
                # it as "unreviewed" rather than silently dropping it. This
                # is how a real app (Plex, Immich) ends up missing from the
                # register in the first place: it just never got a row.
                matches.append(vuln_register.unreviewed_match(
                    cluster=cluster_name, service=svc.service, image=svc.image, exposure_result=exp,
                ))
                continue
            probe_cache: dict[str, php_probe.ProbeResult] = {}

            for entry in applicable:
                probe_argv = vuln_register.probe_argv_for(entry, floors)
                probed_version, probe_error = None, None
                if probe_argv:
                    cache_key = " ".join(probe_argv)
                    if cache_key not in probe_cache:
                        probe_cache[cache_key] = php_probe.probe_runtime(
                            svc.service, probe_argv, manager=manager, jump=jump, user=user, identity=identity,
                        )
                    pr = probe_cache[cache_key]
                    probed_version, probe_error = pr.output, pr.error

                matches.append(vuln_register.build_match(
                    entry, cluster=cluster_name, service=svc.service, image=svc.image, ref=ref,
                    exposure_result=exp, probed_version=probed_version, probe_error=probe_error, floors=floors,
                ))

    return matches


def write_outputs(config: dict, matches: list[vuln_register.VulnMatch]) -> tuple[Path, Path]:
    html_path = _expand(config.get("register_html", "~/.skcapstone/image-monitor/vuln-register.html"))
    json_path = _expand(config.get("register_json", "~/.skcapstone/image-monitor/vuln-register.json"))
    html_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(render_html(matches, generated_at=datetime.datetime.now(datetime.timezone.utc)))
    json_path.write_text(json.dumps([vuln_register.match_to_dict(m) for m in matches], indent=2, sort_keys=True))
    return html_path, json_path


def capture_gtd(matches: list[vuln_register.VulnMatch], *, dry_run: bool) -> int:
    actionable = [m for m in matches if m.status in vuln_register.ACTIONABLE_STATUSES]
    if dry_run:
        return len(actionable)
    captured = 0
    for m in actionable:
        cve_or_id = ", ".join(m.cve_ids) if m.cve_ids else m.vuln_id
        # source_ref keys on our own vuln_id, NOT the first CVE id: two
        # distinct tracked rows can legitimately share the same CVE (e.g.
        # php2shell-2026-10's live-PHP-probe row and windmill-php-floor-
        # 2026-10's image-floor row both cite CVE-2026-109618/109619 for
        # the same service). Keying on cve_ids[0] collided those two into
        # one GTD item and silently dropped the other - caught on a live
        # run against skflow-prod_server. vuln_id is unique per entry by
        # construction (vulns.yml's own "id" field), so it can't collide;
        # the CVE id(s) are still in the GTD item's text and meta.
        text = f"[{m.cluster}] {m.service}: {m.title} ({cve_or_id}) - {m.action}"
        try:
            notify.gtd_upsert_finding(
                cluster=m.cluster, service=m.service, image=m.image, text=text, status="next", context="@fleet",
                source="vuln-register", source_ref=f"{m.vuln_id}/{m.cluster}/{m.service}",
                meta={"severity": m.severity, "exposure": m.exposure, "status": m.status, "cve_ids": m.cve_ids},
            )
            captured += 1
        except Exception:  # noqa: BLE001 - a GTD write failure must not break the run
            log.exception("GTD capture failed for %s/%s/%s", m.vuln_id, m.cluster, m.service)
    return captured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, help="path to your private config.yaml (see config.example.yaml)")
    parser.add_argument("--vulns", default=None, help="override the vulns.yml path from config")
    parser.add_argument("--floors", default=None, help="override floors.yaml path from config")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    config = yaml.safe_load(Path(args.config).read_text())
    config_dir = Path(args.config).parent
    vulns_path = args.vulns or (config_dir / config.get("vulns_file", "vulns.yml"))
    floors_path = args.floors or (config_dir / config.get("floors_file", "floors.yaml"))

    vulns = vuln_register.load_register(vulns_path)
    floors = php_floor.load_floors(floors_path)

    matches = gather_matches(config, vulns, floors)
    totals = vuln_register.summarize(matches)

    if args.dry_run:
        html_path, json_path = "(dry-run, not written)", "(dry-run, not written)"
    else:
        html_path, json_path = write_outputs(config, matches)

    captured = capture_gtd(matches, dry_run=args.dry_run)

    print(f"vuln-register: {totals['total_rows']} row(s), {totals['open_count']} open/actionable, "
          f"{captured} GTD item(s) captured, wrote {html_path} and {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
