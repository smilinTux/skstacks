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
