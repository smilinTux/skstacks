"""Upstream registry lookups: is a newer digest or tag available for an image.

Two paths:

- Docker Hub images go through the public REST API
  (hub.docker.com/v2/repositories/<ns>/<repo>/tags/...), which is the only
  place that exposes `last_updated` and lets us sort tags by it.
- Everything else (GHCR, lscr.io, quay.io, a private registry) goes through
  the generic OCI distribution v2 API, with an anonymous bearer token
  fetched from the realm the registry's own 401 challenge names.

Both paths share one on-disk cache (keyed by registry+repo+tag, with a TTL)
and one backoff-on-429 retry loop, so a run that touches the same
`redis:7-alpine` for ten services only hits the network once.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from imageref import ImageRef, is_docker_hub

USER_AGENT = "skstacks-image-monitor/1.0 (+https://github.com/smilinTux/skstacks)"


class RateLimited(Exception):
    """Raised when a registry keeps answering 429 past the retry budget.

    Callers must treat this as inconclusive, not "no update available" -
    the same convention check_images.sh already uses for docker manifest
    inspect's "toomanyrequests".
    """


@dataclass
class UpstreamResult:
    digest: str | None = None               # upstream digest for the pinned tag, if resolvable
    digest_changed: bool | None = None       # None == couldn't determine (rate-limited/error)
    newer_tag: str | None = None             # a newer tag on the same line, if any
    error: str | None = None                 # set on a non-fatal lookup failure (not rate limiting)
    rate_limited: bool = False


@dataclass
class _Cache:
    path: Path
    ttl_seconds: float
    _data: dict = field(default_factory=dict)
    _loaded: bool = False

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            self._data = json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            self._data = {}

    def get(self, key: str) -> dict | None:
        self._load()
        entry = self._data.get(key)
        if not entry:
            return None
        if time.time() - entry.get("_cached_at", 0) > self.ttl_seconds:
            return None
        return entry

    def set(self, key: str, value: dict) -> None:
        self._load()
        value = {**value, "_cached_at": time.time()}
        self._data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True))
        tmp.replace(self.path)


def load_cache(path: str | Path, ttl_hours: float) -> _Cache:
    return _Cache(path=Path(path).expanduser(), ttl_seconds=ttl_hours * 3600)


def _urlopen(req: urllib.request.Request, timeout: float):
    """Thin wrapper so tests can monkeypatch exactly one function."""
    return urllib.request.urlopen(req, timeout=timeout)


def _get_json(url: str, headers: dict, timeout: float, max_retries: int, backoff_base: float,
              urlopen: Callable = _urlopen) -> tuple[int, dict | None, dict]:
    """GET a URL expecting JSON, retrying with backoff on 429.

    Returns (status_code, parsed_body_or_None, response_headers).
    Raises RateLimited if every retry is also a 429.
    """
    attempt = 0
    while True:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                data = json.loads(body) if body else None
                return resp.status, data, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                attempt += 1
                if attempt > max_retries:
                    raise RateLimited(url)
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            if exc.code == 401:
                return 401, None, dict(exc.headers or {})
            return exc.code, None, dict(exc.headers or {})


# --------------------------------------------------------------------------
# Docker Hub
# --------------------------------------------------------------------------

def dockerhub_tag_digest(ns: str, repo: str, tag: str, *, timeout: float = 15,
                         max_retries: int = 4, backoff_base: float = 5,
                         urlopen: Callable = _urlopen) -> str | None:
    """Return the current digest Docker Hub reports for ns/repo:tag, or None if the tag is gone."""
    url = f"https://hub.docker.com/v2/repositories/{ns}/{repo}/tags/{tag}"
    status, data, _ = _get_json(url, {}, timeout, max_retries, backoff_base, urlopen)
    if status == 404:
        return None
    if status != 200 or not data:
        raise LookupError(f"dockerhub tag lookup {ns}/{repo}:{tag} -> HTTP {status}")
    images = data.get("images") or []
    digests = {img.get("digest") for img in images if img.get("digest")}
    return data.get("digest") or (sorted(digests)[0] if digests else None)


def dockerhub_tags_sorted(ns: str, repo: str, *, page_size: int = 25, timeout: float = 15,
                          max_retries: int = 4, backoff_base: float = 5,
                          urlopen: Callable = _urlopen) -> list[str]:
    """Return ns/repo's tag names, newest `last_updated` first."""
    url = (f"https://hub.docker.com/v2/repositories/{ns}/{repo}/tags"
           f"?page_size={page_size}&ordering=last_updated")
    status, data, _ = _get_json(url, {}, timeout, max_retries, backoff_base, urlopen)
    if status != 200 or not data:
        raise LookupError(f"dockerhub tag list {ns}/{repo} -> HTTP {status}")
    return [r["name"] for r in data.get("results", []) if r.get("name")]


# --------------------------------------------------------------------------
# Generic OCI distribution v2 (GHCR / lscr.io / quay.io / anything else)
# --------------------------------------------------------------------------

_WWW_AUTH_RE = re.compile(r'(\w+)="([^"]*)"')


def _parse_www_authenticate(header: str) -> dict[str, str]:
    return dict(_WWW_AUTH_RE.findall(header or ""))


def _anonymous_token(registry: str, repo: str, *, timeout: float, urlopen: Callable) -> str | None:
    """Fetch an anonymous pull token the way the OCI distribution spec
    describes: probe the registry, read the Www-Authenticate challenge it
    answers with, then ask the realm it names for a token scoped to this repo.
    Returns None if the registry needs no token at all (some do not)."""
    probe_url = f"https://{registry}/v2/{repo}/tags/list"
    req = urllib.request.Request(probe_url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return None  # no auth needed
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            raise
        challenge = _parse_www_authenticate(exc.headers.get("Www-Authenticate", ""))
    realm = challenge.get("realm")
    if not realm:
        raise LookupError(f"{registry} sent no usable auth challenge")
    service = challenge.get("service", "")
    scope = challenge.get("scope") or f"repository:{repo}:pull"
    token_url = f"{realm}?service={service}&scope={scope}"
    req = urllib.request.Request(token_url, headers={"User-Agent": USER_AGENT})
    with urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    return data.get("token") or data.get("access_token")


def registry_v2_digest(registry: str, repo: str, tag: str, *, timeout: float = 15,
                       max_retries: int = 4, backoff_base: float = 5,
                       urlopen: Callable = _urlopen) -> str | None:
    """Return the manifest digest the registry reports for repo:tag (via the
    Docker-Content-Digest header), or None if the tag doesn't exist (404)."""
    token = _anonymous_token(registry, repo, timeout=timeout, urlopen=urlopen)
    headers = {
        "Accept": ("application/vnd.oci.image.index.v1+json, "
                   "application/vnd.docker.distribution.manifest.list.v2+json, "
                   "application/vnd.docker.distribution.manifest.v2+json"),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://{registry}/v2/{repo}/manifests/{tag}"
    attempt = 0
    while True:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers}, method="HEAD")
        try:
            with urlopen(req, timeout=timeout) as resp:
                digest = dict(resp.headers).get("Docker-Content-Digest")
                return digest
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            if exc.code == 429:
                attempt += 1
                if attempt > max_retries:
                    raise RateLimited(url)
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            raise LookupError(f"{registry}/{repo}:{tag} -> HTTP {exc.code}")


def registry_v2_tags(registry: str, repo: str, *, timeout: float = 15,
                     max_retries: int = 4, backoff_base: float = 5,
                     urlopen: Callable = _urlopen) -> list[str]:
    token = _anonymous_token(registry, repo, timeout=timeout, urlopen=urlopen)
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"https://{registry}/v2/{repo}/tags/list"
    status, data, _ = _get_json(url, headers, timeout, max_retries, backoff_base, urlopen)
    if status != 200 or not data:
        raise LookupError(f"{registry}/{repo} tag list -> HTTP {status}")
    return data.get("tags") or []


# --------------------------------------------------------------------------
# Unified entry point
# --------------------------------------------------------------------------

def check_upstream(ref: ImageRef, cache: _Cache, *, timeout: float = 15, max_retries: int = 4,
                   backoff_base: float = 5, urlopen: Callable = _urlopen) -> UpstreamResult:
    """Compare ref's pinned digest against what upstream reports for its tag.

    Cache key is registry+repo+tag (not the digest), so a cache hit still
    answers correctly for an image whose pin changed since the last run.
    """
    if not ref.tag:
        return UpstreamResult(error="no tag to check (digest-only pin)")

    key = f"{ref.registry}/{ref.path}:{ref.tag}"
    cached = cache.get(key)
    if cached is not None:
        return UpstreamResult(
            digest=cached.get("digest"),
            digest_changed=(ref.digest is not None and cached.get("digest") not in (None, ref.digest)),
            newer_tag=cached.get("newer_tag"),
        )

    try:
        if is_docker_hub(ref):
            upstream_digest = dockerhub_tag_digest(ref.namespace, ref.repo, ref.tag, timeout=timeout,
                                                   max_retries=max_retries, backoff_base=backoff_base,
                                                   urlopen=urlopen)
        else:
            upstream_digest = registry_v2_digest(ref.registry, ref.path, ref.tag, timeout=timeout,
                                                 max_retries=max_retries, backoff_base=backoff_base,
                                                 urlopen=urlopen)
    except RateLimited:
        return UpstreamResult(rate_limited=True, error="rate-limited")
    except (LookupError, urllib.error.URLError) as exc:
        return UpstreamResult(error=str(exc))

    cache.set(key, {"digest": upstream_digest})
    changed = ref.digest is not None and upstream_digest is not None and upstream_digest != ref.digest
    return UpstreamResult(digest=upstream_digest, digest_changed=changed)
