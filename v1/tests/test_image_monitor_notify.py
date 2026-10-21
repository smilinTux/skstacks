"""notify.py: the Talk path must fail closed (skip, log, never crash the run)
when credentials are not provisioned yet, must build the right
run_wait_result request when they are, and GTD capture must key on
(source, source_ref) so a rerun's upsert is the idempotent call skos.gtd_ingest
promises, not a duplicate."""
import json
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

import notify  # noqa: E402


def test_missing_credentials_file_raises_not_configured(tmp_path):
    with pytest.raises(notify.NotConfigured):
        notify.load_credentials(tmp_path / "does-not-exist.env")


def test_incomplete_credentials_file_raises_not_configured(tmp_path):
    path = tmp_path / "creds.env"
    path.write_text("WM_NOTIFY_BASE_URL=https://skflow.example.org\n")  # missing workspace/token
    with pytest.raises(notify.NotConfigured):
        notify.load_credentials(path)


def test_well_formed_credentials_file_loads(tmp_path):
    path = tmp_path / "creds.env"
    path.write_text(
        "WM_NOTIFY_BASE_URL=https://skflow.example.org/\n"
        "WM_NOTIFY_WORKSPACE=fwe\n"
        "WM_NOTIFY_TOKEN=abc123\n"
    )
    creds = notify.load_credentials(path)
    assert creds.base_url == "https://skflow.example.org"  # trailing slash stripped
    assert creds.workspace == "fwe"
    assert creds.token == "abc123"


class FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_post_talk_calls_run_wait_result_with_the_right_room_and_token():
    creds = notify.NotifyCredentials(base_url="https://skflow.example.org", workspace="fwe", token="secret-tok")
    seen = {}

    def fake_urlopen(req, timeout):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data)
        return FakeResponse({"posted": True})

    ok = notify.post_talk(creds, title="t", summary="s", severity="error", room="NAM Urgent",
                          ref_id="image-monitor-urgent", urlopen=fake_urlopen)
    assert ok is True
    assert seen["url"] == "https://skflow.example.org/api/w/fwe/jobs/run_wait_result/p/f/ops/notify"
    assert seen["auth"] == "Bearer secret-tok"
    assert seen["body"]["room"] == "NAM Urgent"
    assert seen["body"]["severity"] == "error"


def test_gtd_upsert_uses_image_monitor_source_and_cluster_service_image_ref():
    calls = []

    def fake_upsert(**kwargs):
        calls.append(kwargs)
        return ("item-123", "created")

    item_id, action = notify.gtd_upsert_finding(
        cluster="nam", service="skflow-prod_worker", image="windmill-labs/windmill:1.837.0@sha256:" + "a" * 64,
        text="PHP 8.3.33 is below the floor", upsert_fn=fake_upsert,
    )
    assert item_id == "item-123" and action == "created"
    assert calls[0]["source"] == "image-monitor"
    assert calls[0]["source_ref"] == "nam/skflow-prod_worker/windmill-labs/windmill:1.837.0@sha256:" + "a" * 64


def test_gtd_upsert_is_idempotent_on_rerun_with_the_same_source_ref():
    """A rerun with the same (source, source_ref) must hit the same key, which
    is what makes skos.gtd_ingest.upsert's own "unchanged -> no write"
    behaviour apply instead of creating a duplicate item."""
    seen_refs = []

    def fake_upsert(**kwargs):
        seen_refs.append(kwargs["source_ref"])
        return ("item-1", "unchanged" if len(seen_refs) > 1 else "created")

    for _ in range(2):
        notify.gtd_upsert_finding(cluster="nor", service="skbook-prod_bookstack",
                                  image="lscr.io/linuxserver/bookstack:v26.09-ls285@sha256:" + "b" * 64,
                                  text="PHP 8.5.10 is below the floor", upsert_fn=fake_upsert)

    assert len(set(seen_refs)) == 1
