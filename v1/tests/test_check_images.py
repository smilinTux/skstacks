"""check_images.sh must fail only on images that are really gone. Docker Hub
answers anonymous bursts with `toomanyrequests`; that is inconclusive, not a
missing image (the first local run marked every Docker Hub image FAIL)."""
import os
import pathlib
import subprocess

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "check_images.sh"


def _run(tmp_path, responses):
    stub = tmp_path / "docker"
    cases = "\n".join(f'  *"{img}"*) echo "{msg}" >&2; exit {rc};;' for img, (rc, msg) in responses.items())
    stub.write_text(f'#!/bin/sh\ncase "$*" in\n{cases}\n  *) exit 0;;\nesac\n')
    stub.chmod(0o755)
    lister = tmp_path / "list.py"
    lister.write_text("print('\\n'.join('svc\\t'+i for i in " + repr(list(responses)) + "))\n")
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "SKSTACKS_LIST_IMAGES": str(lister)}
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)


def test_rate_limit_is_inconclusive_not_failure(tmp_path):
    r = _run(tmp_path, {"a/ok:1": (0, ""), "b/limited:1": (1, "toomanyrequests: You have reached your pull rate limit")})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "RATE-LIMITED" in r.stdout


def test_missing_image_fails(tmp_path):
    r = _run(tmp_path, {"c/gone:1": (1, "no such manifest: docker.io/c/gone:1")})
    assert r.returncode == 1 and "FAIL" in r.stdout


def test_access_denied_is_inconclusive_not_failure(tmp_path):
    """A registry answering "denied"/"unauthorized" to an anonymous request
    (a gated package, or a framework-built image whose release tag has not
    been pushed yet) is not distinguishable from "gone" without
    credentials, so it must not permanently fail the canary either."""
    r = _run(tmp_path, {
        "a/ok:1": (0, ""),
        "d/private:1": (1, 'Get "https://ghcr.io/v2/d/private/manifests/1": denied'),
    })
    assert r.returncode == 0, r.stdout + r.stderr
    assert "ACCESS-DENIED" in r.stdout


def test_pinned_digest_with_moved_tag_is_ok(tmp_path):
    """`name:tag@digest` fails `docker manifest inspect` once upstream re-pushes
    the tag ("manifest verification failed"), yet the digest still pulls. The
    pin is what deploys, so it must be judged by the digest alone."""
    stub_fail = (1, "manifest verification failed for digest sha256:abc")
    r = _run(tmp_path, {"e/moved:1@sha256:abc": stub_fail})
    assert r.returncode == 0, r.stdout + r.stderr
    assert "tag moved" in r.stdout


def test_pinned_digest_that_is_gone_fails(tmp_path):
    r = _run(tmp_path, {
        "f/gone:1@sha256:dead": (1, "manifest verification failed for digest sha256:dead"),
        "f/gone@sha256:dead": (1, "manifest unknown"),
    })
    assert r.returncode == 1 and "FAIL" in r.stdout


def test_registry_port_is_not_mistaken_for_a_tag(tmp_path):
    """host:port/name@digest has no tag; the fallback must not rewrite it."""
    r = _run(tmp_path, {"reg.local:5000/g/img@sha256:beef": (1, "manifest unknown")})
    assert r.returncode == 1 and "FAIL" in r.stdout
    assert "tag moved" not in r.stdout
