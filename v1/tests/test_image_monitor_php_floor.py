"""php_floor must flag a version below its configured patch floor, pass one
at or above it, and never crash on a probe that didn't yield a version
(wrong runtime in the container, or the exec itself failing)."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

from php_floor import evaluate, runtimes_for_image  # noqa: E402

FLOORS = {
    "php": {
        "images": ["nextcloud", "linuxserver/bookstack", "windmill-labs/windmill"],
        "probe": ["php", "-r", "echo PHP_VERSION;"],
        "parse": r"(\d+\.\d+\.\d+)",
        "min_versions": {"8.5": 12, "8.4": 27, "8.3": 36, "8.2": 35},
    }
}


def test_below_floor_is_flagged():
    check = evaluate("php", FLOORS, "8.5.10")
    assert check.below_floor
    assert check.floor == 12
    assert check.version == "8.5.10"


def test_at_floor_is_not_flagged():
    check = evaluate("php", FLOORS, "8.5.12")
    assert not check.below_floor


def test_above_floor_is_not_flagged():
    check = evaluate("php", FLOORS, "8.5.20")
    assert not check.below_floor


def test_windmill_php_below_its_own_line_floor():
    check = evaluate("php", FLOORS, "8.3.33")
    assert check.below_floor
    assert check.floor == 36


def test_probe_failure_does_not_crash_and_is_not_flagged():
    check = evaluate("php", FLOORS, None, probe_error="OCI runtime exec failed: exec: \"php\": not found")
    assert not check.below_floor
    assert "probe failed" in check.note


def test_unconfigured_minor_line_is_informational_not_a_failure():
    check = evaluate("php", FLOORS, "8.6.1")
    assert not check.below_floor
    assert "no floor configured" in check.note


def test_runtimes_for_image_matches_by_substring():
    assert runtimes_for_image(FLOORS, "linuxserver/bookstack") == ["php"]
    assert runtimes_for_image(FLOORS, "redis") == []
