"""check_fleet_vulns.gather_matches must wire inventory + exposure + probes
together into rows, must still emit a row for a fleet-wide (no-images)
entry even when no manual_findings cover that cluster, and must skip an
unreachable cluster instead of crashing. capture_gtd must only write GTD
items for actionable rows and must count them without writing anything on
--dry-run."""
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

import check_fleet_vulns  # noqa: E402
import vuln_register  # noqa: E402
from exposure import ExposureResult  # noqa: E402


class FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


FLOORS = {
    "php": {"images": ["nextcloud"], "probe": ["php", "-r", "echo PHP_VERSION;"],
            "parse": r"(\d+\.\d+\.\d+)", "min_versions": {"8.5": 12}},
}

VULNS = [
    {"id": "php2shell", "severity": "critical", "images": ["nextcloud"],
     "check": {"type": "runtime_floor", "floors_ref": "php"}},
    {"id": "emudhra", "severity": "high", "images": [],
     "check": {"type": "manual"}},
]


def _config(manager_hosts=("mgr1",)):
    return {"clusters": {"nor": {"manager_hosts": list(manager_hosts)}}}


def test_gather_matches_produces_a_row_for_the_matched_service_and_the_fleet_wide_entry(monkeypatch):
    def fake_runner(argv, capture_output, text, timeout):
        cmd = argv[-1]
        if cmd == "true":
            return FakeCompleted(0)
        if "service ls" in cmd:
            return FakeCompleted(0, stdout="skhub-prod_nextcloud\n")
        if "{{json .Spec.Labels}}" in cmd:
            return FakeCompleted(0, stdout='{"traefik.http.routers.nc.rule": "Host(`nc.example.org`)"}')
        if "inspect skhub-prod_nextcloud" in cmd:
            return FakeCompleted(0, stdout="nextcloud:34.0.5@sha256:" + "a" * 64 + "\n")
        if "service ps" in cmd:
            return FakeCompleted(0, stdout="node1\n")
        if "docker ps" in cmd:
            return FakeCompleted(0, stdout="c1\n")
        if "docker exec c1" in cmd:
            return FakeCompleted(0, stdout="8.5.10")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_runner)

    matches = check_fleet_vulns.gather_matches(_config(), VULNS, FLOORS)

    nc_rows = [m for m in matches if m.vuln_id == "php2shell"]
    assert len(nc_rows) == 1
    assert nc_rows[0].affected == "yes" and nc_rows[0].exposure == "internet-unauthenticated"

    fleet_rows = [m for m in matches if m.vuln_id == "emudhra"]
    assert len(fleet_rows) == 1
    assert fleet_rows[0].service == "(fleet-wide)"
    assert fleet_rows[0].status == "needs-manual-verification"


def test_gather_matches_flags_an_unmatched_image_as_unreviewed_not_silently_dropped(monkeypatch):
    def fake_runner(argv, capture_output, text, timeout):
        cmd = argv[-1]
        if cmd == "true":
            return FakeCompleted(0)
        if "service ls" in cmd:
            return FakeCompleted(0, stdout="skstream-prod_plex\n")
        if "{{json .Spec.Labels}}" in cmd:
            return FakeCompleted(0, stdout='{"traefik.http.routers.plex.rule": "Host(`plex.example.org`)"}')
        if "inspect skstream-prod_plex" in cmd:
            return FakeCompleted(0, stdout="plexinc/pms-docker@sha256:" + "a" * 64 + "\n")
        raise AssertionError(cmd)

    monkeypatch.setattr(subprocess, "run", fake_runner)

    # VULNS has no entry for "pms-docker" at all.
    matches = check_fleet_vulns.gather_matches(_config(), VULNS, FLOORS)
    plex_rows = [m for m in matches if m.service == "skstream-prod_plex"]
    assert len(plex_rows) == 1
    assert plex_rows[0].status == "unreviewed"
    assert plex_rows[0].severity == "unknown"


def test_capture_gtd_never_captures_unreviewed_rows_to_avoid_flooding_gtd(monkeypatch):
    calls = []

    def fake_upsert_finding(**kwargs):
        calls.append(kwargs)
        return ("item-1", "created")

    import notify
    monkeypatch.setattr(notify, "gtd_upsert_finding", fake_upsert_finding)

    unreviewed = vuln_register.unreviewed_match(cluster="nor", service="skstream-prod_plex", image="img",
                                                exposure_result=ExposureResult(exposure="internet-unauthenticated"))
    count = check_fleet_vulns.capture_gtd([unreviewed], dry_run=False)
    assert count == 0
    assert calls == []


def test_gather_matches_skips_an_unreachable_cluster_without_crashing(monkeypatch):
    def fake_runner(argv, capture_output, text, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    monkeypatch.setattr(subprocess, "run", fake_runner)

    matches = check_fleet_vulns.gather_matches(_config(), VULNS, FLOORS)
    assert matches == []


def test_capture_gtd_only_writes_actionable_rows_and_counts_without_writing_on_dry_run(monkeypatch):
    calls = []

    def fake_upsert_finding(**kwargs):
        calls.append(kwargs)
        return ("item-1", "created")

    import notify
    monkeypatch.setattr(notify, "gtd_upsert_finding", fake_upsert_finding)

    patched = vuln_register.build_match(
        {"id": "x", "severity": "low", "check": {"type": "manual"},
         "manual_findings": [{"cluster": "nor", "service": "s", "affected": "no"}]},
        cluster="nor", service="s", image="img", ref=None, exposure_result=ExposureResult(exposure="n/a"),
        probed_version=None, probe_error=None, floors={},
    )
    needs_check = vuln_register.build_match(
        {"id": "y", "severity": "high", "check": {"type": "manual"}},
        cluster="nor", service="s2", image="img2", ref=None, exposure_result=ExposureResult(exposure="n/a"),
        probed_version=None, probe_error=None, floors={},
    )
    assert patched.status == "not-affected"
    assert needs_check.status == "needs-manual-verification"

    count = check_fleet_vulns.capture_gtd([patched, needs_check], dry_run=False)
    assert count == 1
    assert len(calls) == 1
    assert calls[0]["source"] == "vuln-register"
    assert calls[0]["source_ref"] == "y/nor/s2"

    calls.clear()
    dry_count = check_fleet_vulns.capture_gtd([patched, needs_check], dry_run=True)
    assert dry_count == 1
    assert calls == []


def test_capture_gtd_keys_on_vuln_id_not_cve_id_so_shared_cves_dont_collide(monkeypatch):
    """Regression: php2shell-2026-10 and windmill-php-floor-2026-10 both cite
    CVE-2026-109618 for the same (cluster, service). Keying source_ref on
    the first CVE id collided them into one GTD item and silently dropped
    the other - caught on a live run against nam/skflow-prod_server."""
    calls = []

    def fake_upsert_finding(**kwargs):
        calls.append(kwargs)
        return ("item-x", "created")

    import notify
    monkeypatch.setattr(notify, "gtd_upsert_finding", fake_upsert_finding)

    shared_cve_kwargs = dict(cluster="nam", service="skflow-prod_server", image="img", ref=None,
                             exposure_result=ExposureResult(exposure="internet-unauthenticated"),
                             probed_version=None, probe_error=None, floors={})
    m1 = vuln_register.build_match(
        {"id": "php2shell-2026-10", "cve_ids": ["CVE-2026-109618"], "severity": "critical",
         "check": {"type": "manual", "fixed_version": "8.5.12"}}, **shared_cve_kwargs)
    m2 = vuln_register.build_match(
        {"id": "windmill-php-floor-2026-10", "cve_ids": ["CVE-2026-109618"], "severity": "critical",
         "check": {"type": "manual", "fixed_version": "1.844.0"}}, **shared_cve_kwargs)
    # force both into an actionable status for this test
    m1.status, m2.status = "patch-available", "patch-available"

    check_fleet_vulns.capture_gtd([m1, m2], dry_run=False)

    refs = {c["source_ref"] for c in calls}
    assert len(calls) == 2
    assert refs == {"php2shell-2026-10/nam/skflow-prod_server",
                    "windmill-php-floor-2026-10/nam/skflow-prod_server"}
