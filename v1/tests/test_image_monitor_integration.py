"""End-to-end wiring test for check_fleet_images.gather_findings, with
inventory and the runtime probe stubbed out (no ssh, no network).

Regression coverage for a real bug caught during manual verification: the
floor table's "images" patterns are namespace-qualified
("linuxserver/bookstack", "windmill-labs/windmill"), so matching them
against the bare repo name instead of the full namespace/repo path silently
never matched bookstack or windmill at all - every other image happened to
be either unqualified Docker Hub (no namespace needed) or simply had no
runtime to probe, so the bug produced no visible failure anywhere except a
missing finding."""
import pathlib
import sys
from types import SimpleNamespace

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

import check_fleet_images as cfi  # noqa: E402
from inventory import InventoryResult, ServiceImage  # noqa: E402

FLOORS = {
    "php": {
        "images": ["nextcloud", "linuxserver/bookstack", "windmill-labs/windmill"],
        "probe": ["php", "-r", "echo PHP_VERSION;"],
        "parse": r"(\d+\.\d+\.\d+)",
        "min_versions": {"8.5": 12, "8.4": 27, "8.3": 36, "8.2": 35},
    }
}


def _config(tmp_path):
    return {
        "clusters": {"nor": {"manager_hosts": ["mgr1"]}},
        "cache_file": str(tmp_path / "cache.json"),
        "state_file": str(tmp_path / "state.json"),
        "cache_ttl_hours": 20,
        "registry": {"max_retries": 1, "backoff_base_seconds": 0.01, "request_timeout_seconds": 5},
        "notify": {},
        "gtd": {"context": "@fleet"},
    }


def test_namespace_qualified_runtime_pattern_matches_bookstack(tmp_path, monkeypatch):
    image = "lscr.io/linuxserver/bookstack:v26.09-ls285@sha256:" + "a" * 64
    inv = InventoryResult(cluster="nor", manager_used="mgr1", unreachable=False,
                          services=[ServiceImage(cluster="nor", service="skbook-prod_bookstack", image=image)])
    monkeypatch.setattr(cfi, "collect_cluster_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(cfi.registry, "check_upstream", lambda *a, **k: SimpleNamespace(digest_changed=False))
    monkeypatch.setattr(cfi.php_probe, "probe_runtime",
                       lambda *a, **k: SimpleNamespace(output="8.5.10", error=None))

    digest_findings, urgent_findings = cfi.gather_findings(_config(tmp_path), FLOORS, dry_run=True)
    assert not digest_findings
    assert len(urgent_findings) == 1
    assert "8.5.10" in urgent_findings[0]["text"]
    assert "[nor]" in urgent_findings[0]["text"]


def test_image_with_no_matching_runtime_is_not_probed(tmp_path, monkeypatch):
    image = "redis:7-alpine@sha256:" + "b" * 64
    inv = InventoryResult(cluster="nor", manager_used="mgr1", unreachable=False,
                          services=[ServiceImage(cluster="nor", service="skgallery-prod_redis", image=image)])
    monkeypatch.setattr(cfi, "collect_cluster_inventory", lambda *a, **k: inv)
    monkeypatch.setattr(cfi.registry, "check_upstream", lambda *a, **k: SimpleNamespace(digest_changed=False))

    def _unexpected_probe(*a, **k):
        raise AssertionError("redis has no runtime floor configured; probe_runtime must not be called")

    monkeypatch.setattr(cfi.php_probe, "probe_runtime", _unexpected_probe)

    digest_findings, urgent_findings = cfi.gather_findings(_config(tmp_path), FLOORS, dry_run=True)
    assert not digest_findings and not urgent_findings
