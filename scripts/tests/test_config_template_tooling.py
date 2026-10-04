"""Tests for the config-template generation and scrub tooling that backs the
public smilinTux/skstacks-config-template repo (Gap 1 of the 2026-10-02
config-template-and-safe-deploy design).

- scripts/generate-config-template.sh: copies templates/config-repo/
  verbatim into an output dir. The CI drift job
  (.github/workflows/config-template-drift.yml) runs this and diffs the
  result against a clone of the live template repo; that live-repo
  comparison needs network and isn't exercised here. What's tested here is
  that the generator reproduces the source exactly and clears stale files.
- scripts/scrub-check.sh: fails on a real private IP under a given path,
  with a small allow-list for documentation/placeholder addresses. It
  deliberately does not also hardcode real fleet hostnames/domains (see the
  comment at the top of that script for why); that half is covered by the
  existing estate-denylist gate in .github/workflows/skred-scan.yml.
- the fixture carries no ansible-vault ciphertext (this framework repo's
  skred gate forbids it anywhere in the tree); the documented demo password
  round-trips through a real `ansible-vault encrypt`/`view`, and the
  plaintext `.example` file's placeholder secrets match the framework's own
  real-Ansible render-gate fixture for the same service, so the render-gate
  CI job (v1/tests/render/render_playbooks.py --service skbook) is proof the
  template's placeholder values render through real Ansible, not just a
  structurally-similar stand-in.

Run with: python -m pytest -q scripts/tests/test_config_template_tooling.py
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
GENERATE_SCRIPT = REPO_ROOT / "scripts" / "generate-config-template.sh"
SCRUB_SCRIPT = REPO_ROOT / "scripts" / "scrub-check.sh"
SOURCE_DIR = REPO_ROOT / "templates" / "config-repo"
VAULT_DIR = SOURCE_DIR / "v1" / "ansible" / "optional" / "group_vars" / "prod"
VAULT_EXAMPLE = VAULT_DIR / "skbook-prod_vault.yml.example"
RENDER_GATE_FIXTURE = REPO_ROOT / "v1" / "tests" / "render" / "vars" / "skbook.example.yml"

# Documented demo password for the fixture's vault. Not a secret: the whole
# point is that it is public and that every value it protects is a
# placeholder. See templates/config-repo/README.md "Set the vault password".
DEMO_VAULT_PASSWORD = "skstacks-template-demo"


def run(script: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True)


def list_files_recursive(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def write_fixture(tmp_path: Path, name: str, content: str) -> Path:
    d = tmp_path / "fixture"
    d.mkdir(exist_ok=True)
    (d / name).write_text(content)
    return d


# --- scripts/generate-config-template.sh -----------------------------------

def test_generate_reproduces_source_exactly(tmp_path):
    out = tmp_path / "generated"
    r = run(GENERATE_SCRIPT, [str(out)])
    assert r.returncode == 0, r.stderr

    source_files = list_files_recursive(SOURCE_DIR)
    out_files = list_files_recursive(out)
    assert out_files == source_files

    for f in source_files:
        # the encrypted vault is binary-ish ciphertext; compare bytes
        assert (out / f).read_bytes() == (SOURCE_DIR / f).read_bytes(), f"content mismatch for {f}"


def test_generate_clears_a_stale_file(tmp_path):
    out = tmp_path / "generated"
    out.mkdir()
    (out / "stale-leftover.txt").write_text("should be removed\n")
    r = run(GENERATE_SCRIPT, [str(out)])
    assert r.returncode == 0, r.stderr
    assert not (out / "stale-leftover.txt").exists()


def test_generated_readme_has_no_relative_links_only_valid_inside_this_repo(tmp_path):
    out = tmp_path / "generated"
    run(GENERATE_SCRIPT, [str(out)])
    readme = (out / "README.md").read_text()
    # A link like ../../v1/ansible/... resolves inside this repo but 404s
    # once this file is the ROOT README of the published template repo,
    # which has no v1/ directory at all.
    assert "](../" not in readme


def test_generate_refuses_with_usage_error_when_no_output_dir_given():
    r = run(GENERATE_SCRIPT, [])
    assert r.returncode != 0
    assert "usage" in r.stderr.lower()


# --- scripts/scrub-check.sh --------------------------------------------------

def test_scrub_passes_on_the_real_fixture():
    r = run(SCRUB_SCRIPT, [str(SOURCE_DIR)])
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("ip", ["192.168.50.7", "10.4.9.22", "172.20.5.5"])
def test_scrub_fails_on_a_real_private_ip(tmp_path, ip):
    d = write_fixture(tmp_path, "fixture.yaml", f"backend: http://{ip}:8080\n")
    r = run(SCRUB_SCRIPT, [str(d)])
    assert r.returncode != 0
    assert ip in r.stderr


def test_scrub_passes_on_172_15_and_172_32_outside_the_private_range(tmp_path):
    d = write_fixture(tmp_path, "fixture.yaml", "a: 172.15.5.5\nb: 172.32.5.5\n")
    r = run(SCRUB_SCRIPT, [str(d)])
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.parametrize("ip", ["192.0.2.5", "198.51.100.9", "203.0.113.3", "192.168.1.1", "10.0.0.1"])
def test_scrub_passes_on_rfc5737_and_generic_placeholder_ips(tmp_path, ip):
    d = write_fixture(tmp_path, "fixture.yaml", f"a: {ip}\n")
    r = run(SCRUB_SCRIPT, [str(d)])
    assert r.returncode == 0, r.stdout + r.stderr


def test_scrub_passes_on_generic_placeholder_hostnames(tmp_path):
    d = write_fixture(tmp_path, "fixture.yaml",
                       "a: example.com\nb: example-backend-host\nc: example.test\n")
    r = run(SCRUB_SCRIPT, [str(d)])
    assert r.returncode == 0, r.stdout + r.stderr


def test_scrub_refuses_with_usage_error_when_no_path_given():
    r = run(SCRUB_SCRIPT, [])
    assert r.returncode != 0
    assert "usage" in r.stderr.lower()


# --- the fixture's vault: no ciphertext in THIS repo, documented password --
#
# templates/config-repo/ ships only the plaintext .example file: this
# framework repo's own skred security gate (v2/skred/denylist.py) fails
# closed on any file starting with the $ANSIBLE_VAULT header, anywhere in
# the tree, so real ciphertext can never live here even as a placeholder
# fixture. The published smilinTux/skstacks-config-template repo (a
# separate, plain repo with no such gate) carries the real,
# demo-password-encrypted sibling; the `drift` CI job decrypts it on every
# push/PR and fails if it no longer matches this file exactly. What's
# tested here, without needing network or that published repo, is that the
# documented encrypt command and demo password actually work.

def test_documented_demo_password_round_trips_through_ansible_vault(tmp_path):
    if shutil.which("ansible-vault") is None:
        pytest.skip("ansible-vault not installed")
    pw_file = tmp_path / "vault-pass"
    pw_file.write_text(DEMO_VAULT_PASSWORD)
    encrypted = tmp_path / "skbook-prod_vault.yml"
    enc = subprocess.run(
        ["ansible-vault", "encrypt", "--vault-password-file", str(pw_file),
         "--output", str(encrypted), str(VAULT_EXAMPLE)],
        capture_output=True, text=True,
    )
    assert enc.returncode == 0, enc.stderr
    assert encrypted.read_text().splitlines()[0].startswith("$ANSIBLE_VAULT;1.")

    view = subprocess.run(
        ["ansible-vault", "view", "--vault-password-file", str(pw_file), str(encrypted)],
        capture_output=True, text=True,
    )
    assert view.returncode == 0, view.stderr
    assert view.stdout == VAULT_EXAMPLE.read_text()


def test_no_ansible_vault_ciphertext_is_committed_in_this_repo():
    """The hard constraint itself (v2/skred/denylist.py's VAULT_HEADER
    check): fail the PR fast and locally, instead of discovering it only
    from the skred-scan.yml CI job."""
    hits = []
    for p in SOURCE_DIR.rglob("*"):
        if p.is_file() and p.read_bytes().startswith(b"$ANSIBLE_VAULT"):
            hits.append(str(p.relative_to(REPO_ROOT)))
    assert not hits, f"real ansible-vault ciphertext committed in the framework repo: {hits}"


def test_fixture_placeholder_secrets_match_the_framework_render_gate_fixture():
    """Ties the fixture to v1/tests/render/render_playbooks.py's existing,
    already-CI-gated skbook.example.yml, so the two cannot silently drift
    apart and the render-gate CI job (which renders skbook.example.yml
    through real Ansible) is meaningful proof for this fixture's values too."""
    fixture_vault = yaml.safe_load(VAULT_EXAMPLE.read_text())["skbook"]
    render_gate_vault = yaml.safe_load(RENDER_GATE_FIXTURE.read_text())["skbook"]
    # Every key the render gate fixture sets must carry the identical
    # placeholder value here (CLUSTERNAME/DOMAIN included: both fixtures use
    # the same "render" / "example.test" placeholders).
    for key, value in render_gate_vault.items():
        assert fixture_vault.get(key) == value, (
            f"skbook.{key} diverged between templates/config-repo and "
            f"v1/tests/render/vars/skbook.example.yml"
        )
