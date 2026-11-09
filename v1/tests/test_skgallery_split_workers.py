"""skgallery: SPLIT_WORKERS moves Immich's background jobs out of the API.

Immich runs its API and its job workers (video transcoding, thumbnails,
metadata) in one container by default, under one CPU limit. On one instance
(2026-11-09) software transcoding pinned immich-server at its whole 4-CPU
limit: the API stopped reading upload bodies, the iOS app's 60 s write timeout
gave up on large videos, and Traefik's 10 s health check marked the backend
down (a burst of 503s). With SPLIT_WORKERS the API service runs with
IMMICH_WORKERS_INCLUDE=api and a separate immich-microservices service runs
every other worker under its own limits.

Unset, the stack renders exactly as before: no new service, no environment
override on immich-server.
"""
import yaml

from test_router_middlewares_knob import render


def _services(**over):
    return yaml.safe_load(render("skgallery", **over))["services"]


def test_unset_renders_no_split():
    svcs = _services()
    assert "immich-microservices" not in svcs
    assert "environment" not in svcs["immich-server"]


def test_split_runs_api_only_in_the_server():
    svcs = _services(SPLIT_WORKERS=True)
    assert svcs["immich-server"]["environment"] == ["IMMICH_WORKERS_INCLUDE=api"]
    # The API keeps its router; the worker gets none.
    assert any("traefik.enable=true" == l for l in svcs["immich-server"]["deploy"]["labels"])


def test_split_adds_a_worker_with_every_job_but_the_api():
    w = _services(SPLIT_WORKERS=True)["immich-microservices"]
    s = _services(SPLIT_WORKERS=True)["immich-server"]
    assert w["environment"] == ["IMMICH_WORKERS_EXCLUDE=api"]
    assert w["image"] == s["image"]
    assert w["env_file"] == s["env_file"]
    # Same media storage as the API, or jobs cannot see the uploads.
    assert [v for v in w["volumes"] if "/usr/src/app/upload" in v] == \
        [v for v in s["volumes"] if "/usr/src/app/upload" in v]
    assert "labels" not in w["deploy"]
    assert w["healthcheck"] == {"disable": True}
    assert w["networks"] == ["skgallery-prod"]


def test_split_worker_has_its_own_limits():
    w = _services(SPLIT_WORKERS=True, MICROSERVICES_CPU_LIMIT="6.0",
                  MICROSERVICES_MEMORY_LIMIT="6G")["immich-microservices"]
    assert w["deploy"]["resources"]["limits"] == {"cpus": "6.0", "memory": "6G"}


def test_split_accepts_string_booleans():
    """Vault values often arrive as strings."""
    assert "immich-microservices" in _services(SPLIT_WORKERS="true")
    assert "immich-microservices" not in _services(SPLIT_WORKERS="false")
