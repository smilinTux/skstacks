"""optional/skpatch-hosts/src/skpatch-hosts/systems_sync.py: the post-provision
host-registration provisioner that upserts one dashboard system per node
provision_skpatch_hosts_nodes.yml just provisioned, so the dashboard is
never empty after a deploy.

Exercises `ensure_system`/`list_systems`/`main` against a mocked dashboard
API (urllib is monkeypatched, nothing touches the network): create on
first run, a no-op "unchanged" on a second run with identical desired
state, a name-only change updating in place (PUT) without touching
hostname/port/credentialId identity, and the "orphan" report for a system
under this estate's credential that the current run's target list no
longer names -- confirmed NEVER deleted, only reported."""
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skpatch-hosts" / "src" / "skpatch-hosts"))
import systems_sync as sync  # noqa: E402

CREDENTIAL_ID = 7


class FakeApi:
    """Records calls against an in-memory systems table, matched the same
    way the upstream app matches (hostname, port), with every system
    carrying the SAME credentialId (this script only ever registers
    systems against the one estate credential it was given)."""

    def __init__(self, systems=None):
        self.systems = systems or []
        self._next_id = max([s["id"] for s in self.systems], default=0) + 1
        self.calls = []

    def call(self, method, path, body=None, ok=(200, 201, 204)):
        self.calls.append((method, path, body))
        if method == "GET" and path == "/systems":
            return {"systems": self.systems}
        if method == "POST" and path == "/systems":
            obj = {"id": self._next_id, **body}
            self._next_id += 1
            self.systems.append(obj)
            return {"id": obj["id"]}
        if method == "PUT" and path.startswith("/systems/"):
            sid = int(path.split("/")[-1])
            for s in self.systems:
                if s["id"] == sid:
                    s.update(body)
                    return None
            raise AssertionError(f"PUT to unknown system id {sid}")
        raise AssertionError(f"unexpected call: {method} {path} {body}")


def test_ensure_system_creates_when_missing():
    api = FakeApi(systems=[])
    wanted = {"name": "node1", "hostname": "node1.example.test", "port": 22}
    sys_obj = sync.ensure_system(api, {}, CREDENTIAL_ID, wanted)
    assert sys_obj["id"] == 1
    methods = [c[0] for c in api.calls]
    assert methods == ["POST"]
    created = api.calls[0][2]
    assert created["hostKeyVerificationEnabled"] is False
    assert created["credentialId"] == CREDENTIAL_ID


def test_ensure_system_unchanged_is_a_noop():
    existing = {"id": 1, "name": "node1", "hostname": "node1.example.test", "port": 22, "credentialId": CREDENTIAL_ID}
    api = FakeApi(systems=[existing])
    existing_by_key = {("node1.example.test", 22): existing}
    wanted = {"name": "node1", "hostname": "node1.example.test", "port": 22}
    sync.ensure_system(api, existing_by_key, CREDENTIAL_ID, wanted)
    assert api.calls == []  # no POST, no PUT: nothing differs


def test_ensure_system_name_change_updates_in_place():
    existing = {"id": 1, "name": "old-name", "hostname": "node1.example.test", "port": 22, "credentialId": CREDENTIAL_ID}
    api = FakeApi(systems=[existing])
    existing_by_key = {("node1.example.test", 22): existing}
    wanted = {"name": "node1", "hostname": "node1.example.test", "port": 22}
    sync.ensure_system(api, existing_by_key, CREDENTIAL_ID, wanted)
    put_calls = [c for c in api.calls if c[0] == "PUT"]
    assert len(put_calls) == 1
    assert put_calls[0][2]["name"] == "node1"
    assert put_calls[0][2]["hostname"] == "node1.example.test"  # identity unchanged


def test_main_reports_orphans_without_deleting_them(capsys, monkeypatch):
    existing = [
        {"id": 1, "name": "node1", "hostname": "node1.example.test", "port": 22, "credentialId": CREDENTIAL_ID},
        {"id": 2, "name": "retired-node", "hostname": "gone.internal", "port": 22, "credentialId": CREDENTIAL_ID},
        {"id": 3, "name": "someone-elses-system", "hostname": "other.internal", "port": 22, "credentialId": 99},
    ]
    api = FakeApi(systems=existing)
    monkeypatch.setattr(sync, "Api", lambda url, token: api)
    import io

    cfg = {
        "url": "http://skpatch-hosts-prod_dashboard:3001",
        "token": "example-token",
        "credential_id": CREDENTIAL_ID,
        "systems": [{"name": "node1", "hostname": "node1.example.test", "port": 22}],
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(cfg)))
    rc = sync.main()
    assert rc == 0
    out = capsys.readouterr().out
    assert "node1: unchanged" in out
    assert "retired-node (gone.internal:22)" in out
    assert "someone-elses-system" not in out  # a different credential's system: not this script's concern
    assert len(api.systems) == 3  # nothing deleted


def test_main_rejects_missing_required_fields(capsys, monkeypatch):
    import io

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"url": "http://x"})))
    rc = sync.main()
    assert rc == 2
    captured = capsys.readouterr()
    assert "missing config" in captured.err


def test_api_call_raises_on_http_error(monkeypatch):
    class FakeHTTPError(urllib.error.HTTPError):
        def __init__(self):
            super().__init__("http://x", 404, "not found", {}, None)

        def read(self):
            return b'{"error": "not found"}'

    def fake_urlopen(req, timeout=30):
        raise FakeHTTPError()

    monkeypatch.setattr(sync.urllib.request, "urlopen", fake_urlopen)
    api = sync.Api("http://skpatch-hosts-prod_dashboard:3001", "token")
    with pytest.raises(sync.ApiError, match="HTTP 404"):
        api.call("GET", "/systems/999")
