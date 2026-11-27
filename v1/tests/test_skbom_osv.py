"""skbom enables Dependency-Track's OSV vulnerability source.

On the nor estate, Dependency-Track 4.14.7 had only NVD enabled. NVD matches
CPEs and a Trivy CycloneDX SBOM carries purls, so 100 projects with hundreds of
components showed zero vulnerabilities. The fix was applied by hand through the
admin API; Dependency-Track keeps it in its database, so a fresh skbom lost it.

Two layers:
  1. src/skbom/osv_enable.py against a fake Dependency-Track (a real HTTP
     server on localhost): applies the setting, is idempotent, refuses a bad
     login, never prints the password;
  2. the playbook through real Ansible (render gate): on by default, the
     ecosystem list is a role default the vault can override as a string or a
     list, a malformed list is refused, and OSV_ENABLED: false switches it off.
"""
import io
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "ansible/optional/skbom/src/skbom"))
import osv_enable as osv  # noqa: E402
import test_skbom_render as rend  # noqa: E402

ROLE = HERE.parent / "ansible/optional/skbom"
DEFAULT = "Go;npm;PyPI;Maven;crates.io;Alpine;Debian;Ubuntu;NuGet;Packagist;RubyGems;Wolfi"
PASSWORD = "s3cret-admin-password-xyz"


class FakeDT(BaseHTTPRequestHandler):
    props = None
    healthy = True
    posts = None
    reject_aggregate = False

    def log_message(self, *a):
        pass

    def _send(self, code, body=""):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._send(200 if self.healthy else 503, "{}")
        if self.path == "/api/v1/configProperty":
            if self.headers.get("Authorization") != "Bearer jwt-token":
                return self._send(401)
            return self._send(200, json.dumps(list(self.props.values())))
        self._send(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode()
        if self.path == "/api/v1/user/login":
            form = parse_qs(raw)
            if form.get("username") == ["admin"] and form.get("password") == [PASSWORD]:
                return self._send(200, "jwt-token")
            return self._send(401, "Unauthorized")
        if self.path == "/api/v1/configProperty/aggregate":
            if self.headers.get("Authorization") != "Bearer jwt-token":
                return self._send(401)
            if self.reject_aggregate:
                return self._send(500, "boom")
            items = json.loads(raw)
            type(self).posts.append(items)
            for it in items:
                self.props[(it["groupName"], it["propertyName"])]["propertyValue"] = it["propertyValue"]
            return self._send(200, json.dumps(items))
        self._send(404)


def initial_props():
    def p(name, value, group="vuln-source"):
        return {(group, name): {"groupName": group, "propertyName": name, "propertyValue": value}}
    out = {}
    out.update(p("nvd.enabled", "true"))
    out.update(p("google.osv.enabled", None))
    out.update(p("google.osv.alias.sync.enabled", "false"))
    return out


@pytest.fixture
def dt():
    handler = type("H", (FakeDT,), {"props": initial_props(), "posts": [], "healthy": True, "reject_aggregate": False})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    handler.url = f"http://127.0.0.1:{server.server_address[1]}"
    yield handler
    server.shutdown()


def cfg(dt, **kw):
    base = {"url": dt.url, "username": "admin", "password": PASSWORD, "ecosystems": DEFAULT, "alias_sync": True, "wait_seconds": 0}
    return base | kw


def run(config):
    lines = []
    rc = osv.run(config, sleep=lambda s: None, out=lines.append)
    return rc, lines


def test_enables_osv_and_alias_sync_on_a_fresh_dependency_track(dt):
    rc, lines = run(cfg(dt))
    assert rc == 0 and lines == ["osv: updated (google.osv.enabled, google.osv.alias.sync.enabled)"]
    assert dt.props[("vuln-source", "google.osv.enabled")]["propertyValue"] == DEFAULT
    assert dt.props[("vuln-source", "google.osv.alias.sync.enabled")]["propertyValue"] == "true"
    assert dt.props[("vuln-source", "nvd.enabled")]["propertyValue"] == "true"  # untouched
    assert len(dt.posts) == 1 and {i["propertyName"] for i in dt.posts[0]} == {"google.osv.enabled", "google.osv.alias.sync.enabled"}


def test_second_run_is_a_no_op(dt):
    run(cfg(dt))
    rc, lines = run(cfg(dt))
    assert rc == 0 and lines[0].startswith("osv: unchanged")
    assert len(dt.posts) == 1  # nothing was written again


def test_only_what_differs_is_written(dt):
    dt.props[("vuln-source", "google.osv.alias.sync.enabled")]["propertyValue"] = "true"
    rc, lines = run(cfg(dt))
    assert rc == 0 and lines == ["osv: updated (google.osv.enabled)"]
    assert [i["propertyName"] for i in dt.posts[0]] == ["google.osv.enabled"]


def test_ecosystem_order_and_spacing_do_not_cause_a_rewrite(dt):
    dt.props[("vuln-source", "google.osv.enabled")]["propertyValue"] = "npm; Go;PyPI"
    dt.props[("vuln-source", "google.osv.alias.sync.enabled")]["propertyValue"] = "true"
    rc, lines = run(cfg(dt, ecosystems="Go;npm;PyPI"))
    assert rc == 0 and lines[0].startswith("osv: unchanged")


def test_a_changed_ecosystem_list_is_applied(dt):
    run(cfg(dt))
    rc, lines = run(cfg(dt, ecosystems="Go;npm"))
    assert rc == 0 and "google.osv.enabled" in lines[0]
    assert dt.props[("vuln-source", "google.osv.enabled")]["propertyValue"] == "Go;npm"


def test_alias_sync_can_be_turned_off(dt):
    rc, _ = run(cfg(dt, alias_sync=False))
    assert rc == 0 and dt.props[("vuln-source", "google.osv.alias.sync.enabled")]["propertyValue"] == "false"


def test_refused_login_exits_2_and_never_prints_the_password(dt):
    rc, lines = run(cfg(dt, password="wrong-" + PASSWORD))
    assert rc == 2 and "refused" in lines[0]
    assert all(PASSWORD not in line for line in lines) and dt.posts == []


def test_apiserver_that_never_gets_healthy_is_an_error_and_nothing_is_sent(dt):
    dt.healthy = False
    rc, lines = run(cfg(dt))
    assert rc == 1 and "healthy" in lines[0] and dt.posts == []


def test_a_rejected_update_is_an_error(dt):
    dt.reject_aggregate = True
    rc, lines = run(cfg(dt))
    assert rc == 1 and "HTTP 500" in lines[0]


def test_empty_ecosystem_list_is_refused_before_any_call(dt):
    rc, lines = run(cfg(dt, ecosystems=" ; "))
    assert rc == 1 and "no ecosystems" in lines[0] and dt.posts == []


def test_normalize_accepts_lists_and_strings():
    assert osv.normalize_ecosystems(["Go", " npm ", "Go", ""]) == "Go;npm"
    assert osv.normalize_ecosystems("Go; npm ;;PyPI;") == "Go;npm;PyPI"


def test_script_reads_stdin_and_prints_status_only(dt, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(cfg(dt))))
    assert osv.main() == 0
    out = capsys.readouterr().out
    assert out.strip() == "osv: updated (google.osv.enabled, google.osv.alias.sync.enabled)"
    assert PASSWORD not in out


# playbook through real Ansible ------------------------------------------------

def test_default_ecosystems_match_the_script_default_documented_in_the_readme():
    for env in ("dev", "staging", "prod"):
        text = (ROLE / f"deploy_skbom-{env}.yml").read_text()
        assert f'skb_default_osv_ecosystems: "{DEFAULT}"' in text
    assert DEFAULT in (ROLE / "README.md").read_text()


@pytest.mark.parametrize("env", ["dev", "staging", "prod"])
def test_every_playbook_applies_osv_after_the_deploy_and_before_the_tile(env):
    plays = yaml.safe_load((ROLE / f"deploy_skbom-{env}.yml").read_text())
    names = [t.get("name", "") for p in plays for t in p.get("tasks", [])]
    i = next(k for k, n in enumerate(names) if n.startswith("Enable the OSV vulnerability source"))
    assert names[i - 1] == "Show deploy output" and names[i + 1].startswith("Create or update the skbom portal tile")


def test_every_task_that_touches_the_password_is_no_log():
    tasks = yaml.safe_load((ROLE / "tasks/osv_source.yml").read_text())
    flat = []
    for t in tasks:
        flat += t.get("block", [t])
    carriers = [t for t in flat if "ADMIN_PASSWORD" in json.dumps(t) and "when" not in t or "skb_osv_input" in json.dumps(t)]
    assert carriers, "no task carries the password?"
    for t in carriers:
        if "debug" in t or "fail" in t:
            continue
        assert t.get("no_log") is True, t["name"]
    # the script gets it on stdin, never in argv
    run_task = next(t for t in flat if "command" in t)
    assert "stdin" in run_task["command"] and "ADMIN_PASSWORD" not in json.dumps(run_task["command"]["argv"])


def test_default_vault_renders_and_the_osv_step_is_on(tmp_path):
    out = rend.ok(tmp_path, rend.BASE)
    assert out.exists()


def test_ecosystem_override_as_list_or_string_renders(tmp_path):
    rend.ok(tmp_path / "a", rend.BASE | {"OSV_ECOSYSTEMS": ["Go", "npm"], "ADMIN_PASSWORD": "x" * 20})
    rend.ok(tmp_path / "b", rend.BASE | {"OSV_ECOSYSTEMS": "Go; npm ;PyPI;", "ADMIN_PASSWORD": "x" * 20})


@pytest.mark.parametrize("bad", ["Go npm", "Go;$(id)", "Go|npm", "Go;npm;;PyPI x"])
def test_a_malformed_ecosystem_list_is_refused(tmp_path, bad):
    log = rend.fails(tmp_path, rend.BASE | {"OSV_ECOSYSTEMS": bad})
    assert "OSV_ECOSYSTEMS must be ecosystem names separated by" in log


def test_osv_off_skips_the_validation_and_the_step(tmp_path):
    rend.ok(tmp_path, rend.BASE | {"OSV_ENABLED": False, "OSV_ECOSYSTEMS": "not valid"})
