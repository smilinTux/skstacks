"""skai's preset provisioner (optional/skai/app/provision_presets.py) against
a fake Open WebUI API on loopback (stdlib http.server, no external network):
sign-in as the env admin, create a missing preset, update a changed one,
leave an unchanged one alone, never delete, and fail on a non-admin or an
API error; create the corpus groups, give each corpus's tool server and
preset read grants for its groups only (and leave other connections alone),
and the loop entry point."""
import importlib.util
import json
import pathlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

APP = pathlib.Path(__file__).resolve().parents[1] / "ansible/optional/skai/app/provision_presets.py"
spec = importlib.util.spec_from_file_location("skai_provision", APP)
pp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pp)

EMAIL, PASSWORD, TOKEN = "provisioner@example.test", "example-provisioner-password", "fake-jwt"


def preset(pid="corpus-example-legal", system="Answer from the corpus."):
    return {"id": pid, "base_model_id": "example-chat", "name": "Example Legal",
            "meta": {"description": "d", "toolIds": ["server:skcorpus"], "capabilities": {"citations": True}},
            "params": {"system": system, "function_calling": "native"},
            "access_grants": [{"principal_type": "user", "principal_id": "*", "permission": "read"}]}


class FakeWebUI:
    def __init__(self, role="admin"):
        self.models, self.calls, self.role = {}, [], role
        self.groups = [{"id": "g-admins", "name": "example-admins"}]
        self.tools = []
        self.fail_writes = False
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _auth(self):
                return self.headers.get("Authorization") == f"Bearer {TOKEN}"

            def do_GET(self):
                u = urlparse(self.path)
                fake.calls.append(("GET", u.path))
                if u.path == "/health":
                    return self._send(200, {"status": True})
                if u.path == "/api/v1/groups/" and self._auth():
                    return self._send(200, fake.groups)
                if u.path == "/api/v1/configs/tool_servers" and self._auth():
                    return self._send(200, {"TOOL_SERVER_CONNECTIONS": json.loads(json.dumps(fake.tools))})
                if u.path == "/api/v1/models/model" and self._auth():
                    m = fake.models.get(parse_qs(u.query)["id"][0])
                    if m and m.get("hidden"):
                        return self._send(401, {"detail": "access prohibited"})
                    return self._send(200, m) if m else self._send(404, {"detail": "We could not find what you're looking for :/"})
                return self._send(401, {"detail": "unauthorized"})

            def do_POST(self):
                u = urlparse(self.path)
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                fake.calls.append(("POST", u.path))
                if u.path == "/api/v1/auths/signin":
                    if body == {"email": EMAIL, "password": PASSWORD}:
                        return self._send(200, {"token": TOKEN, "role": fake.role})
                    return self._send(400, {"detail": "bad credentials"})
                if not self._auth():
                    return self._send(401, {"detail": "unauthorized"})
                if fake.fail_writes:
                    return self._send(500, {"detail": "boom"})
                if u.path == "/api/v1/groups/create":
                    g = {"id": f"g-{len(fake.groups)}", "name": body["name"]}
                    fake.groups.append(g)
                    return self._send(200, g)
                if u.path == "/api/v1/configs/tool_servers":
                    fake.tools = body["TOOL_SERVER_CONNECTIONS"]
                    return self._send(200, body)
                if u.path == "/api/v1/models/create":
                    if body["id"] in fake.models:
                        return self._send(400, {"detail": "exists"})
                    fake.models[body["id"]] = dict(body)
                    return self._send(200, body)
                if u.path == "/api/v1/models/model/update":
                    fake.models[body["id"]] = dict(body)
                    return self._send(200, body)
                return self._send(404, {"detail": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def webui(monkeypatch):
    fake = FakeWebUI()
    monkeypatch.setattr(pp, "BASE", fake.url)
    monkeypatch.setenv("WEBUI_ADMIN_EMAIL", EMAIL)
    monkeypatch.setenv("WEBUI_ADMIN_PASSWORD", PASSWORD)
    yield fake
    fake.close()


def test_create_then_unchanged_then_update(webui):
    tok = pp.signin()
    assert pp.provision([preset()], tok) == ["PRESET_CREATED corpus-example-legal"]
    assert webui.models["corpus-example-legal"]["meta"]["toolIds"] == ["server:skcorpus"]
    assert pp.provision([preset()], tok) == ["PRESET_UNCHANGED corpus-example-legal"]
    assert pp.provision([preset(system="New prompt.")], tok) == ["PRESET_UPDATED corpus-example-legal"]
    assert webui.models["corpus-example-legal"]["params"]["system"] == "New prompt."


def test_other_models_are_never_touched_or_deleted(webui):
    webui.models["someone-else"] = {"id": "someone-else", "name": "x"}
    tok = pp.signin()
    pp.provision([preset()], tok)
    assert "someone-else" in webui.models
    assert not any("delete" in p for _, p in webui.calls)


def test_signin_requires_env_credentials(webui, monkeypatch):
    monkeypatch.delenv("WEBUI_ADMIN_PASSWORD")
    with pytest.raises(pp.ApiError, match="must be set"):
        pp.signin()


def test_wrong_password_fails(webui, monkeypatch):
    monkeypatch.setenv("WEBUI_ADMIN_PASSWORD", "wrong")
    with pytest.raises(pp.ApiError, match="sign-in failed"):
        pp.signin()


def test_non_admin_provisioner_is_refused(webui):
    webui.role = "user"
    with pytest.raises(pp.ApiError, match="not an admin"):
        pp.signin()


def test_write_error_fails_loudly(webui):
    tok = pp.signin()
    webui.fail_writes = True
    with pytest.raises(pp.ApiError, match="writing preset"):
        pp.provision([preset()], tok)


def test_main_with_empty_presets_does_nothing(tmp_path, webui):
    f = tmp_path / "presets.json"
    f.write_text("[]")
    assert pp.main(["x", str(f)]) == 0
    assert webui.calls == []


def test_main_end_to_end(tmp_path, webui, capsys):
    f = tmp_path / "presets.json"
    f.write_text(json.dumps([preset(), preset("corpus-example-notes")]))
    assert pp.main(["x", str(f)]) == 0
    out = capsys.readouterr().out
    assert out.split() == ["PRESET_CREATED", "corpus-example-legal", "PRESET_CREATED", "corpus-example-notes",
                           "RECONCILED"]
    assert PASSWORD not in out


def test_a_preset_id_owned_by_someone_else_is_an_error_not_a_create(webui):
    webui.models["corpus-example-legal"] = {"id": "corpus-example-legal", "hidden": True}
    tok = pp.signin()
    with pytest.raises(pp.ApiError, match="reading preset"):
        pp.provision([preset()], tok)
    assert ("POST", "/api/v1/models/create") not in webui.calls


# --- groups and per-corpus tool grants ---------------------------------------

def conn(sid, grants=None):
    return {"url": f"http://skcorpus:8000/c/{sid}", "path": "openapi.json", "type": "openapi",
            "auth_type": "bearer", "key": "example-key", "config": {"enable": True, "access_grants": grants or []},
            "info": {"id": sid, "name": sid}}


def scoped(pid, groups):
    p = preset(pid)
    p.pop("access_grants")
    p["allowed_groups"] = groups
    return p


def test_groups_tools_and_presets_are_scoped_per_corpus(webui):
    webui.tools = [conn("skcorpus-example-legal"), conn("skcorpus-example-notes"), conn("someone-else", [
        {"principal_type": "user", "principal_id": "*", "permission": "read"}])]
    out = pp.reconcile([scoped("corpus-example-legal", ["example-users"]),
                        scoped("corpus-example-notes", ["example-admins", "*"])],
                       {"skcorpus-example-legal": ["example-users"], "skcorpus-example-notes": ["example-admins", "*"]})
    assert out[0] == "GROUP_CREATED example-users"
    assert out[1] == "TOOLS_UPDATED skcorpus-example-legal,skcorpus-example-notes"
    gid = next(g["id"] for g in webui.groups if g["name"] == "example-users")
    by_id = {c["info"]["id"]: c for c in webui.tools}
    assert by_id["skcorpus-example-legal"]["config"]["access_grants"] == [
        {"principal_type": "group", "principal_id": gid, "permission": "read"}]
    assert by_id["skcorpus-example-notes"]["config"]["access_grants"] == [
        {"principal_type": "group", "principal_id": "g-admins", "permission": "read"},
        {"principal_type": "user", "principal_id": "*", "permission": "read"}]
    assert by_id["someone-else"]["config"]["access_grants"][0]["principal_id"] == "*"  # untouched
    assert by_id["skcorpus-example-legal"]["key"] == "example-key"  # the key survives the round trip
    assert webui.models["corpus-example-legal"]["access_grants"] == [
        {"principal_type": "group", "principal_id": gid, "permission": "read"}]
    # second pass: nothing to do, no duplicate group
    again = pp.reconcile([scoped("corpus-example-legal", ["example-users"])], {"skcorpus-example-legal": ["example-users"]})
    assert again == ["TOOLS_UNCHANGED", "PRESET_UNCHANGED corpus-example-legal"]
    assert [g["name"] for g in webui.groups].count("example-users") == 1


def test_a_restart_that_dropped_the_grants_is_repaired(webui):
    webui.tools = [conn("skcorpus-example-legal")]
    tools = {"skcorpus-example-legal": ["example-admins"]}
    pp.reconcile([], tools)
    webui.tools = [conn("skcorpus-example-legal")]  # env value back after a restart: no grants
    assert pp.reconcile([], tools) == ["TOOLS_UPDATED skcorpus-example-legal"]


def test_a_tool_server_missing_from_the_env_is_an_error(webui):
    webui.tools = []
    with pytest.raises(pp.ApiError, match="not registered"):
        pp.reconcile([], {"skcorpus-example-legal": ["example-users"]})


def test_unknown_group_grants_nothing():
    assert pp.grants(["nope"], {}) == []
    assert pp.form(scoped("corpus-x", ["nope"]), {})["access_grants"] == []


def test_loop_mode_retries_and_reports(tmp_path, webui, monkeypatch, capsys):
    f = tmp_path / "presets.json"
    f.write_text(json.dumps({"presets": [scoped("corpus-example-legal", ["example-users"])],
                             "tool_servers": {"skcorpus-example-legal": ["example-users"]}}))
    webui.tools = [conn("skcorpus-example-legal")]
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) == 2:
            raise KeyboardInterrupt
    monkeypatch.setattr(pp.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        pp.main(["x", "--loop", "60", str(f)])
    out = capsys.readouterr().out
    assert out.count("RECONCILED") == 2 and sleeps == [60, 60]
    assert pp.main(["x", "--loop", "abc", str(f)]) == 2
