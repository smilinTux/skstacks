"""The Duplicati-based skbackup (v2.19.0 to v2.22.0) was never deployed on
any instance and is replaced by the host-level restic app. Nothing may
bring it back by accident: no Duplicati image, compose file, swarm deploy
script or Duplicati vault key in the framework's v1 tree."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "v1/ansible/optional/skbackup"


def test_no_duplicati_left_in_v1_ansible_or_render_vars():
    """Only the skbackup README (upgrade note) and the deploy's rejection of
    the old vault keys may still name it."""
    allowed = {APP / "README.md", *APP.glob("deploy_skbackup-*.yml")}
    hits = []
    for base in (ROOT / "v1/ansible", ROOT / "v1/tests/render"):
        for p in base.rglob("*"):
            if p.is_file() and p not in allowed and re.search("duplicati", p.read_text(errors="ignore"), re.I):
                hits.append(str(p.relative_to(ROOT)))
    assert hits == []


def test_skbackup_is_host_level_not_a_swarm_stack():
    assert not list(APP.rglob("*.yml.j2")), "a compose template came back"
    assert not (APP / "src/skbackup/deploy.j2").exists()
    for env in ("dev", "staging", "prod"):
        text = (APP / f"deploy_skbackup-{env}.yml").read_text()
        assert "docker stack deploy" not in text and "select_manager_node" not in text


def test_old_vault_keys_are_rejected_not_ignored():
    """An instance vault still carrying the Duplicati keys fails the deploy
    with a pointer to the upgrade note, instead of silently ignoring them."""
    text = (APP / "deploy_skbackup-prod.yml").read_text()
    for key in ("settings_encryption_key", "ui_password", "jobs"):
        assert key in text
