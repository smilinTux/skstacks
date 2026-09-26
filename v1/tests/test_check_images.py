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
