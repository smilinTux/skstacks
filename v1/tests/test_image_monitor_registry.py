"""registry.py must distinguish three cases upstream can answer with: a
clean result (digest/tags), "rate limited, ask again later" (429, repeated
past the retry budget), and "this needs a token first" (401 with a
Www-Authenticate challenge) - the last one transparently, with one retry
carrying the token. Docker Hub and the generic OCI v2 path are both
exercised since they share no code but must behave the same way to callers."""
import json
import pathlib
import sys
import urllib.error

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts" / "image_monitor"))

import registry  # noqa: E402
from imageref import parse_image_ref  # noqa: E402


class FakeResponse:
    def __init__(self, body, status=200, headers=None):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code, headers=None):
    return urllib.error.HTTPError("https://example.test", code, "err", headers or {}, None)


def test_dockerhub_tag_digest_happy_path():
    def fake_urlopen(req, timeout):
        assert "hub.docker.com/v2/repositories/library/redis/tags/7-alpine" in req.full_url
        return FakeResponse({"digest": "sha256:" + "a" * 64, "images": []})

    digest = registry.dockerhub_tag_digest("library", "redis", "7-alpine", urlopen=fake_urlopen)
    assert digest == "sha256:" + "a" * 64


def test_dockerhub_tag_gone_returns_none():
    calls = []

    def fake_urlopen(req, timeout):
        calls.append(req.full_url)
        raise _http_error(404)

    digest = registry.dockerhub_tag_digest("library", "ghost-repo", "nosuchtag", urlopen=fake_urlopen)
    assert digest is None
    assert calls


def test_rate_limit_exhausts_retries_then_raises(monkeypatch):
    monkeypatch.setattr(registry.time, "sleep", lambda _: None)
    attempts = {"n": 0}

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        raise _http_error(429)

    with pytest.raises(registry.RateLimited):
        registry.dockerhub_tag_digest("library", "redis", "7-alpine", max_retries=2, backoff_base=0.01,
                                      urlopen=fake_urlopen)
    assert attempts["n"] == 3  # the original attempt plus 2 retries


def test_rate_limit_recovers_within_retry_budget(monkeypatch):
    monkeypatch.setattr(registry.time, "sleep", lambda _: None)
    attempts = {"n": 0}

    def fake_urlopen(req, timeout):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _http_error(429)
        return FakeResponse({"digest": "sha256:" + "b" * 64, "images": []})

    digest = registry.dockerhub_tag_digest("library", "redis", "7-alpine", max_retries=4, backoff_base=0.01,
                                           urlopen=fake_urlopen)
    assert digest == "sha256:" + "b" * 64


def test_ghcr_anonymous_token_challenge_then_retry_with_bearer():
    seen_auth_headers = []

    def fake_urlopen(req, timeout):
        if req.full_url == "https://ghcr.io/v2/org/repo/tags/list":
            raise _http_error(401, headers={
                "Www-Authenticate": 'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:org/repo:pull"'
            })
        if req.full_url.startswith("https://ghcr.io/token"):
            return FakeResponse({"token": "anon-token-xyz"})
        if req.full_url == "https://ghcr.io/v2/org/repo/manifests/latest":
            seen_auth_headers.append(req.get_header("Authorization"))
            return FakeResponse(b"", headers={"Docker-Content-Digest": "sha256:" + "c" * 64})
        raise AssertionError(f"unexpected URL {req.full_url}")

    digest = registry.registry_v2_digest("ghcr.io", "org/repo", "latest", urlopen=fake_urlopen)
    assert digest == "sha256:" + "c" * 64
    assert seen_auth_headers == ["Bearer anon-token-xyz"]


def test_registry_v2_tag_gone_returns_none():
    def fake_urlopen(req, timeout):
        if "tags/list" in req.full_url:
            return FakeResponse({"tags": ["1.0"]})
        raise _http_error(404)

    digest = registry.registry_v2_digest("ghcr.io", "org/repo", "doesnotexist", urlopen=fake_urlopen)
    assert digest is None


def test_check_upstream_detects_digest_change_and_caches(tmp_path):
    cache = registry.load_cache(tmp_path / "cache.json", ttl_hours=20)
    ref = parse_image_ref("redis:7-alpine@sha256:" + "0" * 64)
    calls = {"n": 0}

    def fake_urlopen(req, timeout):
        calls["n"] += 1
        return FakeResponse({"digest": "sha256:" + "1" * 64, "images": []})

    result = registry.check_upstream(ref, cache, urlopen=fake_urlopen)
    assert result.digest_changed is True
    assert calls["n"] == 1

    # Second call for the same tag must hit the cache, not the network.
    result2 = registry.check_upstream(ref, cache, urlopen=fake_urlopen)
    assert result2.digest_changed is True
    assert calls["n"] == 1


def test_check_upstream_unchanged_when_digest_matches(tmp_path):
    cache = registry.load_cache(tmp_path / "cache.json", ttl_hours=20)
    digest = "sha256:" + "2" * 64
    ref = parse_image_ref(f"redis:7-alpine@{digest}")

    def fake_urlopen(req, timeout):
        return FakeResponse({"digest": digest, "images": []})

    result = registry.check_upstream(ref, cache, urlopen=fake_urlopen)
    assert result.digest_changed is False


def test_check_upstream_rate_limited_is_inconclusive_not_a_change(tmp_path, monkeypatch):
    monkeypatch.setattr(registry.time, "sleep", lambda _: None)
    cache = registry.load_cache(tmp_path / "cache.json", ttl_hours=20)
    ref = parse_image_ref("redis:7-alpine@sha256:" + "3" * 64)

    def fake_urlopen(req, timeout):
        raise _http_error(429)

    result = registry.check_upstream(ref, cache, max_retries=1, backoff_base=0.01, urlopen=fake_urlopen)
    assert result.rate_limited is True
    assert result.digest_changed is None
