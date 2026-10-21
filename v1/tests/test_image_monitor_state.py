"""state.StateStore must treat a service's first sighting as a baseline (not
a reported change), detect a real digest/tag change on a later run, and
stay quiet when nothing moved - the property that keeps the daily timer
from re-alerting on the same thing forever."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

from state import StateStore, image_changed  # noqa: E402


def test_first_sighting_is_a_baseline_not_a_change(tmp_path):
    store = StateStore.load(tmp_path / "state.json")
    assert image_changed(store, "nor", "svc-a", "redis:7-alpine@sha256:" + "a" * 64) is False


def test_real_change_detected_on_a_later_run(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore.load(path)
    store.record("nor", "svc-a", "redis:7-alpine@sha256:" + "a" * 64)
    store.save()

    store2 = StateStore.load(path)
    assert image_changed(store2, "nor", "svc-a", "redis:7-alpine@sha256:" + "b" * 64) is True


def test_unchanged_image_reports_no_change(tmp_path):
    path = tmp_path / "state.json"
    store = StateStore.load(path)
    store.record("nor", "svc-a", "redis:7-alpine@sha256:" + "a" * 64)
    store.save()

    store2 = StateStore.load(path)
    assert image_changed(store2, "nor", "svc-a", "redis:7-alpine@sha256:" + "a" * 64) is False


def test_missing_state_file_loads_empty_without_raising(tmp_path):
    store = StateStore.load(tmp_path / "does-not-exist.json")
    assert store.is_first_seen("nor", "svc-a")


def test_corrupt_state_file_loads_empty_without_raising(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{not valid json")
    store = StateStore.load(path)
    assert store.is_first_seen("nor", "svc-a")
