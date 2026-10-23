"""Cross-reference the fleet's live inventory against the vulnerability
register (vulns.yml). See vulns.yml's header comment for the four
check.type shapes.

Every version comparison reuses php_floor.evaluate - already tested and
runtime-name agnostic - either against floors.yaml directly (check.type
"runtime_floor", today only "php") or against an ad-hoc one-entry floors
dict built from the vulns.yml entry itself (check.type "line_floor"). No
new per-line comparison code is written here; versions.py only covers the
single-floor "min_fixed_version" case, which is a different shape (a
total-order compare, not a per-line table).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

import php_floor
import versions
from exposure import ExposureResult
from imageref import ImageRef

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "informational": 4, "unknown": 5}
EXPOSURE_ORDER = {"internet-unauthenticated": 0, "internet-behind-sso-gate": 1, "internal-only": 2, "n/a": 3,
                  "unknown": 4}

ACTIONABLE_STATUSES = ("patch-available", "no-patch-yet", "needs-manual-verification", "unknown")

# "unreviewed" is NOT in ACTIONABLE_STATUSES on purpose: it is surfaced on the
# page/JSON (see summarize()'s unreviewed_count and check_fleet_vulns.py's
# gather_matches) but deliberately excluded from GTD capture. With ~130
# services across both clusters and roughly a dozen tracked images, one GTD
# item per untracked image would flood the list with low-value noise; the
# page's own "No register entry for this image" section is the review
# surface for these.
UNREVIEWED_STATUS = "unreviewed"


@dataclass
class VulnMatch:
    vuln_id: str
    cve_ids: list[str]
    aka: str
    title: str
    severity: str
    cvss: float | None
    cluster: str
    service: str
    image: str
    exposure: str
    exposure_mechanism: str
    affected: str              # "yes" | "no" | "unknown"
    probed_version: str | None
    fixed_version: str | None
    status: str                 # "patched" | "patch-available" | "no-patch-yet" | "not-affected" | "needs-manual-verification" | "unknown"
    action: str
    sources: list[str] = field(default_factory=list)
    note: str = ""


def load_register(path: str | Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(Path(path).expanduser().read_text()) or {}
    if "vulnerabilities" not in data:
        raise ValueError(f"{path}: expected a top-level 'vulnerabilities' key")
    return data["vulnerabilities"]


def images_for_entry(entry: dict[str, Any]) -> list[str]:
    return entry.get("images") or []


def matches_image(entry: dict[str, Any], image_repo: str) -> bool:
    """Whether this entry applies to a service's image repo. A fleet-wide
    entry (no `images`) never matches here - it's evaluated once per
    cluster by the caller instead, keyed on manual_findings' service.

    A namespaced pattern ("linuxserver/bookstack", "windmill-labs/windmill")
    substring-matches the full path, same as floors.yaml. A bare pattern
    ("traefik", "mariadb", "redis") matches the repo's last path segment
    EXACTLY, not as a substring: "traefik" must not match "traefik/whoami"
    (a demo app published under the traefik Hub namespace), "ldez/traefik-
    certs-dumper" or "fbonalair/traefik-crowdsec-bouncer" (companion tools
    that merely have "traefik" in their own name) - all three showed up as
    false positives against the real Traefik CVE batch before this guard
    was added.
    """
    patterns = images_for_entry(entry)
    if not patterns:
        return False
    repo_lower = image_repo.lower()
    last_segment = repo_lower.rsplit("/", 1)[-1]
    for pattern in patterns:
        pattern = pattern.lower()
        if "/" in pattern:
            if pattern in repo_lower:
                return True
        elif pattern == last_segment:
            return True
    return False


def probe_argv_for(entry: dict[str, Any], floors: dict[str, Any]) -> list[str] | None:
    """The argv to `docker exec` for this entry's check, if it needs a live
    probe rather than just the image tag. None if it doesn't."""
    check = entry.get("check", {})
    if check.get("type") == "runtime_floor":
        return floors.get(check.get("floors_ref", ""), {}).get("probe")
    if check.get("type") == "line_floor" and check.get("source") == "probe":
        return check.get("probe")
    return None


def _manual_override(entry: dict[str, Any], cluster: str, service: str) -> dict[str, Any] | None:
    for finding in entry.get("manual_findings", []):
        if finding.get("cluster") == cluster and finding.get("service", service) in (service, "*"):
            return finding
    return None


def _line_table_display(table: dict[str, int]) -> str | None:
    if not table:
        return None
    return " / ".join(f"{line}.{patch}" for line, patch in sorted(table.items()))


def _status_for(affected: str, fixed_version: str | None, *, manual: bool) -> tuple[str, str]:
    if affected == "no":
        return ("not-affected" if manual else "patched"), "No action needed."
    if affected == "yes":
        if fixed_version:
            return "patch-available", f"Upgrade to {fixed_version} or later."
        return "no-patch-yet", "Affected; no fixed version published upstream yet. Monitor the advisory."
    if manual:
        return "needs-manual-verification", "No automated check for this entry; verify by hand."
    return "unknown", "Could not determine from the probe or image tag; verify by hand."


def build_match(entry: dict[str, Any], *, cluster: str, service: str, image: str, ref: ImageRef | None,
                exposure_result: ExposureResult, probed_version: str | None, probe_error: str | None,
                floors: dict[str, Any]) -> VulnMatch:
    check = entry.get("check", {"type": "manual"})
    ctype = check.get("type", "manual")

    override = _manual_override(entry, cluster, service)
    if override is not None:
        affected = str(override.get("affected", "unknown"))
        version_seen = override.get("probed_version")
        fixed_version = override.get("fixed_version")
        note = override.get("note", "")
        status, action = _status_for(affected, fixed_version, manual=True)
        verified_at = override.get("verified_at")
        if verified_at:
            note = f"{note} (manually verified {verified_at})".strip()

    elif ctype == "runtime_floor":
        runtime = check["floors_ref"]
        fc = php_floor.evaluate(runtime, floors, probed_version, probe_error)
        fixed_version = _line_table_display(floors.get(runtime, {}).get("min_versions", {}))
        affected = "unknown" if fc.version is None else ("yes" if fc.below_floor else "no")
        version_seen, note = fc.version, fc.note
        status, action = _status_for(affected, fixed_version, manual=False)

    elif ctype == "line_floor":
        version_source = probed_version if check.get("source") == "probe" else (ref.tag if ref else None)
        probe_err = probe_error if check.get("source") == "probe" else None
        ad_hoc = {"_entry": {"parse": check.get("parse") or check.get("tag_pattern"),
                             "min_versions": check.get("min_versions", {})}}
        fc = php_floor.evaluate("_entry", ad_hoc, version_source, probe_err)
        fixed_version = _line_table_display(check.get("min_versions", {}))
        affected = "unknown" if fc.version is None else ("yes" if fc.below_floor else "no")
        version_seen, note = fc.version, fc.note
        status, action = _status_for(affected, fixed_version, manual=False)

    elif ctype == "min_fixed_version":
        tag_text = ref.tag if ref else None
        version_seen = versions.extract(tag_text, check.get("tag_pattern"))
        fixed_version = check.get("fixed_version")
        ok = versions.at_least(version_seen, fixed_version)
        affected = "unknown" if ok is None else ("no" if ok else "yes")
        note = "" if ok is not None else "could not parse a comparable version from the image tag"
        status, action = _status_for(affected, fixed_version, manual=False)

    else:  # manual, no finding recorded for this (cluster, service)
        affected, version_seen, fixed_version = "unknown", None, check.get("fixed_version")
        note = "no automated check configured for this entry; verify by hand"
        status, action = _status_for(affected, fixed_version, manual=True)

    return VulnMatch(
        vuln_id=entry["id"], cve_ids=entry.get("cve_ids", []), aka=entry.get("aka", ""),
        title=entry.get("title", entry["id"]), severity=entry.get("severity", "unknown"),
        cvss=entry.get("cvss"), cluster=cluster, service=service, image=image,
        exposure=exposure_result.exposure, exposure_mechanism=exposure_result.mechanism,
        affected=affected, probed_version=version_seen, fixed_version=fixed_version, status=status,
        action=action, sources=entry.get("sources", []), note=note,
    )


def unreviewed_match(*, cluster: str, service: str, image: str, exposure_result: ExposureResult) -> VulnMatch:
    """A running service whose image matched NO entry in vulns.yml at all.

    Every such service gets its own row with vuln_id "unreviewed" (they all
    share that id, so render_register.py's per-vuln grouping naturally
    collects them into one "No register entry for this image" section with
    no extra rendering code). Severity is "unknown" rather than a guess -
    this status means "nobody has looked yet", not "looked and found
    nothing", so it must never read as a clean bill of health.
    """
    return VulnMatch(
        vuln_id=UNREVIEWED_STATUS, cve_ids=[], aka="", title="No register entry for this image",
        severity="unknown", cvss=None, cluster=cluster, service=service, image=image,
        exposure=exposure_result.exposure, exposure_mechanism=exposure_result.mechanism,
        affected="unknown", probed_version=None, fixed_version=None, status=UNREVIEWED_STATUS,
        action="Not covered by vulns.yml yet - add an entry, or confirm by hand that no tracked CVE applies.",
        sources=[], note="",
    )


def risk_key(match: VulnMatch) -> tuple[int, int]:
    """Sort key for "ranked by severity x exposure" - lower sorts first
    (higher risk)."""
    return (SEVERITY_ORDER.get(match.severity, 5), EXPOSURE_ORDER.get(match.exposure, 4))


def summarize(matches: list[VulnMatch]) -> dict[str, Any]:
    totals: dict[str, Any] = {"total_rows": len(matches), "open_count": 0, "unreviewed_count": 0, "by_status": {},
                              "by_severity_exposure": {}}
    for m in matches:
        totals["by_status"][m.status] = totals["by_status"].get(m.status, 0) + 1
        key = f"{m.severity}/{m.exposure}"
        totals["by_severity_exposure"][key] = totals["by_severity_exposure"].get(key, 0) + 1
        if m.status in ACTIONABLE_STATUSES:
            totals["open_count"] += 1
        elif m.status == UNREVIEWED_STATUS:
            totals["unreviewed_count"] += 1
    return totals


def match_to_dict(m: VulnMatch) -> dict[str, Any]:
    return {
        "vuln_id": m.vuln_id, "cve_ids": m.cve_ids, "aka": m.aka, "title": m.title, "severity": m.severity,
        "cvss": m.cvss, "cluster": m.cluster, "service": m.service, "image": m.image, "exposure": m.exposure,
        "exposure_mechanism": m.exposure_mechanism, "affected": m.affected, "probed_version": m.probed_version,
        "fixed_version": m.fixed_version, "status": m.status, "action": m.action, "sources": m.sources,
        "note": m.note,
    }
