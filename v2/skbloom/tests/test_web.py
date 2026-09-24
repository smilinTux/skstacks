"""The skbloom web concierge API — pure functions (no socket). Talk to it → it proposes
a validated profile + the named-step plan; the app-store lists what it can install."""
from __future__ import annotations

import json
import pytest

from skbloom.web import server as web


@pytest.fixture(autouse=True)
def _v2(monkeypatch):
    # point the API at the real descriptor catalog
    from pathlib import Path
    monkeypatch.setenv("SKBLOOM_V2", str(Path(__file__).resolve().parents[2]))
    monkeypatch.setenv("SKWIRE_LLM_DISABLE", "1")     # deterministic replies in tests
    yield


def test_api_services_lists_the_catalog():
    out = web.api_services()
    names = [s["name"] for s in out["services"]]
    assert "sksso" in names and "skfence" in names
    assert all("capability" in s for s in out["services"])


def test_api_services_exposes_tunable_config_and_secret_keys():
    sk = next(s for s in web.api_services()["services"] if s["name"] == "skfence")
    assert "LOG_LEVEL" in sk["config"]                 # tunable knob (with its default)
    assert sk["config"]["LOG_LEVEL"] == "INFO"
    assert "cloudflare_dns_token" in sk["secrets"]      # key names only, never values
    assert "ha" in sk and "min_replicas" in sk


def test_api_propose_returns_validated_profile_and_plan():
    out = web.api_propose("i want single sign-on and an ingress")
    assert set(n.split("/")[-1] for n in out["profile"]["services"]) >= {"sksso", "skfence"}
    assert out["reply"]
    # the plan is the named-step spine ending in final-check
    assert out["plan"][0] == "preflight" and out["plan"][-1] == "final-check"
    assert any(s.startswith("deploy:") for s in out["plan"])


def test_api_propose_vanity_name_flows_through():
    out = web.api_propose("set up sso and call it login")
    assert out["profile"]["services"] == ["core/sksso=login"]
    assert "deploy:login" in out["plan"]


def test_api_propose_unmatched_is_graceful():
    out = web.api_propose("zzzz nothing here")
    assert out["profile"]["services"] == []
    assert out["reply"]                                # still says something helpful
    assert out["plan"] == [] or "preflight" in out["plan"]


def test_api_status_lists_installed_stacks_from_state(tmp_path, monkeypatch):
    monkeypatch.setenv("SKBLOOM_STATE_DIR", str(tmp_path))
    (tmp_path / "home.json").write_text(
        '{"completed": ["preflight", "bootstrap-cluster", "deploy:login", "deploy:cache", "final-check"]}')
    (tmp_path / "wip.json").write_text('{"completed": ["preflight", "bootstrap-cluster"]}')
    out = web.api_status()
    byname = {c["cluster"]: c for c in out["clusters"]}
    assert byname["home"]["complete"] is True
    assert {s["name"] for s in byname["home"]["services"]} == {"login", "cache"}
    assert byname["wip"]["complete"] is False and byname["wip"]["services"] == []


def test_api_status_includes_service_urls_from_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("SKBLOOM_STATE_DIR", str(tmp_path))
    (tmp_path / "home.json").write_text(json.dumps({
        "completed": ["deploy:login", "final-check"],
        "meta": {"urls": {"login": "https://login.sk.local"}}}))
    svc = web.api_status()["clusters"][0]["services"][0]
    assert svc["name"] == "login" and svc["url"] == "https://login.sk.local"


# ── day-2 control plane ──────────────────────────────────────────────────────

class FakeRunner:
    """Records every argv it's handed; returns a canned (stdout, rc)."""
    def __init__(self, stdout="", rc=0):
        self.stdout, self.rc = stdout, rc
        self.calls = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        return (self.stdout, self.rc)


def _pods_json():
    return json.dumps({"items": [
        {"metadata": {"namespace": "login"},
         "status": {"containerStatuses": [{"ready": True}, {"ready": True}]}},
        {"metadata": {"namespace": "login"},
         "status": {"containerStatuses": [{"ready": True}, {"ready": True}]}},
        {"metadata": {"namespace": "cache"},
         "status": {"containerStatuses": [{"ready": True}, {"ready": False}]}},
    ]})


def test_api_health_parses_ready_total_per_service():
    fake = FakeRunner(stdout=_pods_json(), rc=0)
    out = web.api_health("home", run=fake)
    byname = {s["name"]: s for s in out["services"]}
    assert byname["login"] == {"name": "login", "ready": 4, "total": 4, "healthy": True}
    assert byname["cache"] == {"name": "cache", "ready": 1, "total": 2, "healthy": False}
    # uses the cluster's k3d context and the all-namespaces json query
    assert fake.calls == [["kubectl", "--context", "k3d-home",
                           "get", "pods", "-A", "-o", "json"]]


def test_api_health_fails_soft_on_error():
    assert web.api_health("home", run=FakeRunner(stdout="boom", rc=1)) == {}
    assert web.api_health("home", run=FakeRunner(stdout="not json", rc=0)) == {}


def test_api_restart_issues_rollout_restart():
    fake = FakeRunner(stdout="deployment.apps/login restarted\n", rc=0)
    out = web.api_restart("home", "login", run=fake)
    assert out["ok"] is True and "restarted" in out["output"]
    assert fake.calls == [["kubectl", "--context", "k3d-home",
                           "rollout", "restart", "deploy/login", "-n", "login"]]


def test_api_restart_reports_failure():
    out = web.api_restart("home", "login", run=FakeRunner(stdout="nope", rc=1))
    assert out["ok"] is False


def test_api_scale_issues_scale_with_replicas():
    fake = FakeRunner(stdout="deployment.apps/cache scaled\n", rc=0)
    out = web.api_scale("home", "cache", 3, run=fake)
    assert out["ok"] is True
    assert fake.calls == [["kubectl", "--context", "k3d-home",
                           "scale", "deploy/cache", "-n", "cache", "--replicas=3"]]


def test_log_lines_yields_each_line_from_runner():
    fake = FakeRunner(stdout="line one\nline two\nline three\n", rc=0)
    lines = list(web._log_lines("home", "login", run=fake))
    assert lines == ["line one", "line two", "line three"]
    assert fake.calls == [["kubectl", "--context", "k3d-home",
                           "logs", "-f", "--tail=100", "deploy/login", "-n", "login"]]


def test_log_lines_empty_on_runner_error():
    assert list(web._log_lines("home", "login",
                               run=FakeRunner(stdout="err: not found", rc=1))) == []
