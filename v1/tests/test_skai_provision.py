"""skai's preset provisioner (optional/skai/app/provision_presets.py) against
a fake Open WebUI API on loopback (stdlib http.server, no external network):
sign-in as the env admin, create a missing preset, update a changed one,
leave an unchanged one alone, never delete, and fail on a non-admin or an
API error."""
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
    out = capsys.readouterr().out.split()
    assert out == ["PRESET_CREATED", "corpus-example-legal", "PRESET_CREATED", "corpus-example-notes"]
    assert PASSWORD not in capsys.readouterr().out


def test_a_preset_id_owned_by_someone_else_is_an_error_not_a_create(webui):
    webui.models["corpus-example-legal"] = {"id": "corpus-example-legal", "hidden": True}
    tok = pp.signin()
    with pytest.raises(pp.ApiError, match="reading preset"):
        pp.provision([preset()], tok)
    assert ("POST", "/api/v1/models/create") not in webui.calls
