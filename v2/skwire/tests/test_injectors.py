"""Real injectors — env-file, json-config, and app-API. All satisfy the contract."""
from __future__ import annotations

import json

from skwire import Injector
from skwire.inject import EnvFileInjector, JsonConfigInjector, ApiPostInjector


def test_envfile_injector_writes_and_updates(tmp_path):
    inj = EnvFileInjector(str(tmp_path))
    assert isinstance(inj, Injector) and inj.method == "env"
    inj.inject("sonarr", "API_KEY", "abc")
    p = tmp_path / "sonarr.env"
    assert "API_KEY=abc" in p.read_text()
    inj.inject("sonarr", "API_KEY", "xyz")            # update in place
    body = p.read_text()
    assert "API_KEY=xyz" in body and "API_KEY=abc" not in body
    inj.inject("sonarr", "OTHER", "1")                # add another, keep first
    body = p.read_text()
    assert "API_KEY=xyz" in body and "OTHER=1" in body


def test_jsonconfig_injector_merges(tmp_path):
    inj = JsonConfigInjector(str(tmp_path))
    assert inj.method == "file"
    inj.inject("radarr", "apiKey", "abc")
    inj.inject("radarr", "host", "radarr:7878")
    d = json.loads((tmp_path / "radarr.json").read_text())
    assert d == {"apiKey": "abc", "host": "radarr:7878"}


def test_api_injector_posts_with_injected_transport():
    calls = []
    def fake_post(url, payload, headers):
        calls.append((url, payload, headers)); return True
    inj = ApiPostInjector("http://{target}.svc/api/config", post=fake_post)
    assert inj.method == "api"
    ok = inj.inject("prowlarr", "api_key", "secret123")
    assert ok is True
    url, payload, headers = calls[0]
    assert url == "http://prowlarr.svc/api/config"
    assert payload == {"api_key": "secret123"}


def test_api_injector_reports_failure_not_raises():
    def boom(url, payload, headers): raise RuntimeError("down")
    inj = ApiPostInjector("http://{target}/c", post=boom)
    assert inj.inject("x", "k", "v") is False
