"""The swarm deploy playbook must resolve its roles from any working directory
(instances run it as framework/v2/platform/swarm/ansible/playbooks/deploy.yml)."""
import pathlib
import subprocess

PLAYBOOK = pathlib.Path(__file__).resolve().parents[1] / "ansible" / "playbooks" / "deploy.yml"


def test_deploy_playbook_resolves_roles_from_another_cwd(tmp_path):
    inv = tmp_path / "inv.ini"
    inv.write_text("[swarm_manager_primary]\nm1\n[swarm_managers]\nm1\n[swarm_workers]\n")
    r = subprocess.run(["ansible-playbook", "--syntax-check", "-i", str(inv), str(PLAYBOOK)],
                       cwd=tmp_path, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr


def _tasks(tmp_path, *extra):
    inv = tmp_path / "inv.ini"
    inv.write_text("[swarm_manager_primary]\nm1\n[swarm_managers]\nm1\n[swarm_workers]\nw1\n")
    r = subprocess.run(["ansible-playbook", "--list-tasks", "-i", str(inv), str(PLAYBOOK), *extra],
                       cwd=tmp_path, capture_output=True, text=True, check=True)
    return r.stdout


def test_bootstrap_tag_stops_before_ha_and_traefik(tmp_path):
    out = _tasks(tmp_path, "--tags", "bootstrap")
    # --list-tasks still prints every play header; only task lines (6-space indent) run.
    tasks = [l.lower() for l in out.splitlines() if l.startswith("      ") and l.strip()]
    assert any("initialize docker swarm" in t for t in tasks)
    assert not any("keepalived" in t or "acme" in t for t in tasks)


def test_no_tags_still_runs_everything(tmp_path):
    out = _tasks(tmp_path).lower()
    assert "initialize docker swarm" in out and "keepalived" in out and "acme" in out
