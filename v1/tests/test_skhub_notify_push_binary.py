"""The skhub notify_push fallback must never install an HTTP error page as the binary.

v2.26.17 gate: GitHub answered a 504 while the role fetched the notify_push
binary; ``curl -sL`` saved the 92-byte HTML page, ``test -f`` accepted it,
and notify_push crash-looped on "exec format error". These tests run the
real shell block from each env playbook with fake ``docker`` and ``curl``.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

SKHUB = Path(__file__).resolve().parents[1] / "ansible" / "optional" / "skhub"
ENVS = ("dev", "staging", "prod")
REAL_BIN = "/var/www/html/custom_apps/notify_push/bin/x86_64/notify_push"
ELF = b"\x7fELF\x02\x01\x01" + b"\0" * 57
HTML_504 = b"<html><body><h1>504 Gateway Time-out</h1>\nThe server didn't respond in time.\n</body></html>\n"


def _block(env: str) -> str:
    plays = yaml.safe_load((SKHUB / f"deploy_skhub-{env}.yml").read_text())
    for play in plays:
        for task in play.get("tasks", []) or []:
            shell = task.get("shell") if isinstance(task, dict) else None
            if isinstance(shell, str) and REAL_BIN in shell:
                return shell
    raise AssertionError(f"notify_push binary task not found in deploy_skhub-{env}.yml")


def _run(tmp_path: Path, env: str, existing: bytes | None, download: bytes | None) -> tuple[subprocess.CompletedProcess, Path]:
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    binpath = tmp_path / "custom_apps" / "notify_push" / "bin" / "x86_64" / "notify_push"
    if existing is not None:
        binpath.parent.mkdir(parents=True)
        binpath.write_bytes(existing)
    payload = tmp_path / "payload"
    if download is not None:
        payload.write_bytes(download)
    # docker exec <id> <cmd...> runs <cmd...> here; everything else is a no-op.
    (fakebin / "docker").write_text(
        '#!/bin/bash\n[ "$1" = exec ] || exit 0\nshift 2\n'
        '[ "$1" = php ] && exit 0\n[ "$1" = chown ] && exit 0\nexec "$@"\n'
    )
    # curl -fsSL ... -o <out> <url>: copy the payload, or fail like curl -f on a 504.
    (fakebin / "curl").write_text(
        '#!/bin/bash\nout=""\nwhile [ $# -gt 0 ]; do [ "$1" = -o ] && { out="$2"; shift; }; shift; done\n'
        f'[ -f "{payload}" ] || exit 22\ncp "{payload}" "$out"\n'
    )
    (fakebin / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in fakebin.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    script = _block(env).replace(REAL_BIN, str(binpath))
    script = script.replace("{{ nextcloud_container.stdout | trim }}", "cid").replace(
        "{{ skhub_public_host }}", "hub.example.test"
    )
    proc = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "PATH": f"{fakebin}:{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    return proc, binpath


@pytest.mark.parametrize("env", ENVS)
def test_valid_existing_binary_is_kept(tmp_path: Path, env: str) -> None:
    proc, binpath = _run(tmp_path, env, existing=ELF, download=None)
    assert proc.returncode == 0, proc.stderr
    assert binpath.read_bytes() == ELF


@pytest.mark.parametrize("env", ENVS)
def test_error_page_is_replaced_by_a_real_download(tmp_path: Path, env: str) -> None:
    proc, binpath = _run(tmp_path, env, existing=HTML_504, download=ELF)
    assert proc.returncode == 0, proc.stderr
    assert binpath.read_bytes() == ELF
    assert os.access(binpath, os.X_OK)


@pytest.mark.parametrize("env", ENVS)
def test_failed_download_fails_the_task(tmp_path: Path, env: str) -> None:
    proc, binpath = _run(tmp_path, env, existing=None, download=None)
    assert proc.returncode != 0
    assert "download failed" in proc.stderr
    assert not binpath.exists()
    assert not binpath.with_name("notify_push.part").exists()


@pytest.mark.parametrize("env", ENVS)
def test_downloaded_error_page_fails_the_task(tmp_path: Path, env: str) -> None:
    proc, _ = _run(tmp_path, env, existing=None, download=HTML_504)
    assert proc.returncode != 0
    assert "not an ELF executable" in proc.stderr
