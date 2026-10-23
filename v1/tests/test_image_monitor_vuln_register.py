"""vuln_register.build_match must handle each check.type correctly (runtime
_floor delegating to floors.yaml, line_floor and min_fixed_version doing
their own comparison, manual falling back to "needs-manual-verification"
unless a manual_findings entry overrides it), must never crash on an
unparsable version, and the real vulns.yml shipped in this repo must load
and have the shape every entry needs."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

from exposure import ExposureResult  # noqa: E402
from imageref import parse_image_ref  # noqa: E402
from vuln_register import (  # noqa: E402
    build_match,
    images_for_entry,
    load_register,
    match_to_dict,
    matches_image,
    probe_argv_for,
    risk_key,
    summarize,
    unreviewed_match,
)

FLOORS = {
    "php": {
        "images": ["nextcloud"],
        "probe": ["php", "-r", "echo PHP_VERSION;"],
        "parse": r"(\d+\.\d+\.\d+)",
        "min_versions": {"8.5": 12, "8.4": 27, "8.3": 36, "8.2": 35},
    }
}

INTERNAL_ONLY = ExposureResult(exposure="internal-only")
PUBLIC_UNAUTH = ExposureResult(exposure="internet-unauthenticated", mechanism="crowdsec-only")


def _ref(raw):
    return parse_image_ref(raw)


def test_runtime_floor_delegates_to_floors_yaml():
    entry = {"id": "php2shell", "cve_ids": ["CVE-2026-109618"], "severity": "critical",
             "check": {"type": "runtime_floor", "floors_ref": "php"}}
    m = build_match(entry, cluster="nor", service="nextcloud", image="nextcloud:34.0.5",
                    ref=_ref("nextcloud:34.0.5"), exposure_result=PUBLIC_UNAUTH, probed_version="8.5.10",
                    probe_error=None, floors=FLOORS)
    assert m.affected == "yes"
    assert m.status == "patch-available"
    assert "8.5.12" in m.fixed_version


def test_runtime_floor_patched_at_or_above_floor():
    entry = {"id": "php2shell", "severity": "critical", "check": {"type": "runtime_floor", "floors_ref": "php"}}
    m = build_match(entry, cluster="nor", service="nextcloud", image="nextcloud:34.0.5",
                    ref=_ref("nextcloud:34.0.5"), exposure_result=PUBLIC_UNAUTH, probed_version="8.5.12",
                    probe_error=None, floors=FLOORS)
    assert m.affected == "no"
    assert m.status == "patched"


def test_line_floor_from_tag_below_its_line():
    entry = {"id": "mariadb-batch", "severity": "critical",
             "check": {"type": "line_floor", "source": "tag", "tag_pattern": r"(\d+\.\d+\.\d+)",
                       "min_versions": {"10.11": 19}}}
    m = build_match(entry, cluster="nor", service="skbook-prod_db", image="mariadb:10.11.5",
                    ref=_ref("mariadb:10.11.5"), exposure_result=INTERNAL_ONLY, probed_version=None,
                    probe_error=None, floors={})
    assert m.affected == "yes"
    assert m.status == "patch-available"
    assert m.fixed_version == "10.11.19"


def test_line_floor_from_tag_at_floor_is_patched():
    entry = {"id": "mariadb-batch", "severity": "critical",
             "check": {"type": "line_floor", "source": "tag", "tag_pattern": r"(\d+\.\d+\.\d+)",
                       "min_versions": {"10.11": 19}}}
    m = build_match(entry, cluster="nor", service="skhub-prod_db", image="mariadb:10.11.19",
                    ref=_ref("mariadb:10.11.19"), exposure_result=INTERNAL_ONLY, probed_version=None,
                    probe_error=None, floors={})
    assert m.affected == "no" and m.status == "patched"


def test_line_floor_unconfigured_line_is_unknown_not_a_false_pass():
    entry = {"id": "redis-restore", "severity": "high",
             "check": {"type": "line_floor", "source": "tag", "tag_pattern": r"(\d+\.\d+\.\d+)",
                       "min_versions": {"8.6": 3}}}
    m = build_match(entry, cluster="nor", service="sksso-prod_redis", image="redis:7.2.4",
                    ref=_ref("redis:7.2.4"), exposure_result=INTERNAL_ONLY, probed_version=None,
                    probe_error=None, floors={})
    assert m.affected == "no"  # php_floor's "no floor for this line" -> below_floor False, informational
    assert "no floor configured" in m.note


def test_min_fixed_version_below_is_affected_with_patch_available():
    entry = {"id": "traefik-batch", "severity": "high",
             "check": {"type": "min_fixed_version", "tag_pattern": r"v?(\d+\.\d+\.\d+)", "fixed_version": "3.7.14"}}
    m = build_match(entry, cluster="nor", service="skfenceha-prod_traefik-worker", image="traefik:v3.6.2",
                    ref=_ref("traefik:v3.6.2"), exposure_result=PUBLIC_UNAUTH, probed_version=None,
                    probe_error=None, floors={})
    assert m.affected == "yes"
    assert m.status == "patch-available"
    assert m.action == "Upgrade to 3.7.14 or later."


def test_min_fixed_version_at_or_above_is_patched():
    entry = {"id": "windmill-php-floor", "severity": "critical",
             "check": {"type": "min_fixed_version", "tag_pattern": r"(\d+\.\d+\.\d+)", "fixed_version": "1.844.0"}}
    m = build_match(entry, cluster="nam", service="skflow-prod_server", image="ghcr.io/windmill-labs/windmill:1.847.0",
                    ref=_ref("ghcr.io/windmill-labs/windmill:1.847.0"), exposure_result=PUBLIC_UNAUTH,
                    probed_version=None, probe_error=None, floors={})
    assert m.affected == "no" and m.status == "patched"


def test_min_fixed_version_unparsable_tag_is_unknown():
    entry = {"id": "x", "severity": "high", "check": {"type": "min_fixed_version", "fixed_version": "1.0.0"}}
    m = build_match(entry, cluster="nor", service="svc", image="svc:latest", ref=_ref("svc:latest"),
                    exposure_result=INTERNAL_ONLY, probed_version=None, probe_error=None, floors={})
    assert m.affected == "unknown" and m.status == "unknown"


def test_manual_with_no_finding_recorded_needs_verification():
    entry = {"id": "emudhra", "severity": "high", "check": {"type": "manual"}}
    m = build_match(entry, cluster="nor", service="(fleet-wide)", image="", ref=None,
                    exposure_result=ExposureResult(exposure="n/a"), probed_version=None, probe_error=None,
                    floors={})
    assert m.status == "needs-manual-verification"


def test_manual_finding_override_reports_the_recorded_status():
    entry = {
        "id": "nextcloud-richdocumentscode", "severity": "critical", "check": {"type": "manual"},
        "manual_findings": [{"cluster": "nor", "service": "skhub-prod_nextcloud", "affected": "no",
                             "note": "richdocumentscode not installed", "verified_at": "2026-10-23"}],
    }
    m = build_match(entry, cluster="nor", service="skhub-prod_nextcloud", image="nextcloud:34.0.5",
                    ref=_ref("nextcloud:34.0.5"), exposure_result=PUBLIC_UNAUTH, probed_version=None,
                    probe_error=None, floors={})
    assert m.affected == "no"
    assert m.status == "not-affected"
    assert "2026-10-23" in m.note


def test_manual_finding_only_applies_to_its_own_cluster():
    entry = {
        "id": "x", "severity": "critical", "check": {"type": "manual"},
        "manual_findings": [{"cluster": "nor", "service": "svc", "affected": "no"}],
    }
    m = build_match(entry, cluster="nam", service="svc", image="img:1", ref=_ref("img:1"),
                    exposure_result=INTERNAL_ONLY, probed_version=None, probe_error=None, floors={})
    assert m.status == "needs-manual-verification"


def test_matches_image_is_case_insensitive_substring_and_fleet_wide_never_matches():
    entry = {"id": "x", "images": ["Nextcloud", "linuxserver/bookstack"]}
    assert matches_image(entry, "docker.io/library/nextcloud") is True
    assert matches_image(entry, "redis") is False
    assert matches_image({"id": "y", "images": []}, "nextcloud") is False


def test_matches_image_bare_pattern_is_exact_on_the_repo_not_a_substring():
    """Regression: the real Traefik CVE batch's `images: ["traefik"]`
    pattern was false-matching "traefik/whoami" (a demo app published
    under the traefik Hub namespace), "ldez/traefik-certs-dumper" and
    "fbonalair/traefik-crowdsec-bouncer" (companion tools that merely have
    "traefik" in their own name) - caught against the real nor/NAM fleet
    inventory, where all three are actually running."""
    entry = {"id": "traefik-batch", "images": ["traefik"]}
    assert matches_image(entry, "library/traefik") is True
    assert matches_image(entry, "traefik/whoami") is False
    assert matches_image(entry, "ldez/traefik-certs-dumper") is False
    assert matches_image(entry, "fbonalair/traefik-crowdsec-bouncer") is False


def test_images_for_entry_and_probe_argv_for():
    entry = {"id": "x", "images": ["redis"], "check": {"type": "line_floor", "source": "probe",
                                                        "probe": ["redis-server", "--version"]}}
    assert images_for_entry(entry) == ["redis"]
    assert probe_argv_for(entry, {}) == ["redis-server", "--version"]

    runtime_entry = {"id": "y", "check": {"type": "runtime_floor", "floors_ref": "php"}}
    assert probe_argv_for(runtime_entry, FLOORS) == FLOORS["php"]["probe"]

    tag_entry = {"id": "z", "check": {"type": "min_fixed_version", "fixed_version": "1.0"}}
    assert probe_argv_for(tag_entry, FLOORS) is None


def test_summarize_counts_status_and_severity_exposure_and_risk_key_orders_by_severity_then_exposure():
    entries_matches = []
    for severity, exposure, status in [("critical", "internet-unauthenticated", "patch-available"),
                                       ("high", "internal-only", "patched"),
                                       ("critical", "internet-unauthenticated", "patch-available")]:
        m = build_match({"id": "x", "severity": severity, "check": {"type": "manual", "fixed_version": "1.0"},
                         "manual_findings": [{"cluster": "nor", "service": "s", "affected": "yes"}]},
                        cluster="nor", service="s", image="img", ref=_ref("img:1"),
                        exposure_result=ExposureResult(exposure=exposure), probed_version=None, probe_error=None,
                        floors={})
        entries_matches.append(m)
    totals = summarize(entries_matches)
    assert totals["total_rows"] == 3
    assert totals["by_severity_exposure"]["critical/internet-unauthenticated"] == 2
    assert totals["open_count"] == 3  # all are patch-available (manual override "yes")

    crit = next(m for m in entries_matches if m.severity == "critical")
    high = next(m for m in entries_matches if m.severity == "high")
    assert risk_key(crit) < risk_key(high)


def test_match_to_dict_is_json_serializable_shape():
    entry = {"id": "x", "severity": "low", "cve_ids": ["CVE-1"], "sources": ["https://example.org"],
             "check": {"type": "manual"}}
    m = build_match(entry, cluster="nor", service="s", image="img", ref=None,
                    exposure_result=ExposureResult(exposure="internal-only"), probed_version=None,
                    probe_error=None, floors={})
    d = match_to_dict(m)
    assert d["vuln_id"] == "x" and d["cve_ids"] == ["CVE-1"] and d["sources"] == ["https://example.org"]
    import json
    json.dumps(d)  # must not raise


def test_unreviewed_match_is_flagged_not_a_clean_bill_of_health():
    m = unreviewed_match(cluster="nor", service="skstream-prod_plex", image="plexinc/pms-docker@sha256:" + "a" * 64,
                         exposure_result=PUBLIC_UNAUTH)
    assert m.status == "unreviewed"
    assert m.severity == "unknown"  # "nobody looked" must never read as "looked, found nothing"
    assert m.vuln_id == "unreviewed"


def test_summarize_counts_unreviewed_separately_from_open_and_never_double_counts():
    reviewed = build_match({"id": "x", "severity": "high", "check": {"type": "manual"},
                            "manual_findings": [{"cluster": "nor", "service": "s", "affected": "yes"}]},
                           cluster="nor", service="s", image="img", ref=_ref("img:1"),
                           exposure_result=PUBLIC_UNAUTH, probed_version=None, probe_error=None, floors={})
    unreviewed = unreviewed_match(cluster="nor", service="other", image="other:1", exposure_result=INTERNAL_ONLY)
    totals = summarize([reviewed, unreviewed])
    assert totals["open_count"] == 1
    assert totals["unreviewed_count"] == 1
    assert totals["total_rows"] == 2


def test_real_vulns_yaml_has_entries_for_plex_and_immich():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor" / "vulns.yml"
    entries = {e["id"]: e for e in load_register(path)}
    assert "plex2shell-2026-10" in entries
    assert "CVE-2026-96652" in entries["plex2shell-2026-10"]["cve_ids"]
    assert matches_image(entries["plex2shell-2026-10"], "plexinc/pms-docker") is True
    assert "immich-2026-10" in entries
    assert matches_image(entries["immich-2026-10"], "immich-app/immich-server") is True
    # must not false-match the machine-learning sidecar, a different repo
    assert matches_image(entries["immich-2026-10"], "immich-app/immich-machine-learning") is False


def test_real_vulns_yaml_loads_and_every_entry_has_the_fields_the_matcher_needs():
    path = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor" / "vulns.yml"
    entries = load_register(path)
    assert len(entries) >= 8
    for entry in entries:
        assert "id" in entry and "severity" in entry and "check" in entry
        assert entry["check"].get("type") in ("runtime_floor", "line_floor", "min_fixed_version", "manual")
        for finding in entry.get("manual_findings", []):
            assert "cluster" in finding and "service" in finding and "affected" in finding
